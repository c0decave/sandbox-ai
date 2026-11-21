"""The System monitor page: what the running sandboxes use now and over time - CPU, memory (of the limit), disk IO,
network, processes - grouped by project, with the host's totals, each run's trend, history charts, the warnings and
what to do about them (raise a limit, stop an idle run). Each run's model spend too (sandbox-ai spend): its cost in
the list, the chosen run's cost so far with a bar per budget, and Raise budget (sandbox-ai spend raise, asked first).

It reads what sandbox-ai's collectors recorded (monitor.py, a thread of each run's launcher): `sandbox-ai top --json`
every INTERVAL_MS while the page is shown (none while it is hidden; one read at a time), and `sandbox-ai stats usage
--json --series` for the history when the window or the host changes, at most every HISTORY_MS. On a host of
`sandbox-ai hosts` the same commands run there over SSH; the page follows the host chosen on the Sandboxes page.
The charts are drawn with QPainter (Sparkline, Chart; monitor_charts.py): the app ships without Qt Charts. Below the
list: gauges of what the run chosen (or the host) uses now of its limits, and charts of its CPU, memory (with the
92 % where the sandbox is slowed down and the limit), disk IO, network and processes over the window - the
history's detail (stats usage --series) followed by every look at `top` since (a bounded ring per run and the
host) - and every run's CPU and memory stacked. The CPU and memory cells have a sparkline of the last 15 minutes."""
from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QInputDialog, QProgressBar,
                               QPushButton, QSplitter, QStyledItemDelegate, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import applog, backend, monitor_charts as mc, theme, widgets
from .files import printable, printable_text
from .host_choice import LOCAL, HostChooser, host_label, on_host, valid_host
from .i18n import N_, tr
from .widgets import (ask_first, background, explanation, list_menu, outcome, plain_label, safe_tooltip, set_enabled,
                      table, fill)

INTERVAL_MS = 10000      # a look at what runs now (the collectors sample every 10 s)
HISTORY_MS = 60000       # the history at most this often
READ_SECONDS = 60        # one look may take that long (SSH)
SPARK_ROLE = Qt.ItemDataRole.UserRole + 41
ROW_ROLE = Qt.ItemDataRole.UserRole + 42
LIMIT_ROLE = Qt.ItemDataRole.UserRole + 43  # a CPU or memory cell's limit (its sparkline is scaled to it)
GIB = 1 << 30
SORTS = (("cpu", N_("CPU")), ("mem", N_("Memory")), ("io", N_("Disk IO")), ("net", N_("Network")),
         ("pids", N_("Processes")))
WINDOWS = (("15m", N_("15 minutes")), ("1h", N_("1 hour")), ("24h", N_("24 hours")), ("7d", N_("7 days")), ("30d", N_("30 days")))
COLUMNS = (N_("Run"), N_("Agent"), N_("CPU"), N_("Memory"), N_("Disk IO"), N_("Network"), N_("Processes"),
           N_("Trend"), N_("Warnings"), N_("Cost"))
SEEN_KEPT = 1000  # warnings remembered as announced (more: only those listed now are kept - a long session's
#                  warnings that came and went are let go)
BARS = 4  # budgets shown with a bar for the chosen run (a run has its own, a project's and everything's per day, month)
WARNING_COLUMNS = (N_("Since"), N_("Run"), N_("Warning"), N_("What to do"))
WARNING_TITLES = {"memory": N_("memory near the limit"), "oom": N_("out of memory: the kernel ended processes"),
                  "cpu": N_("held back by the CPU limit"), "io": N_("waiting for the disk (IO pressure)"),
                  "idle": N_("idle, holding memory"), "pids": N_("processes and threads near the limit"),
                  "host-memory": N_("the host is low on memory"), "host-swap": N_("the host is swapping")}


def gib(value):
    return f"{(value or 0) / GIB:.1f}"


def gib_text(value):
    return f"{gib(value)} GiB"


def count_text(value):
    return str(int(round(value)))


def rate_text(value):
    value = float(value or 0)
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if value < 1024 or unit == "GiB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def duration_text(seconds):
    seconds = int(seconds or 0)
    if seconds >= 7200:
        return tr("{hours} h", hours=f"{seconds / 3600:.1f}")
    if seconds >= 120:
        return tr("{minutes} min", minutes=seconds // 60)
    return tr("{seconds} s", seconds=seconds)


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def spend_cost(entry):
    """What a run's model requests cost, in words (spend.cost_text): "USD 1.23", "+ 2 of unknown cost",
    "subscription", "0 (local)"; "-" without a count."""
    if not isinstance(entry, dict):
        return "-"
    parts, requests = [], number(entry.get("requests")) or 0
    usd, unknown = number(entry.get("usd")) or 0.0, number(entry.get("unknown")) or 0
    subscription, local = number(entry.get("subscription")) or 0, number(entry.get("local")) or 0
    if usd or requests - subscription - local - unknown > 0:
        parts.append(f"USD {usd:.2f}")
    if unknown:
        parts.append(tr("{count} of unknown cost", count=int(unknown)))
    if subscription:
        parts.append(tr("subscription"))
    if not parts and local:
        parts.append(tr("0 (local)"))
    return " + ".join(parts) or "USD 0.00"


def warning_texts(warning, name):
    """(what, what to do) of a warning of `sandbox-ai top` in the app's language, from its kind and values (the
    command's own English text where a value is missing)."""
    kind, values = warning.get("kind"), warning.get("values") if isinstance(warning.get("values"), dict) else {}
    argv = warning.get("argv") if isinstance(warning.get("argv"), list) else []
    value = printable(argv[3]) if len(argv) > 3 else "?"
    v = {key: number(values.get(key)) for key in values}
    try:
        if kind == "memory":
            return (tr("{name} uses {used} of {limit} GiB", name=name, used=gib(v["used"]), limit=gib(v["limit"])),
                    tr("Raise --memory to {value}: peak {peak} of {limit} GiB with {count} throttling events",
                       value=value, peak=gib(v["peak"]), limit=gib(v["limit"]), count=int(v.get("high") or 0)))
        if kind == "oom":
            return (tr("The kernel ended {count} processes of {name} for lack of memory", count=int(v["kills"]),
                       name=name),
                    tr("Raise --memory to {value}, or run fewer things at once", value=value) if argv
                    else tr("Run fewer things at once"))
        if kind == "cpu":
            return (tr("{name} is held back by its CPU limit ({limit} CPUs) in {percent} % of the periods", name=name,
                       limit=f"{v['limit']:g}", percent=f"{v['throttled']:.0f}"),
                    tr("Raise --cpus to {value}: held back in {percent} % of the periods for {duration}", value=value,
                       percent=f"{v['throttled']:.0f}", duration=duration_text(v.get("seconds"))))
        if kind == "io":
            return (tr("{name} waits for the disk {percent} % of the time", name=name, percent=f"{v['some']:.0f}"),
                    tr("The disk holds it back: run fewer builds or tests at once, keep caches in /tmp (memory), or "
                       "move the workspace to a faster disk"))
        if kind == "idle":
            # (what fits the run: a session is kept, a run ends for good; an MCP server and a helper wait between
            # calls by nature - their warning offers no command; an older host's says no role)
            held = {"name": name, "memory": gib(v["memory"]), "duration": duration_text(v["seconds"])}
            owner = printable(str(values.get("owner") or name))  # (v holds numbers only)
            advice = {
                "session": lambda: tr("Stop idle session {name}: {memory} GiB for {duration}; it is kept: sandbox-ai "
                                      "resume {name} starts it again", **held),
                "run": lambda: tr("Stop idle run {name}: {memory} GiB for {duration}; it is not kept: what runs in "
                                  "it ends for good", **held),
                "mcp": lambda: tr("An MCP server waits for calls between them: if no running sandbox uses it any "
                                  "more, sandbox-ai mcp stop {server} (it ends it for every sandbox that uses it)",
                                  server=owner),
                "helper": lambda: tr("A helper waits for its session's tasks between them: it ends with {session} "
                                     "(sandbox-ai stop {session})", session=owner),
            }.get(values.get("role"), lambda: tr("Stop idle run {name}: {memory} GiB for {duration}", **held))
            return (tr("{name} has been idle for {duration} and holds {memory} GiB", name=name,
                       duration=duration_text(v["seconds"]), memory=gib(v["memory"])), advice())
        if kind == "pids":
            if v.get("limit"):
                return (tr("{name} has {current} of {limit} processes and threads", name=name,
                           current=int(v["current"]), limit=int(v["limit"])),
                        tr("Raise --pids to {value}: {current} of {limit} processes and threads", value=value,
                           current=int(v["current"]), limit=int(v["limit"])))
            return (tr("{name} has {current} processes and threads", name=name, current=int(v["current"])),
                    tr("{current} processes and threads without a limit: a runaway?", current=int(v["current"])))
        if kind == "host-memory":
            return (tr("The host has {available} of {total} GiB free", available=gib(v["available"]),
                       total=gib(v["total"])),
                    tr("Stop idle runs, or start the next ones with a smaller --memory"))
        if kind == "host-swap":
            return (tr("The host is swapping ({rate} pages a second)", rate=f"{v['swap_in']:.0f}"),
                    tr("It runs slowly: stop idle runs or lower their --memory"))
    except (KeyError, TypeError, ValueError):
        pass
    return printable(warning.get("text") or ""), printable(warning.get("advice") or "")


class SparkDelegate(QStyledItemDelegate):
    """The Trend column: the last samples' CPU (a line) and memory (a filled area below it), each scaled to its own
    highest."""

    def sizeHint(self, option, index):  # noqa: N802 - Qt's name
        hint = super().sizeHint(option, index)
        return QSize(max(hint.width(), 110), max(hint.height(), 26))

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        points = index.data(SPARK_ROLE)
        if not points or len(points) < 2:
            return
        paint_spark(painter, QRectF(option.rect).adjusted(3, 4, -3, -4), points)


def paint_spark(painter, rect, points):
    """POINTS ([[t, cpu, mem, io, net], ...]) into RECT: memory as an area, CPU as a line."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    count = len(points)
    for column, filled, color in ((2, True, theme.color("ok")), (1, False, QColor(theme.colors().get("accent")
                                                                                     or "#e8590c"))):
        values = [number(point[column]) or 0 for point in points]
        top = max(values) or 1
        path = QPainterPath()
        for position, value in enumerate(values):
            x = rect.left() + rect.width() * position / (count - 1)
            y = rect.bottom() - rect.height() * value / top
            if position == 0:
                path.moveTo(x, rect.bottom() if filled else y)
                if filled:
                    path.lineTo(x, y)
            else:
                path.lineTo(x, y)
        tint = QColor(color)
        if filled:
            path.lineTo(rect.right(), rect.bottom())
            path.closeSubpath()
            tint.setAlpha(70)
            painter.fillPath(path, tint)
        else:
            painter.setPen(QPen(tint, 1.4))
            painter.drawPath(path)
    painter.restore()


class Chart(QWidget):
    """The history of what is selected: two lines over time (a run or a project: CPUs and memory; the host: CPU busy
    and free memory), each with its own scale, the highest value and the times at the edges written beside it."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(170)
        self.points, self.labels, self.title = [], (tr("CPU"), tr("Memory")), ""
        self.setAccessibleName(tr("History"))

    def set_points(self, points, labels, title):
        self.points, self.labels, self.title = list(points or []), labels, title
        self.setToolTip(safe_tooltip(title))
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colours = theme.colors()
        text = QColor(colours.get("text") or "#222")
        painter.setPen(text)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.85))
        painter.setFont(small)
        area = QRectF(self.rect()).adjusted(48, 22, -56, -20)
        painter.drawText(QRectF(8, 2, self.width() - 16, 18), Qt.AlignmentFlag.AlignLeft, self.title)
        if len(self.points) < 2:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, tr("No history recorded in this window yet."))
            return
        grid = QColor(text)
        grid.setAlpha(50)
        painter.setPen(QPen(grid, 1))
        painter.drawRect(area)
        start, end = self.points[0][0], self.points[-1][0]
        span = max(1, end - start)
        lines = ((1, QColor(colours.get("accent") or "#e8590c"), self.labels[0], Qt.AlignmentFlag.AlignLeft),
                 (2, QColor(theme.color("ok")), self.labels[1], Qt.AlignmentFlag.AlignRight))
        for column, tint, label, side in lines:
            values = [number(point[column]) or 0 for point in self.points]
            top = max(values) or 1
            room = top * 1.15  # the highest a little below the top edge
            path = QPainterPath()
            for position, (point, value) in enumerate(zip(self.points, values)):
                where = QPointF(area.left() + area.width() * (point[0] - start) / span,
                                area.bottom() - area.height() * value / room)
                path.moveTo(where) if position == 0 else path.lineTo(where)
            painter.setPen(QPen(tint, 1.6))
            painter.drawPath(path)
            painter.setPen(tint)
            box = QRectF(0, area.top() - 2, 46, 30) if side == Qt.AlignmentFlag.AlignLeft else \
                QRectF(area.right() + 4, area.top() - 2, 52, 30)
            painter.drawText(box, side | Qt.AlignmentFlag.AlignTop, f"{label}\n{self.scale(column, top)}")
        painter.setPen(text)
        for when, align in ((start, Qt.AlignmentFlag.AlignLeft), (end, Qt.AlignmentFlag.AlignRight)):
            painter.drawText(QRectF(area.left(), area.bottom() + 2, area.width(), 16), align,
                             time.strftime("%d.%m. %H:%M", time.localtime(when)))

    def scale(self, column, top):
        if self.labels[0] == tr("CPU %") and column == 1:
            return f"{top:.0f} %"
        if column == 1:
            return f"{top:.2f}"
        return f"{gib(top)} GiB"


class MonitorPage(QWidget):
    """See the module's description. SAI: the app's SandboxAI (host_json, host_run, set_limits)."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        self.hosts = HostChooser(sai, tr("The runs of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        self.host_line = plain_label(selectable=True)
        self.host_line.setAccessibleName(tr("This host"))
        layout.addWidget(self.host_line)
        row = QHBoxLayout()
        self.sort = QComboBox()
        self.sort.setAccessibleName(tr("Sort by"))
        for key, title in SORTS:
            self.sort.addItem(tr(title), key)
        self.sort.currentIndexChanged.connect(lambda _=0: self.refresh())
        self.window_box = QComboBox()
        self.window_box.setAccessibleName(tr("History"))
        for key, title in WINDOWS:
            self.window_box.addItem(tr(title), key)
        self.window_box.setCurrentIndex(self.window_box.findData("24h"))
        self.window_box.currentIndexChanged.connect(lambda _=0: self.load_history(force=True))
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setToolTip(tr("Look again now, measuring runs without a recent record"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh(measure=True))
        for widget in (plain_label(tr("Sort by"), word_wrap=False), self.sort,
                       plain_label(tr("History"), word_wrap=False), self.window_box, self.refresh_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.status = plain_label()
        layout.addWidget(self.status)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([tr(title) for title in COLUMNS])
        self.tree.setAccessibleName(tr("Runs by project"))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setItemDelegateForColumn(7, SparkDelegate(self.tree))
        self.tree.setItemDelegateForColumn(2, mc.CellSpark(self.tree, 1, SPARK_ROLE, 7, LIMIT_ROLE))
        self.tree.setItemDelegateForColumn(3, mc.CellSpark(self.tree, 2, SPARK_ROLE, 7, LIMIT_ROLE, filled=True))
        from . import tables
        tables.setup(self.tree, 0, (1, 2, 3, 4, 5, 6, 7, 8, 9))  # the name takes the rest; the others their content
        self.tree.currentItemChanged.connect(lambda *_: self.selection_changed())
        # The projects the human folded stay folded across looks (every INTERVAL_MS the tree is filled anew).
        self.folded = set()
        self.tree.itemCollapsed.connect(lambda item: self.folded.add(item.data(0, ROW_ROLE)))
        self.tree.itemExpanded.connect(lambda item: self.folded.discard(item.data(0, ROW_ROLE)))
        split.addWidget(self.tree)
        history = QGroupBox(tr("History"))
        history_layout = QVBoxLayout(history)
        history_layout.addWidget(explanation(tr(
            "The bars: what the run chosen (nothing chosen: the host) uses now of its limits, in the colours of the "
            "warnings. The charts: the window chosen, from the history and every look since; point at one for the "
            "exact values at that time.")))
        self.gauges_row = QWidget()
        self.gauges_row.setAccessibleName(tr("Now of the limits"))
        gauges = QHBoxLayout(self.gauges_row)
        gauges.setContentsMargins(0, 0, 0, 0)
        self.cpu_gauge, self.memory_gauge, self.pids_gauge = (mc.Gauge(tr("CPU")), mc.Gauge(tr("Memory")),
                                                              mc.Gauge(tr("Processes")))
        for gauge in (self.cpu_gauge, self.memory_gauge, self.pids_gauge):
            gauges.addWidget(gauge)
        history_layout.addWidget(self.gauges_row)
        self.chart_tabs = QTabWidget()
        widgets.arrow_tabs(self.chart_tabs)  # (the app's tab row: arrows at its end when the tabs do not fit)
        self.chart_tabs.setAccessibleName(tr("Charts"))
        self.chart_tabs.setDocumentMode(True)
        self.chart = Chart()
        self.chart_tabs.addTab(self.chart, tr("Overview"))
        self.cpu_chart, self.memory_chart, self.io_chart, self.net_chart, self.pids_chart = (
            mc.TimeChart(tr("CPU")), mc.TimeChart(tr("Memory")), mc.TimeChart(tr("Disk IO")),
            mc.TimeChart(tr("Network")), mc.TimeChart(tr("Processes")))
        for chart in (self.cpu_chart, self.memory_chart, self.io_chart, self.net_chart, self.pids_chart):
            self.chart_tabs.addTab(chart, chart.name)
        self.all_box = QWidget()
        self.all_box.setAccessibleName(tr("All runs"))
        every = QHBoxLayout(self.all_box)
        every.setContentsMargins(0, 0, 0, 0)
        self.all_cpu_chart = mc.TimeChart(tr("Every run's CPUs"))
        self.all_memory_chart = mc.TimeChart(tr("Every run's memory"))
        every.addWidget(self.all_cpu_chart)
        every.addWidget(self.all_memory_chart)
        self.chart_tabs.addTab(self.all_box, tr("All runs"))
        self.chart_tabs.currentChanged.connect(lambda _=0: self.show_charts())
        history_layout.addWidget(self.chart_tabs, 1)
        split.addWidget(history)
        spend = QGroupBox(tr("Model spend"))
        spend_layout = QVBoxLayout(spend)
        self.spend_line = plain_label(selectable=True)
        self.spend_line.setAccessibleName(tr("Model spend"))
        spend_layout.addWidget(self.spend_line)
        self.spend_bars = []
        for _ in range(BARS):
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.hide()
            spend_layout.addWidget(bar)
            self.spend_bars.append(bar)
        spend_buttons = QHBoxLayout()
        self.raise_button = QPushButton(tr("Raise budget…"))
        self.raise_button.setToolTip(tr("Let the chosen run spend more than its budgets (sandbox-ai spend raise)"))
        self.raise_button.clicked.connect(lambda _=False: self.raise_budget())
        spend_buttons.addWidget(self.raise_button)
        spend_buttons.addStretch()
        spend_layout.addLayout(spend_buttons)
        split.addWidget(spend)
        warnings = QGroupBox(tr("Warnings"))
        warnings_layout = QVBoxLayout(warnings)
        self.warnings = table([tr(title) for title in WARNING_COLUMNS], stretch=3, compact=[0, 1, 2])
        self.warnings.setAccessibleName(tr("Warnings"))
        self.warnings.itemSelectionChanged.connect(self.update_buttons)
        warnings_layout.addWidget(self.warnings)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton(tr("Do what it suggests…"))
        self.apply_button.clicked.connect(lambda _=False: self.apply_suggestion())
        self.stop_button = QPushButton(tr("Stop run…"))
        self.stop_button.clicked.connect(lambda _=False: self.stop_run())
        for button in (self.apply_button, self.stop_button):
            buttons.addWidget(button)
        buttons.addStretch()
        warnings_layout.addLayout(buttons)
        split.addWidget(warnings)
        split.setSizes([320, 200, 90, 160])
        layout.addWidget(split, 1)
        # A right click on a run also leads to its terminal tab (opened when there is none) and to its row on the
        # Sandboxes page (go_terminal, go_session: given by the window; this page alone has neither).
        self.go_terminal = self.go_session = None
        list_menu(self.tree, lambda: [self.stop_button, self.raise_button, None, *self.goto_entries()])
        list_menu(self.warnings, lambda: [self.apply_button, self.stop_button])
        self.data, self.history, self.history_key, self.history_at = None, None, None, 0.0
        # The looks at top (a ring per run and the host, bounded) and the history's points per run (made once per
        # history read): the charts.
        self.ring, self.history_points = mc.Ring(), {}
        self.spend, self.spend_asked = None, 0  # the chosen run's (sandbox-ai spend run), the read last asked for
        self.reading, self.history_reading, self.asked, self.seen = None, None, 0, set()
        self.timer = QTimer(self)
        self.timer.setInterval(INTERVAL_MS)
        self.timer.timeout.connect(lambda: self.refresh())
        self.update_buttons()

    # -- the host --

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self):
        self.asked += 1
        # A read of the host before (a slow or unreachable one: up to READ_SECONDS) holds nothing up: its answer comes
        # with an old ticket and is dropped; the new host is read at once.
        self.reading, self.history_reading = None, None
        self.data, self.history, self.history_key, self.seen = None, None, None, set()
        self.ring.clear()
        self.history_points = {}
        self.tree.clear()
        fill(self.warnings, [])
        self.chart.set_points([], (tr("CPU"), tr("Memory")), "")
        self.show_charts()
        self.host_line.setText("")
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):  # noqa: N802
        """Read when shown, every INTERVAL_MS while shown; never while hidden."""
        super().showEvent(event)
        self.timer.start()
        if not event.spontaneous():
            self.refresh()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.timer.stop()

    # -- reading --

    def refresh(self, measure=False):
        """`sandbox-ai top --json` on the host chosen, in the background: one read at a time."""
        host = valid_host(self.host())
        if host is None or self.reading is not None:
            return
        self.asked += 1
        ticket, sort = self.asked, self.sort.currentData() or "cpu"
        argv = ["top", "--json", "--sort", sort, *([] if measure else ["--no-measure"])]
        self.reading = ticket  # (the read in flight: its ticket; host_changed forgets it)
        background(lambda: self.sai.host_run(host, *argv, timeout=READ_SECONDS), owner=self,
                   timeout=READ_SECONDS + 15, what=f"sandbox-ai top on {host}",
                   done=lambda result, error: self.read_done(host, ticket, result, error))

    def read_done(self, host, ticket, result, error):
        if self.reading == ticket:
            self.reading = None
        if ticket != self.asked or host != self.host():
            return  # another host was chosen meanwhile: only its answer counts
        status, out, err = outcome(result, error)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            words = (err or out or "").strip()
            if "invalid choice" in words or "Unknown command" in words or "unknown command" in words:
                self.status.setText(on_host(host, tr("The sandbox-ai there does not have the system monitor yet: "
                                                     "update it (sandbox-ai hosts update).")))
            else:
                self.status.setText(on_host(host, tr("Could not read what runs: {error}",
                                                     error=printable(words[-300:] or f"exit status {status}"))))
            return
        self.data = data
        self.record_looks(data)
        self.show_data()
        self.load_history()

    def load_history(self, force=False):
        """`sandbox-ai stats usage --since WINDOW --json --series --by run` on the host: when the window or the host
        changed (FORCE), else at most every HISTORY_MS."""
        host = valid_host(self.host())
        window = self.window_box.currentData() or "24h"
        key = (host, window)
        if host is None or self.history_reading is not None or not self.isVisible() and not force:
            return
        if not force and key == self.history_key and time.monotonic() - self.history_at < HISTORY_MS / 1000:
            return
        ticket = self.asked
        self.history_reading = key  # (the read in flight; host_changed forgets it)
        background(
            lambda: self.sai.host_run(host, "stats", "usage", "--since", window, "--json", "--series", "--by", "run",
                                      timeout=READ_SECONDS),
            owner=self, timeout=READ_SECONDS + 15, what=f"sandbox-ai stats usage on {host}",
            done=lambda result, error: self.history_done(key, ticket, result, error))

    def history_done(self, key, ticket, result, error):
        if self.history_reading == key:
            self.history_reading = None
        if key[0] != self.host() or key[1] != (self.window_box.currentData() or "24h"):
            if key[0] == self.host() and self.isVisible():
                self.load_history(force=True)  # another window was chosen meanwhile: read that one now
            return
        status, out, _err = outcome(result, error)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            self.history, self.history_key, self.history_at = data, key, time.monotonic()
            self.history_points = {}
            if self.data is not None:
                self.show_data()  # (each run's cost: in the history's spend)
            self.selection_changed()

    # -- showing --

    def show_data(self):
        data = self.data or {}
        host = data.get("host") if isinstance(data.get("host"), dict) else {}
        sample = host.get("sample") if isinstance(host.get("sample"), dict) else {}
        parts = []
        if number(sample.get("cpu_pct")) is not None:
            parts.append(tr("CPU {percent} % of {cpus}", percent=f"{sample['cpu_pct']:g}", cpus=sample.get("cpus")
                            or "?"))
        if number(sample.get("load1")) is not None:
            parts.append(tr("load {load}", load=f"{sample['load1']:g}"))
        if number(sample.get("mem_total")):
            parts.append(tr("memory free {free} of {total} GiB", free=gib(sample.get("mem_avail")),
                            total=gib(sample["mem_total"])))
        if number(sample.get("swap_total")):
            parts.append(tr("swap {used} of {total} GiB", used=gib(sample.get("swap_used")),
                            total=gib(sample["swap_total"])))
        if number(sample.get("rd")) is not None:
            parts.append(tr("disk {read} read, {written} written", read=rate_text(sample.get("rd")),
                            written=rate_text(sample.get("wr"))))
        self.host_line.setText(tr("{host}: {values}", host=host_label(self.host()), values=", ".join(parts))
                               if parts else "")
        runs = [row for row in data.get("runs") or [] if isinstance(row, dict)]
        selected = self.selected_key()
        scrolled = self.tree.verticalScrollBar().value()
        self.tree.blockSignals(True)  # (filling it anew folds and unfolds nothing the human chose)
        self.tree.clear()
        groups, items = {}, {}
        for row in runs:
            project = row.get("project") or ""
            group = groups.get(project)
            if group is None:
                group = QTreeWidgetItem([os.path.basename(project) or tr("(no project)")])
                group.setToolTip(0, safe_tooltip(project))
                group.setData(0, ROW_ROLE, ("project", project))
                font = QFont(group.font(0))
                font.setBold(True)
                group.setFont(0, font)
                groups[project] = group
                self.tree.addTopLevelItem(group)
            parent = items.get(row.get("parent")) or group
            item = QTreeWidgetItem(self.cells(row))
            item.setData(0, ROW_ROLE, ("run", row.get("run")))
            item.setData(7, SPARK_ROLE, [point for point in row.get("spark") or [] if isinstance(point, list)])
            sample = row.get("sample") if isinstance(row.get("sample"), dict) else {}
            item.setData(2, LIMIT_ROLE, number(sample.get("cpu_lim")))
            item.setData(3, LIMIT_ROLE, number(sample.get("mem_lim")))
            if row.get("warnings"):
                item.setForeground(8, QColor(theme.color("bad" if any(w.get("level") == "error"
                                                                      for w in row["warnings"]) else "warn")))
            parent.addChild(item)
            items.setdefault(row.get("name"), item)
        for project, group in groups.items():
            members = [row for row in runs if (row.get("project") or "") == project]
            total = lambda key: sum(number((row.get("sample") or {}).get(key)) or 0 for row in members)  # noqa: E731
            group.setText(2, f"{total('cpu'):.2f}")
            group.setText(3, gib(total("mem")) + " GiB")
            group.setText(4, rate_text(total("rd") + total("wr")))
            group.setText(5, rate_text(total("rx") + total("tx")))
            group.setText(6, str(int(total("pids"))))
        for item in self.tree.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive):
            item.setExpanded(item.data(0, ROW_ROLE) not in self.folded)
        self.tree.blockSignals(False)
        from . import tables
        tables.fit_columns(self.tree)  # (with the runs below their projects: their texts too)
        if self.tree.columnWidth(7) < 110:
            self.tree.setColumnWidth(7, 110)  # the trend has no text to be measured by
        self.select_key(selected)
        if self.selected_key() != selected:
            self.selection_changed()  # (the run chosen is gone: the host's history, the buttons for no run)
        self.tree.doItemsLayout()  # (the scroll bar's range of the rows now, before the position is set)
        self.tree.verticalScrollBar().setValue(scrolled)  # (where the human scrolled to, not the top)
        if not runs:
            self.status.setText(on_host(self.host(), tr("No sandbox is running.")))
        else:
            # (each run's recorder's own interval: monitor_interval in the defaults, 10 s unless changed)
            intervals = sorted({number(row.get("interval")) for row in runs} - {None, 0})
            seconds = "/".join(f"{value:g}" for value in intervals) or "10"
            self.status.setText(on_host(self.host(), tr("{count} running, sampled every {seconds} s",
                                                        count=len(runs), seconds=seconds)))
        self.show_warnings(runs, host)
        self.show_charts()

    def cells(self, row):
        s = row.get("sample") if isinstance(row.get("sample"), dict) else {}
        name = printable(row.get("name") or row.get("run") or "?")
        cpu = f"{s['cpu']:.2f} ({s.get('cpu_pct', 0):.0f} %)" if number(s.get("cpu")) is not None else "-"
        if number(s.get("mem_lim")):
            memory = f"{gib(s.get('mem'))} / {gib(s['mem_lim'])} GiB"
        else:
            memory = f"{gib(s.get('mem'))} GiB" if number(s.get("mem")) is not None else "-"
        io = f"{rate_text(s.get('rd'))} / {rate_text(s.get('wr'))}" if number(s.get("rd")) is not None else "-"
        net = f"{rate_text(s.get('rx'))} / {rate_text(s.get('tx'))}" if number(s.get("rx")) is not None else "-"
        pids = str(s.get("pids")) if number(s.get("pids")) is not None else "-"
        kinds = ", ".join(tr(WARNING_TITLES.get(w.get("kind"), "?")) for w in row.get("warnings") or [])
        return [name, printable(row.get("agent") or ""), cpu, memory, io, net, pids, "", kinds,
                spend_cost(self.spend_runs().get(row.get("run")))]

    def spend_runs(self):
        """{run: {"usd", "requests", "unknown", "subscription"}} of the history's spend (stats usage)."""
        spend = (self.history or {}).get("spend") if isinstance(self.history, dict) else None
        runs = spend.get("runs") if isinstance(spend, dict) else None
        return runs if isinstance(runs, dict) else {}

    def show_warnings(self, runs, host):
        rows, keys, fresh = [], [], []
        entries = [(row, warning) for row in runs for warning in row.get("warnings") or [] if isinstance(warning, dict)]
        entries += [(None, warning) for warning in (host or {}).get("warnings") or [] if isinstance(warning, dict)]
        for row, warning in entries:
            name = printable(row.get("name") if row else host_label(self.host()))
            what, advice = warning_texts(warning, name)
            since = number(warning.get("since"))
            rows.append([time.strftime("%H:%M", time.localtime(since)) if since else "", name,
                         f"{tr(WARNING_TITLES.get(warning.get('kind'), '?'))}: {what}", advice])
            keys.append({"run": row.get("run") if row else None, "name": row.get("name") if row else None,
                         "warning": warning})
            ident = (row.get("run") if row else "host", warning.get("kind"), since)
            if ident not in self.seen:
                self.seen.add(ident)
                fresh.append(f"{name}: {what}")
        if len(self.seen) > SEEN_KEPT:
            self.seen = {(row.get("run") if row else "host", warning.get("kind"), number(warning.get("since")))
                         for row, warning in entries}
        fill(self.warnings, rows, keys=[(key['run'], key['warning'].get('kind')) for key in keys])
        self.warning_keys = keys
        if fresh and self.isVisible():
            from .short_notices import short_notice
            short_notice(self, tr("System monitor"), "\n".join(fresh[:4]), warning=True)
        self.update_buttons()

    # -- the selection and its history --

    def selected_key(self):
        item = self.tree.currentItem()
        return item.data(0, ROW_ROLE) if item is not None else None

    def select_key(self, key):
        if key is None:
            return
        found = [item for item in self.tree.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive)
                 if item.data(0, ROW_ROLE) == key]
        if found:
            self.tree.setCurrentItem(found[0])

    def selection_changed(self):
        self.update_buttons()
        self.load_spend()
        history = self.history if isinstance(self.history, dict) else {}
        key = self.selected_key()
        window = tr(dict(WINDOWS).get(self.window_box.currentData() or "24h", "24 hours"))
        runs = [row for row in history.get("runs") or [] if isinstance(row, dict)]
        if key and key[0] == "run":
            row = next((row for row in runs if row.get("run") == key[1]), None)
            points = row.get("series") if row else []
            item = self.tree.currentItem()
            self.chart.set_points(points, (tr("CPU"), tr("Memory")),
                                  tr("{name}: CPUs and memory, the last {window}", name=printable(
                                      item.text(0) if item is not None else key[1]), window=window))
        elif key and key[0] == "project":
            slots = {}
            for row in runs:
                if (row.get("project") or "") == key[1]:
                    for point in row.get("series") or []:
                        slot = slots.setdefault(point[0], [point[0], 0.0, 0, 0, 0])
                        for column in range(1, 5):
                            slot[column] += number(point[column]) or 0
            self.chart.set_points(sorted(slots.values()), (tr("CPU"), tr("Memory")),
                                  tr("{name}: CPUs and memory of its runs, the last {window}",
                                     name=os.path.basename(key[1]) or tr("(no project)"), window=window))
        else:
            host = history.get("host") if isinstance(history.get("host"), dict) else {}
            self.chart.set_points(host.get("series") or [], (tr("CPU %"), tr("Free memory")),
                                  tr("{host}: CPU busy and free memory, the last {window}",
                                     host=host_label(self.host()), window=window))
        self.show_charts()

    # -- the charts and gauges (monitor_charts.py) --

    def record_looks(self, data):
        """Each run's and the host's sample of a look at top into the ring (a run new to it: its spark first)."""
        runs = [row for row in data.get("runs") or [] if isinstance(row, dict) and isinstance(row.get("run"), str)]
        for row in runs:
            self.ring.seed(row["run"], row.get("spark"))
            self.ring.add(row["run"], row.get("sample"))
        host = data.get("host") if isinstance(data.get("host"), dict) else {}
        self.ring.add("host", host.get("sample"))
        self.ring.keep({row["run"] for row in runs} | {"host"})

    def window_span(self):
        """(seconds, (start, end), its name) of the window chosen."""
        window = self.window_box.currentData() or "24h"
        seconds = mc.WINDOW_SECONDS.get(window, 86400)
        now = time.time()
        return seconds, (now - seconds, now), tr(dict(WINDOWS).get(window, "24 hours"))

    def run_points(self, run, since):
        """The points of RUN from SINCE on: its history's detail (an older host's series), then the looks since."""
        points = self.history_points.get(run)
        if points is None:
            history = self.history if isinstance(self.history, dict) else {}
            row = next((row for row in history.get("runs") or [] if isinstance(row, dict) and row.get("run") == run),
                       {})
            points = mc.from_detail(row.get("detail")) or mc.from_series(row.get("series"))
            self.history_points[run] = points
        return mc.merged(points, self.ring.points(run), since)

    def host_points(self, since):
        points = self.history_points.get(("host",))
        if points is None:
            history = self.history if isinstance(self.history, dict) else {}
            host = history.get("host") if isinstance(history.get("host"), dict) else {}
            points = mc.from_detail(host.get("detail")) or mc.from_series(host.get("series"), mc.HOST_SERIES_KEYS)
            self.history_points[("host",)] = points
        return mc.merged(points, self.ring.points("host"), since)

    def listed_runs(self):
        return [row for row in (self.data or {}).get("runs") or [] if isinstance(row, dict)
                and isinstance(row.get("run"), str)]

    def show_charts(self):
        """The charts and gauges of what is chosen: a run, a project (its runs added up), else the host."""
        seconds, span, window = self.window_span()
        step = max(10.0, seconds / mc.SLOTS)
        key = self.selected_key()
        runs = self.listed_runs()
        if key and key[0] == "run":
            row = next((row for row in runs if row["run"] == key[1]), {"run": key[1]})
            name = printable(row.get("name") or key[1])
            self.run_charts(name, self.run_points(key[1], span[0]), span, window)
            self.run_gauges(row)
        elif key and key[0] == "project":
            members = [row["run"] for row in runs if (row.get("project") or "") == key[1]]
            points = mc.total([self.run_points(run, span[0]) for run in members], step)
            self.run_charts(os.path.basename(key[1]) or tr("(no project)"), points, span, window)
            self.gauges_row.hide()
        else:
            self.host_charts(host_label(self.host()), self.host_points(span[0]), span, window)
            self.host_gauges()
        if self.chart_tabs.currentWidget() is self.all_box:
            self.all_charts(runs, span, step, window)

    def run_charts(self, name, points, span, window):
        def cpus(value):
            return tr("{value} CPUs", value=f"{value:.2f}")

        def title(what):
            return tr("{name}: {what}, the last {window}", name=name, what=what, window=window)
        line = mc.Line
        self.cpu_chart.set_data(title(tr("CPUs")), points, [
            line(tr("CPU"), "cpu", "accent", "area"), line(tr("CPU limit"), "cpu_lim", "bad", "limit")], cpus, span)
        self.memory_chart.set_data(title(tr("memory")), points, [
            line(tr("Memory"), "mem", "series1", "area"),
            line(tr("slowed down from (92 %)"), lambda p: mc.num(p.get("mem_lim")) and p["mem_lim"] * mc.SLOW_SHARE,
                 "warn", "limit"),
            line(tr("limit (processes ended)"), "mem_lim", "bad", "limit")], gib_text, span)
        self.io_net_charts(title, points, span)
        self.pids_chart.set_data(title(tr("processes and threads")), points, [
            line(tr("processes and threads"), "pids", "series3", "area"),
            line(tr("warning (90 %)"), lambda p: mc.num(p.get("pids_lim")) and p["pids_lim"] * mc.PIDS_NEAR, "warn",
                 "limit"),
            line(tr("limit"), "pids_lim", "bad", "limit")], count_text, span)

    def io_net_charts(self, title, points, span, empty=None):
        """Disk IO (read, written) and network (received, sent); an older host's series has each as one sum."""
        line = mc.Line
        for chart, what, keys, total, words, both in (
                (self.io_chart, tr("disk IO"), ("rd", "wr"), "io", (tr("read"), tr("written")),
                 tr("read + written")),
                (self.net_chart, tr("network"), ("rx", "tx"), "net", (tr("received"), tr("sent")),
                 tr("received + sent"))):
            split = any(key in point for point in points for key in keys)
            summed = any(total in point for point in points)
            lines = [line(words[0], keys[0], "series0"), line(words[1], keys[1], "series2")] \
                if split or not summed else []
            lines += [line(both, total, "series4")] if summed else []
            chart.set_data(title(what), points, lines, rate_text, span, empty=empty)

    def host_charts(self, name, points, span, window):
        def title(what):
            return tr("{name}: {what}, the last {window}", name=name, what=what, window=window)

        def used(point):
            total, free = mc.num(point.get("mem_total")), mc.num(point.get("mem_avail"))
            return total - free if total is not None and free is not None else None
        line = mc.Line
        self.cpu_chart.set_data(title(tr("CPU busy")), points, [
            line(tr("CPU busy"), "cpu_pct", "accent", "area"), line(tr("all CPUs"), lambda p: 100.0, "bad", "limit")],
            lambda value: f"{value:.0f} %", span)
        self.memory_chart.set_data(title(tr("memory")), points, [
            line(tr("used"), used, "series1", "area"),
            line(tr("low (warning)"), lambda p: mc.num(p.get("mem_total")) and p["mem_total"] * (
                1 - mc.HOST_MEMORY_LOW), "warn", "limit"),
            line(tr("total"), "mem_total", "bad", "limit"), line(tr("swap used"), "swap_used", "series4")]
            if any("mem_total" in point for point in points) else
            [line(tr("Free memory"), "mem_avail", "series1", "area")], gib_text, span)
        older = tr("Not recorded there: an older sandbox-ai there does not record this of the host (sandbox-ai hosts "
                   "update).")
        self.io_net_charts(title, points, span, empty=older)
        self.pids_chart.set_data(title(tr("processes and threads")), points, [
            line(tr("processes and threads"), "pids", "series3", "area")], count_text, span, empty=older)

    def all_charts(self, runs, span, step, window):
        """Every run listed: its CPUs and its memory stacked on the others' (the STACKED heaviest on their own)."""
        series = {row["run"]: self.run_points(row["run"], span[0]) for row in runs}
        names = {row["run"]: printable(row.get("name") or row["run"]) for row in runs}
        host = ((self.data or {}).get("host") or {}).get("sample") or {}
        for chart, key, unit, what, limit in (
                (self.all_cpu_chart, "cpu", lambda value: tr("{value} CPUs", value=f"{value:.2f}"),
                 tr("Every run's CPUs"), (tr("the host's CPUs"), mc.num(host.get("cpus")))),
                (self.all_memory_chart, "mem", gib_text, tr("Every run's memory"),
                 (tr("the host's memory"), mc.num(host.get("mem_total"))))):
            points = mc.joined(series, key, step)
            latest = {run: next((point[run] for point in reversed(points) if run in point), 0) for run in series}
            order = sorted(series, key=lambda run: -latest[run])
            own, rest = order[:mc.STACKED], order[mc.STACKED:]
            for point in points if rest else ():
                point["others"] = sum(point.get(run, 0) for run in rest)
            lines = [mc.Line(names[run], run, f"series{index}", "stack") for index, run in enumerate(own)]
            if rest:
                lines.append(mc.Line(tr("{count} others", count=len(rest)), "others", "muted", "stack"))
            if limit[1]:
                lines.append(mc.Line(limit[0], lambda p, value=limit[1]: value, "bad", "limit"))
            chart.set_data(tr("{what}, the last {window}", what=what, window=window), points, lines, unit, span,
                           empty=tr("No sandbox is running."))

    def run_gauges(self, row):
        """The run's CPUs, memory and processes now of its limits (without a limit: of the host)."""
        s = row.get("sample") if isinstance(row.get("sample"), dict) else {}
        warned = {}
        for warning in row.get("warnings") or []:
            if isinstance(warning, dict):
                kind = {"oom": "memory"}.get(warning.get("kind"), warning.get("kind"))
                warned[kind] = "error" if warning.get("level") == "error" else warned.get(kind) or "warning"
        cpu, cpu_lim, thr = number(s.get("cpu")), number(s.get("cpu_lim")), number(s.get("thr")) or 0
        if cpu is None:
            self.cpu_gauge.set_value(tr("CPU"), None, "-", "muted")
        elif cpu_lim:
            share = cpu / cpu_lim
            level = "bad" if thr >= mc.THROTTLE_RATIO else mc.level_of(share, 0.9, 2.0, warned.get("cpu"))
            self.cpu_gauge.set_value(tr("CPU"), share, tr("{used} of {limit} CPUs ({percent} %)", used=f"{cpu:.2f}",
                                                         limit=f"{cpu_lim:g}", percent=f"{100 * share:.0f}"), level)
        else:
            self.cpu_gauge.set_value(tr("CPU"), (number(s.get("cpu_pct")) or 0) / 100,
                                     tr("{used} CPUs, no limit", used=f"{cpu:.2f}"),
                                     mc.level_of(None, 1, 1, warned.get("cpu")))
        mem, mem_lim = number(s.get("mem")), number(s.get("mem_lim"))
        if mem is None:
            self.memory_gauge.set_value(tr("Memory"), None, "-", "muted")
        elif mem_lim:
            share = mem / mem_lim
            self.memory_gauge.set_value(tr("Memory"), share, tr("{used} of {limit} GiB ({percent} %)",
                                                               used=gib(mem), limit=gib(mem_lim),
                                                               percent=f"{100 * share:.0f}"),
                                        mc.level_of(share, mc.MEMORY_NEAR, mc.SLOW_SHARE, warned.get("memory")))
        else:
            total = number((((self.data or {}).get("host") or {}).get("sample") or {}).get("mem_total"))
            self.memory_gauge.set_value(tr("Memory"), mem / total if total else None,
                                        tr("{used} GiB, no limit", used=gib(mem)),
                                        mc.level_of(None, 1, 1, warned.get("memory")))
        pids, pids_lim = number(s.get("pids")), number(s.get("pids_lim"))
        if pids is None:
            self.pids_gauge.set_value(tr("Processes"), None, "-", "muted")
        elif pids_lim:
            share = pids / pids_lim
            self.pids_gauge.set_value(tr("Processes"), share, tr("{current} of {limit} ({percent} %)",
                                                                current=int(pids), limit=int(pids_lim),
                                                                percent=f"{100 * share:.0f}"),
                                      mc.level_of(share, mc.PIDS_NEAR, 0.98, warned.get("pids")))
        else:
            self.pids_gauge.set_value(tr("Processes"), None, tr("{current}, no limit", current=int(pids)),
                                      mc.level_of(None, 1, 1, warned.get("pids")))
        self.gauges_row.show()

    def host_gauges(self):
        """The host's CPU busy, memory used of all and swap used now."""
        host = (self.data or {}).get("host") if isinstance((self.data or {}).get("host"), dict) else {}
        s = host.get("sample") if isinstance(host.get("sample"), dict) else {}
        kinds = {warning.get("kind") for warning in host.get("warnings") or [] if isinstance(warning, dict)}
        busy = number(s.get("cpu_pct"))
        self.cpu_gauge.set_value(tr("CPU"), None if busy is None else busy / 100,
                                 "-" if busy is None else tr("{percent} % busy of {cpus}", percent=f"{busy:.0f}",
                                                             cpus=s.get("cpus") or "?"),
                                 mc.level_of(None if busy is None else busy / 100, 0.9, 0.98))
        total, free = number(s.get("mem_total")), number(s.get("mem_avail"))
        if total and free is not None:
            share = 1 - free / total
            self.memory_gauge.set_value(tr("Memory"), share, tr("{used} of {total} GiB used, {free} GiB free",
                                                               used=gib(total - free), total=gib(total),
                                                               free=gib(free)),
                                        mc.level_of(share, 0.85, 1 - mc.HOST_MEMORY_LOW,
                                                    "warning" if "host-memory" in kinds else None))
        else:
            self.memory_gauge.set_value(tr("Memory"), None, "-", "muted")
        swap, swap_total = number(s.get("swap_used")), number(s.get("swap_total"))
        if swap_total:
            share = (swap or 0) / swap_total
            self.pids_gauge.set_value(tr("Swap"), share, tr("{used} of {total} GiB", used=gib(swap),
                                                            total=gib(swap_total)),
                                      mc.level_of(share, 0.5, 0.9, "warning" if "host-swap" in kinds else None))
        else:
            self.pids_gauge.set_value(tr("Swap"), None, tr("none"), "muted")
        self.gauges_row.show()

    # -- the model spend of the run chosen --

    def load_spend(self):
        """`sandbox-ai spend run RUN --json` of the run chosen, in the background (its cost so far and its budgets);
        without a run the window's total from the history."""
        run = self.chosen_run()
        host = valid_host(self.host())
        self.spend_asked += 1
        if run is None or host is None:
            self.spend = None
            self.show_spend()
            return
        ticket = self.spend_asked
        background(lambda: self.sai.host_run(host, "spend", "run", run[0], "--json", timeout=READ_SECONDS),
                   owner=self, timeout=READ_SECONDS + 15, what=f"sandbox-ai spend run {run[0]} on {host}",
                   done=lambda result, error: self.spend_done(ticket, run, result, error))

    def spend_done(self, ticket, run, result, error):
        if ticket != self.spend_asked:
            return  # another run was chosen meanwhile
        status, out, _err = outcome(result, error)
        try:
            data = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            data = None
        self.spend = {"run": run, "data": data if isinstance(data, dict) else None}
        self.show_spend()

    def show_spend(self):
        for bar in self.spend_bars:
            bar.hide()
        chosen = self.spend
        if chosen is None:
            total = ((self.history or {}).get("spend") or {}).get("total") if isinstance(self.history, dict) else None
            window = tr(dict(WINDOWS).get(self.window_box.currentData() or "24h", "24 hours"))
            self.spend_line.setText(tr("Model requests of the last {window}: {cost} ({count} requests). Choose a run "
                                       "for its budgets.", window=window, cost=spend_cost(total),
                                       count=int(number((total or {}).get("requests")) or 0))
                                    if isinstance(total, dict) and total.get("requests") else
                                    tr("Choose a run for what its model requests cost and its budgets."))
            self.update_buttons()
            return
        name = printable(chosen["run"][1])
        data = chosen["data"]
        if data is None:
            self.spend_line.setText(tr("No model request of {name} counted yet (the connection proxy counts them).",
                                       name=name))
            self.update_buttons()
            return
        lines = [tr("{name}: {cost} so far - {count} model requests", name=name, cost=spend_cost(data),
                    count=int(number(data.get("requests")) or 0))]
        budgets = [b for b in data.get("budgets") or [] if isinstance(b, dict)]
        if not budgets:
            lines.append(tr("No budget: --budget USD at the start, or sandbox-ai spend limit set."))
        if data.get("refused"):
            lines.append(tr("A budget is used up: its model requests are refused until it is raised."))
        if data.get("unknown_models"):
            lines.append(tr("No price for {models}: counted in tokens only.",
                            models=", ".join(printable(m) for m in data["unknown_models"][:10])))
        if data.get("fallback") and isinstance(data.get("fallback_price"), dict):
            # Under a budget a model without a price is counted at the table's highest prices (sandbox-ai spend).
            lines.append(tr("{count} request(s) at the fallback price USD {price_in} in / USD {price_out} out per "
                            "million tokens: no price for {models}.", count=int(number(data.get("fallback")) or 0),
                            price_in=f"{number(data['fallback_price'].get('in')) or 0:.2f}",
                            price_out=f"{number(data['fallback_price'].get('out')) or 0:.2f}",
                            models=", ".join(printable(m) for m in (data.get("fallback_models") or [])[:10])))
        self.spend_line.setText("\n".join(lines))
        for bar, budget in zip(self.spend_bars, budgets):
            percent = int(round((number(budget.get("share")) or 0) * 100))
            bar.setValue(min(100, max(0, percent)))
            bar.setFormat(tr("{percent} % of USD {usd} - {label}", percent=percent,
                             usd=f"{number(budget.get('usd')) or 0:.2f}", label=printable(budget.get("label") or "")))
            bar.setAccessibleName(tr("{label}: {percent} % used", label=printable(budget.get("label") or ""),
                                     percent=percent))
            bar.show()
        self.update_buttons()

    def raise_budget(self):
        """`sandbox-ai spend raise RUN USD` for the run chosen: how much is asked, then confirmed (spend-raise)."""
        chosen = self.chosen_run()
        if chosen is None:
            return
        run, name = chosen
        host = self.host()
        usd, ok = QInputDialog.getDouble(self, tr("Raise budget"), tr("USD more for {name}:", name=printable(name)),
                                         5.0, 0.01, 1000000.0, 2)
        if not ok or usd <= 0:
            return
        amount = f"{usd:.2f}"
        if not ask_first(self, "spend-raise", tr("Raise budget"),
                         on_host(host, tr("Let {name} spend USD {usd} more than its budgets? Its refused model "
                                          "requests go again.", name=printable(name), usd=amount)), tr("Raise")):
            return
        background(lambda: self.sai.host_run(host, "spend", "raise", run, amount, timeout=60), owner=self, work=True,
                   timeout=90, source=self.raise_button, what=f"sandbox-ai spend raise {run} {amount}",
                   done=lambda result, error: self.raised(host, result, error))

    def raised(self, host, result, error):
        self.acted(host, tr("Budget raised"), result, error)
        self.load_spend()

    # -- actions --

    def chosen_warning(self):
        rows = sorted({index.row() for index in self.warnings.selectedIndexes()})
        keys = getattr(self, "warning_keys", [])
        if len(rows) != 1:
            return None
        item = self.warnings.item(rows[0], 0)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return keys[key] if isinstance(key, int) and 0 <= key < len(keys) else None

    def chosen_run(self):
        """(run, name) of the warning or the tree row chosen, or None."""
        chosen = self.chosen_warning()
        if chosen is not None and chosen.get("run"):
            return chosen["run"], chosen.get("name") or chosen["run"]
        key = self.selected_key()
        if key and key[0] == "run":
            row = next((row for row in (self.data or {}).get("runs") or [] if row.get("run") == key[1]), {})
            return key[1], row.get("name") or key[1]
        return None

    def tree_start(self):
        """(run, name, why) of the tree's row chosen: a run with a terminal and a row on the Sandboxes page of its
        own (WHY None), else why not (a project, an MCP server's sandbox, nothing chosen)."""
        key = self.selected_key()
        if not key or key[0] != "run":
            return None, None, tr("Choose a run (not a project).")
        row = next((row for row in (self.data or {}).get("runs") or [] if isinstance(row, dict)
                    and row.get("run") == key[1]), {})
        if row.get("kind") == "mcp":
            return None, None, tr("An MCP server's sandbox: it has no terminal of its own (on Sandboxes & sessions "
                                  "it is below the start it serves).")
        return key[1], row.get("name") or key[1], None

    def goto_entries(self):
        """The tree's menu: Go to Terminals (the run's terminal tab, attached when there is none) and Go to Sessions
        (its row on the Sandboxes page, marked) - for a run of the host shown."""
        run, name, why = self.tree_start()
        found = []
        for text, go in ((tr("Go to Terminals"), self.go_terminal), (tr("Go to Sessions"), self.go_session)):
            reason = why or (None if go is not None else tr("Not available here"))
            found.append((text, reason is None, reason,
                          lambda go=go, host=self.host(): go(host, run, name) if go is not None else None))
        return found

    def update_buttons(self):
        chosen = self.chosen_warning()
        argv = (chosen or {}).get("warning", {}).get("argv") if chosen else None
        doable = isinstance(argv, list) and len(argv) >= 2 and argv[0] in ("limits", "stop") and bool(chosen.get("run"))
        set_enabled(self.apply_button, doable, None if doable else tr("Choose a warning that suggests raising a limit "
                                                                       "or stopping a run."))
        run = self.chosen_run()
        set_enabled(self.stop_button, run is not None, None if run else tr("Choose a run."))
        set_enabled(self.raise_button, run is not None, None if run else tr("Choose a run."))

    def apply_suggestion(self):
        chosen = self.chosen_warning()
        if chosen is None:
            return
        argv = [printable(word) for word in chosen["warning"].get("argv") or []]
        host, run, name = self.host(), chosen.get("run"), chosen.get("name") or chosen.get("run")
        if argv[:1] == ["stop"]:
            self.stop_run((run, name))
            return
        if argv[:1] != ["limits"] or len(argv) < 4 or argv[2] not in ("--memory", "--cpus", "--pids"):
            return
        option, value = argv[2], argv[3]
        if not ask_first(self, "limits-raise", tr("Raise a limit"),
                         on_host(host, tr("Raise {option} of {name} to {value} for the rest of its run? It may then "
                                          "use more of that host.", option=option, name=printable(name),
                                          value=value)),
                         tr("Raise"), detail={"current": None, "values": {option.lstrip("-"): value}}):
            return
        values = {option.lstrip("-"): value}
        background(lambda: self.sai.set_limits(run, host=host, **values), owner=self, work=True, timeout=180,
                   source=self.apply_button, what=f"sandbox-ai limits {run} {option} {value}",
                   done=lambda result, error: self.acted(host, tr("Limit raised"), result, error))

    def stop_run(self, chosen=None):
        chosen = chosen or self.chosen_run()
        if chosen is None:
            return
        run, name = chosen
        host = self.host()
        if not ask_first(self, "start-stop", tr("Stop"),
                         on_host(host, tr("Stop {name}? What runs in it ends.", name=printable(name))), tr("Stop")):
            return
        try:
            argv = backend.stop_args(name if backend.valid_run(name) else run)
        except ValueError:
            return
        background(lambda: self.sai.host_run(host, *argv, timeout=180), owner=self, work=True, timeout=200,
                   source=self.stop_button, what=f"sandbox-ai stop {name}",
                   done=lambda result, error: self.acted(host, tr("Stopped"), result, error))

    def acted(self, host, title, result, error):
        status, out, err = outcome(result, error)
        from .short_notices import short_notice
        if status == 0:
            short_notice(self, title, on_host(host, printable_text((out or "").strip()[-300:]) or title))
        else:
            short_notice(self, title, on_host(host, printable_text((err or out or "").strip()[-400:])), warning=True)
            applog.log("WARNING", "monitor", f"{title}: exit status {status}")
        self.refresh(measure=True)
