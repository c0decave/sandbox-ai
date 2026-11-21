"""The System monitor's charts and gauges (monitor_page.py), drawn with QPainter (the app ships without Qt Charts).

TimeChart: values over a time window - lines, filled areas, areas stacked on each other (every run's CPU or memory)
and limits (dashed, following the limit over time: a raised one steps up) - with the latest values beside the
title, the times at the edges, the line broken where samples are missing (a gap: a stopped run, a host asleep) and
the exact values at the time under the mouse as its tooltip. Gauge: a bar of what is used now of its limit, in the
colours the warnings use (warn at the warning's share, bad where the kernel steps in). CellSpark: a sparkline in a
cell of the run table (CPU, memory) beside its text, scaled to the run's limit.

The samples are points: {"t": seconds, KEY: value, ...} with the keys of `sandbox-ai top` (sample) and of `sandbox-ai
stats usage --series` (detail: monitor.DETAIL_KEYS, HOST_DETAIL_KEYS). Ring keeps the samples of each look at `top`
per run and the host, bounded; merged() puts them after the history's; total() and joined() put several runs'
points into the same time slots (a project's sum, every run stacked). Colours come from the theme at each paint:
a change of the theme is followed at once."""
from __future__ import annotations

from collections import deque
import math
import statistics
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QStyledItemDelegate, QToolTip, QWidget

from . import theme
from .i18n import tr
from .widgets import safe_tooltip

SLOW_SHARE = 0.92        # agent_sandbox.MEMORY_HIGH: memory.high at this share of the limit - slowed down above it
MEMORY_NEAR = 0.90       # monitor.MEMORY_NEAR: the memory warning starts here
PIDS_NEAR = 0.90         # monitor.PIDS_NEAR: the processes warning starts here
THROTTLE_RATIO = 50.0    # monitor.THROTTLE_RATIO: held back by the CPU limit in this percentage of the periods
HOST_MEMORY_LOW = 0.10   # monitor.HOST_MEMORY_LOW: the host's low-memory warning below this share free
RING_POINTS = 720        # samples kept per run and for the host (2 hours at 10 s)
RING_KEYS = 256          # runs kept at most (those no longer listed are let go at each look)
GAP = 2.5                # a step this many times the usual one is a gap: the line breaks there
STACKED = 8              # runs stacked on their own in All runs; the others as one
SPARK_WIDTH = 44         # px of a cell's sparkline
SLOTS = 240              # time slots of a window (monitor.SERIES_POINTS): several runs' points are put into them
WINDOW_SECONDS = {"15m": 900, "1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
SERIES_KEYS = ("cpu", "mem", "io", "net")                 # monitor.downsample's columns after the time
HOST_SERIES_KEYS = ("cpu_pct", "mem_avail", "io", "load1")  # monitor.host_downsample's


def num(value):
    """VALUE when it is a finite number (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


# -- points --

def from_detail(detail):
    """The points of a detail ({"t": [...], KEY: [...]}, stats usage --series); [] for anything else."""
    if not isinstance(detail, dict) or not isinstance(detail.get("t"), list):
        return []
    times = detail["t"]
    columns = {key: values for key, values in detail.items()
               if key != "t" and isinstance(key, str) and isinstance(values, list) and len(values) == len(times)}
    found = []
    for index, when in enumerate(times):
        if num(when) is None:
            continue
        point = {"t": float(when)}
        for key, values in columns.items():
            if num(values[index]) is not None:
                point[key] = values[index]
        found.append(point)
    return sorted(found, key=lambda point: point["t"])


def from_series(rows, keys=SERIES_KEYS):
    """The points of a series ([[t, a, b, c, d], ...]: an older host's, without a detail)."""
    found = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) == len(keys) + 1 and num(row[0]) is not None:
            point = {"t": float(row[0])}
            point.update({key: value for key, value in zip(keys, row[1:]) if num(value) is not None})
            found.append(point)
    return sorted(found, key=lambda point: point["t"])


def merged(history, recent, since):
    """HISTORY's points from SINCE on, then RECENT's after its last (the looks at top since the history was read)."""
    found = [point for point in history if point["t"] >= since]
    last = found[-1]["t"] if found else float("-inf")
    return found + [point for point in recent if point["t"] > last and point["t"] >= since]


def slots_of(points, step, how=None):
    """{slot: {key: value}} of POINTS in slots of STEP seconds: the average of each key in a slot (HOW: {key: "max"}
    takes the highest instead)."""
    sums, counts = {}, {}
    for point in points:
        slot = int(point["t"] // step)
        values, seen = sums.setdefault(slot, {}), counts.setdefault(slot, {})
        for key, value in point.items():
            if key == "t" or num(value) is None:
                continue
            if (how or {}).get(key) == "max":
                values[key] = max(values.get(key, value), value)
                seen[key] = 1
            else:
                values[key] = values.get(key, 0) + value
                seen[key] = seen.get(key, 0) + 1
    return {slot: {key: value / counts[slot][key] for key, value in values.items()} for slot, values in sums.items()}


MAX_KEYS = {"mem": "max", "pids": "max", "mem_lim": "max", "pids_lim": "max", "cpu_lim": "max"}


def total(series, step):
    """One run's points of several (SERIES: [points, ...]) in slots of STEP: their values added up (a project's); a
    limit only where every run of the slot has one."""
    each = [slots_of(points, step, MAX_KEYS) for points in series]
    found = []
    for slot in sorted({slot for slots in each for slot in slots}):
        present = [slots[slot] for slots in each if slot in slots]
        point = {"t": float((slot + 1) * step)}
        for key in {key for values in present for key in values}:
            if key.endswith("_lim") and any(key not in values for values in present):
                continue
            point[key] = sum(values.get(key, 0) for values in present)
        found.append(point)
    return found


def joined(series, key, step):
    """SERIES ({name: points}) as one list of points {"t", name: KEY's value} in slots of STEP (every run stacked)."""
    slots = {}
    for name, points in series.items():
        for slot, values in slots_of(points, step, MAX_KEYS).items():
            if key in values:
                slots.setdefault(slot, {})[name] = values[key]
    return [{"t": float((slot + 1) * step), **values} for slot, values in sorted(slots.items())]


class Ring:
    """The samples of the looks at `top`: per KEY (a run, "host") the last RING_POINTS, at most RING_KEYS keys."""

    def __init__(self, points=RING_POINTS, keys=RING_KEYS):
        self.size, self.most, self.rings = points, keys, {}

    def add(self, key, sample, now=None):
        if not isinstance(sample, dict):
            return
        when = num(sample.get("t"))
        when = float(time.time() if now is None else now) if when is None else float(when)
        ring = self.rings.get(key)
        if ring is None:
            if len(self.rings) >= self.most:
                return
            ring = self.rings[key] = deque(maxlen=self.size)
        if ring and ring[-1]["t"] >= when:
            return  # (the same sample again: the collector had not looked since)
        ring.append({"t": when, **{k: v for k, v in sample.items() if k != "t" and num(v) is not None}})

    def seed(self, key, spark):
        """A run's spark ([[t, cpu, mem, io, net], ...]: its last 15 minutes) when nothing is kept of it yet."""
        if key in self.rings:
            return
        for point in from_series(spark):
            self.add(key, {"t": point["t"], **{k: point[k] for k in ("cpu", "mem") if k in point}})

    def points(self, key):
        return list(self.rings.get(key) or ())

    def keep(self, keys):
        """Only KEYS kept (the runs listed now, the host)."""
        for key in [key for key in self.rings if key not in keys]:
            del self.rings[key]

    def clear(self):
        self.rings.clear()


# -- colours --

def token(name):
    """A colour by NAME: a status ("ok", "warn", "bad", "muted"), a theme colour ("accent", "text"), or "series3"."""
    if name.startswith("series"):
        return series_color(int(name[6:] or 0))
    if name in ("ok", "warn", "bad", "muted"):
        return QColor(theme.color(name))
    return QColor(theme.colors().get(name) or "#808080")


def series_color(index):
    """The INDEXth of a set of colours apart from each other, readable on the theme's background: the accent first,
    then its hue turned on."""
    accent = QColor(theme.colors().get("accent") or "#2563eb")
    if index == 0:
        return accent
    hue = accent.hslHueF()
    hue = 0.6 if hue < 0 else hue
    dark = theme.is_dark()
    return QColor.fromHslF((hue + index * 0.382) % 1.0, 0.65, 0.66 if dark else 0.38)


def level_of(share, warn, bad, warned=None):
    """"bad", "warn", "ok" of SHARE (0..1, None: "muted") against the shares WARN and BAD; WARNED (a warning's level
    of the run now: "warning", "error") raises it."""
    found = "muted" if share is None else "bad" if share >= bad else "warn" if share >= warn else "ok"
    if warned == "error":
        return "bad"
    if warned and found in ("ok", "muted"):
        return "warn"
    return found


# -- the chart --

class Line:
    """What a chart draws: LABEL, VALUE(point) -> number or None, COLOUR (a token()), KIND: "line", "area",
    "stack" (on the stacks before it) or "limit" (dashed)."""

    __slots__ = ("label", "value", "colour", "kind")

    def __init__(self, label, value, colour, kind="line"):
        self.label, self.value, self.colour, self.kind = label, value, colour, kind
        if isinstance(value, str):
            self.value = lambda point, key=value: num(point.get(key))


class TimeChart(QWidget):
    """Lines over time (see the module). set_data(title, points, lines, unit, span): UNIT(value) is a value's text,
    SPAN (start, end) the window shown (default: the points' own)."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.setAccessibleName(name)
        self.setMinimumSize(140, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.title, self.points, self.lines, self.unit, self.span = name, [], [], str, None
        self.hover, self.painted = None, set()
        self.empty = tr("No samples in this window yet.")

    def sizeHint(self):  # noqa: N802 - Qt's name
        return QSize(360, 150)

    def set_data(self, title, points, lines, unit=str, span=None, empty=None):
        self.title, self.points, self.lines, self.unit, self.span = title, list(points or []), list(lines), unit, span
        self.empty = empty or tr("No samples in this window yet.")
        self.hover = None
        self.setAccessibleDescription(self.summary())
        self.update()

    # -- what is drawn where --

    def summary(self):
        """The title and the latest value of each line, in words (the accessible description)."""
        latest = [f"{line.label} {self.unit(value)}" for line, value in self.latest()]
        return f"{self.title}: " + (", ".join(latest) if latest else self.empty)

    def latest(self):
        found = []
        for line in self.lines:
            values = [value for value in (line.value(point) for point in self.points) if value is not None]
            if values:
                found.append((line, values[-1]))
        return found

    def plot_rect(self):
        rect = QRectF(self.rect())
        left = 54 if rect.width() >= 240 else 6
        bottom = 18 if rect.height() >= 110 else 4
        return rect.adjusted(left, 22, -8, -bottom)

    def window(self):
        if self.span:
            return float(self.span[0]), float(self.span[1])
        if not self.points:
            return 0.0, 1.0
        start, end = self.points[0]["t"], self.points[-1]["t"]
        return (start - 30, end + 30) if end <= start else (start, end)

    def stacks(self):
        """{line index: [(t, below, above)]} of the stacked lines: each on the ones before it."""
        found, below = {}, [0.0] * len(self.points)
        for index, line in enumerate(self.lines):
            if line.kind != "stack":
                continue
            rows = []
            for position, point in enumerate(self.points):
                value = line.value(point) or 0.0
                rows.append((point["t"], below[position], below[position] + value))
                below[position] += value
            found[index] = rows
        return found

    def top(self):
        highest = 0.0
        for index, rows in self.stacks().items():
            highest = max([highest] + [above for _, _, above in rows])
        for line in self.lines:
            if line.kind != "stack":
                highest = max([highest] + [value for value in (line.value(point) for point in self.points)
                                           if value is not None])
        return highest * 1.1 or 1.0

    def gap(self):
        """The step beyond which two points are apart (a gap): GAP times the usual one."""
        steps = [b["t"] - a["t"] for a, b in zip(self.points, self.points[1:]) if b["t"] > a["t"]]
        return GAP * statistics.median(steps) if steps else float("inf")

    def x_of(self, when, area=None):
        area = area or self.plot_rect()
        start, end = self.window()
        return area.left() + area.width() * (when - start) / max(1e-9, end - start)

    def y_of(self, value, top, area=None):
        area = area or self.plot_rect()
        return area.bottom() - area.height() * min(max(value, 0.0), top) / top

    def segments(self, index):
        """The pieces of line INDEX between its gaps (and where it has no value): [[QPointF, ...], ...]."""
        area, top, gap = self.plot_rect(), self.top(), self.gap()
        line = self.lines[index]
        stacked = self.stacks().get(index)
        found, piece, before = [], [], None
        for position, point in enumerate(self.points):
            value = stacked[position][2] if stacked else line.value(point)
            if value is None or (before is not None and point["t"] - before > gap):
                if piece:
                    found.append(piece)
                piece = []
            if value is not None:
                piece.append(QPointF(self.x_of(point["t"], area), self.y_of(value, top, area)))
            before = point["t"]
        if piece:
            found.append(piece)
        return found

    def nearest(self, x):
        """The index of the point nearest to X (px), or None."""
        if not self.points:
            return None
        area = self.plot_rect()
        return min(range(len(self.points)), key=lambda i: abs(self.x_of(self.points[i]["t"], area) - x))

    def text_at(self, x):
        """The time under X and every line's exact value there (the tooltip)."""
        index = self.nearest(x)
        if index is None:
            return ""
        point = self.points[index]
        start, end = self.window()
        shape = "%d.%m. %H:%M" if end - start >= 86400 else "%H:%M:%S"
        rows = [time.strftime(shape, time.localtime(point["t"]))]
        for line in self.lines:
            value = line.value(point)
            if value is not None:
                rows.append(f"{line.label}: {self.unit(value)}")
        return "\n".join(rows)

    # -- Qt --

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            text = self.text_at(event.pos().x())
            if text:
                QToolTip.showText(event.globalPos(), safe_tooltip(text), self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        index = self.nearest(event.position().x())
        if index != self.hover:
            self.hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.hover = None
        self.update()
        super().leaveEvent(event)

    def pen(self, painter, colour, width=1.0, style=Qt.PenStyle.SolidLine):
        self.painted.add(QColor(colour).name())
        painter.setPen(QPen(colour, width, style))

    def paintEvent(self, _event):  # noqa: N802
        self.painted = set()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = token("text")
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.85))
        painter.setFont(small)
        self.pen(painter, text)
        width = self.width()
        # The latest values beside the title, the lines before the limits, as many as fit in its right half.
        latest = self.latest() if width >= 300 else []
        legend, room, legend_width = [], width * 0.55 - 8, 0
        metrics = painter.fontMetrics()
        for line, value in sorted(latest, key=lambda entry: entry[0].kind == "limit"):
            words = f"{line.label} {self.unit(value)}"
            needed = metrics.horizontalAdvance(words) + 12
            if legend_width + needed > room:
                break
            legend.append((line, words, needed))
            legend_width += needed
        x = width - 8 - legend_width
        for line, words, needed in legend:
            self.pen(painter, token(line.colour))
            painter.drawText(QRectF(x + 12, 2, needed, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             words)
            x += needed
        self.pen(painter, text)
        legend_width += 8 if legend else 0
        title = painter.fontMetrics().elidedText(self.title, Qt.TextElideMode.ElideRight,
                                                 int(max(20, width - 16 - legend_width)))
        painter.drawText(QRectF(8, 2, width - 16, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         title)
        area = self.plot_rect()
        if not self.latest():  # (no line has a value: an older host's, nothing recorded yet)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty)
            return
        grid = QColor(text)
        grid.setAlpha(60)
        self.pen(painter, grid)
        painter.drawRect(area)
        top = self.top()
        if area.left() > 20:
            self.pen(painter, text)
            for value, where in ((top / 1.1, self.y_of(top / 1.1, top, area)), (0, area.bottom())):
                painter.drawText(QRectF(0, where - 8, area.left() - 4, 16),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.unit(value))
            self.pen(painter, grid, 1, Qt.PenStyle.DotLine)
            middle = self.y_of(top / 2.2, top, area)
            painter.drawLine(QPointF(area.left(), middle), QPointF(area.right(), middle))
        painter.save()
        painter.setClipRect(area.adjusted(-1, -1, 1, 1))
        stacks = self.stacks()
        for index, line in enumerate(self.lines):
            colour = token(line.colour)
            pieces = self.segments(index)
            if line.kind in ("area", "stack"):
                tint = QColor(colour)
                tint.setAlpha(90 if line.kind == "area" else 150)
                for piece in pieces:
                    path = QPainterPath()
                    if line.kind == "stack":
                        rows = {self.x_of(t, area): below for t, below, _ in stacks[index]}
                        path.moveTo(piece[0])
                        for spot in piece[1:]:
                            path.lineTo(spot)
                        for spot in reversed(piece):
                            path.lineTo(spot.x(), self.y_of(rows.get(spot.x(), 0.0), top, area))
                    else:
                        path.moveTo(piece[0].x(), area.bottom())
                        for spot in piece:
                            path.lineTo(spot)
                        path.lineTo(piece[-1].x(), area.bottom())
                    path.closeSubpath()
                    self.painted.add(tint.name())
                    painter.fillPath(path, tint)
            style = Qt.PenStyle.DashLine if line.kind == "limit" else Qt.PenStyle.SolidLine
            self.pen(painter, colour, 1.2 if line.kind == "limit" else 1.6, style)
            for piece in pieces:
                if len(piece) == 1:
                    painter.setBrush(colour)
                    painter.drawEllipse(piece[0], 2.5, 2.5)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    continue
                path = QPainterPath(piece[0])
                for spot in piece[1:]:
                    path.lineTo(spot)
                painter.drawPath(path)
        if self.hover is not None and 0 <= self.hover < len(self.points):
            x = self.x_of(self.points[self.hover]["t"], area)
            self.pen(painter, grid, 1)
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        painter.restore()
        if self.height() >= 110:
            self.pen(painter, text)
            start, end = self.window()
            shape = "%d.%m. %H:%M" if end - start >= 86400 else "%H:%M"
            for when, align in ((start, Qt.AlignmentFlag.AlignLeft), (end, Qt.AlignmentFlag.AlignRight)):
                painter.drawText(QRectF(area.left(), area.bottom() + 2, area.width(), 16), align,
                                 time.strftime(shape, time.localtime(when)))


# -- the gauge --

class Gauge(QWidget):
    """A bar: what is used now of its limit, in the colour of its level (level_of)."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.setAccessibleName(name)
        self.label, self.share, self.text, self.level = name, None, "-", "muted"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(80)

    def sizeHint(self):  # noqa: N802
        return QSize(220, self.fontMetrics().height() + 10)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(80, self.fontMetrics().height() + 10)

    def set_value(self, label, share, text, level):
        self.label, self.text, self.level = label, text, level
        self.share = None if share is None else max(0.0, min(1.0, float(share)))
        self.setAccessibleName(f"{label}: {text}")
        self.setToolTip(safe_tooltip(f"{label}: {text}"))
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = token("text")
        bar = QRectF(self.rect()).adjusted(1, 2, -1, -2)
        edge = QColor(text)
        edge.setAlpha(90)
        painter.setPen(QPen(edge, 1))
        painter.drawRoundedRect(bar, 3, 3)
        if self.share:
            fill = QColor(token(self.level))
            fill.setAlpha(120)
            painter.fillRect(QRectF(bar.left() + 1, bar.top() + 1, (bar.width() - 2) * self.share, bar.height() - 2),
                             fill)
        painter.setPen(text)
        words = painter.fontMetrics().elidedText(f"{self.label}: {self.text}", Qt.TextElideMode.ElideRight,
                                                 int(bar.width() - 10))
        painter.drawText(bar.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, words)


# -- a cell's sparkline --

class CellSpark(QStyledItemDelegate):
    """A sparkline beside a cell's text: the COLUMNth value of the rows of the spark (data ROLE of the cell in
    SPARK_COLUMN) scaled to the cell's LIMIT_ROLE (its run's limit) or else the highest; filled for memory."""

    def __init__(self, parent, column, role, spark_column, limit_role, filled=False):
        super().__init__(parent)
        self.column, self.role, self.spark_column, self.limit_role, self.filled = (column, role, spark_column,
                                                                                    limit_role, filled)

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + SPARK_WIDTH + 6, hint.height())

    def values(self, index):
        points = index.siblingAtColumn(self.spark_column).data(self.role) or []
        return [num(point[self.column]) or 0.0 for point in points
                if isinstance(point, list) and len(point) > self.column]

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        values = self.values(index)
        if len(values) < 2 or option.rect.width() < SPARK_WIDTH + 20:
            return
        limit = num(index.data(self.limit_role))
        rect = QRectF(option.rect.right() - SPARK_WIDTH - 2, option.rect.top() + 4, SPARK_WIDTH,
                      option.rect.height() - 8)
        paint_line(painter, rect, values, max(limit or 0.0, max(values)) or 1.0,
                   token("series1" if self.filled else "accent"), self.filled)


def paint_line(painter, rect, values, top, colour, filled):
    """VALUES as a line (FILLED: an area) across RECT, TOP at its top."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    count = len(values)
    for position, value in enumerate(values):
        spot = QPointF(rect.left() + rect.width() * position / (count - 1),
                       rect.bottom() - rect.height() * min(value, top) / top)
        if position == 0:
            path.moveTo(rect.left(), rect.bottom()) if filled else path.moveTo(spot)
            if filled:
                path.lineTo(spot)
        else:
            path.lineTo(spot)
    tint = QColor(colour)
    if filled:
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        tint.setAlpha(110)
        painter.fillPath(path, tint)
    else:
        painter.setPen(QPen(tint, 1.3))
        painter.drawPath(path)
    painter.restore()
