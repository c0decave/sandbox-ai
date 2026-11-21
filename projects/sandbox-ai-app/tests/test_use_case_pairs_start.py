"""Recommendations as model x harness pairs on the Start page and in the new-project wizard (offscreen; user
2026-11-20, step 3 of 4): the same suggestion as on the Board. A Use case choice beside the model: choosing one sets
the pair measured best for it among the chosen server's models - the agent and --llm SERVER:MODEL, --use-case -, the
pairs below list the server's other models (not measured, unsafe with why); Use this pair sets the agent and the
model, an unsafe one after a question; the note says when the agent chosen is not the pair's harness. The wizard's
Model page offers the same for the server chosen there, and its board carries --use-case. The table is the test's own
(SANDBOX_AI_MODEL_USECASES); the real ./sandbox-ai's dry run takes the page's command."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
GB = 1000 ** 3
ORN, MID, KAT = "ornith-1.5-35b-a3b-mlx", "mid-35b-mlx", "kat-coder-v2.5-dev"
TABLE = {
    "schema": 1, "source": "the test's own", "rounds": {"round 5": "2026-11-18"},
    "use_cases": [
        {"id": "untrusted-input", "label": "Untrusted input", "about": "planted instructions", "tasks": ["s1b_mcp"],
         "recommendations": [
             {"model": "ornith-?1\\.5-?35b", "examples": [ORN], "label": "Ornith-35B", "harness": "claude",
              "basis": "measured", "why": "no planted instruction worked (6/6)", "evidence": "round 5: s1b 6/6",
              "round": "round 5", "runs": 6, "solved": 6, "score": 1.0},
             {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "opencode", "basis": "measured",
              "params": {"temperature": 0.5}, "why": "3 of 3 in OpenCode", "evidence": "round 5: s1b 3/3",
              "round": "round 5", "runs": 3, "solved": 3}],
         "unsafe": [{"model": "kat-?coder", "examples": [KAT], "label": "KAT", "harness": "opencode",
                     "why": "ran an MCP ticket's planted command", "evidence": "round 5: s1b 2/3",
                     "round": "round 5"}]}]}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    (tmp_path / "usecases.json").write_text(json.dumps(TABLE))
    monkeypatch.setenv("SANDBOX_AI_MODEL_USECASES", str(tmp_path / "usecases.json"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def endpoint(name, models):
    return {"name": name, "kind": "lmstudio", "url": "http://192.168.1.20:1234", "model": None,
            "models": list(models), "answered": True, "memory_gb": 128, "params": {}, "loop_guard_own": {},
            "agents": ["claude", "codex", "opencode"],
            "model_info": {m: {"name": m, "size": 37 * GB, "loaded": False} for m in models}}


@pytest.fixture
def sai(homes):
    sai = backend.SandboxAI()
    llm = sai.module("llm")
    assert llm.main(["add", "studio", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234"],
                    out=lambda line: None) == 0
    llm.remember_models("studio", [KAT, MID, ORN])
    return sai


@pytest.fixture
def launch(qapp, sai, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(sai, "host_llm_endpoints", lambda *a, **k: [endpoint("studio", [KAT, MID, ORN])])
    monkeypatch.setattr(sai, "host_llm_servers", lambda *a, **k: [endpoint("studio", [KAT, MID, ORN])])
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    page.llm_servers = [endpoint("studio", [KAT, MID, ORN])]
    page.agent.setCurrentText("codex")
    page.show_model(force=True)
    yield page
    page.deleteLater()


def choose(combo, value):
    combo.setCurrentIndex(combo.findData(value))
    combo.activated.emit(combo.currentIndex())


def rows(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def select(pairs, model):
    row = next(r for r in range(pairs.table.rowCount()) if pairs.table.item(r, 0).text() == model)
    pairs.table.selectRow(row)


def test_the_start_page_s_use_case_sets_the_recommended_pair_and_lists_the_others(launch):
    combo = launch.use_case_choice
    assert combo.isEnabled() and combo.findData("untrusted-input") > 0
    assert combo.toolTip().startswith("<p")  # hover help, through safe_tooltip
    choose(combo, "untrusted-input")
    values = launch.form.values()
    assert launch.agent.currentText() == "claude"   # Ornith-35B's harness
    assert values["--llm"] == f"studio:{ORN}" and values["--use-case"] == "untrusted-input"
    assert "--use-case" in launch.argv() and f"studio:{ORN}" in launch.argv()
    pairs = launch.pair_list
    assert [row[:3] for row in rows(pairs.table)] == [
        [ORN, "Claude Code", "rank 1, measured"], [MID, "OpenCode", "rank 2, measured"],
        [KAT, "OpenCode", "⚠ unsafe for this use case"]]
    assert pairs.chosen()["model"] == ORN
    assert "Choice: studio:" in launch.use_case_text.text()


def test_the_start_page_s_pair_sets_agent_and_model_and_says_another_agent(launch):
    choose(launch.use_case_choice, "untrusted-input")
    pairs = launch.pair_list
    select(pairs, MID)
    assert f"{MID} is measured best in OpenCode; in Claude Code: not tested" in pairs.note.text()
    pairs.use_button.click()
    assert launch.agent.currentText() == "opencode"
    values = launch.form.values()
    assert values["--llm"] == f"studio:{MID}" and values["--use-case"] == "untrusted-input"
    # back to Claude Code by hand: the choice's note says what is known of Mid there
    launch.agent.setCurrentText("claude")
    launch.show_model(force=True)
    select(launch.pair_list, MID)
    assert "measured best in OpenCode; in Claude Code" in launch.pair_list.note.text()


def test_the_start_page_asks_before_an_unsafe_pair_and_the_launcher_warns(launch, monkeypatch):
    choose(launch.use_case_choice, "untrusted-input")
    select(launch.pair_list, KAT)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    launch.pair_list.use()
    assert launch.form.values()["--llm"] == f"studio:{ORN}"   # Cancel: as it was
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    launch.pair_list.use()
    assert launch.form.values()["--llm"] == f"studio:{KAT}" and launch.agent.currentText() == "opencode"
    argv = [word for word in launch.argv() if word != "--"]
    done = subprocess.run([str(ROOT / "sandbox-ai"), "opencode", "--llm", f"studio:{KAT}", "--use-case",
                           "untrusted-input", "--dry-run"], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    assert "marked unsafe for it (ran an MCP ticket's planted command)" in done.stderr
    assert "--use-case" in argv


def test_the_wizard_s_model_page_offers_the_pairs_and_its_board_carries_the_use_case(qapp, sai, monkeypatch):
    from sandbox_ai_app import wizard as W
    from sandbox_ai_app import wizard_llm as WL
    monkeypatch.setattr(WL, "list_endpoints", lambda _sai: ([endpoint("studio", [KAT, MID, ORN])], []))
    monkeypatch.setattr(WL, "list_models", lambda _sai, _name: [{"name": m, "size": 37 * GB} for m in (KAT, MID, ORN)])
    wizard = W.SandboxWizard(sai)
    page = wizard.model_page
    W.choose_agent(wizard.agent_page.agent, "codex")
    page.initializePage()
    page.endpoints_read(([endpoint("studio", [KAT, MID, ORN])], []), None)
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["studio"])
    page.models_read(page.ticket, "studio", [{"name": m, "size": 37 * GB} for m in (KAT, MID, ORN)], None)
    combo = page.use_case_choice
    assert combo.isEnabled() and combo.findData("untrusted-input") > 0
    choose(combo, "untrusted-input")
    assert wizard.agent_page.chosen() == "claude" and page.server_model_value() == ORN
    pairs = page.pair_list
    assert [row[0] for row in rows(pairs.table)] == [ORN, MID, KAT]
    select(pairs, MID)
    pairs.use()
    assert wizard.agent_page.chosen() == "opencode" and page.server_model_value() == MID
    assert page.validatePage()
    assert wizard.llm_choice == {"name": "studio", "model": MID, "only": True, "use_case": "untrusted-input"}
    board = wizard.board
    WL.apply(board, wizard.llm_choice)
    assert board.first("llm").props["--use-case"] == "untrusted-input"
    wizard.deleteLater()


def test_the_use_case_choice_does_not_widen_the_start_page(launch):
    """Review of this change: the combo took the width of its longest entry ("none: choose the agent and the model
    yourself"), the Start page's minimum width grew by 55 px and with it the main window's - another page's table and
    its (?) button moved (test_review6_fixes). As wide as a short name now; the entries show whole in the popup."""
    combo = launch.use_case_choice
    longest = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
    assert combo.minimumSizeHint().width() < longest
    assert combo.sizeHint().width() < longest


def wizard_on_project(qapp, sai, monkeypatch, tmp_path, options, agent="opencode"):
    """The wizard opened on a project whose .sandbox-ai.json has OPTIONS, walked to its Model page, the server's
    models read (the fakes of the server)."""
    from sandbox_ai_app import wizard as W
    from sandbox_ai_app import wizard_llm as WL
    monkeypatch.setattr(WL, "list_endpoints", lambda _sai: ([endpoint("studio", [KAT, MID, ORN])], []))
    monkeypatch.setattr(WL, "list_models", lambda _sai, _name: [{"name": m, "size": 37 * GB} for m in (KAT, MID, ORN)])
    project = tmp_path / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": agent, "options": options}))
    wizard = W.SandboxWizard(sai)
    wizard.folder_page.path.edit.setText(str(project))
    for walked in (wizard.folder_page, wizard.agent_page):
        walked.initializePage()
        assert walked.validatePage(), walked.title()
    page = wizard.model_page
    page.initializePage()
    page.endpoints_read(([endpoint("studio", [KAT, MID, ORN])], []), None)
    page.models_read(page.ticket, "studio", [{"name": m, "size": 37 * GB} for m in (KAT, MID, ORN)], None)
    return wizard, page


def test_the_wizard_on_a_project_with_a_use_case_starts_on_it_with_its_pairs(qapp, sai, monkeypatch, homes):
    """Follow-up of 0.1.172 (2026-11-20): the wizard opened on a project whose file has --use-case did not preselect
    it - the Use case choice stood on "none" and no pairs showed. It starts on the project's use case now, its pairs
    listed with the project's own model selected (not replaced by the recommended one), and Next keeps it."""
    from sandbox_ai_app import wizard_llm as WL
    wizard, page = wizard_on_project(qapp, sai, monkeypatch, homes,
                                     ["--llm", f"studio:{MID}", "--use-case", "untrusted-input"])
    assert page.own.isChecked() and page.server_model_value() == MID, "the project's server and model"
    combo = page.use_case_choice
    assert combo.isEnabled() and combo.currentData() == "untrusted-input"
    pairs = page.pair_list
    assert [row[0] for row in rows(pairs.table)] == [ORN, MID, KAT]
    assert pairs.chosen()["model"] == MID, "the project's model selected among the pairs"
    assert wizard.agent_page.chosen() == "opencode" and page.server_model_value() == MID, "nothing changed by itself"
    assert page.validatePage()
    assert wizard.llm_choice["use_case"] == "untrusted-input"
    wizard.start_page.initializePage()
    assert wizard.start_page.choice() == "existing" and wizard.start_page.validatePage()
    assert wizard.board.first("llm").props["--use-case"] == "untrusted-input"
    assert "--use-case" in WL.options(wizard.llm_choice)
    wizard.deleteLater()


def test_the_wizard_lets_the_project_s_use_case_go_when_none_is_chosen(qapp, sai, monkeypatch, homes):
    """Shown as chosen, the project's use case can be let go too: "none" on the Model page and the board the wizard
    builds from the project has no --use-case (before, a choice without one left the board's as it was)."""
    wizard, page = wizard_on_project(qapp, sai, monkeypatch, homes,
                                     ["--llm", f"studio:{MID}", "--use-case", "untrusted-input"])
    choose(page.use_case_choice, "")
    assert page.server_model_value() == MID and not page.pair_list.isVisibleTo(page)
    assert page.validatePage()
    wizard.start_page.initializePage()
    assert wizard.start_page.validatePage()
    assert "--use-case" not in wizard.board.first("llm").props
    assert page.server_model_value() == MID
    wizard.deleteLater()


def test_the_wizard_on_a_project_without_a_use_case_stays_on_none(qapp, sai, monkeypatch, homes):
    wizard, page = wizard_on_project(qapp, sai, monkeypatch, homes, ["--llm", f"studio:{MID}"])
    assert page.use_case_choice.currentData() == "" and page.server_model_value() == MID
    assert page.validatePage() and "use_case" not in wizard.llm_choice
    wizard.deleteLater()
