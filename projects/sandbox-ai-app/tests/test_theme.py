"""Every theme: readable colours (WCAG 2.1 contrast), a palette, a stylesheet Qt accepts, the board and the
terminal follow it."""
import json

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6 import QtGui  # noqa: E402
from sandbox_ai_app import i18n, theme  # noqa: E402

NAMES = list(theme.THEMES_DATA)
# Well-known editor themes, in their upstream palettes; the parametrised tests below check them like the others.
EDITOR_THEMES = {"solarized_dark": "#002b36", "solarized_light": "#fdf6e3", "dracula": "#282a36", "nord": "#2e3440",
                 "gruvbox_dark": "#282828", "one_dark": "#282c34", "catppuccin_mocha": "#1e1e2e",
                 "catppuccin_latte": "#eff1f5", "tokyo_night": "#1a1b26"}
TEXT = 4.5   # normal text
LARGE = 3.0  # borders of controls, the canvas grid


def pairs(t):
    c, s = t["colors"], t["status"]
    texts = [("text", c["text"], bg) for bg in ("window", "base", "alternate", "button", "tooltip")]
    texts += [("secondary", c["secondary"], bg) for bg in ("window", "base")]
    texts += [("placeholder", c["placeholder"], "base"), ("title", c["title"], "window"),
              ("danger", c["danger"], "button"), ("danger", c["danger"], "base"),
              ("accent_text", c["accent_text"], "accent"), ("highlight_text", c["highlight_text"], "highlight"),
              ("nav_text", c["nav_text"], "nav"), ("nav_selected_text", c["nav_selected_text"], "nav_selected"),
              ("text", c["text"], "node"), ("node_title", c.get("node_title", c["text"]), "node_header")]
    texts += [(f"status {k}", v, bg) for k, v in s.items() for bg in ("base", "window")]
    for name, fg, bg in texts:
        yield name, fg, c[bg], bg, TEXT
    for name, fg, bg in (("border", c["border"], "window"), ("border", c["border"], "base"),
                         ("grid", c["grid"], "canvas"), ("accent", c["accent"], "base")):
        yield name, fg, c[bg], bg, LARGE


@pytest.mark.parametrize("name", NAMES)
def test_every_theme_is_readable(name):
    t = theme.THEMES_DATA[name]
    low = [f"{what} {fg} on {where} {bg}: {theme.contrast(fg, bg):.2f} < {need}"
           for what, fg, bg, where, need in pairs(t) if theme.contrast(fg, bg) < need]
    assert not low, "\n".join(low)


@pytest.mark.parametrize("name", NAMES)
def test_every_theme_has_every_token(name):
    t = theme.THEMES_DATA[name]
    assert set(theme.THEMES_DATA["light"]["colors"]) <= set(t["colors"])
    assert set(t["status"]) == {"ok", "warn", "bad", "muted"}
    assert set(t["terminal"]) >= {"background", "foreground", "cursor", "selectionBackground"}
    assert isinstance(t["dark"], bool) and t["radius"] >= 0
    assert t["dark"] == (theme.luminance(t["colors"]["window"]) < 0.18)


def test_the_choices_name_every_theme_once():
    keys = [key for key, _ in theme.choices()]
    assert keys == list(theme.THEMES) and len(set(keys)) == len(keys) == 20
    assert {"light", "dark", "hacker", "hacker_neon", "cyberpunk", "amber", "unicorn", "win311", "mac", "fallout"} <= set(keys)
    assert set(EDITOR_THEMES) <= set(keys)


@pytest.mark.parametrize("name", sorted(EDITOR_THEMES))
def test_the_editor_themes_keep_their_upstream_background(name):
    """The editor's own background is the theme's base and the terminal's background; the parametrised tests
    above check every colour key and the contrast of each, the status colours included."""
    t = theme.THEMES_DATA[name]
    assert t["colors"]["base"] == t["terminal"]["background"] == EDITOR_THEMES[name]
    assert set(theme.THEMES_DATA["light"]["colors"]) <= set(t["colors"]) and t["font"]["families"]


def test_the_chosen_fonts_go_before_the_themes(qapp):
    """fonts.use() names installed fonts only; the theme puts them first: the app's font (apply()), the title's and
    the terminal's fontFamily (xterm.js), the theme's families after them as the fallback."""
    try:
        theme.set_fonts(ui="IBM Plex Sans", mono="Hack")
        theme.apply("dracula")
        assert qapp.font().families()[:2] == ["IBM Plex Sans", theme.SANS[0]]
        assert theme.title_font().families()[0] == "IBM Plex Sans"
        family = json.loads(theme.terminal_theme())["fontFamily"]
        assert family.startswith("Hack, ") and '"JetBrains Mono"' in family
        theme.apply("fallout")  # its own monospace fonts follow the chosen one
        assert json.loads(theme.terminal_theme())["fontFamily"].startswith('Hack, "Share Tech Mono"')
        theme.set_fonts()
        theme.apply("dracula")
        assert qapp.font().families()[0] == theme.SANS[0]
        assert json.loads(theme.terminal_theme())["fontFamily"].startswith('"JetBrains Mono"')
    finally:
        theme.set_fonts()
        theme.apply("system")


@pytest.mark.parametrize("name", NAMES)
def test_applied_with_palette_font_and_a_valid_stylesheet(qapp, name, capfd):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QLabel, QPushButton
    try:
        assert theme.apply(name) == name and theme.current() == name
        c = theme.THEMES_DATA[name]["colors"]
        assert qapp.palette().color(QPalette.ColorRole.Window).name() == c["window"]
        assert theme.is_dark() == theme.THEMES_DATA[name]["dark"]
        families = qapp.font().families()
        assert families[:2] == theme.THEMES_DATA[name]["font"]["families"][:2]
        # Qt parses the stylesheet when a widget is polished; a syntax error is reported on stderr.
        button, label = QPushButton("x"), QLabel("y")
        button.setProperty("role", "primary")
        label.setProperty("role", "title")
        for widget in (button, label):
            widget.ensurePolished()
            widget.show()
        qapp.processEvents()
        assert "Could not parse" not in capfd.readouterr().err
        terminal = json.loads(theme.terminal_theme())
        assert terminal["background"] == theme.THEMES_DATA[name]["terminal"]["background"]
        assert terminal["light"] is not theme.THEMES_DATA[name]["dark"] and terminal["fontFamily"]
        assert not theme.glyph("board").isNull() and not theme.dot("ok").isNull()
    finally:
        theme.apply("system")


def test_system_theme_restores_the_start(qapp):
    from PySide6.QtGui import QPalette
    theme.apply("system")
    before = (qapp.palette().color(QPalette.ColorRole.Window).name(), qapp.font().family())
    theme.apply("hacker")
    assert qapp.font().family() != before[1] or "Mono" in before[1]
    theme.apply("system")
    assert (qapp.palette().color(QPalette.ColorRole.Window).name(), qapp.font().family()) == before
    assert theme.apply("no-such-theme") == "system"


def test_every_navigation_page_has_a_glyph():
    from sandbox_ai_app.ui import PAGES
    assert {key for key, _ in PAGES} <= set(theme.GLYPHS)


def test_every_project_trust_state_has_a_glyph_in_a_status_colour(qapp):
    from sandbox_ai_app.ui import PROJECT_STATES
    assert set(theme.TRUST_GLYPHS) == set(PROJECT_STATES)
    for state, (name, status) in theme.TRUST_GLYPHS.items():
        assert name in theme.GLYPHS and status in theme.data()["status"], state
        assert not theme.trust_glyph(state).isNull()


def test_the_web_interface_has_the_same_themes():
    """sandbox-ai's web interface reads its themes from sandbox/web/themes.json: the app's THEMES_DATA, exported.
    After a change here: export it again (see that file's comment)."""
    import json
    import os
    from pathlib import Path
    home = os.environ.get("SANDBOX_AI_HOME")
    # SANDBOX_AI_HOME names sandbox-ai's folder: its checkout (sandbox/web) or an installed tree (web)
    path = next((p for p in (Path(home) / "sandbox" / "web" / "themes.json", Path(home) / "web" / "themes.json")
                 if p.is_file()), None) if home else None
    if path is None:
        pytest.skip("SANDBOX_AI_HOME names no sandbox-ai with a web interface")
    exported = json.loads(path.read_text())
    assert exported["themes"] == json.loads(json.dumps(theme.THEMES_DATA))
    from sandbox_ai_app import fonts  # the font lists of the settings, with how to get each
    assert exported["fonts"] == {kind: [list(entry) for entry in fonts.KINDS[kind]] for kind in ("ui", "mono")}


@pytest.mark.parametrize("name", ["system", *NAMES])
def test_a_line_between_the_column_titles_shows_where_to_grab_them(qapp, name, monkeypatch):
    """Every look draws an edge between the titles of a table's columns (where one is made wider or moved): a line in
    its border colour after its own rules for the titles, or (a bevelled look) the raised titles' own edges."""
    monkeypatch.setattr(theme, "_current", name)
    sheet, data = theme.stylesheet(), theme.data()
    rule = "QHeaderView::section:horizontal { border-right: 1px solid " + data["colors"]["border"] + "; }"
    if data.get("bevel"):
        assert rule not in sheet and "QHeaderView::section { border-top: 2px solid" in sheet
    else:
        assert rule in sheet and sheet.rindex(rule) > sheet.rindex("QHeaderView::section {")


FOCUSABLE = ["button", "primary", "tool", "check", "radio", "tabs", "nav"]


def focus_target(kind):
    """(the widget to show, the one that takes the focus) of KIND."""
    from PySide6.QtWidgets import QCheckBox, QListWidget, QPushButton, QRadioButton, QTabWidget, QToolButton, QWidget
    if kind in ("button", "primary"):
        widget = QPushButton("Start")
        if kind == "primary":
            widget.setProperty("role", "primary")
        return widget, widget
    if kind == "tool":
        widget = QToolButton()
        widget.setText("Arrange")
        widget.setFocusPolicy(widget.focusPolicy().StrongFocus)
        return widget, widget
    if kind in ("check", "radio"):
        widget = (QCheckBox if kind == "check" else QRadioButton)("follow")
        return widget, widget
    if kind == "tabs":
        widget = QTabWidget()
        widget.addTab(QWidget(), "Blocked")
        widget.addTab(QWidget(), "History")
        return widget, widget.tabBar()
    widget = QListWidget()
    widget.setObjectName("nav")
    widget.addItems(["Start", "Board"])
    widget.setCurrentRow(0)
    return widget, widget


# The looks without costume (light, dark, Mac and the editor themes); the costumed ones (hacker, amber, Fallout,
# cyberpunk, unicorn, Windows 3.11) draw their own focus marks (docs/design-audit-0.1.95.md, open findings).
PLAIN_LOOKS = [name for name in NAMES if theme.THEMES_DATA[name].get("extra") in (None, "mac")]


@pytest.mark.parametrize("name", PLAIN_LOOKS)
@pytest.mark.parametrize("kind", FOCUSABLE)
def test_the_keyboard_focus_is_visible_on_every_control(qapp, name, kind):
    """Design review 0.1.95 (hallmark's floor: every interactive element shows its focus): reached with Tab, a tool
    button, a primary button, the navigation and a tab bar looked exactly as before; a check box showed a faint
    dotted frame. Now each changes at least a ring's worth of pixels, each by a contrast of 3:1 or more (WCAG 2.2's
    focus appearance)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
    theme.apply(name)
    holder = QWidget()
    try:
        layout = QVBoxLayout(holder)
        before_it = QLineEdit()
        layout.addWidget(before_it)
        shown, target = focus_target(kind)
        layout.addWidget(shown)
        holder.resize(260, 160)
        holder.show()
        holder.activateWindow()
        before_it.setFocus()
        qapp.processEvents()
        resting = target.grab().toImage()
        QTest.keyClick(before_it, Qt.Key.Key_Tab)
        qapp.processEvents()
        assert target.hasFocus()
        focused = target.grab().toImage()
        assert focused.size() == resting.size()
        strong = sum(1 for x in range(focused.width()) for y in range(focused.height())
                     if focused.pixel(x, y) != resting.pixel(x, y)
                     and theme.contrast(focused.pixelColor(x, y).name(), resting.pixelColor(x, y).name()) >= 3)
        ratio = focused.devicePixelRatio()
        if kind in ("check", "radio"):  # around the box or the dot (14 px)
            ring = (4 if kind == "check" else 3.14) * 12 * ratio  # the ring's inner edge
        elif kind == "tabs":  # around the current tab, its lower edge open to the page
            tab = target.tabRect(target.currentIndex())
            ring = (tab.width() + 2 * tab.height()) * ratio
        else:
            ring = 2 * (target.width() + min(target.height(), 30)) * ratio
        assert strong >= ring * 0.8, f"{kind} in {name}: {strong} px changed clearly, a ring is {ring:.0f}"
    finally:
        holder.close()
        theme.apply("system")


def test_a_second_hacker_look_has_turquoise_orange_yellow_and_two_greens():
    """User 2026-11-17: "für den hacker mode noch einen zweiten theme mode, da sollen aber farben wie z.b. türkis,
    orange, gelb und helles und dunkles grün ebenfalls vorhanden sein" - the same terminal costume as hacker, on
    black, its marks in those colours (each readable: test_every_theme_is_readable checks it like every theme)."""
    neon = theme.THEMES_DATA["hacker_neon"]
    assert neon["dark"] and neon["extra"] == "hacker" and neon["font"] == theme.THEMES_DATA["hacker"]["font"]

    def hue(color):
        return QtGui.QColor(color).hslHue()

    used = {*neon["colors"].values(), *neon["status"].values(), *neon["terminal"].values()}
    turquoise = [c for c in used if 165 <= hue(c) <= 190 and QtGui.QColor(c).saturation() > 150]
    orange = [c for c in used if 25 <= hue(c) <= 40 and QtGui.QColor(c).saturation() > 150]
    yellow = [c for c in used if 48 <= hue(c) <= 62 and QtGui.QColor(c).saturation() > 150]
    greens = [QtGui.QColor(c).lightness() for c in used if 95 <= hue(c) <= 150 and QtGui.QColor(c).saturation() > 100]
    assert turquoise and orange and yellow, (turquoise, orange, yellow)
    assert max(greens) >= 150 and min(greens) <= 70, greens  # a light and a dark green
    assert neon["colors"]["accent"] in turquoise and neon["status"]["warn"] in orange
    assert neon["colors"]["title"] in yellow
    assert i18n.tr(neon["label"]) and neon["label"] != theme.THEMES_DATA["hacker"]["label"]
