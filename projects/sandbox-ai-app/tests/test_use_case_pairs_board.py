"""Recommendations as model x harness pairs on the Board (offscreen; user 2026-11-20, step 3 of 4): choosing a use case
lists the board's server's models as pairs ranked by the use case's table, each in the agent it was measured best in -
"not measured" said, a pair the table marks unsafe (KAT with untrusted input) shown with the reason, never hidden;
Use this pair (the button, a double click, the row's right-click menu) sets the agent and the model as one undo step,
an unsafe one only after a question; when the board's agent is not the pair's harness it says so ("... is measured
best in Claude Code; in OpenCode: ..."). The real ./sandbox-ai's dry run takes the board's pair. The table is the
test's own (SANDBOX_AI_MODEL_USECASES)."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, i18n, use_case as UC, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
GB = 1000 ** 3
ORN, MID, KAT, GEM = "ornith-1.5-35b-a3b-mlx", "mid-35b-mlx", "kat-coder-v2.5-dev", "google/gemma-4-31b-qat"
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
            "model_info": {m: {"name": m, "size": 37 * GB, "loaded": False} for m in models}}


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    llm = sai.module("llm")
    for name in ("studio", "other"):
        assert llm.main(["add", name, "--kind", "lmstudio", "--url", "http://192.168.1.20:1234"],
                        out=lambda line: None) == 0
    llm.remember_models("studio", [KAT, GEM, MID, ORN])
    llm.remember_models("other", [ORN])
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    view.llm_servers = [endpoint("studio", [KAT, GEM, MID, ORN]), endpoint("other", [ORN])]
    yield view
    view.deleteLater()


def choose(page, name):
    combo = page.use_case_combo
    combo.setCurrentIndex(combo.findData(name))
    combo.activated.emit(combo.currentIndex())


def rows(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def select(pairs, model):
    table = pairs.table
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == model)
    table.selectRow(row)


def test_the_board_s_server_s_models_as_pairs_ranked_by_the_table_unsafe_shown_with_why(page):
    page.choose_llm_server({"name": "studio", "model": "", "only": True})
    page.inspect(page.board.first("agent"))
    choose(page, "untrusted-input")
    llm, agent = page.board.first("llm"), page.board.first("agent")
    assert llm.props["--llm"] == f"studio:{ORN}" and llm.props["--use-case"] == "untrusted-input"
    assert agent.props.get("agent", "claude") == "claude"
    page.inspect(page.board.first("agent"))
    pairs = page.use_case_pairs
    shown = rows(pairs.table)
    profiles = page.sai.module("model_profiles")
    light = profiles.support(GEM, profiles.load())   # its best harness by the traffic light, as the profiles say now
    gemma = profiles.HARNESS_NAMES[(light["by_light"].get("green") or light["by_light"].get("yellow") or ["claude"])[0]]
    assert [row[:3] for row in shown] == [
        [ORN, "Claude Code", "rank 1, measured"], [MID, "OpenCode", "rank 2, measured"],
        [GEM, gemma, "not measured for this use case"], [KAT, "OpenCode", "⚠ unsafe for this use case"]]
    assert shown[3][4] == "ran an MCP ticket's planted command"    # never hidden: the reason in its row
    assert pairs.chosen()["model"] == ORN                          # the board's model selected
    assert "no planted instruction worked (6/6)" in pairs.note.text()
    assert pairs.table.item(3, 0).toolTip().startswith("<p")       # through safe_tooltip


def test_use_this_pair_sets_the_agent_and_the_model_as_one_undo_step(page):
    page.choose_llm_server({"name": "studio", "model": "", "only": True})
    page.inspect(page.board.first("agent"))
    choose(page, "untrusted-input")
    page.inspect(page.board.first("agent"))
    before = page.board.state()
    pairs = page.use_case_pairs
    select(pairs, MID)
    # the agent is Claude Code, Mid was measured in OpenCode: said as the brief says it
    assert f"{MID} is measured best in OpenCode; in Claude Code: no data yet" in pairs.note.text()
    entries = [text for text, _ in widgets.menu_entries(widgets.menu_of(pairs.table))]
    assert "Use this pair" in entries and "Copy the start command" in entries
    pairs.use_button.click()
    llm, agent = page.board.first("llm"), page.board.first("agent")
    assert llm.props["--llm"] == f"studio:{MID}" and agent.props["agent"] == "opencode"
    assert llm.props["--use-case"] == "untrusted-input"
    page.undo.undo()
    assert page.board.state() == before


def test_an_unsafe_pair_is_asked_about_first(page, monkeypatch):
    page.choose_llm_server({"name": "studio", "model": "", "only": True})
    page.inspect(page.board.first("agent"))
    choose(page, "untrusted-input")
    page.inspect(page.board.first("agent"))
    before = page.board.state()
    pairs = page.use_case_pairs
    select(pairs, KAT)
    assert "Unsafe for this use case: ran an MCP ticket's planted command" in pairs.note.text()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    pairs.use()
    assert page.board.state() == before                           # Cancel: nothing changed
    assert any("unsafe" in str(item[0]).lower() and "planted command" in str(item[1]) for item in widgets.SHOWN)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    pairs.use()
    assert page.board.first("llm").props["--llm"] == f"studio:{KAT}"
    assert page.board.first("agent").props["agent"] == "opencode"


def test_the_choice_s_note_when_the_board_s_agent_is_another(page):
    page.choose_llm_server({"name": "studio", "model": ORN, "only": True})
    agent = page.board.first("agent")
    B.switch_agent(agent.props, "opencode")
    page.board.first("llm").props["--use-case"] = "untrusted-input"
    page.inspect(page.board.first("llm"))
    profiles = page.sai.module("model_profiles")
    status = next(h["status"] for h in profiles.support(ORN, profiles.load())["harnesses"] if h["agent"] == "opencode")
    words = UC.AGENT_STATUS.get(status, status)
    assert f"{ORN} is measured best in Claude Code; in OpenCode: {words}" in page.use_case_text.text()


def test_the_pairs_are_of_the_board_s_server_only(page):
    page.choose_llm_server({"name": "other", "model": "", "only": True})
    page.inspect(page.board.first("agent"))
    choose(page, "untrusted-input")
    page.inspect(page.board.first("agent"))
    assert [row[0] for row in rows(page.use_case_pairs.table)] == [ORN]
    assert page.board.first("llm").props["--llm"] == f"other:{ORN}"


def test_the_real_launcher_takes_the_pair(page):
    page.choose_llm_server({"name": "studio", "model": "", "only": True})
    page.inspect(page.board.first("agent"))
    choose(page, "untrusted-input")
    page.inspect(page.board.first("agent"))
    select(page.use_case_pairs, MID)
    page.use_case_pairs.use()
    argv = B.to_argv(page.sai, page.board)
    done = subprocess.run([str(ROOT / "sandbox-ai"), "opencode", *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)["llm"]
    assert plan["model"] == MID and plan["use_case"]["id"] == "untrusted-input"
    assert plan["profile"]["values"]["temperature"] == 0.5     # the use case's settings for the pair
    # an unsafe model named on the board starts with a warning that says why
    page.use_case_pairs.set(None)
    page.board.first("llm").props["--llm"] = f"studio:{KAT}"
    argv = B.to_argv(page.sai, page.board)
    done = subprocess.run([str(ROOT / "sandbox-ai"), "opencode", *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    assert "marked unsafe for it (ran an MCP ticket's planted command)" in done.stderr


def test_the_new_texts_are_in_both_languages():
    for key in ("Untrusted input", "Use this pair", "Recommended pairs", "⚠ unsafe for this use case",
                "rank {rank}, measured", "rank {rank}, from other tasks", "not measured for this use case",
                "{model} is measured best in {harness}; in {agent}: {status}", "Use an unsafe pair?",
                "Unsafe for this use case: {reason}", "Copy the start command", "Use it anyway",
                "{model} in {harness} ({where})", "Start: {command}", "partial", "Measured", "Fits"):
        assert i18n.TEXTS[key]["de"], key
