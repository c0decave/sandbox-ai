"""The fonts that come with the app (reading_fonts.py, the folder typefaces/): every file pinned in fonts.json with its
SHA-256 and its licence beside it, part of the package, loaded only when unchanged; the Help set in Atkinson
Hyperlegible Next, and the setting reading_font (Settings > Appearance) putting a reading font on all of the app's
text at once - terminals and code keep the monospace font - with the theme's font when a file is missing."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, fonts, help_texts, i18n, reading_fonts as R, theme, widgets  # noqa: E402

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parents[1]


def test_every_font_file_is_pinned_unchanged_with_its_licence():
    data = R.manifest()
    files, sources = data["files"], data["sources"]
    present = {p.name for p in R.FOLDER.iterdir() if p.name != "fonts.json"}
    assert present == set(files), "every file in typefaces/ is listed, and only those"
    for name, entry in files.items():
        blob = (R.FOLDER / name).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"] and len(blob) == entry["size"], name
        assert entry["family"] in sources and entry["upstream"].startswith(sources[entry["family"]]["repository"])
        assert sources[entry["family"]]["commit"] in entry["upstream"], name
    pinned = datetime.date.fromisoformat(data["pinned"])
    for family, source in sources.items():
        assert source["license"] == "OFL-1.1" and re.fullmatch(r"[0-9a-f]{40}", source["commit"]), family
        assert (pinned - datetime.date.fromisoformat(source["commit_date"])).days >= 14, f"{family}: >= 14 days old"
        licence = (R.FOLDER / f"OFL-{family}.txt").read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in licence.upper().replace("\n", " ").replace("  ", " ") or \
            "Open Font License, Version 1.1" in licence, family
    for family, names in R.FILES.items():
        assert set(names) <= set(files), family
    assert set(R.LICENSES) <= present


def test_the_fonts_and_their_licences_are_package_data():
    for path in (APP / "pyproject.toml", ROOT / "pyproject.toml"):
        text = path.read_text()
        for pattern in ('"typefaces/*.ttf"', '"typefaces/*.otf"', '"typefaces/*.txt"', '"typefaces/fonts.json"'):
            assert pattern in text, (path, pattern)


def test_the_fonts_are_loaded_and_the_help_uses_them(qapp):
    found = R.register()
    assert set(found) == {R.HELP_FAMILY, R.HELP_MONO, "OpenDyslexic", "Lexend"}
    from PySide6.QtGui import QFontDatabase
    families = set(QFontDatabase.families())
    assert {R.HELP_FAMILY, R.HELP_MONO, "OpenDyslexic", "Lexend"} <= families
    assert R.text_families(["DejaVu Sans"], {}) == [R.HELP_FAMILY, "DejaVu Sans"]
    assert R.text_families(["DejaVu Sans"], {"reading_font": "lexend"}) == ["Lexend", "DejaVu Sans"]
    assert R.mono_families(["monospace"]) == [R.HELP_MONO, "monospace"]


def test_a_missing_or_changed_file_is_not_loaded_and_the_theme_font_stays(qapp, tmp_path):
    folder = tmp_path / "typefaces"
    shutil.copytree(R.FOLDER, folder)
    (folder / "Lexend-Regular.ttf").unlink()
    (folder / "Lexend-Bold.ttf").unlink()
    changed = folder / "OpenDyslexic-Regular.otf"
    changed.write_bytes(changed.read_bytes()[:-1] + b"\0")
    (folder / "OpenDyslexic-Bold.otf").unlink()
    (folder / "AtkinsonHyperlegibleNext-Regular.ttf").unlink()
    (folder / "AtkinsonHyperlegibleNext-Bold.ttf").unlink()
    found = R.register(folder)
    assert set(found) == {R.HELP_MONO}
    assert R.family_of("lexend", folder) is None and R.family_of("opendyslexic", folder) is None
    assert R.text_families(["DejaVu Sans"], {"reading_font": "lexend"}, folder) == ["DejaVu Sans"]
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "fonts.json").write_text("{not json")
    assert R.register(broken) == {} and R.manifest(broken) == {}


def test_the_reading_font_is_a_setting_in_both_languages():
    assert backend.SETTINGS["reading_font"] == "" and R.VALUES == ("", "atkinson", "opendyslexic", "lexend")
    assert "reading_font" in help_texts.SETTING_TIPS and help_texts.SETTING_TIPS["reading_font"].de
    for _value, _family, label, note in R.READING:
        for text in (label, note, "Reading font"):
            assert i18n.TEXTS.get(text, {}).get("de"), text
    assert R.chosen({"reading_font": "comic"}) == "" and R.chosen({"reading_font": 3}) == ""


@pytest.fixture
def settings_env(tmp_path, monkeypatch, qapp):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    theme.apply("system", qapp)  # as main.py does before any window (theme.apply_font needs it)
    yield tmp_path
    fonts.use({})
    theme.apply("system")


def test_settings_switch_the_reading_font_at_once_and_back(settings_env, qapp):
    """Appearance > Reading font: the theme's by default; OpenDyslexic is the app's font at once (the monospace
    font stays), its note below the box; back to the theme's font, the app's font is what it was."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPlainTextEdit
    from sandbox_ai_app.settings import SettingsDialog
    R.register()
    sai = backend.SandboxAI()
    dialog = SettingsDialog(sai)
    log = QPlainTextEdit()
    log.setFont(widgets.MONO)
    try:
        box = dialog.fields["reading_font"]
        assert box.currentData() == "" and [box.itemData(i) for i in range(box.count())] == list(R.VALUES)
        assert "dyslexia" in box.itemData(box.findData("opendyslexic"), Qt.ItemDataRole.ToolTipRole)
        before = qapp.font().families()
        mono = widgets.MONO.families()
        box.setCurrentIndex(box.findData("opendyslexic"))
        qapp.processEvents()
        assert sai.settings()["reading_font"] == "opendyslexic"
        assert qapp.font().families()[0] == "OpenDyslexic", "the whole app at once, no restart"
        assert widgets.MONO.families() == mono and log.font().families() == mono, "code keeps its font"
        assert not dialog.font_hints["reading"].isHidden() and "dyslexia" in dialog.font_hints["reading"].text()
        box.setCurrentIndex(box.findData("atkinson"))
        assert qapp.font().families()[0] == R.HELP_FAMILY
        box.setCurrentIndex(0)
        qapp.processEvents()
        assert qapp.font().families() == before and dialog.font_hints["reading"].isHidden()
    finally:
        dialog.deleteLater()


def test_a_chosen_reading_font_wins_over_the_interface_font(qapp, monkeypatch):
    monkeypatch.setattr(fonts, "_families", ["DejaVu Sans"])
    R.register()
    try:
        assert fonts.use({"ui_font": "DejaVu Sans", "reading_font": "lexend"})["ui"] == "Lexend"
        assert fonts.use({"ui_font": "DejaVu Sans", "reading_font": ""})["ui"] == "DejaVu Sans"
        assert fonts.use({"ui_font": "DejaVu Sans", "reading_font": "no-such"})["ui"] == "DejaVu Sans"
    finally:
        monkeypatch.setattr(fonts, "_families", None)
        fonts.use({})
        theme.apply("system")


def test_the_web_folder_has_the_same_fonts_for_its_help():
    web = ROOT / "sandbox" / "web" / "fonts"
    data = json.loads((web / "fonts.json").read_text())
    assert data["sources"] == R.manifest()["sources"], "the same families at the same commits"
    for name, entry in data["files"].items():
        assert hashlib.sha256((web / name).read_bytes()).hexdigest() == entry["sha256"], name
    assert {p.name for p in web.iterdir() if p.name != "fonts.json"} == set(data["files"])
    assert {f"OFL-{family}.txt" for family in data["sources"]} <= set(data["files"])
