"""Every theme: readable colours (WCAG 2.1 contrast), a palette, a stylesheet Qt accepts, the board and the
terminal follow it."""
import json

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import theme  # noqa: E402

NAMES = list(theme.THEMES_DATA)
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
    assert keys == list(theme.THEMES) and len(set(keys)) == len(keys) == 10
    assert {"light", "dark", "hacker", "cyberpunk", "amber", "unicorn", "win311", "mac", "fallout"} <= set(keys)


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


def test_the_web_interface_has_the_same_themes():
    """sandbox-ai's web interface reads its themes from sandbox/web/themes.json: the app's THEMES_DATA, exported.
    After a change here: export it again (see that file's comment)."""
    import json
    import os
    from pathlib import Path
    home = os.environ.get("SANDBOX_AI_HOME")
    path = Path(home) / "web" / "themes.json" if home else None
    if path is None or not path.is_file():
        pytest.skip("SANDBOX_AI_HOME names no sandbox-ai with a web interface")
    assert json.loads(path.read_text())["themes"] == json.loads(json.dumps(theme.THEMES_DATA))
