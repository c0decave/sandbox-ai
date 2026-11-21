"""MCP tools against the model's class in the app (offscreen). User 2026-11-19: Gemma-4-12B/26B did worse with all four
memory aids at once (round 5 of the local-model research): when a start's --llm model would see more MCP tools than its
class copes with, the Board says so as a finding with the servers to leave out, and the Start page below the model -
a warning, never a refusal. The count is the launcher's own (sandbox-ai's mcp_budget.py): the Board takes it from the
real ./sandbox-ai's dry run ("mcp_tools"), the Start page from this machine's sandbox-ai."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, board as B, i18n, model_profile as MP, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
GEMMA = "gemma-4-12b-it"
ORN35 = "ornith-1.5-35b-a3b-mlx"
IMAGE = "ghcr.io/c0decave/sandbox-ai-mcp-x@sha256:" + "a" * 64


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "config/sandbox-ai").mkdir(parents=True)
    (tmp_path / "config/sandbox-ai/mcp.json").write_text(json.dumps({
        "memory": {"image": IMAGE, "network": "none", "source": "mcp:memory"},
        "tasks": {"image": IMAGE, "network": "none", "source": "mcp:tasks"},
        "seqthink": {"image": IMAGE, "network": "none", "source": "mcp:sequential-thinking"}}))
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    assert sai.module("llm").main(["add", "studio", "--kind", "lmstudio", "--url", "http://127.0.0.1:9", "--model",
                                   GEMMA], out=lambda line: None) == 0
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    view.llm_servers = [{"name": "studio", "kind": "lmstudio", "model": GEMMA, "models": [GEMMA, ORN35],
                         "params": {}, "loop_guard_own": {}}]
    yield view
    view.deleteLater()


def board_with(page, model, servers):
    B.switch_agent(page.board.first("agent").props, "claude")
    page.choose_llm_server({"name": "studio", "model": model, "only": True})
    for name in servers:
        page.board.add("mcp", name=name)


def checked_by_the_launcher(page):
    """The real ./sandbox-ai's dry run of the board (as BoardPage.check runs it, here in the foreground), its answer
    given to the page as the check's."""
    argv = B.check_argv(page.sai, page.board, local=True)
    done = subprocess.run([str(ROOT / "sandbox-ai"), "claude", *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr  # a warning, never a refusal
    plan = json.loads(done.stdout)
    page.check_token += 1
    page.checking_state = page.board.state()
    page.checked(page.check_token, (plan, done.stderr), None, "local")
    return plan, done.stderr


def findings(page):
    return [page.findings.item(row).text() for row in range(page.findings.count())]


def test_the_board_names_the_servers_to_leave_out(page):
    board_with(page, GEMMA, ["memory", "tasks", "seqthink"])
    plan, stderr = checked_by_the_launcher(page)
    assert plan["mcp_tools"]["tools"] == 20 and "would see 20 MCP tools" in stderr
    found = [text for text in findings(page) if "MCP tools" in text]
    assert found == ["! gemma-4-12b-it would see 20 MCP tools, more than a small model copes with (about 10). "
                     "Servers to leave out: tasks (10 tools) - then 10. A warning only: the start "
                     "goes on, you decide."], findings(page)
    llm = page.board.first("llm")
    row = next(r for r in range(page.findings.count()) if "MCP tools" in page.findings.item(r).text())
    assert page.findings.item(row).data(QtCore.Qt.ItemDataRole.UserRole) == llm.id  # (on the LLM part)
    # Fewer servers: checked again, the finding goes.
    page.board.remove(next(n.id for n in page.board.of_kind("mcp") if n.props.get("name") == "tasks"))
    page.board.remove(next(n.id for n in page.board.of_kind("mcp") if n.props.get("name") == "memory"))
    page.refresh()
    checked_by_the_launcher(page)
    assert not [text for text in findings(page) if "MCP tools" in text]


def test_a_mid_sized_model_keeps_them(page):
    board_with(page, ORN35, ["memory", "tasks", "seqthink"])
    plan, _ = checked_by_the_launcher(page)
    assert plan["mcp_tools"]["class"] == "mid" and plan["mcp_tools"]["over"] is False
    assert not [text for text in findings(page) if "MCP tools" in text]


def test_in_german(page):
    i18n.set_language("de")
    try:
        board_with(page, GEMMA, ["memory", "tasks"])
        checked_by_the_launcher(page)
        found = [text for text in findings(page) if "MCP-Werkzeuge" in text]
        assert found == ["! gemma-4-12b-it sähe 19 MCP-Werkzeuge, mehr als ein kleines Modell verkraftet (etwa 10). "
                         "Wegzulassende Server: tasks (10 Werkzeuge) - dann 9. Nur eine Warnung: der Start geht "
                         "weiter, Sie entscheiden."], findings(page)
    finally:
        i18n.set_language("en")


def test_the_texts_from_a_report():
    report = {"model": "m<b>", "class": "small", "budget": 10, "tools": 19, "about": True, "unknown": ["own"],
              "over": True, "drop": ["tasks"], "left": 9, "evidence": "round 5: ...",
              "servers": [{"server": "tasks", "tools": 10}, {"server": "memory", "tools": 9},
                          {"server": "own", "tools": None}]}
    lines = MP.tool_budget_lines(report)
    assert lines[0] == "m<b> would see about 19 MCP tools, more than a small model copes with (about 10)."
    assert lines[1] == "Servers to leave out: tasks (10 tools) - then 9."
    assert lines[2] == "The tools of own are not known yet (counted after its first start in this project)."
    assert MP.tool_budget_lines({**report, "over": False}) == [] and MP.tool_budget_lines(None) == []
    assert "round 5" in MP.tool_budget_details(report)


def test_the_start_page_says_it_below_the_model(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui

    def synchronous(function, *args, done, **_kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(ui, "background", synchronous)
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_llm_servers", lambda *a, **k: [])
    launch = ui.LaunchPage(sai, lambda *a, **k: None)
    launch.agent.setCurrentText("claude")

    def choose(model, servers):
        field = launch.form.widgets["--llm"]
        value = f"studio:{model}"
        field.setCurrentText(value) if isinstance(field, QtWidgets.QComboBox) else field.setText(value)
        launch.form.widgets["--mcp"].set_value(servers)
        launch.form.changed.emit()
        return launch.tool_budget_note

    note = choose(GEMMA, ["memory", "tasks"])
    assert not note.isHidden() and note.property("role") == "notice-warn"
    assert note.text().splitlines()[:2] == [
        "gemma-4-12b-it would see 19 MCP tools, more than a small model copes with (about 10).",
        "Servers to leave out: tasks (10 tools) - then 9."]
    assert "Gemma 4 12B and 26B-A4B" in note.toolTip() and "&lt;" not in note.toolTip()
    assert "--mcp" in launch.argv(), "said, never refused: the servers stay in the start"
    assert choose(ORN35, ["memory", "tasks"]).isHidden()
    assert choose(GEMMA, ["seqthink"]).isHidden()
    assert choose("gpt-5", ["memory", "tasks"]).isHidden(), "a model of no class: nothing said"
    launch.deleteLater()


def test_the_texts_are_in_both_languages():
    for key in (*MP.TOOL_SIZES.values(), *MP.TOOL_TEXTS):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"], key
