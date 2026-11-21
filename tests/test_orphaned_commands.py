"""What the commands for a running sandbox do with an orphaned one (2026-11-16, after test_supervisor_gone.py): a run
whose supervisor (the launcher on the host) is gone while its Podman pod runs on keeps its run directory and its
control socket now - the sandbox's own supervisor (runtime.py, PID 1) still listens there. Before, every command named
it "does not run now (orphaned; see sandbox-ai list)" and did nothing, the kill switch too. Now:

- the kill switch reaches it (`net cut NAME MODE`, `net cut --all-runs MODE`: the app's emergency stop): through its
  control socket when that answers and confirms; else its pod is stopped - fail closed - said and recorded; never a
  success that is not confirmed. Lifting a cut (off) is refused: nothing would hold what it gives back;
- what only reads (`net show`, `approvals list`, `usage`) works where the data is there, marked as orphaned;
- what widens access (`net allow`, `workspace add`, `approvals allow`, ...) is refused with the precise reason (its
  supervisor's end as recorded) and what to do (stop, then resume): nothing supervises the run;
- every other command says the same precise reason instead of "does not run now (orphaned; ...)".

Podman is the fake of live_fakes.py on PATH (every module instance of the commands sees the same); the sandbox's
supervisor is runtime.control_server on a real Unix socket."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from live_fakes import Host

ROOT = Path(__file__).resolve().parents[1]
WHY = "Podman could not remove its pod (no space left on device)"


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_orphaned", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


net = load("net")
rt = net.launcher().RUNTIME


def dead_pid():
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    child.wait()
    return child.pid


class Orphans:
    """Runs registered in a short runtime directory (socket paths: at most 107 bytes) whose launcher is gone while
    the fake Podman lists their pods running: orphaned. Each with its sandbox's supervisor (a real control_server and
    RuntimeNetwork with the kill switch) unless SUPERVISOR is False (its socket gone with it)."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.host = Host(tmp_path / "host")
        self.short = Path(tempfile.mkdtemp(prefix="orph-", dir="/tmp"))
        for key in ("PATH", "FAKE_PODMAN_LOG", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_CACHE_HOME"):
            monkeypatch.setenv(key, self.host.env[key])
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(self.short))
        monkeypatch.setenv("FAKE_PODMAN_GRAPHROOT", str(self.host.store))
        monkeypatch.setenv("FAKE_PODMAN_POD_LIVE", "1")
        monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
        net.liveness()._cache.clear()
        self.host.store.mkdir(parents=True, exist_ok=True)
        self.networks, self.stops = {}, []

    def pods(self):
        try:
            return json.loads((self.host.store / "fake-pods.json").read_text())
        except OSError:
            return {}

    def add(self, run, supervisor=True, alive=False, ended=True, **fields):
        directory = net.own_runs_dir() / run
        directory.mkdir(parents=True)
        session = self.tmp / f"session-{run}"
        session.mkdir()
        record = {"run": run, "name": run, "agent": "claude", "backend": "podman", "pod": f"agent-{run}",
                  "pid": os.getpid() if alive else dead_pid(), "pid_ns": net.liveness().pid_namespace(),
                  "workspace": str(self.tmp), "session_dir": str(session), "started": "2026-11-16T09:40:00Z",
                  "net_cut": 1, **fields}
        (directory / "run.json").write_text(json.dumps(record))
        if not alive:
            self.host.store.mkdir(parents=True, exist_ok=True)
            (self.host.store / "fake-pods.json").write_text(json.dumps({**self.pods(), f"agent-{run}": {}}))
            for live in (net.liveness(), net.starts_module().module("liveness")):
                live._cache.clear()  # (Podman's listing is kept 2 s: a command of the test's before saw no such pod)
            if ended:
                net.liveness().record_supervisor_end(directory, WHY, "launcher", run=run, pod=f"agent-{run}",
                                                     cut_error="no answer")
        if supervisor:
            network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
            network.cut_settings = rt.cut_settings({"host_ports": [], "api": {"https": ["192.0.2.10"], "tcp": [],
                                                                             "loopback": [], "names": []},
                                                    "tor": False, "bypass": [], "machine_host": None})
            stop = threading.Event()
            thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock", network, stop),
                                      daemon=True)
            thread.start()
            self.stops.append((stop, thread))
            self.networks[run] = network
            deadline = time.monotonic() + 5
            while not (directory / "control.sock").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            rt.starting_cuts(network, directory)
        return directory

    def calls(self):
        return self.host.calls()

    def close(self):
        for stop, thread in self.stops:
            stop.set()
            thread.join(5)
        shutil.rmtree(self.short, ignore_errors=True)


@pytest.fixture
def orphans(tmp_path, monkeypatch):
    made = Orphans(tmp_path, monkeypatch)
    yield made
    made.close()


def run_net(*argv):
    lines = []
    status = net.main(list(argv), out=lines.append)
    return status, lines


def launcher_log(orphans):
    path = Path(orphans.host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log"
    return path.read_text() if path.exists() else ""


# --- the kill switch reaches an orphaned run ------------------------------------------------------------------------


def test_net_cut_reaches_an_orphaned_run_through_its_control_socket(orphans, capsys):
    """Before: "claude-0e7d6eda does not run now (orphaned; see sandbox-ai list)" - and nothing was cut."""
    directory = orphans.add("claude-0e7d6eda")
    status, lines = run_net("cut", "claude-0e7d6eda", "api")
    assert status == 0, capsys.readouterr().err
    assert orphans.networks["claude-0e7d6eda"].cut_mode == "api"  # its supervisor in the sandbox set it
    assert json.loads((directory / "cut.json").read_text())["mode"] == "api"
    text = "\n".join(lines)
    assert "claude-0e7d6eda: kill switch api: only its model API stays reachable" in text
    assert "orphaned" in text and WHY in text and "sandbox-ai stop claude-0e7d6eda" in text
    assert "undo:" not in text  # a cut of an orphaned run is not lifted (see below)
    assert not any(call[:2] == ["pod", "stop"] for call in orphans.calls())  # cut, not stopped: the work is saved first
    # As JSON (the app): the run's result, marked orphaned.
    status, lines = run_net("cut", "claude-0e7d6eda", "all", "--json")
    [entry] = json.loads("\n".join(lines))["runs"]
    assert status == 0 and entry["ok"] is True and entry["cut"] == "all" and entry["orphaned"] is True
    assert entry["stopped"] is False and orphans.networks["claude-0e7d6eda"].cut_mode == "all"


def test_net_cut_without_a_control_socket_stops_the_pod_says_so_and_records_it(orphans, capsys):
    directory = orphans.add("claude-0e7d6eda", supervisor=False)
    status, lines = run_net("cut", "claude-0e7d6eda", "all")
    assert status == 0, capsys.readouterr().err
    text = "\n".join(lines)
    assert "claude-0e7d6eda: the kill switch could not reach it" in text and "does not answer" in text
    assert "so its pod agent-claude-0e7d6eda was stopped: nothing of it runs now" in text
    assert ["pod", "stop", "--time", str(net.ORPHAN_STOP_WAIT), "agent-claude-0e7d6eda"] in orphans.calls()
    assert "agent-claude-0e7d6eda" not in orphans.pods()  # confirmed: Podman lists nothing of it running
    log = launcher_log(orphans)
    assert "kill switch all for the orphaned claude-0e7d6eda" in log and "its pod agent-claude-0e7d6eda was stopped" \
        in log
    history = net.starts_module().history("claude-0e7d6eda")
    assert any(entry["event"] == "stop" and entry.get("by") == "kill switch" for entry in history)
    assert not directory.exists()  # it has ended: its end recorded, its directory gone (net.forget_ended)


def test_net_cut_never_says_done_when_neither_the_cut_nor_the_stop_is_confirmed(orphans, monkeypatch, capsys):
    orphans.add("claude-0e7d6eda", supervisor=False)
    monkeypatch.setenv("FAKE_PODMAN_POD_RM_FAIL", "125:Error: no space left on device")  # its pod runs on
    status, lines = run_net("cut", "claude-0e7d6eda", "all", "--json")
    [entry] = json.loads("\n".join(lines))["runs"]
    assert status == 1 and entry["ok"] is False and entry["stopped"] is False and entry["cut"] is None
    assert "could not be stopped" in entry["error"] and "podman pod rm --force agent-claude-0e7d6eda" in entry["error"]
    status, lines = run_net("cut", "claude-0e7d6eda", "all")
    assert status == 1 and lines[0].startswith("claude-0e7d6eda: NOT cut")


def test_lifting_the_cut_of_an_orphaned_run_is_refused(orphans, capsys):
    orphans.add("claude-0e7d6eda")
    assert run_net("cut", "claude-0e7d6eda", "all")[0] == 0
    status, _ = run_net("cut", "claude-0e7d6eda", "off")
    err = capsys.readouterr().err
    assert status == 1 and "claude-0e7d6eda is orphaned: nothing supervises it, so net cut off is refused" in err
    assert WHY in err and "sandbox-ai stop claude-0e7d6eda, then: sandbox-ai resume claude-0e7d6eda" in err
    assert orphans.networks["claude-0e7d6eda"].cut_mode == "all"


def test_all_runs_reaches_the_orphaned_runs_too(orphans, capsys):
    """The app's emergency stop (net cut --all-runs all --json on every host): the running run, the orphan that
    answers and the orphan whose socket is gone - each one's result in the one report."""
    orphans.add("claude-11111111", alive=True)
    orphans.add("claude-22222222")
    orphans.add("claude-33333333", supervisor=False)
    status, lines = run_net("cut", "--all-runs", "all", "--json")
    report = json.loads("\n".join(lines))
    by_run = {entry["run"]: entry for entry in report["runs"]}
    assert status == 0, (report, capsys.readouterr().err)
    assert by_run["claude-11111111"]["ok"] and by_run["claude-11111111"]["cut"] == "all"
    assert not by_run["claude-11111111"].get("orphaned")
    assert by_run["claude-22222222"]["ok"] and by_run["claude-22222222"]["cut"] == "all"
    assert by_run["claude-22222222"]["orphaned"] is True
    assert by_run["claude-33333333"]["ok"] and by_run["claude-33333333"]["stopped"] is True
    assert all(network.cut_mode == "all" for network in orphans.networks.values())
    # Lift every cut: the running one is lifted; an orphan's cut stays, said.
    status, lines = run_net("cut", "--all-runs", "off")
    assert status == 1 and "claude-11111111: kill switch off: the cut is lifted: it reaches again what it may " \
        "reach" in lines
    assert any(line.startswith("claude-22222222: NOT lifted: claude-22222222 is orphaned") for line in lines)
    assert orphans.networks["claude-11111111"].cut_mode is None
    assert orphans.networks["claude-22222222"].cut_mode == "all"


def test_all_runs_without_orphans_says_as_before(orphans):
    orphans.add("claude-11111111", alive=True)
    status, lines = run_net("cut", "--all-runs", "all")
    assert status == 0 and lines == ["claude-11111111: kill switch all: everything is cut, its model API too",
                                     "undo: sandbox-ai net cut --all-runs off"]


# --- what only reads ------------------------------------------------------------------------------------------------


def test_net_show_works_on_an_orphaned_run_marked_as_such(orphans, capsys):
    directory = orphans.add("claude-0e7d6eda")
    assert run_net("cut", "claude-0e7d6eda", "api")[0] == 0
    status, lines = run_net("show", "claude-0e7d6eda")
    assert status == 0, capsys.readouterr().err
    text = "\n".join(lines)
    assert lines[0].startswith("claude-0e7d6eda is orphaned: ") and WHY in lines[0]
    assert "kill switch: api" in text and "net cut claude-0e7d6eda off" not in text  # (refused: no hint to it)
    status, lines = run_net("show", "claude-0e7d6eda", "--json")
    shown = json.loads("\n".join(lines))
    assert status == 0 and shown["orphaned"] is True and WHY in shown["orphaned_why"] and shown["answered"] is True
    assert shown["cut"] == "api"
    # Its socket gone: what its run directory recorded, said as such.
    (directory / "names.json").write_text(json.dumps({"example.org": {"addresses": ["192.0.2.7"]}}))
    orphans.stops[0][0].set()
    orphans.stops[0][1].join(5)
    (directory / "control.sock").unlink(missing_ok=True)
    status, lines = run_net("show", "claude-0e7d6eda", "--json")
    shown = json.loads("\n".join(lines))
    assert status == 0 and shown["answered"] is False and shown["cut"] == "api"
    assert [entry["names"] for entry in shown["https"]] == [["example.org"]]
    status, lines = run_net("show", "claude-0e7d6eda")
    assert status == 0 and "its supervisor did not answer" in "\n".join(lines)


def test_approvals_list_shows_an_orphans_questions_and_allow_is_refused(orphans, capsys):
    approvals = load("approvals")
    directory = orphans.add("claude-0e7d6eda")
    (directory / "approvals").mkdir()
    question = {"id": "1763286000-1-1", "kind": "dns", "name": "example.org", "time": "2026-11-16T09:50:00Z"}
    (directory / "approvals" / "1763286000-1-1.json").write_text(json.dumps(question))
    lines = []
    assert approvals.main(["list", "--json"], out=lines.append) == 0
    [listed] = json.loads("\n".join(lines))
    assert listed["id"] == "1763286000-1-1" and listed["orphaned"] is True and WHY in listed["orphaned_why"]
    lines = []
    assert approvals.main(["list"], out=lines.append) == 0
    assert any("(orphaned)" in line for line in lines)
    assert any(line.startswith("claude-0e7d6eda is orphaned: ") for line in lines)
    assert approvals.main(["allow", "1763286000-1-1"], out=lines.append) == 1
    err = capsys.readouterr().err
    assert "claude-0e7d6eda is orphaned: nothing supervises it, so approvals allow is refused" in err
    assert "sandbox-ai stop claude-0e7d6eda, then: sandbox-ai resume claude-0e7d6eda" in err
    assert not (directory / "approvals" / "1763286000-1-1.answer").exists()
    # A deny only takes away: it is written for the sandbox's supervisor to apply.
    assert approvals.main(["deny", "1763286000-1-1"], out=lines.append) == 0
    assert json.loads((directory / "approvals" / "1763286000-1-1.answer").read_text())["decision"] == "deny"


def test_usage_works_on_an_orphaned_run_marked_as_such(orphans, capsys):
    usage = load("usage")
    orphans.add("claude-0e7d6eda")
    orphans.add("claude-11111111", alive=True)
    lines = []
    assert usage.main(["claude-0e7d6eda", "--json", "--interval", "0.1"], out=lines.append) == 0, \
        capsys.readouterr().err
    found = json.loads("\n".join(lines))
    assert found["run"] == "claude-0e7d6eda" and found["orphaned"] is True and WHY in found["orphaned_why"]
    lines = []
    assert usage.main(["claude-0e7d6eda", "--interval", "0.1"], out=lines.append) == 0
    assert lines[0].startswith("claude-0e7d6eda (orphaned: ") and WHY in lines[0]
    lines = []
    assert usage.main(["--all", "--json", "--interval", "0.1"], out=lines.append) == 0
    assert {entry["run"]: entry.get("orphaned") for entry in json.loads("\n".join(lines))} == \
        {"claude-0e7d6eda": True, "claude-11111111": False}


# --- what widens access: refused, precisely --------------------------------------------------------------------------


@pytest.mark.parametrize("command", [["net", "allow", "claude-0e7d6eda", "--allow-host", "example.org"],
                                     ["net", "untaint", "claude-0e7d6eda"],
                                     ["workspace", "add", "claude-0e7d6eda", "WORKSPACE"]])
def test_what_widens_access_is_refused_with_the_reason_and_the_fix(orphans, command, capsys):
    orphans.add("claude-0e7d6eda")
    (orphans.tmp / "more").mkdir()
    argv = [str(orphans.tmp / "more") if word == "WORKSPACE" else word for word in command[1:]]
    module = net if command[0] == "net" else load("workspaces")
    assert module.main(argv, out=lambda line: None) == 1
    err = capsys.readouterr().err
    assert f"claude-0e7d6eda is orphaned: nothing supervises it, so {command[0]} {command[1]} is refused" in err
    assert "fail closed" in err and WHY in err
    assert "sandbox-ai stop claude-0e7d6eda, then: sandbox-ai resume claude-0e7d6eda" in err
    assert "does not run now" not in err


def test_every_other_command_says_the_precise_reason_too(orphans, capsys):
    orphans.add("claude-0e7d6eda")
    assert net.main(["revoke", "claude-0e7d6eda", "--allow-host", "example.org"], out=lambda line: None) == 1
    err = capsys.readouterr().err
    assert "claude-0e7d6eda is orphaned: net revoke needs its supervisor, which is gone" in err and WHY in err
    assert "does not run now" not in err


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------


LIVE = {"FAKE_PODMAN_POD_LIVE": "1"}
NO_SPACE = "Error: removing pod: writing to database: no space left on device"


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest")
    return made


def orphaned_run(host):
    """The user's case: the sandbox ends, Podman cannot remove its pod, which runs on; the run directory is kept."""
    host.run("claude", env={"FAKE_PODMAN_RUN": "1:0", "FAKE_PODMAN_POD_RM_FAIL": f"125:{NO_SPACE}"})
    base = Path(host.env["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs"
    [directory] = [path for path in base.iterdir() if (path / "run.json").exists()]
    return directory, json.loads((directory / "run.json").read_text())


FAKE_SUPERVISOR = r'''
import json, os, socket, sys
os.chdir(sys.argv[1])
cuts = {}
server = socket.socket(socket.AF_UNIX)
server.bind("control.sock")
server.listen(4)
open("cut.ready", "w").close()
print("listening", flush=True)
while True:
    connection, _ = server.accept()
    with connection:
        data = b""
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
        request = json.loads(data)
        with open("requests.jsonl", "a") as log:
            log.write(json.dumps(request) + "\n")
        if request.get("op") == "cut":
            names = [request["by"]] if isinstance(request["by"], str) else request["by"]
            for name in names:
                cuts.pop(name, None) if request["mode"] == "off" else cuts.__setitem__(name, request["mode"])
            answer = {"ok": True, "cut": "all" if "all" in cuts.values() else (sorted(cuts.values()) or [None])[0],
                      "cuts": cuts}
        elif request.get("op") == "show":
            answer = {"ok": True, "nets": [], "https": ["192.0.2.7/32"], "tcp": [], "dns": [], "cut": None}
        else:
            answer = {"ok": False, "error": "op not served here"}
        connection.sendall(json.dumps(answer).encode() + b"\n")
'''


def supervisor(directory):
    """The sandbox's own supervisor, which still runs in the orphaned pod: answers on the run's control socket."""
    child = subprocess.Popen([sys.executable, "-I", "-c", FAKE_SUPERVISOR, str(directory)], stdout=subprocess.PIPE,
                             text=True)
    assert child.stdout.readline().strip() == "listening"
    return child


def test_end_to_end_net_cut_on_an_orphaned_run_with_its_control_socket(host):
    """Before: "NAME does not run now (orphaned; see sandbox-ai list)", nothing cut."""
    directory, record = orphaned_run(host)
    name = record["name"]
    child = supervisor(directory)
    try:
        cut = host.run("net", "cut", name, "all", env=LIVE)
        assert cut.returncode == 0, cut.stderr
        assert f"{name}: kill switch all" in cut.stdout and "orphaned" in cut.stdout
        requests = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        assert requests[-1]["op"] == "cut" and requests[-1]["mode"] == "all"
        assert not any(call[:2] == ["pod", "stop"] for call in host.calls())
        lift = host.run("net", "cut", name, "off", env=LIVE)
        assert lift.returncode == 1 and "so net cut off is refused" in lift.stderr
    finally:
        child.kill()
        child.wait()


def test_end_to_end_net_show_works_and_net_allow_is_refused_on_an_orphaned_run(host):
    directory, record = orphaned_run(host)
    name = record["name"]
    child = supervisor(directory)
    try:
        shown = host.run("net", "show", name, env=LIVE)
        assert shown.returncode == 0, shown.stderr
        assert shown.stdout.startswith(f"{name} is orphaned: ") and "192.0.2.7" in shown.stdout
        allowed = host.run("net", "allow", name, "--allow-host", "example.org", env=LIVE)
        assert allowed.returncode == 1
        assert f"{name} is orphaned: nothing supervises it, so net allow is refused" in allowed.stderr
        assert "no space left on device" in allowed.stderr and f"sandbox-ai stop {name}, then: sandbox-ai resume" \
            in allowed.stderr
        assert not any(json.loads(line)["op"] == "allow"
                       for line in (directory / "requests.jsonl").read_text().splitlines())
    finally:
        child.kill()
        child.wait()


def test_end_to_end_net_cut_on_an_orphaned_run_without_a_control_socket_stops_its_pod(host):
    directory, record = orphaned_run(host)
    name = record["name"]
    cut = host.run("net", "cut", name, "all", env=LIVE)
    assert cut.returncode == 0, cut.stderr
    assert f"so its pod {record['pod']} was stopped: nothing of it runs now" in cut.stdout
    assert ["pod", "stop", "--time", "10", record["pod"]] in host.calls()
    log = (Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log").read_text()
    assert f"its pod {record['pod']} was stopped" in log
    listed = host.run("list", "--json", env=LIVE)
    assert not any(entry.get("state") == "orphaned" for entry in json.loads(listed.stdout))
