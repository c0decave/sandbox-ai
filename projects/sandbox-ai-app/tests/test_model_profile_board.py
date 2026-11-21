"""Model x harness profiles in the app (offscreen, 0.1.98). User: "have perfect configs and settings for the models
ready and auto loaded if the board is started" - applied automatically AND visibly: the Board's LLM part says the
profile of its model in the board's agent ("Model profile: qwen3-coder-next × opencode (verified)"), its inspector
and the agent's show the values with "your value wins" where the board or the server sets one, a switch turns it off
(--no-llm-profile, which the real ./sandbox-ai takes), and a pair known not to work is warned about, never refused.
The Start page says it below the model too."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, i18n, model_profile as MP, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
QCN = "qwen/qwen3-coder-next"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    assert sai.module("llm").main(["add", "studio", "--kind", "lmstudio", "--url", "http://127.0.0.1:9", "--model",
                                   QCN], out=lambda line: None) == 0
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    view.llm_servers = [{"name": "studio", "kind": "lmstudio", "model": QCN, "models": [QCN], "params": {},
                         "loop_guard_own": {}}]
    yield view
    view.deleteLater()


def with_model(page, agent, model):
    B.switch_agent(page.board.first("agent").props, agent)
    page.choose_llm_server({"name": "studio", "model": model, "only": True})
    page.inspect(page.board.first("llm"))


def texts(widget):
    return [label.text() for label in widget.findChildren(QtWidgets.QLabel)]


def dry_run(agent, argv):
    done = subprocess.run([str(ROOT / "sandbox-ai"), agent, *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def test_the_llm_part_shows_the_profile_and_its_values(page):
    with_model(page, "opencode", QCN)
    assert page.profile_label.text() == "Model profile: qwen3-coder-next × opencode (verified)"   # round 8
    values = page.profile_values.text()
    assert "temperature (temperature): 0.7" in values and "context length (num_ctx): 131072" in values
    assert "turn limit (loop guard steps) (loop_guard.steps): 150" in values
    assert "102 requests" in page.profile_label.toolTip() and "parallel 1" in page.profile_label.toolTip()
    assert page.profile_switch.isChecked() and page.profile_switch.isEnabled()
    # On the card itself, at a glance.
    item = page.items[page.board.first("llm").id]
    assert "Model profile: qwen3-coder-next × opencode (verified)" in item.toolTip()


def test_your_values_are_marked_as_winning(page):
    with_model(page, "opencode", QCN)
    page.set_prop(page.board.first("llm").id, "params", {"temperature": "0.2", "loop_guard.steps": "60"})
    page.llm_servers[0]["params"] = {"*": {"top_k": 40}}
    page.inspect(page.board.first("llm"))
    lines = page.profile_values.text().splitlines()
    assert "temperature (temperature): 0.7 — your value wins" in lines
    assert "top K (top_k): 20 — your value wins" in lines
    assert "turn limit (loop guard steps) (loop_guard.steps): 150 — your value wins" in lines
    assert "top P (top_p): 0.8" in lines


def test_the_switch_turns_it_off_and_the_real_launcher_takes_it(page):
    with_model(page, "opencode", QCN)
    page.profile_switch.setChecked(False)
    assert page.board.first("llm").props["--no-llm-profile"] is True
    argv = B.to_argv(page.sai, page.board)
    assert "--no-llm-profile" in argv
    plan, stderr = dry_run("opencode", argv)
    assert plan["llm"]["profile"]["enabled"] is False and "is off (--no-llm-profile)" in stderr
    page.inspect(page.board.first("llm"))
    assert page.profile_label.text() == "Model profile off: qwen3-coder-next × opencode"
    again = B.from_argv(page.sai, "opencode", argv)
    assert again.first("llm").props.get("--no-llm-profile") is True, "back from the command line (a project file)"
    page.profile_switch.setChecked(True)
    argv = B.to_argv(page.sai, page.board)
    assert "--no-llm-profile" not in argv
    plan, stderr = dry_run("opencode", argv)
    assert plan["llm"]["profile"]["applied"] and "profile qwen3-coder-next × opencode (verified)" in stderr


def test_same_model_other_agent_and_a_pair_known_bad(page):
    with_model(page, "hermes", QCN)  # round 8: 14 graded runs, mean 0.80 (round 1 had a smoke test only)
    assert page.profile_label.text() == "Model profile: qwen3-coder-next × hermes (partly working)"
    with_model(page, "codex", "qwen/qwen3.8-27b")
    assert page.profile_label.text() == "Model profile: qwen3.8-27b × codex (known not to work)"
    warnings = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)
                if w.property("role") == "notice-warn"]
    assert any("prefill" in text for text in warnings), warnings
    assert page.profile_badge().startswith("! ")
    assert "context length (num_ctx): 49152" in page.profile_values.text()


def test_the_agent_part_shows_it_beside_the_models_advanced_settings(page):
    with_model(page, "claude", "nvidia/nemotron-3-super")
    page.inspect(page.board.first("agent"))
    assert page.profile_label.text() == "Model profile: nemotron-3-super × claude (partly working)"
    assert "CLAUDE_CODE_DISABLE_THINKING: 1" in page.profile_values.text()


def test_no_profile_for_a_model_of_no_family(page):
    page.llm_servers[0]["models"] = ["llama3:8b"]
    with_model(page, "opencode", "llama3:8b")
    assert "Model profile" not in " ".join(texts(page.inspector_holder))
    assert page.profile_badge() == ""


def test_in_german(page):
    i18n.set_language("de")
    try:
        with_model(page, "opencode", QCN)
        assert page.profile_label.text() == "Modellprofil: qwen3-coder-next × opencode (verifiziert)"
        assert page.profile_switch.text() == "Modellprofil anwenden (seine Einstellungen unter Ihren)"
        assert "Temperatur (temperature): 0.7" in page.profile_values.text()
    finally:
        i18n.set_language("en")


def test_the_start_page_says_it_below_the_model(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_llm_servers", lambda *a, **k: [])
    launch = ui.LaunchPage(sai, lambda *a, **k: None)
    launch.agent.setCurrentText("codex")
    launch.form.widgets["--llm"].setCurrentText("studio:qwen/qwen3.8-27b") \
        if isinstance(launch.form.widgets["--llm"], QtWidgets.QComboBox) \
        else launch.form.widgets["--llm"].setText("studio:qwen/qwen3.8-27b")
    launch.form.changed.emit()
    note = launch.model_profile_note
    assert not note.isHidden()
    assert note.text().startswith("! Model profile: qwen3.8-27b × codex (known not to work)")
    assert "prefill" in note.text() and "49152" in note.toolTip()
    launch.form.widgets["--no-llm-profile"].setChecked(True)
    launch.form.changed.emit()
    assert note.text().startswith("Model profile off: qwen3.8-27b × codex")
    assert "--no-llm-profile" in launch.argv()
    launch.deleteLater()


def test_the_texts_are_in_both_languages():
    for key in (*MP.STATUS.values(), *MP.WORDS.values()):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"], key


# --- the "What we know" box ------------------------------------------------------------------------------------------
# User: "please also add a box to the app with agent + harness what we know and what we dont with short bullet points
# and what is working and what not." Beside the profile on the Board (the LLM part's and the agent's properties) and
# on the Start page: the pair's status in words, then ✓ works, ✗ does not work, ? not known yet - information only.

def box_lines(box):
    return box.text().splitlines()


def test_the_box_says_what_we_know_of_a_measured_pair(page):
    """A pair short of verified says what is missing (Qwen3-Coder-Next × Codex; round 8 verified it in OpenCode)."""
    with_model(page, "codex", QCN)
    box = page.knowledge_box
    assert not box.isHidden() and box.title() == "What we know: qwen3-coder-next × codex"
    lines = box_lines(box)
    # derived (scripts/profile_status.py): the mean is short of 0.85 - what is missing is said
    assert lines[1] == "Partly working: something passed, short of verified (the evidence says what is missing)."
    assert lines[2].startswith("Measured: 21 graded runs (11 passed), 5 use cases") and \
        "partial: mean 0.82 (at least 0.85)" in lines[2] and "By hand: round 1: smoke 2/2" in lines[2]
    own = "  • smoke test: 2 of 2 passed (23-27 s; with the proxy's Codex fixes)"
    for heading, bullet in (("✓ Works", own),
                            ("✗ Does not work", "  • debug task: 0 of 2 (20-min limit, up to 193 requests)"),
                            ("? Not known yet", "  • Ollama, llama.cpp and vLLM servers: not measured (LM Studio only)")):
        assert heading in lines and bullet in lines, (heading, lines)
        assert lines.index(heading) < lines.index(bullet)
    assert lines.index("✓ Works") < lines.index("✗ Does not work") < lines.index("? Not known yet")
    # The pair's own bullets first, then the family's.
    assert lines.index(own) < lines.index("  • smoke test passed in all nine agents measured (16-45 s in round 1)")
    shown = " ".join(texts(page.inspector_holder))
    assert own.strip(" •") in shown and "What we know" in shown
    # The agent's properties show it too, beside the model's settings.
    page.inspect(page.board.first("agent"))
    assert page.knowledge_box.title() == "What we know: qwen3-coder-next × codex"
    # A verified pair says so (round 8: OpenCode).
    with_model(page, "opencode", QCN)
    assert box_lines(page.knowledge_box)[1].startswith("Verified: at least 10 graded runs over 5 use cases")


def test_the_box_folds_and_its_commands_are_on_right_click(page):
    """Compact: one click folds it to its title line (and it stays folded on the next part shown); its header's
    commands (fold, copy) are on its context menu."""
    with_model(page, "opencode", QCN)
    box = page.knowledge_box
    assert box.body.isVisibleTo(box)
    assert widgets.menu_entries(box.menu()) == [("Hide details", True), ("Copy what we know", True)]
    box.header.click()
    assert not box.body.isVisibleTo(box)
    page.inspect(page.board.first("llm"))
    box = page.knowledge_box
    assert not box.body.isVisibleTo(box), "folded stays folded"
    assert widgets.menu_entries(box.menu()) == [("Show details", True), ("Copy what we know", True)]
    next(a for a in box.menu().actions() if a.text() == "Show details").trigger()
    assert box.body.isVisibleTo(box)
    next(a for a in box.menu().actions() if a.text() == "Copy what we know").trigger()
    assert QtWidgets.QApplication.clipboard().text() == box.text()


def test_the_box_for_a_pair_without_an_entry(page):
    """Not tested in this agent: said plainly, with the family's general notes and what it knows of the model."""
    page.llm_servers[0]["models"] = ["google/gemma-4-31b-qat"]
    with_model(page, "goose", "google/gemma-4-31b-qat")
    lines = box_lines(page.knowledge_box)
    assert lines[0] == "What we know: gemma-4 × goose"
    assert lines[1] == "This pair was not tested: the model's settings for every agent apply."
    assert "measured: gemma-4-31b-qat (GGUF)" in lines
    assert "  • vision perfect on all three APIs" in lines and "? Not known yet" in lines
    assert not any("smoke test passed (78 s)" in line for line in lines), "another agent's result is not shown"


def test_the_box_for_a_model_known_not_to_work(page):
    with_model(page, "codex", "qwen/qwen3.8-27b")
    lines = box_lines(page.knowledge_box)
    assert lines[1] == "This pair was not tested: the model's settings for every agent apply."
    assert lines[2].startswith("Known not to work: too slow for big agent contexts")
    assert "  • no run in a real agent" in lines


def test_the_box_for_a_model_of_no_family(page):
    page.llm_servers[0]["models"] = ["llama3:8b"]
    with_model(page, "opencode", "llama3:8b")
    box = page.knowledge_box
    assert not box.isHidden()
    assert box_lines(box) == ["What we know: llama3:8b × opencode",
                              "Nothing is known about this model: no model family of sandbox-ai's research matches "
                              "its name. The agent's and the server's own settings apply."]
    assert page.profile_badge() == ""


def test_the_box_in_german(page):
    i18n.set_language("de")
    try:
        with_model(page, "codex", QCN)
        lines = box_lines(page.knowledge_box)
        assert lines[0] == "Was wir wissen: qwen3-coder-next × codex"
        assert lines[1].startswith("Teilweise funktionsfähig: Etwas wurde bestanden")
        assert "✓ Funktioniert" in lines and "✗ Funktioniert nicht" in lines and "? Noch nicht bekannt" in lines
        assert widgets.menu_entries(page.knowledge_box.menu())[1] == ("Kopieren, was wir wissen", True)
    finally:
        i18n.set_language("en")


def test_the_start_page_shows_the_box_below_the_model(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_llm_servers", lambda *a, **k: [])
    launch = ui.LaunchPage(sai, lambda *a, **k: None)

    def choose(agent, value):
        launch.agent.setCurrentText(agent)
        field = launch.form.widgets["--llm"]
        field.setCurrentText(value) if isinstance(field, QtWidgets.QComboBox) else field.setText(value)
        launch.form.changed.emit()
        return launch.knowledge_box

    box = choose("opencode", "")
    assert box.isHidden(), "no model chosen: no box"
    box = choose("opencode", f"studio:{QCN}")
    assert not box.isHidden() and box.title() == "What we know: qwen3-coder-next × opencode"
    assert "  • smoke test: 2 of 2 passed (23-27 s)" in box_lines(box)
    box = choose("codex", "studio:qwen/qwen3.8-27b")
    assert box.title() == "What we know: qwen3.8-27b × codex"
    assert "This pair was not tested: the model's settings for every agent apply." in box_lines(box)
    box = choose("opencode", "studio:llama3:8b")
    assert not box.isHidden() and box_lines(box)[1].startswith("Nothing is known about this model")
    assert launch.model_profile_note.isHidden()
    launch.deleteLater()


def test_the_box_texts_are_in_both_languages():
    for key in (*MP.STATUS_WORDS.values(), *(title for _, _, title in MP.GROUPS)):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"], key
