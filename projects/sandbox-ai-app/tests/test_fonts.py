"""The fonts of the settings: installed ones from the font database (a list stands in for it here), the others
marked not installed with how to get them, the chosen ones on the app's widgets, its monospace font (MONO) and the
terminal's fontFamily, and the theme's or the system's font when the chosen one is missing."""
import json

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import fonts, theme, widgets  # noqa: E402


@pytest.fixture
def installed(qapp, monkeypatch):
    """Set the installed families; afterwards the app's own fonts and the system theme again."""
    def use(families):
        monkeypatch.setattr(fonts, "_families", list(families))
    yield use
    monkeypatch.setattr(fonts, "_families", None)
    fonts.use({})
    theme.apply("system")


def test_the_lists_offer_well_liked_fonts_with_how_to_get_each():
    mono = [family for family, _ in fonts.MONO_FONTS]
    ui = [family for family, _ in fonts.UI_FONTS]
    assert {"JetBrains Mono", "Fira Code", "Cascadia Code", "Source Code Pro", "Hack", "IBM Plex Mono", "Iosevka",
            "Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono"} <= set(mono)
    assert {"Inter", "IBM Plex Sans", "Noto Sans", "Cantarell"} <= set(ui)
    assert len(set(mono)) == len(mono) and len(set(ui)) == len(ui)
    for family, how in fonts.MONO_FONTS + fonts.UI_FONTS:
        assert how.strip() and "http" not in how, family  # text to read, never a link the app would open
    assert "fonts-jetbrains-mono" in fonts.how_to_get("mono", "JetBrains Mono") and fonts.how_to_get("mono", "x") == ""


def test_installed_ones_are_found_the_others_marked(installed):
    installed(["DejaVu Sans Mono", "hack", "Inter", "Some Other"])
    marks = {family: have for family, have, _ in fonts.choices("mono")}
    assert marks["DejaVu Sans Mono"] and marks["Hack"] and not marks["JetBrains Mono"]
    assert [family for family, have, _ in fonts.choices("ui") if have] == ["Inter"]
    assert fonts.pick("mono", "Hack") == "hack", "as the font database spells it"
    assert fonts.pick("mono", "JetBrains Mono") is None, "not installed"
    assert fonts.pick("mono", "Some Other") is None and fonts.pick("ui", "Hack") is None, "not one of the list"
    assert fonts.pick("mono", "") is None and fonts.pick("mono", None) is None


def test_the_real_font_database_is_asked(qapp):
    from PySide6.QtGui import QFontDatabase
    assert fonts.installed() == list(QFontDatabase.families())


def test_chosen_fonts_reach_the_widgets_mono_and_the_terminal(installed, qapp):
    from PySide6.QtWidgets import QLabel, QPlainTextEdit
    installed(["Hack", "IBM Plex Sans", "DejaVu Sans"])
    log, label = QPlainTextEdit(), QLabel("x")
    log.setFont(widgets.MONO)  # a view made before the choice
    chosen = fonts.use({"ui_font": "IBM Plex Sans", "mono_font": "Hack"})
    assert chosen == {"ui": "IBM Plex Sans", "mono": "Hack"}
    assert widgets.MONO.families() == ["Hack", "monospace"]
    assert widgets.MONO.styleHint() == widgets.QFont.StyleHint.Monospace
    assert log.font().families() == ["Hack", "monospace"], "views showing MONO follow"
    assert label.font().families()[:1] != ["Hack"], "the others keep the app's font"
    theme.apply("nord")
    assert qapp.font().families()[:2] == ["IBM Plex Sans", theme.SANS[0]]
    assert json.loads(theme.terminal_theme())["fontFamily"].startswith("Hack, ")
    theme.apply("system")
    assert qapp.font().families()[0] == "IBM Plex Sans", "also on the system's look"


def test_a_missing_font_falls_back_to_the_themes(installed, qapp):
    from PySide6.QtWidgets import QPlainTextEdit
    installed(["Hack"])
    fonts.use({"mono_font": "Hack"})
    log = QPlainTextEdit()
    log.setFont(widgets.MONO)
    installed([])  # uninstalled since
    chosen = fonts.use({"ui_font": "Inter", "mono_font": "Hack"})
    assert chosen == {"ui": None, "mono": None}
    assert widgets.MONO.families() == ["monospace"] and log.font().families() == ["monospace"]
    theme.apply("dark")
    assert qapp.font().families() == theme.THEMES_DATA["dark"]["font"]["families"]
    assert json.loads(theme.terminal_theme())["fontFamily"].startswith('"JetBrains Mono"'), "the theme's list"
    fonts.use({"ui_font": 7, "mono_font": ["x"]})  # a damaged settings file: the defaults
    assert theme.chosen_fonts() == {"ui": None, "mono": None}
