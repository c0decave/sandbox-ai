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
import time
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
        if not fields.get("no_terminal"):
            (directory / "tmux" / "attach.sock").touch()  # its terminal relay (sessions.terminal_state)
    data.pop("no_terminal", None)
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
        assert found["web"]["app_tab"] is None and found["old"]["app_tab"] is None
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


def test_a_running_session_without_its_terminal_says_so(tmp_path, monkeypatch):
    """User report from live use (2026-11-14): a session listed as running had no terminal yet - attach failed. Now
    list --json says "terminal": starting while its start goes on (it began less than READY_TIMEOUT ago, or its
    supervisor wrote to its log lately), "missing" (not attachable, attach_why no-terminal, "terminal_log": the end of
    its supervisor.log) once nothing more comes; "ready" with its relay there."""
    make_start("web", tmp_path, pid=41)
    make_start("slow", tmp_path, pid=42, no_terminal=True)
    make_start("stuck", tmp_path, pid=43, no_terminal=True)
    log = sessions.session_dir("slow") / "supervisor.log"
    log.write_text("agent-sandbox: starting the MCP server fetch (its image is pulled once, its sandbox started)…\n"
                   "podman: Copying blob 1/3\n")
    stuck = sessions.session_dir("stuck") / "supervisor.log"
    stuck.write_text("agent-sandbox: starting\nError: the VPN tunnel did not come up\n")
    os.utime(stuck, (1, 1))  # said nothing for long
    found = {e["name"]: e for e in listed()}
    assert found["web"]["terminal"] == "ready" and found["web"]["attachable"] is True
    assert found["slow"]["terminal"] == "starting" and found["slow"]["attachable"] is True
    assert found["slow"]["terminal_step"].startswith("starting the MCP server fetch")  # where its start is
    assert found["stuck"]["terminal"] == "missing" and found["stuck"]["attachable"] is False
    assert found["stuck"]["attach_why"] == "no-terminal"
    assert found["stuck"]["terminal_log"].endswith("Error: the VPN tunnel did not come up")
    lines = []
    st.show(listed(), out=lines.append)
    text = "\n".join(lines)
    assert "running (no terminal: see its log)" in text and "running (starting)" in text
    # A start that began a moment ago: starting, whatever its log says.
    data = sessions.load("stuck")
    assert sessions.terminal_state("stuck", data, clock=lambda: sessions.utc_seconds(data["started"]) + 5)[0] == \
        "starting"
    import calendar
    for text in ("2026-11-02T09:30:00Z", "2000-02-29T23:59:59Z", "1999-01-01T00:00:00Z"):
        assert sessions.utc_seconds(text) == calendar.timegm(sessions.time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    assert sessions.terminal_state("stuck", {**data, "mode": "foreground"}) == ("ready", "")


def test_stop_all_stops_every_start_one_after_another(monkeypatch, capsys):
    """`sandbox-ai stop --all` (user wish, live use 2026-11-14): every running, paused or orphaned start of this user
    on this host - sessions, runs, tasks; not the MCP servers - each as `stop NAME` stops it, asked first with the
    list unless --yes, refused inside a sandbox; a summary, and exit status 1 when one did not stop. Fakes only: no
    start, no Podman, no tmux is touched."""
    listed = [{"name": "web", "kind": "session", "state": "running"},
              {"name": "held", "kind": "session", "state": "paused"},
              {"name": "claude-1a2b", "kind": "run", "state": "running"},
              {"name": "lost", "kind": "run", "state": "orphaned"},
              {"name": "old", "kind": "session", "state": "stopped"},
              {"name": "mcp-fetch", "kind": "mcp", "state": "running"}]
    stopped = []

    def act(command, name, out=print, wait=None, **kw):
        assert command == "stop"
        if name == "lost":
            raise ValueError("its pod could not be stopped")
        stopped.append((name, wait))
    monkeypatch.setattr(st, "entries", lambda *a, **k: listed)
    monkeypatch.setattr(st, "act", act)
    lines = []
    assert st.stop_all(lines.append, wait=5, yes=True, inside=False) == 1, "one did not stop"
    assert stopped == [("web", 5), ("held", 5), ("claude-1a2b", 5)], "one after another; not the MCP server"
    assert lines[-1] == "stopped 3 of 4; not stopped: lost"
    assert "sandbox-ai stop: lost: its pod could not be stopped" in capsys.readouterr().err
    # Asked first with the list; no answer yes: nothing stops.
    stopped.clear()
    asked = []
    assert st.stop_all(lines.append, ask=lambda text: asked.append(text) or "n", inside=False) == 1
    assert stopped == [] and "web (running), held (paused), claude-1a2b (running), lost (orphaned)" in asked[0]
    assert lines[-1] == "nothing stopped"
    listed[3]["state"] = "stopped"
    assert st.stop_all(lines.append, ask=lambda text: "yes", inside=False) == 0 and lines[-1] == "stopped 3 of 3"
    # No terminal to ask on: refused without --yes; inside a sandbox: refused.
    monkeypatch.setattr(st.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="confirm with --yes"):
        st.stop_all(lines.append, inside=False)
    with pytest.raises(ValueError, match="refused inside a sandbox"):
        st.stop_all(lines.append, yes=True, inside=True)
    # Nothing runs: said, nothing done.
    monkeypatch.setattr(st, "entries", lambda *a, **k: [listed[4], listed[5]])
    assert st.stop_all(lines.append, yes=True, inside=False) == 0 and lines[-1] == "nothing to stop: no start runs"
    # The command line: NAME or --all; --yes only with --all.
    calls = []
    monkeypatch.setattr(st, "stop_all", lambda out, wait=None, yes=False: calls.append((wait, yes)) or 0)
    assert st.main(["stop", "--all", "--yes", "--wait", "7"]) == 0 and calls == [(7.0, True)]
    assert st.main(["stop"]) == 1 and st.main(["stop", "web", "--all"]) == 1 and st.main(["stop", "web", "--yes"]) == 1
    assert "stop takes NAME, or --all" in capsys.readouterr().err


def test_show(scenario, tmp_path):
    lines = []
    if scenario == "happy":
        make_start("web", tmp_path, pid=41)
        st.show(st.entries(), lines.append)
        assert lines[0].split() == ["NAME", "KIND", "AGENT", "STATE", "STARTED", "HOME", "WORKSPACE"]
        assert re.fullmatch(r"web +session +codex +running +(\d+|a|an) (minute|hour|day|week|month|year)s? ago +kept "
                            r"+/srv/app", lines[1]), lines[1]
    elif scenario == "sad":
        st.show([], lines.append)
        assert lines == ["Nothing runs and no start is kept. Start one with: sandbox-ai claude (or another agent: "
                         "sandbox-ai --help)"]
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
        assert result.returncode == 0 and result.stdout.startswith("Nothing runs and no start is kept. Start one")
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
    run, tree = Mock(return_value=Mock(returncode=0, stderr="")), Mock()
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


def days_ago(days):
    return st.iso(time.time() - days * 86400)


def move_notice(days):
    """The default expiry's notice, as if it had been given DAYS days ago."""
    path = st.state_root() / st.EXPIRY_FILE
    data = json.loads(path.read_text())
    for key in ("noticed", "said", "last_said"):
        if key in data:
            data[key] = days_ago(days)
    path.write_text(json.dumps(data))


def test_kept_starts_expire_by_default_after_a_notice_and_its_grace(tmp_path):
    """No keep_days: kept starts unused for 90 days go - only stopped ones, and only once that was announced and 14 days
    have passed since (gc says so the first time, and how to switch it off: keep_days 0)."""
    make_start("old", started=days_ago(400), created=days_ago(400))
    make_start("recent", started=days_ago(60), created=days_ago(60))
    make_start("busy", tmp_path, pid=41, created=days_ago(400))
    make_start("lent", tmp_path, pid=42, paused=True, created=days_ago(400))  # paused: in use
    lines = []
    assert st.main(["gc", "--no-compact"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "kept starts unused for more than 90 days are removed" in text and "keep_days 0" in text
    assert "the kept start old" in text and "will be removed from" in text
    assert all(sessions.session_dir(key).exists() for key in ("old", "recent", "busy", "lent"))
    notice = json.loads((st.state_root() / st.EXPIRY_FILE).read_text())
    assert notice["days"] == 90 and notice["names"] == ["old"]
    result = st.gc(compact=False)  # still within the grace: nothing goes
    assert result["removed"] == [] and [e["name"] for e in result["expiring"]] == ["old"]
    assert result["expiry"]["origin"] == "default" and result["expiry"]["days"] == 90
    move_notice(15)
    dry = st.gc(dry_run=True, compact=False)
    assert [e["name"] for e in dry["removed"]] == ["old"] and sessions.session_dir("old").exists()
    lines = []
    assert st.main(["gc", "--no-compact"], out=lines.append) == 0
    assert any(line.startswith("removed the kept start old with its HOME") for line in lines), lines
    assert not sessions.session_dir("old").exists()
    assert all(sessions.session_dir(key).exists() for key in ("recent", "busy", "lent"))


def test_keep_days_0_switches_the_default_expiry_off(tmp_path):
    (tmp_path / "config" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "config" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "keep_days": 0}))
    make_start("old", started=days_ago(400), created=days_ago(400))
    result = st.gc(compact=False)
    assert result["removed"] == [] and result["expiring"] == [] and result["expiry"]["origin"] == "off"
    assert not (st.state_root() / st.EXPIRY_FILE).exists()
    assert st.expiry_setting() == (None, "off") and st.keep_days() is None


def test_the_daily_pass_and_a_start_apply_and_announce_the_default(tmp_path):
    make_start("old", started=days_ago(400), created=days_ago(400))
    assert st.expiry_notice() is None  # nothing announced yet: a start says nothing
    first = st.expire_by_default()  # the daily pass: found, nothing removed, nobody told yet
    assert first["removed"] == [] and [e["name"] for e in first["expiring"]] == ["old"]
    assert first["expiring"][0]["removed_from"] is None
    move_notice(30)  # weeks of daily passes without a start, gc or the app: still nobody told, nothing goes
    assert st.expire_by_default()["removed"] == [] and sessions.session_dir("old").exists()
    said = st.expiry_notice()  # a start tells it: the grace begins now
    assert "old" in said and "keep_days 0" in said and "90 days" in said
    assert st.expiry_notice() is None  # once a day
    move_notice(15)
    done = st.expire_by_default()
    assert [e["name"] for e in done["removed"]] == ["old"] and not sessions.session_dir("old").exists()
    assert st.expiry_notice() is None  # after the grace a start says nothing more


def test_usage_says_what_the_default_expiry_removes(tmp_path):
    make_start("old", started=days_ago(400), created=days_ago(400))
    found = st.usage()
    expiry = found["would_free"]["expiry"]
    assert expiry["origin"] == "default" and expiry["days"] == 90 and expiry["count"] == 1
    assert expiry["names"] == ["old"] and expiry["removed_from"]
    lines = []
    st.show_usage(found, lines.append, colour=False)
    said = " ".join(" ".join(lines).split())  # (the table wraps its last column)
    assert "unused for more than 90 days" in said and "keep_days 0" in said, lines


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
    # Its new start is checked before the stop (one that would fail leaves it running), and the stop says why.
    done = []
    monkeypatch.setattr(sessions, "check_restart", lambda key, mcp=(): done.append(("check", key, list(mcp))))
    monkeypatch.setattr(sessions, "stop", lambda key, *rest, by=None: done.append(("stop", key, by)))
    monkeypatch.setattr(sessions, "resume_locked", lambda key, workspaces=(), mounts=(), mcp=(): done.append(
        ("resume", key, list(mcp))) or (0, True))
    result = mcp.attach("web-search", "web", restart=True, out=out)
    assert result["attached"] == "restarted" and done == [
        ("check", "web", ["web-search"]),
        ("stop", "web", "sandbox-ai mcp attach web-search web --restart (to start it again with the MCP server "
                        "web-search)"),
        ("resume", "web", ["web-search"])]
    # asked in a terminal: yes restarts, no keeps it for the next start
    done.clear()
    assert mcp.attach("demo", "web", ask=lambda question: True, out=out)["attached"] == "restarted"
    assert done[1][:2] == ("stop", "web")
    # A check that fails: not stopped.
    done.clear()

    def refused(key, mcp=()):
        raise ValueError("web was not restarted (it runs on as it was): its image is not there")
    monkeypatch.setattr(sessions, "check_restart", refused)
    with pytest.raises(ValueError, match="runs on as it was"):
        mcp.attach("demo", "web", restart=True, out=out)
    assert done == []


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
    return [line[:line.index("  ")] for line in lines[1:] if not ANSI.sub("", line).startswith("Not shown:")]


def flat_show(found, out):
    """sandbox-ai list --flat, uncoloured: every entry a row of its own (none below another), running first and the
    newest first, STARTED as how long ago, HOME as ~."""
    t = st.module("textout")
    printable = launcher.printable
    rows = [[printable(e["name"]), e["kind"], printable(e.get("agent") or "-"),
             e["state"] + (", expired" if e.get("expired") else ""), t.ago(e.get("started")), e.get("home") or "-",
             printable(t.home_short(e.get("workspace") or ("serves " + ", ".join(e["serves"]) if e.get("serves")
                                                            else "-")))]
            for e in sorted(found, key=st.newest_first)]
    t.table(["NAME", "KIND", "AGENT", "STATE", "STARTED", "HOME", "WORKSPACE"], rows, out, indent=0,
            colour=False, empty=st.EMPTY)


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
    coloured = shown(["list", "-a"])
    assert coloured[0].startswith("\x1b[1mNAME"), "the headers bold"
    row = next(line for line in coloured if line.startswith("\x1b[36mweb"))
    assert "\x1b[36msession\x1b[0m" in row and "\x1b[32mrunning\x1b[0m" in row, "a session cyan, running green"
    server = next(line for line in coloured if "mcp-search" in line)
    assert "\x1b[35mmcp\x1b[0m" in server and "\x1b[35mmcp-search\x1b[0m" in server, "an MCP server magenta"
    assert "\x1b[90mstopped\x1b[0m" in next(line for line in coloured if line.startswith("\x1b[36mold"))
    plain = [ANSI.sub("", line) for line in coloured]
    assert plain != coloured
    assert shown(["ps", "-a", "--no-color"]) == plain and shown(["list", "-a", "-nc"]) == plain
    assert shown(["list", "-a", "--color=never"]) == plain and shown(["list", "-a", "--color", "always", "--no-color"]) == plain
    terminal.tty = False  # piped: none, unless asked for
    assert shown(["list", "-a"]) == plain and shown(["list", "-a", "--color=auto"]) == plain
    assert shown(["list", "-a", "--color=always"]) == coloured and shown(["list", "-a", "-nc", "--color=always"]) == coloured
    terminal.tty = True
    monkeypatch.setenv("NO_COLOR", "1")  # no-color.org
    assert shown(["list", "-a"]) == plain and shown(["list", "-a", "--color=always"]) == coloured
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert shown(["list", "-a"]) == plain
    monkeypatch.setenv("TERM", "xterm")
    for argv in (["list", "--json"], ["ps", "--json", "--color=always"], ["list", "-a", "--json", "--flat"]):
        text = "\n".join(shown(argv))
        assert "\x1b" not in text and json.loads(text), "--json is never coloured"
    st.record("web", "start", "web", "session-web")
    assert shown(["list", "--history", "web", "--color=always"])[0].startswith("\x1b[1mTIME")
    assert all("\x1b" not in line for line in shown(["list", "--history", "web", "--no-color"]))


def test_list_shows_what_runs_by_default_and_the_rest_on_request(tmp_path, terminal):
    """The user's wish (2026-11-16): `sandbox-ai list` shows only what runs (as `docker ps`) - running, paused and
    orphaned starts with their helpers; the MCP servers with --mcp, the stopped starts with --stopped, all of it with
    --all (-a). A line below says what is not shown and the option that shows it; --json lists everything as before
    (scripts, the app and the web interface read it)."""
    a_registry(tmp_path)
    register("claude-1234abcd", name="quick", mcp_servers=["helper-codex-0a1b2c3d"])
    make_mcp(tmp_path, "helper-codex-0a1b2c3d", pid=47, helper="codex")
    plain = [ANSI.sub("", line) for line in shown(["list", "-nc"])]
    names = [name.split()[-1] for name in names_shown(plain)]
    assert "web" in names and "api" in names and "quick" in names, plain
    assert "mcp-helper-codex-0a1b2c3d" in names, "a helper belongs to its lead"
    assert "old" not in names, "stopped: only with --stopped"
    assert not any(name.startswith("mcp-") and "helper" not in name for name in names), "MCP servers: only with --mcp"
    assert "Not shown:" in plain[-1] and "--stopped" in plain[-1] and "--mcp" in plain[-1] and "--all" in plain[-1]
    stopped = [ANSI.sub("", line) for line in shown(["list", "-nc", "--stopped"])]
    assert any(line.startswith("old ") for line in stopped)
    assert not any(" mcp " in line and "helper" not in line for line in stopped)
    with_mcp = "\n".join(shown(["list", "-nc", "--mcp"]))
    assert "mcp-search" in with_mcp and "\nold " not in with_mcp
    everything = "\n".join(shown(["list", "-nc", "-a"]))
    assert "mcp-search" in everything and "\nold " in everything and "mcp-idle" in everything
    assert "Not shown" not in everything
    assert {entry["name"] for entry in listed()} >= {"web", "api", "old", "mcp-search"}, "--json: everything, as before"


def test_list_with_nothing_running_says_what_is_kept(tmp_path, terminal):
    make_start("old")
    plain = [ANSI.sub("", line) for line in shown(["list", "-nc"])]
    assert plain[0].startswith("Nothing runs") and any("1 stopped start" in line and "--stopped" in line
                                                       for line in plain), plain


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
            if line.startswith("Not shown:"):  # the line below the table: what the default leaves out
                continue
            for column in starts:
                assert line[column - 2:column] == "  " and line[column] != " ", (line, column)


def test_list_is_a_readable_table_running_and_newest_first(tmp_path, terminal, monkeypatch):
    """`sandbox-ai list` as `docker ps`: running first, the newest first; STARTED as how long ago; your HOME as ~; a
    workspace too long for the terminal cut from the left with an ellipsis (whole with --no-trunc, or --wide); colour on
    a terminal only; --json in the registry's order with its keys and full values."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "90")
    terminal.encoding = "utf-8"
    now = st.module("textout").utc_seconds(st.now())

    def stamp(ago):
        return st.time.strftime("%Y-%m-%dT%H:%M:%SZ", st.time.gmtime(now - ago))
    long = str(tmp_path / "home" / "src" / ("a-long-folder-name-" * 4) / "app")
    register("claude-0000aaaa", name="older", started=stamp(7200), workspace=str(tmp_path / "home" / "src" / "x"))
    register("claude-1111bbbb", name="newer", started=stamp(300), workspace=long)
    make_start("kept", started=stamp(86400 * 3))
    lines = shown(["list", "-a", "-nc"])
    assert names_shown(lines) == ["newer", "older", "kept"]
    assert lines[0].split() == ["NAME", "KIND", "AGENT", "STATE", "STARTED", "HOME", "WORKSPACE"]
    assert re.search(r"  5 minutes ago  +discarded  +\u2026", lines[1]) and lines[1].endswith("/app"), lines[1]
    assert all(len(line) <= 90 for line in lines), lines
    assert re.search(r"  2 hours ago  +discarded  +~/src/x$", lines[2]) and "  3 days ago  " in lines[3]
    for flag in ("--no-trunc", "--wide"):
        whole = "\n".join(shown(["list", "-a", "-nc", flag]))
        assert "\u2026" not in whole and "~/src/a-long-folder-name-" in whole
    assert names_shown(shown(["list", "-a", "-nc", "--flat"])) == ["newer", "older", "kept"]
    terminal.tty = False
    assert "\x1b" not in "".join(shown(["list", "-a"])), "no colour in a pipe"
    found = listed()
    assert [e["name"] for e in found] == ["older", "newer", "kept"], "--json keeps the registry's order"
    assert next(e for e in found if e["name"] == "newer")["workspace"] == long and \
        next(e for e in found if e["name"] == "newer")["started"] == stamp(300)


def test_list_groups_what_belongs_to_a_start(tmp_path, terminal):
    """Each start with its MCP servers below it; a server two starts use below the one it was started for, naming
    the other; a server of an older version (no started_for) below the start that uses it; one no listed start uses
    after the starts. --json says where each row is shown and what each server was started for."""
    a_registry(tmp_path)
    lines = shown(["ps", "-a", "--no-color"])
    assert names_shown(lines) == ["api", "└─ mcp-legacy", "web", "├─ mcp-fetch", "└─ mcp-search", "old", "mcp-idle"]
    fetch = next(line for line in lines if "mcp-fetch" in line)
    assert fetch.endswith("  also serves api"), "no workspace of its own (its run is not listed): who else uses it"
    terminal.encoding = "ascii"  # no box drawing where the output cannot carry it
    assert names_shown(shown(["list", "-a", "-nc"]))[1:5] == ["`- mcp-legacy", "web", "|- mcp-fetch", "`- mcp-search"]
    found = {e["name"]: e for e in listed()}
    assert found["mcp-fetch"]["serves"] == ["web", "api"] and found["mcp-fetch"]["started_for"] == "web"
    assert found["mcp-fetch"]["listed_under"] == "web" and found["mcp-search"]["listed_under"] == "web"
    assert found["mcp-legacy"]["started_for"] is None and found["mcp-legacy"]["listed_under"] == "api"
    assert found["mcp-idle"]["started_for"] is None and found["mcp-idle"]["listed_under"] is None
    assert found["mcp-idle"]["serves"] == []
    assert all(found[name]["listed_under"] is None for name in ("api", "web", "old"))
    # The flat listing names every user, the one it was started for first.
    assert next(line for line in shown(["list", "-a", "--flat", "-nc"]) if "mcp-fetch" in line).endswith("serves web, api")
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
    assert names_shown(shown(["list", "-a", "--no-color"])) == [  # below it in start order, as listed
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
    assert names_shown(shown(["list", "-a", "--no-color"]))[:3] == ["rev-claude", "├─ rev-codex", "└─ mcp-team-rev"]
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


def test_list_flat_is_one_row_per_entry(tmp_path, terminal):
    """--flat: one row per entry, nothing below another; running first, the newest first (colour aside)."""
    a_registry(tmp_path)
    register("claude-1234abcd", name="quick", workspace="/w/q")
    make_mcp(tmp_path, "gone")  # stopped: --all
    expected = []  # (the default leaves stopped starts and MCP servers out: test_list_shows_what_runs_by_default...)
    flat_show(st.entries(True), expected.append)
    assert shown(["list", "-a", "--flat", "--no-color"]) == expected
    assert [ANSI.sub("", line) for line in shown(["list", "-a", "--flat", "--color=always"])] == expected
    assert names_shown(expected) != names_shown(shown(["list", "-a", "-nc"])), "grouped by default"
    expected = []
    flat_show([], expected.append)
    shutil.rmtree(sessions.root())
    shutil.rmtree(st.state_root() / "mcp")
    shutil.rmtree(launcher.runs_dir())
    assert shown(["list", "-a", "--flat", "-nc"]) == expected == shown(["list", "-a", "-nc"])


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
    assert result.returncode == 0 and result.stdout.startswith(f"Usage:  sandbox-ai {command} "), result.stdout[:200]
    assert "ps" in shared_commands() and "list" in shared_commands()


def test_the_launchers_short_options_after_resume_name_are_its_own():
    """resume's own -m, -w, -r, -j would swallow the launcher's -me, -wa, -ru ... (argparse reads -me as -m e)."""
    long_forms = st.launcher_long_forms(["resume", "web", "-me", "4g", "-ru", "r.json", "-m", "/a:/b", "-r", "--",
                                         "-me"])
    assert long_forms == ["resume", "web", "--memory", "4g", "--rules", "r.json", "-m", "/a:/b", "-r", "--", "-me"]
    assert st.launcher_long_forms(["stop", "web", "-w", "3"]) == ["stop", "web", "-w", "3"]
    assert st.launcher_long_forms(["resume", "web", "-me=4g", "-m=/a:/b"]) == ["resume", "web", "--memory=4g", "-m=/a:/b"]


def test_list_names_the_app_tab_a_running_start_was_started_in(tmp_path):
    make_start("web", tmp_path, pid=41, app_tab="0123456789abcdef")
    make_start("old", app_tab="fedcba9876543210")  # stopped: no tab to attach any more
    found = {e["name"]: e for e in listed()}
    assert found["web"]["app_tab"] == "0123456789abcdef" and found["old"]["app_tab"] is None


def test_usage_summary_counts_podman_and_explains_its_reclaimable():
    """The summary of gc --usage: sandbox-ai's parts and Podman's, sorted by size, each share of the whole; what gc
    frees per part (Podman's own "reclaimable" counts every software's images: said in HOW, not counted)."""
    result = {"total": 3 << 30, "parts": {"starts": {"bytes": 2 << 30}, "logs": {"bytes": 1 << 30},
                                          "mcp": {"bytes": 0}, "data": {"bytes": 0}},
              "podman": {"images": {"total": 842, "active": 3, "bytes": 59 << 30, "reclaimable": 56 << 30},
                         "volumes": {"total": 85, "active": 0, "bytes": 1 << 30, "reclaimable": 1 << 30},
                         "dangling": {"total": 8, "bytes": 10 << 30, "images": []}},
              "would_free": {"leftovers": {"count": 0, "bytes": 0}, "compress": {"bytes": 400, "saves": 300},
                             "expiry": {"origin": "default", "due": {"count": 0, "bytes": 0}},
                             "caches": {"count": 2, "bytes": 1 << 30}, "rootfs": {"count": 0, "bytes": 0},
                             "images": {"count": 8, "bytes": 10 << 30}}}
    summary = st.usage_summary(result)
    keys = [row["key"] for row in summary["rows"]]
    assert keys[:3] == ["podman_images", "starts", "logs"] and "mcp" not in keys  # empty parts are left out
    rows = {row["key"]: row for row in summary["rows"]}
    assert summary["total"] == (3 << 30) + (60 << 30)
    assert rows["podman_images"]["reclaimable"] == 10 << 30 and "56.0 GiB" in rows["podman_images"]["how"]
    assert rows["starts"]["reclaimable"] == 1 << 30 and "--caches" in rows["starts"]["how"]
    assert rows["logs"]["reclaimable"] == 300
    assert rows["podman_volumes"]["reclaimable"] == 0 and "podman volume prune" in rows["podman_volumes"]["how"]
    # without Podman: only sandbox-ai's parts; nothing at all: no division by zero
    assert [row["key"] for row in st.usage_summary({**result, "podman": None})["rows"]] == ["starts", "logs"]
    empty = st.usage_summary({"total": 0, "parts": {}, "podman": None, "would_free": {}})
    assert empty == {"rows": [], "total": 0, "reclaimable": 0}


def test_show_usage_tables_and_colour(tmp_path):
    make_start("old", started=days_ago(400), created=days_ago(400))
    home = sessions.session_dir("old") / "home"
    (home / ".cache/pip").mkdir(parents=True)
    (home / ".cache/pip/w").write_bytes(b"x" * 300_000)
    found = st.usage()
    item = found["parts"]["starts"]["items"][0]
    assert item["expires_why"] == "default" and item["expires"]
    plain, coloured = [], []
    st.show_usage(found, plain.append, colour=False)
    st.show_usage(found, coloured.append, colour=True)
    assert not any("\033" in line for line in plain) and any("\033[" in line for line in coloured)
    row = next(line for line in plain if line.lstrip().startswith("old "))
    assert "stopped, kept" in row and "ago" in row
    assert any("Total" in line and "sandbox-ai gc --caches --images" in line for line in plain)
    lines = []
    st.show_usage(found, lines.append, as_json=True)
    assert json.loads("\n".join(lines))["summary"]["rows"]


def test_a_packed_home_is_unpacked_before_any_start_uses_it(tmp_path, monkeypatch):
    """gc --pack's archive: launch (every start and resume) and --restore-home unpack it first, under the start's
    lock - never an empty HOME in its place; one that cannot be unpacked stops the start, the archive stays."""
    make_start("web", started="2026-11-01T00:00:00Z", keep=True)
    directory = sessions.session_dir("web")
    (directory / "home/notes.md").write_text("mine\n")
    packs = sessions.homepack()
    monkeypatch.setattr(packs, "zstd", lambda: None)
    packs.pack(directory)
    assert not (directory / "home").exists()
    data = sessions.load("web")
    data.pop("pod", None)
    seen = []

    def popen(*args, **kwargs):
        seen.append((directory / "home/notes.md").read_text())
        raise RuntimeError("stop before the supervisor")
    monkeypatch.setattr(sessions.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="stop before"):
        sessions.launch("web", data, [], SimpleNamespace(backend="podman", podman_url=None, binary=False))
    assert seen == ["mine\n"] and not packs.packed(directory)
    # --restore-home: unpacked first (then: no record of its HOME to restore from)
    packs.pack(directory)
    with pytest.raises(ValueError, match="no record of its HOME"):
        sessions.restore_home("web", yes=True)
    assert (directory / "home/notes.md").read_text() == "mine\n"
    # damaged: the start stops, the archive stays, no empty HOME
    record = packs.pack(directory)
    (directory / record["archive"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="could not be unpacked"):
        sessions.launch("web", data, [], SimpleNamespace(backend="podman", podman_url=None, binary=False))
    assert not (directory / "home").exists() and (directory / record["archive"]).exists()


# -- list --folders: the host folders of the starts (the desktop app's Files page reads it per host) --

def folders_listed(argv=()):
    lines = []
    assert st.main(["list", "--folders", "--json", *argv], out=lines.append) == 0
    return json.loads("\n".join(lines))


def test_list_folders_names_each_running_and_kept_starts_workspaces_and_home(tmp_path):
    """Contract of `sandbox-ai list --folders --json` (the app's backend.host_file_roots reads it over SSH): a list of
    {kind, name, id, path}; kinds as the Files page names them; each folder once, links resolved, only those there."""
    ws = {name: tmp_path / name for name in ("web-ws", "web-extra", "run-ws", "run-home", "old-ws", "old-extra")}
    for path in ws.values():
        path.mkdir()
    (tmp_path / "link-to-run-ws").symlink_to(ws["run-ws"])
    # A running session (its run registered with the start's id), its workspaces as the launcher checked them.
    make_start("web", tmp_path, pid=41, workspace=str(ws["web-ws"]))
    register("session-web", start="web", agent_home=str(sessions.session_dir("web") / "home"),
             workspace=str(ws["web-ws"]),
             workspace_checks={"workspaces": [{"host": str(ws["web-ws"]), "sandbox": "/work", "mode": "rw"},
                                              {"host": str(ws["web-extra"]), "sandbox": "/workspaces/web-extra",
                                               "mode": "rw"}]})
    # A plain run (no start directory), its workspace given through a link, its HOME of its own.
    register("claude-1234abcd", name="quick", workspace=str(tmp_path / "link-to-run-ws"),
             agent_home=str(ws["run-home"]))
    # A stopped kept session: its workspace, another of its options, its HOME; one whose workspace is gone.
    make_start("old", workspace=str(ws["old-ws"]), cwd=str(tmp_path),
               options=["--backend", "podman", "--workspace", f"{ws['old-ws']},{ws['old-extra']}"])
    make_start("gone", workspace=str(tmp_path / "deleted"))
    # An MCP server's sandbox is no start of an agent: not listed.
    register("mcp-demo", workspace=str(ws["run-ws"]), kind="mcp")
    found = folders_listed()
    assert all(set(entry) == {"kind", "name", "id", "path"} for entry in found), found
    assert all(entry["kind"] in st.FOLDER_KINDS for entry in found)
    shown = [(entry["kind"], entry["name"], entry["path"]) for entry in found]
    assert ("run", "web", str(ws["web-ws"])) in shown and ("run", "web", str(ws["web-extra"])) in shown
    assert ("session-home", "web", str(sessions.session_dir("web") / "home")) in shown
    assert ("run", "quick", str(ws["run-ws"])) in shown, "a link resolved"
    assert ("run-home", "quick", str(ws["run-home"])) in shown
    assert ("session", "old", str(ws["old-ws"])) in shown and ("session", "old", str(ws["old-extra"])) in shown
    assert ("session-home", "old", str(sessions.session_dir("old") / "home")) in shown
    assert ("session", "gone", str(tmp_path / "deleted")) not in shown, "a folder that is not there"
    assert ("session-home", "gone", str(sessions.session_dir("gone") / "home")) in shown
    assert len({entry["path"] for entry in found}) == len(found), "each folder once"
    assert {entry["id"] for entry in found} == {"web", "claude-1234abcd", "old", "gone"}
    # The table for a human: one row per folder.
    lines = []
    assert st.main(["list", "--folders"], out=lines.append) == 0
    assert lines[0].split() == ["NAME", "KIND", "FOLDER"] and len(lines) == len(found) + 1


def test_list_folders_builds_the_launchers_parser_once_per_agent_not_once_per_start(tmp_path, monkeypatch):
    """Each kept start's options are read as the launcher reads them (workspaces.next_start_targets: parse_args), and
    each reading built the launcher's whole parser anew, 150 options: 4 ms a start, most of `list --folders` (1000
    kept starts: 4.2 s). One parser per agent for the listing, the same folders."""
    workspaces = st.module("workspaces")
    made, expected = {}, []
    for index in range(12):
        agent = ("codex", "claude", "opencode")[index % 3]
        folders = [tmp_path / f"ws-{index}", tmp_path / f"extra-{index}", tmp_path / f"named-{index}"]
        for folder in folders:
            folder.mkdir()
        options = ["--backend", "podman", "--workspace", f"{folders[0]},{folders[1]}", "--workspace", str(folders[2]),
                   "--workspace-name", f"given{index}={folders[2]}", "--preset", "github", "--allow-host", "example.org"]
        made[f"s{index:02d}"] = make_start(f"s{index:02d}", agent=agent, workspace=str(folders[0]), cwd=str(tmp_path),
                                           options=options)
        expected += [("session", f"s{index:02d}", str(folder)) for folder in folders]
        expected.append(("session-home", f"s{index:02d}", str(sessions.session_dir(f"s{index:02d}") / "home")))
    (tmp_path / "broken-ws").mkdir()
    make_start("broken", agent="codex", workspace=str(tmp_path / "broken-ws"), cwd=str(tmp_path),
               options=["--no-such-option", "--workspace", str(tmp_path)])  # (not readable: its workspace alone)
    one_by_one = {key: workspaces.next_start_targets(data, data["options"]) for key, data in made.items()}
    l = workspaces.module("agent_sandbox")
    built, real = [], l.build_parser

    def build_parser(agent=None):
        built.append(agent)
        return real(agent)
    monkeypatch.setattr(l, "build_parser", build_parser)
    found = folders_listed()
    assert sorted(built) == ["claude", "codex", "opencode"], built
    assert [(entry["kind"], entry["name"], entry["path"]) for entry in found if entry["name"] != "broken"] == expected
    assert [(entry["kind"], entry["path"]) for entry in found if entry["name"] == "broken"] == [
        ("session", str(tmp_path / "broken-ws")), ("session-home", str(sessions.session_dir("broken") / "home"))]
    # The same reading with a parser used again as with one of its own, for every start.
    shared = {}
    assert {key: workspaces.next_start_targets(data, data["options"], shared) for key, data in made.items()} == one_by_one
    assert sorted(shared) == ["claude", "codex", "opencode"]
    assert one_by_one["s04"][2][1] == "/workspaces/given4"


def test_list_folders_reads_only_and_says_what_it_goes_without(tmp_path, capsys):
    assert folders_listed() == []
    lines = []
    assert st.main(["list", "--folders"], out=lines.append) == 0 and lines == ["No folders: nothing runs or is kept here."]
    # Read only: an ended start without a HOME stays for list to prune (list --folders removes nothing).
    make_start("done", keep=False)
    (sessions.session_dir("done") / "exit.json").write_text(json.dumps({"ended": "2026-11-01T00:00:00Z"}))
    shutil.rmtree(sessions.session_dir("done") / "home")
    folders_listed()
    assert sessions.session_dir("done").is_dir()
    for extra in (["--history", "web"], ["--all"], ["--flat"], ["--all-users"]):
        assert st.main(["list", "--folders", *extra], out=lambda line: None) == 1
        assert "--folders goes without" in capsys.readouterr().err
