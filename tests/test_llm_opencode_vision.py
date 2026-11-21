"""OpenCode and a vision model behind --llm (local-model research round 5, finding F1). sandbox-ai's OpenCode
configuration named each model with its window only; OpenCode then takes a custom provider's model for text-only and
answers the read of an image with "ERROR: Cannot read image (this model does not support image input)" - also for
Gemma-4-31B, Ornith-1.5-122B or Qwen3.6, which read images (round 4's vision probes: perfect). The server's listing
says which models read images (LM Studio: capabilities.vision, type vlm; OpenRouter-like: input modalities); the
start remembers it with the endpoint's models (like their windows) and gives those models
modalities.input [text, image] and attachment true in OpenCode's (and OpenCode 2's) provider configuration."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_llm_opencode_vision", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402

VISION, TEXT, NAME = "google/gemma-4-31b-qat", "qwen/qwen3-coder-next", "studio"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)


@pytest.fixture
def studio():
    server, port = model_mock.start(models=(TEXT, VISION), lmstudio=True, vision=(VISION,),
                                    loaded={VISION: 65536, TEXT: 65536})
    lines = []
    assert llm.main(["add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}", "--model", TEXT],
                    out=lines.append) == 0, lines
    yield port
    server.shutdown()


def start(model, **options):
    defaults = {"llm": f"{NAME}:{model}", "agent": "opencode", "env": [], "secret_env": [], "llm_param": [],
                "vpn": None, "no_provider_defaults": False, "llm_models": None}
    return argparse.Namespace(**{**defaults, **options})


def models_of(config):
    return next(iter(config["provider"].values()))["models"]


def test_opencode_config_gives_vision_models_image_input():
    endpoint = llm.check_record(NAME, {"kind": "lmstudio", "url": "http://127.0.0.1:1234", "models": [TEXT, VISION]})
    models = models_of(llm.opencode_config(endpoint, VISION, contexts={VISION: 65536}, vision={VISION}))
    assert models[VISION]["modalities"] == {"input": ["text", "image"], "output": ["text"]}
    assert models[VISION]["attachment"] is True and models[VISION]["limit"]["context"] == 65536
    assert "modalities" not in models[TEXT] and "attachment" not in models[TEXT]
    # without a window known the image input is still declared
    assert models_of(llm.opencode_config(endpoint, VISION, vision={VISION}))[VISION]["attachment"] is True


def test_regression_a_vision_model_reads_images_in_opencode(studio):
    """Round 5 (mock_v9_uibug__opencode): "ERROR: Cannot read image (this model does not support image input)" for
    Gemma-4-31B. The listing's vision flag reaches OpenCode's configuration; a text model gets none."""
    a = start(VISION)
    found, model = llm.selection(a)
    config, _ = llm.opencode_layer(a, found, model)
    models = models_of(config)
    assert models[VISION]["modalities"]["input"] == ["text", "image"]
    assert "modalities" not in models[TEXT]


def test_the_vision_flag_is_remembered_for_starts_that_do_not_ask(studio, monkeypatch):
    lines = []
    assert llm.main(["models", NAME], out=lines.append) == 0, lines
    assert llm.endpoint(NAME)["vision"] == [VISION]
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
    a = start(TEXT)
    found, model = llm.selection(a)
    assert models_of(llm.opencode_layer(a, found, model)[0])[VISION]["attachment"] is True


def test_a_stored_vision_list_is_checked():
    record = {"kind": "lmstudio", "url": "http://127.0.0.1:1234", "models": [VISION], "vision": [VISION]}
    assert llm.check_record(NAME, record)["vision"] == [VISION]
    with pytest.raises(ValueError, match="vision"):
        llm.check_record(NAME, {**record, "vision": "all"})
    with pytest.raises(ValueError, match="vision"):
        llm.check_record(NAME, {**record, "vision": ["bad model\\n"]})
