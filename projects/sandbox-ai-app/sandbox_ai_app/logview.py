"""The log window: one place for everything the app reports - its own messages (a board's notes, what a page could not
read), warnings and errors, exceptions, Qt's warnings and, on request, the debug entries - instead of a line of notes
on each page. A panel at the bottom of the window (on every page; it can be undocked, folded and closed like the
others), with a level to show from, a filter and Copy / Clear.

Entries come from applog (any thread) through LogHub, which keeps the last MAX_ENTRIES and hands each new one to the
GUI thread. The panel shows them as plain text (paths and names may be the agent's). The entries shown can be narrowed
to one area (where they come from: board, command, terminal, SandboxesPage...), and "Copy for a bug report" puts the
end of app.log and launcher.log with the versions on the clipboard, secrets masked (bug_report())."""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import platform
import threading
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import applog, theme
from .i18n import N_, tr
from .files import printable_text
from .widgets import MONO

MAX_ENTRIES = 5000
# The levels to show from (the panel's chooser): each shows its level and the ones above it.
SHOW_FROM = (("ERROR", N_("Errors")), ("WARNING", N_("Warnings")), ("INFO", N_("Info")), ("DEBUG", N_("Debug")))
RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "FATAL": 4}
DEFAULT_LEVEL = "INFO"
REPORT_LINES = 400  # lines of app.log (and of launcher.log) a bug report takes from their end
REPORT_READ = 512 << 10  # bytes read from the end of each file for it


def area_of(where):
    """The area of an entry's WHERE, as the log window's area chooser lists it: the component without its function
    or place ("board", "command", "SandboxesPage", "qt" for "qt.qpa.xcb file.cpp:12", "files_page" for
    "files_page.Pane.list")."""
    text = str(where or "").strip()
    return text.split(" ", 1)[0].split(".", 1)[0] or "app"


def file_tail(path, lines=REPORT_LINES, limit=REPORT_READ):
    """The last LINES lines of the file PATH (at most LIMIT bytes read from its end), or None when it cannot be read."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    found = text.splitlines()
    if size > limit and found:
        found = found[1:]  # (the first line read is cut)
    return "\n".join(found[-lines:])


def masked(text):
    """TEXT for a bug report: every secret masked (applog.redact), the home folder said as ~."""
    text = applog.redact(text)
    home = str(Path.home())
    return text.replace(home, "~") if home and home not in ("/", "") else text


def bug_report(entries=(), lines=REPORT_LINES):
    """What "Copy for a bug report" puts on the clipboard: the versions (the app, sandbox-ai, Python, Qt, the
    system), the end of app.log and of launcher.log (each its last LINES lines) - or, without app.log, the log
    window's own entries (ENTRIES) - every secret masked and the home folder said as ~."""
    known = applog.versions()
    parts = ["sandbox-ai app: bug report", "Versions: " + ", ".join(
        f"{name} {value}" for name, value in known.items() if name != "sandbox-ai home"),
        f"System: {platform.platform()}", f"Log: {applog.log_path()}"]
    app_log = file_tail(applog.log_path(), lines)
    if app_log is None:
        app_log = "\n".join(line_of(entry) for entry in list(entries)[-lines:])
        parts += ["", f"--- the log window's last {lines} entries (app.log could not be read) ---", app_log]
    else:
        parts += ["", f"--- app.log, its last {lines} lines ---", app_log]
    launcher = file_tail(applog.log_path().with_name("launcher.log"), lines)
    parts += ["", f"--- launcher.log, its last {lines} lines ---", launcher if launcher is not None else
              "(none: the launcher wrote no log here)"]
    return masked("\n".join(parts)) + "\n"


class LogHub(QObject):
    """Every entry of applog, kept (the last MAX_ENTRIES) and announced in the GUI thread (added: the entries that
    arrived together, in their order)."""
    added = Signal(object)
    _from_thread = Signal()

    def __init__(self):
        super().__init__()
        self.entries = deque(maxlen=MAX_ENTRIES)
        self.lock = threading.Lock()
        # The entries logged since the event loop last handed them on: a burst (a page reporting a lot, Debug) goes
        # to the panels together - one insertion and one scroll per panel, not one each (2,000 entries held the
        # window 0.3 s per panel).
        self.pending = []
        # Queued: an entry logged in a worker thread reaches the panel in the GUI thread.
        self._from_thread.connect(self._arrived, Qt.ConnectionType.QueuedConnection)
        applog.add_listener(self.receive)

    def receive(self, entry):
        """Always through the event loop, also from the GUI thread: an entry may come from inside Qt (its message
        handler during a layout or a paint, an exception hook) - changing a widget right there could re-enter Qt.
        One event for the entries that come before it is handled."""
        with self.lock:
            self.pending.append(entry)
            first = len(self.pending) == 1
        if first:
            self._from_thread.emit()

    def _arrived(self, entry=None):
        """The entries pending (and ENTRY), kept and announced."""
        with self.lock:
            batch, self.pending = self.pending, []
            if entry is not None:
                batch.append(entry)
            self.entries.extend(batch)
        if batch:
            self.added.emit(batch)

    def snapshot(self):
        with self.lock:
            return list(self.entries)

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.pending.clear()


_hub = None
# A message whose line in the log is clicked does something: {its first line: function} (the last LINKS_KEPT) - the
# Board's "Preparing (2 of 4): ..." opens the terminal tab where it runs.
LINKS = {}
LINKS_KEPT = 50


def link(message, function):
    """A click on the line of MESSAGE in a log panel calls FUNCTION() (the last one given for that message)."""
    key = str(message).split("\n", 1)[0].strip()
    if not key:
        return
    LINKS.pop(key, None)
    LINKS[key] = function
    while len(LINKS) > LINKS_KEPT:
        LINKS.pop(next(iter(LINKS)))


def link_of(line):
    """The function of the message whose line LINE is (a line of the log panel), else None."""
    text = str(line)
    for key in reversed(list(LINKS)):
        if text.endswith(": " + printable_text(key)):
            return LINKS[key]
    return None


PANELS = weakref.WeakSet()  # every LogPanel alive (each chooses its own level)


def shown_levels():
    """The level each LogPanel still alive shows from (one whose window was deleted is forgotten)."""
    levels = []
    for panel in list(PANELS):
        try:
            levels.append(panel.chosen())
        except RuntimeError:  # its Qt object is gone
            PANELS.discard(panel)
    return levels


def hub():
    """The app's one LogHub (made on first use, in the GUI thread)."""
    global _hub
    if _hub is None:
        _hub = LogHub()
    return _hub


def line_of(entry):
    """One entry as text: its time (hh:mm:ss), level, where, and the message (further lines indented); made once
    per entry (kept in it as "line": the filter and every redraw read it again)."""
    line = entry.get("line")
    if line is not None:
        return line
    time = entry.get("time", "")
    clock = time[11:19] if len(time) >= 19 else time
    message = printable_text(str(entry.get("message", "")).rstrip("\n"))
    line = f"{clock} {entry.get('level', 'INFO'):<7} {printable_text(entry.get('where', ''))}: " + \
        message.replace("\n", "\n    ")
    entry["line"] = line
    return line


class LogPanel(QWidget):
    """The log window's content: level chooser, filter, Copy, Clear, and the entries."""
    unread = Signal(int)  # warnings and errors that arrived while the panel was folded or hidden
    own_window = Signal()  # its button "Own window": the log in a window of its own (LogWindow)

    def __init__(self, level=DEFAULT_LEVEL, detachable=False):
        super().__init__()
        PANELS.add(self)
        self.setToolTip(tr("Everything the app reports: messages, warnings, errors and, at Debug, what it does"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 4)
        layout.setSpacing(2)
        bar = QHBoxLayout()
        self.level = QComboBox()
        self.level.setAccessibleName(tr("Show from level"))
        for key, title in SHOW_FROM:
            self.level.addItem(tr(title), key)
        self.level.setCurrentIndex(max(0, self.level.findData(level)))
        self.level.currentIndexChanged.connect(lambda _i: self.level_changed())
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter"))
        self.filter.setAccessibleName(tr("Filter the log"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda _t: self.show_all())
        self.area = QComboBox()
        self.area.setAccessibleName(tr("Show the area"))
        self.area.setToolTip(tr("Show only the entries of one area (where they come from: the Board, commands, "
                                "terminals, a page)"))
        self.area.addItem(tr("All areas"), None)
        self.areas = set()
        self.area.currentIndexChanged.connect(lambda _i: self.show_all())
        copy = QPushButton(tr("Copy"))
        copy.setToolTip(tr("Copy the entries shown"))
        copy.clicked.connect(lambda _=False: QApplication.clipboard().setText(self.text.toPlainText()))
        report = QPushButton(tr("Copy for a bug report"))
        report.setToolTip(tr("Copy the end of app.log and launcher.log with the versions, secrets masked, to paste "
                             "into a bug report"))
        report.clicked.connect(lambda _=False: self.copy_report())
        clear = QPushButton(tr("Clear"))
        clear.setToolTip(tr("Empty the log window (app.log keeps everything)"))
        clear.clicked.connect(lambda _=False: self.clear())
        bar.addWidget(self.level)
        bar.addWidget(self.area)
        bar.addWidget(self.filter, 1)
        bar.addWidget(copy)
        bar.addWidget(report)
        bar.addWidget(clear)
        if detachable:
            window = QPushButton(tr("Own window"))
            window.setToolTip(tr("The log in a window of its own, usable while a dialog is open (Ctrl+Shift+L)"))
            window.clicked.connect(lambda _=False: self.own_window.emit())
            bar.addWidget(window)
        layout.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(MONO)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setMaximumBlockCount(MAX_ENTRIES * 2)
        self.text.setAccessibleName(tr("Log"))
        self.text.viewport().installEventFilter(self)  # (a click on a line with a link: link())
        layout.addWidget(self.text, 1)
        self.unread_count = 0
        self.watching = False  # the panel is unfolded and visible: nothing counts as unread
        self.hub = hub()
        self.hub.added.connect(self.add)
        self.note_areas(self.hub.snapshot())
        self.level_changed()

    def chosen(self):
        return self.level.currentData() or DEFAULT_LEVEL

    def note_areas(self, entries):
        """The areas of ENTRIES offered in the area chooser (sorted; the one chosen stays chosen)."""
        found = {area_of(entry.get("where")) for entry in entries} - self.areas
        if not found:
            return
        self.areas |= found
        chosen = self.area.currentData()
        self.area.blockSignals(True)
        try:
            while self.area.count() > 1:
                self.area.removeItem(1)
            for area in sorted(self.areas, key=str.lower):
                self.area.addItem(printable_text(area), area)
            self.area.setCurrentIndex(max(0, self.area.findData(chosen)) if chosen is not None else 0)
        finally:
            self.area.blockSignals(False)

    def copy_report(self):
        """Copy for a bug report: bug_report() on the clipboard; said in the log (how much, not what)."""
        text = bug_report(self.hub.snapshot())
        QApplication.clipboard().setText(text)
        applog.log("INFO", "log", f"a bug report was copied to the clipboard ({len(text.splitlines())} lines, secrets "
                                  "masked)")
        return text

    def eventFilter(self, watched, event):  # noqa: N802
        """A click (no text selected by it) on the line of a message with a link (link()) does what it says."""
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and \
                watched is self.text.viewport() and not self.text.textCursor().hasSelection():
            self.click_line(self.text.cursorForPosition(event.position().toPoint()).block().text())
        return super().eventFilter(watched, event)

    def click_line(self, line):
        """LINE of the panel clicked: its message's link followed (True), if it has one."""
        function = link_of(line)
        if function is None:
            return False
        try:
            function()
        except Exception as exc:  # noqa: BLE001 - a link that cannot be followed: said, never a crash
            applog.log("WARNING", "log", f"the link of this line could not be followed: {exc}")
        return True

    def level_changed(self):
        # (debug entries are made while any panel - the docked one, the log window of its own - shows them)
        applog.want_debug(any(level == "DEBUG" for level in shown_levels()))
        self.show_all()

    def shows(self, entry):
        return self.shown_by()(entry)

    def shown_by(self):
        """shows() as a function, with the level and the filter read once (show_all asks it for each of up to
        MAX_ENTRIES entries: reading both widgets for each was most of a redraw - at every key typed into the filter)."""
        least, needle = RANK[self.chosen()], self.filter.text().strip().lower()
        area = self.area.currentData()

        def shows(entry):
            if RANK.get(entry.get("level"), 1) < least:
                return False
            if area is not None and area_of(entry.get("where")) != area:
                return False
            return not needle or needle in line_of(entry).lower()
        return shows

    def formats(self):
        """The colours of the levels in the theme now (made again only when the theme changed)."""
        key = theme.current() if hasattr(theme, "current") else None
        cached = getattr(self, "_formats", None)
        if cached is not None and cached[0] == key and key is not None:
            return cached[1]
        colors = theme.colors()
        result = {}
        for level, key in (("ERROR", "bad"), ("FATAL", "bad"), ("WARNING", "warn"), ("DEBUG", "secondary")):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme.color(key) if key in ("bad", "warn") else colors.get(key, colors["text"])))
            result[level] = fmt
        self._formats = (theme.current() if hasattr(theme, "current") else None, result)
        return result

    def append(self, entry, formats):
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(line_of(entry), formats.get(entry.get("level"), QTextCharFormat()))

    def insert_lines(self, cursor, lines, fmt):
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText("\n".join(lines), fmt)

    def show_all(self):
        """The entries again, after the level or the filter changed."""
        formats = self.formats()
        self.text.setUpdatesEnabled(False)
        try:
            self.text.clear()
            shows = self.shown_by()
            self.insert_entries([entry for entry in self.hub.snapshot() if shows(entry)], formats)
        finally:
            self.text.setUpdatesEnabled(True)
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def insert_entries(self, entries, formats):
        """ENTRIES at the end. The entries of one level that follow each other go in as one text, in one edit: one
        insertion per entry took half a second for the MAX_ENTRIES at Debug - at every new theme and every key typed
        into the filter."""
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        lines, level = [], None
        for entry in entries:
            if lines and entry.get("level") != level:
                self.insert_lines(cursor, lines, formats.get(level, QTextCharFormat()))
                lines = []
            level = entry.get("level")
            lines.append(line_of(entry))
        if lines:
            self.insert_lines(cursor, lines, formats.get(level, QTextCharFormat()))
        cursor.endEditBlock()

    def add(self, entries):
        """New ENTRIES (LogHub.added: a list; one entry alone also does)."""
        entries = [entries] if isinstance(entries, dict) else entries
        self.note_areas(entries)
        shows = self.shown_by()
        shown = [entry for entry in entries if shows(entry)]
        if not shown:
            return
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 2
        if len(shown) == 1:
            self.append(shown[0], self.formats())
        else:
            self.insert_entries(shown, self.formats())
        if at_end:  # following the end; someone reading further up keeps their place
            bar.setValue(bar.maximum())
        if not self.watching:
            for entry in shown:
                if RANK.get(entry.get("level"), 1) >= RANK["WARNING"]:
                    self.unread_count += 1
                    self.unread.emit(self.unread_count)

    def changeEvent(self, event):  # noqa: N802
        """A new theme (or the system's colours): the levels' colours made again, the entries drawn in them."""
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "_formats", None) is not None:
            self._formats = None
            self.show_all()
        super().changeEvent(event)

    def set_watching(self, on):
        self.watching = bool(on)
        if on and self.unread_count:
            self.unread_count = 0
            self.unread.emit(0)

    def clear(self):
        self.hub.clear()
        self.text.clear()
        self.unread_count = 0
        self.unread.emit(0)


class LogWindow(QWidget):
    """The log in a window of its own (View > Log in its own window, Ctrl+Shift+L, also inside a dialog): a top-level
    window without a parent, so no dialog blocks it (widgets.nonblocking_dialogs; a file dialog blocks its window only): what
    a dialog reported can be read and copied while the dialog is still open. Closing it only hides it."""

    def __init__(self, level=DEFAULT_LEVEL):
        super().__init__(None, Qt.WindowType.Window)
        from .widgets import close_controls
        close_controls(self)  # an [x] at its top right, Esc closes it
        self.setObjectName("logWindow")
        self.setWindowTitle(tr("sandbox-ai: log"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = LogPanel(level)
        self.panel.set_watching(True)
        layout.addWidget(self.panel)
        self.resize(900, 420)

    def bring_up(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
