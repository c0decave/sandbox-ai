"""A built-in preset's last live result in the app (the user, 2026-11-22: "mir scheint das alle presets auch noch
nicht getestet sind"): sandbox-ai's live_evidence.json (scripts/import_live_report.py from a live check's report)
says per preset and backend what passed or failed; the gallery shows it on the preset's card, in its details and in
the preset choice - ✓ with the date and version when it passed, ✗ with the reason when it failed, nothing without
data (no negative word for no data)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, i18n, presets as PR, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ENTRIES = [{"kind": "preset", "name": "coding-claude", "backend": "podman", "agent": "claude", "result": "passed",
            "date": "2026-11-23", "version": "0.1.212", "report": "REPORT-20261123-091500", "detail": "33 s"},
           {"kind": "preset", "name": "development", "backend": "podman", "agent": "claude", "result": "passed",
            "date": "2026-11-23", "version": "0.1.212", "report": "REPORT-20261123-091500", "detail": "40 s"},
           {"kind": "preset", "name": "development", "backend": "bwrap", "agent": "claude", "result": "failed",
            "date": "2026-11-23", "version": "0.1.212", "report": "REPORT-20261123-091500",
            "detail": "bwrap: setting up uid map: Permission denied"}]


@pytest.fixture
def sai(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = backend.SandboxAI()
    module = sai.module("presets")
    if not hasattr(module, "live_light"):
        pytest.skip("this sandbox-ai has no live evidence yet")
    evidence = tmp_path / "live_evidence.json"
    evidence.write_text(json.dumps({"schema": 1, "made_by": "test", "entries": ENTRIES}))
    monkeypatch.setattr(module, "LIVE_EVIDENCE", evidence)
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [])
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [])
    return sai


def sai_preset(sai, name):
    return PR.catalogue(sai, mine=[]).find(f"sandbox-ai:{name}")


def test_a_preset_says_its_last_live_check_and_nothing_without_one(sai):
    green = sai_preset(sai, "coding-claude")
    assert green.live == {"light": "green", "date": "2026-11-23", "version": "0.1.212", "why": ""}
    assert PR.live_text(green) == "✓ Live check passed on 2026-11-23 (sandbox-ai 0.1.212)"
    red = sai_preset(sai, "development")
    assert red.live["light"] == "red"
    assert PR.live_text(red) == ("✗ Live check failed on 2026-11-23 (sandbox-ai 0.1.212): bwrap: bwrap: setting up "
                                 "uid map: Permission denied")
    none = sai_preset(sai, "pentest-lab")
    assert none.live == {} and PR.live_text(none) == "", "no data yet: nothing said"
    i18n.set_language("de")
    try:
        assert PR.live_text(green) == "✓ Live-Prüfung bestanden am 2026-11-23 (sandbox-ai 0.1.212)"
    finally:
        i18n.set_language("en")


def test_another_hosts_preset_brings_its_light_along(sai):
    document = {**sai.module("presets").public(sai.module("presets").catalog()["coding-claude"]),
                "live": {"light": "red", "date": "2026-11-20", "version": "0.1.209", "why": "podman: exit 3"}}
    preset = PR.from_sandbox_ai(sai, document, host="ct1")
    assert PR.live_text(preset) == "✗ Live check failed on 2026-11-20 (sandbox-ai 0.1.209): podman: exit 3"
    assert PR.from_sandbox_ai(sai, {**document, "live": None}, host="ct1").live == {}
    assert PR.from_sandbox_ai(sai, {**document, "live": {"light": "purple"}}, host="ct1").live == {}


def test_the_gallery_shows_it_on_the_card_in_the_details_and_in_the_choice(sai, tmp_path):
    from sandbox_ai_app.preset_gallery import PresetGallery, fill_preset_combo
    gallery = PresetGallery(sai, str(tmp_path / "project"), "claude", {}, None)
    try:
        cards = {card.preset.key: card for card in gallery.cards}
        assert "✓ live check" in cards["sandbox-ai:sai-coding-claude"].meta.text()
        assert "✗ live check" in cards["sandbox-ai:sai-development"].meta.text()
        assert "live check" not in cards["sandbox-ai:sai-pentest-lab"].meta.text()
        gallery.choose(cards["sandbox-ai:sai-development"])
        assert "✗ Live check failed on 2026-11-23" in gallery.meta.text()
        gallery.choose(cards["sandbox-ai:sai-pentest-lab"])
        assert "Live check" not in gallery.meta.text()
    finally:
        gallery.deleteLater()
    combo = QtWidgets.QComboBox()
    fill_preset_combo(combo, PR.catalogue(sai, mine=[]))
    index = combo.findData("sandbox-ai:sai-coding-claude")
    assert combo.itemText(index).endswith("  ✓")
    assert "Live check passed on 2026-11-23" in combo.itemData(index, 3)
    assert not combo.itemText(combo.findData("sandbox-ai:sai-pentest-lab")).endswith(("✓", "✗"))
