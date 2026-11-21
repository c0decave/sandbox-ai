"""The Board's team start gets what a single start has since 0.1.156 (user 2026-11-19): the session check before it
- a team of that name runs (Attach to its lead), has other members or a member gone, or a member's session name is
another start's: said before anything runs, with Attach to its lead / Stop it and start / Start as another name /
Cancel - and the login question for every member's agent on the board's host (login --status --json there), Codex's
device sign-in included; team start then runs with --no-login-question, so that its terminal does not ask again.

The page offscreen with team start's verdicts as the real one gives them, then end to end: the real ./sandbox-ai of
another host (fake ssh, fake Podman: tests/live_fakes.py) with a team whose members run there."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import board_view, prerequisites, session_start, terminal, widgets  # noqa: E402
from test_board_team import teamed  # noqa: E402,F401 - the fixture
from test_board_view import page  # noqa: E402,F401 - the fixture
from test_remote_session_start import asked, fleet, make_board, with_login_terminal  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME to a checkout")

RUNNING = {"ok": False, "code": "exists", "error": "team rev runs already - rev-claude: running (a session of claude, in "
           "tmux, its supervisor (PID 7) runs, its terminal is there); rev-codex: running (...)",
           "team": {"members": ["claude", "codex"], "lead": "claude", "same": True, "verdict": "attach",
                    "states": {"claude": "running", "codex": "running"},
                    "seen": {"claude": {"state": "running", "attachable": True, "terminal": "ready"},
                             "codex": {"state": "running", "attachable": True, "terminal": "ready"}}}}
OK = {"ok": True, "name": "rev", "plan": [{"session": "rev-claude"}, {"session": "rev-codex"}], "logins": []}


def at_once(function, *args, done, **_kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


@pytest.fixture
def team(teamed, monkeypatch):
    page, node = teamed
    page.verdicts = OK  # (team start's verdict as this sandbox-ai gives it: with the logins it lacks)
    for module in (session_start, prerequisites):
        monkeypatch.setattr(module, "background", at_once)
    page.check()
    return page, node


def started_words(page):
    argv = page.started[-1][0]
    return argv[argv.index("team"):] if "team" in argv else argv


def test_a_team_that_runs_is_attached_to_its_lead(team, monkeypatch):
    page, node = team
    page.verdicts = RUNNING
    page.board.first("network").props["--allow-pypi"] = True  # (asked anew)
    page.check()
    assert [p.level for p in page.team_findings()] == ["warning"]
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "attach")
    assert page.start() is False
    question, = asked("Team rev")
    assert "Team rev runs already on this machine." in question and "rev-codex: running" in question
    argv, title, _embedded, _cwd = page.started[-1]
    assert argv == page.sai.attach_argv("local", "rev-claude") and title == "session rev-claude"
    assert [shown for shown, _text, _ in widgets.SHOWN].count("Team rev") == 1


def test_start_as_another_name_renames_the_team_and_starts_it(team, monkeypatch):
    page, node = team
    page.verdicts = RUNNING
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    checked = []

    def team_check(host, **values):
        checked.append(values["name"])
        return RUNNING if values["name"] == "rev" else {**OK, "name": values["name"]}
    monkeypatch.setattr(page.sai, "team_check", team_check)
    page.start()
    name = page.board.node(node.id).props["name"]
    assert name != "rev" and name.startswith("rev-") and len(name) <= 20 and checked[-1] == name
    words = started_words(page)
    assert words[words.index("--") - 1] == name, "started under the fresh name, after team start's check of it"
    page.undo.undo()
    assert page.board.node(node.id).props["name"] == "rev", "the rename is one step of Undo"


def test_stop_it_and_start_stops_the_team_and_continues_it(team, monkeypatch):
    page, node = team
    page.verdicts = RUNNING
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "stop")
    stopped = []
    monkeypatch.setattr(page.sai, "team_stop", lambda host, name: stopped.append((host, name)) or (0, "", ""))
    monkeypatch.setattr(page.sai, "team_check", lambda host, **values: {
        **OK, "continues": ["claude", "codex"]} if stopped else RUNNING)
    page.start()
    assert stopped == [("local", "rev")]
    assert asked("Stop"), "asked first (team-stop)"
    words = started_words(page)
    assert words[:2] == ["team", "start"] and words[words.index("--") - 1] == "rev"
    assert "team start continues the team that is there" in page.team_verdict_text()


def test_cancel_starts_nothing_and_says_why(team, monkeypatch):
    page, node = team
    page.verdicts = RUNNING
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    before = len(page.started)
    assert page.start() is False and len(page.started) == before
    assert "Not started: a team named rev is there already." in page.notice.text()


def test_a_members_name_in_use_is_asked_about_too_not_an_error(team, monkeypatch):
    page, node = team
    page.verdicts = {"ok": False, "code": "taken", "error": "these names are in use already: rev-codex (seen: a "
                     "session of codex, in tmux)", "sessions": {"rev-codex": {"agent": "codex", "state": "stopped"}}}
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    assert [p.level for p in page.team_findings()] == ["warning"]
    offered = session_start.team_choices(page.verdicts, "rev-ab")
    assert [key for key, _ in offered] == ["other"], "nothing of a team to attach to or stop"
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    monkeypatch.setattr(page.sai, "team_check", lambda host, **v: page.verdicts if v["name"] == "rev" else OK)
    page.start()
    assert "is used by another start on this machine" in asked("Team rev")[0]
    assert started_words(page)[started_words(page).index("--") - 1] != "rev"


def test_other_members_offer_stop_and_start_as_another_name():
    verdict = {"ok": False, "code": "exists", "error": "team rev exists with claude, goose (lead claude), not ...",
               "team": {"members": ["claude", "goose"], "lead": "claude", "same": False, "verdict": "exists",
                        "states": {"claude": "running", "goose": "stopped"}, "seen": {}}}
    assert session_start.team_choices(verdict, "rev-1f") == [("stop", "Stop the team and start as rev-1f"),
                                                             ("other", "Start as rev-1f")]
    assert "with other members: claude, goose" in session_start.team_what("ct1", "rev", verdict)
    verdict["team"]["states"] = {"claude": "stopped", "goose": "gone"}
    assert [k for k, _ in session_start.team_choices(verdict, "x")] == ["other"], "nothing runs: nothing to stop"
    assert session_start.other_team_name("a" * 20, "1f") == "a" * 17 + "-1f"


def test_every_members_login_is_asked_for_and_team_start_does_not_ask_again(team, monkeypatch):
    page, node = team
    node.props["members"] = ["claude", "codex", "goose"]
    node.props["llms"] = {"claude": "lab"}  # (its own LLM server: no login of sandbox-ai's)
    page.check()
    assert page.prerequisite_login_agents() == ["codex"], "goose has no login of sandbox-ai's; claude has its server"
    node.props["llms"] = {}
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    assert page.prerequisite_login_agents() == ["claude", "codex"]
    asked_for = []
    monkeypatch.setattr(page.sai, "logged_in_here", lambda agents, session=None: asked_for.append(list(agents))
                        or {"claude"})
    monkeypatch.setattr(page.sai, "host_logged_in", lambda host, agents, session=None: {"claude"} & set(agents))
    page.logins = []
    monkeypatch.setattr(page, "login_terminal", lambda: (lambda argv, title, ended: page.logins.append(
        (argv, ended)) or True))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "without")
    page.start()
    assert asked_for[0] == ["claude", "codex"] and "There is no login for codex on this machine" in \
        asked("Log in to codex")[0]
    assert "--no-login-question" in started_words(page), "the app asked: team start's terminal does not again"
    # A team start there that does not say which logins it lacks (older): no option it would refuse.
    page.verdicts = {k: v for k, v in OK.items() if k != "logins"}
    page.board.first("network").props["--allow-pypi"] = False
    page.check()
    before = len(page.started)
    page.start()
    assert len(page.started) == before + 1 and "--no-login-question" not in started_words(page)


def test_log_in_now_runs_the_members_login_before_team_start(team, monkeypatch):
    page, node = team
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    there = set()
    monkeypatch.setattr(page.sai, "logged_in_here", lambda agents, session=None: there & set(agents))
    monkeypatch.setattr(page.sai, "host_logged_in", lambda host, agents, session=None: there & set(agents))
    page.logins = []
    monkeypatch.setattr(page, "login_terminal", lambda: (lambda argv, title, ended: page.logins.append(
        (argv, ended)) or True))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "login")
    before = len(page.started)
    page.start()
    assert len(page.started) == before, "the team waits for the logins"
    argv, ended = page.logins[0]
    assert terminal.login_of(argv) == "claude"
    there.add("claude")
    ended(0)
    argv, ended = page.logins[1]
    assert terminal.login_of(argv) == "codex", "one member's agent after the other (Codex: its device sign-in)"
    there.add("codex")
    ended(0)
    assert started_words(page)[:2] == ["team", "start"]


# --- end to end: the real ./sandbox-ai of another host ----------------------------------------------------------------

def team_on(fleet, host, name, members):
    """Team NAME with MEMBERS on HOST as its team start made it: its record, and each member a session that runs
    there (started through that host's real sandbox-ai, held where its pod is made; the lead with its terminal)."""
    side = fleet.of(host)
    record = side.home / ".local/state/sandbox-ai/teams" / name / "team.json"
    (record.parent / "board").mkdir(parents=True)
    record.write_text(json.dumps({"version": 1, "name": name, "members": list(members), "lead": members[0],
                                  "workspace": str(side.workspace), "options": ["--no-auth"], "vpn": None,
                                  "member_vpn": {}, "shared_openvpn": False, "roles": {}, "member_options": {},
                                  "created": "2026-11-19T00:00:00Z"}))
    for index, agent in enumerate(members):
        fleet.running(host, agent, f"{name}-{agent}", "--no-auth", ready=index == 0)


def team_board(fleet, monkeypatch, host, members, name="rev", options=("--no-auth",)):
    page = make_board(fleet, monkeypatch, members[0], host, options=options)
    page.board.add("team", name=name, lead=members[0], members=list(members))
    page.rebuild()
    page.check()
    return page


def test_e2e_a_team_that_runs_on_a_host_is_attached_to_its_lead(fleet, monkeypatch):
    team_on(fleet, "ct1", "rev", ["claude", "codex"])
    page = team_board(fleet, monkeypatch, "ct1", ["claude", "codex"])
    verdict = page.team_verdict[2]
    assert verdict["code"] == "exists" and verdict["team"]["verdict"] == "attach", verdict
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "attach")
    assert page.start(embedded=True) is False
    question, = asked("Team rev")
    assert "Team rev runs already on ct1." in question and "rev-codex: running (a session of codex" in question
    assert page.started[-1][0] == page.sai.attach_argv("ct1", "rev-claude")


def test_e2e_every_members_login_on_the_host_and_team_start_without_its_question(fleet, monkeypatch, real_logins):
    page = with_login_terminal(team_board(fleet, monkeypatch, "ct1", ["claude", "codex"], name="lg", options=()))
    verdict = page.team_verdict[2]
    assert verdict["ok"] and {e["agent"] for e in verdict["logins"]} == {"claude", "codex"}, verdict
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "without")
    page.start(embedded=True)
    question, = asked("Log in to claude, codex")
    assert "There is no login for claude, codex on ct1" in question
    words = terminal.remote_words(page.started[-1][0][-1])
    assert words[words.index("team"):][:2] == ["team", "start"] and "--no-login-question" in words


def test_a_paused_team_is_asked_about_and_continued(team, monkeypatch):
    """As a single start's paused session: Continue the team (team start resumes its members), another name, Cancel;
    a stopped one of the same members simply continues."""
    page, node = team
    page.verdicts = {**OK, "continues": ["claude", "codex"],
                     "team": {"members": ["claude", "codex"], "lead": "claude", "same": True, "verdict": "continue",
                              "states": {"claude": "paused", "codex": "stopped"}, "seen": {}}}
    page.board.first("network").props["--allow-pypi"] = True
    page.check()
    assert "team start continues the team that is there: rev-claude, rev-codex" in page.team_verdict_text()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    before = len(page.started)
    page.start()
    assert len(page.started) == before and "Team rev is paused on this machine" in asked("Team rev")[0]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "continue")
    page.start()
    words = started_words(page)
    assert words[:2] == ["team", "start"] and words[words.index("--") - 1] == "rev"
    assert [k for k, _ in session_start.team_choices(page.verdicts, "x")] == ["continue", "other"]
    page.verdicts["team"]["states"] = {"claude": "stopped", "codex": "stopped"}
    page.board.first("network").props["--allow-pypi"] = False
    page.check()
    widgets.SHOWN.clear()
    before = len(page.started)
    page.start()
    assert len(page.started) == before + 1 and not asked("Team rev"), "stopped: it continues without a question"
