"""Agents and presets that have not passed their live test yet stay visible in the app and start like any other,
marked "not live-tested yet" (en/de): the preset gallery's cards and the preset combo, the Board's agent choice and the
wizard's. A badge only (nothing to enable, nothing hidden). It is sandbox-ai's own (its launcher's EXPERIMENTAL_PARTS,
through `presets list --json` for another host): the app keeps no list of its own."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, i18n, presets as PR, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

EXPERIMENTAL = {"coding-hermes": "hermes", "hermes-claude": "hermes", "hermes-kanban": "hermes",
                "hermes-local-llm": "hermes", "coding-qwen": "qwen", "qwen-local-llm": "qwen", "coding-dsh": "dsh",
                "dsh-local-llm": "dsh", "coding-goose": "goose", "goose-local-llm": "goose",
                "cairn-pentest-board": "cairn", "default-hermes": "hermes", "default-qwen": "qwen",
                "default-dsh": "dsh", "default-goose": "goose"}


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


def test_the_catalogue_marks_exactly_these_presets(env):
    sai, _ = env
    presets = PR.sandbox_ai_presets(sai)[0]
    marked = {preset.name: preset.experimental["names"] for preset in presets if preset.experimental}
    assert marked == {name: [part] for name, part in EXPERIMENTAL.items()}  # all listed, none hidden
    goose = next(p for p in presets if p.name == "coding-goose")
    assert goose.experimental == {"names": ["goose"]}
    assert PR.experimental_text(goose.experimental) == "Not live-tested yet: goose."


def test_another_hosts_badge_comes_from_its_json_checked(env):
    sai, _ = env
    document = {"id": "coding-qwen", "title": "Coding with Qwen Code", "description": "d", "agents": ["qwen"],
                "experimental": {"names": ["qwen"]}}
    assert PR.from_sandbox_ai(sai, document, host="ct1").experimental == {"names": ["qwen"]}
    for forged in ({"names": ["Q W; rm"]}, {"names": "qwen"}, "qwen", {}):
        assert PR.from_sandbox_ai(sai, {**document, "experimental": forged}, host="ct1").experimental == {}
    assert PR.experimental_of({"experimental": {"names": ["Q W", "qwen", 3]}}) == {"names": ["qwen"]}


def test_the_gallery_shows_the_badge_on_the_card_and_in_the_combo(env):
    from sandbox_ai_app.preset_gallery import PresetCard, fill_preset_combo
    sai, root = env
    catalogue = PR.catalogue(sai)
    hermes = next(p for p in catalogue.presets if p.name == "coding-hermes")
    card = PresetCard(hermes, PR.build(sai, hermes, root / "project"), 0)
    try:
        assert card.experimental is not None and card.experimental.text() == "Not live-tested yet"
        assert "Not live-tested yet: hermes." in card.toolTip() and "SANDBOX_AI_EXPERIMENTAL" not in card.toolTip()
        assert card.accessibleName().endswith("(not live-tested yet)")
    finally:
        card.deleteLater()
    claude = next(p for p in catalogue.presets if p.name == "coding-claude")
    plain = PresetCard(claude, PR.build(sai, claude, root / "project"), 0)
    try:
        assert plain.experimental is None and "live-tested" not in plain.accessibleName()
    finally:
        plain.deleteLater()
    combo = QtWidgets.QComboBox()
    fill_preset_combo(combo, catalogue)
    texts = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert texts[hermes.key].endswith("(not live-tested yet)") and not texts[claude.key].endswith(")")
    assert "Not live-tested yet: hermes." in combo.itemData(combo.findData(hermes.key), Qt.ItemDataRole.ToolTipRole)


def test_the_wizard_marks_the_agents_and_keeps_the_name(env):
    from sandbox_ai_app.wizard import SandboxWizard
    sai, _ = env
    wizard = SandboxWizard(sai, lambda: {"default_agent": "hermes"})
    try:
        page = wizard.agent_page
        texts = {page.agent.itemData(i): page.agent.itemText(i) for i in range(page.agent.count())}
        assert texts["hermes"] == "hermes (not live-tested yet)" and texts["goose"] == "goose (not live-tested yet)"
        assert texts["claude"] == "claude" and texts["codex"] == "codex"
        assert page.chosen() == "hermes" and not page.experimental.isHidden()
        assert page.experimental.text() == "Not live-tested yet: hermes."
        page.agent.setCurrentText("claude")  # (an agent with no badge: its text is its name)
        assert page.chosen() == "claude" and page.experimental.isHidden()
    finally:
        wizard.deleteLater()


def test_the_boards_agent_choice_marks_them_too(env):
    from sandbox_ai_app.board_view import BoardPage
    sai, _ = env
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        page.inspect(page.board.first("agent"))
        combo = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if c.findData("qwen") >= 0)
        assert combo.itemText(combo.findData("qwen")) == "qwen (not live-tested yet)"
        combo.setCurrentIndex(combo.findData("qwen"))
        assert page.board.first("agent").props["agent"] == "qwen"  # the name, never the badge
    finally:
        page.deleteLater()


def test_the_badge_speaks_german(env):
    old = i18n.language()
    try:
        i18n.set_language("de")
        assert PR.experimental_text({"names": ["dsh"]}) == "Noch nicht live getestet: dsh."
        assert i18n.tr("{agent} (not live-tested yet)", agent="dsh") == "dsh (noch nicht live getestet)"
        assert i18n.tr("Not live-tested yet") == "Noch nicht live getestet"
    finally:
        i18n.set_language(old)
