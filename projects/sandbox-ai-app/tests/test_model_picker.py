"""The model picker beside the Model choice (user wish from live use, 2026-11-14): long lists of the model servers'
models get a search field that filters as it is typed, sorts by name, parameter count and size, and a filter by what
a model is for - text, code, vision (VL), image (diffusion), embedding, reasoning - from what the servers report
(Ollama's families, LM Studio's vlm, OpenRouter's modalities), else from the name; shown as tags per model. On the
Board's agent part and the Start page; fake server answers, nothing is read on the GUI thread."""
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets, wizard_llm as WL  # noqa: E402
from sandbox_ai_app.model_picker import ModelPicker  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests

MODELS = [{"name": "qwen2.5-coder:32b", "size": 19_000_000_000, "parameters": "32.8B"},
          {"name": "llava:13b", "size": 8_000_000_000, "families": ["llama", "clip"], "parameters": "13B"},
          {"name": "nomic-embed-text", "size": 270_000_000},
          {"name": "deepseek-r1:7b", "size": 4_700_000_000},
          {"name": "flux-schnell", "capabilities": ["image"]},
          {"name": "llama3.1:70b", "size": 40_000_000_000, "parameters": "70.6B"}]


def servers():
    return [{"name": "home", "kind": "ollama", "url": "http://127.0.0.1:11434", "agents": ["claude", "codex",
                                                                                          "opencode"],
             "models": [m["name"] for m in MODELS], "model_info": {m["name"]: m for m in MODELS},
             "answered": True, "anthropic": True}]


def test_tags_sizes_and_parameters_from_the_server_or_the_name():
    rows = {r["model"]: r for r in WL.picker_rows(servers(), "codex") if r["model"]}
    assert rows["qwen2.5-coder:32b"]["tags"] == ["text", "code"] and rows["qwen2.5-coder:32b"]["params"] == 32.8
    assert rows["llava:13b"]["tags"] == ["text", "vision"], "Ollama's families: clip"
    assert rows["nomic-embed-text"]["tags"] == ["embedding"]
    assert rows["deepseek-r1:7b"]["tags"] == ["text", "reasoning"] and rows["deepseek-r1:7b"]["params"] == 7.0
    assert rows["flux-schnell"]["tags"] == ["image"]
    assert WL.parameter_billions("mixtral:8x7b") == 56.0 and WL.parameter_billions({"name": "x",
                                                                                     "parameters": "350M"}) == 0.35
    assert WL.model_tags({"name": "m", "type": "vlm"}) == ["text", "vision"]
    assert WL.model_tags("qwen2.5-vl:7b") == ["text", "vision"] and WL.model_tags("stable-diffusion-xl") == ["image"]
    by_params = [r["model"] for r in WL.sort_rows(list(rows.values()), "params")]
    assert by_params[:3] == ["llama3.1:70b", "qwen2.5-coder:32b", "llava:13b"] and by_params[-1] == "nomic-embed-text"
    by_size = [r["model"] for r in WL.sort_rows(list(rows.values()), "size")]
    assert by_size[0] == "llama3.1:70b" and by_size[-1] == "flux-schnell", "unknown size last"
    assert [r["model"] for r in WL.filter_rows(list(rows.values()), "QWEN")] == ["qwen2.5-coder:32b"]
    assert [r["model"] for r in WL.filter_rows(list(rows.values()), "", "vision")] == ["llava:13b"]


def test_the_picker_filters_as_typed_sorts_and_tags(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    picker = ModelPicker(None, WL.picker_rows(servers(), "codex"), "home:llava:13b")
    total = picker.table.rowCount()
    assert total == 7 and picker.current_value() == "home:llava:13b", "the agent's provider, then each model"
    picker.search.setText("coder")
    assert picker.table.rowCount() == 1 and "home:qwen2.5-coder:32b" in picker.table.item(0, 0).text()
    assert picker.table.item(0, 1).text() == "[text] [code]" and picker.table.item(0, 2).text() == "32.8B"
    picker.search.clear()
    picker.tag.setCurrentIndex(picker.tag.findData("embedding"))
    assert [picker.table.item(r, 0).text() for r in range(picker.table.rowCount())] == ["home:nomic-embed-text"]
    picker.tag.setCurrentIndex(0)
    picker.sort.setCurrentIndex(picker.sort.findData("params"))
    assert "llama3.1:70b" in picker.table.item(1, 0).text(), "the agent's provider first, then the largest"
    assert "of 7 models" in picker.count.text()
    picker.table.selectRow(1)
    picker.take(1)
    assert picker.chosen() == "home:llama3.1:70b"
    picker.deleteLater()


def test_the_start_page_and_the_board_have_the_picker(window, monkeypatch):
    page = window.launch
    page.llm_servers = servers()
    picked = []
    monkeypatch.setattr("sandbox_ai_app.model_picker.ModelPicker.exec",
                        lambda self: (self.table.selectRow(2), self.take(2), picked.append(self.chosen()))[-1] and 1
                        or QtWidgets.QDialog.DialogCode.Accepted)
    assert page.model_picker.toolTip().startswith("Choose a model")
    page.model_picker.click()
    assert picked and page.model_choice.currentData() == picked[0]
    board = window.board
    assert board.llm_server_row is not None
    tags = WL.picker_rows(servers(), "claude")
    assert any(r["tags"] for r in tags)
    assert json.dumps(backend.SandboxAI.host_llm_models.__doc__)
