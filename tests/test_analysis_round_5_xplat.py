"""Analysis round 5 of 0.1.97 (cross-platform and regressions of older features): what the round found, each with the
test that failed before its fix. End to end through the real ./sandbox-ai where the bug was visible to a user
(tests/live_fakes.py stands in for Podman, tmux and ssh)."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import live_fakes as F  # noqa: E402
import model_mock  # noqa: E402

MODEL = "qwen/qwen3-coder-next"
OTHERS = ("google/gemma-3-27b", "openai/gpt-oss-20b")


def requests(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- a dry run asks no model server (regression of 0.1.96's --llm-models) -----------------------------------------

@pytest.mark.parametrize("kind", ["lmstudio", "ollama", "openai-compatible"])
def test_a_dry_run_of_llm_asks_the_server_nothing(tmp_path, kind):
    """`--dry-run` is "Print plan; no DNS, auth reads, copies, pods or processes", and the app's Board and Start pages
    run it for every preview. Since the server's other models are offered (--llm-models all, 0.1.96), each dry run
    of --llm asked the server for its model list (and Ollama for /api/ps) with a key read from the keyring: up to 3 s
    per preview with a server that does not answer, a DNS lookup and a request with the key for a remote one. A dry
    run uses what is remembered for the server (llm add, llm models); the start itself still asks."""
    log = tmp_path / "mock.jsonl"
    server, port = model_mock.start(models=(MODEL, *OTHERS), lmstudio=kind == "lmstudio" or None,
                                    ollama_ps=kind == "ollama" or None, log=str(log))
    try:
        host = F.Host(tmp_path / "host")
        host.add_image("localhost/opencode-sandbox:latest", "localhost/codex-sandbox:latest",
                       "localhost/qwen-sandbox:latest")
        env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "", "SANDBOX_AI_SECRETS": "file"}
        added = host.run("llm", "add", "s", "--kind", kind, "--url", f"http://127.0.0.1:{port}", "--model", MODEL,
                         env=env)
        assert added.returncode == 0, added.stderr
        before = len(requests(log))
        for agent in ("opencode", "codex", "qwen"):
            dry = host.run(agent, "--llm", "s", "--dry-run", env=env)
            assert dry.returncode == 0, dry.stderr
            assert json.loads(dry.stdout)["llm"]["model"] == MODEL
        asked = requests(log)[before:]
        assert asked == [], f"the dry runs asked the server: {[(r['method'], r['path']) for r in asked]}"
        # What `llm models` remembered is what a dry run offers; still without a request.
        assert host.run("llm", "models", "s", env=env).returncode == 0
        before = len(requests(log))
        dry = host.run("opencode", "--llm", "s", "--dry-run", env=env)
        assert json.loads(dry.stdout)["llm"]["models"] == [MODEL, *OTHERS] and requests(log)[before:] == []
    finally:
        server.shutdown()


# --- macOS: the notes that pointed to --browser, which macOS refuses -----------------------------------------------

def load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_round5", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start(**more):
    from types import SimpleNamespace
    return SimpleNamespace(**{"agent_args": [], "backend": "podman", "podman_url": None, "browser": None,
                              "helper_of": None, "mcp_socket_dir": None, "probe": False, "binary": None, **more})


@pytest.mark.parametrize("args", [[], ["web"]])
def test_dshs_note_on_a_mac_does_not_send_you_to_browser(monkeypatch, args):
    """On a Mac (Podman in podman machine's VM) `--browser` with a browser you see is refused (its live view is
    Linux-only for now), yet DSH's note said "start with --browser" for its web UI. There it says so instead, and
    what works (the chat, a task); on Linux the note is as before."""
    dsh = load("dsh")
    monkeypatch.setattr(sys, "platform", "darwin")
    said = dsh.note(start(agent_args=args), False)
    assert "start with --browser" not in said and "Linux-only for now" in said
    assert "headless" in said if args else "chat in this terminal" in said
    remote = dsh.note(start(agent_args=args, podman_url="ssh://lab/run/podman.sock"), False)
    assert "start with --browser" in remote, "another engine (--podman-url) shows the browser as on Linux"
    monkeypatch.setattr(sys, "platform", "linux")
    assert "start with --browser" in dsh.note(start(agent_args=args), False)


def test_openhands_canvas_note_on_a_mac_does_not_send_you_to_browser(monkeypatch):
    openhands = load("openhands")
    monkeypatch.setattr(sys, "platform", "darwin")
    said = openhands.canvas_note(start())
    assert "start with --browser" not in said and "Linux-only for now" in said and "-- task" in said
    monkeypatch.setattr(sys, "platform", "linux")
    assert "start with --browser" in openhands.canvas_note(start())


# --- a killed launcher's Cairn relay (0.1.97) goes with its run -----------------------------------------------------

def test_a_killed_launchers_cairn_relay_folder_goes_with_its_run(tmp_path):
    """A start with --cairn has the relay's folder RUN.cairn beside its run directory (its socket). A launcher killed
    with SIGKILL left it for good: `sandbox-ai list` (and gc) removed the run directory, its .browser and .browser-vnc
    beside it, but not .cairn. The real ./sandbox-ai, started and held at its pod, then killed."""
    import time
    host = F.Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest")
    host.add_image("localhost/sandbox-ai-cairn:latest", labels={"sandbox-ai.role": "cairn"})
    host.add_image("localhost/sandbox-ai-browser:latest", labels={"sandbox-ai.role": "browser"})
    release = host.directory / "release"
    process = host.popen("claude", "--cairn", "--no-tmux", env={"FAKE_PODMAN_RUN": "0:0",
                                                                "FAKE_PODMAN_POD_HOLD": str(release)})
    runs = host.runtime / "sandbox-ai" / "runs"
    try:
        for _ in range(1200):
            if list(runs.glob("*.cairn")) and any(c[:2] == ["pod", "create"] for c in host.calls()):
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        assert process.poll() is None, process.communicate(timeout=30)
        [relay] = list(runs.glob("*.cairn"))
        process.kill()
        process.communicate(timeout=30)
    finally:
        release.write_text("go")
        if process.poll() is None:
            process.kill()
    assert relay.is_dir()
    done = host.run("list", "-a")
    assert done.returncode == 0, done.stderr
    assert not relay.with_suffix("").exists(), "its run directory went"
    assert not relay.exists(), "the relay's folder of the ended run went with it"


def test_on_a_mac_a_killed_launchers_cairn_relay_volume_goes_with_its_run(tmp_path, monkeypatch):
    """On a Mac the relay's socket lies in a Podman volume of the run (cairn.relay_volume), removed by the launcher's
    cleanup only: a killed launcher left one volume per start in podman machine's VM. The ended run's cleanup
    (net.forget_ended, which `sandbox-ai list` and gc run) removes it - without --force, so a volume a container still
    uses stays - and only for a run with Cairn whose sockets were in the VM."""
    from types import SimpleNamespace
    net = load("net")
    liveness = net.liveness()
    cairn = load("cairn")
    calls = []
    monkeypatch.setattr(liveness, "RUNNER", lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0,
                                                                                                   stdout="", stderr=""))
    monkeypatch.setattr(liveness, "remove_ended_pod", lambda record: False)
    monkeypatch.setattr(net.launcher(), "load_starts_module", lambda: SimpleNamespace(run_event=lambda *a: None))
    record = {"run": "claude-1234abcd", "pid": 7, "pid_start": 1, "backend": "podman", "pod": "agent-claude-1234abcd",
              "container": "agent-claude-1234abcd-agent", "cairn": {"container": "agent-claude-1234abcd-service-cairn"}}
    for given, removed in ((record, True), ({**record, "cairn": None}, False),
                           ({k: v for k, v in record.items() if k != "container"}, False)):
        calls.clear()
        directory = tmp_path / "runs" / "claude-1234abcd"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps(given))
        net.forget_ended(directory, given)
        assert not directory.exists()
        volume = cairn.relay_volume(SimpleNamespace(run_id="claude-1234abcd"))
        assert (["podman", "volume", "rm", volume] in calls) is removed, calls
        assert not any("--force" in call for call in calls)
