"""No "not live-tested yet" badge in the app any more (the user, 2026-11-22: "BITTE "noch nicht live getestet" überall
entfernen!"): every agent's presets are listed as before, the gallery's cards and the preset combo show their title
alone, the Board's agent choice and the wizard's list the agents by their names with nothing below the choice, and a
preset document of another host's older sandbox-ai that still carries "experimental" shows no badge either."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, i18n, presets as PR, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# The presets that carried the badge until 0.1.209: all still listed.
AGENT_PRESETS = {"coding-hermes", "hermes-claude", "hermes-kanban", "hermes-local-llm", "coding-qwen", "qwen-local-llm",
                 "coding-dsh", "dsh-local-llm", "coding-goose", "goose-local-llm", "coding-letta", "letta-local-llm",
                 "coding-openhands", "openhands-local-llm", "coding-nanobot", "nanobot-local-llm",
                 "cairn-pentest-board", "cairn-local-llm", "default-openhands", "default-nanobot", "default-letta",
                 "default-hermes", "default-qwen", "default-dsh", "default-goose", "default-opencode2",
                 "coding-opencode2", "opencode2-local-llm", "coding-pi", "pi-local-llm", "default-pi"}
WORDS = ("live-tested", "live getestet")


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return backend.SandboxAI(), tmp_path


def unmarked(text):
    return not any(word in str(text).lower() for word in WORDS)


def test_every_agents_presets_are_listed_without_a_badge(env):
    sai, _ = env
    presets = PR.sandbox_ai_presets(sai)[0]
    assert AGENT_PRESETS <= {preset.name for preset in presets}  # all listed, none hidden
    assert not any(hasattr(preset, "experimental") for preset in presets)
    assert not hasattr(PR, "experimental_text") and not hasattr(sai, "experimental_agent")


def test_another_hosts_older_badge_is_not_shown(env):
    """An older sandbox-ai on another host still says "experimental" in `presets list --json`: the app shows no
    badge for it, the preset is read as any other."""
    sai, root = env
    document = {"id": "coding-qwen", "title": "Coding with Qwen Code", "description": "d", "agents": ["qwen"],
                "experimental": {"names": ["qwen"]}}
    preset = PR.from_sandbox_ai(sai, document, host="ct1")
    assert preset.name == "coding-qwen" and preset.agents == ("qwen",) and not hasattr(preset, "experimental")
    from sandbox_ai_app.preset_gallery import PresetCard
    card = PresetCard(preset, PR.build(sai, preset, root / "project"), 0)
    try:
        assert unmarked(card.toolTip()) and card.accessibleName() == preset.text(preset.title)
    finally:
        card.deleteLater()


def test_the_gallery_shows_no_badge_on_the_card_or_in_the_combo(env):
    from sandbox_ai_app.preset_gallery import PresetCard, fill_preset_combo
    sai, root = env
    catalogue = PR.catalogue(sai)
    hermes = next(p for p in catalogue.presets if p.name == "coding-hermes")
    card = PresetCard(hermes, PR.build(sai, hermes, root / "project"), 0)
    try:
        assert not hasattr(card, "experimental")
        assert card.accessibleName() == hermes.text(hermes.title) and unmarked(card.toolTip())
        assert all(unmarked(label.text()) for label in card.findChildren(QtWidgets.QLabel))
    finally:
        card.deleteLater()
    combo = QtWidgets.QComboBox()
    fill_preset_combo(combo, catalogue)
    texts = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert texts[hermes.key] == "   " + hermes.text(hermes.title)
    assert all(unmarked(text) for text in texts.values())
    assert unmarked(combo.itemData(combo.findData(hermes.key), Qt.ItemDataRole.ToolTipRole))


def test_the_wizard_lists_the_agents_by_their_names(env):
    from sandbox_ai_app.wizard import SandboxWizard
    sai, _ = env
    wizard = SandboxWizard(sai, lambda: {"default_agent": "hermes"})
    try:
        page = wizard.agent_page
        texts = {page.agent.itemData(i): page.agent.itemText(i) for i in range(page.agent.count())}
        assert texts == {agent: agent for agent in sai.agents}
        assert page.chosen() == "hermes" and not hasattr(page, "experimental")
        assert all(unmarked(label.text()) for label in page.findChildren(QtWidgets.QLabel))
        page.agent.setCurrentIndex(page.agent.findData("claude"))
        assert page.chosen() == "claude"
    finally:
        wizard.deleteLater()


def test_the_boards_agent_choice_lists_them_by_their_names(env):
    from sandbox_ai_app.board_view import BoardPage
    sai, _ = env
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        page.inspect(page.board.first("agent"))
        combo = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if c.findData("qwen") >= 0)
        assert combo.itemText(combo.findData("qwen")) == "qwen"
        assert combo.itemData(combo.findData("qwen"), Qt.ItemDataRole.ToolTipRole) is None
        combo.setCurrentIndex(combo.findData("qwen"))
        assert page.board.first("agent").props["agent"] == "qwen"
        labels = page.inspector_holder.findChildren(QtWidgets.QLabel)
        assert all(unmarked(label.text()) for label in labels)
    finally:
        page.deleteLater()


def test_the_words_are_gone_from_the_translations():
    old = i18n.language()
    try:
        i18n.set_language("de")
        for key in ("Not live-tested yet", "not live-tested yet", "{agent} (not live-tested yet)",
                    "Not live-tested yet: {names}."):
            assert key not in i18n.TEXTS, key
    finally:
        i18n.set_language(old)
