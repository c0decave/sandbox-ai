"""The app never showed what Ollama really serves a model with: its models table had no window for an Ollama model
(Ollama's listing has none) and the Advanced settings' "Context window" said "as the server reports it per model" -
what the start then told the agent came from /api/show, the model's own maximum, until the start learnt the served
one itself. `sandbox-ai llm models NAME --json` now carries each Ollama model's served window and how it was learnt
(served_context, served_how: "loaded", "modelfile", "default"); the models table shows it (its tooltip says how), and
the Advanced settings say it for the model chosen. Offscreen, and end to end through the real sandbox-ai's
`llm models` against the Ollama-like mock server."""
import json
import os
from pathlib import Path
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, llm_page, llm_params_view as LPV, wizard_llm as WL  # noqa: E402
from test_llm_params_ui import SERVER, VIEW, homes, sai  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
LOADED, OWN, PLAIN = "qwen2.5-coder:7b", "llama3.1:8b", "gemma3:4b"
LISTING = [{"name": LOADED, "size": 1, "loaded": True, "details": "", "served_context": 16384, "served_how": "loaded"},
           {"name": OWN, "size": 1, "loaded": False, "details": "", "served_context": 8192, "served_how": "modelfile"},
           {"name": PLAIN, "size": 1, "loaded": False, "details": "", "served_how": "default"}]


def test_the_listing_keeps_the_served_window_and_how_and_drops_what_is_not_one():
    models = {m["name"]: m for m in WL.parse_models(json.dumps(LISTING + [
        {"name": "x:1", "size": 1, "loaded": False, "details": "", "served_context": "8192", "served_how": "evil"},
        {"name": "y:1", "size": 1, "loaded": False, "details": "", "served_context": True, "served_how": None}]))}
    assert (models[OWN]["served_context"], models[OWN]["served_how"]) == (8192, "modelfile")
    assert "served_context" not in models[PLAIN] and models[PLAIN]["served_how"] == "default"
    assert "served_context" not in models["x:1"] and "served_how" not in models["x:1"]
    assert "served_context" not in models["y:1"] and "served_how" not in models["y:1"]


def test_the_context_column_and_its_words():
    models = {m["name"]: m for m in WL.parse_models(json.dumps(LISTING))}
    assert WL.model_cell(models[LOADED], "context") == ("16k", 16384)
    assert WL.model_cell(models[OWN], "context") == ("8k", 8192)
    assert WL.model_cell(models[PLAIN], "context")[0] == "default"
    assert WL.model_cell({"name": "m", "context": 32768}, "context") == ("32k", 32768), "LM Studio's, as before"
    assert [k for k, _ in WL.model_columns(list(models.values()))].count("context") == 1
    assert WL.served_text(models[OWN]) == "8192 tokens, as Ollama serves it (its Modelfile's num_ctx)"
    assert WL.served_text(models[LOADED]) == "16384 tokens, as Ollama serves it (as it is loaded)"
    assert "OLLAMA_CONTEXT_LENGTH" in WL.served_text(models[PLAIN]) and "loading" in WL.served_text(models[PLAIN])
    assert WL.served_text({"name": "m", "context": 4096}) == ""


def test_the_page_shows_it_in_the_table_and_the_advanced_settings(qapp, sai, monkeypatch):  # noqa: F811
    shown = []

    def run(dialog):
        shown.append(dialog)
        texts = {}
        for model in (OWN, PLAIN, LOADED):
            dialog.scope.setCurrentIndex(dialog.scope.findData(model))
            texts[model] = dialog.context.text()
        dialog.scope.setCurrentIndex(dialog.scope.findData("qwen3:8b"))
        texts["reported"] = dialog.context.text()
        shown.append(texts)
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(LPV.ParamsDialog, "exec", run)
    page = llm_page.LlmPage(sai)
    page.show()
    page.servers.selectRow(0)
    page.models_of, page.model_list = ("local", "lm"), [*WL.parse_models(json.dumps(LISTING)), {"name": "qwen3:8b"}]
    page.show_models()
    table = page.model_table
    headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    column = headers.index("Context")
    rows = {table.item(r, 0).text(): r for r in range(table.rowCount())}
    assert table.item(rows[OWN], column).text() == "8k"
    assert "its Modelfile's num_ctx" in table.item(rows[OWN], 0).toolTip()
    assert table.item(rows[PLAIN], column).text() == "default"
    table.selectRow(rows[OWN])
    page.params_button.click()
    texts = shown[1]
    assert texts[OWN] == "8192 tokens, as Ollama serves it (its Modelfile's num_ctx)"
    assert texts[LOADED] == "16384 tokens, as Ollama serves it (as it is loaded)"
    assert "OLLAMA_CONTEXT_LENGTH" in texts[PLAIN]
    assert "32768" in texts["reported"], "a window the server reports stays as it was"
    page.close()
    page.deleteLater()


def test_e2e_the_real_llm_models_of_an_ollama_server(homes):  # noqa: F811
    root = Path(os.environ.get("SANDBOX_AI_HOME") or Path(backend.shutil.which("sandbox-ai")).resolve().parent)
    sys.path.insert(0, str(root / "tests/devtools"))
    import model_mock
    server, port = model_mock.start(models=(LOADED, OWN, PLAIN), ollama_ps=True,
                                    running={LOADED: {"context": 16384, "expires_at": "2318-01-01T00:00:00Z"}},
                                    modelfile_context={OWN: 8192})
    try:
        sai = backend.SandboxAI()
        status, out, err = sai.call("llm", "add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}",
                                    timeout=60)
        assert status == 0, err
        models = {m["name"]: m for m in WL.list_models(sai, "ol")}
        assert WL.served_text(models[OWN]) == "8192 tokens, as Ollama serves it (its Modelfile's num_ctx)"
        assert models[LOADED]["served_how"] == "loaded" and models[PLAIN]["served_how"] == "default"
    finally:
        server.shutdown()
