"""Deep test round 2 (user journeys across 0.1.93-0.1.95) in the app: the paths a real user takes through several
features in a row, offscreen through the app's own pages. Every friction point or bug found on the way is a test
named after it."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QComboBox  # noqa: E402
from sandbox_ai_app import backend, board as B, board_view  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the board page fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def choose(page, agent):
    page.inspect(page.board.first("agent"))
    combo = [c for c in page.findChildren(QComboBox) if c.accessibleName() == "Agent"][-1]
    board_view.choose_agent(combo, agent)
    return page.board.first("agent").props


# == Journey 3: the power user switches the Board's agent qwen -> dsh -> claude ======================================

def test_switching_the_agent_drops_what_only_the_last_agent_takes(page, tmp_path):
    """Board: Qwen Code with --qwen-enable updates and effort high, then the agent switched to DeepSeek Harness: the
    board said "--qwen-enable is for Qwen Code only." and the launcher "--effort: dsh sets its effort per model ...",
    and Start stayed refused until both were found and cleared by hand. Switching the agent drops what only the
    agent before took (its -enable options, an effort the new one does not know, a model alias only it has) - one
    step of Undo; what every agent takes stays."""
    page.load(B.from_argv(page.sai, "qwen", ["--qwen-enable", "updates", "--effort", "high", "-D"], [],
                          [str(tmp_path)]), str(tmp_path))
    props = choose(page, "dsh")
    assert props["agent"] == "dsh"
    assert "--qwen-enable" not in props and "effort" not in props, props
    assert props.get("--dangerously-skip-permissions") is True, "what every agent takes stays"
    assert not [p for p in B.problems(page.sai, page.board) if p.level == "error"], B.problems(page.sai, page.board)
    page.undo.undo()
    props = page.board.first("agent").props
    assert props["agent"] == "qwen" and props["--qwen-enable"] == ["updates"] and props["effort"] == "high"
    # Claude with opus and xhigh, then Codex: Codex knows xhigh but not the alias opus.
    page.board.first("agent").props.update(agent="claude", model="opus", effort="xhigh")
    page.board.first("agent").props.pop("--qwen-enable")
    props = choose(page, "codex")
    assert props["agent"] == "codex" and props.get("effort") == "xhigh" and not props.get("model"), props
    # A model the agents share (a server's model, typed) stays.
    page.board.first("agent").props.update(model="qwen3-coder-30b")
    props = choose(page, "qwen")
    assert props.get("model") == "qwen3-coder-30b"


# == Journey 5: the local-LLM user picks a model from a long list =====================================================

def test_the_model_pickers_search_takes_several_words(qapp, monkeypatch):
    """Model picker, LM Studio with 40 models: "qwen 30b" found nothing - the search looked for the words with their
    space as one piece of text, so "qwen3-coder-30b-a3b" never matched. Each word must match, in any order."""
    from sandbox_ai_app import widgets, wizard_llm as WL
    from sandbox_ai_app.model_picker import ModelPicker
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    names = ["qwen3-coder-30b-a3b-instruct", "qwen3-8b", "gemma-3-27b", "llama-3.1-70b-instruct"]
    servers = [{"name": "lms", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "agents": ["opencode"],
                "models": names, "model_info": {n: {"name": n} for n in names}, "answered": True}]
    picker = ModelPicker(None, WL.picker_rows(servers, "opencode"))
    picker.search.setText("qwen 30b")
    assert [picker.table.item(r, 0).text().split()[0] for r in range(picker.table.rowCount())] == \
        ["lms:qwen3-coder-30b-a3b-instruct"]
    picker.search.setText("  INSTRUCT   llama ")
    assert [picker.table.item(r, 0).text().split()[0] for r in range(picker.table.rowCount())] == ["lms:llama-3.1-70b-instruct"]
    picker.search.setText("qwen gemma")
    assert picker.table.rowCount() == 0
    picker.deleteLater()
