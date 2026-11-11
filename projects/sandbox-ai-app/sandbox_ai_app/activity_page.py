"""The Activity page: what the agents did in a sandbox, live - prompts, replies, tool calls, commands, file
changes, network, approvals, MCP calls, the session's start and end, tokens.

The timeline comes from `sandbox-ai activity --json --follow` (a QProcess whose output is read as it comes);
everything in it may quote the agent, so it is shown as plain text with control characters escaped.

On a host of `sandbox-ai hosts` (the host chooser at the top) the timeline is `sandbox-ai activity ... --json --tail N`
there, over SSH (host_run, in the background): ssh hands no stream, so "follow" asks again every few seconds and adds
what is new (by each record's id). Its sources are that host's running starts and sessions, or a project or log folder
there, typed; since, kinds and the filter text go to the command as --since, --kind and --grep."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QProcess, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu,
    QPlainTextEdit, QPushButton, QSplitter, QTableView, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import backend, theme, widgets
from .files import printable
from .i18n import N_, tr
from .widgets import MONO, flow, plain_label, shown_as_text

KINDS = {"prompt": N_("prompt"), "reply": N_("reply"), "tool": N_("tool"), "command": N_("command"),
         "file": N_("file"), "network": N_("network"), "approval": N_("approval"), "mcp": N_("MCP"),
         "session": N_("session"), "tokens": N_("tokens")}
COLUMNS = (N_("Time"), N_("Kind"), N_("Agent"), N_("What"))
RECORD_ROLE = Qt.ItemDataRole.UserRole + 20
TAIL = 5000           # events read when a timeline is opened
KEEP = 20000          # events kept in the page; the oldest go first
LINE_LIMIT = 1 << 20  # one line of the command's output
LOCAL = "local"
REMOTE_TAIL = 2000    # events read from another host at each look
REMOTE_POLL = 5000    # ms between two looks at another host's timeline while "follow" is on
REMOTE_SECONDS = 120  # one look may take that long (SSH)
SINCE = re.compile(r"[0-9]{1,6}[smhd]|[0-9]{1,2}:[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9:.+Z-]{0,20})?")


def kind_color(record):
    """The colour of RECORD's kind in the current theme; refusals and errors in the theme's "bad" colour."""
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    if details.get("verdict") == "denied" or details.get("status") in ("error", "denied") or \
            details.get("answer") in ("deny", "denied"):
        return theme.color("bad")
    tokens = theme.colors()
    return {"prompt": tokens.get("accent"), "reply": tokens.get("text"), "tool": tokens.get("secondary"),
            "command": theme.color("warn"), "file": theme.color("ok"), "network": theme.color("muted"),
            "approval": theme.color("warn"), "mcp": tokens.get("accent"), "session": theme.color("muted"),
            "tokens": theme.color("muted")}.get(record.get("kind")) or tokens.get("text")


def event_time(text):
    """Seconds since the epoch of an event's time (2026-11-04T10:00:01.500Z), or None."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d{1,9})?Z", text) if isinstance(text, str) else None
    if not match:
        return None
    moment = backend.parse_time(match.group(1) + "Z")
    return None if moment is None else moment + (float(match.group(2)) if match.group(2) else 0.0)


def short_time(text, today=None):
    """HH:MM:SS of an event (local time), with the date before it when it is not today."""
    moment = event_time(text)
    if moment is None:
        return printable(text or "")
    today = today or time.strftime("%Y-%m-%d")
    day = time.strftime("%Y-%m-%d", time.localtime(moment))
    clock = time.strftime("%H:%M:%S", time.localtime(moment))
    if day == today:
        return clock
    return time.strftime("%d.%m. " if widgets.i18n.language() == "de" else "%m-%d ", time.localtime(moment)) + clock


SANDBOX_SOURCES = {"files", "egress", "proxy", "approvals", "sandbox-ai"}


def agent_text(record):
    """The agent (and subagent) for what its transcript says, "sandbox" for what sandbox-ai observed."""
    if record.get("source") in SANDBOX_SOURCES:
        return "sandbox"
    label = str(record.get("agent") or "-")
    if record.get("subagent"):
        label += " / " + str(record["subagent"]).split(":")[0]
    return printable(label)[:40]


def detail_text(record):
    """RECORD as indented JSON, every line with control and direction characters escaped."""
    text = json.dumps(record, indent=2, ensure_ascii=False)
    return "\n".join(printable(line) for line in text.split("\n"))


def text_line(record):
    """One line of the text export: date and time (local), kind, agent, summary."""
    moment = event_time(record.get("time"))
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment)) if moment is not None else "?"
    return f"{when}  {str(record.get('kind', ''))[:8]:<8}  {agent_text(record):<16}  " \
           f"{printable(record.get('summary') or '')}"


class Entry:
    """A record with what the table shows of it, computed once."""
    __slots__ = ("record", "cells", "search", "kind", "color")

    def __init__(self, record):
        self.record = record
        self.kind = str(record.get("kind") or "")
        summary = printable(record.get("summary") or "")
        self.cells = [short_time(record.get("time")), tr(KINDS[self.kind]) if self.kind in KINDS else printable(self.kind),
                      agent_text(record), widgets.elide(summary)]
        details = json.dumps(record.get("details"), ensure_ascii=False)[:4000]
        self.search = " ".join([summary, self.cells[2], self.kind, details]).lower()
        self.color = None


class ActivityModel(QAbstractTableModel):
    """The events, oldest first; an event that replaces an earlier one (same id, "update") takes its row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries, self.rows = [], {}  # rows: id -> row

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(COLUMNS):
            return tr(COLUMNS[section])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.cells[column]
        if role == Qt.ItemDataRole.ForegroundRole and column in (1, 3):
            if entry.color is None:
                entry.color = QColor(kind_color(entry.record))
            if column == 1 or entry.color.name() == QColor(theme.color("bad")).name():
                return entry.color
            return None
        if role == Qt.ItemDataRole.FontRole and column == 1 and entry.kind == "prompt":
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ToolTipRole and column == 3:
            return widgets.safe_tooltip(printable(entry.record.get("summary") or ""))
        if role == Qt.ItemDataRole.ToolTipRole and column == 0:
            return widgets.safe_tooltip(printable(entry.record.get("time") or ""))
        if role == RECORD_ROLE:
            return entry.record  # through Qt: a copy with its keys sorted; the page reads record_at()
        return None

    def record_at(self, row):
        return self.entries[row].record if 0 <= row < len(self.entries) else None

    def add(self, records):
        """Append RECORDS (updates replace their earlier event); keep at most KEEP events."""
        fresh = []
        for record in records:
            ident = record.get("id")
            row = self.rows.get(ident) if isinstance(ident, str) else None
            if row is not None and record.get("update"):
                self.entries[row] = Entry(record)
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
            else:
                fresh.append(Entry(record))
        if fresh:
            start = len(self.entries)
            self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
            for offset, entry in enumerate(fresh):
                self.entries.append(entry)
                if isinstance(entry.record.get("id"), str):
                    self.rows[entry.record["id"]] = start + offset
            self.endInsertRows()
        if len(self.entries) > KEEP:
            drop = len(self.entries) - KEEP
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self.entries[:drop]
            self.rows = {e.record["id"]: row for row, e in enumerate(self.entries) if isinstance(e.record.get("id"), str)}
            self.endRemoveRows()

    def clear(self):
        self.beginResetModel()
        self.entries, self.rows = [], {}
        self.endResetModel()

    def recolor(self):
        """A new theme: the kinds take its colours."""
        for entry in self.entries:
            entry.color = None
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, len(COLUMNS) - 1))


class ActivityFilter(QSortFilterProxyModel):
    """The events of the kinds chosen that contain the text typed (in their summary, agent or details)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kinds, self.text = set(KINDS), ""

    def set_filter(self, kinds=None, text=None):
        changing = hasattr(self, "beginFilterChange")  # Qt 6.9 on; invalidateFilter() before
        if changing:
            self.beginFilterChange()
        if kinds is not None:
            self.kinds = set(kinds)
        if text is not None:
            self.text = text.strip().lower()
        if changing:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        entries = self.sourceModel().entries
        if not 0 <= row < len(entries):
            return False
        entry = entries[row]
        return (entry.kind in self.kinds or entry.kind not in KINDS) and (not self.text or self.text in entry.search)


class ActivityPage(QWidget):
    """The timeline of a run, a session or every run of a project, with filters, details, export, and jumps to
    the Connections page (network events) and the Files page (file events). WINDOW: the main window (for the
    jumps; may be None)."""
    status_changed = Signal(str)

    def __init__(self, sai, window=None):
        super().__init__()
        self.sai, self.main_window = sai, window  # never self.window: it would hide QWidget.window()
        self.process, self.buffer, self.errors, self.starts = None, b"", "", {}
        # One look at a host at a time: remote_busy stays until the look running has come back, also when its result
        # no longer counts (stop(), a new filter: remote_token); remote_again: one more look then, with what is chosen.
        self.seen, self.remote_token, self.remote_busy, self.remote_again = set(), 0, False, False
        self.listing_token = 0  # the newest listing of a host's sources (its own counter: a poll never drops it)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The activity of this machine's sandboxes, or of a host's (over SSH)"))
        self.hosts.changed.connect(self.host_changed)
        outer.addWidget(self.hosts)
        self.poll = QTimer(self)
        self.poll.setInterval(REMOTE_POLL)
        self.poll.timeout.connect(lambda: self.isVisible() and self.look_again())  # not while the page is hidden
        self.refilter = QTimer(self)  # a host's timeline is filtered by the command: asked again once typing stops
        self.refilter.setSingleShot(True)
        self.refilter.setInterval(700)
        self.refilter.timeout.connect(self.restart)
        self.tabs = QTabWidget()
        timeline = QWidget()
        layout = QVBoxLayout(timeline)
        self.source = QComboBox()
        self.source.setAccessibleName(tr("Show"))
        widgets.compact_combo(self.source, 30)  # long log folders: elided here, whole in the list and the tooltip
        self.source.currentIndexChanged.connect(lambda _=0: self.restart())
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(self.refresh_sources)
        other = QPushButton(tr("Other folder…"))
        other.clicked.connect(self.browse)
        self.kinds_button = QToolButton()
        self.kinds_button.setText(tr("Kinds"))
        self.kinds_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.kinds_button)
        self.kind_actions = {}
        for kind, title in KINDS.items():
            action = menu.addAction(tr(title))
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(lambda _=False: self.apply_filter())
            self.kind_actions[kind] = action
        menu.addSeparator()
        menu.addAction(tr("All kinds")).triggered.connect(lambda: self.set_kinds(KINDS))
        menu.addAction(tr("Only what the agent did")).triggered.connect(
            lambda: self.set_kinds(["prompt", "reply", "tool", "command", "file", "mcp", "approval"]))
        self.kinds_button.setMenu(menu)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("filter (text, path, host, command)"))
        self.filter.setMinimumWidth(160)
        self.filter.setAccessibleName(tr("Filter"))
        self.filter.textChanged.connect(lambda _="": self.apply_filter())
        self.since = QLineEdit()
        self.since.setPlaceholderText(tr("since (10m, 2h, 3d, 14:30)"))
        self.since.setToolTip(tr("Only events from then on (sandbox-ai activity --since)"))
        self.since.setMaximumWidth(150)
        self.since.setAccessibleName(tr("Since"))
        self.since.editingFinished.connect(self.restart)
        self.follow = QCheckBox(tr("follow"))
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda _=False: self.restart())
        export = QPushButton(tr("Export…"))
        export_menu = QMenu(export)
        export_menu.addAction(tr("As JSON lines…")).triggered.connect(lambda: self.export("json"))
        export_menu.addAction(tr("As text…")).triggered.connect(lambda: self.export("text"))
        export.setMenu(export_menu)
        show = QLabel(tr("Show"))
        show.setBuddy(self.source)
        # A row that wraps when the window is narrow (it made the window 1450 px wide before).
        layout.addWidget(flow(show, self.source, refresh, other, self.kinds_button, self.filter, self.since,
                              self.follow, export))
        split = QSplitter(Qt.Orientation.Vertical)
        self.model = ActivityModel(self)
        self.proxy = ActivityFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setAccessibleName(tr("Timeline"))
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # several rows (Ctrl, Shift) to copy them (Ctrl+C); the buttons below act on one
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        widgets.tables.setup(self.table, 3, (0, 1, 2))  # What takes the rest
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.show_details())
        self.table.doubleClicked.connect(lambda _index: self.jump())
        self.detail = QPlainTextEdit()
        self.detail.setAccessibleName(tr("Details"))
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setPlaceholderText(tr("Choose an event to see everything recorded about it."))
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([460, 200])
        layout.addWidget(split, 1)
        bottom = QHBoxLayout()
        self.connections_button = QPushButton(tr("Show in Connections"))
        self.connections_button.clicked.connect(self.show_connections)
        self.files_button = QPushButton(tr("Show in Files"))
        self.files_button.clicked.connect(self.show_file)
        self.status = plain_label(selectable=True)  # wraps: a long status never widens the window
        self.status.setAccessibleName(tr("Status"))
        bottom.addWidget(self.connections_button)
        bottom.addWidget(self.files_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        # the status on a row of its own: beside the buttons a wrapped status got no room for its second line (a
        # window at its smallest, in German)
        layout.addWidget(self.status)
        # The canary log (sandbox-ai canaries log): opened decoys and tokens that left, with who and what around it.
        from .canaries_view import CanariesView  # here: it uses this module's helpers
        self.canaries = CanariesView(sai)
        self.canaries.counted.connect(self.canaries_counted)
        # What the safeguards refused or ended (sandbox-ai safeguards): why, and the command that would allow it.
        from .safeguards_view import SafeguardsView
        self.safeguards = SafeguardsView(sai)
        self.safeguards.counted.connect(self.safeguards_counted)
        self.tabs.addTab(timeline, tr("Timeline"))
        self.tabs.addTab(self.canaries, tr("Canaries"))
        self.tabs.addTab(self.safeguards, tr("Safeguards"))
        outer.addWidget(self.tabs)
        self.update_buttons()
        self.started = False

    def canaries_counted(self, count):
        """The Canaries tab names how many events there are, in the theme's "bad" colour when there are any."""
        index = self.tabs.indexOf(self.canaries)
        self.tabs.setTabText(index, tr("Canaries ({count})", count=count) if count else tr("Canaries"))
        self.tabs.tabBar().setTabTextColor(index, theme.color("bad") if count else
                                           self.tabs.tabBar().palette().color(self.tabs.tabBar().foregroundRole()))

    def safeguards_counted(self, count):
        """The Safeguards tab names how many records there are, in the theme's warning colour when there are any."""
        index = self.tabs.indexOf(self.safeguards)
        self.tabs.setTabText(index, tr("Safeguards ({count})", count=count) if count else tr("Safeguards"))
        self.tabs.tabBar().setTabTextColor(index, theme.color("warn") if count else
                                           self.tabs.tabBar().palette().color(self.tabs.tabBar().foregroundRole()))

    def show_safeguards(self):
        """Open the Safeguards tab (a popup's button)."""
        self.tabs.setCurrentWidget(self.safeguards)
        self.safeguards.refresh()

    # -- what to show --

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self, host):
        """Another host chosen: its sources and timeline, and its canary log. Nothing of the host before stays: its
        --follow stream ends, its rows go, and its sources (this machine's log folders too) are never asked for on
        this one."""
        self.stop()
        self.listing_token += 1
        self.remote_sources = []
        self.model.clear()
        self.starts, self.errors, self.seen = {}, "", set()
        self.detail.clear()
        self.source.blockSignals(True)
        self.source.clear()
        self.source.blockSignals(False)
        self.canaries.set_host(host)
        if self.started:
            self.refresh_sources()

    remote_sources = local_sources = ()

    def sources(self):
        """[(title, arguments of sandbox-ai activity)]: running sandboxes, sessions, project log folders (as the last
        read in the background found them: read_sources here, another host's listing there; see refresh_sources)."""
        return list(self.remote_sources if self.host() != LOCAL else self.local_sources)

    def read_sources(self):
        """This machine's sources (in a thread: sandbox-ai's registry read asks Podman about runs whose launcher is
        gone, and prunes what ended)."""
        found = []
        try:
            runs = self.sai.runs()
        except Exception:  # noqa: BLE001 - the page shows what it can
            runs = {}
        for name, record in sorted(runs.items()):
            session = backend.session_of(record)
            if session:
                continue  # listed as a session below
            workspace = printable(record.get("workspace") or "")
            found.append((tr("Running: {run} ({workspace})", run=printable(name), workspace=workspace), [name]))
        try:
            sessions = self.sai.sessions()
        except Exception:  # noqa: BLE001
            sessions = []
        for row in sessions:
            name, status = row[0], row[3] if len(row) > 3 else ""
            found.append((tr("Session: {name} ({status})", name=printable(name), status=printable(status)),
                          ["--session", name]))
        for directory in self.sai.log_directories():
            found.append((tr("Project: {name} (every run)", name=printable(directory.name)), ["--log-dir", str(directory)]))
        return found

    def refresh_sources(self, select=None):
        host = self.host()
        if host == LOCAL:
            self.listing_token += 1
            token = self.listing_token
            widgets.background(self.read_sources, owner=self, timeout=90, work=False,
                               done=lambda result, error: self.local_listed(token, select, result, error))
            return
        if select is None:
            # That host's running starts and sessions (`sandbox-ai list --json` there, in the background).
            self.listing_token += 1
            token = self.listing_token
            self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
            widgets.background(lambda: self.sai.host_instances(host), owner=self, timeout=90,
                               done=lambda result, error: self.remote_listed(host, token, result, error))
            return
        self.fill_sources(select)

    def local_listed(self, token, select, result, error):
        if token != self.listing_token or self.host() != LOCAL:
            return  # read for this machine before another host was chosen: dropped
        self.local_sources = result if error is None else []
        if error is not None:
            self.status.setText(printable(error)[:300])
        self.fill_sources(select)

    def remote_listed(self, host, token, result, error):
        if token != self.listing_token or host != self.host():
            return
        found = []
        for instance in result if error is None else []:
            if instance.kind == "session":
                found.append((tr("Session: {name} ({status})", name=printable(instance.name),
                                 status=printable(instance.state)), ["--session", instance.name]))
            elif instance.kind == "run":
                found.append((tr("Running: {run} ({workspace})", run=printable(instance.name),
                                 workspace=printable(instance.project)), [instance.name]))
        self.remote_sources = found
        if error is not None:
            self.status.setText(tr("{host}: no answer", host=self.hosts.label(host)) + f" ({printable(error)[:300]})")
        self.fill_sources(None)

    def fill_sources(self, select=None):
        current = select or self.source.currentData()
        self.source.blockSignals(True)
        self.source.clear()
        for title, arguments in self.sources():
            self.source.addItem(title, arguments)
        if current and self.source.findData(current) < 0 and (self.host() == LOCAL or select is not None or
                                                               current[0] in ("--project", "--log-dir")):
            self.source.addItem(tr("Folder: {path}", path=printable(current[-1])), current)
        index = self.source.findData(current) if current else -1
        self.source.setCurrentIndex(index if index >= 0 else 0)
        self.source.blockSignals(False)
        self.restart()

    def browse(self):
        if self.host() != LOCAL:
            # A folder on that host: typed (this machine's file dialog cannot see it).
            path, ok = QInputDialog.getText(self, tr("Log folder"), shown_as_text(tr(
                "A project folder or a log folder on {host} (absolute path):", host=self.hosts.label())))
            path = path.strip()
            if ok and backend.valid_directory(path):
                kind = "--log-dir" if "/sandbox-ai/logs/" in path or path.rstrip("/").endswith("/logs") else "--project"
                self.refresh_sources(select=[kind, path])
            elif ok and path:
                self.status.setText(tr("Not an absolute path: {path}", path=printable(path)))
            return
        from .state import choose_folder
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_sources(select=["--log-dir", path])

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self.poll.isActive():  # followed while hidden: a look now, not after the next interval
            self.look_again()
        if not self.started:
            self.started = True
            self.refresh_sources()
            self.canaries.refresh()  # the tab names how many events there are
            self.safeguards.refresh()

    # -- the command --

    def since_value(self):
        text = self.since.text().strip()
        return text if SINCE.fullmatch(text) else None

    def arguments(self):
        chosen = self.source.currentData()
        if not chosen:
            return None
        since = self.since_value()
        return ["activity", *chosen, "--json", "--tail", str(TAIL), *([f"--since={since}"] if since else []),
                *(["--follow"] if self.follow.isChecked() else [])]

    def restart(self):
        """Read the timeline chosen again from the start (the newest TAIL events, then what comes)."""
        self.stop()
        self.model.clear()
        self.starts, self.errors, self.seen = {}, "", set()
        self.detail.clear()
        if self.since.text().strip() and self.since_value() is None:
            self.status.setText(tr("Since: 10m, 2h, 3d, 14:30 or a date such as 2026-11-04."))
            return
        if self.host() != LOCAL:
            self.start_remote()
            return
        arguments = self.arguments()
        if not (Path(self.sai.directory) / "activity.py").is_file():
            self.status.setText(tr("This sandbox-ai has no activity timeline yet; update it with: sandbox-ai update"))
            return
        if arguments is None:
            self.status.setText(tr("No sandbox has recorded activity yet: it is written while a sandbox with a log "
                                   "folder runs."))
            return
        argv = self.sai.command(*arguments)
        process = QProcess(self)
        process.setProgram(argv[0])
        process.setArguments(argv[1:])
        process.setStandardInputFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(lambda p=process: self.read(p))
        process.readyReadStandardError.connect(lambda p=process: self.read_errors(p))
        process.finished.connect(lambda code, _status, p=process: self.finished(p, code))
        self.process, self.buffer = process, b""
        process.start()
        self.status.setText(tr("Reading…"))

    # -- another host: asked again while "follow" is on --

    def remote_query(self):
        """(source arguments, kinds or (), grep or None) of the timeline chosen on another host."""
        kinds = self.chosen_kinds()
        grep = self.filter.text().strip()[:200] or None
        return list(self.source.currentData() or []), (kinds if len(kinds) < len(KINDS) else ()), grep

    def start_remote(self):
        self.remote_token += 1
        if not self.source.currentData():
            self.status.setText(tr("No running sandbox on {host}: choose a project or log folder there (Other "
                                   "folder…).", host=self.hosts.label()))
            return
        self.look_again()
        if self.follow.isChecked():
            self.poll.start()

    def look_again(self):
        """One look at the host's timeline (one at a time: asked again while one runs, it follows that one); what is
        new is added."""
        if self.host() == LOCAL:
            return
        if self.remote_busy:
            self.remote_again = True
            return
        self.remote_again = False
        host, token = self.host(), self.remote_token
        source, kinds, grep = self.remote_query()
        since = self.since_value()
        self.remote_busy = True
        if not self.model.rowCount():
            self.status.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        widgets.background(lambda: self.sai.host_activity(host, source, REMOTE_TAIL, since, kinds, grep,
                                                          timeout=REMOTE_SECONDS), owner=self,
                           timeout=REMOTE_SECONDS + 15,
                           done=lambda result, error: self.remote_read(host, token, result, error))

    def remote_read(self, host, token, result, error):
        self.remote_busy = False
        if token != self.remote_token or host != self.host():
            # A look from before (stop(), another filter or host): not shown; the look asked for meanwhile follows.
            if self.remote_again:
                self.look_again()
            return
        if self.remote_again:
            self.look_again()
        if error is not None:
            self.status.setText(printable(str(error).removeprefix("sandbox-ai activity: "))[:500])
            return
        new = []
        for record in result:
            key = record.get("id") if isinstance(record.get("id"), str) else json.dumps(record, sort_keys=True)[:4000]
            if key not in self.seen:
                self.seen.add(key)
                new.append(record)
        if new:
            self.add_lines([json.dumps(record).encode() for record in new])
        else:
            self.update_status()

    def stop(self):
        """Kill the command, without waiting for it (the GUI thread): deleted once it has ended (deleting a
        running QProcess would wait for it). Another host's looks stop too."""
        self.poll.stop()
        self.remote_token += 1  # the look running (remote_busy until it is back) is no longer shown
        self.remote_again = False
        process, self.process = self.process, None
        if process is None:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
            return
        process.finished.connect(lambda *_: process.deleteLater())
        process.kill()

    def shutdown(self):
        self.stop()

    def read(self, process):
        if process is not self.process:
            return
        self.buffer += bytes(process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b"\n")
        if len(self.buffer) > LINE_LIMIT:
            self.buffer = b""  # one line that never ends: dropped
        self.add_lines(lines)

    def add_lines(self, lines):
        records = []
        for line in lines:
            if not line.strip() or len(line) > LINE_LIMIT:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and isinstance(record.get("kind"), str):
                records.append(record)
                details = record.get("details") if isinstance(record.get("details"), dict) else {}
                if record["kind"] == "session" and details.get("event") == "start" and isinstance(record.get("run"), str):
                    self.starts[record["run"]] = details
        if not records:
            return
        follow = self.follow.isChecked() and self.at_bottom()
        self.model.add(records)
        widgets.tables.fit_columns(self.table)
        self.update_status()
        if follow:
            self.table.scrollToBottom()

    def at_bottom(self):
        bar = self.table.verticalScrollBar()
        return bar.value() >= bar.maximum() - 2

    def read_errors(self, process):
        if process is self.process:
            self.errors = (self.errors + bytes(process.readAllStandardError()).decode("utf-8", "replace"))[-4000:]

    def finished(self, process, code):
        if process is not self.process:
            return
        self.read(process)
        if self.buffer.strip():
            self.add_lines([self.buffer])
            self.buffer = b""
        message = self.errors.strip().splitlines()[-1] if self.errors.strip() else ""
        if code != 0 and message:
            self.status.setText(printable(message.removeprefix("sandbox-ai activity: ")))
        else:
            self.update_status()

    def update_status(self):
        total, shown = self.model.rowCount(), self.proxy.rowCount()
        if total == shown:
            self.status.setText(tr("{count} events", count=widgets.number(total)))
        else:
            self.status.setText(tr("{shown} of {count} events shown", shown=widgets.number(shown),
                                   count=widgets.number(total)))

    # -- filters, details --

    def chosen_kinds(self):
        return [kind for kind, action in self.kind_actions.items() if action.isChecked()]

    def set_kinds(self, kinds):
        for kind, action in self.kind_actions.items():
            action.blockSignals(True)
            action.setChecked(kind in kinds)
            action.blockSignals(False)
        self.apply_filter()

    def apply_filter(self):
        kinds = self.chosen_kinds()
        self.kinds_button.setText(tr("Kinds") if len(kinds) == len(KINDS) else
                                  tr("Kinds ({count})", count=len(kinds)))
        self.proxy.set_filter(kinds, self.filter.text())
        self.update_status()
        if self.host() != LOCAL and getattr(self, "started", False):
            self.refilter.start()  # the host's command filters too (--kind, --grep): asked again

    def current(self):
        """The record of the selected row, or None."""
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.model.record_at(self.proxy.mapToSource(rows[0]).row()) if rows else None

    def show_details(self):
        record = self.current()
        self.detail.setPlainText(detail_text(record) if record is not None else "")
        self.update_buttons()

    def update_buttons(self):
        record = self.current()
        if self.host() != LOCAL:
            why = tr("Only for this machine's sandboxes")
            widgets.set_enabled(self.connections_button, False, why)
            widgets.set_enabled(self.files_button, False, why)
            return
        widgets.set_enabled(self.connections_button, record is not None and record.get("kind") == "network",
                            tr("Choose a network event"))
        widgets.set_enabled(self.files_button, record is not None and self.host_path(record) is not None,
                            tr("Choose a file event inside a workspace"))
        count = len(self.table.selectionModel().selectedRows()) if self.table.selectionModel() else 0
        if count > 1:
            for button in (self.connections_button, self.files_button):
                widgets.set_enabled(button, False, tr("Acts on one row: {count} are selected.", count=count))

    def shown_records(self):
        return [self.model.record_at(self.proxy.mapToSource(self.proxy.index(row, 0)).row())
                for row in range(self.proxy.rowCount())]

    def export(self, kind):
        records = self.shown_records()
        suffix = "jsonl" if kind == "json" else "txt"
        from .state import choose_save
        path = choose_save(self, tr("Export the activity"), f"activity.{suffix}",
                           tr("JSON lines (*.jsonl)") if kind == "json" else tr("Text (*.txt)"), "export")
        if not path:
            return None
        lines = [json.dumps(r, ensure_ascii=False) if kind == "json" else text_line(r) for r in records]
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("".join(line + "\n" for line in lines))
        except OSError as exc:
            widgets.warn(self, tr("Not exported"), tr("Cannot write {path}: {error}", path=printable(path),
                                                      error=printable(exc)))
            return None
        self.status.setText(tr("{count} events exported to {path}", count=widgets.number(len(records)),
                               path=printable(path)))
        return path

    # -- jumps to the other pages --

    def start_of(self, record):
        details = self.starts.get(record.get("run")) if isinstance(record.get("run"), str) else None
        return details if isinstance(details, dict) else {}

    def host_path(self, record):
        """The host path of a file event's sandbox path (a workspace, or a kept HOME), or None."""
        if record.get("kind") != "file":
            return None
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        path = details.get("path") or details.get("folder")
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        start = self.start_of(record)
        mapping = [(w.get("sandbox"), w.get("host")) for w in start.get("workspaces") or [] if isinstance(w, dict)]
        if isinstance(start.get("home"), str):
            mapping.append(("/root", start["home"]))
        if not path.startswith("/"):
            path = "/work/" + path
        for inside, outside in mapping:
            if not isinstance(inside, str) or not isinstance(outside, str):
                continue
            if path == inside or path.startswith(inside.rstrip("/") + "/"):
                relative = path[len(inside):].strip("/")
                if ".." in relative.split("/"):
                    return None
                return outside, relative
        return None

    def jump(self):
        record = self.current()
        if record is None:
            return
        if record.get("kind") == "network":
            self.show_connections()
        elif record.get("kind") == "file":
            self.show_file()

    def show_connections(self):
        record = self.current()
        window = self.main_window
        if record is None or window is None or not hasattr(window, "connections"):
            return False
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        log_dir = self.start_of(record).get("log_dir")
        page = window.connections
        run = record.get("run")
        suffix = f"-{run}.jsonl" if isinstance(run, str) else None
        if isinstance(log_dir, str) and log_dir:
            page.refresh_directories(select=log_dir, log_suffix=suffix)  # listed in a thread: chosen there
        elif suffix:
            for index in range(page.file.count()):
                if page.file.itemText(index).endswith(suffix):
                    page.file.setCurrentIndex(index)
                    break
        target = details.get("target") or details.get("host") or details.get("address") or ""
        page.filter.setText(str(target)[:200])
        window.show_page("connections")
        return True

    def show_file(self):
        record = self.current()
        window = self.main_window
        found = self.host_path(record) if record is not None else None
        if found is None or window is None or not hasattr(window, "files"):
            return False
        root, relative = found
        page = window.files
        real = os.path.realpath(root)
        parts = [p for p in relative.split("/") if p]
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        if details.get("path"):
            folder, name = parts[:-1], parts[-1] if parts else None
        else:
            folder, name = parts, None

        def go():
            page.roots.setCurrentIndex(page.roots.findData(real))
            selected = name if name and Path(real, *folder, name).exists() else None
            page.go("/".join(folder), then=lambda _listed: selected and widgets.select_keys(page.table, [selected]))
        if page.roots.findData(real) < 0:  # its folder is listed once the roots are read again (in the background)
            page.others.append(("other", "", root))
            page.refresh_roots(select=real, then=go)
        else:
            go()
        window.show_page("files")
        return True

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:
            self.model.recolor()
            self.canaries.recolor()
            self.safeguards.recolor()
