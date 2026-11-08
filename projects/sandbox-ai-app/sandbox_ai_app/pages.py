"""The newer pages of the app: approvals, statistics, files and credentials (images: images_page.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from . import backend, files, theme
from .files import printable
from .state import choose_files, choose_folder
from .i18n import N_, tr
from .widgets import (
    MONO, SORT_ROLE, AsyncCommand, Task, background, bars, confirm, fill, fit_columns, outcome, plain_label,
    row_key, safe_tooltip, selected, selected_rows, table, warn,
)


ANSWER_WAIT = 90  # seconds an answer may take (sandbox-ai approvals: up to 60)
REMOTE_ANSWER_WAIT = 130  # an answer on another host: SSH on top (and "always" waits for the certificate check)
REMOTE_POLL = 5000  # ms between two looks at the other hosts' open questions (one look per host at a time)
LOCAL = "local"  # the host of this machine's own questions
SNAPSHOT_WAIT = 30  # seconds a scan of the questions may take before the next one is tried
POLL_WAIT = 0.2  # seconds a scan on the GUI thread waits for one running in a thread


def kind_name(kind):
    return {"connect": tr("connection"), "dns": tr("name lookup"), "push": tr("push"),
            "browser": tr("open a URL"), "mcp": tr("MCP tool call"), "http": tr("HTTP request"),
            "tool": tr("agent tool"), "device": tr("device"), "canary": tr("canary")}.get(kind, printable(kind))


def shown(value, limit=300):
    """A value the agent chose (a text, or the arguments of a tool call), escaped and shortened."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    value = printable(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def describe(question):
    """What a question asks, in the app's language; everything the agent chose is escaped. A question of another
    host is said as that host's sandbox-ai says it (escaped there and here)."""
    if isinstance(question, backend.RemoteQuestion):
        return printable(question.get("question") or question.get("kind") or "?")
    kind = question.get("kind")
    if kind == "connect":
        text = tr("connect to {destination}", destination=printable(question.get("destination", "?")))
        protocol = question.get("protocol")
        if protocol and protocol != "tcp":
            text += f" ({printable(protocol)[:10]})"
        if question.get("host"):
            text += " " + tr("(named {host} by the agent, not verified)", host=printable(question["host"]))
        return text
    if kind == "dns":
        return tr("look up the name {name}", name=printable(question.get("name", "?")))
    if kind == "push":
        refs = question.get("refs") if isinstance(question.get("refs"), list) else []
        return tr("push {refs} to {url} despite privacy warnings", refs=", ".join(printable(r) for r in refs[:10]),
                  url=printable(question.get("url", "?")))
    if kind == "browser":
        return tr("open {url} in your browser", url=printable(question.get("url", "?")))
    # Rules of the project ask for these: which call it is must be seen before it is allowed.
    rule = tr("rule {rule} of {ruleset}", rule=shown(question.get("rule", "?"), 80),
              ruleset=shown(question.get("ruleset", "?"), 120))
    if kind == "mcp":
        return tr("call the MCP tool {tool} of {server} with {arguments} ({rule})",
                  tool=shown(question.get("tool", "?"), 128), server=shown(question.get("server", "?"), 40),
                  arguments=shown(question.get("arguments", "")), rule=rule)
    if kind == "tool":
        return tr("let {agent} use its tool {tool} with {arguments} ({rule})",
                  agent=shown(question.get("agent", "?"), 20), tool=shown(question.get("tool", "?"), 128),
                  arguments=shown(question.get("arguments", "")), rule=rule)
    if kind == "http":
        return f"{shown(question.get('method', '?'), 20)} {shown(question.get('url', '?'), 500)} ({rule})"
    if kind == "device":
        nodes = question.get("nodes") if isinstance(question.get("nodes"), list) else []
        return tr("attach the device {device} ({rule})", device=", ".join(shown(n, 200) for n in nodes[:10])
                  or shown(question.get("value", "?")), rule=rule)
    if kind == "canary":
        return tr("canary {name}: {summary}", name=shown(question.get("canary", "?"), 40),
                  summary=shown(question.get("summary", ""), 500))
    return printable(kind)


def openable_url(text):
    """QUrl of TEXT if it is an http or https URL with a host (what "Open here" opens), else None."""
    url = QUrl(str(text or ""), QUrl.ParsingMode.StrictMode)
    if url.isValid() and url.scheme().lower() in ("http", "https") and url.host():
        return url
    return None


def scope_items(combo):
    for scope, text in (("once", tr("once")), ("run", tr("for this run")), ("always", tr("always (this project)"))):
        combo.addItem(text, scope)



def asker(name, record):
    """Which sandbox asks: its run NAME, workspace (~ for HOME) and session, from its run.json RECORD."""
    record = record if isinstance(record, dict) else {}
    workspace = str(record.get("workspace") or "")
    home = str(Path.home())
    if workspace == home or workspace.startswith(home + os.sep):
        workspace = "~" + workspace[len(home):]
    details = [printable(workspace)] if workspace else []
    if record.get("session"):
        details.append(tr("session {name}", name=printable(record["session"])))
    return f"{printable(name)} ({', '.join(details)})" if details else printable(name)

def context_text(context):
    """(explanation, log lines) of a question (SandboxAI.question_context): the explanation in plain words,
    the run's log entries apart - quoted, as they are in the log (escaped), never as text of the app."""
    lines = []
    if context.get("kind") == "dns":
        lines.append(tr("The agent wants to look up {name}. Nothing is looked up before you answer: the name itself "
                        "can carry data out. Allowing resolves it and opens only its public addresses.",
                        name=printable(context.get("name", ""))))
    elif context.get("kind") == "connect":
        lines.append(tr("Address {address}, port {port}, protocol {protocol}.", address=printable(context.get("address")),
                        port=printable(context.get("port")), protocol=printable(context.get("protocol") or "tcp")))
        if context.get("host"):
            lines.append(tr("The agent calls it {host}: not verified, the address is what is opened.",
                            host=printable(context["host"])))
    recent = context.get("recent") or []
    if not recent and context.get("log"):
        lines.append(tr("No earlier connections of this run to it in its log."))
    log = ["> " + "  ".join(printable(row[i]) for i in (0, 1, 4) if row[i]) for row in recent]
    return "\n".join(lines), log


class QuestionsWindow(QDialog):
    """The open questions while another dialog is open. A modal dialog (a message, the wizard, Settings, the
    gallery) blocks the main window, and with it the Approvals page, while a question is refused without an
    answer after a while. This window is application-modal itself, so it is above that dialog and gets the
    input; it answers through the Approvals page (the same path, the same checks)."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle(tr("Questions of the running sandboxes"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("A sandbox asks while a dialog is open. Answer here, or close this window "
                                        "and answer on the Approvals page.")))
        self.table = table([tr("Sandbox"), tr("Question")])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.allow_button = QPushButton(tr("Allow once"))
        self.allow_button.clicked.connect(lambda: self.answer("allow"))
        self.deny_button = QPushButton(tr("Deny"))
        self.deny_button.setToolTip(tr("Refused this time"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        for widget in (self.allow_button, self.deny_button):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.resize(620, 280)

    def show_questions(self):
        page = self.page
        keys = list(page.questions)
        fill(self.table, [[page.labels.get(key, printable(key[1])), describe(page.questions[key][1])] for key in keys],
             keys=keys)
        if not selected_rows(self.table) and len(keys) == 1:
            self.table.selectRow(0)
        self.update_buttons()

    def current(self):
        rows = selected_rows(self.table)
        return self.page.questions.get(row_key(self.table, rows[0])) if rows else None

    def update_buttons(self):
        entry = None if self.page.answering is not None else self.current()
        self.allow_button.setEnabled(entry is not None)
        self.deny_button.setEnabled(entry is not None)

    def answer(self, decision):
        entry = self.current()
        if entry is not None:
            self.page.answer(decision, "once", entry=entry, parent=self)


class ApprovalsPage(QWidget):
    """Open questions of every running sandbox, on this machine and on every host of `sandbox-ai hosts`, answered
    through `sandbox-ai approvals` (there: over SSH); the history (of this machine).

    Rows are found by their (host, run, question id) key, never by their position: the table can be sorted, and a
    question that disappears leaves nothing selected, so Allow can only answer the question highlighted. The other
    hosts are read every REMOTE_POLL ms in threads, one look per host at a time, whether the page is shown or not
    (their questions are refused after a minute too)."""
    pending_changed = Signal(int, list)  # number of open questions (all hosts), the new ones [(sandbox, question)]

    def __init__(self, sai, interval=1000, remote_interval=None):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        open_box = QGroupBox(tr("Open questions of the running sandboxes, on every host"))
        open_layout = QVBoxLayout(open_box)
        self.url = plain_label()
        font = QFont()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.url.setFont(font)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.hide()
        url_row = QHBoxLayout()
        url_row.addWidget(self.url, 1)
        # A browser question of another host: the page is opened here, where the human is, and that host is told
        # not to open it (--no-open).
        self.open_here_button = QPushButton(tr("Open here"))
        self.open_here_button.setToolTip(tr("Open the page in this computer's browser and allow the question without "
                                            "opening it on its host"))
        self.open_here_button.clicked.connect(lambda: self.open_here())
        self.open_here_button.hide()
        url_row.addWidget(self.open_here_button)
        open_layout.addLayout(url_row)
        self.open = table([tr("Sandbox"), tr("Kind"), tr("Question"), tr("Asked"), tr("Refused in"), tr("Host")])
        self.open.itemSelectionChanged.connect(self.selection_changed)
        open_layout.addWidget(self.open)
        # What the selected question means (resolved names, the run's earlier connections): in a thread.
        self.context = plain_label(role="secondary")
        self.context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        open_layout.addWidget(self.context)
        # The run's log entries to the same address or name: quoted, monospace, read-only, under a label that
        # says whose words they are - the agent chose these names and URLs.
        self.context_log_label = plain_label(tr("Last connections of this run to it, from its log (text the agent "
                                                "chose, shown as it is):"), role="secondary")
        self.context_log = QPlainTextEdit()
        self.context_log.setReadOnly(True)
        self.context_log.setFont(MONO)
        self.context_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.context_log.setMaximumHeight(110)
        for widget in (self.context_log_label, self.context_log):
            widget.hide()
            open_layout.addWidget(widget)
        row = QHBoxLayout()
        self.scope = QComboBox()
        scope_items(self.scope)
        self.allow_button = QPushButton(tr("Allow"))
        self.allow_button.clicked.connect(lambda: self.answer("allow"))
        self.deny_button = QPushButton(tr("Deny"))
        self.deny_button.setToolTip(tr("Refused; not asked again in this run (once: only this time)"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        self.block_button = QPushButton(tr("Block always"))
        self.block_button.setToolTip(tr("Refused now and at every later start of this project, without a question "
                                        "(Forget… below lifts it)"))
        self.block_button.clicked.connect(lambda: self.answer("deny", "always"))
        # The administrator's max_approval_scope caps the answers (sandbox-ai refuses wider ones anyway).
        self.max_scope = sai.max_scope()
        for index in range(self.scope.count()):
            if backend.SCOPES.index(self.scope.itemData(index)) > backend.SCOPES.index(self.max_scope):
                self.scope.model().item(index).setEnabled(False)
        for widget in (QLabel(tr("Scope")), self.scope, self.allow_button, self.deny_button, self.block_button):
            row.addWidget(widget)
        row.addStretch()
        open_layout.addLayout(row)
        self.message = plain_label()
        open_layout.addWidget(self.message)
        self.remote_note = plain_label(role="secondary")  # hosts that did not answer
        self.remote_note.hide()
        open_layout.addWidget(self.remote_note)
        self.seconds = sai.approval_seconds
        hint = plain_label(tr("Without an answer within {seconds} seconds a question is refused. A deny lasts at "
                              "most for the run; “always” is remembered for the project. Allowing a URL opens it "
                              "in your browser.", seconds=self.seconds))
        hint.setEnabled(False)
        open_layout.addWidget(hint)
        history_box = QGroupBox(tr("History"))
        history_layout = QVBoxLayout(history_box)
        self.history = table([tr("Time"), tr("Sandbox"), tr("Question"), tr("Answer"), tr("Scope"), tr("Reason")])
        history_layout.addWidget(self.history)
        always_box = QGroupBox(tr("Allowed or blocked always (per project)"))
        always_layout = QVBoxLayout(always_box)
        self.always = table([tr("Project"), tr("Allowed"), tr("Kind")])
        always_layout.addWidget(self.always)
        row = QHBoxLayout()
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(lambda: self.load_always())
        self.forget_button = QPushButton(tr("Forget…"))
        self.forget_button.setToolTip(tr("The project's sandboxes ask again next time"))
        self.forget_button.clicked.connect(lambda: self.forget())
        for widget in (refresh, self.forget_button):
            row.addWidget(widget)
        row.addStretch()
        always_layout.addLayout(row)
        self.always_message = plain_label(role="secondary")
        always_layout.addWidget(self.always_message)
        split = self.split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(open_box)
        split.addWidget(history_box)
        split.addWidget(always_box)
        layout.addWidget(split, 1)
        self.entries, self.questions, self.labels, self.seen, self.history_key = [], {}, {}, set(), None
        self.entry_keys, self.local_pending = [], []
        # The other hosts: {host: [RemoteQuestion]} as last read, why a host did not answer, the reads running
        # ({host: ThreadCall}) and the hosts to read once more when their read is done (after an answer).
        self.remote, self.remote_problems, self.remote_reading, self.remote_again = {}, {}, {}, set()
        self.hosts_reading, self.hosts_again, self.known_hosts = None, False, None
        self.project, self.marked, self.project_pending = None, None, 0
        self.open_box = open_box
        self.records, self.contexts, self.always_token, self.always_loaded = {}, {}, 0, False
        self.answering = None  # the ThreadCall that sends an answer
        # Questions asked while a modal dialog blocks this page are offered in a window above it; the questions
        # open when it was closed are not offered again.
        self.questions_window, self.dismissed = None, set()
        # The timer scans in a thread, so the questions are read even while something else keeps the window
        # busy; a newer scan (or a direct poll) makes an older result void.
        self.polling, self.generation = None, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_soon)
        if interval:
            self.timer.start(interval)
        self.remote_timer = QTimer(self)
        self.remote_timer.timeout.connect(lambda: self.poll_remote())
        remote_interval = (REMOTE_POLL if interval else 0) if remote_interval is None else remote_interval
        if remote_interval:
            self.remote_timer.start(remote_interval)
            QTimer.singleShot(0, lambda: self.poll_remote())
        self.poll()

    def poll_soon(self):
        """Scan in a thread (the timer's way); the result is applied on the GUI thread."""
        if self.polling is not None:
            return
        self.generation += 1
        token = self.generation
        self.polling = background(self.sai.approvals_snapshot, None, SNAPSHOT_WAIT, timeout=SNAPSHOT_WAIT + 5,
                                  done=lambda result, error: self.polled(token, result, error))

    def polled(self, token, result, error):
        self.polling = None
        if token == self.generation:
            self.apply(result, error)

    def poll(self, now=None):
        """Scan now (cached: a question file is parsed once; a few small files, no command)."""
        try:
            result, error = self.sai.approvals_snapshot(wait=POLL_WAIT), None
        except TimeoutError:  # a scan in a thread holds the cache (maybe stuck): its result, or the next, comes
            self.poll_soon()
            return
        except Exception as exc:  # noqa: BLE001 - shown, polled again
            result, error = None, exc
        self.generation += 1
        self.apply(result, error, now)

    def apply(self, result, error, now=None):
        if error is not None:
            self.message.setText(tr("Cannot read the questions: {error}", error=printable(error)))
            return
        pending, answered = result
        self.local_pending = pending
        self.show_history(answered)
        self.show_all(now)

    def show_all(self, now=None):
        """This machine's open questions and the other hosts' in one table; new ones are announced once."""
        pending = [(LOCAL, run, record, q) for run, record, q in self.local_pending] + [
            (host, q.get("run", ""), {}, q) for host in sorted(self.remote) for q in self.remote[host]]
        self.show_open(pending, now)
        keys = {(host, run, q.get("id")) for host, run, _, q in pending}
        new = [(self.label_of(host, run, record, q), q) for host, run, record, q in pending
               if (host, run, q.get("id")) not in self.seen]
        self.seen = keys
        self.offer_questions()
        self.pending_changed.emit(len(pending), new)

    def label_of(self, host, run, record, question):
        """Which sandbox asks, and on which host when it is not this machine."""
        if host == LOCAL:
            return asker(run, record)
        return tr("{sandbox} on {host}", sandbox=printable(question.get("sandbox") or run), host=printable(host))

    # -- the other hosts --

    def poll_remote(self, hosts=None):
        """Read the open questions of the other hosts in threads, one read per host at a time: HOSTS (after an answer
        there; read once more when a read of it runs already), or every host of `sandbox-ai hosts`."""
        if hosts is not None:
            for host in hosts:
                if host in self.remote_reading:
                    self.remote_again.add(host)
                else:
                    self.read_host(host)
            return
        if self.hosts_reading is not None:
            self.hosts_again = True
            return
        self.hosts_reading = background(self.sai.remote_hosts, timeout=30, done=self.hosts_listed)

    def hosts_listed(self, hosts, error):
        self.hosts_reading = None
        hosts = list(hosts or []) if error is None else list(self.remote)
        self.known_hosts = set(hosts)
        for gone in (set(self.remote) | set(self.remote_problems)) - set(hosts):
            self.remote.pop(gone, None)
            self.remote_problems.pop(gone, None)
        for host in hosts:
            if host not in self.remote_reading:
                self.read_host(host)
        if self.hosts_again:
            self.hosts_again = False
            self.poll_remote()
        self.show_remote_note()
        self.show_all()

    def read_host(self, host):
        self.remote_reading[host] = background(
            self.sai.host_questions, host, timeout=backend.REMOTE_QUESTION_WAIT + 15,
            done=lambda result, error, h=host: self.host_read(h, result, error))

    def host_read(self, host, result, error):
        self.remote_reading.pop(host, None)
        if self.known_hosts is not None and host not in self.known_hosts:
            self.remote_again.discard(host)
            return  # removed from the list meanwhile: its questions are not answered from here any more
        if error is None:
            self.remote[host] = list(result or [])
            self.remote_problems.pop(host, None)
        else:
            # Its last questions stay while their time runs (a host that answers again soon must not announce them
            # anew); those whose time is up go.
            self.remote_problems[host] = printable(error)[:200]
            kept = [q for q in self.remote.get(host, []) if q.timing()[1] != 0]
            if kept:
                self.remote[host] = kept
            else:
                self.remote.pop(host, None)
        if host in self.remote_again:
            self.remote_again.discard(host)
            self.read_host(host)
        self.show_remote_note()
        self.show_all()

    def show_remote_note(self):
        problems = sorted(self.remote_problems.items())
        self.remote_note.setVisible(bool(problems))
        self.remote_note.setText("\n".join(tr("{host}: no answer", host=printable(host)) + f" ({text})"
                                           for host, text in problems))

    def offer_questions(self):
        """While a modal dialog blocks the window, open questions are shown in a window above it."""
        keys = set(self.questions)
        self.dismissed &= keys
        window = self.questions_window
        if window is not None and window.isVisible():
            if keys:
                window.show_questions()
            else:
                window.hide()
            return
        if not keys - self.dismissed or QApplication.activeModalWidget() is None:
            return
        if window is None:
            window = self.questions_window = QuestionsWindow(self)
            window.finished.connect(lambda _=0: self.dismissed.update(self.questions))
        window.show_questions()
        window.show()
        window.raise_()
        window.activateWindow()

    def set_project(self, path):
        """Questions of the current project's sandboxes are shown in bold, and counted in the box's title."""
        self.project = os.path.realpath(path) if path else None
        self.poll()

    def of_project(self, record):
        workspace = record.get("workspace") if isinstance(record, dict) else None
        return bool(self.project and workspace and os.path.realpath(str(workspace)) == self.project)

    def show_open(self, pending, now=None):
        """PENDING: [(host, run, record, question)], LOCAL questions first."""
        keys = [(host, run, q.get("id")) for host, run, _, q in pending]
        self.project_pending = sum(1 for host, _, record, _ in pending if host == LOCAL and self.of_project(record))
        elsewhere = sum(1 for host, *_ in pending if host != LOCAL)
        title = tr("Open questions of the running sandboxes, on every host")
        if elsewhere:
            title += "  " + tr("({count} on other hosts)", count=elsewhere)
        if self.project_pending:
            title += "  " + tr("({count} of the current project)", count=self.project_pending)
        self.open_box.setTitle(title)
        # The questions themselves are taken every time (a host's newer read counts down from its own time left).
        self.questions = {key: (run, q, host) for key, (host, run, _, q) in zip(keys, pending)}
        self.labels = {key: self.label_of(host, run, record, q) for key, (host, run, record, q) in zip(keys, pending)}
        self.records = {key: record for key, (_, _, record, _) in zip(keys, pending)}
        if keys != self.entry_keys or self.marked != self.project:
            self.marked = self.project
            had_selection = selected_rows(self.open) != []
            self.entry_keys = keys
            self.entries = [(run, q, host) for host, run, _, q in pending]
            self.contexts = {key: text for key, text in self.contexts.items() if key in self.questions}
            # fill() selects the highlighted question again where it now is, or nothing when it is gone.
            fill(self.open, [[asker(run, record) if host == LOCAL else printable(q.get("sandbox") or run),
                              kind_name(q.get("kind")), describe(q), "", "",
                              tr("this machine") if host == LOCAL else printable(host)]
                             for host, run, record, q in pending], keys=keys)
            if not had_selection and self.current() is None and len(self.entries) == 1:
                # The one open question (e.g. after its notification) is ready to answer; a selection that
                # just lost its question never moves to another one.
                self.open.selectRow(0)
            for row in range(self.open.rowCount()):
                key = row_key(self.open, row)
                entry = self.questions.get(key)
                mine = self.of_project(self.records.get(key))
                for column in range(self.open.columnCount()):
                    if mine or (column == 2 and entry and entry[1].get("kind") == "browser"):
                        item = self.open.item(row, column)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        if mine and column == 0:
                            item.setToolTip(safe_tooltip(tr("A sandbox of the current project")))
            fit_columns(self.open)  # bold text is wider
            self.selection_changed()
        # The cells themselves, collected first: setting them may move rows when the table is sorted by them.
        cells = [(self.questions.get(row_key(self.open, row)), self.open.item(row, 3), self.open.item(row, 4))
                 for row in range(self.open.rowCount())]
        for entry, asked, cell in cells:
            if entry is None or asked is None or cell is None:
                continue
            age, left = self.timing(entry, now)
            asked.setText(tr("{seconds} s ago", seconds=age) if age is not None else "?")
            asked.setData(SORT_ROLE, age if age is not None else -1)
            if left is None:
                cell.setText(tr("no time limit"))
                cell.setData(SORT_ROLE, 1 << 30)
                cell.setForeground(self.open.palette().text())
            else:
                cell.setText(tr("{seconds} s", seconds=left) if left else tr("refused"))
                cell.setData(SORT_ROLE, left)
                color = "bad" if left <= 10 else "warn" if left <= 30 else None
                cell.setForeground(QColor(theme.color(color)) if color else self.open.palette().text())
        self.update_buttons()

    def timing(self, entry, now=None):
        """(age, seconds left) of ENTRY (run, question, host); another host's counts down from its own time left."""
        question = entry[1]
        if isinstance(question, backend.RemoteQuestion):
            return question.timing(now)
        return backend.question_timing(question, now, default=self.seconds)

    def show_history(self, answered):
        key = tuple((run, q.get("id"), json.dumps(q.get("result"), sort_keys=True)) for run, _, q in answered)
        if key == self.history_key:
            return
        self.history_key = key
        rows, colors, keys = [], [], []
        for run, record, q in sorted(answered, key=lambda e: str(e[2].get("time", "")), reverse=True):
            result = q.get("result") if isinstance(q.get("result"), dict) else {}
            decision = result.get("decision")
            rows.append([printable(str(q.get("time", "")).replace("T", " ").rstrip("Z")), asker(run, record), describe(q),
                         {"allow": tr("allowed"), "deny": tr("denied")}.get(decision, tr("open")),
                         printable(result.get("scope", "")), printable(result.get("reason", ""))])
            colors.append("ok" if decision == "allow" else "bad" if decision == "deny" else None)
            keys.append((run, q.get("id")))
        fill(self.history, rows, colors, keys=keys)

    def current(self):
        """(run, question, host) of the highlighted row, found by its key; None when nothing is selected."""
        rows = selected_rows(self.open)
        return self.questions.get(row_key(self.open, rows[0])) if rows else None

    def widest(self, entry):
        """The widest answer ENTRY may get: this machine's policy, or what the other host said (its policy, the run's
        security mode, the kind of question)."""
        if entry is not None and entry[2] != LOCAL:
            scope = entry[1].get("max_scope")
            return scope if scope in backend.SCOPES else "once"
        return self.max_scope

    def update_buttons(self):
        entry = None if self.answering is not None else self.current()
        widest = self.widest(entry)
        self.allow_button.setEnabled(entry is not None)
        self.deny_button.setEnabled(entry is not None)
        self.block_button.setEnabled(entry is not None and widest == "always"
                                     and entry[1].get("kind") in ("connect", "dns"))
        self.scope.setEnabled(entry is not None and not backend.question_url(entry[1]))
        # The scopes the selected question may get (another host's policy is that host's).
        for index in range(self.scope.count()):
            self.scope.model().item(index).setEnabled(
                backend.SCOPES.index(self.scope.itemData(index)) <= backend.SCOPES.index(widest))
        if backend.SCOPES.index(self.scope.currentData()) > backend.SCOPES.index(widest):
            self.scope.setCurrentIndex(self.scope.findData("once"))
        remote_url = entry is not None and entry[2] != LOCAL and openable_url(entry[1].get("url")) is not None \
            and entry[1].get("kind") == "browser"
        self.open_here_button.setVisible(remote_url)
        self.open_here_button.setEnabled(remote_url)
        if self.questions_window is not None:
            self.questions_window.update_buttons()

    def selection_changed(self):
        entry = self.current()
        url = backend.question_url(entry[1]) if entry else None
        self.url.setVisible(bool(url))
        if url and entry[2] != LOCAL:
            self.url.setText(tr("{host} asks to open: {url}", host=printable(entry[2]), url=url))
        else:
            self.url.setText(tr("Open in your browser: {url}", url=url) if url else "")
        self.update_buttons()
        self.show_context()

    def show_context(self):
        """Details of the selected question, looked up once per question in a thread (a name lookup may
        take seconds); another host's log is not read from here."""
        entry = self.current()
        if entry and entry[2] != LOCAL:
            self.set_context(tr("Asked on {host}: the answer goes there, over SSH.", host=printable(entry[2])), [])
            return
        if not entry or entry[1].get("kind") not in ("dns", "connect"):
            self.set_context("", [])
            return
        run, question, host = entry
        key = (host, run, question.get("id"))
        if key in self.contexts:
            self.set_context(*self.contexts[key])
            return
        self.contexts[key] = (tr("Reading what the run's log knows about it…"), [])
        self.set_context(*self.contexts[key])
        background(self.sai.question_context, run, self.records.get(key, {}), question, timeout=30,
                   done=lambda result, error: self.context_ready(key, result, error))

    def context_ready(self, key, result, error):
        if key not in self.questions:
            return
        self.contexts[key] = context_text(result) if error is None else \
            (tr("No details: {error}", error=printable(error)), [])
        entry = self.current()
        if entry and (entry[2], entry[0], entry[1].get("id")) == key:
            self.set_context(*self.contexts[key])

    def set_context(self, text, log):
        self.context.setText(text)
        self.context_log.setPlainText("\n".join(log))
        self.context_log_label.setVisible(bool(log))
        self.context_log.setVisible(bool(log))

    # -- allowed always --

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.always_loaded:
            self.load_always()

    def load_always(self, note=None):
        """What was allowed "always", per project (read from sandbox-ai's file in a thread); NOTE is shown
        when it is there (e.g. "Forgotten.")."""
        self.always_loaded = True
        self.always_token += 1
        token = self.always_token
        background(self.sai.remembered, timeout=30,
                   done=lambda rows, error: self.always_ready(token, rows, error, note))

    def always_ready(self, token, rows, error, note=None):
        if token != self.always_token:
            return
        if isinstance(error, AttributeError):
            self.always_message.setText(tr("This sandbox-ai version cannot list them yet: update it."))
            rows = []
        elif error is not None:
            self.always_message.setText(tr("Cannot read them: {error}", error=printable(error)))
            rows = []
        else:
            self.always_message.setText(note or ("" if rows else tr("Nothing is allowed always.")))
        kinds = {"host": tr("allowed: host name"), "tcp": tr("allowed: address and port"),
                 "blocked-host": tr("blocked: host name"), "blocked-tcp": tr("blocked: address and port")}
        fill(self.always, [[printable(project), printable(entry), kinds.get(kind, printable(kind))]
                           for project, entry, kind in rows], [("bad" if kind.startswith("blocked") else None)
                                                             for _, _, kind in rows],
             keys=[(project, entry) for project, entry, _ in rows])

    def forget(self):
        rows = selected_rows(self.always)
        key = row_key(self.always, rows[0]) if rows else None
        if not key:
            self.always_message.setText(tr("Select an entry first."))
            return
        project, entry = key
        if not confirm(self, tr("Forget"), tr("Forget {entry} for {project}? The project's next sandboxes ask again. "
                                              "Sandboxes running now keep what they already allowed until they end "
                                              "(take it back there with Sandboxes › Revoke…, sandbox-ai net revoke).",
                                              entry=printable(entry), project=printable(project)),
                       tr("Forget"), destructive=True):
            return
        background(self.sai.forget_remembered, project, entry, owner=self, source=self.forget_button, timeout=90,
                   done=self.forgotten)

    def forgotten(self, result, error):
        status, out, err = outcome(result, error)
        self.load_always(note=printable((out + err).strip()) if status != 0 else tr("Forgotten."))

    def answer(self, decision, scope=None, entry=None, parent=None, open_here=False):
        """Send the answer in the background (`sandbox-ai approvals` may take up to a minute; on another host through
        `sandbox-ai hosts run HOST --`). Allow asks first, naming the sandbox, its host, the question and the scope;
        Deny takes one click and lasts for the run at most ("once" from the scope box: this time only); Block always
        is a deny with the scope "always". ENTRY: the (run, question, host) to answer (default: the one highlighted);
        PARENT: the window asking; OPEN_HERE (a browser question of another host): allowed there with --no-open, and
        the page opened here once that host took the answer."""
        entry = entry or self.current()
        if not entry or self.answering is not None:
            return
        run, question, host = entry
        if scope is None:
            scope = self.scope.currentData()
            if decision == "deny" and scope != "once":
                scope = "run"
        widest = self.widest(entry)
        if backend.SCOPES.index(scope) > backend.SCOPES.index(widest):
            self.message.setText(tr("The administrator's policy allows answers up to “{scope}”.", scope=widest))
            return
        url = openable_url(question.get("url")) if open_here and question.get("kind") == "browser" else None
        if open_here and url is None:
            self.message.setText(tr("Only http and https pages are opened here."))
            return
        if decision == "allow" and not self.confirm_allow(entry, scope, parent, open_here=url is not None):
            return
        if self.answering is not None:  # answered meanwhile (in the other window, while the question was open)
            return
        self.message.setText(tr("Sending the answer…"))
        ident = question.get("id", "")
        done = lambda result, error: self.answered(result, error, decision, scope, host, url)  # noqa: E731
        if host == LOCAL:
            # sandbox-ai answers within 60 s; a call that hangs past that (e.g. on a stuck pipe) frees the buttons.
            self.answering = background(self.sai.answer, ident, decision, scope, timeout=ANSWER_WAIT, done=done)
        elif not self.sai.question_id.fullmatch(str(ident)):
            self.message.setText(tr("Not a question of sandbox-ai: {ident}", ident=printable(ident)[:60]))
            return
        else:
            argv = ["approvals", decision, ident, "--scope", scope] + (["--no-open"] if url is not None else [])
            self.answering = background(lambda: self.sai.host_run(host, *argv, timeout=REMOTE_ANSWER_WAIT - 30),
                                        timeout=REMOTE_ANSWER_WAIT, done=done)
        self.update_buttons()

    def open_here(self):
        """A browser question of another host: allowed there without opening it (--no-open), opened here."""
        self.answer("allow", "once", open_here=True)

    def confirm_allow(self, entry, scope, parent=None, open_here=False):
        run, question, host = entry
        url = backend.question_url(question)
        lines = [tr("Sandbox: {sandbox}", sandbox=self.labels.get((host, run, question.get("id")), printable(run)))]
        if host != LOCAL:
            lines.append(tr("Host: {host}", host=printable(host)))
        lines.append(tr("Asks to: {question}", question=describe(question)))
        if open_here:
            lines.append(tr("The page opens in this computer's browser, not on {host}.", host=printable(host)))
        if not url:
            lines.append(tr("Scope: {scope}", scope=self.scope.itemText(self.scope.findData(scope))))
        if scope == "always" and not url:
            lines.append(tr("“Always” is remembered for this project: later runs get this without asking."))
        # The safe choice is the default when the answer outlives this run.
        return confirm(parent or self, tr("Allow this request?"), "\n".join(lines), tr("Allow"),
                       destructive=scope == "always" and not url)

    def answered(self, result, error, decision="deny", scope="once", host=LOCAL, url=None):
        self.answering = None
        self.scope.setCurrentIndex(self.scope.findData("once"))  # a wider scope is chosen for one answer only
        status, out, err = outcome(result, error)
        text = printable((out + err).strip())
        if status != 0:
            self.message.setText(tr("Not answered"))
            warn(self, tr("Not answered"), text or tr("sandbox-ai refused the answer."), modal=False)
        else:
            self.message.setText(text)
            if url is not None and decision == "allow":
                QDesktopServices.openUrl(url)
        if host == LOCAL:
            self.poll()
            if status == 0 and scope == "always":
                self.load_always()  # it is listed there now
        else:
            self.poll_remote([host])  # read again now, also when a read of it runs already
            self.update_buttons()


class StatisticsPage(QWidget):
    """What the logs of a project say: model calls and tokens, hosts, credentials, pushes, refusals,
    processes, MCP tool calls."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.directory = QComboBox()
        self.directory.setMinimumWidth(320)
        self.directory.currentIndexChanged.connect(self.load)
        browse = QPushButton(tr("Other folder…"))
        browse.clicked.connect(self.browse)
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(lambda: self.refresh_directories())
        for widget in (QLabel(tr("Logs")), self.directory, browse, refresh):
            top.addWidget(widget)
        top.addStretch()
        layout.addLayout(top)
        self.summary = plain_label()
        layout.addWidget(self.summary)
        grid = QGridLayout()
        holder = QWidget()
        holder.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        self.tables = {}
        sections = (
            ("models", tr("Model calls"), [tr("Model"), tr("Calls"), ""]),
            ("tokens", tr("Tokens"), [tr("Kind"), tr("Tokens"), ""]),
            ("hosts", tr("HTTP requests per host"), [tr("Host"), tr("Requests"), ""]),
            ("credentials", tr("Credentials sent (header names only)"), [tr("Header"), tr("Host"), tr("Times")]),
            ("refused", tr("Refused connections"), [tr("What"), tr("Times")]),
            ("programs", tr("Programs started"), [tr("Program"), tr("Times"), ""]),
            ("tools", tr("MCP tool calls (all servers)"), [tr("Server"), tr("Tool"), tr("Calls"), tr("Refused")]),
        )
        for index, (key, title, headers) in enumerate(sections):
            box = QGroupBox(title)
            box_layout = QVBoxLayout(box)
            widget = table(headers)
            widget.setMinimumHeight(160)
            box_layout.addWidget(widget)
            grid.addWidget(box, index // 2, index % 2)
            self.tables[key] = widget
        self.data, self.generation = None, 0
        self.refresh_directories()

    def refresh_directories(self, select=None):
        current = select or self.directory.currentData()
        self.directory.blockSignals(True)
        self.directory.clear()
        for path in self.sai.log_directories():
            self.directory.addItem(path.name, str(path))
        if current and self.directory.findData(str(current)) < 0:
            self.directory.addItem(Path(current).name, str(current))
        if current:
            self.directory.setCurrentIndex(self.directory.findData(str(current)))
        self.directory.blockSignals(False)
        self.load()

    def browse(self):
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_directories(select=path)

    def set_project(self, path):
        """The logs of the project PATH (its default log folder, as the launcher names it)."""
        try:
            folder = self.sai.launcher.default_log_dir(Path(path))
        except Exception:  # noqa: BLE001 - an older launcher: the choice stays
            return
        self.refresh_directories(select=str(folder))

    def load(self):
        directory = self.directory.currentData()
        if not directory or not os.path.isdir(directory):
            self.generation += 1
            self.data = None
            self.summary.setText(tr("No logs yet: they appear once a sandbox has run with its connection proxy."))
            for widget in self.tables.values():
                fill(widget, [])
            return
        # Reading big logs takes a while: in a thread; only the newest request's result is shown.
        self.generation += 1
        token = self.generation
        self.summary.setText(tr("Reading the logs…"))
        background(self.sai.statistics, directory, owner=self, timeout=300,
                   done=lambda data, error: self.loaded(token, data, error))

    def loaded(self, token, data, error):
        if token != self.generation:
            return
        if error is not None:
            self.summary.setText(tr("Cannot read the logs: {error}", error=printable(error)))
            return
        self.show_data(data)

    def show_data(self, data):
        self.data = data
        pushes = data.get("pushes", {})
        text = tr("{requests} HTTP requests, {processes} processes; pushes: {ok} checked, {refused} refused",
                  requests=data.get("requests", 0), processes=data.get("processes", 0), ok=pushes.get("ok", 0),
                  refused=pushes.get("refused", 0))
        if data.get("older_lines_skipped"):
            text += " " + tr("(only the newest entries: {count} older lines not read)", count=data["older_lines_skipped"])
        self.summary.setText(text)

        def counted(key, pairs):
            rows = [[printable(name), count, ""] for name, count in pairs]
            fill(self.tables[key], rows)
            bars(self.tables[key], 2, [count for _, count in pairs])
        counted("models", sorted(data.get("model_calls", {}).items(), key=lambda p: -p[1]))
        counted("tokens", sorted(data.get("tokens", {}).items()))
        counted("hosts", data.get("hosts", []))
        counted("programs", data.get("programs", []))
        fill(self.tables["credentials"], [[*backend.split_credential(entry), count]
                                          for entry, count in data.get("credentials", [])])
        fill(self.tables["refused"], [[printable(entry), count] for entry, count in data.get("refused", [])],
             ["bad"] * len(data.get("refused", [])))
        tools, colours = [], []
        for server, calls in sorted((data.get("tool_calls") or {}).items()):
            counts, refused = {}, {}
            for call in calls:  # (time, tool) from older sandbox-ai, (time, tool, decision) since the MCP gateway
                tool = str(call[1]) if len(call) > 1 else "?"
                counts[tool] = counts.get(tool, 0) + 1
                if len(call) > 2 and call[2] not in (None, "allow"):
                    refused[tool] = refused.get(tool, 0) + 1
            for tool, count in sorted(counts.items(), key=lambda p: (-p[1], p[0])):
                tools.append([printable(server), printable(tool), count, refused.get(tool, 0)])
                colours.append("bad" if refused.get(tool) else None)
        fill(self.tables["tools"], tools, colours)


ROOT_KINDS = {
    "run": N_("Running sandbox {name}: workspace"), "run-home": N_("Running sandbox {name}: HOME"),
    "session": N_("Session {name}: workspace"), "session-home": N_("Session {name}: HOME"),
    "project": N_("Current project"), "other": N_("Chosen folder"),
}


def root_title(kind, label, path):
    template = ROOT_KINDS.get(kind, "Chosen folder")
    return f"{tr(template, name=printable(label))}  ({printable(path)})"


class FilesPage(QWidget):
    """The workspaces and HOME folders of sandboxes and sessions: browse, copy out, copy in. Symbolic links
    are shown, never followed (see files.py)."""

    def __init__(self, sai, projects=lambda: []):
        super().__init__()
        self.sai, self.projects = sai, projects
        self.root, self.relative, self.entries, self.others = None, "", [], []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.roots = QComboBox()
        self.roots.setMinimumWidth(420)
        self.roots.currentIndexChanged.connect(self.choose_root)
        refresh_roots = QPushButton(tr("Refresh"))
        refresh_roots.clicked.connect(lambda _=False: self.refresh_roots())
        other = QPushButton(tr("Other folder…"))
        other.clicked.connect(self.add_other)
        for widget in (QLabel(tr("Folder")), self.roots, refresh_roots, other):
            top.addWidget(widget)
        top.addStretch()
        layout.addLayout(top)
        where = QHBoxLayout()
        up = QPushButton(tr("Up"))
        up.clicked.connect(self.up)
        self.where = plain_label(word_wrap=False)
        where.addWidget(up)
        where.addWidget(self.where, 1)
        layout.addLayout(where)
        self.table = table([tr("Name"), tr("Type"), tr("Size"), tr("Modified"), tr("Points to (not followed)")],
                           multi=True)
        self.table.cellDoubleClicked.connect(self.open_row)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        download = QPushButton(tr("Copy to the host…"))
        download.clicked.connect(self.download_dialog)
        upload = QPushButton(tr("Copy files here…"))
        upload.clicked.connect(self.upload_dialog)
        upload_dir = QPushButton(tr("Copy a folder here…"))
        upload_dir.clicked.connect(self.upload_folder_dialog)
        self.copy_buttons = (download, upload, upload_dir)
        self.replace = QCheckBox(tr("Replace existing files"))
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(self.list)
        for widget in (download, upload, upload_dir, self.replace):
            buttons.addWidget(widget)
        buttons.addStretch()
        buttons.addWidget(refresh)
        layout.addLayout(buttons)
        # Copies (up to 1 GiB) run in a thread with a Cancel button; one at a time.
        self.task = Task()
        self.task.changed.connect(lambda running: [b.setEnabled(not running) for b in self.copy_buttons])
        layout.addWidget(self.task)
        self.message = plain_label()
        layout.addWidget(self.message)
        self.last_report = []
        note = plain_label(tr("Symbolic links are shown but never followed or copied; copies stop at {limit} MiB.",
                              limit=files.LIMIT >> 20))
        note.setEnabled(False)
        layout.addWidget(note)
        self.refresh_roots()

    def set_project(self, path):
        """Show the current project's folder."""
        self.refresh_roots(select=os.path.realpath(path))

    def refresh_roots(self, select=None):
        keep = select or self.roots.currentData()
        self.roots.blockSignals(True)
        self.roots.clear()
        for kind, label, path in self.sai.file_roots(extra=list(self.projects()) + self.others):
            self.roots.addItem(root_title(kind, label, path), path)
        index = self.roots.findData(keep) if keep else -1
        self.roots.setCurrentIndex(index if index >= 0 else 0)
        self.roots.blockSignals(False)
        self.choose_root()

    def add_other(self):
        path = choose_folder(self, tr("Folder to browse"), "browse")
        if path:
            self.others.append(("other", "", path))
            self.refresh_roots()
            self.roots.setCurrentIndex(self.roots.findData(os.path.realpath(path)))

    def choose_root(self):
        path = self.roots.currentData()
        self.root, self.relative = None, ""
        self.table.clearSelection()  # names selected in another folder mean other files here
        if path:
            try:
                self.root = files.Root(path)
            except OSError as exc:
                self.message.setText(tr("Cannot open {path}: {error}", path=printable(path), error=printable(exc)))
        self.list()

    def go(self, relative):
        relative = "/".join(files.parts(relative))
        if relative != self.relative:
            self.table.clearSelection()  # src selected in the parent is not src/src here
        self.relative = relative
        self.list()

    def up(self):
        self.go("/".join(files.parts(self.relative)[:-1]))

    def list(self):
        self.entries = []
        if self.root is None:
            self.where.setText("")
            fill(self.table, [])
            return
        self.where.setText(printable(self.root.path + ("/" + self.relative if self.relative else "")))
        try:
            self.entries = self.root.list(self.relative)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            self.relative = ""
            self.table.clearSelection()
            try:
                self.entries = self.root.list("")
            except (OSError, ValueError):
                self.entries = []
        kinds = {"dir": tr("folder"), "file": tr("file"), "link": tr("symbolic link"), "other": tr("special file")}
        rows, colors = [], []
        for entry in self.entries:
            rows.append([printable(entry.name), kinds[entry.kind], files.size_text(entry.size) if entry.kind == "file" else "",
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime)),
                         "→ " + printable(entry.target) if entry.kind == "link" else ""])
            colors.append("warn" if entry.kind == "link" else "muted" if entry.kind == "other" else None)
        fill(self.table, rows, colors, keys=[entry.name for entry in self.entries])

    def path_of(self, entry):
        return f"{self.relative}/{entry.name}" if self.relative else entry.name

    def entry_at(self, row):
        """The entry shown in the visual ROW (found by its name: the table may be sorted), or None."""
        name = row_key(self.table, row)
        return next((entry for entry in self.entries if entry.name == name), None)

    def open_row(self, row, _column=0):
        entry = self.entry_at(row)
        if entry is None:
            return
        if entry.kind == "dir":
            self.go(self.path_of(entry))
        elif entry.kind == "link":
            self.message.setText(tr("{name} is a symbolic link to {target}; it is not followed.",
                                    name=printable(entry.name), target=printable(entry.target)))

    def chosen(self):
        return [entry for entry in (self.entry_at(row) for row in selected_rows(self.table)) if entry is not None]

    def report(self, done, problems, copied_out=False):
        parts = []
        if done:
            parts.append(tr("{files} file(s) in {folders} folder(s), {size} copied",
                            files=sum(r.files for r in done), folders=sum(r.directories for r in done),
                            size=files.size_text(sum(r.bytes for r in done))))
        skipped = [p for r in done for p in r.skipped]
        if skipped:
            parts.append(tr("skipped: {list}", list=", ".join(f"{printable(p)} ({printable(why)})"
                                                               for p, why in skipped[:10])))
        parts += [printable(problem) for problem in problems]
        risky = [path for r in done for path in r.risky] if copied_out else []
        executable = [path for r in done for path in r.executable] if copied_out else []
        if executable:
            parts.append(tr("Copied without the executable bit (make a file executable yourself once you trust it): "
                            "{list}", list=", ".join(printable(path) for path in executable[:20])
                            + (" …" if len(executable) > 20 else "")))
        if risky:
            listed = ", ".join(printable(path) for path in risky[:20]) + (" …" if len(risky) > 20 else "")
            warning = tr("Careful: the agent could have written these files, and tools run them or read them as "
                         "configuration when you use the copy on the host (git, direnv, shells, editors, make): "
                         "{list}. Look at them before you use the copied folder. Copied files are never "
                         "executable on the host.", list=listed)
            parts.append(warning)
            warn(self, tr("Files that run or configure tools"), warning)
        self.message.setText("\n".join(parts))
        self.last_report = parts
        return parts

    def download(self, entries, destination):
        """Copy ENTRIES (of the shown folder) into the host folder DESTINATION, in a thread; the report
        follows when it is done (False: another copy still runs)."""
        root, items = self.root, [(entry.name, self.path_of(entry)) for entry in entries]

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    done.append(root.copy_out(path, destination, cancelled=cancelled))
                except (OSError, ValueError) as exc:
                    problems.append(f"{name}: {exc}")
            return done, problems
        return self.task.thread(work, lambda result, error: self.copied(result, error, True),
                                tr("Copying to the host…"), cancellable=True)

    def upload(self, paths):
        """Copy the host files or folders PATHS into the shown folder, in a thread (see download)."""
        root, relative, overwrite = self.root, self.relative, self.replace.isChecked()

        def work(cancelled):
            done, problems = [], []
            for path in paths:
                try:
                    done.append(root.copy_in(path, relative, overwrite=overwrite, cancelled=cancelled))
                except (OSError, ValueError) as exc:
                    problems.append(f"{Path(path).name}: {exc}")
            return done, problems
        return self.task.thread(work, lambda result, error: self.copied(result, error, False),
                                tr("Copying into the sandbox's folder…"), cancellable=True)

    def copied(self, result, error, out):
        done, problems = result if error is None else ([], [str(error)])
        if not out:
            self.list()
        self.report(done, problems, copied_out=out)

    def download_dialog(self):
        entries = self.chosen()
        if not entries or self.root is None:
            QMessageBox.information(self, tr("Files"), tr("Select files or folders first."))
            return
        destination = choose_folder(self, tr("Copy to which folder on the host?"), "copy-out")
        if destination:
            self.download(entries, destination)

    def upload_dialog(self):
        if self.root is None:
            return
        paths = choose_files(self, tr("Files to copy into {path}", path=self.where.text()), "copy-in")
        if paths:
            self.upload(paths)

    def upload_folder_dialog(self):
        if self.root is None:
            return
        path = choose_folder(self, tr("Folder to copy into {path}", path=self.where.text()), "copy-in")
        if path:
            self.upload([path])


from .credentials_page import CredentialsPage  # noqa: E402,F401 - the Credentials page lives in its own module
from .images_page import ImagesPage  # noqa: E402,F401 - the Images page lives in its own module
