"""The Board's icon of each kind of part (board.KINDS): sandbox_ai_app/icons/board/KIND.svg, an own line drawing on a
24x24 grid in currentColor. Drawn with QtSvg in the colour asked for - the theme's title or text colour, so it reads
in every theme - once per kind, size, colour and device pixel ratio and then kept: a board with many parts paints
them from the cache, never renders an SVG per paint. A kind without a drawing of its own (a newer board's) gets the
neutral fallback.svg; an unreadable file never raises (fallback, else an empty pixmap).

The icon is decoration only: the part's title stays its accessible name, its tooltips are unchanged."""
from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

FOLDER = Path(__file__).resolve().parent / "icons" / "board"
FALLBACK = "fallback"
SIZE = 16  # the icon's size in a node's title band, the parts list and the menus
GAP = 6.0  # between the icon and the title
NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")  # a kind's key as a file name (anything else: the fallback)
KEPT = 512  # pixmaps kept at most (theme after theme, zoom after zoom: the old ones go)
SOURCE_LIMIT = 64 << 10  # bytes of one SVG read at most

_sources = {}  # name: the SVG's text ("" when unreadable)
_pixmaps = {}  # (name, size, colour, ratio): QPixmap
_icons = {}  # (name, size, colours): QIcon
renders = 0  # SVGs rendered so far (tests: a paint takes the cache)


def path_of(kind):
    """The SVG file of KIND (no check that it exists)."""
    return FOLDER / f"{kind}.svg"


def source(name):
    """The SVG text of NAME ("" when it has no readable file)."""
    text = _sources.get(name)
    if text is None:
        text = ""
        if NAME.fullmatch(str(name)):
            try:
                with open(path_of(name), "rb") as stream:
                    data = stream.read(SOURCE_LIMIT + 1)
                if len(data) <= SOURCE_LIMIT:
                    text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
        _sources[name] = text
    return text


def name_of(kind):
    """The drawing KIND is shown with: its own, else the fallback."""
    return kind if source(kind) else FALLBACK


def renderer(name, color):
    """A QSvgRenderer of NAME's drawing in COLOR (currentColor replaced), or None when Qt cannot read it."""
    text = source(name)
    if not text:
        return None
    made = QSvgRenderer(QByteArray(text.replace("currentColor", QColor(color).name()).encode("utf-8")))
    return made if made.isValid() else None


def ratio_of(value):
    """A device pixel ratio rounded to a quarter (zooming does not fill the cache with ratios a hair apart)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 1.0
    return min(8.0, max(0.25, round(value * 4) / 4)) if value == value else 1.0


def pixmap(kind, size=SIZE, color=None, ratio=1.0):
    """KIND's icon, SIZE logical pixels square, in COLOR (default: the theme's text colour), for a device of RATIO
    pixels per logical pixel: rendered once, then the same pixmap (a QPixmap is shared, never changed in place)."""
    global renders
    color = QColor(color if color is not None else theme.colors()["text"]).name()
    size, ratio = max(1, int(size)), ratio_of(ratio)
    name = name_of(kind)
    key = (name, size, color, ratio)
    found = _pixmaps.get(key)
    if found is not None:
        return found
    side = max(1, int(round(size * ratio)))
    made = QPixmap(side, side)
    made.fill(Qt.GlobalColor.transparent)
    drawing = renderer(name, color) or (renderer(FALLBACK, color) if name != FALLBACK else None)
    if drawing is not None:
        painter = QPainter(made)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        drawing.render(painter, QRectF(0, 0, side, side))
        painter.end()
        renders += 1
    made.setDevicePixelRatio(ratio)
    if len(_pixmaps) >= KEPT:
        _pixmaps.clear()
    _pixmaps[key] = made
    return made


def paint(painter, kind, top_left, size=SIZE, color=None):
    """KIND's icon drawn by PAINTER at TOP_LEFT (logical coordinates), sharp at the painter's device ratio and
    scale (a zoomed board)."""
    transform = painter.worldTransform()
    scale = max(abs(transform.m11()), abs(transform.m22())) or 1.0
    device = painter.device()
    ratio = (device.devicePixelRatioF() if device is not None else 1.0) * scale
    shown = pixmap(kind, size, color, ratio)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawPixmap(QRectF(QPointF(top_left), QPointF(top_left) + QPointF(size, size)), shown,
                       QRectF(0, 0, shown.width(), shown.height()))
    painter.restore()


def icon(kind, size=SIZE):
    """KIND's icon as a QIcon for lists and menus: in the theme's text colour, the selection's text colour when
    selected, the disabled colour when greyed out; sharp at ratios 1 and 2 and the screen's own."""
    from PySide6.QtGui import QGuiApplication
    colors = theme.colors()
    shades = (colors["text"], colors["highlight_text"], colors["disabled"])
    screen = QGuiApplication.primaryScreen() if QGuiApplication.instance() is not None else None
    ratios = sorted({1.0, 2.0, ratio_of(screen.devicePixelRatio() if screen is not None else 1.0)})
    key = (name_of(kind), size, shades, tuple(ratios))
    found = _icons.get(key)
    if found is None:
        found = QIcon()
        for mode, shade in zip((QIcon.Mode.Normal, QIcon.Mode.Selected, QIcon.Mode.Disabled), shades):
            for ratio in ratios:
                found.addPixmap(pixmap(kind, size, shade, ratio), mode)
        if len(_icons) >= KEPT:
            _icons.clear()
        _icons[key] = found
    return QIcon(found)
