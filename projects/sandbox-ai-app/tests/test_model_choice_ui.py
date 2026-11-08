"""The Model choice beside the agent, offscreen: on the launch page and in the board's agent node. It lists the agent's
own provider and every registered model server (sandbox-ai llm list) as NAME and NAME:MODEL, greys out a server the
agent cannot use (Claude Code without the Anthropic API), and choosing one gives --llm NAME[:MODEL] (and
--no-provider-defaults with "only this server") and no --model."""
import os
import shlex

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
SERVERS = [{"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434", "models": ["qwen3-coder:30b"],
            "agents": ["claude", "codex", "opencode"]},
           {"name": "api", "kind": "openai-compatible", "url": "http://192.168.1.21:4000", "models": [],
            "agents": ["codex", "opencode"]}]


@pytest.fixture
def sai(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [dict(s) for s in SERVERS])
    return sai


def items(combo):
    return [(combo.itemData(i), combo.model().item(i).isEnabled()) for i in range(combo.count())]


def command(page):
    return shlex.split(page.preview.toPlainText())


def test_the_launch_page_offers_the_servers_beside_the_agent(sai, wait):
    from sandbox_ai_app.ui import LaunchPage
    page = LaunchPage(sai, lambda *a, **k: None)
    wait(lambda: page.llm_servers)
    page.agent.setCurrentText("claude")
    assert items(page.model_choice) == [("", True), ("ollama", True), ("ollama:qwen3-coder:30b", True),
                                        ("api", False)]  # Claude Code: no Anthropic API at "api"
    assert "provider" in page.model_choice.itemText(0) and not page.model_only.isEnabled()
    page.form.widgets["--model"].setText("sonnet")
    page.model_choice.setCurrentIndex(page.model_choice.findData("ollama:qwen3-coder:30b"))
    page.model_choice.activated.emit(page.model_choice.currentIndex())
    line = command(page)
    assert line[line.index("--llm") + 1] == "ollama:qwen3-coder:30b" and "--model" not in line
    assert page.model_only.isEnabled() and "--no-provider-defaults" not in line
    page.model_only.setChecked(True)
    assert "--no-provider-defaults" in command(page)
    page.agent.setCurrentText("codex")  # Codex can use every server
    assert all(enabled for _, enabled in items(page.model_choice))
    assert page.model_choice.currentData() == "ollama:qwen3-coder:30b" and page.model_only.isChecked()
    # typed into the options: the choice follows
    page.form.widgets["--llm"].setText("api")
    assert page.model_choice.currentData() == "api"
    # back to the provider: neither --llm nor --no-provider-defaults
    page.model_choice.setCurrentIndex(0)
    page.model_choice.activated.emit(0)
    line = command(page)
    assert "--llm" not in line and "--no-provider-defaults" not in line and not page.model_only.isEnabled()
    page.deleteLater()


def test_the_board_agent_node_has_the_model_server_beside_the_agent(sai, wait):
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    agent = page.board.first("agent")
    page.set_prop(agent.id, "model", "claude-opus-5-5")
    page.inspect(page.board.first("agent"))
    wait(lambda: page.llm_servers is not None)
    page.inspect(page.board.first("agent"))
    combo = page.llm_combo
    assert items(combo) == [("", True), ("ollama", True), ("ollama:qwen3-coder:30b", True), ("api", False)]
    combo.setCurrentIndex(combo.findData("ollama"))
    combo.activated.emit(combo.currentIndex())
    assert page.board.first("llm").props["--llm"] == "ollama"
    assert "model" not in page.board.first("agent").props
    preview = page.preview.toPlainText()
    assert "--llm ollama" in preview and "--model" not in preview
    page.inspect(page.board.first("agent"))
    assert page.llm_combo.currentData() == "ollama" and not page.model_combo.isEnabled()
    # only this server, then undo; then the agent's provider again
    page.llm_combo.parentWidget().findChild(QtWidgets.QCheckBox).setChecked(True)
    assert page.board.first("network").props.get("--no-provider-defaults") is True
    page.undo.undo()
    assert not page.board.first("network").props.get("--no-provider-defaults")
    page.inspect(page.board.first("agent"))
    page.llm_combo.setCurrentIndex(0)
    page.llm_combo.activated.emit(0)
    assert page.board.first("llm") is None and "--llm" not in page.preview.toPlainText()
    page.deleteLater()
