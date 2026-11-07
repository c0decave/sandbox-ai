"""Several agents in one setup, mode 3: a team (sandbox-ai team). One sandbox per agent on a shared workspace, each a
named session, the first one leading; a task board between them as the MCP server team-NAME (team_board.py)."""
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tb = load("team_board_for_tests", "team_board.py")
team = load("team_for_tests", "team.py")


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


# --- the board -----------------------------------------------------------------------------------------

def board(tmp_path, members=("claude", "codex")):
    clock = {"t": 0.0}
    return tb.Board(list(members), tmp_path, clock=lambda: clock["t"],
                    sleep=lambda s: clock.update(t=clock["t"] + s)), clock


def test_a_task_goes_from_the_lead_to_a_member_and_back(tmp_path):
    lead, _ = board(tmp_path)
    member = tb.Board(["claude", "codex"], tmp_path)  # another connection: another instance, the same file
    assert lead.post("codex", "write the tests", sender="claude") == {"id": 1}
    assert member.wait_for_task("codex", 0) == {"id": 1, "from": "claude", "task": "write the tests"}
    assert member.wait_for_task("claude", 0)["id"] is None, "it is codex's now: nobody else gets it"
    assert member.wait_for_task("codex", 0)["again"] is True, "codex gets it again until it reports"
    assert lead.wait_for_answer(1, 0)["state"] == "working"
    member.done(1, "12 tests, all pass")
    assert lead.wait_for_answer(1, 0) == {"id": 1, "state": "done", "answer": "12 tests, all pass"}
    assert lead.board()["tasks"] == [{"id": 1, "from": "claude", "to": "codex", "state": "done",
                                      "task": "write the tests"}]
    with pytest.raises(ValueError, match="done already"):
        member.done(1, "again")


def test_waits_end_in_time(tmp_path):
    lead, clock = board(tmp_path)
    assert lead.wait_for_task("codex", 5)["task"] is None and clock["t"] >= 5
    lead.post("codex", "x")
    assert lead.wait_for_answer(1, 3)["state"] == "open"


def test_the_board_refuses_what_it_cannot_hold(tmp_path):
    lead, _ = board(tmp_path)
    for call in (lambda: lead.post("gemini", "x"), lambda: lead.post("codex", ""), lambda: lead.post("codex", 3),
                 lambda: lead.post("codex", "x" * (tb.TASK_LIMIT + 1)), lambda: lead.wait_for_task("nobody"),
                 lambda: lead.wait_for_task("codex", tb.WAIT_LIMIT + 1), lambda: lead.wait_for_task("codex", True),
                 lambda: lead.done(99, "x"), lambda: lead.done("1", "x"), lambda: lead.wait_for_answer(None)):
        with pytest.raises(ValueError):
            call()
    with pytest.raises(ValueError):
        tb.Board(["claude"], tmp_path)  # a team has two members at least
    (tmp_path / tb.BOARD).write_text(json.dumps({"next": 1, "tasks": [{"id": 1, "state": "evil"}]}))
    with pytest.raises(ValueError, match="damaged"):
        lead.board()
    (tmp_path / tb.BOARD).unlink()
    (tmp_path / tb.BOARD).symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(OSError):
        lead.post("codex", "x")  # never follows a link planted for it


def test_old_done_tasks_make_room_open_ones_never_go(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "TASKS", 3)
    lead, _ = board(tmp_path)
    for k in range(3):
        lead.post("codex", f"t{k}")
    lead.wait_for_task("codex", 0)
    lead.done(1, "ok")
    lead.post("codex", "t3")
    assert [t["id"] for t in lead.board()["tasks"]] == [2, 3, 4]
    with pytest.raises(ValueError, match="the board is full"):
        lead.post("codex", "t4")  # nothing done to drop: the human's open tasks stay
    assert [t["id"] for t in lead.board()["tasks"]] == [2, 3, 4], "and nothing was written"


def test_a_task_whose_handing_out_was_lost_comes_again(tmp_path):
    """The agent's MCP call ran past its own timeout: the task is "working", nobody read it. It comes back."""
    lead, _ = board(tmp_path)
    lead.post("codex", "migrate the schema")
    assert lead.wait_for_task("codex", 0)["id"] == 1  # this answer is lost on the way
    again = lead.wait_for_task("codex", 0)
    assert again == {"id": 1, "from": "", "task": "migrate the schema", "again": True}
    lead.done(1, "migrated")
    assert lead.wait_for_task("codex", 0)["id"] is None


def test_the_board_stays_readable(tmp_path, monkeypatch):
    lead, _ = board(tmp_path)
    lead.post("codex", "ü" * 10)
    assert "ü" in (tmp_path / tb.BOARD).read_text(encoding="utf-8"), "UTF-8, not six bytes per character"
    monkeypatch.setattr(tb, "BOARD_LIMIT", 2000)
    with pytest.raises(ValueError, match="the board is full"):
        lead.post("codex", "ü" * 1500)
    assert len(lead.board()["tasks"]) == 1, "what was there is still readable"
    before = (tmp_path / tb.BOARD).stat().st_mtime_ns
    lead.wait_for_task("claude", 0)
    lead.board()
    assert (tmp_path / tb.BOARD).stat().st_mtime_ns == before, "a wait that changes nothing writes nothing"


def test_the_board_speaks_mcp(tmp_path):
    lead, _ = board(tmp_path, ("claude", "codex", "opencode"))
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "post", "arguments": {"to": "opencode", "task": "docs", "sender": "claude"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "rm", "arguments": {}}}]
    out = io.StringIO()
    lead.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["post", "wait_for_task", "done", "wait_for_answer",
                                                                  "board"]
    assert replies[1]["result"]["tools"][0]["inputSchema"]["properties"]["to"]["enum"] == ["claude", "codex",
                                                                                            "opencode"]
    assert json.loads(replies[2]["result"]["content"][0]["text"]) == {"id": 1}
    assert replies[3]["result"]["isError"]


def test_many_connections_never_lose_a_task(tmp_path):
    def poster(k):
        tb.Board(["claude", "codex"], tmp_path).post("codex", f"task {k}")
    threads = [threading.Thread(target=poster, args=(k,)) for k in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tasks = tb.Board(["claude", "codex"], tmp_path).board()["tasks"]
    assert sorted(t["id"] for t in tasks) == list(range(1, 21))


# --- the team ------------------------------------------------------------------------------------------

def test_start_runs_one_session_per_agent_with_its_role(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    lines = []
    assert team.start("app", ["claude", "codex", "opencode"], tmp_path, ["--allow-pypi", "--with", "opencode"],
                      lines.append, run) == 0
    assert [argv[3] for argv in calls] == ["claude", "codex", "opencode"]
    for argv, agent in zip(calls, ("claude", "codex", "opencode")):
        assert argv[argv.index("--session") + 1] == f"app-{agent}"
        assert argv[argv.index("--mcp") + 1] == "team-app" and argv[argv.index("--workspace") + 1] == str(tmp_path)
        assert "--allow-pypi" in argv[:argv.index("--")]
    assert "You lead the team app" in calls[0][-1] and "codex, opencode" in calls[0][-1]
    assert "You are codex in the team app" in calls[1][-1] and "wait_for_task" in calls[1][-1]
    assert calls[2][-2] == "--prompt", "OpenCode takes its first prompt as an option"
    assert lines[-1] == "sandbox-ai team: talk to the lead with: sandbox-ai attach app-claude"
    data = team.load("app")
    assert data["lead"] == "claude" and data["members"] == ["claude", "codex", "opencode"]
    with pytest.raises(ValueError, match="exists"):
        team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run)


def test_the_boards_definition_is_an_offline_mcp_server(tmp_path):
    team.start("app", ["codex", "claude"], tmp_path, [], lambda line: None,
               lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    mcp = load("mcp_for_team_tests", "mcp.py")
    definition = mcp.definitions()["team-app"]
    assert definition["command"] == ["python3", "-I", "/opt/sandbox-ai/team_board.py", "--members", "codex,claude"]
    assert "--offline" in definition["options"] and definition["workspace"] == str(team.teams_dir() / "app/board")
    assert mcp.validated({"team-app": definition}, "test")  # it passes the checks of every definition


def test_names_agents_and_options_are_checked(tmp_path):
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    for name in ("", "Upper", "a" * 21, "../x", "-x"):
        with pytest.raises(ValueError, match="name"):
            team.start(name, ["claude", "codex"], tmp_path, [], print, run)
    for agents in (["claude"], ["claude", "claude"], ["claude", "gemini"]):
        with pytest.raises(ValueError, match="--agents"):
            team.start("x", agents, tmp_path, [], print, run)
    for options in (["--session=a"], ["--workspace", "/x"], ["-s", "a"], ["-w", "/x"], ["-W"], ["-N", "n"],
                    ["--mcp-socket-dir", "/x"], ["--config", "/c.json"]):
        with pytest.raises(ValueError, match="chooses these itself"):
            team.start("y", ["claude", "codex"], tmp_path, options, print, run)
    (team.teams_dir() / "left").mkdir(parents=True)
    with pytest.raises(ValueError, match="exists"):  # a directory left over, no record: never a traceback
        team.start("left", ["claude", "codex"], tmp_path, [], print, run)


def test_a_session_of_the_same_name_is_never_taken_over(tmp_path):
    """Another project's stopped session app-claude: the team would resume it (its options, its workspace) and
    team delete would remove its HOME. Refused before anything starts."""
    run = lambda argv, **kw: (_ for _ in ()).throw(AssertionError("started"))  # noqa: E731
    with pytest.raises(ValueError, match="in use already: app-claude"):
        team.start("app", ["claude", "codex"], tmp_path, [], print, run, taken=["app-claude"])
    assert not (team.teams_dir() / "app").exists()


def test_a_member_that_does_not_start_is_named(tmp_path):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1 if argv[3] == "codex" else 0, "", "Agent login missing")
    lines = []
    assert team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run) == 1
    assert any("app-codex did not start" in line and "login missing" in line for line in lines)
    assert any("sandbox-ai team stop app" in line for line in lines)


def test_post_show_stop_and_delete(tmp_path, monkeypatch):
    team.start("app", ["claude", "codex"], tmp_path, [], lambda line: None,
               lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    lines = []
    assert team.post("app", "codex", "look at the logs", lines.append) == 0
    tb.Board(["claude", "codex"], team.teams_dir() / "app/board").post("claude", "\x1b]52;c;ZXZpbA==\x07 hi",
                                                                       sender="\x1b[2J")
    monkeypatch.setattr(team, "member_states", lambda data: {"claude": "running", "codex": "running"})
    team.show("app", lines.append)
    assert any("app-codex" in line and "member" in line and "running" in line for line in lines)
    assert any("#1" in line and "human -> codex" in line and "look at the logs" in line for line in lines)
    assert not any("\x1b" in line for line in lines), "a member's text never reaches the terminal as a sequence"
    assert any("\\x1b]52" in line for line in lines)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[3:])
        return subprocess.CompletedProcess(argv, 0, "", "")
    running = {"claude": "running", "codex": "running"}
    assert team.stop("app", lines.append, run, running) == 0
    assert calls == [["stop", "app-codex"], ["stop", "app-claude"], ["stop", "team-app"]]
    with pytest.raises(ValueError, match="--yes"):
        team.delete("app", False, lines.append, run, running)
    # A member that cannot be stopped: stop says so, delete keeps the team.
    failing = lambda argv, **kw: subprocess.CompletedProcess(argv, 1 if argv[3] == "stop" and argv[4] == "app-codex"  # noqa: E731
                                                              else 0, "", "busy")
    assert team.stop("app", lines.append, failing, running) == 1
    assert team.delete("app", True, lines.append, failing, running) == 1 and team.load("app")
    calls.clear()
    mcp = load("mcp_for_team_delete_test", "mcp.py")
    mcp.state_dir("team-app").mkdir(parents=True)
    assert team.delete("app", True, lines.append, run, {"claude": "stopped", "codex": "running"}) == 0
    assert ["stop", "app-claude"] not in calls, "a stopped member is not stopped again"
    assert ["delete", "app-claude", "--yes"] in calls and not (team.teams_dir() / "app").exists()
    assert not mcp.state_dir("team-app").exists(), "the board server's state goes too"


def test_a_damaged_team_never_stops_another_start(tmp_path):
    record = team.teams_dir() / "bad" / "team.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"name": "bad", "members": [1, {"x": 2}], "lead": 1}))
    (team.teams_dir() / "worse").mkdir()
    (team.teams_dir() / "worse" / "team.json").write_text("not json")
    mcp = load("mcp_for_damaged_team_test", "mcp.py")
    assert "team-bad" not in mcp.definitions() and "demo" in mcp.definitions()
    with pytest.raises(ValueError, match="damaged"):
        team.load("bad")


def test_the_command_line(tmp_path, capsys):
    assert team.main(["list"]) == 0
    assert team.main(["show", "nope"]) == 1 and "no team nope" in capsys.readouterr().err
    main = load("main_for_team_tests", "main.py")
    assert main.PYTHON_COMMANDS["team"] == ("team.py",)
    assert shlex.split("sandbox-ai team start app --agents claude,codex -- --allow-pypi")
