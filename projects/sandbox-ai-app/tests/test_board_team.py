"""The Board's Team part (0.1.98): a team (sandbox-ai team) designed, started and watched from the Board, as single
sessions and bridges are. The part holds the team's name, lead and members (sandbox-ai's team agents), each member's
role and its own model, LLM server and mode; every member gets the board's settings on its project folder. With the
part the board's Start runs `sandbox-ai team start` on the board's host (the Teams page's and the command line's way),
after team start's own check of the board there (`team start --dry-run --json`: the app does not copy its rules); the
part shows the team's board (members, tasks and answers, read-only) and leads to the Teams page. Never written into
the project file; kept with the board's draft.

Unit tests, the page (offscreen), then the real ./sandbox-ai (tests/live_fakes.py: Podman stood in for) runs what the
Board built."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPushButton, QTableWidget  # noqa: E402

from sandbox_ai_app import backend, board as B, board_view, team_page, widgets  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def team_board(tmp_path, **props):
    board = B.Board()
    for kind in ("agent", "proxy", "network"):
        board.add(kind)
    board.add("workspace", path=str(tmp_path))
    node = board.add("team", **{"name": "rev", "lead": "claude", "members": ["claude", "codex"], **props})
    return board, node


def test_the_team_part_is_no_launcher_option_and_never_reaches_the_project_file(tmp_path):
    sai = backend.SandboxAI()
    board, node = team_board(tmp_path, roles={"codex": "reviews"})
    assert (board.first("agent").id, node.id) in board.edges, "added beside the agent, it is connected"
    assert node.title() == "Team: rev" and node.key() == "team"
    plain, _ = team_board(tmp_path)
    plain.remove(plain.first("team").id)
    assert B.to_argv(sai, board) == B.to_argv(sai, plain)
    document, _ = B.project_document(sai, board)
    assert "reviews" not in json.dumps(document) and "team" not in json.dumps(document)
    again = B.Board.checked(board.state(), sai)  # kept with the board's draft, checked as any draft
    assert again.first("team").props["roles"] == {"codex": "reviews"}
    board.layout()
    assert node.x == board.first("proxy").x, "in the column of what the agent talks to"
    board.disconnect(board.first("agent").id, node.id)
    assert B.team_node(board) is None and B.team_values(sai, board) is None, "not connected: no team"


def test_its_values_are_what_team_start_takes(tmp_path):
    sai = backend.SandboxAI()
    board, node = team_board(tmp_path, members=["codex", "goose", "claude"], lead="codex",
                             roles={"goose": " writes the docs ", "qwen": "not a member"},
                             models={"codex": "gpt-5"}, llms={"goose": "lab:qwen3"}, modes={"goose": "high", "codex": ""})
    board.first("agent").props.update(agent="claude", model="opus")
    board.first("network").props["--allow-pypi"] = True
    board.document_extra["name"] = "proj"
    values = B.team_values(sai, board)
    assert values["name"] == "rev" and values["agents"] == ["codex", "goose", "claude"], "the lead first"
    assert values["workspace"] == str(tmp_path) and values["roles"] == {"goose": "writes the docs"}
    assert values["member_options"] == {"codex": {"--model": "gpt-5"}, "goose": {"--llm": "lab:qwen3", "--mode": "high"},
                                        "claude": {"--model": "opus"}}, "the board's agent takes the Agent part's model"
    options = values["options"]
    assert "--allow-pypi" in options and options[-1] == "--no-config"
    assert not {"--workspace", "--model", "--project-name"} & set(options), "what team start chooses, or per member"
    argv = sai.team_start_args("local", **values)
    assert argv[:2] == ["team", "start"] and argv[argv.index("--") - 1] == "rev"
    assert ["--role", "goose=writes the docs"] == argv[argv.index("--role"):argv.index("--role") + 2]
    assert "codex=gpt-5" in argv and "goose=lab:qwen3" in argv and "goose=high" in argv
    assert B.command_line(sai, board).startswith("sandbox-ai team start --agents codex,goose,claude --workspace ")


@pytest.mark.parametrize("change, level, text", [
    (lambda b, n: n.props.update(name="Bad Name"), "error", "give it a name"),
    (lambda b, n: n.props.update(members=["claude"]), "error", "at least one more member"),
    (lambda b, n: b.add("workspace", path="/srv/other"), "error", "share one project folder"),
    (lambda b, n: b.add("bridge", peer="docs", message=True), "error", "makes no bridges"),
    (lambda b, n: b.first("agent").props.update(arguments="--verbose"), "warning", "not passed on"),
])
def test_its_problems(tmp_path, change, level, text):
    sai = backend.SandboxAI()
    board, node = team_board(tmp_path)
    assert [p for p in B.problems(sai, board) if p.node == node.id] == []
    change(board, node)
    found = [p for p in B.problems(sai, board) if p.node == node.id]
    assert [p.level for p in found] == [level] and text in found[0].text


def test_its_badge_says_the_members_share_the_folder(tmp_path):
    board, node = team_board(tmp_path)
    _score, badges = B.badges(board)
    assert any(b.node == node.id and "2 sandboxes of these settings (claude, codex) on one project folder" in b.text
               for b in badges)


def test_the_team_agents_are_sandbox_ais(tmp_path):
    sai = backend.SandboxAI()
    assert backend.TEAM_AGENTS == tuple(sai.module("team").AGENTS) == team_page.AGENTS
    board, node = team_board(tmp_path, members=["claude", "gemini", "pi", "codex", "claude"])
    # (Pi is a team agent since 0.1.98; no agent of sandbox-ai's is left out)
    assert B.team_members(node.props) == ["claude", "pi", "codex"], "a team agent once each, the lead first"


# --- the page (offscreen) --------------------------------------------------------------------------------------------

def synchronous(function, *args, done, **_kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def teamed(page, tmp_path, monkeypatch):
    monkeypatch.setattr(board_view, "background", synchronous)
    checks = []
    page.verdicts = {"ok": True, "plan": [{"session": "rev-claude"}, {"session": "rev-codex"}]}

    def team_check(host, **values):
        checks.append((host, values))
        return page.verdicts
    monkeypatch.setattr(page.sai, "team_check", team_check)
    monkeypatch.setattr(page.sai, "team_show", lambda host, name: (_ for _ in ()).throw(ValueError(f"no team {name}")))
    page.checks = checks
    page.board.add("workspace", path=str(tmp_path))
    node = page.board.add("team", name="rev", lead="claude", members=["claude", "codex"])
    page.rebuild()
    page.refresh()
    return page, node


def test_a_new_team_part_is_led_by_the_boards_agent(page):
    page.board.first("agent").props["agent"] = "goose"
    page.add_node("team")
    node = page.board.first("team")
    assert node.props["lead"] == "goose" and node.props["members"] == ["goose", "codex"]


def test_the_inspector_edits_the_team_part(teamed):
    page, node = teamed
    node_id = node.id

    def field(cls, test):
        page.inspect(page.board.node(node_id))
        return [w for w in page.findChildren(cls) if test(w)][-1]

    def props():
        return page.board.node(node_id).props
    name = field(QLineEdit, lambda w: w.accessibleName() == "Team name")
    assert name.text() == "rev"
    name.setText("review")
    name.editingFinished.emit()
    assert props()["name"] == "review"
    lead = field(QComboBox, lambda w: w.accessibleName() == "Lead")
    lead.setCurrentIndex(lead.findData("codex"))
    assert props()["lead"] == "codex" and props()["members"][0] == "codex"
    assert not field(QCheckBox, lambda w: w.text() == "codex").isEnabled(), "the lead is always a member"
    field(QCheckBox, lambda w: w.text() == "goose").setChecked(True)
    assert "goose" in props()["members"]
    role = field(QLineEdit, lambda w: w.accessibleName() == "Role of goose")
    role.setText("writes the docs")
    role.editingFinished.emit()
    assert props()["roles"] == {"goose": "writes the docs"}
    mode = field(QComboBox, lambda w: w.accessibleName() == "Mode of goose")
    mode.setCurrentIndex(mode.findData("high"))
    assert props()["modes"] == {"goose": "high"}
    page.undo.undo()
    assert props().get("modes") in ({}, None), "one step of Undo each"
    model = field(QLineEdit, lambda w: w.accessibleName() == "Model of claude")
    model.setText("opus")
    model.editingFinished.emit()
    assert props()["models"] == {"claude": "opus"}
    assert "--member-model claude=opus" in page.preview.toPlainText() and "--role 'goose=writes the docs'" in \
        page.preview.toPlainText(), "the command line shows team start"


def test_team_starts_verdict_is_asked_on_the_boards_host_and_its_refusal_marks_the_part(teamed):
    page, node = teamed
    page.check()
    assert page.checks and page.checks[-1][0] == "local" and page.checks[-1][1]["agents"] == ["claude", "codex"]
    assert not [p for p in page.team_findings()]
    page.verdicts = {"ok": False, "error": "VPN profile wg would run in 2 members at once (claude, codex)",
                     "code": None}
    page.board.first("network").props["--allow-pypi"] = True  # (another board: asked again)
    page.check()
    found = page.team_findings()
    assert [p.level for p in found] == ["error"] and "would run in 2 members at once" in found[0].text
    assert found[0].node == node.id
    assert any("would run in 2 members" in page.findings.item(i).text() for i in range(page.findings.count()))
    page.verdicts = {"ok": False, "error": "team rev exists; see: sandbox-ai team show rev", "code": "exists"}
    page.board.first("network").props["--allow-pypi"] = False
    page.check()
    assert [p.level for p in page.team_findings()] == ["warning"], "a team that runs: its board is shown, no red"


def test_start_runs_team_start_on_the_boards_host_once_team_start_accepts_it(teamed):
    page, node = teamed
    node.props["roles"] = {"codex": "reviews"}
    page.check()
    assert page.start() is True
    argv, title, _embedded, _cwd = page.started[-1]
    assert title == "sandbox-ai team start rev"
    words = argv[argv.index("team"):]
    assert words[:4] == ["team", "start", "--agents", "claude,codex"] and "codex=reviews" in words
    assert words[words.index("--") - 1] == "rev" and "--no-config" in words[words.index("--"):]
    # The launcher's refusal of the board's own agent is not what holds a team back: each member's is, in team
    # start's verdict (the board's agent need not even be a member).
    page.launcher_refusal = ("Agent login missing for claude", page.board.state(), page.host())
    assert page.start() is True
    page.verdicts = {"ok": False, "error": "rev-codex: unknown LLM server lab", "code": None}
    node.props["llms"] = {"codex": "lab"}
    before = len(page.started)
    assert page.start() is False and len(page.started) == before
    assert any("unknown LLM server lab" in str(shown) for shown in widgets.SHOWN)


def test_the_part_shows_the_teams_board_read_only_and_leads_to_the_teams_page(teamed, monkeypatch):
    page, node = teamed
    shown = {"name": "rev", "members": ["claude", "codex"], "lead": "claude", "workspace": "/w",
             "states": {"claude": "running", "codex": "paused"},
             "board": {"tasks": [{"id": 1, "from": "claude", "to": "codex", "state": "done", "task": "write\x1b[31m tests",
                                  "answer": "done: 12 tests"}]}}
    monkeypatch.setattr(page.sai, "team_show", lambda host, name: shown)
    wanted = []
    page.team_wanted.connect(lambda name, host: wanted.append((name, host)))
    page.inspect(node)
    tasks = next(w for w in page.findChildren(QTableWidget) if w.accessibleName() == "The team's board")
    assert tasks.rowCount() == 1 and tasks.item(0, 5).text() == "done: 12 tests"
    assert "\x1b" not in tasks.item(0, 4).text(), "what the members wrote is shown as text"
    members = next(w for w in page.findChildren(QTableWidget) if w.accessibleName() == "Members")
    assert [members.item(r, 0).text() for r in range(members.rowCount())] == ["rev-claude", "rev-codex"]
    assert tasks.editTriggers() == QTableWidget.EditTrigger.NoEditTriggers
    button = next(w for w in page.findChildren(QPushButton) if w.text() == "Open on the Teams page")
    button.click()
    assert wanted == [("rev", "local")]


def test_on_another_host_the_team_is_checked_started_and_shown_there_as_host_name(teamed, monkeypatch):
    """Remote parity, as bridges have it: the board's host is where team start checks, starts and shows the team;
    its sessions are named HOST:NAME here."""
    page, node = teamed
    monkeypatch.setattr(page, "host", lambda: "lab1")
    monkeypatch.setattr(page.sai, "host_terminal", lambda host, *args: ["ssh", "-tt", host, "sandbox-ai", *args])
    monkeypatch.setattr(page.sai, "host_dry_run", lambda host, agent, argv: ({"agent": agent}, ""))
    monkeypatch.setattr(page.sai, "host_launch_options", lambda host, agent: {o.long for o in page.sai.options()})
    shown = {"name": "rev", "members": ["claude", "codex"], "lead": "claude", "states": {"claude": "running"},
             "board": {"tasks": []}}
    asked = []
    monkeypatch.setattr(page.sai, "team_show", lambda host, name: asked.append((host, name)) or shown)
    page.check()
    assert page.checks[-1][0] == "lab1"
    assert page.start() is True
    argv, title, _embedded, _cwd = page.started[-1]
    assert argv[:3] == ["ssh", "-tt", "lab1"] and title == "sandbox-ai team start rev (lab1)"
    page.inspect(page.board.node(node.id))
    assert asked[-1] == ("lab1", "rev")
    members = [w for w in page.findChildren(QTableWidget) if w.accessibleName() == "Members"][-1]
    assert [members.item(r, 0).text() for r in range(members.rowCount())] == ["lab1:rev-claude", "lab1:rev-codex"]
    wanted = []
    page.team_wanted.connect(lambda name, host: wanted.append((name, host)))
    [w for w in page.findChildren(QPushButton) if w.text() == "Open on the Teams page"][-1].click()
    assert wanted == [("rev", "lab1")]


def test_the_teams_page_selects_the_team_the_board_asked_for(qapp, monkeypatch):
    monkeypatch.setattr(team_page, "background", synchronous)
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "teams", lambda host: [{"name": "a", "members": ["claude", "codex"], "workspace": "/w"},
                                                    {"name": "rev", "members": ["claude", "codex"], "workspace": "/w"}])
    monkeypatch.setattr(sai, "team_show", lambda host, name: {"name": name, "members": ["claude", "codex"],
                                                              "lead": "claude", "states": {}, "board": {"tasks": []}})
    view = team_page.TeamPage(sai, lambda *a, **k: None)
    view.show_named("local", "rev")
    assert view.chosen() == "rev"
    view.deleteLater()


# --- the real ./sandbox-ai --------------------------------------------------------------------------------------

def test_e2e_the_boards_team_is_checked_and_started_by_the_real_sandbox_ai(teamed, tmp_path, monkeypatch):
    """team start's verdict comes from the real ./sandbox-ai (fake Podman); the argv the Board starts makes the team
    with its roles and member options, and the part then shows its board."""
    page, node = teamed
    root = Path(os.environ["SANDBOX_AI_HOME"])
    sys.path.append(str(root / "tests"))  # (after the app's own tests: conftest.app_tests_first)
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.env["SANDBOX_AI_SECRETS"] = "file"
    host.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest")

    def host_run(name, *args, timeout=60, **_kw):
        done = subprocess.run(host.command(*args), env=host.env, cwd=host.workspace, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    monkeypatch.setattr(page.sai, "host_run", host_run)
    monkeypatch.setattr(page.sai, "team_check", backend.SandboxAI.team_check.__get__(page.sai))
    monkeypatch.setattr(page.sai, "team_show", backend.SandboxAI.team_show.__get__(page.sai))
    page.board.first("workspace").props["path"] = str(host.workspace)
    (page.board.first("home") or page.board.add("home")).props["--no-auth"] = True
    node.props.update(name="e2bt", roles={"codex": "reviews every change"}, modes={"codex": "high"})
    page.check()
    verdict = page.team_verdict
    assert verdict is not None and verdict[3] is None and verdict[2]["ok"], verdict
    assert [m["session"] for m in verdict[2]["plan"]] == ["e2bt-claude", "e2bt-codex"]
    page.board.add("mcp", name="nosuch")  # the launcher refuses an unknown MCP server: said on the part
    page.check()
    assert any("e2bt-claude" in p.text and "nosuch" in p.text for p in page.team_findings()), page.team_verdict
    page.board.remove(page.board.of_kind("mcp")[0].id)
    page.check()
    assert page.start() is True
    argv = page.started[-1][0]
    done = subprocess.run(host.command(*argv[argv.index("team"):]), env=host.env, cwd=host.workspace,
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    shown = json.loads(host.run("team", "show", "--json", "e2bt").stdout)
    assert shown["roles"] == {"codex": "reviews every change"}, done.stdout + done.stderr
    assert shown["member_options"] == {"codex": {"--mode": "high"}} and "--no-config" in shown["options"]
    page.inspect(page.board.node(node.id))
    members = next(w for w in page.findChildren(QTableWidget) if w.accessibleName() == "Members")
    assert [members.item(r, 0).text() for r in range(members.rowCount())] == ["e2bt-claude", "e2bt-codex"]
    page.check()
    assert any(p.level == "warning" and "exists" in p.text for p in page.team_findings()), "started: it exists"


def test_the_guide_offers_the_team_agents_as_lead():
    from sandbox_ai_app import guide
    lead = next(q for q in guide.NODE_QUESTIONS["team"] if q.key == "lead")
    assert tuple(v for v, _ in lead.choices) == backend.TEAM_AGENTS


def test_the_teams_dialog_takes_each_members_role_and_own_options(qapp):
    """The Teams page's Start a team… has them too, under "Per member (advanced)", for the members only."""
    sai = backend.SandboxAI()
    dialog = team_page.StartDialog(None, "local", False, "/srv/w", [], modes=("lax", "low", "medium", "high",
                                                                             "paranoid"))
    dialog.name.setText("t")
    dialog.members["codex"].setChecked(True)
    assert dialog.member_settings() == ({}, {}), "nothing while Per member (advanced) is not ticked"
    dialog.per_member.setChecked(True)
    row, role, model, llm, mode = dialog.member_own["codex"]
    assert row.isVisibleTo(dialog) and not dialog.member_own["qwen"][0].isVisibleTo(dialog)
    role.setText(" reviews every change ")
    model.setText("gpt-5")
    dialog.member_own["claude"][4].setCurrentIndex(dialog.member_own["claude"][4].findData("high"))
    dialog.member_own["qwen"][1].setText("not a member")
    roles, own = dialog.member_settings()
    assert roles == {"codex": "reviews every change"}
    assert own == {"claude": {"--mode": "high"}, "codex": {"--model": "gpt-5"}}
    argv = sai.team_start_argv("local", "t", dialog.agents(), "/srv/w", [], roles=roles, member_options=own)
    words = argv[argv.index("team"):]
    assert "codex=reviews every change" in words and "claude=high" in words and "codex=gpt-5" in words
    dialog.deleteLater()
