"""The Help's screenshots: a page of the window as it is now, with numbered callouts whose arrows point at its real
controls, drawn with QPainter. The controls are found as the tours find them - by the page's attribute
(tours.control names it), at the position the widget has now, never by fixed pixels.

A screenshot is made on demand in the running app: in the human's theme, language, fonts and version, of their own
window (nothing of it is written anywhere or shipped). A control that is not on screen now (on another tab, folded
away, not made yet) gets no callout in the picture; the legend still names it, and "Show me" brings it into view.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

BADGE = 13.0  # the callout's circle: radius in logical pixels
GAP = 30.0  # how far outside its control a callout sits
MARGIN = 70  # what is kept around the controls when a tall page is cut to them


@dataclass
class Mark:
    number: int
    target: str
    rect: QRectF  # the control, in the picture's logical pixels
    badge: QPointF  # the centre of the numbered circle
    tip: QPointF  # where the arrow ends (inside rect)


@dataclass
class Shot:
    image: QImage  # at the device pixel ratio asked for; its logical size is width x height
    width: int
    height: int
    marks: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # targets not on screen now
    page_width: int = 0  # the page's own width (a picture is never wider)


def readable_on(fill):
    """Black or white, whichever is easier to read on FILL (WCAG contrast)."""
    from .theme import contrast
    return QColor("#ffffff") if contrast("#ffffff", fill.name()) >= contrast("#000000", fill.name()) else \
        QColor("#000000")


def colours(palette=None):
    """(callout colour, its text, halo) from the theme: the highlight, made strong enough to stand out."""
    from .theme import contrast
    palette = palette or QApplication.palette()
    accent = QColor(palette.color(palette.ColorRole.Highlight))
    base = QColor(palette.color(palette.ColorRole.Base))
    if contrast(accent.name(), base.name()) < 3.0:  # a pale highlight: the callouts in a strong orange instead
        accent = QColor("#c2410c") if base.lightness() > 128 else QColor("#fb923c")
    halo = QColor("#ffffff") if base.lightness() > 128 else QColor("#0b0d10")  # keeps them apart from the page
    return accent, readable_on(accent), halo


def widget_rects(origin, page, targets):
    """{target: QRect in ORIGIN's coordinates} of the TARGETS (attributes of PAGE) that are on screen in ORIGIN now:
    shown, inside it, and inside every scrolled view around them (a control scrolled out of view is not)."""
    from PySide6.QtWidgets import QAbstractScrollArea
    found = {}
    for target in targets:
        widget = getattr(page, target, None)
        if not isinstance(widget, QWidget) or not (widget is origin or origin.isAncestorOf(widget)):
            continue
        if widget is not origin and not widget.isVisibleTo(origin):
            continue
        rect = QRect(widget.mapTo(origin, QPoint(0, 0)), widget.size())
        child, parent = widget, widget.parentWidget()
        while parent is not None and parent is not origin:
            area = parent.parentWidget()
            if isinstance(area, QAbstractScrollArea) and area.viewport() is parent:
                rect = rect.intersected(QRect(parent.mapTo(origin, QPoint(0, 0)), parent.size()))
            child, parent = parent, parent.parentWidget()
        rect = rect.intersected(origin.rect())
        if rect.width() > 4 and rect.height() > 4:
            found[target] = rect
    return found


@contextlib.contextmanager
def scrolled_to(origin, page, targets):
    """While none of TARGETS is on screen in ORIGIN but one is shown, scrolled out of its view: the views around the
    first such one scrolled to it for the picture, and back right after."""
    from PySide6.QtWidgets import QScrollArea
    moved = []
    if not widget_rects(origin, page, targets):
        for target in targets:
            widget = getattr(page, target, None)
            if not isinstance(widget, QWidget) or not origin.isAncestorOf(widget) or not widget.isVisibleTo(origin):
                continue
            parent = widget.parentWidget()
            while parent is not None and parent is not origin:
                if isinstance(parent, QScrollArea):
                    bars = (parent.horizontalScrollBar(), parent.verticalScrollBar())
                    moved.append((bars, [bar.value() for bar in bars]))
                    parent.ensureWidgetVisible(widget, 40, 60)
                parent = parent.parentWidget()
            if moved:
                QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
                break
    try:
        yield
    finally:
        for bars, values in reversed(moved):
            for bar, value in zip(bars, values):
                bar.setValue(value)


def grab_page(window, key, targets=()):
    """(picture, its size, {target: QRect}) of page KEY of WINDOW and its controls TARGETS on screen (widget_rects,
    in the picture's coordinates): with the main window, its workspace - the
    page with its panels, as the navigation shows them (MainWindow.showing) -; else the page alone, laid out at
    the size the window gives its pages and rendered without being shown on screen."""
    try:
        page = getattr(window, key)
    except AttributeError:
        return None, None, {}
    if not isinstance(page, QWidget):
        return None, None, {}
    workspace = getattr(window, "workspace", None)
    if isinstance(workspace, QWidget) and hasattr(window, "showing"):
        with window.showing(key), scrolled_to(workspace, page, targets):
            return workspace.grab(), workspace.size(), widget_rects(workspace, page, targets)
    stack = getattr(window, "pages", None)
    frame = stack.frame_of(page) if stack is not None and hasattr(stack, "frame_of") else None
    frame = frame or page
    hidden = not frame.isVisible()
    if hidden:
        if stack is not None and stack.width() > 100:
            frame.setGeometry(stack.rect())
        frame.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        frame.show()
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
    try:
        return page.grab(), page.size(), widget_rects(page, page, targets)
    finally:
        if hidden:
            frame.hide()
            frame.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)


def crop_box(size, rects):
    """The part of a page of SIZE to show: its whole width, and in height the controls RECTS with MARGIN around
    them (no less than half the width, so the picture keeps its context)."""
    width, height = size.width(), size.height()
    if not rects:
        return QRect(0, 0, width, min(height, int(width * 0.75)))
    top = max(0, min(r.top() for r in rects) - MARGIN)
    bottom = min(height, max(r.bottom() for r in rects) + MARGIN)
    least = min(height, int(width * 0.5))
    if bottom - top < least:
        grow = least - (bottom - top)
        top = max(0, top - grow // 2)
        bottom = min(height, top + least)
        top = max(0, bottom - least)
    return QRect(0, top, width, bottom - top)


def capture(window, key, targets, width=680, ratio=1.0, palette=None):
    """The Shot of page KEY with a numbered callout for each of TARGETS on screen there, WIDTH logical pixels wide
    (never wider than the page) at device pixel RATIO; None when the page cannot be shown."""
    pixmap, size, rects = grab_page(window, key, targets)
    if pixmap is None or pixmap.isNull():
        return None
    box = crop_box(size, list(rects.values()))
    scale_from = pixmap.devicePixelRatio() or 1.0
    piece = pixmap.copy(QRect(int(box.x() * scale_from), int(box.y() * scale_from), int(box.width() * scale_from),
                              int(box.height() * scale_from)))
    width = max(160, min(int(width), box.width()))
    factor = width / box.width()
    height = max(1, round(box.height() * factor))
    image = piece.toImage().scaled(int(width * ratio), int(height * ratio), Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    placed = []
    for number, target in enumerate((t for t in targets if t in rects), start=1):
        r = rects[target]
        placed.append((number, target, QRectF((r.x() - box.x()) * factor, (r.y() - box.y()) * factor,
                                              r.width() * factor, r.height() * factor)))
    shot = annotate(image, width, height, placed, palette)
    shot.missing = [t for t in targets if t not in rects]
    shot.page_width = box.width()
    return shot


def nearest_inside(rect, point, inset=6.0):
    """The point of RECT nearest to POINT, moved INSET into it (never past its middle)."""
    inner = rect.adjusted(min(inset, rect.width() / 3), min(inset, rect.height() / 3),
                          -min(inset, rect.width() / 3), -min(inset, rect.height() / 3))
    return QPointF(min(max(point.x(), inner.left()), inner.right()), min(max(point.y(), inner.top()), inner.bottom()))


def badge_spot(rect, width, height, taken, avoid):
    """Where the callout of RECT goes: just outside it (above, left, below, right) where the circle fits in the
    picture and touches no other callout; inside its top-left corner for a control as large as the picture."""
    r = BADGE + 2
    x_in = min(rect.left() + 34, rect.center().x())
    candidates = [QPointF(x_in, rect.top() - GAP), QPointF(rect.left() - GAP, rect.center().y()),
                  QPointF(x_in, rect.bottom() + GAP), QPointF(rect.right() + GAP, rect.center().y()),
                  QPointF(rect.right() - 34, rect.top() - GAP), QPointF(rect.left() - GAP, rect.top() - GAP * 0.7),
                  QPointF(rect.right() + GAP, rect.top() - GAP * 0.7), QPointF(rect.left() + 40, rect.top() + 40),
                  QPointF(rect.center().x(), rect.top() + 40), QPointF(rect.right() - 40, rect.top() + 40)]

    def fits(point):
        if not (r <= point.x() <= width - r and r <= point.y() <= height - r):
            return False
        if any(math.hypot(point.x() - other.x(), point.y() - other.y()) < 2 * r + 6 for other in taken):
            return False
        return True

    def clear(point):
        circle = QRectF(point.x() - r, point.y() - r, 2 * r, 2 * r)
        return not any(circle.intersects(other) for other in avoid)
    for point in candidates:
        if fits(point) and clear(point):
            return point
    for point in candidates:
        if fits(point):
            return point
    clamp = QPointF(min(max(rect.left() + 20, r), width - r), min(max(rect.top() + 20, r), height - r))
    return clamp


def annotate(image, width, height, placed, palette=None):
    """IMAGE (logical WIDTH x HEIGHT) with each (number, target, rect) of PLACED outlined, its numbered circle beside
    it and an arrow from the circle into the control. Returns the Shot."""
    accent, on_accent, halo = colours(palette)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    marks, taken = [], []
    rects = [rect for _n, _t, rect in placed]
    for number, target, rect in placed:
        others = [other for other in rects if other is not rect]
        spot = badge_spot(rect, width, height, taken, others)
        taken.append(spot)
        tip = nearest_inside(rect, spot)
        if rect.contains(spot):  # inside a large control: the arrow points further in, towards its middle
            towards = QPointF(rect.center().x() - spot.x(), rect.center().y() - spot.y())
            length = math.hypot(towards.x(), towards.y()) or 1.0
            reach = min(36.0, length)
            tip = QPointF(spot.x() + towards.x() / length * reach, spot.y() + towards.y() / length * reach)
            tip = nearest_inside(rect, tip)
        marks.append(Mark(number, target, rect, spot, tip))
    for mark in marks:  # outlines first, then arrows, then the circles over everything
        outline = mark.rect.adjusted(-2, -2, 2, 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(halo, 4.5))
        painter.drawRoundedRect(outline, 5, 5)
        painter.setPen(QPen(accent, 2.2))
        painter.drawRoundedRect(outline, 5, 5)
    for mark in marks:
        draw_arrow(painter, mark.badge, mark.tip, accent, halo)
    for mark in marks:
        draw_badge(painter, mark.badge, mark.number, accent, on_accent, halo)
    painter.end()
    return Shot(image, width, height, marks)


def draw_arrow(painter, start, end, colour, halo):
    """A line from the edge of the circle at START to END with a head at END, on a halo that keeps it visible on any
    background."""
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length = math.hypot(dx, dy)
    if length < BADGE + 6:
        return
    ux, uy = dx / length, dy / length
    begin = QPointF(start.x() + ux * (BADGE + 1), start.y() + uy * (BADGE + 1))
    head = 9.0
    base = QPointF(end.x() - ux * head, end.y() - uy * head)
    wing = QPointF(-uy * head * 0.55, ux * head * 0.55)
    triangle = QPolygonF([end, QPointF(base.x() + wing.x(), base.y() + wing.y()),
                          QPointF(base.x() - wing.x(), base.y() - wing.y())])
    for pen_colour, line_width in ((halo, 5.5), (colour, 2.5)):
        painter.setPen(QPen(pen_colour, line_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(pen_colour)
        painter.drawLine(begin, base)
        painter.drawPolygon(triangle)


def draw_badge(painter, centre, number, fill, text, ring):
    """The numbered circle at CENTRE."""
    painter.setPen(QPen(ring, 2.2))
    painter.setBrush(fill)
    painter.drawEllipse(centre, BADGE, BADGE)
    font = QFont(QApplication.font())
    font.setBold(True)
    font.setPixelSize(int(BADGE * 1.15))
    painter.setFont(font)
    painter.setPen(text)
    path_rect = QRectF(centre.x() - BADGE, centre.y() - BADGE, 2 * BADGE, 2 * BADGE)
    painter.drawText(path_rect, int(Qt.AlignmentFlag.AlignCenter), str(number))


def badge_image(number, size=24, ratio=1.0, palette=None):
    """The numbered circle alone (the legend's), SIZE logical pixels at device pixel RATIO."""
    accent, on_accent, halo = colours(palette)
    image = QImage(int(size * ratio), int(size * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.setDevicePixelRatio(ratio)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    draw_badge(painter, QPointF(size / 2, size / 2), number, accent, on_accent, halo)
    painter.end()
    return image

