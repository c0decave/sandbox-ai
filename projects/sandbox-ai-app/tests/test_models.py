"""The model lists of the three agents, each model with its version: Claude Code's aliases and ids, Codex's
models_cache.json, OpenCode's models.dev catalogue; untrusted files read carefully."""
import json
import os

import pytest

from sandbox_ai_app import models as M


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "home" / ".cache"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_version_labels():
    assert M.version_label("claude-opus-5-5") == "Opus 5.5"
    assert M.version_label("claude-fable-5") == "Fable 5"
    assert M.version_label("claude-haiku-4-5-20251001") == "Haiku 4.5 (2025-10-01)"
    assert M.version_label("anthropic/claude-sonnet-5-5") == "Sonnet 5.5"
    assert M.version_label("gpt-5.1-codex-max") == "GPT-5.1 Codex Max"
    assert M.version_label("o3") == "o3" and M.version_label("qwen3-coder") == ""
    assert M.label("claude-opus-5-5") == "Opus 5.5 — claude-opus-5-5"
    assert M.label("x/y", "Y Large", "2026-05-01") == "Y Large (2026-05-01) — x/y"


def test_claude_lists_its_aliases_with_what_they_stand_for_and_every_id(home):
    rows = dict(M.choices("claude"))
    assert list(rows)[:4] == ["fable", "opus", "sonnet", "haiku"], "the launcher's AGENT_MODELS first"
    assert rows["opus"].startswith("Opus 5.5 now") and rows["opus"].endswith("— opus")
    assert rows["claude-opus-5-5"] == "Opus 5.5 — claude-opus-5-5" and rows["claude-haiku-4-5"].startswith("Haiku 4.5")
    assert all(model in rows for model in M.CLAUDE_ALIASES.values())


def test_codex_lists_its_cached_models_with_their_display_names(home, tmp_path):
    assert M.choices("codex") == [], "nothing cached: typed only"
    (home / ".codex").mkdir()
    (home / ".codex" / "models_cache.json").write_text(json.dumps({"fetched_at": "2026-11-01T00:00:00Z", "models": [
        {"slug": "gpt-5.2-codex", "display_name": "GPT-5.2-Codex"}, {"slug": "gpt-5.1-mini"},
        {"slug": "hidden-one", "visibility": "hide"}, {"slug": "bad name\x1b[2J"}, "junk"]}))
    session = tmp_path / "state" / "sandbox-ai" / "sessions" / "s1" / "home" / ".codex"
    session.mkdir(parents=True)
    (session / "models_cache.json").write_text(json.dumps([{"id": "gpt-6", "name": "GPT-6\x1b[31m"}]))
    rows = dict(M.choices("codex"))
    assert rows["gpt-5.2-codex"] == "GPT-5.2-Codex — gpt-5.2-codex" and rows["gpt-5.1-mini"] == "GPT-5.1 Mini — gpt-5.1-mini"
    assert "hidden-one" not in rows and not any("\x1b" in k or "\x1b" in v for k, v in rows.items())
    assert rows["gpt-6"].endswith("— gpt-6")


def test_opencode_lists_the_catalogue_newest_first_with_release_dates(home):
    cache = home / ".cache" / "opencode"
    cache.mkdir(parents=True)
    (cache / "models.json").write_text(json.dumps({
        "anthropic": {"models": {"claude-opus-5-5": {"name": "Claude Opus 5.5", "release_date": "2026-09-30"},
                                 "claude-opus-4-8": {"name": "Claude Opus 4.8", "release_date": "2026-03-01"}}},
        "openai": {"models": {"gpt-5.2": {"name": "GPT-5.2", "release_date": "2026-06-01"}}},
        "somewhere": {"models": {"x": {"name": "X"}}}}))
    rows = M.choices("opencode")
    assert rows[0] == ("anthropic/claude-opus-5-5", "Claude Opus 5.5 (2026-09-30) — anthropic/claude-opus-5-5")
    assert [r[0] for r in rows] == ["anthropic/claude-opus-5-5", "anthropic/claude-opus-4-8", "openai/gpt-5.2"]


def test_a_link_or_a_huge_file_is_not_read(home, tmp_path, monkeypatch):
    (home / ".codex").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"models": [{"slug": "gpt-x"}]}))
    os.symlink(target, home / ".codex" / "models_cache.json")
    assert M.choices("codex") == []
    monkeypatch.setattr(M, "LIMIT", 10)
    assert M.read_json(target) is None


def test_the_combo_shows_versions_and_gives_the_id(qapp):
    from PySide6.QtWidgets import QComboBox
    combo = QComboBox()
    combo.setEditable(True)
    M.fill_combo(combo, "claude", "claude-opus-5-5")
    assert combo.currentText() == "Opus 5.5 — claude-opus-5-5" and M.combo_value(combo) == "claude-opus-5-5"
    M.fill_combo(combo, "claude", "my-own-model")
    assert M.combo_value(combo) == "my-own-model", "a model not listed stays as typed"
    M.fill_combo(combo, "claude", "")
    assert M.combo_value(combo) == ""
    combo.setEditText("Sonnet 5.5 — claude-sonnet-5-5 ")
    assert M.combo_value(combo) == "claude-sonnet-5-5"
