"""Happy/sad/edge contracts for the registry of every start (sandbox-ai list, rename, attach, pause, resume,
stop, delete, gc and the history); only process boundaries are faked (a /proc tree, the backend)."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
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


def make_mcp(tmp_path, name, pid=None, **fields):
    directory = st.state_root() / "mcp" / name
    (directory / "sock").mkdir(parents=True)
    data = {"backend": "podman", "started": "2026-11-03T07:00:00Z", "command": ["x"], **fields}
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


def test_entries_say_what_belongs_together(tmp_path):
    """list --json names a running start's pod, its --with companions, whose helper it is and its teams: the app and
    the web group the starts by them."""
    register("claude-1234abcd", pod="agent-claude-1234abcd", companions=["codex"], mcp_servers=["team-app", "web"])
    register("codex-helper99", helper_of="claude-1234abcd", backend="bwrap")
    found = {e["name"]: e for e in listed()}
    lead, helper = found["claude-1234abcd"], found["codex-helper99"]
    assert lead["pod"] == "agent-claude-1234abcd" and lead["companions"] == ["codex"] and lead["teams"] == ["app"]
    assert helper["helper_of"] == "claude-1234abcd" and helper["pod"] is None and helper["companions"] == []


def test_entries_survive_damaged_grouping_fields(tmp_path):
    """A damaged run.json never fails the listing: each grouping field is checked and a bad value left out (helper_of
    to a run that is gone, to itself or round in a circle; companions that are no list or no known agent)."""
    register("claude-aaaa0001", companions=5, pod=7, helper_of="gone-run", mcp_servers=["team-ok", "team-B@D", 3])
    register("codex-aaaa0002", companions=["codex", "nobody", 3, "opencode", "opencode"], pod="",
             helper_of="codex-aaaa0002")
    register("claude-aaaa0003", helper_of="claude-aaaa0004", teams="x")
    register("claude-aaaa0004", helper_of="claude-aaaa0003", mcp_servers="team-x")
    register("claude-aaaa0005", helper_of="claude-aaaa0003", companions={"a": 1})
    damaged = register("claude-aaaa0006")  # its id is no string: not listed
    (damaged / "run.json").write_text(json.dumps({"run": 5, "pid": os.getpid(), "started": "2026-11-03T08:00:00Z"}))
    register("claude-aaaa0007", helper_of=["claude-aaaa0001"])
    found = {e["name"]: e for e in listed()}
    assert set(found) == {f"claude-aaaa000{i}" for i in (1, 3, 4, 5, 7)} | {"codex-aaaa0002"}
    first, second = found["claude-aaaa0001"], found["codex-aaaa0002"]
    assert (first["companions"], first["pod"], first["helper_of"], first["teams"]) == ([], None, None, ["ok"])
    assert (second["companions"], second["pod"], second["helper_of"]) == (["codex", "opencode"], None, None)
    assert found["claude-aaaa0003"]["helper_of"] is None and found["claude-aaaa0004"]["helper_of"] is None
    assert found["claude-aaaa0004"]["teams"] == [] and found["claude-aaaa0005"]["companions"] == []
    assert found["claude-aaaa0005"]["helper_of"] == "claude-aaaa0003"  # leads into the circle, is not on it
    assert found["claude-aaaa0007"]["helper_of"] is None


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


def test_resume_by_an_earlier_name_names_the_current_one(tmp_path, capsys):
    """Review 6 N1: a start renamed twice is not 'discarded' under its middle name: the current name is said."""
    make_start("s1")
    st.rename("s1", "s1x", lambda line: None)
    st.rename("s1x", "newname", lambda line: None)
    for command in ("resume", "attach"):
        assert st.main([command, "s1x"]) == 1
        err = capsys.readouterr().err
        assert f"s1x was renamed to newname: sandbox-ai {command} newname" in err and "discarded" not in err
    # A name given away and then used by a start that ended: that one's own history speaks.
    st.record("s1x", "exit", "other", "other", status=0)
    assert st.main(["resume", "s1x"]) == 1
    assert "has ended and its HOME was discarded" in capsys.readouterr().err


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
        assert resumed.call_args.args[1:] == ([str(tmp_path)], [f"{tmp_path}:/mnt/x:ro"], [], [], []) and \
            not stopped.called
    elif scenario == "sad":
        register("claude-1", name="task")
        assert st.main(["resume", "task", "--workspace", str(tmp_path)]) == 1  # a plain run: nothing to start again
        assert st.main(["resume", "web", "--review-home", "--workspace", str(tmp_path)]) == 1
        assert not resumed.called
    else:
        monkeypatch.setattr(st, "find", lambda name: {**st.entries(True)[0], "state": "running"})
        lines = []
        assert st.main(["resume", "web", "--restart"], out=lines.append) == 0
        assert stopped.called and resumed.call_args.args[1:] == ([], [], [], [], []) and "stopping it" in lines[0]


def test_resume_takes_launcher_options_and_drop_option(scenario, tmp_path, monkeypatch):
    """resume NAME --gui --cpus 4 --drop-option allow-host: the launcher's options after NAME change the saved
    options; a running start is checked before it is stopped (a refused change leaves it running)."""
    make_start("web", started="2026-11-01T00:00:00Z")
    resumed, stopped = Mock(return_value=0), Mock(return_value=0)
    monkeypatch.setattr(sessions, "resume", resumed)
    monkeypatch.setattr(sessions, "stop", stopped)
    if scenario == "happy":
        assert st.main(["resume", "web", "--gui", "--cpus", "4", "--allow-host", "example.org", "--drop-option",
                        "allow-pypi"]) == 0
        assert resumed.call_args.args[1:] == ([], [], [], ["--gui", "--cpus", "4", "--allow-host", "example.org"],
                                              ["allow-pypi"])
    elif scenario == "sad":
        with pytest.raises(SystemExit):
            st.main(["stop", "web", "--gui"])  # only resume takes the launcher's options
        assert st.main(["resume", "web", "--review-home", "--gui"]) == 1
        assert st.main(["resume", "web", "--yes"]) == 1  # --yes goes with --restore-home
        register("claude-1", name="task")
        assert st.main(["resume", "task", "--gui"]) == 1  # a plain run: nothing to start again
        assert not resumed.called
    else:
        monkeypatch.setattr(st, "find", lambda name: {**st.entries(True)[0], "state": "running"})
        monkeypatch.setattr(sessions, "check_start", Mock(side_effect=ValueError("would be refused: paranoid")))
        assert st.main(["resume", "web", "--restart", "--mode", "paranoid", "--gui"]) == 1
        assert not stopped.called and not resumed.called  # checked first: it runs on as it was


def test_resume_restore_home_from_the_command_line(tmp_path, monkeypatch):
    make_start("web", started="2026-11-01T00:00:00Z")
    restored = Mock(return_value=0)
    monkeypatch.setattr(sessions, "restore_home", restored)
    assert st.main(["resume", "web", "--restore-home", "--path", ".bashrc", "--yes", "--json"]) == 0
    assert restored.call_args.args[1:4] == ([".bashrc"], True, True)
    assert st.main(["resume", "web", "--restore-home", "--review-home"]) == 1
    assert st.main(["resume", "web", "--restore-home", "--workspace", str(tmp_path)]) == 1
    assert restored.call_count == 1


# --- the single commands are gone: sandbox-ai does all they did ---------------------------------------------

OLD_COMMANDS = {"claude-sandbox": ["claude"], "codex-sandbox": ["codex"], "opencode-sandbox": ["opencode"],
                "sandbox-mcp": ["mcp"], "sandbox-wizard": ["wizard"], "sandbox-build-image": ["build"],
                "sandbox-session": ["session"]}


@pytest.mark.parametrize("command", sorted(OLD_COMMANDS))
def test_the_old_single_commands_are_gone_and_sandbox_ai_does_what_they_did(command, tmp_path):
    """Deprecated since 0.1.30, removed since: no wrapper in the checkout, no console entry point in the package, and
    sandbox-ai offers each of them (its --help answers)."""
    assert not (ROOT / command).exists()
    assert command not in (ROOT / "pyproject.toml").read_text().split("[project.scripts]")[1].split("[")[0]
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    result = subprocess.run([str(ROOT / "sandbox-ai"), *OLD_COMMANDS[command], "--help"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "deprecated" not in result.stderr and result.stdout


def test_the_console_entry_point_runs_main(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("sandbox_cli_test", ROOT / "sandbox/cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: calls.append(argv))
    monkeypatch.setattr(cli.sys, "argv", ["sandbox-ai", "list", "--json"])
    cli.main()
    assert capsys.readouterr().err == "" and str(calls[-1][2]).endswith("main.py") and calls[-1][3:] == ["list",
                                                                                                         "--json"]
    assert not hasattr(cli, "claude") and not hasattr(cli, "build_image")


# --- an MCP server for a named start: mcp attach NAME START, mcp start NAME --for START ------------------------

def kept(key, tmp_path, pid=None, **fields):
    """A start directory whose start folder is one of this test's (not /, which holds the trust store)."""
    folder = tmp_path / "projects" / key
    folder.mkdir(parents=True, exist_ok=True)
    return make_start(key, tmp_path, pid=pid, **{"cwd": str(folder), "workspace": str(folder), **fields})


@pytest.fixture
def attaching(monkeypatch, tmp_path):
    """mcp.py works on this test's registry; no administrator's policy; the starts' agents need no login."""
    monkeypatch.setattr(mcp, "_starts", st)
    monkeypatch.setattr(mcp.launcher(), "load_policy", lambda path=None: {})
    (tmp_path / "config/sandbox-ai").mkdir(parents=True)
    (tmp_path / "config/sandbox-ai/mcp.json").write_text(json.dumps({
        "web-search": {"command": ["python3", "-I", "/opt/x.py"], "options": ["--allow-github"]}}))
    said = []
    return said.append, said


def test_attach_to_a_stopped_start_keeps_it_for_its_next_start(tmp_path, attaching):
    out, said = attaching
    kept("web", tmp_path)
    result = mcp.attach("demo", "web", out=out)
    assert result["attached"] == "next start" and "sandbox-ai resume web" in said[-1]
    assert sessions.load("web")["options"] == ["--backend", "podman", "--mcp", "demo"]
    assert {e["name"]: e for e in listed()}["web"]["mcp"] == ["demo"]
    assert mcp.attach("demo", "web", out=out)["attached"] == "already"
    assert sessions.load("web")["options"].count("demo") == 1
    # whom the server serves: mcp list (and sandbox-ai list for a running server)
    assert mcp.serving() == {"demo": ["web"]}
    with pytest.raises(ValueError, match="Unknown MCP server: nope"):
        mcp.attach("nope", "web", out=out)
    assert "nope" not in sessions.load("web")["options"]


def test_attach_to_a_running_start_needs_its_restart(tmp_path, attaching, monkeypatch):
    out, said = attaching
    kept("web", tmp_path, pid=41)
    register("session-web", session="agent-session-web", start="web", mcp_servers=[])
    result = mcp.attach("demo", "web", out=out)
    assert result["attached"] == "after a restart" and result["state"] == "running"
    assert "cannot take a new MCP server while it runs" in said[-1] and "--restart" in said[-1]
    assert "--mcp" in sessions.load("web")["options"], "kept: it is there from its next start on"
    entry = {e["name"]: e for e in listed()}["web"]
    assert entry["mcp"] == ["demo"] and entry["mcp_running"] == []
    # --restart: stopped and started again with it (the agent continues its conversation)
    done = []
    monkeypatch.setattr(sessions, "stop", lambda key, *rest: done.append(("stop", key)))
    monkeypatch.setattr(sessions, "resume_locked", lambda key, workspaces=(), mounts=(), mcp=(): done.append(
        ("resume", key, list(mcp))) or (0, True))
    result = mcp.attach("web-search", "web", restart=True, out=out)
    assert result["attached"] == "restarted" and done == [("stop", "web"), ("resume", "web", ["web-search"])]
    # asked in a terminal: yes restarts, no keeps it for the next start
    done.clear()
    assert mcp.attach("demo", "web", ask=lambda question: True, out=out)["attached"] == "restarted"
    assert done[0] == ("stop", "web")


def test_what_cannot_be_attached(tmp_path, attaching, monkeypatch):
    out, _ = attaching
    register("claude-1234abcd", name="quick")
    with pytest.raises(ValueError, match="without a kept start.*cannot take a new MCP server.*--mcp demo"):
        mcp.attach("demo", "quick", out=out)
    make_mcp(tmp_path, "demo", pid=43)
    with pytest.raises(ValueError, match="MCP server's sandbox"):
        mcp.attach("demo", "mcp-demo", out=out)
    with pytest.raises(ValueError, match="nothing named"):
        mcp.attach("demo", "nobody", out=out)


def test_attach_keeps_to_the_policy_and_the_security_mode(tmp_path, attaching, monkeypatch):
    out, _ = attaching
    kept("web", tmp_path)
    monkeypatch.setattr(mcp.launcher(), "load_policy", lambda path=None: {"forbid": ["--mcp"]})
    with pytest.raises(ValueError, match="--mcp.*policy"):
        mcp.attach("demo", "web", out=out)
    monkeypatch.setattr(mcp.launcher(), "load_policy", lambda path=None: {})
    kept("tight", tmp_path, options=["--backend", "podman", "--mode", "paranoid", "--no-keep"])
    with pytest.raises(ValueError, match="widens the security mode paranoid"):
        mcp.attach("web-search", "tight", out=out)  # a server that reaches the network
    assert "--mcp" not in sessions.load("tight")["options"]


def test_mcp_start_for_a_start_and_attach_on_the_command_line(tmp_path, attaching, monkeypatch, capsys):
    out, _ = attaching
    kept("web", tmp_path, options=["--backend", "bwrap"], backend="bwrap")
    started = []
    monkeypatch.setattr(mcp, "ensure_running", lambda name, a, vpn=None: started.append((name, a.backend, a.mcp,
                                                                                         vpn)) or "started")
    monkeypatch.setattr(mcp.sys.stdin, "isatty", lambda: False)
    assert mcp.cli(["start", "demo", "--for", "web", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["server_started"] and result["attached"] == "next start"
    assert started == [("demo", "bwrap", ["demo"], None)], "started as the start starts it: its backend"
    kept("other", tmp_path)
    assert mcp.cli(["attach", "demo", "other"]) == 0
    assert "--mcp" in sessions.load("other")["options"]
    assert mcp.cli(["list", "--json"]) == 0
    servers = {e["name"]: e for e in json.loads(capsys.readouterr().out.split("\n", 1)[1])}
    assert servers["demo"]["serves"] == ["other", "web"] or sorted(servers["demo"]["serves"]) == ["other", "web"]
    with pytest.raises(ValueError, match="--for"):
        mcp.cli(["start", "demo", "--restart"])


def test_resume_takes_an_mcp_server(tmp_path, monkeypatch):
    kept("web", tmp_path)
    launched = []
    monkeypatch.setattr(sessions, "launch", lambda key, data, args, a=None, event="rebirth": launched.append(
        list(data["options"])) or 0)
    monkeypatch.setattr(sessions, "review_home", lambda key, data: None)
    assert st.main(["resume", "web", "--mcp", "demo"], out=print) == 0
    assert launched == [["--backend", "podman", "--mcp", "demo"]]


# --- list: colour, the grouped listing, --flat ---------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Terminal:
    """sys.stdout as a terminal (or not: TTY False), with an encoding."""

    def __init__(self, tty=True, encoding="utf-8"):
        self.tty, self.encoding = tty, encoding

    def isatty(self):
        return self.tty

    def write(self, text):
        return len(text)

    def flush(self):
        pass


@pytest.fixture
def terminal(monkeypatch):
    """No colour variables from the environment of the test run; sys.stdout a terminal (set .tty to change it)."""
    for name in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "300")  # nothing wraps
    stream = Terminal()
    # starts.py's own sys with this stdout (pytest puts its capture back into the real sys.stdout for each phase)
    monkeypatch.setattr(st, "sys", type("Sys", (), {"stdout": stream, "__getattr__": lambda self, name: getattr(
        sys, name)})())
    return stream


def shown(argv):
    lines = []
    assert st.main(list(argv), out=lines.append) == 0
    return lines


def names_shown(lines):
    """The NAME column of a plain table's rows (tree markers included)."""
    return [line[:line.index("  ")] for line in lines[1:]]


def old_show(found, out):
    """sandbox-ai list's table before the grouped listing, uncoloured: what --flat must still print."""
    t = st.module("textout")
    printable = launcher.printable
    rows = [[printable(e["name"]), e["kind"], printable(e.get("agent") or "-"),
             e["state"] + (", expired" if e.get("expired") else ""), e.get("home") or "-",
             st.shown_time(e.get("started")),
             printable(e.get("workspace") or ("serves " + ", ".join(e["serves"]) if e.get("serves") else "-"))]
            for e in found]
    t.table(["NAME", "KIND", "AGENT", "STATE", "HOME", "STARTED (UTC)", "WORKSPACE"], rows, out, indent=0,
            colour=False, empty="nothing runs and no start is kept; start one with: sandbox-ai claude|codex|opencode")


def a_registry(tmp_path):
    """Two running starts and a stopped one; MCP servers: search (web's), fetch (used by api and web, started for
    web), legacy (api's, an mcp.json of an older version: no started_for), idle (started for a run that is gone, no
    start uses it)."""
    make_start("api", tmp_path, pid=41, options=["--backend", "podman", "--mcp", "fetch", "--mcp", "legacy"])
    make_start("web", tmp_path, pid=42, options=["--backend", "podman", "--mcp", "search"])
    register("session-web", session="agent-session-web", start="web", mcp_servers=["search", "fetch"])
    make_start("old")
    make_mcp(tmp_path, "search", pid=43, started_for={"start": "web", "run": "session-web"})
    make_mcp(tmp_path, "fetch", pid=44, started_for={"start": "web", "run": None})
    make_mcp(tmp_path, "legacy", pid=45)
    make_mcp(tmp_path, "idle", pid=46, started_for={"start": None, "run": "claude-gone0000"})


def test_list_colours_on_a_terminal_only(tmp_path, terminal, monkeypatch):
    a_registry(tmp_path)
    coloured = shown(["list"])
    assert coloured[0].startswith("\x1b[1mNAME"), "the headers bold"
    row = next(line for line in coloured if line.startswith("\x1b[36mweb"))
    assert "\x1b[36msession\x1b[0m" in row and "\x1b[32mrunning\x1b[0m" in row, "a session cyan, running green"
    server = next(line for line in coloured if "mcp-search" in line)
    assert "\x1b[35mmcp\x1b[0m" in server and "\x1b[35mmcp-search\x1b[0m" in server, "an MCP server magenta"
    assert "\x1b[90mstopped\x1b[0m" in next(line for line in coloured if line.startswith("\x1b[36mold"))
    plain = [ANSI.sub("", line) for line in coloured]
    assert plain != coloured
    assert shown(["ps", "--no-color"]) == plain and shown(["list", "-nc"]) == plain
    assert shown(["list", "--color=never"]) == plain and shown(["list", "--color", "always", "--no-color"]) == plain
    terminal.tty = False  # piped: none, unless asked for
    assert shown(["list"]) == plain and shown(["list", "--color=auto"]) == plain
    assert shown(["list", "--color=always"]) == coloured and shown(["list", "-nc", "--color=always"]) == coloured
    terminal.tty = True
    monkeypatch.setenv("NO_COLOR", "1")  # no-color.org
    assert shown(["list"]) == plain and shown(["list", "--color=always"]) == coloured
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert shown(["list"]) == plain
    monkeypatch.setenv("TERM", "xterm")
    for argv in (["list", "--json"], ["ps", "--json", "--color=always"], ["list", "-a", "--json", "--flat"]):
        text = "\n".join(shown(argv))
        assert "\x1b" not in text and json.loads(text), "--json is never coloured"
    st.record("web", "start", "web", "session-web")
    assert shown(["list", "--history", "web", "--color=always"])[0].startswith("\x1b[1mTIME")
    assert all("\x1b" not in line for line in shown(["list", "--history", "web", "--no-color"]))


def test_a_helper_and_a_run_have_colours_of_their_own(tmp_path, terminal):
    register("claude-1234abcd", name="quick", mcp_servers=["helper-codex-0a1b2c3d"])
    make_mcp(tmp_path, "helper-codex-0a1b2c3d", pid=47, helper="codex")
    coloured = shown(["list", "--color=always"])
    assert "\x1b[34mrun\x1b[0m" in next(line for line in coloured if "quick" in line), "a run blue"
    helper = next(line for line in coloured if "mcp-helper-codex" in line)
    assert "\x1b[38;5;141mmcp\x1b[0m" in helper, "a helper violet"
    assert names_shown(shown(["list", "-nc"])) == ["quick", "└─ mcp-helper-codex-0a1b2c3d"]


def test_list_aligns_coloured_columns(tmp_path, terminal):
    """The columns are padded on what is visible: with colour codes and the tree markers, each column starts at the
    same place in every row, and the coloured table is the plain one with codes added."""
    a_registry(tmp_path)
    register("claude-1234abcd", name="quick")
    make_mcp(tmp_path, "gone")
    for argv in (["list"], ["list", "--flat"], ["list", "-a"]):
        coloured = shown([*argv, "--color=always"])
        plain = shown([*argv, "--no-color"])
        assert [ANSI.sub("", line) for line in coloured] == plain
        header = plain[0]
        starts = [header.index(name) for name in ("KIND", "AGENT", "STATE", "HOME", "STARTED", "WORKSPACE")]
        for line in plain[1:]:
            for column in starts:
                assert line[column - 2:column] == "  " and line[column] != " ", (line, column)


def test_list_groups_what_belongs_to_a_start(tmp_path, terminal):
    """Each start with its MCP servers below it; a server two starts use below the one it was started for, naming
    the other; a server of an older version (no started_for) below the start that uses it; one no listed start uses
    after the starts. --json says where each row is shown and what each server was started for."""
    a_registry(tmp_path)
    lines = shown(["ps", "--no-color"])
    assert names_shown(lines) == ["api", "└─ mcp-legacy", "web", "├─ mcp-fetch", "└─ mcp-search", "old", "mcp-idle"]
    fetch = next(line for line in lines if "mcp-fetch" in line)
    assert fetch.endswith("  also serves api"), "no workspace of its own (its run is not listed): who else uses it"
    terminal.encoding = "ascii"  # no box drawing where the output cannot carry it
    assert names_shown(shown(["list", "-nc"]))[1:5] == ["`- mcp-legacy", "web", "|- mcp-fetch", "`- mcp-search"]
    found = {e["name"]: e for e in listed()}
    assert found["mcp-fetch"]["serves"] == ["web", "api"] and found["mcp-fetch"]["started_for"] == "web"
    assert found["mcp-fetch"]["listed_under"] == "web" and found["mcp-search"]["listed_under"] == "web"
    assert found["mcp-legacy"]["started_for"] is None and found["mcp-legacy"]["listed_under"] == "api"
    assert found["mcp-idle"]["started_for"] is None and found["mcp-idle"]["listed_under"] is None
    assert found["mcp-idle"]["serves"] == []
    assert all(found[name]["listed_under"] is None for name in ("api", "web", "old"))
    # The flat listing names every user, the one it was started for first.
    assert next(line for line in shown(["list", "--flat", "-nc"]) if "mcp-fetch" in line).endswith("serves web, api")
    # With a workspace of its own, the others in brackets after it.
    entry = {**found["mcp-fetch"], "workspace": "/srv/fetch"}
    assert st.row(entry, False, "", "web")[-1] == "/srv/fetch  (also serves api)"
    assert st.row({**entry, "serves": ["web"]}, False, "", "web")[-1] == "/srv/fetch"


def test_list_puts_a_helper_below_its_lead(tmp_path, terminal):
    """A run that is another's helper (helper_of: the lead's run) below its lead, and the MCP servers of both below
    the lead too: one level deep, as in the app and the web."""
    make_mcp(tmp_path, "idle", pid=46)
    make_mcp(tmp_path, "legacy", pid=45)
    register("claude-lead0001", mcp_servers=["idle"])
    register("codex-helper99", helper_of="claude-lead0001", mcp_servers=["legacy"], started="2026-11-03T09:00:00Z")
    assert names_shown(shown(["list", "--no-color"])) == [  # below it in start order, as listed
        "claude-lead0001", "├─ mcp-idle", "├─ mcp-legacy", "└─ codex-helper99"]
    assert {e["name"]: e["listed_under"] for e in listed()} == {
        "claude-lead0001": None, "codex-helper99": "claude-lead0001", "mcp-idle": "claude-lead0001",
        "mcp-legacy": "claude-lead0001"}


def test_list_puts_a_teams_members_below_its_lead(tmp_path, terminal):
    """User report: the members of a team (sandbox-ai team) and its board stood apart, as if they belonged to no
    agent. The board below the lead it was started for, the other members below the lead too ("lead": its name, read
    from the team's record); without a record (damaged, another team's name) a member stands on its own."""
    team = st.module("team")
    record = team.record_path("rev")
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 1, "name": "rev", "members": ["claude", "codex"], "lead": "claude",
                                  "workspace": "/w", "options": []}))
    make_start("rev-claude", tmp_path, pid=41, options=["--mcp", "team-rev"])
    make_start("rev-codex", tmp_path, pid=42, options=["--mcp", "team-rev"])
    make_start("work", tmp_path, pid=43, options=["--mcp", "team-rev"])  # not of its name: no member
    register("session-rev-claude", start="rev-claude", mcp_servers=["team-rev"])
    make_mcp(tmp_path, "team-rev", pid=44, started_for={"start": "rev-claude", "run": "session-rev-claude"})
    found = {e["name"]: e for e in listed()}
    assert found["rev-codex"]["lead"] == "rev-claude" and found["rev-codex"]["listed_under"] == "rev-claude"
    assert found["rev-claude"]["lead"] is None and found["rev-claude"]["listed_under"] is None
    assert found["work"]["lead"] is None and found["work"]["listed_under"] is None
    assert found["mcp-team-rev"]["listed_under"] == "rev-claude" and found["mcp-team-rev"]["owner_recorded"] is True
    assert names_shown(shown(["list", "--no-color"]))[:3] == ["rev-claude", "├─ rev-codex", "└─ mcp-team-rev"]
    record.write_text("{damaged")
    found = {e["name"]: e for e in listed()}
    assert found["rev-codex"]["lead"] is None and found["rev-codex"]["listed_under"] is None


def test_an_mcp_server_says_whether_it_recorded_its_start():
    """owner_recorded: its mcp.json has started_for (null: started on its own); not before 0.1.78, whose servers the
    app places by their start time."""
    make_mcp(None, "alone", started_for=None)
    make_mcp(None, "legacy")
    found = {e["name"]: e for e in listed(["--all"])}
    assert found["mcp-alone"]["owner_recorded"] is True and found["mcp-legacy"]["owner_recorded"] is False


def test_list_flat_is_the_table_of_before(tmp_path, terminal):
    """--flat: one row per entry in the listed order, exactly the table of earlier versions (colour aside)."""
    a_registry(tmp_path)
    register("claude-1234abcd", name="quick", workspace="/w/q")
    make_mcp(tmp_path, "gone")  # stopped: --all
    for argv in ([], ["--all"]):
        expected = []
        old_show(st.entries(bool(argv)), expected.append)
        assert shown(["list", "--flat", "--no-color", *argv]) == expected
        assert [ANSI.sub("", line) for line in shown(["list", "--flat", "--color=always", *argv])] == expected
    assert names_shown(expected) != names_shown(shown(["list", "-nc", "--all"])), "grouped by default"
    expected = []
    old_show([], expected.append)
    shutil.rmtree(sessions.root())
    shutil.rmtree(st.state_root() / "mcp")
    shutil.rmtree(launcher.runs_dir())
    assert shown(["list", "--flat", "-nc"]) == expected == shown(["list", "-nc"])


def test_parents_one_level_and_circles(scenario):
    """parents(): below the top of the chain; a circle stays at the top; a reference to nothing listed is none."""
    if scenario == "happy":
        found = [{"name": "lead", "kind": "session", "run": "r1"},
                 {"name": "helper", "kind": "run", "run": "r2", "helper_of": "r1"},
                 {"name": "mcp-x", "kind": "mcp", "serves": ["helper"]}]
        assert st.parents(found) == {1: 0, 2: 0}
    elif scenario == "sad":
        found = [{"name": "a", "kind": "run", "run": "ra", "helper_of": "rb"},
                 {"name": "b", "kind": "run", "run": "rb", "helper_of": "ra"},
                 {"name": "mcp-y", "kind": "mcp", "serves": ["nobody", 5]}]
        assert st.parents(found) == {}
    else:
        found = [{"name": "mcp-z", "kind": "mcp", "serves": "web"}, {"name": "web", "kind": "session"},
                 {"name": "mcp-w", "kind": "mcp", "serves": ["mcp-z", "web"]}]
        assert st.parents(found) == {2: 1}, "an MCP server below a start only"


def test_started_for_records(scenario):
    """mcp.json's started_for: what mcp.py records (the launcher's run and start, or what mcp attach sets) and what
    list reads back (anything else from disk: None)."""
    if scenario == "happy":
        assert mcp.started_for(SimpleNamespace(run_id="claude-12345678", start_key="web", start_id=None)) == {
            "start": "web", "run": "claude-12345678"}
        assert mcp.started_for(SimpleNamespace(started_for={"start": "web", "run": None}, run_id="x")) == {
            "start": "web", "run": None}
        assert st.started_for({"started_for": {"start": "web", "run": 5}}) == {"start": "web", "run": None}
    elif scenario == "sad":
        assert mcp.started_for(SimpleNamespace(backend="podman")) is None  # sandbox-ai mcp start on its own
        assert st.started_for({}) is None and st.started_for({"started_for": "web"}) is None
    else:
        assert mcp.started_for(SimpleNamespace(started_for={"start": 3}, run_id="x")) is None
        assert st.started_for({"started_for": {"start": "", "run": None}}) is None and st.started_for([]) is None


def test_mcp_attach_records_what_it_starts_a_server_for(tmp_path, attaching, monkeypatch):
    out, _ = attaching
    kept("web", tmp_path)
    seen = []
    monkeypatch.setattr(mcp, "ensure_running", lambda name, a, vpn=None: seen.append(a.started_for) or "started")
    mcp.attach("demo", "web", start_server=True, out=out)
    assert seen == [{"start": "web", "run": None}]


def shared_commands():
    """The commands of main.py whose script another command runs too (ps and list, the starts.py commands, devices
    and limits): each must name itself in its usage. The agents' own launcher has a usage of its own."""
    spec = importlib.util.spec_from_file_location("sandbox_main_for_starts_test", ROOT / "sandbox/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    scripts = [script for script, *_ in main.PYTHON_COMMANDS.values()]
    return sorted(command for command, (script, *_) in main.PYTHON_COMMANDS.items()
                  if scripts.count(script) > 1 and script != "agent_sandbox.py" and not command.startswith("_"))


@pytest.mark.parametrize("command", shared_commands())
def test_every_alias_names_itself_in_its_help(command, tmp_path):
    """sandbox-ai ps --help says "sandbox-ai ps", not the name of the command it shares its script with."""
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
           "XDG_CONFIG_HOME": str(tmp_path / "config")}
    result = subprocess.run([str(ROOT / "sandbox-ai"), command, "--help"], capture_output=True, text=True, env=env,
                            timeout=120)
    assert result.returncode == 0 and result.stdout.startswith(f"usage: sandbox-ai {command} "), result.stdout[:200]
    assert "ps" in shared_commands() and "list" in shared_commands()


def test_the_launchers_short_options_after_resume_name_are_its_own():
    """resume's own -m, -w, -r, -j would swallow the launcher's -me, -wa, -ru ... (argparse reads -me as -m e)."""
    long_forms = st.launcher_long_forms(["resume", "web", "-me", "4g", "-ru", "r.json", "-m", "/a:/b", "-r", "--",
                                         "-me"])
    assert long_forms == ["resume", "web", "--memory", "4g", "--rules", "r.json", "-m", "/a:/b", "-r", "--", "-me"]
    assert st.launcher_long_forms(["stop", "web", "-w", "3"]) == ["stop", "web", "-w", "3"]
    assert st.launcher_long_forms(["resume", "web", "-me=4g", "-m=/a:/b"]) == ["resume", "web", "--memory=4g", "-m=/a:/b"]
