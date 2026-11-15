"""The Board's LLM server node and the server's other models (hotfix 0.1.96, offscreen). User: "Auch supporten, wenn
aktiviert ist, dass in z.B. OpenCode andere auf dem LLM-Server vorhandene Modelle geladen/ausgewählt werden können."
A start offers every model of the server in the agent (--llm-models all, the default) or the ones named
(--llm-models MODEL,MODEL); the node keeps that option through the command line and the project file, shows it at a
glance and edits it in its inspector. Every argv is parsed with sandbox-ai's own argparse."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, widgets  # noqa: E402
from sandbox_ai_app.board_view import summary  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def test_regression_the_llm_node_keeps_the_models_it_offers(homes, tmp_path):
    """User: "dass in z.B. OpenCode andere auf dem LLM-Server vorhandene Modelle geladen/ausgewählt werden können":
    the node says that the agent offers all of the server's models, or the ones --llm-models names, and gives the
    option back to the start and the project file."""
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "opencode", ["--workspace", str(tmp_path), "--llm", "lm:qwen3:8b"])
    node = board.first("llm")
    assert "all its models" in summary(node) and "--llm-models" not in B.to_argv(sai, board)
    board = B.from_argv(sai, "opencode", ["--workspace", str(tmp_path), "--llm", "lm:qwen3:8b", "--llm-models",
                                          "llama3.1:8b,gemma3:12b"])
    node = board.first("llm")
    assert node.props["--llm-models"] == "llama3.1:8b,gemma3:12b" and board.extra == []
    assert "models: llama3.1:8b,gemma3:12b" in summary(node)
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--llm-models") + 1] == "llama3.1:8b,gemma3:12b"
    document, _ = B.project_document(sai, board)
    again = B.from_project(sai, document, tmp_path)
    assert again.first("llm").props["--llm-models"] == "llama3.1:8b,gemma3:12b"
    # The models named with the server (NAME:MODEL,MODEL) are the list themselves: nothing else said.
    named = B.from_argv(sai, "opencode", ["--llm", "lm:qwen3:8b,llama3.1:8b"]).first("llm")
    assert "all its models" not in summary(named)


def test_the_inspector_edits_the_models_offered(qapp, homes, monkeypatch):
    (homes / "cwd").mkdir()
    monkeypatch.chdir(homes / "cwd")
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    node = page.add_node("llm", **{"--llm": "lm:qwen3:8b"}) or page.board.first("llm")
    page.inspect(node)
    labels = [label.text() for label in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("Models offered in the agent" in text for text in labels), labels
    edits = [edit for edit in page.inspector_holder.findChildren(QtWidgets.QLineEdit)
             if edit.placeholderText() == "all|MODEL[,MODEL...]"]
    assert edits, "no field for --llm-models"
    edits[0].setText("llama3.1:8b")
    edits[0].editingFinished.emit()
    assert page.board.node(node.id).props.get("--llm-models") == "llama3.1:8b"
