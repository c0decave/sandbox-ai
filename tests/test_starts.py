"""Happy/sad/edge contracts for the registry of every start (sandbox-ai list, rename, attach, pause, resume,
stop, delete, gc and the history); only process boundaries are faked (a /proc tree, the backend)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_starts_test", ROOT / "sandbox/starts.py")
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)
sessions = st.module("sessions")
net = st.module("net")
mcp = st.module("mcp")
launcher = st.launcher()


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
    for loaded in (sessions, mcp):
        monkeypatch.setattr(loaded, "PROC", tmp_path / "proc")
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(sessions.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sessions, "DETACH_GRACE", 0)
    monkeypatch.setattr(sessions, "ENDING_WAIT", 0)


def fake_process(tmp_path, pid, cmdline, start=None):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(str(part)) for part in cmdline) + b"\0")
    tail = " 0" * 13 + f" {start}" if start is not None else ""
    (entry / "stat").write_text(f"{pid} (python3) S 1 1 1 0 -1{tail}\n")


def make_start(key, tmp_path=None, pid=None, paused=False, **fields):
    """A start directory as sessions.py writes it; with PID a running supervisor (a fake /proc entry)."""
    directory = sessions.session_dir(key)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "codex", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": "/srv/app", "cwd": "/", "created": "2026-11-01T10:00:00Z", "pod": f"agent-session-{key}",
            **fields}
    if pid:
        data.update(pid=pid, started="2026-11-02T09:30:00Z")
        fake_process(tmp_path, pid, ["python3", "agent_sandbox.py", "--tmux-socket-dir", directory / "tmux"])
    if paused:
        (directory / "paused").touch()
    sessions.save(key, data)
    return data


def register(run, pid=None, **fields):
    """A run.json of a running launcher (this test process: alive for net.running())."""
    directory = launcher.runs_dir() / run
    directory.mkdir(parents=True)
    record = {"run": run, "agent": "claude", "backend": "podman", "workspace": "/srv/other", "pid": pid or os.getpid(),
              "started": "2026-11-03T08:00:00Z", "session": None, **fields}
    (directory / "run.json").write_text(json.dumps(record))
    return directory


def make_mcp(tmp_path, name, pid=None):
    directory = st.state_root() / "mcp" / name
    (directory / "sock").mkdir(parents=True)
    data = {"backend": "podman", "started": "2026-11-03T07:00:00Z", "command": ["x"]}
    if pid:
        data["pid"] = pid
        fake_process(tmp_path, pid, ["python3", "agent_sandbox.py", "--mcp-socket-dir", launcher.mcp_socket_dir(name)])
    (directory / "mcp.json").write_text(json.dumps(data))


def listed(argv=()):
    lines = []
    assert st.main(["list", "--json", *argv], out=lines.append) == 0
    return json.loads("\n".join(lines))


def test_entries(scenario, tmp_path):
    if scenario == "happy":
        make_start("web", tmp_path, pid=41)
        make_start("old")
        register("session-web", session="agent-session-web")  # the session's own run: one entry, not two
        register("claude-1234abcd")
        make_mcp(tmp_path, "demo", pid=43)
        register("mcp-demo", pid=os.getpid())
        found = {e["name"]: e for e in listed()}
        assert set(found) == {"web", "old", "claude-1234abcd", "mcp-demo"}
        assert found["web"]["state"] == "running" and found["web"]["run"] == "session-web"
        assert found["web"]["attachable"] is True and found["web"]["home"] == "kept"
        assert found["old"]["state"] == "stopped" and found["old"]["pid"] is None
        assert found["claude-1234abcd"]["kind"] == "run" and found["claude-1234abcd"]["home"] == "discarded"
        assert found["mcp-demo"]["kind"] == "mcp" and found["mcp-demo"]["state"] == "running"
        # Running first, then the stopped ones.
        assert [e["state"] for e in listed()][-1] == "stopped"
    elif scenario == "sad":
        assert listed() == []
        make_mcp(tmp_path, "idle")  # ran once, does not run now
        assert listed() == []
        assert [e["name"] for e in listed(["--all"])] == ["mcp-idle"]
        assert listed(["--all"])[0]["state"] == "stopped"
    else:
        # A damaged start directory is shown as such; a symlinked one and stray files are skipped.
        bad = sessions.root() / "bad"
        bad.mkdir(parents=True)
        (bad / "session.json").write_text("{not json")
        (sessions.root() / "stray").write_text("")
        (sessions.root() / "linked").symlink_to(tmp_path)
        (sessions.root() / "empty").mkdir()
        assert [(e["name"], e["state"]) for e in listed()] == [("bad", "damaged")]


def test_entries_say_why_nothing_can_be_attached(scenario, tmp_path):
    """attach_why: one word for the desktop app and the web page, null exactly when attachable."""
    def foreground_start(key, pid, paused=False):  # a kept start in the foreground (--no-tmux): its own launcher
        fake_process(tmp_path, pid, ["python3", "agent_sandbox.py", "claude"], start=7)
        data = make_start(key, paused=paused, mode="foreground", pid_start=7, started="2026-11-02T09:30:00Z")
        sessions.save(key, {**data, "pid": pid})

    if scenario == "happy":
        make_start("web", tmp_path, pid=41)
        register("claude-1234abcd")  # a plain run: no start directory, no tmux
        foreground_start("task", 42)
        make_mcp(tmp_path, "demo", pid=43)
        register("mcp-demo", pid=os.getpid())
        found = {e["name"]: (e["attachable"], e["attach_why"]) for e in listed()}
        assert found == {"web": (True, None), "claude-1234abcd": (False, "foreground"),
                         "task": (False, "foreground"), "mcp-demo": (False, "mcp")}
    elif scenario == "sad":
        make_start("held", tmp_path, pid=41, paused=True)
        make_start("old")
        found = {e["name"]: (e["state"], e["attachable"], e["attach_why"]) for e in listed()}
        assert found == {"held": ("paused", False, "paused"), "old": ("stopped", False, "stopped")}
    else:
        bad = sessions.root() / "bad"
        bad.mkdir(parents=True)
        (bad / "session.json").write_text("{not json")
        foreground_start("task", 42, paused=True)  # paused, but the foreground is what matters
        found = {e["name"]: e["attach_why"] for e in listed()}
        assert found == {"bad": "damaged", "task": "foreground"}
        assert all(e["attach_why"] is None for e in listed() if e["attachable"])


def test_show(scenario, tmp_path):
    lines = []
    if scenario == "happy":
        make_start("web", tmp_path, pid=41)
        st.show(st.entries(), lines.append)
        assert lines[0].split() == ["NAME", "KIND", "AGENT", "STATE", "HOME", "STARTED", "(UTC)", "WORKSPACE"]
        assert lines[1].split() == ["web", "session", "codex", "running", "kept", "2026-11-02", "09:30", "/srv/app"]
    elif scenario == "sad":
        st.show([], lines.append)
        assert lines == ["nothing runs and no start is kept; start one with: sandbox-ai claude|codex|opencode"]
    else:
        # Names and workspaces come from files the sandbox may have influenced: shown escaped.
        register("claude-1", name="evil\x1b[2Jname", workspace="/w/\x07bell")
        st.show(st.entries(), lines.append)
        assert "\x1b" not in "".join(lines) and "\x07" not in "".join(lines)


def test_shown_time(scenario):
    if scenario == "happy":
        assert st.shown_time("2026-11-04T14:30:12Z") == "2026-11-04 14:30"
    elif scenario == "sad":
        assert st.shown_time(None) == "-" and st.shown_time(12) == "-"
    else:
        assert st.shown_time("now") == "-"


def test_read_json_and_text(scenario, tmp_path):
    path = tmp_path / "x.json"
    if scenario == "happy":
        path.write_text('{"a": 1}')
        assert st.read_json(path) == {"a": 1} and st.text("x") == "x"
    elif scenario == "sad":
        assert st.read_json(path) is None and st.text(5) is None
    else:
        path.write_text("[1]")
        assert st.read_json(path) is None and st.text(None) is None


def test_module_shares_one_launcher(scenario):
    if scenario == "happy":
        assert sessions.launcher() is launcher and net.launcher() is launcher and mcp.launcher() is launcher
    elif scenario == "sad":
        assert st.module("sessions") is sessions  # loaded once
    else:
        assert st.module("agent_sandbox") is launcher


def test_main_and_alias(scenario, tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    command = {"happy": ["list"], "sad": ["list", "--bogus"], "edge": ["ps", "--json"]}[scenario]
    result = subprocess.run([str(ROOT / "sandbox-ai"), *command], capture_output=True, text=True, env=env)
    if scenario == "happy":
        assert result.returncode == 0 and result.stdout.startswith("nothing runs")
    elif scenario == "sad":
        assert result.returncode == 2 and "unrecognized arguments" in result.stderr
    else:
        assert result.returncode == 0 and json.loads(result.stdout) == []


def history_events(name):
    return [(e["event"], e.get("run")) for e in st.history(name)]


def test_valid_and_check_name(scenario):
    if scenario == "happy":
        assert st.valid_name("claude-app-1a2b") and st.valid_name("A.b_c-1")
        st.check_name("review")
    elif scenario == "sad":
        for name in ("", "-x", ".x", "a b", "a/b", "x" * 41, None, 5):
            assert not st.valid_name(name)
            with pytest.raises(ValueError, match="1-40 characters"):
                st.check_name(name)
    else:
        assert st.valid_name("mcp-x")
        with pytest.raises(ValueError, match="belong to the MCP server"):
            st.check_name("mcp-x")


def test_sanitise(scenario):
    if scenario == "happy":
        assert st.sanitise("my-app") == "my-app" and st.sanitise("App_2.0") == "App_2.0"
    elif scenario == "sad":
        assert st.sanitise("") == "work" and st.sanitise("...") == "work" and st.sanitise("日本") == "work"
    else:
        assert st.sanitise("Mein Projekt (alt)") == "Mein-Projekt-alt"
        assert st.sanitise("../../etc") == "etc" and len(st.sanitise("y" * 90)) == 40


def test_auto_name(scenario):
    tokens = iter(["aaaa", "bbbb"])
    if scenario == "happy":
        assert st.auto_name("claude", "shop", set(), lambda n: "1a2b") == "claude-shop-1a2b"
    elif scenario == "sad":
        # In use: another token.
        assert st.auto_name("codex", "shop", {"codex-shop-aaaa"}, lambda n: next(tokens)) == "codex-shop-bbbb"
        with pytest.raises(ValueError, match="choose one with --name"):
            st.auto_name("codex", "shop", {"codex-shop-aaaa"}, lambda n: "aaaa")
    else:
        name = st.auto_name("opencode", "a-very-long-project-name-that-goes-on", set(), lambda n: "c0de")
        assert len(name) <= 40 and name.endswith("-c0de") and st.valid_name(name)


def test_names_in_use_and_check_free(scenario, tmp_path):
    make_start("web", tmp_path, pid=41, name="frontend")
    make_start("old")
    register("claude-1234abcd", name="review")
    if scenario == "happy":
        assert {"frontend", "web", "old", "review", "claude-1234abcd"} <= st.names_in_use()
        st.check_free("fresh")
    elif scenario == "sad":
        with pytest.raises(ValueError, match="The name review is in use \\(running run\\)"):
            st.check_free("review")
        with pytest.raises(ValueError, match="belongs to a kept start \\(stopped\\); continue it with: sandbox-ai "
                                             "resume old"):
            st.check_free("old")
        with pytest.raises(ValueError, match="in use"):
            st.check_free("web")  # the id of the renamed session
    else:
        found = st.entries()
        own = next(e for e in found if e["name"] == "frontend")
        st.check_free("web", found, own=own)  # its own id is no clash


def test_record_and_history(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        assert st.record("review", "start", "claude-1", "claude-1", agent="claude", status=None)
        assert st.record("review", "exit", "claude-1", "claude-1", status=0)
        found = st.history("review")
        assert [e["event"] for e in found] == ["start", "exit"] and "status" not in found[0]
        path = st.history_path("review")
        assert stat_mode(path) == 0o600 and stat_mode(path.parent) == 0o700
    elif scenario == "sad":
        assert st.record("../evil", "start", "x") is False  # never a path from a name that is none
        assert not st.history_dir().exists()
        st.history_dir().mkdir(parents=True)
        (st.history_dir() / "linked.jsonl").symlink_to(tmp_path / "elsewhere")
        assert st.record("linked", "start", "x") is False and not (tmp_path / "elsewhere").exists()
        assert st.history("linked") == []
        with pytest.raises(ValueError, match="not a name"):
            st.history("a/b")
    else:
        monkeypatch.setattr(st, "HISTORY_LIMIT", 2000)
        monkeypatch.setattr(st, "HISTORY_KEEP", 5)
        for index in range(40):
            st.record("busy", "pause", "x", f"run-{index}")
        found = st.history("busy")
        assert len(found) <= 20 and found[-1]["run"] == "run-39"
        (st.history_path("busy")).write_text("{broken\n" + json.dumps({"event": "stop"}) + "\n[1]\n")
        assert st.history("busy") == [{"event": "stop"}]


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_write_and_move_history(scenario):
    st.record("old", "start", "s1", "r1")
    st.record("old", "start", "other", "r0")  # an earlier start of that name: stays
    if scenario == "happy":
        st.move_history("old", "new", "s1")
        assert history_events("new") == [("start", "r1"), ("rename", None)]
        assert history_events("old") == [("start", "r0"), ("rename", None)]
        assert st.history("old")[-1]["to"] == "new" and st.history("new")[-1]["from"] == "old"
    elif scenario == "sad":
        st.write_history("old", [])
        assert not st.history_path("old").exists()
    else:
        # NEW had a history of its own: merged in time order.
        st.write_history("old", [{"time": "2026-11-01T10:00:00Z", "event": "start", "id": "s1"}])
        st.write_history("new", [{"time": "2026-11-02T10:00:00Z", "event": "exit", "id": "earlier"}])
        st.move_history("old", "new", "s1")
        assert [e["id"] for e in st.history("new")] == ["s1", "earlier", "s1"]


def test_run_event(scenario, tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    record = {"run": "codex-1", "name": "fix", "start": None, "agent": "codex", "workspace": "/w"}
    (control / "run.json").write_text(json.dumps(record))
    if scenario == "happy":
        assert st.run_event(control, "start") and st.run_event(control, "exit", 3)
        assert [(e["event"], e.get("status"), e["id"]) for e in st.history("fix")] == \
            [("start", None, "codex-1"), ("exit", 3, "codex-1")]
        assert st.history("fix")[0]["workspace"] == "/w"
    elif scenario == "sad":
        (control / "run.json").write_text(json.dumps({**record, "name": "../x"}))
        assert st.run_event(control, "start") is False
        assert st.run_event(tmp_path / "missing", "start") is False
    else:
        (control / "run.json").write_text(json.dumps({**record, "start": "fix"}))
        assert st.run_event(control, "exit", None)
        assert st.history("fix")[0]["id"] == "fix" and st.history("fix")[0]["status"] is None
        assert st.run_event(control, "exit", 0) is False  # once per run


def test_rewrite_json(scenario, tmp_path):
    path = tmp_path / "run.json"
    if scenario == "happy":
        path.write_text(json.dumps({"run": "x", "name": "a"}))
        st.rewrite_json(path, {"name": "b"})
        assert json.loads(path.read_text()) == {"run": "x", "name": "b"}
    elif scenario == "sad":
        with pytest.raises(ValueError, match="cannot read"):
            st.rewrite_json(path, {"name": "b"})
    else:
        path.write_text("[]")
        with pytest.raises(ValueError):
            st.rewrite_json(path, {})


def test_find(scenario, tmp_path):
    make_start("web", tmp_path, pid=41, name="frontend")
    register("session-web", session="agent-session-web")
    register("claude-1", name="twin")
    register("claude-2", name="twin")
    if scenario == "happy":
        assert st.find("frontend")["id"] == "web"
        assert st.find("web")["name"] == "frontend"  # the id still works
        assert st.find("session-web")["name"] == "frontend"  # and the run id
    elif scenario == "sad":
        with pytest.raises(ValueError, match="nothing named nope runs or is kept"):
            st.find("nope")
    else:
        with pytest.raises(ValueError, match="several starts are named twin; name one by its run id: claude-1, claude-2"):
            st.find("twin")
        assert st.find("claude-2")["run"] == "claude-2"


def test_rename(scenario, tmp_path):
    lines = []
    if scenario == "happy":
        make_start("web", tmp_path, pid=41)
        run = register("session-web", session="agent-session-web")
        st.record("web", "start", "web", "session-web")
        assert st.main(["rename", "web", "frontend"], out=lines.append) == 0
        assert lines == ["web is now frontend"]
        assert sessions.load("web")["name"] == "frontend" and sessions.resolve("frontend") == "web"
        assert json.loads((run / "run.json").read_text())["name"] == "frontend"
        assert history_events("frontend") == [("start", "session-web"), ("rename", None)]
        # Back again; the id stays.
        assert st.rename("frontend", "web", lines.append) == 0 and sessions.load("web")["name"] == "web"
    elif scenario == "sad":
        make_start("a")
        make_start("b")
        register("claude-9", name="busy")
        for old, new, message in (("a", "b", "belongs to a kept start"), ("a", "busy", "is in use"),
                                  ("a", "x y", "1-40 characters"), ("a", "mcp-a", "MCP server"),
                                  ("zz", "c", "nothing named zz")):
            with pytest.raises(ValueError, match=message):
                st.rename(old, new)
        make_mcp(tmp_path, "demo", pid=43)
        with pytest.raises(ValueError, match="keep the name of their definition"):
            st.rename("mcp-demo", "x")
        assert st.main(["rename", "zz", "c"]) == 1
    else:
        # A plain run: its run.json says the new name; its history follows.
        run = register("codex-77", name="codex-app-1a2b")
        st.record("codex-app-1a2b", "start", "codex-77", "codex-77")
        st.rename("codex-77", "fix", lines.append)
        assert json.loads((run / "run.json").read_text())["name"] == "fix"
        assert st.find("fix")["run"] == "codex-77"
        assert st.rename("fix", "fix", lines.append) == 0 and lines[-1] == "fix: already its name"


def test_event_details(scenario):
    if scenario == "happy":
        assert st.event_details({"event": "exit", "status": 0}) == "status 0"
        assert st.event_details({"event": "start", "agent": "codex", "workspace": "/w"}) == "codex /w"
    elif scenario == "sad":
        assert st.event_details({"event": "exit"}) == "status unknown"
        assert st.event_details({"event": "pause"}) == ""
    else:
        assert st.event_details({"event": "rename", "to": "b"}) == "renamed to b"
        assert st.event_details({"event": "rename", "from": "a\x1b"}) == "renamed from a\\x1b"


def test_show_history(scenario, tmp_path):
    lines = []
    if scenario == "happy":
        st.record("fix", "start", "codex-1", "codex-1", agent="codex")
        st.record("fix", "exit", "codex-1", "codex-1", status=0)
        assert st.main(["list", "--history", "fix"], out=lines.append) == 0
        assert lines[0].split() == ["TIME", "(UTC)", "EVENT", "RUN", "DETAILS"]
        assert lines[2].split()[2:] == ["exit", "codex-1", "status", "0"]
        lines.clear()
        st.main(["list", "--history", "fix", "--json"], out=lines.append)
        assert [e["event"] for e in json.loads("\n".join(lines))] == ["start", "exit"]
    elif scenario == "sad":
        assert st.show_history("nobody", lines.append) == 0 and lines == ["no history of nobody"]
    else:
        # Asked by the id of a renamed start: the history of its name.
        make_start("web", tmp_path, pid=41, name="frontend")
        st.record("frontend", "pause", "web", "session-web")
        st.show_history("web", lines.append)
        assert "pause" in lines[1]


# --- attach, pause, resume, stop, delete by name ---------------------------------------------------------


def test_act_on_sessions(scenario, tmp_path, monkeypatch):
    make_start("web", tmp_path, pid=41, name="frontend")
    calls = []
    for command in ("attach", "pause", "resume", "stop", "delete"):
        monkeypatch.setattr(sessions, command, lambda key, *args, command=command: calls.append((command, key, args)) or 0)
    if scenario == "happy":
        for command in ("attach", "pause", "resume", "stop"):
            assert st.main([command, "frontend"]) == 0
        assert calls == [("attach", "web", ()), ("pause", "web", ()), ("resume", "web", ()), ("stop", "web", ())]
    elif scenario == "sad":
        assert st.main(["attach", "nobody"]) == 1
        assert st.main(["delete", "frontend", "--yes"]) == 0 and calls == [("delete", "web", (True,))]
    else:
        assert st.main(["stop", "web", "--wait", "5"]) == 0 and calls == [("stop", "web", (5.0,))]


def test_act_on_runs_and_mcp_servers(scenario, tmp_path, monkeypatch, capsys):
    register("claude-1234abcd", name="task")
    if scenario == "happy":
        stop = Mock(return_value=0)
        monkeypatch.setattr(net, "stop", stop)
        assert st.main(["stop", "task"]) == 0
        assert stop.call_args.args[0]["run"] == "claude-1234abcd"
        assert [e["event"] for e in st.history("task")] == ["stop"]
    elif scenario == "sad":
        assert st.main(["attach", "task"]) == 1
        assert "runs without tmux" in capsys.readouterr().err
        assert st.main(["delete", "task", "--yes"]) == 1
        assert "keeps nothing to delete" in capsys.readouterr().err
        # Ended, with a history: said so.
        st.record("old", "exit", "x", "x", status=0)
        assert st.main(["resume", "old"]) == 1
        assert "has ended and its HOME was discarded" in capsys.readouterr().err
    else:
        make_mcp(tmp_path, "demo", pid=43)
        stop = Mock(return_value=0)
        monkeypatch.setattr(mcp, "stop", stop)
        assert st.main(["stop", "mcp-demo"]) == 0 and stop.call_args.args == ("demo",)
        assert st.main(["pause", "mcp-demo"]) == 1
        assert "starts with the agents that use it" in capsys.readouterr().err


def test_pause_run(scenario, tmp_path, monkeypatch):
    backend = "bwrap" if scenario == "edge" else "podman"
    directory = register("codex-1", name="task", backend=backend, pod="agent-codex-1" if scenario != "sad" else None)
    monkeypatch.setattr(net, "launcher_process", lambda record: (os.getpid(), 1))
    run, tree = Mock(), Mock()
    monkeypatch.setattr(st.subprocess, "run", run)
    monkeypatch.setattr(sessions, "signal_tree", tree)
    if scenario == "sad":
        # Registered by an older version: no pod to pause.
        with pytest.raises(ValueError, match="did not record its pod"):
            st.act("pause", "task")
        return
    assert st.act("pause", "task") == 0 and (directory / "paused").exists()
    assert listed()[0]["state"] == "paused"
    assert st.act("pause", "task") == 0  # already: nothing more
    assert st.act("resume", "task") == 0 and not (directory / "paused").exists()
    if scenario == "happy":
        assert [c.args[0][-3:] for c in run.call_args_list] == [["pod", "pause", "agent-codex-1"],
                                                                ["pod", "unpause", "agent-codex-1"]]
        tree.assert_not_called()
    else:
        # bwrap: the processes below the launcher; the launcher keeps its terminal.
        assert [(c.args[1], c.kwargs) for c in tree.call_args_list] == [
            (st.signal.SIGSTOP, {"include_root": False}), (st.signal.SIGCONT, {"include_root": False})]
        run.assert_not_called()
    assert [e["event"] for e in st.history("task")] == ["pause", "resume"]
    # Stopping a paused run thaws it first: frozen processes could not end.
    assert st.act("pause", "task") == 0
    monkeypatch.setattr(net, "stop", Mock(return_value=0))
    assert st.act("stop", "task") == 0 and not (directory / "paused").exists()
    assert [e["event"] for e in st.history("task")][2:] == ["pause", "resume", "stop"]


def test_mode_of_running_starts(scenario, tmp_path):
    """sandbox-ai list --json names the security mode a running sandbox recorded (modes.py)."""
    if scenario == "happy":
        register("claude-1", name="task", mode={"name": "high", "origin": "--mode"})
        assert listed()[0]["mode"] == "high"
    elif scenario == "sad":
        register("claude-1", name="task")  # an older run.json: no mode
        assert listed()[0]["mode"] is None and st.mode_of(None) is None
    else:
        make_start("web", tmp_path, pid=41)
        register("session-web", session="agent-session-web", mode={"name": "paranoid"})
        assert next(e for e in listed() if e["name"] == "web")["mode"] == "paranoid"
        assert st.mode_of({"mode": "high"}) is None  # not the record's shape


def test_entries_prune_ended_sessions_without_home(tmp_path):
    make_start("gone", keep=False)
    make_start("kept")
    assert [e["name"] for e in listed()] == ["kept"]
    assert not sessions.session_dir("gone").exists()


# --- kept starts: expiry and gc -----------------------------------------------------------------------------


def test_keep_days(scenario, tmp_path):
    path = tmp_path / "config" / "sandbox-ai" / "defaults.json"
    path.parent.mkdir(parents=True)
    if scenario == "happy":
        path.write_text(json.dumps({"version": 1, "keep_days": 30}))
        assert st.keep_days() == 30
    elif scenario == "sad":
        assert st.keep_days() is None  # never expire unless asked
    else:
        for value in (0, "30", True, -1):
            path.write_text(json.dumps({"version": 1, "keep_days": value}))
            assert st.keep_days() is None


def test_seconds_and_iso(scenario):
    if scenario == "happy":
        assert st.iso(st.seconds("2026-11-04T10:00:00Z")) == "2026-11-04T10:00:00Z"
    elif scenario == "sad":
        assert st.seconds("yesterday") is None and st.seconds(None) is None
    else:
        assert st.seconds("1970-01-01T00:00:00Z") == 0


def test_write_exit(scenario, tmp_path):
    if scenario == "happy":
        make_start("task")
        st.write_exit("task", "codex-1", 3)
        assert json.loads((sessions.session_dir("task") / "exit.json").read_text())["status"] == 3
        entry = next(e for e in listed() if e["id"] == "task")
        assert entry["status"] == 3 and entry["ended"] and entry["last_used"] == entry["ended"]
    elif scenario == "sad":
        st.write_exit("gone", "codex-1", 0)  # no directory: nothing written
        st.write_exit("../x", "codex-1", 0)
        assert not sessions.root().exists()
    else:
        make_start("task")
        (sessions.session_dir("task") / "exit.json").mkdir()  # planted where the file goes
        with pytest.raises(OSError):
            st.write_exit("task", "codex-1", 0)


def test_entries_of_kept_starts(scenario, tmp_path):
    if scenario == "happy":
        make_start("task", mode="foreground", keep=True, run="codex-1", started="2026-11-01T00:00:00Z")
        entry = listed()[0]
        assert (entry["kind"], entry["home"], entry["attachable"], entry["run"]) == ("run", "kept", False, "codex-1")
        assert entry["expires"] is None and entry["expired"] is False
    elif scenario == "sad":
        # keep_days: expired after that long unused (still listed; gc or the next start removes it).
        (tmp_path / "config" / "sandbox-ai").mkdir(parents=True)
        (tmp_path / "config" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "keep_days": 2}))
        make_start("old", started="2020-01-01T00:00:00Z", created="2020-01-01T00:00:00Z")
        entry = listed()[0]
        assert entry["expires"] == "2020-01-03T00:00:00Z" and entry["expired"] is True
        lines = []
        st.show(st.entries(), lines.append)
        assert "stopped, expired" in lines[1]
    else:
        register("claude-9", name="mine", own_home=True)
        assert listed()[0]["home"] == "own"


def test_gc(scenario, tmp_path, monkeypatch):
    make_start("old", started="2020-01-01T00:00:00Z", created="2020-01-01T00:00:00Z")
    make_start("fresh", started=st.now(), created=st.now())
    make_start("gone", keep=False)
    make_start("busy", tmp_path, pid=41, started="2020-01-01T00:00:00Z")
    st.write_history("ancient", [{"time": "2020-01-01T00:00:00Z", "event": "exit", "id": "x"}])
    st.write_history("recent", [{"time": st.now(), "event": "exit", "id": "y"}])
    st.write_history("old", [{"time": "2020-01-01T00:00:00Z", "event": "exit", "id": "old"}])
    lines = []
    if scenario == "happy":
        assert st.main(["gc", "--older-than", "30"], out=lines.append) == 0
        assert not sessions.session_dir("old").exists() and not sessions.session_dir("gone").exists()
        assert sessions.session_dir("fresh").exists() and sessions.session_dir("busy").exists()
        assert not st.history_path("ancient").exists() and st.history_path("recent").exists()
        assert "removed the kept start old with its HOME (last used 2020-01-01 00:00 UTC)" in lines
        assert "removed the leftovers of gone (ended; its HOME was not kept)" in lines
        assert [e["event"] for e in st.history("old")][-1] == "delete"  # its history notes the removal
    elif scenario == "sad":
        # Without an age nothing kept goes: kept starts never expire on their own.
        assert st.main(["gc", "--json"], out=lines.append) == 0
        result = json.loads("\n".join(lines))
        assert result["removed"] == [] and result["leftovers"] == ["gone"] and result["histories"] == []
        assert sessions.session_dir("old").exists()
        assert st.main(["gc", "--older-than", "-1"]) == 1
    else:
        assert st.main(["gc", "--older-than", "30", "--dry-run"], out=lines.append) == 0
        assert "would remove the kept start old with its HOME (last used 2020-01-01 00:00 UTC)" in lines
        assert "would remove the leftovers of gone (ended; its HOME was not kept)" in lines
        assert all(sessions.session_dir(key).exists() for key in ("old", "gone", "fresh"))
        assert st.history_path("ancient").exists()
        # A start held by another command is left alone and named.
        monkeypatch.setattr(sessions, "delete", Mock(side_effect=ValueError("being started or stopped")))
        result = st.gc(30)
        assert result["skipped"] == [{"name": "old", "reason": "being started or stopped"}]


def test_gc_expired_only_and_keep_days(tmp_path):
    (tmp_path / "config" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "config" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "keep_days": 30}))
    make_start("old", started="2020-01-01T00:00:00Z", created="2020-01-01T00:00:00Z")
    make_start("gone", keep=False)
    st.write_history("ancient", [{"time": "2020-01-01T00:00:00Z", "event": "exit", "id": "x"}])
    result = st.gc(30, expired_only=True)  # what a new start does with keep_days
    assert [e["name"] for e in result["removed"]] == ["old"] and result["leftovers"] == [] and result["histories"] == []
    assert st.history_path("ancient").exists()
    assert st.gc()["days"] == 30  # keep_days when no age is given


def test_resume_review_home(scenario, tmp_path):
    make_start("web", started="2026-11-01T00:00:00Z")
    home = sessions.session_dir("web") / "home"
    review = st.module("home_review")
    (home / ".bashrc").write_text("a\n")
    review.record(home, sessions.session_dir("web"))
    (home / ".bashrc").write_text("b\n")
    lines = []
    if scenario == "happy":
        assert st.main(["resume", "web", "--review-home", "--json"], out=lines.append) == 0
        assert json.loads("\n".join(lines))["review"]["changes"][0]["change"] == "changed"
    elif scenario == "sad":
        register("claude-1", name="task")
        assert st.main(["resume", "task", "--review-home"]) == 1  # a plain run keeps no HOME
        assert st.main(["resume", "web", "--json"]) == 1  # --json goes with --review-home
    else:
        assert st.main(["resume", "web", "--review-home"], out=lines.append) == 0
        assert "== changed .bashrc" in lines
        assert not (sessions.session_dir("web") / "tmux" / "attach.sock").exists()  # nothing started


def test_resume_with_workspaces_and_restart(scenario, tmp_path, monkeypatch):
    """resume NAME --workspace/--mount: a kept start comes back with them; --restart stops a running one first."""
    make_start("web", started="2026-11-01T00:00:00Z")
    resumed, stopped = Mock(return_value=0), Mock(return_value=0)
    monkeypatch.setattr(sessions, "resume", resumed)
    monkeypatch.setattr(sessions, "stop", stopped)
    if scenario == "happy":
        assert st.main(["resume", "web", "-w", str(tmp_path), "--mount", f"{tmp_path}:/mnt/x:ro"]) == 0
        assert resumed.call_args.args[1:] == ([str(tmp_path)], [f"{tmp_path}:/mnt/x:ro"]) and not stopped.called
    elif scenario == "sad":
        register("claude-1", name="task")
        assert st.main(["resume", "task", "--workspace", str(tmp_path)]) == 1  # a plain run: nothing to start again
        assert st.main(["resume", "web", "--review-home", "--workspace", str(tmp_path)]) == 1
        assert not resumed.called
    else:
        monkeypatch.setattr(st, "find", lambda name: {**st.entries(True)[0], "state": "running"})
        lines = []
        assert st.main(["resume", "web", "--restart"], out=lines.append) == 0
        assert stopped.called and resumed.call_args.args[1:] == ([], []) and "stopping it" in lines[0]


# --- the single commands are deprecated ---------------------------------------------------------------------

OLD_COMMANDS = {"claude-sandbox": "sandbox-ai claude", "codex-sandbox": "sandbox-ai codex",
                "opencode-sandbox": "sandbox-ai opencode", "sandbox-mcp": "sandbox-ai mcp",
                "sandbox-wizard": "sandbox-ai wizard", "sandbox-build-image": "sandbox-ai build",
                "sandbox-session": "sandbox-ai list|attach|pause|resume|stop|delete"}


@pytest.mark.parametrize("command", sorted(OLD_COMMANDS))
def test_old_commands_say_they_are_deprecated(command, tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    env.pop("SANDBOX_AI_NO_DEPRECATION", None)
    result = subprocess.run([str(ROOT / command), "--help"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines()[0] == (f"{command}: deprecated, to be removed in a later version; use: "
                                             f"{OLD_COMMANDS[command]} (SANDBOX_AI_NO_DEPRECATION=1 hides this note)")
    quiet = subprocess.run([str(ROOT / command), "--help"], capture_output=True, text=True,
                           env={**env, "SANDBOX_AI_NO_DEPRECATION": "1"})
    assert "deprecated" not in quiet.stderr and quiet.stdout == result.stdout
    # sandbox-ai itself says nothing of the kind.
    new = subprocess.run([str(ROOT / "sandbox-ai"), "list"], capture_output=True, text=True, env=env)
    assert "deprecated" not in new.stderr


def test_console_entry_points_say_so_too(scenario, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("sandbox_cli_test", ROOT / "sandbox/cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: calls.append(argv))
    monkeypatch.delenv("SANDBOX_AI_NO_DEPRECATION", raising=False)
    if scenario == "happy":
        cli.claude()
        assert "claude-sandbox: deprecated" in capsys.readouterr().err
        assert str(calls[-1][2]).endswith("agent_sandbox.py") and calls[-1][3] == "claude"
    elif scenario == "sad":
        monkeypatch.setenv("SANDBOX_AI_NO_DEPRECATION", "1")
        cli.session()
        assert capsys.readouterr().err == "" and str(calls[-1][2]).endswith("sessions.py")
    else:
        cli.main()  # sandbox-ai: not deprecated
        assert capsys.readouterr().err == "" and str(calls[-1][2]).endswith("main.py")
