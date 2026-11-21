"""macOS: the Podman volume of Cairn's relay (sandbox-ai-cairn-RUN, cairn.relay_volume) of a launcher killed with
SIGKILL. Analysis round 5 made the ended run's cleanup (net.forget_ended, which `sandbox-ai list` and gc run) remove
it - verified here: it does (tests/test_analysis_round_5_xplat.py has its unit test). But only once, and without
--force: when Podman refused it then (a container of the run's pod still used it, Podman did not answer), the run's
directory went all the same, and nothing named the volume any more - it stayed in podman machine's VM for good.
`sandbox-ai gc` now looks at the volumes themselves (liveness.leftover_cairn_volumes): one of a run that is not
alive goes, still without --force (one a container uses stays for a later gc), one of a live run stays. End to end
through the real ./sandbox-ai (tests/live_fakes.py: the fake podman's volumes)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import live_fakes as F  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_cairn_volume_gc", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


liveness = load("liveness")
DEAD, LIVE = "claude-0123abcd", "claude-4567cdef"


def fake_podman(monkeypatch, volumes, in_use=(), answers=True):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if not answers:
            raise subprocess.TimeoutExpired(argv, 30)
        if argv[1:3] == ["volume", "ls"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(volumes) + "\n", stderr="")
        if argv[1:3] == ["volume", "rm"]:
            used = argv[-1] in in_use
            return SimpleNamespace(returncode=2 if used else 0, stdout="", stderr="volume is being used" if used else "")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(liveness, "RUNNER", runner)
    return calls


def test_the_volumes_of_ended_runs_go_and_only_those(monkeypatch):
    volumes = [f"sandbox-ai-cairn-{DEAD}", f"sandbox-ai-cairn-{LIVE}", "sandbox-ai-lend-claude-0123abcd",
               "my-data", "sandbox-ai-cairn-", "sandbox-ai-cairn-.hidden", "sandbox-ai-cairn-" + "a" * 90]
    calls = fake_podman(monkeypatch, volumes)
    assert liveness.leftover_cairn_volumes(lambda run: run == LIVE) == [DEAD]
    removed = [call for call in calls if call[1:3] == ["volume", "rm"]]
    assert removed == [["podman", "volume", "rm", f"sandbox-ai-cairn-{DEAD}"]], "never --force, never another"


def test_a_volume_a_container_still_uses_stays_for_a_later_gc(monkeypatch):
    fake_podman(monkeypatch, [f"sandbox-ai-cairn-{DEAD}"], in_use={f"sandbox-ai-cairn-{DEAD}"})
    assert liveness.leftover_cairn_volumes(lambda run: False) == []


def test_a_dry_run_names_them_and_removes_nothing(monkeypatch):
    calls = fake_podman(monkeypatch, [f"sandbox-ai-cairn-{DEAD}"])
    assert liveness.leftover_cairn_volumes(lambda run: False, remove=False) == [DEAD]
    assert not [call for call in calls if call[1:3] == ["volume", "rm"]]


def test_podman_that_does_not_answer_leaves_everything(monkeypatch):
    fake_podman(monkeypatch, [f"sandbox-ai-cairn-{DEAD}"], answers=False)
    assert liveness.leftover_cairn_volumes(lambda run: False) == []

    def failing(argv, **kwargs):
        return SimpleNamespace(returncode=125, stdout="", stderr="cannot connect")
    monkeypatch.setattr(liveness, "RUNNER", failing)
    assert liveness.leftover_cairn_volumes(lambda run: False) == []
    monkeypatch.setattr(liveness, "RUNNER", lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("podman")))
    assert liveness.leftover_cairn_volumes(lambda run: False) == []


def test_a_run_that_cannot_be_judged_is_left_alone(monkeypatch):
    calls = fake_podman(monkeypatch, [f"sandbox-ai-cairn-{DEAD}"])

    def unknown(run):
        raise OSError("cannot tell")
    assert liveness.leftover_cairn_volumes(unknown) == []
    assert not [call for call in calls if call[1:3] == ["volume", "rm"]]


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_gc_removes_the_relay_volume_a_killed_launchers_cleanup_could_not(tmp_path):
    host = F.Host(tmp_path)
    store = host.store
    store.mkdir(parents=True, exist_ok=True)
    volume = f"sandbox-ai-cairn-{DEAD}"
    (store / "fake-volumes.json").write_text(json.dumps([volume, "my-data"]))
    # The killed launcher's run, ended: the first cleanup (sandbox-ai list) cannot remove its volume - a container of
    # its pod still used it - and its directory goes all the same.
    runs = host.runtime / "sandbox-ai/runs"
    runs.mkdir(parents=True, exist_ok=True)
    directory = runs / DEAD
    directory.mkdir()
    record = {"run": DEAD, "pid": 2 ** 22 + 7, "pid_start": 1, "pid_ns": liveness.pid_namespace(),
              "backend": "podman", "pod": f"agent-{DEAD}",
              "container": f"agent-{DEAD}-agent", "cairn": {"container": f"agent-{DEAD}-service-cairn"}}
    (directory / "run.json").write_text(json.dumps(record))
    listed = host.run("list", "-a", env={"FAKE_PODMAN_VOLUME_IN_USE": volume})
    assert listed.returncode == 0, listed.stderr[-2000:]
    assert not directory.exists(), "the run's directory went"
    assert volume in json.loads((store / "fake-volumes.json").read_text()), "the volume stayed: nothing names it now"
    # gc finds it all the same.
    dry = host.run("gc", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr[-2000:]
    assert json.loads(dry.stdout)["cairn_volumes"] == [DEAD]
    done = host.run("gc")
    assert done.returncode == 0, done.stderr[-2000:]
    assert f"removed the Cairn relay volume of 1 ended run(s): {DEAD}" in done.stdout, done.stdout
    assert json.loads((store / "fake-volumes.json").read_text()) == ["my-data"]
    assert "nothing to remove" in host.run("gc").stdout
