"""Review round 2 (contracts and what the user sees): messages that named only Claude Code, Codex and OpenCode where
every agent sandbox-ai starts is meant (Hermes, DSH, Qwen Code and Goose too)."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("review2_texts_launcher", "sandbox/agent_sandbox.py")
AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose")


def test_a_project_file_without_an_agent_names_every_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "work"
    work.mkdir()
    path = work / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "nobody", "options": []}))
    s.trust_config(path)
    with pytest.raises(ValueError) as caught:
        s.main(["goose", "--workspace", str(work), "--dry-run"])
    assert "names no agent" in str(caught.value)
    for agent in AGENTS:
        assert agent in str(caught.value), agent


def test_the_web_pages_refusals_name_every_agent_they_take():
    webui = load("review2_texts_webui", "sandbox/webui.py")
    assert set(webui.AGENTS) == set(AGENTS)
    api = webui.Api.__new__(webui.Api)
    refusals = []
    for call in (lambda: webui.Api.live_server({}), lambda: webui.Api.image_target("-x"),
                 lambda: api.image_new({"host": "local"}, {"name": "v1", "from": "nobody"}),
                 lambda: api.image_recipe({"host": "local"}, {"recipe": "media", "from": "nobody"})):
        with pytest.raises(webui.Refused) as caught:
            call()
        refusals.append(str(caught.value))
    for text in refusals:
        assert "goose" in text and "hermes" in text, text
