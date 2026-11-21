"""The model lists of every agent (sandbox-ai's table of cloud models), grouped by provider, with their effort levels;
what Codex and OpenCode fetched themselves on top; untrusted files read carefully."""
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
    assert rows["opus"].startswith("Claude Opus 5.5 now") and rows["opus"].endswith("— opus")
    assert rows["fable"].startswith("Claude Fable 5.2 now")
    assert rows["claude-opus-5-5"] == "Claude Opus 5.5 — claude-opus-5-5"
    assert rows["claude-haiku-4-5"].startswith("Claude Haiku 4.5")
    assert list(rows) == list(M.known_ids()["claude"]), "the table's, in its order"


def test_every_agent_has_its_models_grouped_by_provider(home):
    for agent in ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
                  "letta", "pi"):
        groups = M.grouped(agent)
        assert groups and all(rows for _, rows in groups), agent
    providers = [provider for provider, _ in M.grouped("opencode")]
    assert {"Anthropic", "OpenAI", "Google", "OpenRouter"} <= set(providers)
    assert [provider for provider, _ in M.grouped("hermes")] == ["Nous Portal"]
    tip = dict((model, tip) for _, rows in M.grouped("claude") for model, _, tip in rows)["claude-opus-4-6"]
    assert "Effort: low, medium, high, max (default high)" in tip and "Context: 200k tokens" in tip


def test_efforts_per_model_and_the_reset(home):
    assert M.efforts("claude", "claude-opus-4-6") == ("low", "medium", "high", "max")
    assert M.efforts("claude", "anything-else") == ("low", "medium", "high", "xhigh", "max")
    assert M.efforts("opencode", "anthropic/claude-opus-5-5") == ()
    props = {"agent": "claude", "model": "claude-haiku-4-5", "effort": "high"}
    assert M.keep_effort(props, "claude") == "high" and "effort" not in props
    props = {"agent": "claude", "model": "opus", "effort": "max"}
    assert M.keep_effort(props, "claude") is None and props["effort"] == "max"
    assert M.effort_problem("claude", "claude-opus-4-6", "xhigh") == \
        "--effort for claude-opus-4-6 in Claude Code is one of low, medium, high, max: 'xhigh'"
    assert M.unknown_note("claude", "opus") is None and "not in the known list" in M.unknown_note("claude", "x-1")


def test_codex_adds_its_cached_models_the_table_does_not_list(home, tmp_path):
    table = [model for model, _ in M.choices("codex")]
    assert table == list(M.known_ids()["codex"]), "nothing cached: the table's"
    (home / ".codex").mkdir()
    (home / ".codex" / "models_cache.json").write_text(json.dumps({"fetched_at": "2026-11-01T00:00:00Z", "models": [
        {"slug": "gpt-5.2-codex", "display_name": "GPT-5.2-Codex"}, {"slug": "gpt-5.1-mini"},
        {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol"},
        {"slug": "hidden-one", "visibility": "hide"}, {"slug": "bad name\x1b[2J"}, "junk"]}))
    session = tmp_path / "state" / "sandbox-ai" / "sessions" / "s1" / "home" / ".codex"
    session.mkdir(parents=True)
    (session / "models_cache.json").write_text(json.dumps([{"id": "gpt-7", "name": "GPT-7\x1b[31m"}]))
    groups = dict(M.grouped("codex"))
    fetched = dict((m, t) for m, t, _ in groups["Fetched by the agent on this computer"])
    assert fetched["gpt-5.2-codex"] == "GPT-5.2-Codex — gpt-5.2-codex" and fetched["gpt-5.1-mini"] == \
        "GPT-5.1 Mini — gpt-5.1-mini"
    assert "gpt-6-sol" not in fetched, "the table has it already"
    assert "hidden-one" not in fetched and not any("\x1b" in k or "\x1b" in v for k, v in fetched.items())
    assert fetched["gpt-7"].endswith("— gpt-7")


def test_opencode_adds_the_catalogue_newest_first_with_release_dates(home):
    cache = home / ".cache" / "opencode"
    cache.mkdir(parents=True)
    (cache / "models.json").write_text(json.dumps({
        "anthropic": {"models": {"claude-opus-5-5": {"name": "Claude Opus 5.5", "release_date": "2026-09-30"},
                                 "claude-opus-9": {"name": "Claude Opus 9", "release_date": "2027-03-01"}}},
        "openai": {"models": {"gpt-9": {"name": "GPT-9", "release_date": "2027-06-01"}}},
        "somewhere": {"models": {"x": {"name": "X"}}}}))
    rows = dict(M.grouped("opencode"))["Fetched by the agent on this computer"]
    assert [r[0] for r in rows] == ["anthropic/claude-opus-9", "openai/gpt-9"], "the table's own are not repeated"
    assert rows[0][1] == "Claude Opus 9 (2027-03-01) — anthropic/claude-opus-9"


def test_a_link_or_a_huge_file_is_not_read(home, tmp_path, monkeypatch):
    (home / ".codex").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"models": [{"slug": "gpt-x"}]}))
    os.symlink(target, home / ".codex" / "models_cache.json")
    assert "gpt-x" not in dict(M.choices("codex"))
    monkeypatch.setattr(M, "LIMIT", 10)
    assert M.read_json(target) is None


def test_the_combo_shows_names_groups_and_gives_the_id(qapp):
    from PySide6.QtWidgets import QComboBox
    combo = QComboBox()
    combo.setEditable(True)
    M.fill_combo(combo, "claude", "claude-opus-5-5")
    assert combo.currentText() == "Claude Opus 5.5 — claude-opus-5-5" and M.combo_value(combo) == "claude-opus-5-5"
    heading = combo.findData(M.HEADER + "Anthropic")
    assert heading > 0 and not combo.model().item(heading).isEnabled()
    combo.setCurrentIndex(heading)  # (a heading is never a model)
    assert M.combo_value(combo) == ""
    M.fill_combo(combo, "claude", "my-own-model")
    assert M.combo_value(combo) == "my-own-model", "a model not listed stays as typed"
    M.fill_combo(combo, "claude", "")
    assert M.combo_value(combo) == ""
    combo.setEditText("Claude Sonnet 5.5 — claude-sonnet-5-5 ")
    assert M.combo_value(combo) == "claude-sonnet-5-5"
