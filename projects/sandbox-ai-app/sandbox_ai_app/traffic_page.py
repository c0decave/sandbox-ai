"""The LLM traffic page: what an agent and its model exchanged, request by request (`sandbox-ai llm traffic`) - the
messages, the system part, the tools, the tool calls and their results as they went to the model, what the relay
changed (sampling parameters, max_tokens, the loop guard's notes and pauses), and the answer put together from its
stream.

The list comes from `sandbox-ai llm traffic SOURCE --summary --json --tail N [--follow] [--grep TEXT]` (a QProcess
whose output is read as it comes: one short line per request); a request's whole record and its readable view from
`sandbox-ai llm traffic SOURCE RUN --request N --json`, read and laid out in a thread. Searching asks the command again
with --grep (the whole run, not only the list). Everything here quotes the agent and the model, so every text is shown
escaped; long texts are cut on the screen (the export holds them whole).

Host at the top: this machine's sandboxes, or a host's of `sandbox-ai hosts` - its sessions and running starts, each
read, followed and exported there (`sandbox-ai hosts run HOST -- llm traffic ...`, SandboxAI.host_command)."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import subprocess

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit,
    QPushButton, QSplitter, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
    QWidget,
)

from . import applog
from . import theme, widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import MONO, flow, plain_label

LOCAL = "local"  # this machine, as sandbox-ai hosts names it

COLUMNS = (N_("#"), N_("Time"), N_("Model"), N_("Tokens in/out"), N_("Time taken"), N_("Status"), N_("New"),
           N_("Loop guard"))
TAIL = 2000              # requests read when a run is opened
KEEP = 10000             # requests kept in the list; the oldest go first
LINE_LIMIT = 1 << 20     # one line of the list's output
TEXT_LIMIT = 20000       # characters of one message shown before "Show all"
HTML_LIMIT = 3 << 20     # characters of the chat's page; beyond, the messages are listed folded
RAW_LIMIT = 1 << 20      # characters of the raw record shown
DETAIL_SECONDS = 120
EXPORT_SECONDS = 600
FILE = re.compile(r"llm-traffic-(\d{8}-\d{6})-([A-Za-z0-9._-]{1,128}?)\.jsonl(?:\.\d{1,3})?(?:\.gz)?")  # .gz: gc
ROLE_COLORS = {"user": "accent", "assistant": "ok", "system": "muted", "tool": "muted", "developer": "muted",
               "error": "bad"}


def short_time(text):
    """HH:MM:SS (local) of a record's time (2026-11-13T10:00:01.250Z), the date before it when it is not today."""
    from .activity_page import short_time as clock
    return clock(text) if isinstance(text, str) and text else "-"


def ms_text(ms):
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "-"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms"


def tokens_text(summary):
    found = [summary.get("tokens_in"), summary.get("tokens_out")]
    if all(value is None for value in found):
        return "-"
    return " / ".join(widgets.number(value) if isinstance(value, int) else "-" for value in found)


def status_text(summary):
    if summary.get("status") is None:
        return tr("no answer")
    return str(summary["status"]) + (f" ({summary['finish_reason']})" if summary.get("finish_reason") else "")


def loop_text(summary):
    marks = [str(mark) for mark in summary.get("loop") or []]
    if summary.get("truncated"):
        marks.append(tr("truncated"))
    return ", ".join(marks)


def cells(summary):
    """The table's texts of one request (SUMMARY: a line of --summary --json)."""
    new = f"+{summary.get('new', 0)} / {summary.get('messages', 0)}"
    return [str(summary.get("n") or "?"), short_time(summary.get("time")), printable(summary.get("model") or "?")[:80],
            tokens_text(summary), ms_text(summary.get("ms")), printable(status_text(summary)), new,
            printable(loop_text(summary))]


class Request:
    __slots__ = ("summary", "cells", "key")

    def __init__(self, summary):
        self.summary = summary
        self.cells = cells(summary)
        self.key = (summary.get("run"), summary.get("n"))


class TrafficModel(QAbstractTableModel):
    """The requests, oldest first (a request read again keeps its row)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.keys = [], set()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and \
                0 <= section < len(COLUMNS):
            return tr(COLUMNS[section])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        request, column = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return request.cells[column]
        if role == Qt.ItemDataRole.ForegroundRole:
            summary = request.summary
            failed = summary.get("error") or not isinstance(summary.get("status"), int) or summary["status"] >= 400
            if column == 5 and failed:
                return QColor(theme.color("bad"))
            if column == 7 and request.cells[7]:
                return QColor(theme.color("warn"))
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            summary = request.summary
            lines = [printable(summary.get("time") or ""), printable(summary.get("endpoint") or summary.get("host")
                                                                      or "")]
            if summary.get("preview"):
                lines.append(printable(summary["preview"])[:300])
            if summary.get("tool_calls"):
                lines.append(tr("Tool calls: {names}", names=printable(", ".join(summary["tool_calls"]))[:300]))
            if summary.get("error"):
                lines.append(printable(summary["error"])[:300])
            return widgets.safe_tooltip("\n".join(line for line in lines if line))
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 3, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def display_text(self, row, column):
        """What row ROW shows in COLUMN, without an index made for it (tables.fit_columns measures with it after every
        read: 200 rows of 8 columns, each cell an index and a call of data())."""
        return self.rows[row].cells[column] if 0 <= row < len(self.rows) and 0 <= column < len(COLUMNS) else None

    def summary_at(self, row):
        return self.rows[row].summary if 0 <= row < len(self.rows) else None

    def add(self, summaries):
        fresh = []
        for summary in summaries:
            request = Request(summary)
            if request.key in self.keys:
                continue
            self.keys.add(request.key)
            fresh.append(request)
        if fresh:
            start = len(self.rows)
            self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
            self.rows.extend(fresh)
            self.endInsertRows()
        if len(self.rows) > KEEP:
            drop = len(self.rows) - KEEP
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            for request in self.rows[:drop]:
                self.keys.discard(request.key)
            del self.rows[:drop]
            self.endRemoveRows()
        return len(fresh)

    def clear(self):
        self.beginResetModel()
        self.rows, self.keys = [], set()
        self.endResetModel()

    def recolor(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLUMNS) - 1))


# -- the chat --------------------------------------------------------------------------------------------------------

def label_of(item):
    kind, role = item.get("kind"), str(item.get("role") or "?")
    name = printable(item.get("name") or "?")
    ident = f" [{printable(item['id'])}]" if item.get("id") else ""
    if kind == "tool_call":
        return tr("{role} → tool call {name}", role=role, name=name) + ident
    if kind == "tool_result":
        return tr("tool result") + ident + (" " + tr("(error)") if item.get("error") else "")
    if kind == "reasoning":
        return tr("{role} (reasoning)", role=role)
    if kind == "other":
        return f"{role} ({name})"
    return role


def folded_by_default(item):
    return item.get("kind") in ("tool_call", "tool_result", "reasoning", "other") or item.get("role") == "system"


def escaped(text):
    return html.escape(printable_text(text))


def chat_html(view, new_only=True, toggled=(), whole=(), colors=None):
    """The chat of a request's VIEW (`--request N --json`'s "view") as HTML: the system part, the messages (only those
    new in this request with NEW_ONLY), the answer. Tool calls, results, reasoning and the system part are folded
    (one line) unless their key is in TOGGLED (and the others the other way round); a long text is cut unless its key
    is in WHOLE. Keys: "s" (system), "m<index>" (messages), "a<index>" (answer); links toggle:KEY and whole:KEY."""
    colors = colors or {}
    parts, size = [], 0

    def block(key, item, relay=None):
        nonlocal size
        folded = folded_by_default(item) != (key in toggled)
        text = str(item.get("text") or "")
        color = colors.get(ROLE_COLORS.get(str(item.get("role")), "text"), "")
        style = f' style="color:{color}"' if color else ""
        arrow = "▸" if folded else "▾"
        title = f'<a href="toggle:{key}">{arrow}</a> <b{style}>{html.escape(label_of(item))}</b>'
        if item.get("new") is False and not new_only:
            title += f' <span style="color:{colors.get("muted", "")}">· {html.escape(tr("earlier"))}</span>'
        if relay:
            title += f' <span style="color:{colors.get("warn", "")}">· {html.escape(tr("the relay added a note"))}' \
                     "</span>"
        if folded or size > HTML_LIMIT:
            first = printable(text.strip().split("\n", 1)[0])[:160]
            parts.append(f"<p>{title} <span style=\"color:{colors.get('muted', '')}\">{html.escape(first)}"
                         f"{'…' if len(text) > len(first) else ''}</span></p>")
            return
        shown = text if key in whole or len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT]
        body = escaped(shown)
        if len(shown) < len(text):
            body += f'\n<a href="whole:{key}">{html.escape(tr("Show all ({count} characters)", count=widgets.number(len(text))))}</a>'
        parts.append(f"<p>{title}</p><pre style=\"white-space: pre-wrap; margin-left: 1.2em\">{body}</pre>")
        size += len(body)

    if view.get("system") and not new_only:
        block("s", {"role": "system", "kind": "text", "text": view["system"]})
    shown = [item for item in view.get("messages") or [] if not new_only or item.get("new")]
    if new_only:
        total = len({item.get("message") for item in view.get("messages") or []})
        new = len({item.get("message") for item in shown})
        parts.append(f"<p style=\"color:{colors.get('muted', '')}\">"
                     f"{html.escape(tr('New in this request: {new} of {total} messages', new=new, total=total))}</p>")
    for index, item in enumerate(shown):
        block(f"m{item.get('message')}-{index}", item, item.get("relay"))
    parts.append(f"<hr><p><b>{html.escape(tr('Answer'))}</b></p>")
    if not view.get("answer"):
        parts.append(f"<p style=\"color:{colors.get('muted', '')}\">{html.escape(tr('No answer recorded.'))}</p>")
    for index, item in enumerate(view.get("answer") or []):
        block(f"a{index}", item)
    return "\n".join(parts)


def parameter_rows(view):
    """[(field, value as sent, change or None)]: the request's parameters, and what the relay changed of them (a field
    it took away or a note it added too)."""
    changes = {str(c.get("field")): c for c in view.get("changes") or [] if isinstance(c, dict)}
    rows = []
    for key, value in sorted((view.get("params") or {}).items()):
        rows.append((key, json.dumps(value, ensure_ascii=False), changes.pop(key, None)))
        if key == "extra_params" and isinstance(value, dict):
            for name in list(changes):
                if name.startswith("extra_params."):
                    rows.append((name, json.dumps(changes[name].get("to"), ensure_ascii=False), changes.pop(name)))
    for field, change in changes.items():
        value = change.get("note") if field == "messages" else change.get("to", tr("(removed)"))
        rows.append((field, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), change))
    return rows


def change_text(change):
    """What the relay did to a field, in words."""
    why = {"parameter": tr("the server's or the start's parameter"), "boost": tr("raised after a cut answer"),
           "loop guard": tr("the loop guard")}.get(str(change.get("why")), printable(change.get("why") or ""))
    if change.get("field") == "messages":
        return tr("The loop guard added a note to {where}", where=printable(change.get("where") or "?"))
    if "from" in change:
        return tr("Was {old}; changed by {why}", old=printable(json.dumps(change["from"], ensure_ascii=False)),
                  why=why)
    return tr("Not set by the agent; set by {why}", why=why)


def traffic_runs(directories, running=()):
    """[(directory, run, started)] of the LLM traffic logs in DIRECTORIES, newest first (only their names read)."""
    found = {}
    for directory in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = FILE.fullmatch(name)
            if match:
                key = (str(directory), match.group(2))
                found[key] = min(found.get(key, match.group(1)), match.group(1))
    return sorted(((directory, run, started) for (directory, run), started in found.items()),
                  key=lambda entry: (entry[1] in running, entry[2]), reverse=True)


def started_text(stamp):
    if not re.fullmatch(r"\d{8}-\d{6}", stamp or ""):
        return printable(stamp or "")
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"


class ChatWindow(QDialog):
    """One request's chat in a window of its own (closes with its [x] or Esc)."""

    def __init__(self, parent, title_text, page_html):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # (closed, it stayed a hidden child of the page)
        self.setWindowTitle(title_text)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setAccessibleName(tr("Chat"))
        self.browser.setHtml(page_html)
        layout.addWidget(self.browser)
        self.resize(900, 700)


class TrafficPage(QWidget):
    """The requests of a run or a session, its chat, parameters and raw record; search, export, follow."""
    status_changed = Signal(str)

    def __init__(self, sai, window=None):
        super().__init__()
        self.sai, self.main_window = sai, window
        self.process, self.buffer, self.errors = None, b"", ""
        self.sources_token, self.detail_token = 0, 0
        self.detail, self.toggled, self.whole = None, set(), set()
        self.windows = []
        self.started = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Whose traffic: this machine's sandboxes, or a host's of `sandbox-ai hosts` (its starts, read and followed
        # there: sandbox-ai llm traffic over SSH).
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The LLM traffic of this machine's sandboxes, or of a host's (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        self.source = QComboBox()
        self.source.setAccessibleName(tr("Run or session"))
        widgets.compact_combo(self.source, 34)
        self.source.currentIndexChanged.connect(lambda _=0: self.restart())
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(lambda: self.refresh_sources())
        other = self.other_button = QPushButton(tr("Other folder…"))
        other.setToolTip(tr("A log folder of this machine"))
        other.clicked.connect(self.browse)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("search the run (messages, tools, answers)"))
        self.search.setAccessibleName(tr("Search"))
        self.search.setMinimumWidth(180)
        self.search.setClearButtonEnabled(True)
        self.research = QTimer(self)  # the command searches: asked again once typing stops
        self.research.setSingleShot(True)
        self.research.setInterval(600)
        self.research.timeout.connect(self.restart)
        self.search.textChanged.connect(lambda _="": self.research.start())
        self.follow = QCheckBox(tr("follow"))
        self.follow.setChecked(True)
        self.follow.setToolTip(tr("New requests appear as the agent sends them"))
        self.follow.toggled.connect(lambda _=False: self.restart())
        self.export_button = QPushButton(tr("Export…"))
        menu = QMenu(self.export_button)
        menu.addAction(tr("This request as Markdown…")).triggered.connect(lambda: self.export("md", True))
        menu.addAction(tr("This request as JSON…")).triggered.connect(lambda: self.export("json", True))
        menu.addSeparator()
        menu.addAction(tr("The whole run as Markdown…")).triggered.connect(lambda: self.export("md", False))
        menu.addAction(tr("The whole run as JSON lines…")).triggered.connect(lambda: self.export("json", False))
        self.export_button.setMenu(menu)
        show = QLabel(tr("Show"))
        show.setBuddy(self.source)
        layout.addWidget(flow(self.hosts, show, self.source, refresh, other, self.search, self.follow,
                              self.export_button))
        split = QSplitter(Qt.Orientation.Horizontal)
        self.model = TrafficModel(self)
        self.table = QTableView()
        self.table.setAccessibleName(tr("Requests"))
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        widgets.tables.setup(self.table, 2, (0, 1, 3, 4, 5, 6))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.load_detail())
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.heading = plain_label(selectable=True)
        self.heading.setAccessibleName(tr("Request"))
        self.new_only = QCheckBox(tr("New in this request"))
        self.new_only.setChecked(True)
        self.new_only.setToolTip(tr("Only the messages this request added to the conversation (what is new since the "
                                    "request it continues); off: the whole conversation as it went to the model"))
        self.new_only.toggled.connect(lambda _=False: self.render())
        self.open_window = QPushButton(tr("Open in a window"))
        self.open_window.clicked.connect(self.show_window)
        row = QHBoxLayout()
        row.addWidget(self.new_only)
        row.addStretch(1)
        row.addWidget(self.open_window)
        widgets.list_menu(self.table, lambda: [self.open_window, self.export_button, None, refresh])
        right_layout.addWidget(self.heading)
        right_layout.addLayout(row)
        self.tabs = QTabWidget()
        widgets.arrow_tabs(self.tabs)  # (the app's tab row: arrows at its end when the tabs do not fit)
        self.chat = QTextBrowser()
        self.chat.setAccessibleName(tr("Chat"))
        self.chat.setOpenLinks(False)
        self.chat.anchorClicked.connect(self.clicked)
        self.chat.setPlaceholderText(tr("Choose a request to see what went to the model and what came back."))
        self.params = QTableWidget(0, 3)
        self.params.setAccessibleName(tr("Parameters"))
        self.params.setHorizontalHeaderLabels([tr("Field"), tr("Value as sent"), tr("The relay's change")])
        self.params.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.params.verticalHeader().setVisible(False)
        widgets.tables.setup(self.params, 2, (0, 1))
        self.raw = QPlainTextEdit()
        self.raw.setAccessibleName(tr("Raw JSON"))
        self.raw.setReadOnly(True)
        self.raw.setFont(MONO)
        self.tabs.addTab(self.chat, tr("Chat"))
        self.tabs.addTab(self.params, tr("Parameters"))
        self.tabs.addTab(self.raw, tr("Raw JSON"))
        right_layout.addWidget(self.tabs, 1)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([480, 620])
        layout.addWidget(split, 1)
        self.status = plain_label(selectable=True, log="traffic", fit=True)
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        self.sources = []
        self.update_buttons()

    # -- what to show --

    def read_sources(self):
        """[(title, {"args", "run"})]: the sessions, then every run with an LLM traffic log in the log folders (the
        default ones and those of running sandboxes), the running first (in a thread: the registry asks Podman)."""
        found = []
        try:
            runs = self.sai.runs()
        except Exception as _exc:  # noqa: BLE001 - the page shows what it can
            applog.swallowed("traffic_page", "TrafficPage.read_sources: the page shows what it can", _exc)
            runs = {}
        try:
            sessions = self.sai.sessions()
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("traffic_page", "TrafficPage.read_sources failed (went on without it)", _exc)
            sessions = []
        for row in sessions:
            name, status = row[0], row[3] if len(row) > 3 else ""
            found.append((tr("Session: {name} ({status})", name=printable(name), status=printable(status)),
                          {"args": ["--session", str(name)], "run": None}))
        directories = [str(d) for d in self.sai.log_directories()]
        for record in runs.values():
            folder = record.get("log_dir") if isinstance(record, dict) else None
            if isinstance(folder, str) and folder and folder not in directories:
                directories.append(folder)
        for directory, run, started in traffic_runs(directories, set(runs)):
            state = tr("running") if run in runs else started_text(started)
            found.append((tr("Run: {run} · {state} ({folder})", run=printable(run), state=state,
                             folder=printable(Path(directory).name)), {"args": ["--log-dir", directory], "run": run}))
        return found

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self):
        """Another host chosen: its starts as the sources, nothing of the host before (its --follow ends)."""
        self.stop()
        self.model.clear()
        self.clear_detail()
        self.sources = []
        self.source.blockSignals(True)
        self.source.clear()
        self.source.blockSignals(False)
        widgets.set_enabled(self.other_button, self.host() == LOCAL,
                            tr("A log folder of this machine: choose this machine as the host."))
        if self.started:
            self.refresh_sources()

    def remote_sources(self, host):
        """[(title, {"args", "run"})] of HOST (in a thread): its sessions and its runs that run now (sandbox-ai list
        --json there); each read and followed there."""
        found = []
        for instance in self.sai.host_instances(host):
            if instance.kind == "session":
                found.append((tr("Session: {name} ({status})", name=printable(instance.name),
                                 status=printable(instance.state)), {"args": ["--session", instance.name], "run": None}))
            elif instance.kind == "run" and instance.state == "running":
                found.append((tr("Run: {run} · {state} ({folder})", run=printable(instance.name), state=tr("running"),
                                 folder=printable(host)), {"args": [], "run": instance.name}))
        return found

    def refresh_sources(self, select=None):
        self.sources_token += 1
        token, host = self.sources_token, self.host()
        if host != LOCAL:
            widgets.background(self.remote_sources, host, owner=self, timeout=120,
                               done=lambda result, error: self.listed(token, select, result, error))
            return
        widgets.background(self.read_sources, owner=self, timeout=90,
                           done=lambda result, error: self.listed(token, select, result, error))

    def listed(self, token, select, result, error):
        if token != self.sources_token:
            return
        self.sources = result if error is None else []
        if error is not None:
            self.status.setText(printable_text(error)[:300])
        current = select or self.source.currentData()
        self.source.blockSignals(True)
        self.source.clear()
        for title, data in self.sources:
            self.source.addItem(title, data)
        if select is not None and self.source.findData(select) < 0:
            self.source.addItem(tr("Folder: {path}", path=printable(select["args"][-1])), select)
        index = self.source.findData(current) if current else -1
        self.source.setCurrentIndex(index if index >= 0 else 0)
        self.source.blockSignals(False)
        self.restart()

    def browse(self):
        from .state import choose_folder
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_sources(select={"args": ["--log-dir", path], "run": None})

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.started:
            self.started = True
            self.refresh_sources()

    def chosen(self):
        data = self.source.currentData()
        return data if isinstance(data, dict) and isinstance(data.get("args"), list) else None

    def source_args(self, run=None):
        """The arguments of `sandbox-ai llm traffic` for the source chosen (and RUN, when given)."""
        chosen = self.chosen()
        if chosen is None:
            return None
        run = run or chosen.get("run")
        return ["llm", "traffic", *chosen["args"], *([run] if run else [])]

    # -- the list --

    def restart(self):
        """Read the requests of the source chosen again (the newest TAIL, then what comes with follow)."""
        self.research.stop()
        self.stop()
        self.model.clear()
        self.detail, self.errors = None, ""
        self.clear_detail()
        arguments = self.source_args()
        host = self.host()
        if host == LOCAL and not (Path(self.sai.directory) / "llm_traffic.py").is_file():
            self.status.setText(tr("This sandbox-ai has no LLM traffic log yet; update it with: sandbox-ai update"))
            return
        if arguments is None:
            self.status.setText(tr("No LLM traffic recorded yet: it is written while a sandbox with --llm runs (or "
                                   "with --llm-traffic all)."))
            return
        query = self.search.text().strip()[:200]
        arguments += ["--summary", "--json", "--tail", str(TAIL), *(["--grep", query] if query else []),
                      *(["--follow"] if self.follow.isChecked() else [])]
        argv = self.sai.command(*arguments) if host == LOCAL else self.sai.host_command(host, *arguments)
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

    def stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
            return
        process.finished.connect(process.deleteLater)
        process.kill()

    def shutdown(self):
        """The app ends: the command too (waited for briefly), and the chat windows."""
        self.stop()  # (kills the command; never waited for on the GUI thread)
        for window in list(self.windows):
            window.close()

    def read(self, process):
        if process is not self.process:
            return
        self.buffer += bytes(process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b"\n")
        if len(self.buffer) > LINE_LIMIT:
            self.buffer = b""
        self.add_lines(lines)

    def add_lines(self, lines):
        summaries = []
        for line in lines:
            if not line.strip() or len(line) > LINE_LIMIT:
                continue
            try:
                summary = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(summary, dict) and isinstance(summary.get("n"), int):
                summaries.append(summary)
        if not summaries:
            return
        bar = self.table.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        self.model.add(summaries)
        widgets.tables.fit_columns(self.table)
        self.update_status()
        if self.follow.isChecked() and at_bottom:
            self.table.scrollToBottom()
        if self.current() is None and self.model.rowCount():
            self.update_buttons()

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
        old = self.sai.too_old_text(self.host(), (code, "", self.errors), "llm traffic") if code == 2 else None
        if old:
            self.status.setText(old)
        elif code != 0 and message:
            self.status.setText(printable_text(message.removeprefix("sandbox-ai llm traffic: ")))
        else:
            self.update_status()

    def update_status(self):
        count = self.model.rowCount()
        text = tr("{count} requests", count=widgets.number(count))
        if self.search.text().strip():
            text = tr("{count} requests contain {text}", count=widgets.number(count),
                      text=printable(self.search.text().strip())[:60])
        self.status.setText(text)
        self.status_changed.emit(text)

    # -- one request --

    def current(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.model.summary_at(rows[0].row()) if rows else None

    def clear_detail(self):
        self.detail_token += 1
        self.detail, self.toggled, self.whole = None, set(), set()
        self.heading.setText("")
        self.chat.clear()
        self.raw.clear()
        self.params.setRowCount(0)
        self.update_buttons()

    def load_detail(self):
        summary = self.current()
        self.clear_detail()
        if summary is None:
            return
        arguments = self.source_args(summary.get("run"))
        if arguments is None:
            return
        self.heading.setText(tr("Request #{n} · {model} · {time}", n=summary.get("n"),
                                model=printable(summary.get("model") or "?"),
                                time=short_time(summary.get("time"))))
        self.chat.setPlainText(tr("Reading…"))
        token = self.detail_token
        argv = [*arguments, "--request", str(summary["n"]), "--json"]
        widgets.background(self.fetch, argv, self.host(), owner=self, timeout=DETAIL_SECONDS,
                           done=lambda result, error: self.fetched(token, result, error))

    def fetch(self, argv, host=LOCAL):
        """The record and its view (in a thread): `sandbox-ai llm traffic ... --request N --json` (on HOST), and the
        raw JSON's text."""
        code, out, err = self.sai.call(*argv, timeout=DETAIL_SECONDS) if host == LOCAL else \
            self.sai.host_run(host, *argv, timeout=DETAIL_SECONDS)
        if code != 0:
            raise ValueError((err.strip().splitlines() or [tr("no answer")])[-1].removeprefix("sandbox-ai llm traffic: "))
        document = json.loads(out.strip().splitlines()[-1])
        raw = json.dumps(document.get("record"), indent=2, ensure_ascii=False)
        if len(raw) > RAW_LIMIT:
            raw = raw[:RAW_LIMIT] + "\n…" + tr("(cut here: Export… holds all of it)")
        return document, printable_text(raw)

    def fetched(self, token, result, error):
        if token != self.detail_token:
            return
        if error is not None:
            self.chat.setPlainText(printable_text(error)[:500])
            return
        self.detail, raw = result
        self.raw.setPlainText(raw)
        self.fill_params()
        self.render()
        self.update_buttons()

    def colors(self):
        tokens = theme.colors()
        return {"accent": tokens.get("accent"), "ok": theme.color("ok"), "warn": theme.color("warn"),
                "bad": theme.color("bad"), "muted": theme.color("muted"), "text": tokens.get("text")}

    def page_html(self):
        view = (self.detail or {}).get("view") or {}
        return chat_html(view, self.new_only.isChecked(), self.toggled, self.whole, self.colors())

    def render(self):
        if self.detail is None:
            return
        bar = self.chat.verticalScrollBar()
        position = bar.value()
        self.chat.setHtml(self.page_html())
        bar.setValue(position)

    def clicked(self, url):
        target = url.toString() if isinstance(url, QUrl) else str(url)
        kind, _, key = target.partition(":")
        if kind == "toggle":
            self.toggled ^= {key}
        elif kind == "whole":
            self.whole.add(key)
        else:
            return
        self.render()

    def fill_params(self):
        view = (self.detail or {}).get("view") or {}
        rows = parameter_rows(view)
        self.params.setRowCount(len(rows))
        warn = QColor(theme.color("warn"))
        for index, (field, value, change) in enumerate(rows):
            items = [QTableWidgetItem(printable(field)), QTableWidgetItem(printable(value)[:2000]),
                     QTableWidgetItem(change_text(change) if change else "")]
            for column, item in enumerate(items):
                item.setToolTip(widgets.safe_tooltip(item.text()))
                if change:
                    item.setForeground(warn)
                self.params.setItem(index, column, item)
        widgets.tables.fit_columns(self.params, force=True)

    def show_window(self):
        if self.detail is None:
            return None
        summary = (self.detail or {}).get("summary") or {}
        window = ChatWindow(self, tr("Request #{n} · {model}", n=summary.get("n"),
                                     model=printable(summary.get("model") or "?")), self.page_html())
        window.browser.anchorClicked.connect(lambda url, w=window: self.window_clicked(w, url))
        window.toggled, window.whole, window.view = set(self.toggled), set(self.whole), self.detail.get("view") or {}
        window.new_only = self.new_only.isChecked()
        window.finished.connect(lambda _=0, w=window: self.windows.remove(w) if w in self.windows else None)
        self.windows.append(window)
        window.show()
        return window

    def window_clicked(self, window, url):
        kind, _, key = url.toString().partition(":")
        if kind == "toggle":
            window.toggled ^= {key}
        elif kind == "whole":
            window.whole.add(key)
        window.browser.setHtml(chat_html(window.view, window.new_only, window.toggled, window.whole, self.colors()))

    def update_buttons(self):
        has = self.detail is not None
        widgets.set_enabled(self.open_window, has, tr("Choose a request"))
        widgets.set_enabled(self.export_button, self.chosen() is not None and self.model.rowCount() > 0,
                            tr("No request to export"))

    # -- export --

    def export(self, kind, one):
        """Write the request chosen (ONE) or the whole run as Markdown or JSON (lines), in a thread; the path, or
        None when nothing was written."""
        summary = self.current() if one else None
        if one and summary is None:
            self.status.setText(tr("Choose a request first."))
            return None
        run = (summary or {}).get("run") or (self.model.summary_at(0) or {}).get("run")
        arguments = self.source_args(run)
        if arguments is None:
            return None
        arguments += [*(["--request", str(summary["n"]), "--full"] if one else []),
                      "--markdown" if kind == "md" else "--json"]
        name = f"llm-traffic-{run or 'run'}" + (f"-{summary['n']}" if one else "") + (".md" if kind == "md" else
                                                                                       ".jsonl")
        from .state import choose_save
        path = choose_save(self, tr("Export the LLM traffic"), name,
                           tr("Markdown (*.md)") if kind == "md" else tr("JSON lines (*.jsonl)"), "export")
        if not path:
            return None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            widgets.warn(self, tr("Not exported"), tr("Cannot write {path}: {error}", path=printable(path),
                                                      error=printable_text(exc)))
            return None
        host = self.host()
        argv = self.sai.command(*arguments) if host == LOCAL else self.sai.host_command(host, *arguments)

        def write():
            try:
                done = subprocess.run(argv, stdout=fd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                      timeout=EXPORT_SECONDS, check=False)
            finally:
                os.close(fd)
            if done.returncode:
                raise ValueError(done.stderr.decode("utf-8", "replace").strip()[-300:] or tr("no answer"))
            return path
        self.status.setText(tr("Exporting…"))
        widgets.background(write, owner=self, timeout=EXPORT_SECONDS + 10, work=True,
                           done=lambda result, error: self.exported(path, error))
        return path

    def exported(self, path, error):
        if error is not None:
            self.status.setText(tr("Not exported: {error}", error=printable_text(error)[:300]))
            return
        self.status.setText(tr("Exported to {path}", path=printable(path)))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:
            self.model.recolor()
            if self.detail is not None:
                self.fill_params()
                self.render()
