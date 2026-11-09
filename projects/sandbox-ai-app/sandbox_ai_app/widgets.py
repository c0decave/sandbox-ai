"""Small widgets and helpers shared by the pages.

Everything that shows text from sandbox-ai or from a sandbox (host names, URLs, file names, command output)
shows it as plain text: message boxes, labels and form rows force Qt.PlainText, tooltips are escaped. Long
work never runs on the GUI thread: AsyncCommand runs a command (in its own process group, so Cancel ends it
and its children), ThreadCall runs a function, Task shows either inline with a Cancel button."""
from __future__ import annotations

import html
import os
import signal
import sys
import threading
import time

from PySide6.QtCore import QItemSelectionModel, QObject, QProcess, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLayout,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QWidgetItem,
)

from . import i18n, theme
from . import applog
from .i18n import tr

MONO = QFont("monospace")
MONO.setStyleHint(QFont.StyleHint.Monospace)
CELL_LIMIT = 300  # characters shown in a table cell; the rest is in its tooltip
TOOLTIP_LIMIT = 2000
SORT_ROLE = Qt.ItemDataRole.UserRole + 7
BAR_ROLE = Qt.ItemDataRole.UserRole + 8
BADGE_ROLE = Qt.ItemDataRole.UserRole + 9
# Tests set this to True or False: confirm() then answers at once, and messages are collected in SHOWN.
AUTO_ANSWER = None
SHOWN = []


# -- text --

def safe_tooltip(text, limit=TOOLTIP_LIMIT):
    """TEXT for setToolTip: escaped and marked as rich text, so markup in it is shown, never interpreted."""
    if not text:
        return ""
    text = str(text)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "<p style='white-space:pre-wrap'>" + html.escape(text, quote=False) + "</p>"


def shown_as_text(text):
    """TEXT for a place Qt reads as rich text when it looks like markup (Qt.AutoText: the labels of QInputDialog and of
    the static QMessageBox calls): escaped and marked as rich text, so a name an agent or a host chose (a file
    "<b>Allow</b>", a URL) is shown as it is, never interpreted."""
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def elide(text, limit=CELL_LIMIT):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def number(value):
    """12,345 or 12.345 (German): thousands separated."""
    text = f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{int(value):,}"
    if i18n.language() == "de":
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def local_time(text, seconds=False):
    """A time as sandbox-ai writes it (UTC, 2026-11-03T10:00:00Z) in local time, or the text itself."""
    from .backend import parse_time
    moment = parse_time(text)
    if moment is None:
        return str(text or "")
    pattern = "%d.%m.%Y %H:%M" if i18n.language() == "de" else "%Y-%m-%d %H:%M"
    return time.strftime(pattern + (":%S" if seconds else ""), time.localtime(moment))


def plain_label(text="", word_wrap=True, role=None):
    """A label that never interprets its text as HTML (texts from logs and questions come from the agent)."""
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(word_wrap)
    label.setText(str(text))
    if role:
        label.setProperty("role", role)
    return label


class ElidedLabel(QLabel):
    """A one-line label that never asks for more room than ABOUT characters: a longer text is cut with "…" where the
    room ends, whole in its tooltip (a status line under a canvas that does not scroll)."""

    def __init__(self, about=40, role=None):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.about = about
        self.elides = True  # cut on purpose (tests/test_layout.py: not a clipped text)
        if role:
            self.setProperty("role", role)

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        return QSize(min(hint.width(), self.fontMetrics().averageCharWidth() * self.about), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text):  # noqa: N802
        super().setText(str(text))
        self.setToolTip(safe_tooltip(text) if text else "")

    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), text)


def title(text, subtitle=None):
    """The heading of a page: a title and an optional line of explanation."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(2)
    heading = plain_label(text, word_wrap=False, role="title")
    heading.setAccessibleName(text)
    layout.addWidget(heading)
    if subtitle:
        layout.addWidget(plain_label(subtitle, role="secondary"))
    return box


def section(text):
    return plain_label(text, word_wrap=False, role="section")


def button(text, tooltip, slot=None, role=None, shortcut=None):
    """A push button with a tooltip (every button explains itself) and an optional role (primary, danger)."""
    widget = QPushButton(text)
    widget.setToolTip(safe_tooltip(tooltip))
    if role:
        widget.setProperty("role", role)
    if shortcut:
        widget.setShortcut(shortcut)
    if slot:
        widget.clicked.connect(lambda _=False: slot())
    return widget


def set_enabled(widget, enabled, why=None, tooltip=None):
    """Enable WIDGET; when disabled, its tooltip says why."""
    widget.setEnabled(bool(enabled))
    if not hasattr(widget, "_tooltip"):
        widget._tooltip = tooltip if tooltip is not None else widget.toolTip()
    widget.setToolTip(widget._tooltip if enabled or not why else safe_tooltip(why))


# -- dialogs (plain text only) --

LONG_MESSAGE = (24, 160)  # lines, characters in a line: a message beyond either scrolls (long_message)


def long_message(text):
    """Whether TEXT is too long for a plain message box: it is shown in a scrolling area then, never cut."""
    lines = str(text).split("\n")
    return len(lines) > LONG_MESSAGE[0] or any(len(line) > LONG_MESSAGE[1] for line in lines)


def _scrolling(box, label):
    """The message LABEL of the QMessageBox BOX inside a scroll area in its place: a long question (every workspace
    and option of a project file) is all there to read, the buttons stay on the screen."""
    layout = box.layout()
    index = layout.indexOf(label) if layout is not None else -1
    if index < 0:
        return
    row, column, rows, columns = layout.getItemPosition(index)
    layout.removeWidget(label)
    area = QScrollArea(box)
    area.setObjectName("message_scroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(label)
    area.setMinimumSize(640, 360)
    area.setAccessibleName(tr("Message"))
    layout.addWidget(area, row, column, rows, columns)


def _box(parent, icon, title_text, text, details=None):
    box = QMessageBox(icon, str(title_text), str(text), QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    label = box.findChild(QLabel, "qt_msgbox_label")
    if label is not None:
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if long_message(text):
            _scrolling(box, label)
    if details:
        box.setDetailedText(str(details))  # always plain text
    return box


def inform(parent, title_text, text, details=None, warning=False, modal=True):
    """A message with OK. Not MODAL (the outcome of a finished background job): shown without blocking
    anything, so the Approvals page stays usable while it is open; inform() then returns at once."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, details))
        return
    box = _box(parent, QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information, title_text, text, details)
    box.addButton(tr("OK"), QMessageBox.ButtonRole.AcceptRole)
    if modal:
        box.exec()
        return
    show_modeless(box)


def warn(parent, title_text, text, details=None, modal=True):
    inform(parent, title_text, text, details, warning=True, modal=modal)


def show_modeless(dialog):
    """Show DIALOG without blocking any window; it is deleted when it is closed."""
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()
    return dialog


def confirm(parent, title_text, text, accept, destructive=False, details=None, reject=None):
    """True if the human chose ACCEPT (a verb, e.g. "Delete"); the safe choice is the default."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, details))
        return bool(AUTO_ANSWER)
    box = _box(parent, QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question, title_text, text,
               details)
    yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(reject or tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
    if destructive:
        yes.setProperty("role", "danger")
    box.setDefaultButton(no if destructive else yes)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


def ask_first(parent, action, title_text, text, accept, detail=None, details=None, reject=None):
    """The app's one confirmation policy: True when ACTION (a key of backend.NEEDS_CONFIRM or APP_NEEDS_CONFIRM) may
    go ahead - at once when backend.needs_confirm(ACTION, DETAIL) says it needs no confirmation, else only when the
    human chose ACCEPT in a destructive confirmation (Cancel the default)."""
    from . import backend  # here: backend never imports Qt, and the widgets need no backend otherwise
    if backend.needs_confirm(action, detail) is None:
        return True
    return confirm(parent, title_text, text, accept, destructive=True, details=details, reject=reject)


# -- fields where a secret's value is typed: a "Show value" toggle beside each, on by default --
# What is typed is shown while typing unless the human turns it off; the choice is one for every such field, kept in the
# app's state (state.json: SHOW_VALUES). A value already stored is never read back into any field.
SHOW_VALUES = "show_secret_values"
_values_shown = [True]  # without a State (tests, a dialog before the window): this run's choice
_toggles = []  # the toggles shown now: a change in one is the change of all


def values_shown():
    from . import state
    kept = state.current()
    return kept.get(SHOW_VALUES, True, bool) if kept is not None else _values_shown[0]


def set_values_shown(on):
    from . import state
    _values_shown[0] = bool(on)
    kept = state.current()
    if kept is not None:
        kept.set(SHOW_VALUES, bool(on))
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover
        isValid = lambda _w: True  # noqa: E731
    _toggles[:] = [t for t in _toggles if isValid(t)]
    for toggle in _toggles:
        toggle.blockSignals(True)
        toggle.setChecked(bool(on))
        toggle.blockSignals(False)
        apply_echo(toggle.edit)


def apply_echo(edit):
    """EDIT's echo mode: hidden (Password) only for a secret's value while "Show value" is off."""
    from PySide6.QtWidgets import QLineEdit
    hidden = bool(edit.property("secret_value")) and not values_shown()
    edit.setEchoMode(QLineEdit.EchoMode.Password if hidden else QLineEdit.EchoMode.Normal)


def secret_value(edit, secret=True):
    """EDIT (a QLineEdit for a secret's value) with its "Show value" toggle beside it: the row's widget to lay out.
    SECRET False: not a secret's value now (e.g. a token field that names a variable): shown as typed, the toggle
    greyed out. The toggle is edit.show_toggle."""
    from PySide6.QtWidgets import QCheckBox
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    toggle = QCheckBox(tr("Show value"))
    toggle.setAccessibleName(tr("Show value"))
    toggle.setToolTip(tr("Show what is typed here (a value kept already is never read back)"))
    toggle.edit = edit
    toggle.setChecked(values_shown())
    toggle.toggled.connect(set_values_shown)
    edit.show_toggle = toggle
    _toggles.append(toggle)
    row.addWidget(edit, 1)
    row.addWidget(toggle)
    mark_secret(edit, secret)
    return box


def mark_secret(edit, secret=True):
    """Whether EDIT holds a secret's value now (its echo and toggle follow)."""
    edit.setProperty("secret_value", bool(secret))
    toggle = getattr(edit, "show_toggle", None)
    if toggle is not None:
        toggle.setEnabled(bool(secret))
    apply_echo(edit)


def show_text(parent, title_text, text, modal=True):
    """TEXT in a read-only view; not MODAL: shown without blocking (see inform)."""
    if AUTO_ANSWER is not None:
        SHOWN.append((title_text, text, None))
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(str(title_text))
    dialog.resize(760, 520)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit(str(text))
    view.setReadOnly(True)
    view.setFont(MONO)
    layout.addWidget(view)
    buttons = QDialogButtonBox()
    buttons.addButton(tr("Close"), QDialogButtonBox.ButtonRole.RejectRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if modal:
        dialog.exec()
        return
    show_modeless(dialog)


class Notice(QWidget):
    """An inline message of a page: a sentence in plain words, the raw details expandable (plain text)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.label = plain_label()
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.toggle = QToolButton()
        self.toggle.setText(tr("Details"))
        self.toggle.setToolTip(tr("Show or hide what sandbox-ai printed"))
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(lambda on: self.details.setVisible(on))
        close = QToolButton()
        close.setText("×")
        close.setToolTip(tr("Hide this message"))
        close.clicked.connect(self.clear)
        row.addWidget(self.label, 1)
        row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(MONO)
        self.details.setMaximumHeight(160)
        self.details.hide()
        layout.addWidget(self.details)
        self.hide()

    def show_message(self, text, level="info", details=None):
        self.label.setText(str(text))
        self.label.setProperty("role", {"warn": "notice-warn", "error": "notice-error", "ok": "notice-ok"}.get(level))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        details = (details or "").strip()
        self.details.setPlainText(details)
        self.toggle.setVisible(bool(details))
        self.toggle.setChecked(False)
        self.details.hide()
        self.show()

    def text(self):
        return self.label.text() if self.isVisibleTo(self.parentWidget() or self) or not self.isHidden() else ""

    def clear(self):
        self.label.setText("")
        self.details.setPlainText("")
        self.hide()


# -- background work --

# The command becomes the leader of a process group of its own, so that Cancel reaches its children too.
SETSID = ("import os, sys\n"
          "os.setsid()\n"
          "try:\n"
          "    os.execvp(sys.argv[1], sys.argv[1:])\n"
          "except OSError as exc:\n"
          "    sys.stderr.write(f'cannot run {sys.argv[1]!r}: {exc.strerror}\\n')\n"
          "    sys.exit(127)\n")
CANNOT_RUN = "cannot run "  # the wrapper's own message: the command did not start (status -1, not 127)


class AsyncCommand(QObject):
    """A command run with QProcess, without blocking the window: finished(exit status, stdout, stderr).
    A command that cannot start, crashes, is cancelled or takes longer than TIMEOUT seconds finishes with
    status -1. INPUT (bytes) is written to its standard input, which is then closed (secrets go this way)."""
    finished = Signal(int, str, str)
    KILL_AFTER = 1500  # ms between SIGTERM and SIGKILL when a command is stopped
    stopped = {}  # {QProcess: pid} of every stopped command still running (end_stopped at the app's end)

    def __init__(self, timeout=120, parent=None):
        super().__init__(parent)
        self.timeout, self.process, self.cancelled = timeout, None, False
        self.stopping = set()  # stopped commands that have not ended yet (kept until they have)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.stop)

    @property
    def running(self):
        return self.process is not None

    def start(self, argv, input=None):
        if self.process is not None:
            return False
        # Without a parent: a QProcess child that deleteLater() was called on crashes PySide6 when its parent
        # is garbage-collected first. The process is kept in self.process while it runs.
        process = QProcess()
        process.setProgram(sys.executable)
        process.setArguments(["-I", "-c", SETSID, *argv])
        if input is None:
            process.setStandardInputFile(QProcess.nullDevice())
        process.finished.connect(lambda code, status, p=process: self._done(
            p, code if status == QProcess.ExitStatus.NormalExit and not self.cancelled else -1))
        process.errorOccurred.connect(lambda error, p=process: error == QProcess.ProcessError.FailedToStart
                                      and self._done(p, -1, p.errorString()))
        self.process, self.cancelled = process, False
        self.timer.start(int(self.timeout * 1000))
        self.started_at = time.monotonic()
        applog.debug("command", argv=[str(a) for a in argv][-6:], timeout=self.timeout)
        process.start()
        if input is not None:
            process.write(input if isinstance(input, bytes) else str(input).encode())
            process.closeWriteChannel()
        return True

    def _done(self, process, status, error=""):
        try:
            from shiboken6 import isValid
            if not isValid(process):  # destroyed while it still ran (the app quit): nothing left to read
                return
        except ImportError:  # pragma: no cover
            pass
        if process is not self.process:
            if process in self.stopping:  # a stopped command has ended: no SIGKILL for it any more
                self.stopping.discard(process)
                AsyncCommand.stopped.pop(process, None)
                process.deleteLater()
            return
        self.process = None
        self.timer.stop()
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace") + error
        if status == 127 and err.startswith(CANNOT_RUN):
            status = -1
        applog.debug("command done", status=status, seconds=round(time.monotonic() - getattr(self, "started_at", 0), 2),
                     error=err.strip()[-300:] if status else "")
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
        else:  # stopped: deleted once it has ended (deleting a running QProcess waits for it)
            self.stopping.add(process)
            AsyncCommand.stopped[process] = process.processId()
        self.finished.emit(status, out, err)

    def stop(self):
        """End the command and every process it started (its process group): SIGTERM now, SIGKILL after
        KILL_AFTER ms if it still runs. Nothing is waited for (this runs on the GUI thread): the command counts
        as stopped at once (finished(-1, ...)), a new one may start right away."""
        process = self.process
        if process is None:
            return
        self.cancelled = True
        pid = process.processId()
        if pid:
            self._terminate(process, pid)
        else:  # still starting: ended as soon as it runs
            process.started.connect(lambda p=process: self._terminate(p, p.processId()))
        self._done(process, -1, tr("stopped"))

    def _terminate(self, process, pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        QTimer.singleShot(self.KILL_AFTER, self, lambda p=process, n=pid: self._kill(p, n))

    @classmethod
    def end_stopped(cls, grace=1.5):
        """When the app quits (no timer fires any more): one wait of at most GRACE seconds for every stopped
        command still running, then SIGKILL to the groups of those still there."""
        deadline = time.monotonic() + grace
        for process, pid in list(cls.stopped.items()):
            if process.state() != QProcess.ProcessState.NotRunning and \
                    not process.waitForFinished(max(0, int((deadline - time.monotonic()) * 1000))):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _kill(self, process, pid):
        """SIGKILL to the group of a stopped command whose lead has not ended yet (while it has not, the group
        number cannot have been reused)."""
        if process in self.stopping and process.state() != QProcess.ProcessState.NotRunning:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class ThreadCall(QObject):
    """FUNCTION(*args) in a daemon thread; finished(result, error) is emitted on the GUI thread (the thread
    that made the ThreadCall), so whatever is connected to it, a lambda too, may touch widgets.

    An abandoned call (abandon_all() when the window closes, or a background() timeout) emits nothing: the
    check and the emit happen under one lock, so no result reaches a receiver after it was given up."""
    finished = Signal(object, object)
    _result = Signal(object, object)  # from the worker thread to this object's (the GUI) thread: queued
    _alive = set()
    _lock = threading.Lock()

    def __init__(self, function, *args):
        super().__init__()
        self.function, self.args, self.abandoned, self.thread = function, args, False, None
        with ThreadCall._lock:
            ThreadCall._alive.add(self)
        self._result.connect(self._deliver)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        try:
            result, error = self.function(*self.args), None
        except Exception as exc:  # noqa: BLE001 - reported to the page
            result, error = None, exc
        with ThreadCall._lock:
            if self.abandoned:
                ThreadCall._alive.discard(self)
                return
            try:
                self._result.emit(result, error)
            except RuntimeError:  # the Qt object is gone already (the app is closing)
                ThreadCall._alive.discard(self)

    @Slot(object, object)
    def _deliver(self, result, error):
        with ThreadCall._lock:
            ThreadCall._alive.discard(self)
            if self.abandoned:
                return
        self.finished.emit(result, error)

    def abandon(self):
        with ThreadCall._lock:
            self.abandoned = True

    @classmethod
    def running(cls):
        with cls._lock:
            return [call for call in cls._alive if call.thread is not None and call.thread.is_alive()]

    @classmethod
    def wait_all(cls, timeout):
        """Wait up to TIMEOUT seconds for every running call to end; the calls still running."""
        deadline = time.monotonic() + timeout
        for call in cls.running():
            call.thread.join(max(0.0, deadline - time.monotonic()))
        return cls.running()

    @classmethod
    def abandon_all(cls):
        with cls._lock:
            for call in cls._alive:
                call.abandoned = True


def background(function, *args, done, source=None, owner=None, timeout=None):
    """Run FUNCTION(*args) in a thread, never on the GUI thread (which must keep answering approvals);
    DONE(result, error) runs on the GUI thread, once. SOURCE (a widget or a list of them, e.g. the button
    that started it) is disabled meanwhile; OWNER (a page) counts its running calls for busy(). After
    TIMEOUT seconds without a result, DONE gets a TimeoutError and the buttons work again (a late result is
    dropped). Returns the ThreadCall."""
    sources = [w for w in (source if isinstance(source, (list, tuple)) else [source]) if w is not None]
    for widget in sources:
        widget.setEnabled(False)
    if owner is not None:
        owner._background = getattr(owner, "_background", 0) + 1
    call = ThreadCall(function, *args)
    timer = None
    began, name = time.monotonic(), getattr(function, "__qualname__", None) or repr(function)
    applog.debug("background", call=name, timeout=timeout)

    def finish(result, error):
        try:  # a late result (after a timeout or the window's end) never reaches a deleted page or button
            from shiboken6 import isValid
            if owner is not None and not isValid(owner):
                return
        except ImportError:  # pragma: no cover
            pass
        applog.debug("background done", call=name, seconds=round(time.monotonic() - began, 2),
                     error=f"{type(error).__name__}: {error}"[:300] if error is not None else "")
        if call.abandoned and not isinstance(error, TimeoutError):
            return
        if timer is not None:
            timer.stop()
        for widget in sources:
            try:
                widget.setEnabled(True)
            except RuntimeError:  # deleted meanwhile
                pass
        if owner is not None:
            owner._background -= 1
        done(result, error)
    call.finished.connect(finish)
    if timeout:
        timer = QTimer(call)
        timer.setSingleShot(True)

        def expired():
            call.abandon()
            finish(None, TimeoutError(tr("No answer within {seconds} s.", seconds=int(timeout))))
        timer.timeout.connect(expired)
        timer.start(int(timeout * 1000))
    return call.start()


def busy(owner):
    """True while background() calls of OWNER run."""
    return getattr(owner, "_background", 0) > 0


def outcome(result, error):
    """(exit status, stdout, stderr) of a background SandboxAI.call: an exception counts as a failure."""
    return result if error is None else (1, "", str(error))


class Task(QWidget):
    """Inline progress of a page's background job: a busy bar, what runs, Cancel. While it runs, the button
    that started it is disabled; results go to DONE(status, out, err) or DONE(result, error)."""
    changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setMaximumWidth(140)
        self.bar.setMaximumHeight(12)
        self.bar.setTextVisible(False)
        self.label = plain_label(role="secondary")
        self.cancel_button = button(tr("Cancel"), tr("Stop what runs now"), self.cancel)
        layout.addWidget(self.bar)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.cancel_button)
        self.command, self.call, self.source, self.done = None, None, None, None
        self.cancel_event = threading.Event()
        self.hide()

    @property
    def running(self):
        return self.command is not None or self.call is not None

    def _begin(self, text, source, cancellable):
        self.label.setText(text)
        self.cancel_button.setVisible(cancellable)
        self.source = source
        if source is not None:
            source.setEnabled(False)
        self.show()
        self.changed.emit(True)

    def _end(self):
        self.command, self.call = None, None
        if self.source is not None:
            try:
                self.source.setEnabled(True)
            except RuntimeError:
                pass
        self.source = None
        self.hide()
        self.changed.emit(False)

    def run(self, argv, done, text, source=None, timeout=120, input=None):
        if self.running:
            return False
        self.command = AsyncCommand(timeout, self)
        self.done = done
        self.command.finished.connect(self._command_done)
        self._begin(text, source, True)
        self.command.start(argv, input=input)
        return True

    def _command_done(self, status, out, err):
        done, cancelled = self.done, self.command is not None and self.command.cancelled
        self._end()
        if done:
            done(-2 if cancelled else status, out, err)

    def thread(self, function, done, text, source=None, cancellable=False):
        """FUNCTION(cancelled) in a thread; Cancel sets the event it gets (copies stop at the next chunk)."""
        if self.running:
            return False
        self.cancel_event = threading.Event()
        self.call = ThreadCall(function, self.cancel_event.is_set)
        self.done = done
        self.call.finished.connect(self._thread_done)
        self._begin(text, source, cancellable)
        self.call.start()
        return True

    def _thread_done(self, result, error):
        done = self.done
        self._end()
        if done:
            done(result, error)

    def cancel(self):
        if self.command is not None:
            self.command.stop()
        elif self.call is not None:
            self.cancel_event.set()
            self.label.setText(tr("Cancelling…"))


# -- tables --

class SortItem(QTableWidgetItem):
    """A cell that sorts by its SORT_ROLE value (numbers as numbers, times as times)."""

    def __lt__(self, other):
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None:
            try:
                return mine < theirs
            except TypeError:
                pass
        return super().__lt__(other)


def table(headers, multi=False, stretch=None, numeric=(), sortable=True):
    """A read-only table; STRETCH is the column that takes the free width, NUMERIC columns are right-aligned.
    A SORTABLE table keeps the order fill() was given until the user clicks a column header; rows may then
    be in any order, so pages find what a row stands for with row_key(), never from its position."""
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi
                            else QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.verticalHeader().setVisible(False)
    widget.setWordWrap(False)
    widget.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    header = widget.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setMaximumSectionSize(520)
    header.setMinimumSectionSize(48)
    header.setStretchLastSection(stretch is None)
    if stretch is not None:
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    widget.numeric = set(numeric)
    widget.stretch = stretch
    widget.setSortingEnabled(False)
    header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # no column yet: the order given to fill()
    widget.sortable = sortable
    widget.row_keys = []
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().setDefaultSectionSize(28)
    return widget


def fill(widget, rows, colors=None, tooltips=None, icons=None, sort=None, keys=None):
    """Fill WIDGET with ROWS. COLORS per row: None or a theme colour name ("ok", "warn", "bad", "muted");
    ICONS per row: a theme status for a dot in the first column (stays visible when the row is selected);
    SORT per row: values to sort by, column by column (None: the cell). Long texts are cut, the full text is
    in the tooltip.

    KEYS per row: what the row stands for (any hashable value; default: the text of its first column). The
    row's index in ROWS is kept in Qt.UserRole of its first cell and row_key() gives its key, wherever
    sorting moved the row. The selection follows the keys: a row selected before is selected again, and a
    selected row whose key is gone leaves nothing selected (never the row that took its place)."""
    keys = list(keys) if keys is not None else ["" if not row or row[0] is None else str(row[0]) for row in rows]
    if len(keys) != len(rows):
        raise ValueError("fill: one key per row")
    previous = selected_keys(widget)
    widget.clearSelection()
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)
    widget.row_keys = keys
    widget.setRowCount(len(rows))
    for r, row in enumerate(rows):
        _set_row(widget, r, r, row, colors[r] if colors else None, tooltips[r] if tooltips else None,
                 icons[r] if icons else None, sort[r] if sort else None)
    if getattr(widget, "sortable", False) or sorting:
        widget.setSortingEnabled(True)
    if previous:
        select_keys(widget, previous)
    fit_columns(widget)


def _set_row(widget, r, index, row, color=None, tip=None, icon=None, sort=None):
    """The cells of the visual row R from ROW; INDEX: its index in widget.row_keys."""
    numeric = getattr(widget, "numeric", set())
    for c, value in enumerate(row):
        raw = value
        if c in numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
            text = number(value)
        else:
            text = "" if value is None else str(value)
        item = SortItem(elide(text))
        item.setData(SORT_ROLE, raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else
                     (sort[c] if sort and c < len(sort) and sort[c] is not None else text.lower()))
        if c in numeric:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if len(text) > CELL_LIMIT or c == getattr(widget, "stretch", None) or tip:
            item.setToolTip(safe_tooltip(tip if tip and c == 0 else text))
        if color:
            item.setForeground(QColor(theme.color(color)))
        if c == 0 and icon:
            item.setIcon(theme.dot(icon))
        if c == 0:
            item.setData(Qt.ItemDataRole.UserRole, index)
        widget.setItem(r, c, item)


def append_rows(widget, rows, colors=None, keys=(), drop=None):
    """Add ROWS (with their KEYS) to a table fill() filled, at its end, without touching the rows it has; the
    rows whose key DROP(key) says are gone are removed first. The selection stays on its rows. A table
    that has collected many more keys than rows (dropped ones) is better filled again: False then, and
    nothing is done."""
    keys = list(keys)
    if len(keys) != len(rows):
        raise ValueError("append_rows: one key per row")
    known = getattr(widget, "row_keys", None)
    if known is None or len(known) + len(keys) > 2 * (widget.rowCount() + len(keys)) + 1000:
        return False
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)
    if drop is not None:
        gone = [row for row in range(widget.rowCount()) if drop(row_key(widget, row))]
        for row in reversed(gone):
            widget.removeRow(row)
    first = widget.rowCount()
    widget.setRowCount(first + len(rows))
    for offset, row in enumerate(rows):
        _set_row(widget, first + offset, len(known) + offset, row, colors[offset] if colors else None)
    known.extend(keys)
    if getattr(widget, "sortable", False) or sorting:
        widget.setSortingEnabled(True)
    return True


def fit_columns(widget):
    """Columns as wide as their content (within limits); the stretch column takes the rest. Columns the human
    sized (state.State.table) keep their widths."""
    if getattr(widget, "user_sized", False):
        return
    header = widget.horizontalHeader()
    for c in range(widget.columnCount()):
        if c != getattr(widget, "stretch", None):
            widget.resizeColumnToContents(c)
            if header.sectionSize(c) > 360:
                header.resizeSection(c, 360)


def row_texts(widget, row):
    return [widget.item(row, c).text() if widget.item(row, c) else "" for c in range(widget.columnCount())]


def selected(widget, column=0):
    """The text of COLUMN in the selected row (read from the row itself, so sorting does not matter)."""
    rows = widget.selectionModel().selectedRows() if widget.selectionModel() else []
    if not rows:
        return None
    item = widget.item(rows[0].row(), column)
    return item.text() if item else None


def selected_rows(widget):
    """The visual rows selected, top to bottom: use row_key() to know what they stand for."""
    return sorted(index.row() for index in widget.selectionModel().selectedRows()) if widget.selectionModel() else []


def row_data(widget, row, column=0):
    item = widget.item(row, column)
    return item.data(Qt.ItemDataRole.UserRole) if item else None


def row_index(widget, row):
    """The index in the ROWS given to fill() of the visual ROW, or None."""
    index = row_data(widget, row)
    return index if isinstance(index, int) and 0 <= index < len(getattr(widget, "row_keys", [])) else None


def row_key(widget, row):
    """The key fill() was given for the visual ROW (wherever sorting moved it), or None."""
    index = row_index(widget, row)
    return None if index is None else widget.row_keys[index]


def selected_keys(widget):
    """The keys of the selected rows, top to bottom."""
    keys = (row_key(widget, row) for row in selected_rows(widget))
    return [key for key in keys if key is not None]


def find_row(widget, key):
    """The visual row whose key is KEY, or -1."""
    return next((row for row in range(widget.rowCount()) if row_key(widget, row) == key), -1)


def select_keys(widget, keys):
    """Select the rows whose key is in KEYS (only the first of them in a single-selection table)."""
    model, wanted = widget.selectionModel(), list(keys)
    if model is None:
        return
    multi = widget.selectionMode() != QAbstractItemView.SelectionMode.SingleSelection
    flags = QItemSelectionModel.SelectionFlag
    first = True
    for row in range(widget.rowCount()):
        if row_key(widget, row) in wanted:
            model.select(widget.model().index(row, 0),
                         (flags.ClearAndSelect if first else flags.Select) | flags.Rows)
            first = False
            if not multi:
                break


class BarDelegate(QStyledItemDelegate):
    """A bar in a table cell, from the fraction (0..1) in BAR_ROLE: drawn by the view, so it scrolls and
    selects with its row."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        fraction = index.data(BAR_ROLE)
        if not isinstance(fraction, (int, float)):
            return
        rect = option.rect.adjusted(6, option.rect.height() // 2 - 4, -6, -(option.rect.height() // 2 - 4))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(option.palette.color(QPalette.ColorRole.Mid))
        track.setAlpha(70)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, 3, 3)
        filled = QRect(rect)
        filled.setWidth(max(2, int(rect.width() * max(0.0, min(1.0, fraction)))))
        painter.setBrush(option.palette.color(QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(filled, 3, 3)
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(max(hint.width(), 120), hint.height())


def bars(widget, column, values):
    """Bars in COLUMN of WIDGET, relative to the largest value (painted by BarDelegate). VALUES are in the
    order of the rows given to fill(), which sorting may have changed on the screen."""
    if not isinstance(widget.itemDelegateForColumn(column), BarDelegate):
        widget.setItemDelegateForColumn(column, BarDelegate(widget))
    largest = max([v for v in values if isinstance(v, (int, float))] or [1]) or 1
    sorting = widget.isSortingEnabled()
    widget.setSortingEnabled(False)  # no row moves while the bars are set
    for row in range(widget.rowCount()):
        index = row_index(widget, row)
        value = values[index] if index is not None and index < len(values) else 0
        item = widget.item(row, column)
        if item is None:
            item = SortItem("")
            widget.setItem(row, column, item)
        item.setText("")
        item.setData(BAR_ROLE, (value or 0) / largest)
        item.setData(SORT_ROLE, value or 0)
    widget.setSortingEnabled(sorting)


class NavDelegate(QStyledItemDelegate):
    """Navigation entries: icon, text and a count as a pill that stays readable when selected."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        count = index.data(BADGE_ROLE)
        if not count:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = str(count) if count < 100 else "99+"
        font = QFont(option.font)
        font.setBold(True)
        font.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 12
        height = painter.fontMetrics().height() + 2
        rect = QRect(option.rect.right() - width - 8, option.rect.center().y() - height // 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d93025"))
        painter.drawRoundedRect(rect, height / 2, height / 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 40, max(38, hint.height()))


# -- layout --

class FlowLayout(QLayout):
    """Lays out widgets in rows that wrap when the window is narrow (button rows, toolbars)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self.items = []
        self.gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802
        self.items.append(item)

    def addWidget(self, widget):  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def addStretch(self):  # noqa: N802 - rows wrap; a stretch has no meaning here
        pass

    def count(self):
        return len(self.items)

    def itemAt(self, index):  # noqa: N802
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):  # noqa: N802
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self.items:
            if item.widget() is None or not item.widget().isHidden():
                size = size.expandedTo(item.minimumSize())
        return size + QSize(2, 2)

    def _layout(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        for item in self.items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self.gap, 0
            if apply:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + self.gap
            line = max(line, hint.height())
        return y + line - rect.y()


def compact_combo(combo, chars=24):
    """COMBO no wider than about CHARS characters, whatever its longest item (Qt sizes a combo to its longest item
    otherwise, and a long one makes the whole window wider than the screen): the popup still shows every item in
    full, and each item's full text is its tooltip."""
    from PySide6.QtWidgets import QComboBox
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    for index in range(combo.count()):
        if not combo.itemData(index, Qt.ItemDataRole.ToolTipRole):
            combo.setItemData(index, safe_tooltip(combo.itemText(index)), Qt.ItemDataRole.ToolTipRole)
    return combo


def tame_combos(root, chars=28):
    """Every combo box below ROOT whose longest item is longer than CHARS characters: compact_combo."""
    from PySide6.QtWidgets import QComboBox
    for combo in root.findChildren(QComboBox):
        if combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon:
            continue
        longest = max((len(combo.itemText(i)) for i in range(combo.count())), default=0)
        if longest > chars:
            compact_combo(combo, chars)


def name_views(root):
    """Every table, tree or list below ROOT without an accessible name gets the title of the group box it sits in
    (a screen reader says "Plugins, table" instead of only "table"); the pages name their own views, this is the
    fallback for a view added without one."""
    from PySide6.QtWidgets import QAbstractItemView, QComboBox, QGroupBox, QHeaderView
    for view in root.findChildren(QAbstractItemView):
        if view.accessibleName() or isinstance(view, QHeaderView):
            continue
        parent = view.parentWidget()
        while parent is not None and parent is not root and not isinstance(parent, (QGroupBox, QComboBox)):
            parent = parent.parentWidget()
        heading = QGroupBox.title(parent) if isinstance(parent, QGroupBox) else ""  # (a box may shadow title())
        if heading:
            view.setAccessibleName(heading.replace("&", ""))


def flow(*widgets):
    holder = QWidget()
    layout = FlowLayout(holder)
    for widget in widgets:
        if widget is not None:
            layout.addWidget(widget)
    holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return holder


def card():
    frame = QFrame()
    frame.setProperty("role", "card")
    return frame


def standard_icon(name):
    return QApplication.style().standardIcon(getattr(QStyle.StandardPixmap, name))


class Busy:
    """Wait cursor around a short call (no subprocess, no network: those run in the background)."""

    def __enter__(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def __exit__(self, *exc):
        QApplication.restoreOverrideCursor()
