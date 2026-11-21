"""`sandbox-ai shell NAME [-- COMMAND ...]`: a shell of the human in a running sandbox, under the agent's rules.

The relay (runtime.relay_shell) runs inside the sandbox's confinement, beside the attach relay; here it runs in this
process against a socket of a temporary folder, with real processes, ptys and pipes. The host's side
(runtime.shell_client, sessions.shell, starts.shell) only pipes frames: only process boundaries are faked (a /proc
tree, podman exec on macOS)."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_shell_test")
st = load("starts", "sandbox_starts_shell_test")
sessions = st.module("sessions")
launcher = st.launcher()
activity = load("activity", "sandbox_activity_shell_test")


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(st.module("net"), "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(sessions.sys.stdin, "isatty", lambda: False)


def relay(tmp_path, name="shell.sock"):
    """A shell relay serving in this process, its working directory TMP_PATH/work; its socket's path."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    path = tmp_path / name
    threading.Thread(target=rt.shell_relay, args=(str(path), str(work)), daemon=True).start()
    for _ in range(250):
        if path.exists():
            break
        time.sleep(0.02)
    return str(path)


# -- the relay (its contracts: tests/test_agent_sandbox_contracts.py) ----------------------------------------------

def test_the_shell_ends_with_its_client(tmp_path):
    """The client gone (the terminal closed), what the shell started ends too: no shell of the human lives on."""
    path = relay(tmp_path)
    marker = tmp_path / "work" / "pid"
    client = socket.socket(socket.AF_UNIX)
    client.connect(path)
    request = json.dumps({"argv": ["sh", "-c", f"echo $$ > {shlex.quote(str(marker))}; exec sleep 300"],
                          "tty": False}).encode()
    client.sendall(rt.attach_frame(b"c", request))
    for _ in range(250):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.02)
    pid = int(marker.read_text())
    client.close()
    for _ in range(250):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        with open(f"/proc/{pid}/stat") as stream:  # a zombie of the relay's thread counts as ended
            if stream.read().split(") ")[1].startswith("Z"):
                break
        time.sleep(0.02)
    else:
        pytest.fail("the shell's process outlived its client")


def test_the_relay_starts_inside_the_sandbox_with_the_attach_relay():
    detached = rt.tmux_command(["claude"], detached=True)
    script = detached[2]
    assert "--internal-attach-relay /sandbox/tmux/attach.sock" in script
    assert "\n/usr/bin/python3 -I -S /sandbox/runtime.py --internal-shell-relay /sandbox/tmux/shell.sock &\n" in script
    # It ends with the tmux session, as the attach relay does (the guard's account of the end, 0.1.97).
    assert 'kill "$shells" 2>/dev/null' in script.splitlines()
    # Only after tmux is up, and before the loop that keeps the guard alive.
    assert script.index("new-session") < script.index("--internal-shell-relay") < script.index("while tmux")
    assert rt.SHELL_SOCKET == "/sandbox/tmux/shell.sock" and launcher.SHELL_SOCKET == rt.SHELL_SOCKET
    source = (ROOT / "sandbox" / "runtime.py").read_text()
    assert 'sys.argv[1] == "--internal-shell-relay"' in source and 'sys.argv[1] == "--internal-shell-client"' in source


def test_the_relay_and_client_through_the_entry_points(tmp_path):
    """runtime.py --internal-shell-relay and --internal-shell-client, as the sandbox and podman exec (macOS) run
    them."""
    work = tmp_path / "work"
    work.mkdir()
    path = tmp_path / "shell.sock"
    runtime = str(ROOT / "sandbox" / "runtime.py")
    server = subprocess.Popen([sys.executable, "-I", "-S", runtime, "--internal-shell-relay", str(path)], cwd=work)
    try:
        for _ in range(250):
            if path.exists():
                break
            time.sleep(0.02)
        done = subprocess.run([sys.executable, "-I", "-S", runtime, "--internal-shell-client", str(path), "pipe",
                               "sh", "-c", "echo out; echo err >&2; exit 5"], capture_output=True, timeout=30)
        assert (done.returncode, done.stdout, done.stderr) == (5, b"out\n", b"err\n")
    finally:
        server.kill()
        server.wait()


# -- sessions.shell and the command ------------------------------------------------------------------------------

def fake_process(tmp_path, pid, cmdline):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(str(part)) for part in cmdline) + b"\0")
    (entry / "stat").write_text(f"{pid} (python3) S 1 1 1 0 -1\n")


def make_start(key, tmp_path, pid=None, paused=False, sockets=("attach.sock", "shell.sock"), **fields):
    directory = sessions.session_dir(key)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    for name in sockets:
        (directory / "tmux" / name).touch()
    data = {"agent": "codex", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": "/srv/app", "cwd": "/", "created": "2026-11-01T10:00:00Z", "pod": f"agent-session-{key}",
            "name": key, **fields}
    if pid:
        data.update(pid=pid, started="2026-11-02T09:30:00Z")
        fake_process(tmp_path, pid, ["python3", "agent_sandbox.py", "--tmux-socket-dir", directory / "tmux"])
    if paused:
        (directory / "paused").touch()
    sessions.save(key, data)
    return data


@pytest.fixture
def recorded(monkeypatch):
    """The timeline's events (sessions.activity_event) and the client's calls."""
    events, calls = [], []
    monkeypatch.setattr(sessions, "activity_event",
                        lambda name, event, **details: events.append((name, event, details)))
    monkeypatch.setattr(launcher, "shell_client", lambda path, argv, tty, **_: calls.append((path, argv, tty)) or 6)
    return SimpleNamespace(events=events, calls=calls)


def test_a_shell_in_a_running_start(scenario, tmp_path, recorded, capsys):
    make_start("web", tmp_path, pid=41)
    argv = {"happy": ["shell", "web"], "sad": ["shell", "web", "-n", "--", "git", "status", "-s"],
            "edge": ["shell", "web", "--", "sh", "-c", "exit 3", "--", "-n"]}[scenario]
    assert st.main(argv) == 6  # the command's exit status, passed on
    command = {"happy": [], "sad": ["git", "status", "-s"], "edge": ["sh", "-c", "exit 3", "--", "-n"]}[scenario]
    assert recorded.calls == [(str(sessions.session_dir("web") / "tmux" / "shell.sock"), command, False)]
    assert recorded.events == [("web", "shell", {"command": command}), ("web", "shell-end", {"status": 6})]


def test_where_no_shell_is_opened(scenario, tmp_path, recorded, capsys):
    if scenario == "happy":
        make_start("web", tmp_path, pid=41, paused=True)
        assert st.main(["shell", "web"]) == 1
        assert "is paused; run: sandbox-ai resume web" in capsys.readouterr().err
    elif scenario == "sad":
        make_start("old", tmp_path, pid=44, sockets=("attach.sock",))  # started before 0.1.97: no shell relay
        assert st.main(["shell", "old"]) == 1
        err = capsys.readouterr().err
        assert "sandbox-ai attach old" in err and '"shell"' in err and "sandbox-ai resume old --restart" in err
    else:
        assert st.main(["shell", "nothing"]) == 1
        assert "nothing named nothing" in capsys.readouterr().err
        make_start("web", tmp_path, pid=41)
        assert st.main(["shell", "web", "ls", "-l"]) == 2  # a command goes after --
        assert "sandbox-ai shell NAME -- ls -l" in capsys.readouterr().err
    assert recorded.calls == []


def test_a_foreground_start_has_no_shell(tmp_path, monkeypatch, recorded):
    make_start("task", tmp_path, pid=45, mode="foreground")
    monkeypatch.setattr(sessions, "status", lambda name, data: "running")
    with pytest.raises(ValueError, match="runs without tmux .*no shell relay"):
        sessions.shell("task")
    assert recorded.calls == [] and recorded.events == []


def test_an_mcp_server_or_a_plain_run_has_no_shell(tmp_path, monkeypatch, recorded):
    monkeypatch.setattr(st, "find", lambda name, found=None: {"name": name, "id": f"mcp-{name}", "kind": "mcp",
                                                               "state": "running", "directory": None})
    with pytest.raises(ValueError, match="MCP server sandbox"):
        st.shell("demo")
    monkeypatch.setattr(st, "find", lambda name, found=None: {"name": name, "id": name, "kind": "run",
                                                               "state": "running", "directory": None})
    with pytest.raises(ValueError, match="runs without tmux"):
        st.shell("claude-1234abcd")
    assert recorded.calls == []


def test_a_shell_on_macos_runs_the_client_in_the_container(tmp_path, monkeypatch, recorded):
    monkeypatch.setattr(sys, "platform", "darwin")
    make_start("web", tmp_path, pid=41, sockets=(), shell_relay=1)  # the sockets are in podman machine's VM
    seen = []
    monkeypatch.setattr(sessions.subprocess, "run", lambda command, **k: seen.append(command) or
                        SimpleNamespace(returncode=4))
    assert sessions.shell("web", ["make", "test"], tty=True) == 4
    assert seen == [["podman", "exec", "--interactive", "--tty", "--user=0:0", "agent-session-web-agent",
                     "/usr/bin/python3", "-I", "-S", "/sandbox/runtime.py", "--internal-shell-client",
                     rt.SHELL_SOCKET, "tty", "make", "test"]]
    assert sessions.shell_machine({"pod": "agent-x"}, [], False, runner=lambda c, **k: seen.append(c) or
                                  SimpleNamespace(returncode=0)) == 0
    assert "--tty" not in seen[-1] and seen[-1][-2:] == [rt.SHELL_SOCKET, "pipe"]
    assert recorded.calls == []  # no client on the Mac itself


def test_a_start_of_an_older_sandbox_ai_on_macos_is_told_so(tmp_path, monkeypatch, recorded):
    """On a Mac the sockets are in podman machine's VM, so the Linux check (shell.sock beside attach.sock) cannot see
    a start made before 0.1.97: podman exec ran the old runtime.py (\"internal entry point; use sandbox-ai\") or found
    no relay. The session's record says whether its start made the relay (analysis round 5)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    make_start("old", tmp_path, pid=44, sockets=())
    seen = []
    monkeypatch.setattr(sessions.subprocess, "run", lambda command, **k: seen.append(command) or
                        SimpleNamespace(returncode=0))
    with pytest.raises(ValueError, match="before 0.1.97.*sandbox-ai resume old --restart"):
        sessions.shell("old")
    assert seen == [] and recorded.calls == []


def test_a_session_start_records_its_shell_relay(tmp_path, monkeypatch):
    """Every start of a session (start, resume, rebirth) by this sandbox-ai has the relay: its record says so."""
    data = make_start("web", tmp_path)
    monkeypatch.setattr(sessions, "effective", lambda data: (None, None))
    monkeypatch.setattr(sessions, "clear_stale_pod", lambda name, data: None)

    class Started(Exception):
        pass

    def popen(command, **kwargs):
        raise Started()
    monkeypatch.setattr(sessions.subprocess, "Popen", popen)
    with pytest.raises(Started):
        sessions.launch("web", data, [])
    assert data["shell_relay"] == sessions.SEALED_RELAYS == 2  # (0.1.117: its relays lie in tmux/relay)


def test_a_real_shell_through_the_command(tmp_path, monkeypatch):
    """sandbox-ai shell NAME -- COMMAND end to end on this machine: a running start whose relay serves for real."""
    make_start("web", tmp_path, pid=41, sockets=("attach.sock",))
    relay(tmp_path / "state" / "sandbox-ai" / "sessions" / "web" / "tmux")
    events = []
    monkeypatch.setattr(sessions, "activity_event", lambda name, event, **details: events.append((event, details)))
    out_r, out_w = os.pipe()
    saved = os.dup(1)
    os.dup2(out_w, 1)
    try:
        status = st.main(["shell", "web", "-n", "--", "sh", "-c", "echo inside; exit 9"])
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(out_w)
    assert status == 9 and os.read(out_r, 4096) == b"inside\n"
    assert events == [("shell", {"command": ["sh", "-c", "echo inside; exit 9"]}), ("shell-end", {"status": 9})]


def test_the_timeline_says_the_human_opened_a_shell(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    runs = tmp_path / "runs"
    (runs / "session-dev").mkdir(parents=True)
    (runs / "session-dev" / "run.json").write_text(json.dumps({"run": "session-dev", "agent": "codex",
                                                               "log_dir": str(logs)}))
    assert activity.session_event("dev", "shell", runs) is True
    assert activity.session_event("dev", "shell", runs, command=["curl", "-H", "Authorization: Bearer abcdefgh12345678",
                                                                 "https://example.org"]) is True
    assert activity.session_event("dev", "shell-end", runs, status=2) is True
    records = activity.read_records(logs / "activity.jsonl")
    assert [r["kind"] for r in records] == ["session"] * 3
    assert records[0]["summary"] == "session dev shell opened by the human"
    assert records[0]["details"] == {"event": "shell", "command": "", "by": "human"}
    assert records[1]["summary"].startswith("session dev: the human ran in its sandbox: curl -H")
    assert "abcdefgh12345678" not in json.dumps(records[1])  # a credential in the command is redacted
    assert records[2]["summary"] == "session dev shell closed (exit status 2)"
    assert records[2]["details"]["exit_code"] == 2


def test_tab_completion_and_help_name_the_command():
    completion = load("completion", "sandbox_completion_shell_test")
    assert completion.NAMED[("shell", None, "name")] is completion.start_names
    assert "shell" in completion.complete(["sh"])
    assert {"-n", "--no-tty"} <= set(completion.complete(["shell", "-"]))
    main = (ROOT / "sandbox" / "main.py").read_text()
    assert '"shell": ("starts.py", "shell")' in main and '("shell", "Your shell in a running start' in main
    assert "sandbox-ai shell NAME [-n] [-- COMMAND ...]" in (ROOT / "sandbox" / "starts.py").read_text()
    assert "shell" in st.parser()._subparsers._group_actions[0].choices
