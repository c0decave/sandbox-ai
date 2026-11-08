"""The app's looks: the system's own, and nine of the app's (light, dark, hacker, cyberpunk, amber, unicorn,
Windows 3.11, Mac, Fallout). Each is a set of tokens - colours, font, corner radius, bevelled or flat
controls, the board's canvas and the terminal's colours - from which a QPalette (with the Fusion style,
which follows it), an application stylesheet and the app's own icons are made (painted: no files, no
network).

Text colours are chosen for a contrast of at least 4.5:1 against every background they are drawn on, borders
of controls and the canvas grid at least 3:1 (WCAG 2.1); tests/test_theme.py checks every theme."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .i18n import N_

_system = None  # (style name, palette, font) the application started with
_current = "system"

MONO = ["JetBrains Mono", "Fira Code", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace"]
SANS = ["Inter", "Cantarell", "Noto Sans", "Segoe UI", "Ubuntu", "DejaVu Sans", "sans-serif"]

# Every theme: label, dark, colours, status colours, font (families, size in pt, or None: the system's),
# radius (px), bevel (Windows 3.11 style 3D controls), canvas (board) and terminal colours.
# Colour tokens:
#   window, base (fields, tables, cards), alternate (every other row), text, secondary (hints), placeholder,
#   button, border, highlight + highlight_text (selection), accent + accent_text (primary buttons, focus),
#   danger, tooltip, disabled, nav + nav_text (the navigation), nav_selected + nav_selected_text,
#   title (page titles), canvas, grid (the board), node (a board node), node_header (its title band) and,
#   optionally, node_title (the title's colour there; default: text).
THEMES_DATA = {
    "light": {
        "label": N_("light"), "dark": False,
        "colors": {"window": "#f5f6f8", "base": "#ffffff", "alternate": "#f7f8fa", "text": "#1d1f23",
                   "secondary": "#575c66", "placeholder": "#6b707a", "button": "#ffffff", "border": "#828893",
                   "highlight": "#2563eb", "highlight_text": "#ffffff", "accent": "#2563eb", "accent_text": "#ffffff",
                   "danger": "#b42318", "tooltip": "#ffffff", "disabled": "#8a8f98", "nav": "#eceef2",
                   "nav_text": "#2b2f36", "nav_selected": "#dbe6ff", "nav_selected_text": "#1d3f99",
                   "title": "#111318", "canvas": "#f0f2f5", "grid": "#818791", "node": "#ffffff",
                   "node_header": "#e8eefc"},
        "status": {"ok": "#16692b", "warn": "#8a5300", "bad": "#b42318", "muted": "#575c66"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#ffffff", "foreground": "#1d1f23", "cursor": "#2563eb",
                     "selectionBackground": "#c7d7fe"},
    },
    "dark": {
        "label": N_("dark"), "dark": True,
        "colors": {"window": "#17191d", "base": "#1f2228", "alternate": "#23272e", "text": "#e6e8eb",
                   "secondary": "#a9afb9", "placeholder": "#959ba6", "button": "#262a31", "border": "#6b727e",
                   "highlight": "#3a63c8", "highlight_text": "#ffffff", "accent": "#7aa2ff", "accent_text": "#0b1020",
                   "danger": "#ff8a80", "tooltip": "#262a31", "disabled": "#80868f", "nav": "#121418",
                   "nav_text": "#c9cdd4", "nav_selected": "#26324d", "nav_selected_text": "#dbe5ff",
                   "title": "#f3f4f6", "canvas": "#14161a", "grid": "#626975", "node": "#1f2228",
                   "node_header": "#28334a"},
        "status": {"ok": "#7ddc8f", "warn": "#ffc46b", "bad": "#ff8a80", "muted": "#a9afb9"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#14161a", "foreground": "#e6e8eb", "cursor": "#7aa2ff",
                     "selectionBackground": "#2c4a8a"},
    },
    "hacker": {
        "label": N_("hacker"), "dark": True,
        "colors": {"window": "#000000", "base": "#050a05", "alternate": "#081108", "text": "#33ff66",
                   "secondary": "#22c24d", "placeholder": "#1fae45", "button": "#031003", "border": "#1f9e40",
                   "highlight": "#33ff66", "highlight_text": "#000000", "accent": "#33ff66", "accent_text": "#000000",
                   "danger": "#ff5555", "tooltip": "#031003", "disabled": "#1b7a33", "nav": "#000000",
                   "nav_text": "#33ff66", "nav_selected": "#0b3d18", "nav_selected_text": "#9dffb5",
                   "title": "#66ff8c", "canvas": "#000000", "grid": "#178a35", "node": "#030c03",
                   "node_header": "#0b3d18"},
        "status": {"ok": "#33ff66", "warn": "#e6ff33", "bad": "#ff5555", "muted": "#22c24d"},
        "font": {"families": MONO, "size": 10}, "radius": 0, "bevel": False, "extra": "hacker",
        "terminal": {"background": "#000000", "foreground": "#33ff66", "cursor": "#33ff66",
                     "selectionBackground": "#0b5a22"},
    },
    "cyberpunk": {
        "label": N_("cyberpunk"), "dark": True,
        "colors": {"window": "#0d0221", "base": "#150533", "alternate": "#1b0840", "text": "#f2e9ff",
                   "secondary": "#05d9e8", "placeholder": "#9a86c9", "button": "#1e0a4a", "border": "#c03fd0",
                   "highlight": "#ff2a6d", "highlight_text": "#0d0221", "accent": "#ff2a6d", "accent_text": "#0d0221",
                   "danger": "#ff6b8b", "tooltip": "#1e0a4a", "disabled": "#7e6aa8", "nav": "#08011a",
                   "nav_text": "#05d9e8", "nav_selected": "#ff2a6d", "nav_selected_text": "#0d0221",
                   "title": "#f9f002", "canvas": "#0a011c", "grid": "#8d36b9", "node": "#170639",
                   "node_header": "#3a0f66"},
        "status": {"ok": "#3dffb0", "warn": "#f9f002", "bad": "#ff6b8b", "muted": "#b8a6e6"},
        "font": {"families": ["Rajdhani", "Orbitron", "Exo 2", *SANS], "size": 10}, "radius": 2, "bevel": False, "extra": "cyberpunk",
        "terminal": {"background": "#0a011c", "foreground": "#f2e9ff", "cursor": "#ff2a6d",
                     "selectionBackground": "#5b1a7a"},
    },
    "amber": {
        "label": N_("amber"), "dark": True,
        "colors": {"window": "#0f0900", "base": "#160d00", "alternate": "#1d1200", "text": "#ffb000",
                   "secondary": "#d99400", "placeholder": "#c48600", "button": "#1d1200", "border": "#a86f00",
                   "highlight": "#ffb000", "highlight_text": "#120a00", "accent": "#ffb000", "accent_text": "#120a00",
                   "danger": "#ff7043", "tooltip": "#1d1200", "disabled": "#8a5c00", "nav": "#0a0600",
                   "nav_text": "#ffb000", "nav_selected": "#4a3000", "nav_selected_text": "#ffd27a",
                   "title": "#ffc94d", "canvas": "#0a0600", "grid": "#8f5f00", "node": "#140c00",
                   "node_header": "#3d2700"},
        "status": {"ok": "#ffd27a", "warn": "#ff9e2c", "bad": "#ff7043", "muted": "#d99400"},
        "font": {"families": MONO, "size": 10}, "radius": 0, "bevel": False, "extra": "amber",
        "terminal": {"background": "#0a0600", "foreground": "#ffb000", "cursor": "#ffb000",
                     "selectionBackground": "#5c3b00"},
    },
    "unicorn": {
        "label": N_("unicorn"), "dark": False,
        "colors": {"window": "#fff4fb", "base": "#ffffff", "alternate": "#fdf2ff", "text": "#3b1f4a",
                   "secondary": "#6e4c84", "placeholder": "#7d5d92", "button": "#ffffff", "border": "#a878bd",
                   "highlight": "#a020c8", "highlight_text": "#ffffff", "accent": "#a020c8", "accent_text": "#ffffff",
                   "danger": "#c2185b", "tooltip": "#fffaff", "disabled": "#a58fb3", "nav": "#fbe3f6",
                   "nav_text": "#4a2160", "nav_selected": "#e7d1ff", "nav_selected_text": "#5b118a",
                   "title": "#7a1fa2", "canvas": "#fff7fd", "grid": "#aa77bb", "node": "#ffffff",
                   "node_header": "#f6defb"},
        "status": {"ok": "#1d7a4d", "warn": "#9a4d00", "bad": "#c2185b", "muted": "#6e4c84"},
        "font": {"families": ["Nunito", "Quicksand", "Comfortaa", *SANS], "size": 10}, "radius": 14, "bevel": False, "extra": "unicorn",
        "gradient": ["#ffd6f0", "#e7d1ff", "#d3f5ff", "#d6ffe8"],
        "terminal": {"background": "#fff7fd", "foreground": "#3b1f4a", "cursor": "#a020c8",
                     "selectionBackground": "#ead2ff"},
    },
    "win311": {
        "label": N_("Windows 3.11"), "dark": False,
        "colors": {"window": "#c0c0c0", "base": "#ffffff", "alternate": "#f0f0f0", "text": "#000000",
                   "secondary": "#303030", "placeholder": "#4a4a4a", "button": "#c0c0c0", "border": "#000000",
                   "highlight": "#000080", "highlight_text": "#ffffff", "accent": "#000080", "accent_text": "#ffffff",
                   "danger": "#800000", "tooltip": "#ffffe1", "disabled": "#5e5e5e", "nav": "#ffffff",
                   "nav_text": "#000000", "nav_selected": "#000080", "nav_selected_text": "#ffffff",
                   "title": "#000080", "canvas": "#008080", "grid": "#00eded", "node": "#c0c0c0",
                   "node_header": "#000080", "node_title": "#ffffff", "light_edge": "#ffffff", "dark_edge": "#808080"},
        "status": {"ok": "#005900", "warn": "#654600", "bad": "#800000", "muted": "#303030"},
        "font": {"families": ["MS Sans Serif", "Microsoft Sans Serif", "Arial", "Liberation Sans", "sans-serif"],
                 "size": 9}, "radius": 0, "bevel": True, "extra": "win311",
        "terminal": {"background": "#000000", "foreground": "#c0c0c0", "cursor": "#ffffff",
                     "selectionBackground": "#000080"},
    },
    "mac": {
        "label": N_("Mac"), "dark": False,
        "colors": {"window": "#ececec", "base": "#ffffff", "alternate": "#f5f5f7", "text": "#1d1d1f",
                   "secondary": "#55555a", "placeholder": "#6e6e73", "button": "#ffffff", "border": "#828287",
                   "highlight": "#0064d1", "highlight_text": "#ffffff", "accent": "#0064d1", "accent_text": "#ffffff",
                   "danger": "#c4251b", "tooltip": "#f5f5f7", "disabled": "#8e8e93", "nav": "#e8e8ed",
                   "nav_text": "#1d1d1f", "nav_selected": "#d4d4da", "nav_selected_text": "#1d1d1f",
                   "title": "#1d1d1f", "canvas": "#f5f5f7", "grid": "#8a8a8f", "node": "#ffffff",
                   "node_header": "#e8f0fc"},
        "status": {"ok": "#197834", "warn": "#8a5300", "bad": "#c4251b", "muted": "#55555a"},
        "font": {"families": ["SF Pro Text", "Helvetica Neue", "Inter", "Helvetica", *SANS], "size": 10},
        "radius": 7, "bevel": False, "extra": "mac",
        "terminal": {"background": "#ffffff", "foreground": "#1d1d1f", "cursor": "#0064d1",
                     "selectionBackground": "#b3d7ff"},
    },
    "fallout": {
        "label": N_("Fallout"), "dark": True,
        "colors": {"window": "#0b1a0b", "base": "#0f220f", "alternate": "#122812", "text": "#4dff88",
                   "secondary": "#3ccf6d", "placeholder": "#36b862", "button": "#102610", "border": "#2fa65a",
                   "highlight": "#4dff88", "highlight_text": "#0b1a0b", "accent": "#4dff88", "accent_text": "#0b1a0b",
                   "danger": "#ffb347", "tooltip": "#102610", "disabled": "#258a48", "nav": "#081408",
                   "nav_text": "#4dff88", "nav_selected": "#4dff88", "nav_selected_text": "#081408",
                   "title": "#8dffb2", "canvas": "#081408", "grid": "#26874a", "node": "#0f220f",
                   "node_header": "#1c4a2a"},
        "status": {"ok": "#4dff88", "warn": "#f2e35c", "bad": "#ffb347", "muted": "#3ccf6d"},
        "font": {"families": ["Share Tech Mono", "VT323", *MONO], "size": 11}, "radius": 4, "bevel": False, "extra": "fallout",
        "terminal": {"background": "#081408", "foreground": "#4dff88", "cursor": "#4dff88",
                     "selectionBackground": "#1c5a32"},
    },
}

THEMES = ("system", *THEMES_DATA)
# Kept for code that knows only light and dark.
COLORS = {name: data["colors"] for name, data in THEMES_DATA.items()}
STATUS = {name: data["status"] for name, data in THEMES_DATA.items()}


def choices():
    """(key, label) of every theme, for the theme boxes; labels are translated by the caller."""
    return [("system", N_("as the system"))] + [(key, data["label"]) for key, data in THEMES_DATA.items()]


def luminance(color):
    color = QColor(color)

    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def palette(mode):
    """The QPalette of the theme MODE."""
    c, R, G = THEMES_DATA[mode]["colors"], QPalette.ColorRole, QPalette.ColorGroup
    p = QPalette()
    roles = {R.Window: c["window"], R.WindowText: c["text"], R.Base: c["base"], R.AlternateBase: c["alternate"],
             R.ToolTipBase: c["tooltip"], R.ToolTipText: c["text"], R.Text: c["text"], R.Button: c["button"],
             R.ButtonText: c["text"], R.BrightText: c["danger"], R.Highlight: c["highlight"],
             R.HighlightedText: c["highlight_text"], R.Link: c["accent"], R.LinkVisited: c["accent"],
             R.PlaceholderText: c["placeholder"], R.Light: c.get("light_edge", c["button"]),
             R.Midlight: c["border"], R.Mid: c["border"], R.Dark: c.get("dark_edge", c["border"]),
             R.Shadow: "#000000"}
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, QColor(c["disabled"]))
    return p


def mode_of(p=None):
    """ "dark" or "light": the look of the palette in use (also for the system theme)."""
    return "dark" if is_dark(p) else "light"


def data():
    """The tokens of the theme in use; for the system theme the light or dark ones, with the system's
    palette colours where they exist."""
    if _current in THEMES_DATA:
        return THEMES_DATA[_current]
    app = QApplication.instance()
    p = app.palette() if app else QPalette()
    base = dict(THEMES_DATA[mode_of(p)])
    colors_ = dict(base["colors"])
    colors_.update(window=p.color(QPalette.ColorRole.Window).name(), base=p.color(QPalette.ColorRole.Base).name(),
                   text=p.color(QPalette.ColorRole.WindowText).name(), button=p.color(QPalette.ColorRole.Button).name())
    return {**base, "colors": colors_, "font": None}


def colors():
    """The colour tokens for the current look."""
    return data()["colors"]


def _bevel(c, pressed=False):
    light, dark = c.get("light_edge", "#ffffff"), c.get("dark_edge", "#808080")
    if pressed:
        light, dark = dark, light
    return (f"border-top: 2px solid {light}; border-left: 2px solid {light}; "
            f"border-right: 2px solid {dark}; border-bottom: 2px solid {dark};")


def _asset_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "sandbox-ai-app" / "theme"


def _paint(path, size, draw):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(painter, size)
    painter.end()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pixmap.save(str(temporary), "PNG")
    os.replace(temporary, path)


def assets(name=None):
    """Small images the stylesheet needs (a check mark, Windows 3.11's cross, arrows), painted in the theme's
    colours into the user's cache: Qt stylesheets can only refer to files. {key: path for url()} or {} when
    the cache cannot be written (the stylesheet then does without)."""
    name = name or _current
    t = THEMES_DATA.get(name)
    if t is None or QApplication.instance() is None:
        return {}
    c = t["colors"]
    folder = _asset_dir() / name
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink():
            return {}

        def pen(color, width):
            result = QPen(QColor(color), width)
            result.setCapStyle(Qt.PenCapStyle.RoundCap)
            result.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            return result

        def check(painter, size):
            painter.setPen(pen(c["accent_text"], size / 7))
            painter.drawPolyline([QPointF(size * .22, size * .52), QPointF(size * .42, size * .72),
                                  QPointF(size * .78, size * .30)])

        def cross(painter, size):  # Windows 3.11 checked its boxes with an X
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(QColor(c["text"]), 1.5))
            painter.drawLine(QPointF(1, 1), QPointF(size - 2, size - 2))
            painter.drawLine(QPointF(size - 2, 1), QPointF(1, size - 2))

        def dot(painter, size):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["text"] if t.get("bevel") else c["accent_text"]))
            painter.drawEllipse(QRectF(size * .3, size * .3, size * .4, size * .4))

        def triangle(direction):
            def draw(painter, size):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(c["text"]))
                a, b, m = size * .25, size * .75, size * .5
                points = {"down": [(a, a + 1), (b, a + 1), (m, b - 1)], "up": [(a, b - 1), (b, b - 1), (m, a + 1)],
                          "left": [(b - 1, a), (b - 1, b), (a + 1, m)], "right": [(a + 1, a), (a + 1, b), (b - 1, m)]}
                painter.drawPolygon([QPointF(x, y) for x, y in points[direction]])
            return draw

        def chevron(painter, size):  # macOS pop-up buttons
            painter.setPen(pen(c["secondary"], size / 9))
            painter.drawPolyline([QPointF(size * .3, size * .42), QPointF(size * .5, size * .62),
                                  QPointF(size * .7, size * .42)])
        def scanlines(painter, size):  # Fallout's Pip-Boy screen: a darker line every third pixel row
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.fillRect(0, 0, size, size, QColor(c["base"]))
            line = QColor(c["window"])
            for y in range(0, size, 3):
                painter.fillRect(0, y, size, 1, line)
        found = {}
        for key, size, draw in (("check", 16, check), ("cross", 13, cross), ("dot", 16, dot), ("chevron", 16, chevron),
                                ("scanlines", 12, scanlines),
                                ("down", 12, triangle("down")), ("up", 12, triangle("up")),
                                ("left", 12, triangle("left")), ("right", 12, triangle("right"))):
            path = folder / f"{key}.png"
            _paint(path, size, draw)
            found[key] = path.as_posix()
        return found
    except OSError:
        return {}


def _win311(c, a):
    """Windows 3.11: bevelled grey controls, sunken white fields, navy title bars, an X in check boxes,
    scroll bars with arrow buttons, bold system text on buttons."""
    raised, sunken = _bevel(c), _bevel(c, pressed=True)
    arrow = lambda key: f'image: url("{a[key]}");' if key in a else ""  # noqa: E731
    return f"""
QLabel[role="title"] {{ background: {c['highlight']}; color: {c['highlight_text']}; padding: 3px 8px; font-size: 12pt; }}
QLabel[role="section"] {{ color: {c['text']}; font-weight: 700; }}
QLabel#brand {{ background: {c['highlight']}; color: {c['highlight_text']}; padding: 6px 10px; margin: 4px; }}
QPushButton {{ font-weight: 700; color: {c['text']}; }}
QPushButton[role="primary"] {{ background: {c['button']}; color: {c['text']}; border: 2px solid {c['text']};
                               padding: 4px 14px; }}
QPushButton[role="primary"]:pressed {{ {sunken} }}
QPushButton:focus {{ text-decoration: underline; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ {sunken} border-radius: 0;
    background: {c['base']}; padding: 2px 4px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    {sunken} }}
QComboBox::drop-down {{ width: 16px; border: none; {raised} background: {c['button']}; }}
QComboBox::down-arrow {{ {arrow('down')} }}
QTableWidget, QTableView, QTreeView, QListView {{ {sunken} border-radius: 0; gridline-color: {c['window']}; }}
QHeaderView::section {{ {raised} background: {c['button']}; color: {c['text']}; padding: 3px 6px; }}
QTabWidget::pane {{ {raised} border-radius: 0; top: -2px; }}
QTabBar::tab {{ {raised} border-bottom: none; background: {c['button']}; color: {c['text']}; padding: 4px 12px;
               margin-right: 2px; border-radius: 0; }}
QTabBar::tab:selected {{ font-weight: 700; margin-bottom: -2px; border-top-left-radius: 0; border-top-right-radius: 0; }}
QGroupBox {{ border: 1px solid {c['dark_edge']}; border-radius: 0; background: transparent; margin-top: 14px; }}
QGroupBox::title {{ color: {c['text']}; background: {c['window']}; }}
QFrame[role="kpi"], QFrame[role="card"] {{ {raised} border-radius: 0; background: {c['window']}; }}
QListWidget#nav {{ {sunken} background: {c['nav']}; margin: 4px; }}
QListWidget#nav::item {{ border-radius: 0; padding: 4px 8px; margin: 0; }}
QListWidget#nav::item:hover {{ background: transparent; text-decoration: underline; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {c['text']}; border-radius: 0;
                        background: {c['base']}; }}
QCheckBox::indicator:checked {{ background: {c['base']}; border-color: {c['text']}; {arrow('cross')} }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {c['text']}; border-radius: 7px;
                           background: {c['base']}; }}
QRadioButton::indicator:checked {{ background: {c['base']}; border-color: {c['text']}; {arrow('dot')} }}
QScrollBar:vertical {{ width: 17px; background: {c['alternate']}; margin: 17px 0 17px 0; border: 1px solid {c['text']}; }}
QScrollBar:horizontal {{ height: 17px; background: {c['alternate']}; margin: 0 17px 0 17px; border: 1px solid {c['text']}; }}
QScrollBar::handle {{ {raised} background: {c['button']}; border-radius: 0; min-height: 17px; min-width: 17px; }}
QScrollBar::handle:hover {{ background: {c['button']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ {raised} background: {c['button']}; subcontrol-origin: margin; }}
QScrollBar::add-line:vertical {{ height: 15px; subcontrol-position: bottom; }}
QScrollBar::sub-line:vertical {{ height: 15px; subcontrol-position: top; }}
QScrollBar::add-line:horizontal {{ width: 15px; subcontrol-position: right; }}
QScrollBar::sub-line:horizontal {{ width: 15px; subcontrol-position: left; }}
QScrollBar::up-arrow:vertical {{ {arrow('up')} }}
QScrollBar::down-arrow:vertical {{ {arrow('down')} }}
QScrollBar::left-arrow:horizontal {{ {arrow('left')} }}
QScrollBar::right-arrow:horizontal {{ {arrow('right')} }}
QMenuBar {{ background: {c['base']}; border-bottom: 1px solid {c['text']}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {c['highlight']}; color: {c['highlight_text']}; }}
QMenu {{ border: 1px solid {c['text']}; border-radius: 0; }}
QMenu::item {{ border-radius: 0; }}
QStatusBar {{ border-top: 1px solid {c['dark_edge']}; color: {c['text']}; }}
QToolTip {{ border: 1px solid {c['text']}; border-radius: 0; }}
QSplitter::handle {{ background: {c['window']}; }}
"""


def _mac(c, a):
    """macOS-like: soft white push buttons with a hairline, a blue default button, segmented tabs, sidebar
    pills, borderless tables with zebra rows, rounded group boxes, thin scroll bars, chevron pop-ups."""
    chevron = f'image: url("{a["chevron"]}");' if "chevron" in a else ""
    return f"""
QLabel[role="title"] {{ font-size: 20pt; font-weight: 700; letter-spacing: -0.3px; }}
QLabel#brand {{ font-size: 12pt; color: {c['secondary']}; padding: 12px 14px 4px 14px; }}
QPushButton {{ background: {c['button']}; border: 1px solid #b9b9be; border-bottom-color: #a1a1a6;
              border-radius: 6px; padding: 4px 14px; min-height: 18px; }}
QPushButton:hover {{ border-color: #a1a1a6; }}
QPushButton:pressed {{ background: #e5e5ea; }}
QPushButton:focus {{ border: 2px solid #7fb0ee; padding: 3px 13px; }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2f86f6, stop:1 {c['accent']});
                               color: {c['accent_text']}; border: 1px solid #0a55b6; font-weight: 600; }}
QPushButton[role="primary"]:pressed {{ background: #0a55b6; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    border: 1px solid #b9b9be; border-radius: 6px; padding: 4px 7px; background: {c['base']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid #7fb0ee; padding: 3px 6px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ {chevron} }}
QTabWidget::pane {{ border: 1px solid #d1d1d6; border-radius: 8px; top: -12px; padding-top: 14px; background: #e6e6ea; }}
QTabWidget QScrollArea, QTabWidget QScrollArea > QWidget > QWidget {{ background: #e6e6ea; }}
QTabWidget::tab-bar {{ alignment: center; }}
QTabBar::tab {{ background: #e3e3e8; color: {c['text']}; border: 1px solid #c9c9ce; border-right: none;
               padding: 4px 14px; margin: 0; border-radius: 0; }}
QTabBar::tab:first {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; }}
QTabBar::tab:last {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; border-right: 1px solid #c9c9ce; }}
QTabBar::tab:only-one {{ border-radius: 7px; border-right: 1px solid #c9c9ce; }}
QTabBar::tab:selected {{ background: {c['base']}; font-weight: 600; border-top-left-radius: 0; border-top-right-radius: 0; }}
QTabBar::tab:first:selected {{ border-top-left-radius: 7px; }}
QTabBar::tab:last:selected {{ border-top-right-radius: 7px; }}
QGroupBox {{ background: #f5f5f7; border: 1px solid #d9d9de; border-radius: 10px; }}
QFrame[role="kpi"], QFrame[role="card"] {{ background: {c['base']}; border: 1px solid #d9d9de; border-radius: 10px; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px solid #d9d9de; border-radius: 8px;
    gridline-color: transparent; alternate-background-color: #f5f5f7; }}
QHeaderView::section {{ background: {c['base']}; color: {c['secondary']}; border: none;
                        border-bottom: 1px solid #d9d9de; font-weight: 500; padding: 5px 8px; }}
QListWidget#nav {{ background: {c['nav']}; padding: 6px 8px; }}
QListWidget#nav::item {{ border-radius: 6px; padding: 6px 10px; margin: 1px 0; }}
QListWidget#nav::item:hover {{ background: #dedee3; }}
QListWidget#nav::item:selected {{ background: {c['nav_selected']}; color: {c['nav_selected_text']}; }}
QWidget#navPanel {{ background: {c['nav']}; border-right: 1px solid #d1d1d6; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #b0b0b5; border-radius: 4px; background: {c['base']}; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid #b0b0b5; border-radius: 8px; background: {c['base']}; }}
QScrollBar:vertical {{ width: 10px; }}
QScrollBar:horizontal {{ height: 10px; }}
QScrollBar::handle {{ background: #c4c4c9; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:hover {{ background: #a1a1a6; }}
QMenu {{ border-radius: 8px; border: 1px solid #d1d1d6; padding: 5px; }}
QMenu::item {{ border-radius: 5px; padding: 4px 16px; }}
QMenu::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QToolTip {{ border-radius: 6px; border: 1px solid #d1d1d6; }}
QStatusBar {{ color: {c['secondary']}; border-top: 1px solid #d9d9de; }}
"""


def _hacker(c, a):
    """A terminal, not a desktop: transparent controls framed in phosphor green that light up when touched,
    fields that are only an underline, dashed rules, inverted selections."""
    return f"""
QLabel[role="title"] {{ border-bottom: 1px dashed {c['border']}; padding-bottom: 4px; }}
QPushButton {{ background: transparent; border: 1px solid {c['border']}; border-radius: 0; padding: 4px 14px; }}
QPushButton:hover, QPushButton:focus {{ background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']}; }}
QPushButton[role="primary"] {{ background: transparent; color: {c['accent']}; border: 1px solid {c['accent']}; }}
QPushButton[role="primary"]:hover {{ background: {c['accent']}; color: {c['accent_text']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border: none; border-bottom: 1px solid {c['border']}; border-radius: 0;
    background: transparent; padding: 3px 2px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: none; border-bottom: 2px solid {c['accent']}; }}
QPlainTextEdit, QTextEdit {{ border: 1px dashed {c['border']}; border-radius: 0; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px dashed {c['border']}; border-radius: 0; gridline-color: {c['window']}; }}
QHeaderView::section {{ background: {c['window']}; border: none; border-bottom: 1px dashed {c['border']}; color: {c['accent']}; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 1px dashed {c['border']}; border-radius: 0; background: transparent; }}
QTabBar::tab {{ border: none; border-bottom: 2px solid transparent; padding: 5px 12px; }}
QTabBar::tab:selected {{ border-bottom: 2px solid {c['accent']}; background: transparent; color: {c['accent']}; }}
QTabWidget::pane {{ border: 1px dashed {c['border']}; border-radius: 0; }}
QListWidget#nav::item {{ border-radius: 0; }}
QListWidget#nav::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QScrollBar::handle {{ border-radius: 0; }}
"""


def _fallout(c, a):
    """A Pip-Boy: scan lines on every screen surface, thick rounded frames, inverted title bars and selections."""
    scan = f'background-image: url("{a["scanlines"]}");' if "scanlines" in a else ""
    return f"""
QMainWindow, QDialog {{ {scan} }}
QTableWidget, QTableView, QTreeView, QListView, QPlainTextEdit, QTextEdit {{ {scan} border: 2px solid {c['border']};
    border-radius: 6px; }}
QLabel[role="title"] {{ background: {c['accent']}; color: {c['accent_text']}; padding: 2px 10px; border-radius: 4px; }}
QLabel[role="section"] {{ color: {c['title']}; border-bottom: 2px solid {c['border']}; }}
QLabel#brand {{ background: {c['accent']}; color: {c['accent_text']}; border-radius: 4px; margin: 6px; padding: 4px 10px; }}
QPushButton {{ border: 2px solid {c['border']}; border-radius: 6px; background: {c['button']}; padding: 4px 14px; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton[role="primary"], QPushButton:pressed {{ background: {c['accent']}; color: {c['accent_text']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border: 2px solid {c['border']}; border-radius: 6px; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 2px solid {c['border']}; border-radius: 8px; }}
QTabBar::tab {{ border: 2px solid {c['border']}; border-radius: 6px; margin: 0 3px 4px 0; padding: 3px 12px; }}
QTabBar::tab:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QTabWidget::pane {{ border: 2px solid {c['border']}; border-radius: 8px; top: 0; }}
QHeaderView::section {{ background: {c['node_header']}; color: {c['title']}; border: none; }}
QListWidget#nav {{ border-right: 2px solid {c['border']}; }}
"""


def _amber(c, a):
    """A VT220: double-ruled frames, square everything, reverse-video selections, no hover effects."""
    return f"""
QPushButton {{ border: 3px double {c['border']}; border-radius: 0; background: {c['window']}; padding: 3px 14px; }}
QPushButton:hover {{ border-color: {c['border']}; }}
QPushButton:focus, QPushButton:pressed, QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']};
    border-color: {c['accent']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{ border: 1px solid {c['border']}; border-radius: 0; }}
QTableWidget, QTableView, QTreeView, QListView, QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{
    border: 3px double {c['border']}; border-radius: 0; }}
QTabBar::tab {{ border: 1px solid {c['border']}; border-radius: 0; padding: 3px 12px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QTabWidget::pane {{ border: 3px double {c['border']}; border-radius: 0; }}
QHeaderView::section {{ border: none; border-bottom: 3px double {c['border']}; }}
QLabel[role="title"] {{ border-top: 3px double {c['border']}; border-bottom: 3px double {c['border']}; padding: 2px 0; }}
QListWidget#nav {{ border-right: 3px double {c['border']}; }}
QListWidget#nav::item {{ border-radius: 0; }}
QListWidget#nav::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QScrollBar::handle {{ border-radius: 0; }}
"""


def _cyberpunk(c, a):
    """Neon: pink-to-purple gradient buttons, cyan focus glow lines, yellow titles over a cyan rule, cut
    corners suggested by asymmetric radii."""
    return f"""
QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2a0a5e, stop:1 #12043a);
    border: 1px solid #c03fd0; border-radius: 0; border-top-right-radius: 8px; border-bottom-left-radius: 8px;
    padding: 5px 16px; color: {c['text']}; }}
QPushButton:hover {{ border-color: #05d9e8; color: #05d9e8; }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff2a6d, stop:1 #c03fd0);
    color: {c['accent_text']}; border: 1px solid #ff2a6d; font-weight: 700; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid #05d9e8; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{ border-radius: 0;
    border-top-right-radius: 6px; border-bottom-left-radius: 6px; }}
QLabel[role="title"] {{ color: #f9f002; border-bottom: 2px solid #05d9e8; padding-bottom: 3px; letter-spacing: 2px; }}
QLabel#brand {{ color: #ff2a6d; letter-spacing: 2px; }}
QTabBar::tab:selected {{ border: none; border-bottom: 3px solid #ff2a6d; background: transparent; color: #f9f002; }}
QTabBar::tab {{ border: none; border-bottom: 3px solid transparent; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 1px solid #c03fd0; border-radius: 0;
    border-top-right-radius: 12px; border-bottom-left-radius: 12px; }}
QHeaderView::section {{ color: #05d9e8; border-bottom: 1px solid #ff2a6d; }}
QListWidget#nav::item:selected {{ border-radius: 0; border-left: 4px solid #f9f002; }}
"""


def _unicorn(c, a):
    """Soft and round: pill buttons with a pastel gradient, pill tabs, cards with generous radii."""
    return f"""
QPushButton {{ border-radius: 14px; padding: 6px 18px; border: 1px solid {c['border']};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #fbe9ff); }}
QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffe3f3, stop:1 #e9dcff); }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c2185b, stop:1 #7a1fa2);
    color: #ffffff; border: none; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border-radius: 12px; padding: 4px 10px; }}
QTabBar::tab {{ border-radius: 12px; margin: 2px 3px; padding: 4px 14px; border: 1px solid transparent; }}
QTabBar::tab:selected {{ background: #e7d1ff; color: {c['nav_selected_text']}; border: 1px solid {c['border']};
    border-radius: 12px; }}
QTabWidget::pane {{ border-radius: 16px; top: 0; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"], QTableWidget, QTableView {{ border-radius: 16px; }}
QListWidget#nav::item {{ border-radius: 14px; }}
QLabel[role="title"] {{ color: #7a1fa2; }}
"""


EXTRAS = {"win311": _win311, "mac": _mac, "hacker": _hacker, "fallout": _fallout, "amber": _amber,
          "cyberpunk": _cyberpunk, "unicorn": _unicorn}


# The navigation as a strip of icons (MainWindow.set_compact_nav sets the property): no room is lost to the paddings
# the titles need, so each icon fits in its narrow row whatever the theme pads (with them it was cut away). The
# attribute makes these rules win over every theme's own QListWidget#nav::item.
COMPACT_NAV = """
QListWidget#nav[compact="true"] { padding: 4px 0px; }
QListWidget#nav[compact="true"]::item { padding: 6px 0px 6px 8px; margin: 1px 4px; }
QListWidget#nav[compact="true"]::item:disabled { padding: 0px; }
"""


def stylesheet():
    t = data()
    c, s, r = t["colors"], t["status"], t["radius"]
    small = max(0, r - 2)
    bevel = t.get("bevel")
    gradient = t.get("gradient")
    nav_background = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, " +
                      ", ".join(f"stop:{i / (len(gradient) - 1):.2f} {g}" for i, g in enumerate(gradient)) + ")"
                      if gradient else c["nav"])
    button = (f"QPushButton {{ padding: 4px 14px; {_bevel(c)} border-radius: 0; background: {c['button']};"
              f" min-height: 18px; }}\n"
              f"QPushButton:pressed {{ {_bevel(c, pressed=True)} }}\n"
              if bevel else
              f"QPushButton {{ padding: 6px 14px; border: 1px solid {c['border']}; border-radius: {small}px;"
              f" background: {c['button']}; min-height: 18px; }}\n"
              f"QPushButton:hover {{ border-color: {c['accent']}; }}\n"
              f"QPushButton:pressed {{ background: {c['alternate']}; }}\n"
              f"QPushButton:focus {{ border: 2px solid {c['accent']}; padding: 5px 13px; }}\n")
    images = assets() if _current in THEMES_DATA else {}
    check = f'image: url("{images["check"]}");' if "check" in images else ""
    extra = EXTRAS[t["extra"]](c, images) if t.get("extra") in EXTRAS else ""
    frame = f"border: 1px solid {c['border']}; border-radius: {r}px; background: {c['base']};"
    if bevel:
        frame = f"{_bevel(c, pressed=True)} border-radius: 0; background: {c['base']};"
    return f"""
QWidget {{ font-size: {t['font']['size'] if t.get('font') else 10}pt; }}
QMainWindow, QDialog {{ background: {c['window']}; }}
QLabel[role="title"] {{ font-size: 17pt; font-weight: 700; padding: 2px 0 2px 0; color: {c['title']}; }}
QLabel[role="section"] {{ font-size: 11pt; font-weight: 600; padding-top: 6px; color: {c['title']}; }}
QLabel[role="secondary"] {{ color: {c['secondary']}; }}
QLabel[role="notice-warn"] {{ color: {s['warn']}; }}
QLabel[role="notice-error"] {{ color: {s['bad']}; font-weight: 600; }}
QLabel[role="notice-ok"] {{ color: {s['ok']}; }}
QLabel[role="kpi-value"] {{ font-size: 18pt; font-weight: 700; color: {c['accent'] if t['dark'] else c['title']}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 700; color: {c['nav_text']}; padding: 10px 12px 6px 12px;
               background: transparent; }}
QFrame[role="kpi"], QFrame[role="card"] {{ {frame} }}
QGroupBox {{ {frame} margin-top: 14px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['title']};
                    font-weight: 600; }}
{button}QPushButton:disabled {{ color: {c['disabled']}; border-color: {c['disabled']}; }}
QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']};
                               font-weight: 600; }}
QPushButton[role="primary"]:hover {{ border-color: {c['text']}; }}
QPushButton[role="primary"]:disabled {{ background: {c['button']}; color: {c['disabled']};
                                        border-color: {c['disabled']}; }}
QPushButton[role="danger"] {{ color: {c['danger']}; border-color: {c['danger']}; }}
QPushButton[role="danger"]:disabled {{ color: {c['disabled']}; border-color: {c['disabled']}; }}
QToolButton {{ padding: 4px 8px; border: 1px solid transparent; border-radius: {small}px; }}
QToolButton:hover {{ border-color: {c['border']}; background: {c['alternate']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    border: 1px solid {c['border']}; border-radius: {small}px; padding: 4px 6px; background: {c['base']};
    color: {c['text']}; selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {c['accent']}; }}
QComboBox QAbstractItemView {{ background: {c['base']}; color: {c['text']}; border: 1px solid {c['border']};
                               selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px solid {c['border']}; border-radius: {small}px;
    background: {c['base']}; alternate-background-color: {c['alternate']}; gridline-color: {c['alternate']};
    selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QHeaderView::section {{ padding: 6px 8px; border: none; border-bottom: 1px solid {c['border']};
                        background: {c['window']}; color: {c['title']}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {small}px; top: -1px; }}
QTabBar::tab {{ padding: 7px 14px; border: 1px solid transparent; border-bottom: none; color: {c['secondary']}; }}
QTabBar::tab:selected {{ border-color: {c['border']}; background: {c['base']}; color: {c['text']};
                         border-top-left-radius: {small}px; border-top-right-radius: {small}px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QListWidget#nav {{ border: none; background: {nav_background}; padding: 6px 6px; outline: none;
                   color: {c['nav_text']}; }}
QListWidget#nav::item {{ padding: 8px 10px; margin: 1px 0; border-radius: {small}px; color: {c['nav_text']}; }}
QListWidget#nav::item:hover {{ background: {c['alternate']}; }}
QListWidget#nav::item:selected {{ background: {c['nav_selected']}; color: {c['nav_selected_text']};
                                  font-weight: 600; }}
QListWidget#nav::item:disabled {{ background: transparent; color: {c['nav_text']}; padding: 12px 10px 2px 10px;
                                  font-size: 8pt; font-weight: 700; }}
QWidget#navPanel {{ background: {nav_background}; }}
QWidget#projectBar {{ background: {c['window']}; }}
QWidget#tabStrip {{ background: {c['window']}; border-bottom: 1px solid {c['border']}; }}
QTabBar#projectTabs::tab {{ padding: 6px 12px; margin-right: 2px; border: 1px solid transparent; border-bottom: none;
                            color: {c['secondary']}; }}
QTabBar#projectTabs::tab:selected {{ border-color: {c['border']}; background: {c['base']}; color: {c['text']};
                                     font-weight: 600; border-top-left-radius: {small}px;
                                     border-top-right-radius: {small}px; }}
QTabBar#projectTabs::tab:hover {{ color: {c['text']}; }}
QToolButton#tabClose {{ padding: 0; border: none; }}
QToolButton#tabClose:hover {{ background: {c['alternate']}; border-radius: {small}px; }}
QDockWidget {{ color: {c['title']}; }}
QDockWidget::title {{ background: {c['alternate']}; padding: 5px 8px; border: 1px solid {c['border']}; }}
QLabel#projectName {{ font-size: 14pt; font-weight: 700; color: {c['title']}; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {c['border']};
                                                 background: {c['base']}; border-radius: {min(3, r)}px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; {check} }}
QRadioButton::indicator:checked {{ border-color: {c['accent']}; background: qradialgradient(cx:0.5, cy:0.5,
    radius:0.5, fx:0.5, fy:0.5, stop:0 {c['accent']}, stop:0.5 {c['accent']}, stop:0.6 {c['base']}, stop:1 {c['base']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {c['disabled']}; }}
QListWidget::item {{ padding: 3px 4px; }}
QToolTip {{ border: 1px solid {c['border']}; padding: 5px; background: {c['tooltip']}; color: {c['text']}; }}
QFrame#helpPopover {{ background: {c['base']}; border: 2px solid {c['accent']}; border-radius: {small}px; }}
QFrame#helpPopover QLabel {{ background: transparent; }}
QScrollBar:vertical {{ width: 11px; background: transparent; margin: 0; }}
QScrollBar:horizontal {{ height: 11px; background: transparent; margin: 0; }}
QScrollBar::handle {{ background: {c['border']}; border-radius: {min(5, r)}px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {c['secondary']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ color: {c['secondary']}; }}
QSplitter::handle {{ background: {c['alternate']}; }}
QMenu {{ background: {c['base']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: {small}px; }}
QMenu::item:selected {{ background: {c['highlight']}; color: {c['highlight_text']}; }}
""" + extra + COMPACT_NAV


def font(name=None):
    """The QFont of theme NAME (default: the one in use), or None for the system's font."""
    spec = THEMES_DATA.get(name or _current, {}).get("font")
    if not spec:
        return None
    result = QFont()
    result.setFamilies(list(spec["families"]))
    result.setPointSize(spec["size"])
    if spec["families"] and spec["families"][0] in MONO + ["Share Tech Mono", "VT323"]:
        result.setStyleHint(QFont.StyleHint.Monospace)
    return result


_style_name = None  # the base style apply() set last ("Fusion", or the system's)


def apply(mode, app=None):
    """Switch the whole application to theme MODE (one of THEMES; anything else is "system")."""
    global _system, _current
    app = app or QApplication.instance()
    if mode not in THEMES:
        mode = "system"
    if _system is None:
        _system = (app.style().name(), QPalette(app.palette()), QFont(app.font()))
    # A new QStyle only when the style really changes: setStyle() deletes the old one, and code still running
    # inside a widget's event (the theme's own combo box, a menu) then used freed memory (a crash on theme change).
    # (app.style() is Qt's style-sheet proxy once a sheet is set, whose name is not the base style's: the base
    # style set last is remembered here instead.)
    global _style_name
    wanted = _system[0] if mode == "system" else "Fusion"
    if (_style_name or app.style().name()).lower() != str(wanted).lower():
        app.setStyle(wanted)
    _style_name = str(wanted)
    if mode == "system":
        app.setPalette(_system[1])
        app.setFont(_system[2])
    else:
        app.setPalette(palette(mode))
        app.setFont(font(mode) or _system[2])
    _current = mode
    sheet = stylesheet()
    if app.styleSheet() != sheet:  # the same sheet again would re-polish every widget for nothing
        if app.styleSheet():
            # Qt goes from one sheet to another about six times slower (about 3 s for this window) than from none
            # to one; clearing first, in the same turn of the event loop, shows nothing unstyled in between.
            app.setStyleSheet("")
        app.setStyleSheet(sheet)
    return mode


def apply_soon(mode, after=None):
    """apply(MODE) once the event that asked for it is over (a combo box's or a menu's own handling must not run on
    a style that apply() replaced), with a busy cursor while the widgets are polished; then AFTER(mode)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QCursor

    def run():
        app = QApplication.instance()
        app.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            applied = apply(mode)
        finally:
            app.restoreOverrideCursor()
        if after is not None:
            after(applied)
    QTimer.singleShot(0, run)


def current():
    return _current


def is_dark(p=None):
    if p is None and _current in THEMES_DATA:
        return THEMES_DATA[_current]["dark"]
    app = QApplication.instance()
    p = p or (app.palette() if app else QPalette())
    return p.color(QPalette.ColorRole.Window).lightness() < 128


def color(name):
    """A status colour ("ok", "warn", "bad", "muted") readable on the current background."""
    return data()["status"][name]


def terminal_theme():
    """The terminal's colours and font for the theme in use, as JSON for the terminal page (xterm.js)."""
    t = data()
    spec = THEMES_DATA.get(_current, {}).get("font")
    families = [f for f in (spec["families"] if spec else []) if f in MONO + ["Share Tech Mono", "VT323"]]
    return json.dumps({**t["terminal"], "light": not t["dark"],
                       "fontFamily": ", ".join(f'"{f}"' if " " in f else f for f in (families or MONO))})


def dot(name, size=10):
    """A small round icon in a status colour: status stays visible in a selected table row."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color(name)))
    painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return QIcon(pixmap)


# The navigation's icons: simple line drawings on a 24 x 24 grid, painted in the navigation's text colour.
GLYPHS = {
    "launch": [("poly", [(8, 5), (19, 12), (8, 19)], True)],
    "board": [("rect", (3, 4, 7, 6)), ("rect", (14, 14, 7, 6)), ("rect", (14, 4, 7, 6)),
              ("line", (10, 7, 14, 7)), ("line", (17.5, 10, 17.5, 14))],
    "sandboxes": [("rect", (3, 5, 18, 14)), ("line", (3, 9, 21, 9)), ("dot", (6, 7))],
    "approvals": [("poly", [(12, 3), (20, 6), (20, 12), (12, 21), (4, 12), (4, 6)], True),
                  ("poly", [(8, 12), (11, 15), (16, 9)], False)],
    "connections": [("dot", (5, 12)), ("dot", (19, 6)), ("dot", (19, 18)), ("line", (5, 12, 19, 6)),
                    ("line", (5, 12, 19, 18))],
    "activity": [("poly", [(3, 12), (7, 12), (10, 5), (14, 19), (17, 12), (21, 12)], False)],
    "statistics": [("line", (5, 20, 5, 12)), ("line", (10, 20, 10, 6)), ("line", (15, 20, 15, 14)),
                   ("line", (20, 20, 20, 9))],
    "files": [("poly", [(3, 6), (9, 6), (11, 8), (21, 8), (21, 19), (3, 19)], True)],
    "mcp": [("rect", (4, 4, 16, 6)), ("rect", (4, 14, 16, 6)), ("dot", (7, 7)), ("dot", (7, 17))],
    "llm": [("rect", (3, 4, 18, 12)), ("poly", [(8, 16), (8, 21), (13, 16)], True), ("line", (7, 8, 17, 8)),
            ("line", (7, 12, 14, 12))],
    "images": [("poly", [(12, 3), (20, 7.5), (20, 16.5), (12, 21), (4, 16.5), (4, 7.5)], True),
               ("line", (12, 12, 12, 21)), ("line", (4, 7.5, 12, 12)), ("line", (20, 7.5, 12, 12))],
    "credentials": [("circle", (8, 12, 4)), ("line", (12, 12, 21, 12)), ("line", (18, 12, 18, 16)),
                    ("line", (21, 12, 21, 15))],
    "terminals": [("rect", (3, 4, 18, 16)), ("poly", [(7, 9), (10, 12), (7, 15)], False), ("line", (12, 15, 17, 15))],
    "kits": [("rect", (3, 8, 18, 12)), ("poly", [(9, 8), (9, 5), (15, 5), (15, 8)], False), ("line", (3, 13, 21, 13)),
             ("line", (12, 11, 12, 15))],
    "project": [("poly", [(3, 6), (9, 6), (11, 8), (21, 8), (21, 19), (3, 19)], True), ("line", (3, 11, 21, 11))],
    # The window's own controls (the header bar), for desktops whose window manager shows none.
    "minimize": [("line", (5, 17, 19, 17))],
    "maximize": [("rect", (5, 5, 14, 14)), ("line", (5, 8, 19, 8))],
    "restore": [("rect", (4, 9, 11, 11)), ("poly", [(8, 9), (8, 5), (20, 5), (20, 16), (15, 16)], False)],
    "fullscreen": [("poly", [(4, 9), (4, 4), (9, 4)], False), ("poly", [(15, 4), (20, 4), (20, 9)], False),
                   ("poly", [(20, 15), (20, 20), (15, 20)], False), ("poly", [(9, 20), (4, 20), (4, 15)], False)],
    "tray": [("rect", (3, 15, 18, 5)), ("line", (12, 3, 12, 12)), ("poly", [(8, 8), (12, 12), (16, 8)], False)],
    "quit": [("line", (6, 6, 18, 18)), ("line", (18, 6, 6, 18))],
    "fold-open": [("poly", [(6, 9), (12, 15), (18, 9)], False)],
    "fold-closed": [("poly", [(9, 6), (15, 12), (9, 18)], False)],
    "system": [("circle", (12, 12, 3.5)), ("circle", (12, 12, 8))],
    "hosts": [("rect", (3, 4, 7, 16)), ("rect", (14, 4, 7, 16)), ("dot", (6.5, 8)), ("dot", (17.5, 8)),
              ("line", (10, 14, 14, 14))],
    "help": [("circle", (12, 12, 9)), ("poly", [(9, 9.5), (9.6, 7.4), (12, 6.6), (14.4, 7.4), (15, 9.5), (12, 12),
                                                 (12, 14)], False), ("dot", (12, 17.3))],
}


def _glyph_pixmap(name, color_, size):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    tint = QColor(color_)
    pen = QPen(tint, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for shape in GLYPHS.get(name, []):
        kind = shape[0]
        if kind == "line":
            x1, y1, x2, y2 = shape[1]
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        elif kind == "rect":
            x, y, w, h = shape[1]
            painter.drawRoundedRect(QRectF(x, y, w, h), 2, 2)
        elif kind == "circle":
            x, y, radius = shape[1]
            painter.drawEllipse(QPointF(x, y), radius, radius)
        elif kind == "dot":
            painter.setBrush(tint)
            painter.drawEllipse(QPointF(*shape[1]), 1.6, 1.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif kind == "poly":
            path = QPainterPath(QPointF(*shape[1][0]))
            for point in shape[1][1:]:
                path.lineTo(QPointF(*point))
            if shape[2]:
                path.closeSubpath()
            painter.drawPath(path)
    painter.end()
    return pixmap


def glyph(name, color_=None, size=20):
    """The navigation icon NAME (a key of GLYPHS) in COLOR (default: the navigation's text colour; a
    selected item gets the colour of the selected navigation text)."""
    icon = QIcon(_glyph_pixmap(name, color_ or colors()["nav_text"], size))
    if color_ is None:
        icon.addPixmap(_glyph_pixmap(name, colors()["nav_selected_text"], size), QIcon.Mode.Selected)
    return icon


def counted_glyph(name, count, size=24):
    """The navigation icon NAME with COUNT on a badge in its corner (the strip of icons shows no text): in the
    navigation's colours, the badge in the "bad" status colour."""
    icon = QIcon()
    text = str(count) if count < 100 else "99+"
    for tint, mode in ((colors()["nav_text"], QIcon.Mode.Normal), (colors()["nav_selected_text"], QIcon.Mode.Selected)):
        pixmap = _glyph_pixmap(name, tint, size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        badge = QFont()
        badge.setBold(True)
        badge.setPixelSize(max(7, int(size * 0.36)))
        painter.setFont(badge)
        width = max(size * 0.5, painter.fontMetrics().horizontalAdvance(text) + size * 0.16)
        area = QRectF(size - width, 0, width, size * 0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color("bad")))
        painter.drawRoundedRect(area, area.height() / 2, area.height() / 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(area, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def app_icon(attention=False):
    """The window and tray icon: a shield on a rounded square (orange with a dot when a question waits)."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 64
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1f63c6"))
        painter.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 13 * s, 13 * s)
        shield = QPainterPath()
        shield.moveTo(32 * s, 11 * s)
        shield.lineTo(49 * s, 17 * s)
        shield.cubicTo(49 * s, 34 * s, 43 * s, 46 * s, 32 * s, 53 * s)
        shield.cubicTo(21 * s, 46 * s, 15 * s, 34 * s, 15 * s, 17 * s)
        shield.closeSubpath()
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(shield)
        pen = QPen(QColor("#1f63c6"), max(1.0, 4 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(25 * s, 31 * s), QPointF(30 * s, 37 * s))
        painter.drawLine(QPointF(30 * s, 37 * s), QPointF(40 * s, 25 * s))
        if attention:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e8710a"))
            painter.drawEllipse(QRectF(40 * s, 2 * s, 22 * s, 22 * s))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def title_font():
    result = font() or QFont()
    result.setPointSize(17)
    result.setWeight(QFont.Weight.Bold)
    return result
