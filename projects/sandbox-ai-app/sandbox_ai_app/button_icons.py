"""The icon buttons of the Sessions page's two rows ("Session" and "While it runs"): each action an own line drawing,
an inline SVG on a 24x24 grid in currentColor (no icon font, nothing downloaded), drawn with QtSvg in the theme's
text colour (a danger button's in the danger colour, a disabled one's in the disabled colour) and drawn again after a
change of theme. Rendered once per drawing, colour, size and device pixel ratio, then kept.

An IconButton shows its icon alone by default; its text is its tooltip's first line (above what the button does, or
why it is greyed out) and its accessible name; widgets.button_text gives its text to the right-click menus, which
keep their words. Settings › Appearance › "Show button texts beside the icons" (the setting button_texts) brings the
texts back beside the icons, at once in every window (set_texts_shown)."""
from __future__ import annotations

import html
import weakref

from PySide6.QtCore import QByteArray, QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QToolTip

from . import theme

SIZE = 18  # px: an icon in its button
KEPT = 512  # pixmaps kept at most (theme after theme: the old ones go)
_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" '
         'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">')
_TAIL = "</g></svg>"
_DOT = '<circle cx="{x}" cy="{y}" r="1.1" fill="currentColor" stroke="none"/>'
PLUS = '<path d="M20 9v6M17 12h6"/>'  # (at the right edge: added)
MINUS = '<path d="M17 12h6"/>'  # (taken away)


def _svg(*parts):
    return _HEAD + "".join(parts) + _TAIL


GLOBE = '<circle cx="9" cy="12" r="6.5"/><path d="M2.5 12h13"/><path d="M9 5.5c2.2 2.2 2.2 10.8 0 13M9 5.5c-2.2 2.2-2.2 10.8 0 13"/>'
CHIP = '<rect x="4" y="8" width="9" height="8" rx="1.5"/><path d="M6.5 5v3M10.5 5v3M6.5 16v3M10.5 16v3M1.5 10.5H4M1.5 13.5H4"/>'
FOLDER = '<path d="M2 7A1.5 1.5 0 0 1 3.5 5.5h3.5l2 2H14a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5H3.5A1.5 1.5 0 0 1 2 17.5z"/>'
SERVERS = ('<rect x="2.5" y="4.5" width="12" height="6" rx="1.5"/><rect x="2.5" y="13.5" width="12" height="6" rx="1.5"/>'
           + _DOT.format(x=5.5, y=7.5) + _DOT.format(x=5.5, y=16.5))
CUBE = '<path d="M9 3l6 3.5v9L9 19l-6-3.5v-9z"/><path d="M3 6.5L9 10l6-3.5M9 10v9"/>'

# Every action of the two rows: the page's key of the button -> its drawing.
SVGS = {
    # Session
    "attach": _svg('<rect x="2.5" y="7.5" width="13" height="13" rx="2"/><path d="M5.5 11.5l2.5 2-2.5 2M10 16h3"/>',
                   '<path d="M14 3.5h6.5V10M20.5 3.5L13 11"/>'),
    "attach-app": _svg('<rect x="2.5" y="3.5" width="19" height="17" rx="2"/><path d="M2.5 8h19"/>',
                       '<path d="M6.5 11.5l3 2.5-3 2.5M12 16.5h5"/>'),
    "shell": _svg('<path d="M4 6l6 6-6 6"/><path d="M12.5 18.5h8"/>'),
    "pause": _svg('<path d="M8.5 5.5v13M15.5 5.5v13"/>'),
    "resume": _svg('<path d="M7.5 5l11 7-11 7z"/>'),
    "delete": _svg('<path d="M4 6.5h16M9.5 6.5V4h5v2.5"/><path d="M6 6.5l1 13.5h10l1-13.5"/><path d="M10 10.5v6M14 10.5v6"/>'),
    "resume-with": _svg('<path d="M3.5 5l10 7-10 7z"/>', PLUS),
    "copy": _svg('<rect x="2.5" y="2.5" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="12" height="12" rx="2"/>'),
    "move": _svg('<rect x="2.5" y="5.5" width="10" height="13" rx="2"/><path d="M9 12h12.5M17.5 8l4 4-4 4"/>'),
    "export": _svg('<path d="M12 15V3.5M7 8.5l5-5 5 5"/><path d="M4 13.5V19a1.5 1.5 0 0 0 1.5 1.5h13A1.5 1.5 0 0 0 20 19v-5.5"/>'),
    "checkpoints": _svg('<path d="M3.5 12A8.5 8.5 0 1 0 6 6"/><path d="M3 3.5V8h4.5"/><path d="M12 7.5V12l3 2"/>'),
    "memory": _svg('<path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H19v15H7.5A2.5 2.5 0 0 0 5 20.5z"/><path d="M5 20.5A2.5 2.5 0 0 1 7.5 18"/>',
                   '<path d="M11 3v7l2-1.5 2 1.5V3"/>'),
    "files": _svg('<path d="M13.5 2.5H6A1.5 1.5 0 0 0 4.5 4v16A1.5 1.5 0 0 0 6 21.5h12a1.5 1.5 0 0 0 1.5-1.5V8.5z"/>',
                  '<path d="M13.5 2.5v6h6"/><path d="M8 13h8M8 17h5"/>'),
    "storage": _svg('<ellipse cx="12" cy="5.5" rx="7.5" ry="2.5"/><path d="M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 '
                    '7.5-2.5v-13"/><path d="M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5"/>'),
    "optimize": _svg('<path d="M15.5 2.5L11 11.5"/><path d="M6 11.5h9.5l1.5 9H4.5z"/><path d="M8.5 15v5.5M12.5 15v5.5"/>'),
    # While it runs
    "allow": _svg(GLOBE, PLUS),
    "revoke": _svg(GLOBE, MINUS),
    "allowed": _svg('<path d="M3.5 7l2 2 3.5-3.5M3.5 15.5l2 2 3.5-3.5"/><path d="M12 7.5h8.5M12 16h8.5"/>'),
    "log": _svg('<path d="M3.5 7.5h15M15 4l3.5 3.5L15 11"/><path d="M20.5 16.5h-15M9 13l-3.5 3.5L9 20"/>'),
    "add-device": _svg(CHIP, PLUS),
    "remove-device": _svg(CHIP, MINUS),
    "add-workspace": _svg(FOLDER, PLUS),
    "remove-workspace": _svg(FOLDER, MINUS),
    "add-mcp": _svg(SERVERS, PLUS),
    "remove-mcp": _svg(SERVERS, MINUS),
    "add-kits": _svg(CUBE, PLUS),
    "remove-kits": _svg(CUBE, MINUS),
    "limits": _svg('<path d="M3.5 6h9M17.5 6h3M3.5 12h3M11.5 12h9M3.5 18h11M19.5 18h1"/>',
                   '<circle cx="15" cy="6" r="2.2"/><circle cx="9" cy="12" r="2.2"/><circle cx="17" cy="18" r="2.2"/>'),
    "rules": _svg('<path d="M19.5 10A8 8 0 0 0 5.5 7.5"/><path d="M4.5 3.5v4.5H9"/><path d="M4.5 14a8 8 0 0 0 14 2.5"/>',
                  '<path d="M19.5 20.5V16H15"/><path d="M9 12h6"/>'),
    "browser": _svg('<rect x="2.5" y="3.5" width="19" height="17" rx="2"/><path d="M2.5 8.5h19"/>',
                    _DOT.format(x=5.5, y=6), _DOT.format(x=8.5, y=6), '<circle cx="12" cy="14.5" r="3.5"/>'),
    "credentials": _svg('<circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5l9-9M16 7l3 3M13.5 9.5l2 2"/>'),
    "stop-all": _svg('<rect x="7.5" y="7.5" width="13" height="13" rx="2"/><path d="M3.5 16.5V5.5a2 2 0 0 1 2-2h11"/>'),
}

ICON_ONLY = "QPushButton { padding-left: 5px; padding-right: 5px; min-width: 0px; }"
_pixmaps = {}  # (key, size, colour, ratio): QPixmap
_SHOWN = [False]  # the setting button_texts: the texts beside the icons
_BUTTONS = weakref.WeakSet()


def pixmap(key, size=SIZE, color=None, ratio=1.0):
    """The drawing KEY, SIZE logical pixels square, in COLOR (default: the theme's text colour), for RATIO device
    pixels per logical pixel; an unknown key or a drawing Qt cannot read: an empty (transparent) pixmap."""
    color = QColor(color if color is not None else theme.colors()["text"]).name()
    size, ratio = max(1, int(size)), max(0.25, min(8.0, round(float(ratio) * 4) / 4))
    found = _pixmaps.get((key, size, color, ratio))
    if found is not None:
        return found
    side = max(1, int(round(size * ratio)))
    made = QPixmap(side, side)
    made.fill(Qt.GlobalColor.transparent)
    drawing = QSvgRenderer(QByteArray(SVGS.get(key, "").replace("currentColor", color).encode("utf-8")))
    if drawing.isValid():
        painter = QPainter(made)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        drawing.render(painter, QRectF(0, 0, side, side))
        painter.end()
    made.setDevicePixelRatio(ratio)
    if len(_pixmaps) >= KEPT:
        _pixmaps.clear()
    _pixmaps[(key, size, color, ratio)] = made
    return made


def icon(key, tint, size=SIZE):
    """KEY's QIcon: in TINT, greyed in the theme's disabled colour; sharp at ratios 1 and 2."""
    made = QIcon()
    disabled = theme.colors()["disabled"]
    for ratio in (1.0, 2.0):
        made.addPixmap(pixmap(key, size, tint, ratio), QIcon.Mode.Normal)
        made.addPixmap(pixmap(key, size, disabled, ratio), QIcon.Mode.Disabled)
    return made


def texts_shown():
    return _SHOWN[0]


def set_texts_shown(on):
    """The texts beside the icons (ON) or the icons alone, at once on every IconButton there is (and those made
    later)."""
    on = bool(on)
    if on == _SHOWN[0]:
        return
    _SHOWN[0] = on
    areas = {}
    for button in list(_BUTTONS):
        try:
            button.apply_text()
        except RuntimeError:  # its C++ side is gone
            _BUTTONS.discard(button)
            continue
        # Its rows laid out again now, up to the page's scroll area (which fits the page again below).
        widget = button.parentWidget()
        while widget is not None and not isinstance(widget, QScrollArea):
            if widget.layout() is not None:
                widget.layout().activate()
            widget = widget.parentWidget()
        if widget is not None:
            areas[id(widget)] = widget
    # A row of icons is lower than one of texts: Qt grows the page's scroll area by itself, but would keep the room
    # of the texts after they went.
    for area in areas.values():
        refit(area)
        QTimer.singleShot(0, area, lambda a=area: refit(a))  # (and once what is still pending is laid out)


def refit(area):
    """AREA (a QScrollArea) fits its widget to its room and its widget's height for that width again."""
    QApplication.sendEvent(area, QEvent(QEvent.Type.LayoutRequest))


def rich(text):
    """TEXT as the body of a tooltip: kept when it is one already (safe_tooltip's), else escaped."""
    text = str(text or "")
    return text if text.startswith("<p style=") else ("<p style='white-space:pre-wrap'>"
                                                      + html.escape(text, quote=False) + "</p>") if text else ""


class IconButton(QPushButton):
    """A push button of the Sessions page's rows: KEY's icon (SVGS), its TEXT shown beside it only while the setting
    button_texts is on. TEXT is its label (setText changes it), NAME its accessible name (default: TEXT without
    "…"), ROLE its style role (danger). toolTip() is what it does or why it is greyed out, as on any button; the tooltip shown starts with the
    label while the text is hidden (shown_tooltip)."""

    def __init__(self, key, text, name=None, role=None, parent=None):
        super().__init__(parent)
        self.icon_key, self.label = key, str(text)
        if role:
            self.setProperty("role", role)  # (danger: drawn in the danger colour)
        self.setAccessibleName(name or self.label.rstrip("…"))
        self.setIconSize(QSize(SIZE, SIZE))
        _BUTTONS.add(self)
        self.paint_icon()
        self.apply_text()

    # -- the text --

    def setText(self, text):  # noqa: N802 - the label changes (Secrets and logins on HOST…); shown when texts are
        self.label = str(text)
        self.apply_text()

    def apply_text(self):
        super().setText(self.label if _SHOWN[0] else "")
        # The icon alone: little room beside it (the themes give a text 14 px or more on each side).
        sheet = "" if _SHOWN[0] else ICON_ONLY
        if self.styleSheet() != sheet:
            self.setStyleSheet(sheet)
        self.updateGeometry()

    @property
    def menu_text(self):
        """The text of its entry in a right-click menu (widgets.button_text)."""
        return self.label

    def shown_tooltip(self):
        """The tooltip as shown: with the icon alone, the label first (bold), then what toolTip() says."""
        if _SHOWN[0]:
            return self.toolTip()
        head = "<p style='white-space:pre-wrap'><b>" + html.escape(self.label, quote=False) + "</b></p>"
        return head + rich(self.toolTip())

    # -- the icon --

    def tint(self):
        colors = theme.colors()
        return colors["danger"] if self.property("role") == "danger" else colors["text"]

    def paint_icon(self):
        self.setIcon(icon(self.icon_key, self.tint()))

    def changeEvent(self, event):  # noqa: N802
        """Another look (theme): drawn again in its colours."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self.paint_icon()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and not _SHOWN[0]:
            QToolTip.showText(event.globalPos(), self.shown_tooltip(), self)
            return True
        return super().event(event)
