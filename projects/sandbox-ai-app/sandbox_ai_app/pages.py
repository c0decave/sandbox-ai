"""The newer pages of the app: approvals, statistics and credentials (images: images_page.py, files: files_page.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from . import backend, theme
from .files import printable
from .state import choose_folder
from .i18n import tr
from .widgets import (
    MONO, SORT_ROLE, AsyncCommand, ask_first, background, bars, compact_combo, confirm, fill, find_row, fit_columns,
    flow, outcome,
    plain_label, row_key, safe_tooltip, selected, selected_rows, set_enabled, table, warn, shown_as_text,
)


ANSWER_WAIT = 90  # seconds an answer may take (sandbox-ai approvals: up to 60)
REMOTE_ANSWER_WAIT = 130  # an answer on another host: SSH on top (and "always" waits for the certificate check)
REMOTE_POLL = 5000  # ms between two looks at the other hosts' open questions (one look per host at a time)
REMOTE_BACKOFF = 30  # s at most between two looks at a host that does not answer (REMOTE_POLL, doubled each time)
REMOTE_GRACE = 60  # seconds the questions of a host that stopped answering stay listed after its last answer
WAKE_SECONDS = 120  # seconds a host is asked for questions after the app started something (it may not be listed yet)
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


# The kinds sandbox-ai approves at most for the run (approvals.widest_answer): a push, a request, a tool call, a device.
RUN_AT_MOST = ("push", "http", "mcp", "tool", "device")
# Keys of the Approvals page and of its Questions window: (key, modifiers) -> action. None of them is a menu
# mnemonic in English or German (Alt+letters: &Datei, &Ansicht) or a key of the window's menus (Ctrl+Shift+B: Only
# icons); they are taken while the focus is in the open questions, before any shortcut of the window.
SHORTCUTS = {(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier): "allow",
             (Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier): "allow",
             (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "deny",
             (Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "block"}
MODIFIERS = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier
             | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.KeypadModifier)


def shortcut_of(event):
    """The action of the Approvals page a key EVENT stands for, or None."""
    try:
        modifiers = event.modifiers() & MODIFIERS
        return next((action for (key, wanted), action in SHORTCUTS.items()
                     if event.key() == key and modifiers == wanted), None)
    except (AttributeError, TypeError):
        return None


def local_widest(policy, record, question):
    """The widest answer an allow of a question of this machine keeps, as sandbox-ai's approvals.widest_answer says
    it: the administrator's POLICY, the run's security mode (its run.json RECORD) and the kind's own cap. None: no
    allow at all - a name stays pinned for the run once allowed, so where only "once" is allowed sandbox-ai refuses
    to allow it (approvals.DNS_ONCE)."""
    caps = [policy if policy in backend.SCOPES else "always"]
    mode = record.get("mode") if isinstance(record, dict) and isinstance(record.get("mode"), dict) else {}
    if mode.get("approval_scope") in backend.SCOPES:
        caps.append(mode["approval_scope"])
    if isinstance(question, dict) and question.get("kind") in RUN_AT_MOST:
        caps.append("run")
    widest = min(caps, key=backend.SCOPES.index)
    if isinstance(question, dict) and question.get("kind") == "dns" and widest == "once":
        return None
    return widest


def remote_browser(entry):
    """True for a question of another host to open a page: the page is opened here, where the human is."""
    return entry is not None and entry[2] != LOCAL and entry[1].get("kind") == "browser"



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
    """The open questions while another dialog is open. A modal dialog (the wizard, Settings, the gallery) blocks the
    main window, and with it the Approvals page, while a question is refused without an answer after a while. This
    window is application-modal itself, so it is above that dialog and gets the input; it answers through the
    Approvals page (the same path, the same checks, the same keys: Ctrl+Return, Ctrl+Shift+D, Ctrl+Shift+K)."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle(tr("Questions of the running sandboxes"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("A sandbox asks while a dialog is open. Answer here, or close this window "
                                        "and answer on the Approvals page.")))
        self.table = table([tr("Sandbox"), tr("Question")], stretch=1)
        from .tables import keep
        keep(self.table, "dialog-questions")
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.table.setAccessibleName(tr("Open questions"))
        self.allow_button = QPushButton(tr("Allow once"))
        self.allow_button.setAccessibleName(tr("Allow once"))
        self.allow_button.setToolTip(tr("Allowed this time only (Ctrl+Return)"))
        self.allow_button.clicked.connect(lambda: self.answer("allow"))
        self.deny_button = QPushButton(tr("Deny for this run"))
        self.deny_button.setAccessibleName(tr("Deny for this run"))
        self.deny_button.setToolTip(tr("Refused; not asked again in this run (Ctrl+Shift+D)"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        self.block_button = QPushButton(tr("Block always…"))
        self.block_button.setAccessibleName(tr("Block always"))
        self.block_button.setProperty("role", "danger")
        self.block_button.setToolTip(tr("Refused now and at every later start of this project, without a question "
                                        "(Ctrl+Shift+K, asked first)"))
        self.block_button.clicked.connect(lambda: self.answer("block"))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        for widget in (self.allow_button, self.deny_button, self.block_button):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        for first, second in ((self.table, self.allow_button), (self.allow_button, self.deny_button),
                              (self.deny_button, self.block_button), (self.block_button, close)):
            QWidget.setTabOrder(first, second)
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
        entry = self.current()
        rules = self.page.answer_rules(entry)
        self.allow_button.setText(tr("Open here and allow") if remote_browser(entry) else tr("Allow once"))
        set_enabled(self.allow_button, rules["allow"] is None, rules["allow"])
        set_enabled(self.deny_button, rules["deny"] is None, rules["deny"])
        set_enabled(self.block_button, rules["block"] is None, rules["block"])

    def shortcut(self, action):
        button = {"allow": self.allow_button, "deny": self.deny_button, "block": self.block_button}[action]
        if button.isEnabled():
            button.click()

    def answer(self, decision):
        entry = self.current()
        if entry is None:
            return
        if decision == "block":
            self.page.answer("deny", "always", entry=entry, parent=self)
            return
        self.page.answer(decision, "once" if decision == "allow" else "run", entry=entry, parent=self,
                         open_here=decision == "allow" and remote_browser(entry))


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
        open_layout.addWidget(self.url)
        # Whose questions are listed: every host's, this machine's or one host's (Hosts › Show questions).
        filter_row = QHBoxLayout()
        self.host_filter = QComboBox()
        self.host_filter.setAccessibleName(tr("Questions of"))
        self.host_filter.setToolTip(tr("Whose questions are listed: every host's, this machine's or one host's"))
        self.host_filter.addItem(tr("All hosts"), None)
        self.host_filter.addItem(tr("This machine"), LOCAL)
        self.host_filter.currentIndexChanged.connect(lambda _=0: self.show_all())
        questions_of = QLabel(tr("Questions of"))
        questions_of.setBuddy(self.host_filter)
        filter_row.addWidget(questions_of)
        filter_row.addWidget(self.host_filter)
        filter_row.addStretch()
        open_layout.addLayout(filter_row)
        self.open = table([tr("Sandbox"), tr("Kind"), tr("Question"), tr("Asked"), tr("Refused in"), tr("Host")],
                          stretch=2, compact=(1, 3, 4, 5))
        self.open.setAccessibleName(tr("Open questions"))
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
        self.scope.setAccessibleName(tr("Scope of an allow"))
        self.scope.setToolTip(tr("How long an allow lasts: once, for this run, or always for the project"))
        scope_items(self.scope)
        # Allow takes the scope of the box; Deny is for the run (as the web interface's and the command line's),
        # Deny once only this time; Block always is a deny remembered for the project, asked first.
        self.allow_button = QPushButton(tr("Allow"))
        self.allow_button.setAccessibleName(tr("Allow"))
        self.allow_button.setToolTip(tr("Allow with the scope chosen (Ctrl+Return)"))
        self.allow_button.clicked.connect(lambda: self.answer("allow"))
        self.deny_button = QPushButton(tr("Deny for this run"))
        self.deny_button.setAccessibleName(tr("Deny for this run"))
        self.deny_button.setToolTip(tr("Refused; not asked again in this run (Ctrl+Shift+D)"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        self.deny_once_button = QPushButton(tr("Deny once"))
        self.deny_once_button.setAccessibleName(tr("Deny once"))
        self.deny_once_button.setToolTip(tr("Refused this time only: the sandbox may ask again"))
        self.deny_once_button.clicked.connect(lambda: self.answer("deny", "once"))
        self.block_button = QPushButton(tr("Block always…"))
        self.block_button.setAccessibleName(tr("Block always"))
        self.block_button.setProperty("role", "danger")
        self.block_button.setToolTip(tr("Refused now and at every later start of this project, without a question "
                                        "(Forget… below lifts it; Ctrl+Shift+K)"))
        self.block_button.clicked.connect(lambda: self.answer("deny", "always"))
        # The administrator's max_approval_scope caps the allows (sandbox-ai refuses wider ones anyway); read again
        # at every scan, so a policy changed meanwhile counts.
        self.max_scope = sai.max_scope()
        scope_label = QLabel(tr("Scope"))
        scope_label.setBuddy(self.scope)
        # A row that wraps when the window is narrow (in German it made the window 1100 px wide).
        open_layout.addWidget(flow(scope_label, self.scope, self.allow_button, self.deny_button, self.deny_once_button,
                                   self.block_button))
        # Tab goes from whose questions to the questions, the scope and the answers, in the order they are read.
        order = (self.host_filter, self.open, self.scope, self.allow_button, self.deny_button, self.deny_once_button,
                 self.block_button)
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)
        self.message = plain_label()
        open_layout.addWidget(self.message)
        self.remote_note = plain_label(role="secondary")  # hosts that did not answer
        self.remote_note.hide()
        open_layout.addWidget(self.remote_note)
        self.seconds = sai.approval_seconds
        hint = plain_label(tr("Without an answer within {seconds} seconds a question is refused; a page to open "
                              "waits until it is answered. A deny lasts at most for the run; “always” is remembered "
                              "for the project. Allowing a URL opens it in your browser.", seconds=self.seconds))
        hint.setEnabled(False)
        open_layout.addWidget(hint)
        history_box = QGroupBox(tr("History"))
        history_layout = QVBoxLayout(history_box)
        # Whose history: this machine's (read at every scan) or a host's (`sandbox-ai approvals history` there, as
        # its table says it, when chosen or refreshed).
        history_row = QHBoxLayout()
        self.history_host = QComboBox()
        self.history_host.setAccessibleName(tr("History of"))
        self.history_host.setToolTip(tr("Whose answers: this machine's or a host's"))
        self.history_host.addItem(tr("This machine"), LOCAL)
        self.history_host.activated.connect(lambda _=0: self.load_history())
        history_refresh = QPushButton(tr("Refresh"))
        history_refresh.setAccessibleName(tr("Refresh the history"))
        history_refresh.clicked.connect(lambda: self.load_history())
        history_of = QLabel(tr("History of"))
        history_of.setBuddy(self.history_host)
        for widget in (history_of, self.history_host, history_refresh):
            history_row.addWidget(widget)
        history_row.addStretch()
        history_layout.addLayout(history_row)
        # A ModelTable: a long-running sandbox's history (hundreds of answers) is filled in one step.
        self.history = table([tr("Time"), tr("Sandbox"), tr("Question"), tr("Answer"), tr("Scope"), tr("Reason")],
                             stretch=2, compact=(0, 3, 4), many=True)
        self.history.setAccessibleName(tr("History of the answers"))
        history_layout.addWidget(self.history)
        self.history_text = QPlainTextEdit()  # a host's history: its own table, escaped
        self.history_text.setReadOnly(True)
        self.history_text.setFont(MONO)
        self.history_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.history_text.setAccessibleName(tr("History of the answers on the host chosen"))
        self.history_text.hide()
        history_layout.addWidget(self.history_text)
        self.history_token = 0
        always_box = QGroupBox(tr("Allowed or blocked always (per project)"))
        always_layout = QVBoxLayout(always_box)
        self.always = table([tr("Project"), tr("Allowed"), tr("Kind")], stretch=0, compact=(2,))
        self.always.setAccessibleName(tr("Allowed or blocked always"))
        always_layout.addWidget(self.always)
        row = QHBoxLayout()
        # Whose list: this machine's (sandbox-ai's file) or a host's (`sandbox-ai approvals remembered` there).
        self.always_host = QComboBox()
        self.always_host.setAccessibleName(tr("Remembered on"))
        self.always_host.setToolTip(tr("Whose list: this machine's or a host's"))
        self.always_host.addItem(tr("This machine"), LOCAL)
        self.always_host.activated.connect(lambda _=0: self.load_always())
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh the remembered answers"))
        refresh.clicked.connect(lambda: self.load_always())
        self.forget_button = QPushButton(tr("Forget…"))
        self.forget_button.setAccessibleName(tr("Forget"))
        self.forget_button.setToolTip(tr("The project's sandboxes ask again next time"))
        self.forget_button.clicked.connect(lambda: self.forget())
        for widget in (self.always_host, refresh, self.forget_button):
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
        self.remote_failures, self.remote_next = {}, {}  # hosts that do not answer: how often, when to ask again
        # When each host answered last (time.monotonic() when that read began), and the questions answered there
        # ({(host, run, id): time.monotonic() of the answer}) that a read begun after the answer still listed.
        self.remote_seen, self.answered_there = {}, {}
        self.hosts_reading, self.hosts_again, self.known_hosts = None, False, None
        # Questions only come from sandboxes that run: a host whose starts were read (the Sandboxes page, set_fleet)
        # and showed none running is not asked for questions at all (quiet), until a read of its starts shows one or
        # the app starts something there (woken: {host or None for every host: time.monotonic() until then}). This
        # machine the same way: while its registry lists no running sandbox, no question folder is read (scan).
        self.quiet_hosts, self.woken = set(), {}
        # The last scan of this machine applied ((open, answered, widest)): the same again, while no question counts
        # down anywhere, changes nothing on the page - nothing is done for it.
        self.local_read = None
        self.project, self.marked, self.project_pending = None, None, 0
        self.open_box = open_box
        self.records, self.contexts, self.always_token, self.always_loaded = {}, {}, 0, False
        # The answers on their way, per question ({(host, run, id): ThreadCall}): a slow host's answer (over SSH, up
        # to REMOTE_ANSWER_WAIT) never holds up the others, whose time runs.
        self.answers = {}
        # Questions asked while a modal dialog blocks this page are offered in a window above it; the questions
        # open when it was closed are not offered again.
        self.questions_window, self.dismissed = None, set()
        # The page's own questions before an answer (Allow…, Block always…) are open: no Questions window above them,
        # where a second answer could be started on top of the first.
        self.confirming = 0
        # The keys of the answers (eventFilter): the application's filter is on while the open questions are shown.
        self.keys_on = False
        open_box.installEventFilter(self)
        # The timer scans in a thread, so the questions are read even while something else keeps the window
        # busy; a newer scan (or a direct poll) makes an older result void.
        self.polling, self.generation = None, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_soon)
        if interval:
            self.timer.start(interval)
        self.remote_timer = QTimer(self)
        self.remote_timer.timeout.connect(lambda: self.poll_remote(polled=True))
        remote_interval = (REMOTE_POLL if interval else 0) if remote_interval is None else remote_interval
        if remote_interval:
            self.remote_timer.start(remote_interval)
            QTimer.singleShot(0, lambda: self.poll_remote())
        # The app's page: its first scan in a thread too, after the window's first paint (MainWindow.eventFilter) or
        # at the timer's first tick (in a thread meanwhile it would slow the window's making and first paint down);
        # a page without its timer (the tests): scanned now.
        self.first_scan_due = bool(interval)
        if not interval:
            self.poll()

    def first_scan(self):
        """The window has painted itself: the first scan (in a thread), unless the timer did it already."""
        if self.first_scan_due:
            self.poll_soon()

    def poll_soon(self):
        """Scan in a thread (the timer's way); the result is applied on the GUI thread."""
        self.first_scan_due = False
        if self.polling is not None:
            return
        self.generation += 1
        token = self.generation
        self.polling = background(self.scan, SNAPSHOT_WAIT, timeout=SNAPSHOT_WAIT + 5,
                                  done=lambda result, error: self.polled(token, result, error))

    def scan(self, wait):
        """(open, answered) of this machine's questions and the policy's widest scope, read together. While no sandbox
        runs here (the registry, files and /proc only), no question folder and no policy is read: there can be no
        question (the widest scope stays as last read; it is read again with the first question)."""
        runs = self.sai.live_runs()
        if not runs:
            return self.sai.approvals_snapshot(runs={}, wait=wait), getattr(self, "max_scope", "once")
        snapshot = self.sai.approvals_snapshot(runs=runs, wait=wait)
        try:
            widest = self.sai.max_scope()
        except Exception:  # noqa: BLE001 - a policy that cannot be read: the narrowest, sandbox-ai decides anyway
            widest = "once"
        return snapshot, widest

    def polled(self, token, result, error):
        self.polling = None
        if token == self.generation:
            if error is None:
                result, self.max_scope = result
            self.apply(result, error)

    def poll(self, now=None):
        """Scan now (cached: a question file is parsed once; a few small files, no command)."""
        try:
            (result, self.max_scope), error = self.scan(POLL_WAIT), None
        except TimeoutError:  # a scan in a thread holds the cache (maybe stuck): its result, or the next, comes
            self.poll_soon()
            return
        except Exception as exc:  # noqa: BLE001 - shown, polled again
            result, error = None, exc
        self.generation += 1
        self.apply(result, error, now)

    def apply(self, result, error, now=None):
        if error is not None:
            self.local_read = None
            self.message.setText(tr("Cannot read the questions: {error}", error=printable(error)))
            return
        pending, answered = result
        read, previous = (pending, answered, self.max_scope), self.local_read
        self.local_read = read
        if read == previous and not self.questions:
            return  # nothing new, and no question anywhere whose time runs: the page stays as it is
        self.local_pending = pending
        if previous is None or answered != previous[1]:
            self.show_history(answered)
        self.show_all(now)

    def show_all(self, now=None):
        """This machine's open questions and the other hosts' in one table; new ones are announced once."""
        pending = [(LOCAL, run, record, q) for run, record, q in self.local_pending] + [
            (host, q.get("run", ""), {}, q) for host in sorted(self.remote) for q in self.remote[host]]
        self.show_open(pending, now)
        keys = {(host, run, q.get("id")) for host, run, _, q in pending}
        new = [(self.label_of(host, run, record, q), q, (host, run, q.get("id"))) for host, run, record, q in pending
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

    def poll_remote(self, hosts=None, polled=False):
        """Read the open questions of the other hosts in threads, one read per host at a time: HOSTS (after an answer
        there; read once more when a read of it runs already), or every host of `sandbox-ai hosts`."""
        if hosts is not None:
            for host in hosts:
                if host in self.remote_reading:
                    self.remote_again.add(host)
                else:
                    self.read_host(host)
            return
        # A host whose read hangs (it gives up only after its timeout): its questions go after REMOTE_GRACE all the same.
        if [host for host in list(self.remote_problems) if host in self.remote and self.expire_remote(host)]:
            self.show_all()
        if self.hosts_reading is not None:
            self.hosts_again = True
            return
        self.hosts_polled = polled  # (the timer: hosts that did not answer wait; a refresh asks every host)
        self.hosts_reading = background(self.sai.remote_hosts, timeout=30, done=self.hosts_listed)

    def forget_host(self, name):
        """Host NAME was removed from the list (the Hosts page): its questions go at once, not at the next read."""
        if self.known_hosts is not None:
            self.known_hosts.discard(name)
        self.drop_host(name)
        self.remote_again.discard(name)
        self.fill_hosts(sorted(self.known_hosts or ()))
        self.show_remote_note()
        self.show_all()

    def drop_host(self, name):
        for store in (self.remote, self.remote_problems, self.remote_seen, self.remote_failures, self.remote_next):
            store.pop(name, None)
        for key in [key for key in self.answered_there if key[0] == name]:
            del self.answered_there[key]

    def fill_hosts(self, hosts):
        """The host choices of the question filter and of the remembered list: the choice stays while its host does
        (filled again only when the hosts changed: an open list must not close under the mouse every few seconds)."""
        if list(hosts) == getattr(self, "filled_hosts", None):
            return
        self.filled_hosts = list(hosts)
        remembered_on, history_of = self.always_host.currentData(), self.history_host.currentData()
        for combo, extra in ((self.host_filter, [(tr("All hosts"), None)]), (self.always_host, []),
                             (self.history_host, [])):
            chosen = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for text, data in extra + [(tr("This machine"), LOCAL)] + [(printable(host), host) for host in hosts]:
                combo.addItem(text, data)
            index = combo.findData(chosen)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        if self.always_host.currentData() != remembered_on and self.always_loaded:
            self.load_always()  # its host is gone: this machine's list, not that host's rows under another name
        if self.history_host.currentData() != history_of:
            self.load_history()

    def show_host(self, host):
        """List only HOST's questions (None: every host's), e.g. for Hosts › Show questions."""
        index = self.host_filter.findData(host)
        if index < 0 and host is not None:  # not read yet (the page was not polled since the host came)
            self.fill_hosts(sorted({*(self.known_hosts or ()), host}))
            index = self.host_filter.findData(host)
        self.host_filter.setCurrentIndex(max(0, index))
        self.show_all()

    def select_question(self, key):
        """Select the question KEY (host, run, id), e.g. from its notification; False when it is gone."""
        if key not in self.questions:
            return False
        if self.host_filter.currentData() not in (None, key[0]):
            self.host_filter.setCurrentIndex(0)  # every host's: the question must be seen
        self.show_all()
        row = find_row(self.open, key)
        if row < 0:
            return False
        self.open.clearSelection()
        self.open.selectRow(row)
        self.open.setFocus()
        return True

    def hosts_listed(self, hosts, error):
        self.hosts_reading = None
        hosts = list(hosts or []) if error is None else list(self.remote)
        self.known_hosts = set(hosts)
        self.fill_hosts(sorted(hosts))
        for gone in (set(self.remote) | set(self.remote_problems)) - set(hosts):
            self.drop_host(gone)
        now = time.monotonic()
        for host in hosts:
            # A host that did not answer is asked again after a while (remote_next), not every REMOTE_POLL: each look
            # is a Python process and an ssh that waits for its timeout. One where nothing runs is not asked (quiet).
            if host not in self.remote_reading and (not getattr(self, "hosts_polled", False)
                                                    or now >= self.remote_next.get(host, 0)) \
                    and not self.quiet(host, now):
                self.read_host(host)
        if self.hosts_again:
            self.hosts_again = False
            self.poll_remote()
        self.show_remote_note()
        self.show_all()

    def quiet(self, host, now=None):
        """True while HOST is not asked for questions: its starts were read last with none running, it lists no
        question, and the app has not started anything there within WAKE_SECONDS."""
        if host not in self.quiet_hosts or self.remote.get(host):
            return False
        now = time.monotonic() if now is None else now
        return not any(now < self.woken.get(key, 0) for key in (host, None))

    def set_fleet(self, fleet):
        """What the last reads of the hosts' starts showed (SandboxesPage.fleet: {host: {"active": starts running,
        paused or starting, None when not known}}): a host with none is not asked for questions any more; one that
        has one again (or is not known any more) is asked again at once."""
        for host, seen in (fleet or {}).items():
            if host == LOCAL:
                continue
            active = seen.get("active") if isinstance(seen, dict) else None
            if active == 0:
                if host not in self.quiet_hosts:
                    self.quiet_hosts.add(host)
                    if self.quiet(host):  # not asked any more: no note about its last read either
                        for store in (self.remote_problems, self.remote_failures, self.remote_next):
                            store.pop(host, None)
                        self.show_remote_note()
            elif host in self.quiet_hosts:
                self.quiet_hosts.discard(host)
                self.wake(host)

    def wake_hosts(self, hosts=None, seconds=WAKE_SECONDS):
        """The app started something on HOSTS (None: wherever, e.g. in a terminal): they are asked for questions for
        at least SECONDS from now on, whatever the last read of their starts said, and at once."""
        until = time.monotonic() + seconds
        for key in ([None] if hosts is None else hosts):
            if key != LOCAL:
                self.woken[key] = max(until, self.woken.get(key, 0))
        for host in (sorted(self.quiet_hosts) if hosts is None else hosts):
            if host != LOCAL and host in self.quiet_hosts:
                self.wake(host)

    def wake(self, host):
        """HOST is asked for questions again: now (or once more after the read that runs), whatever its back-off."""
        self.remote_next.pop(host, None)
        if self.known_hosts is not None and host not in self.known_hosts:
            return
        if host in self.remote_reading:
            self.remote_again.add(host)
        else:
            self.read_host(host)

    def read_host(self, host):
        started = time.monotonic()
        self.remote_reading[host] = background(
            self.sai.host_questions, host, timeout=backend.REMOTE_QUESTION_WAIT + 15,
            done=lambda result, error, h=host: self.host_read(h, result, error, started))

    def host_read(self, host, result, error, started=None):
        self.remote_reading.pop(host, None)
        started = time.monotonic() if started is None else started
        if self.known_hosts is not None and host not in self.known_hosts:
            self.remote_again.discard(host)
            return  # removed from the list meanwhile: its questions are not answered from here any more
        if error is None:
            self.remote[host] = list(result or [])
            self.remote_problems.pop(host, None)
            self.remote_next.pop(host, None)
            self.remote_failures.pop(host, None)
            self.remote_seen[host] = started
            # An answer there counts until a read that began after it no longer lists the question.
            listed = {(host, q.get("run", ""), q.get("id")) for q in self.remote[host]}
            for key, at in list(self.answered_there.items()):
                if key[0] == host and started >= at and key not in listed:
                    del self.answered_there[key]
        else:
            # Its last questions stay while their time runs, at most REMOTE_GRACE seconds after its last answer (a
            # host that answers again soon must not announce them anew; one that stopped answering must not keep
            # them, a page to open with no deadline included, for ever).
            self.remote_problems[host] = printable(error)[:200]
            self.expire_remote(host)
            failures = self.remote_failures[host] = self.remote_failures.get(host, 0) + 1
            self.remote_next[host] = time.monotonic() + min(REMOTE_BACKOFF, REMOTE_POLL / 1000 * 2 ** failures)
        if host in self.remote_again:
            self.remote_again.discard(host)
            self.read_host(host)
        self.show_remote_note()
        self.show_all()

    def expire_remote(self, host, now=None):
        """The questions of HOST, a host that does not answer, that may still be listed: those whose time runs, while
        its last answer is at most REMOTE_GRACE seconds old."""
        now = time.monotonic() if now is None else now
        seen = self.remote_seen.get(host)
        kept = [q for q in self.remote.get(host, []) if q.timing()[1] != 0] \
            if seen is not None and now - seen <= REMOTE_GRACE else []
        if kept:
            self.remote[host] = kept
        elif self.remote.pop(host, None) is not None:
            for key in [key for key in self.answered_there if key[0] == host]:
                del self.answered_there[key]
            return True
        return False

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
        if not keys - self.dismissed or not self.blocked_by_dialog():
            return
        if window is None:
            window = self.questions_window = QuestionsWindow(self)
            window.finished.connect(lambda _=0: self.dismissed.update(self.questions))
            window.installEventFilter(self)  # its Show and Hide turn the keys of the answers on and off
        window.show_questions()
        window.show()
        window.raise_()
        window.activateWindow()

    def blocked_by_dialog(self):
        """True while a modal dialog of another part of the app (a message, the wizard, Settings, the gallery) blocks
        the window: then the open questions are offered above it. Never over the answers' own confirmations (Allow…,
        Block always…, from this page or the Questions window): a second answer must not be stacked on the first."""
        modal = QApplication.activeModalWidget()
        if modal is None or self.confirming:
            return False
        window = self.questions_window
        return modal is not window and not (window is not None and window.isAncestorOf(modal))

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
        # The table lists the questions of the host chosen above (all are counted, announced and offered anyway).
        chosen = self.host_filter.currentData()
        pending = [entry for entry in pending if chosen is None or entry[0] == chosen]
        keys = [(host, run, q.get("id")) for host, run, _, q in pending]
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
            if self.key_of(entry) in self.answered_there:
                cell.setText(tr("answered"))
                cell.setData(SORT_ROLE, 1 << 30)
                cell.setForeground(QColor(theme.color("muted")))
            elif left is None:
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

    @property
    def answering(self):
        """An answer on its way (any question's), or None when none is."""
        return next(iter(self.answers.values()), None)

    @staticmethod
    def key_of(entry):
        run, question, host = entry
        return host, run, question.get("id")

    def widest(self, entry):
        """The widest allow ENTRY may get: this machine's policy, the run's security mode and the kind (as sandbox-ai
        caps it), or what the other host said of it (the same, there). A deny is never capped."""
        if entry is not None and entry[2] != LOCAL:
            scope = entry[1].get("max_scope")
            return scope if scope in backend.SCOPES else "once"
        if entry is not None:
            return local_widest(self.max_scope, self.records.get(self.key_of(entry)), entry[1])
        return self.max_scope if self.max_scope in backend.SCOPES else "once"

    def expired(self, entry, now=None):
        """True when sandbox-ai refused ENTRY already (its time is up); a question to open a page has no deadline."""
        if entry[1].get("kind") == "browser":
            return False
        return self.timing(entry, now)[1] == 0

    def answer_rules(self, entry):
        """{"allow", "deny", "block"}: None where the button may answer ENTRY, else why not."""
        if entry is None:
            why = tr("Select a question first.")
        elif self.key_of(entry) in self.answers:
            why = tr("The answer is on its way.")
        elif self.key_of(entry) in self.answered_there:
            why = tr("Answered: {host} lists it until it has read the answer.", host=printable(entry[2]))
        elif self.expired(entry):
            why = tr("Its time is up: sandbox-ai refused it already.")
        else:
            why = None
        rules = {"allow": why, "deny": why, "block": why}
        if why is None and entry[2] != LOCAL:
            rules["allow"] = self.remote_allow_refused(entry)
        elif why is None and self.widest(entry) is None:
            rules["allow"] = tr("This run approves at most once, and an allowed name stays pinned for the run: deny "
                                "it, or allow the name at the next start (--allow-host).")
        if why is None and entry[1].get("kind") not in ("connect", "dns"):
            rules["block"] = tr("Only names and addresses can be blocked always.")
        return rules

    @staticmethod
    def remote_allow_refused(entry):
        """Why another host's question ENTRY cannot be allowed from here, or None: that host allows no allow (its
        policy, the run's mode), or its page cannot be opened here (refused there, not http/https, not whole)."""
        question = entry[1]
        if question.get("allow_refused") or question.get("max_scope") is None:
            return tr("On {host} this can only be denied: {reason}", host=printable(entry[2]),
                      reason=printable(question.get("allow_refused") or "?"))
        if question.get("kind") != "browser":
            return None
        if question.get("url_refused"):
            return tr("{host} does not let the page be opened elsewhere: {reason}", host=printable(entry[2]),
                      reason=printable(question["url_refused"]))
        if question.get("url_truncated"):
            return tr("The address came shortened from {host}: it is not opened here.", host=printable(entry[2]))
        if openable_url(question.get("url")) is None:
            return tr("Only http and https pages are opened here.")
        return None

    def update_buttons(self):
        entry = self.current()
        rules = self.answer_rules(entry)
        widest = self.widest(entry)
        # Another host's page is opened here, where the human is: that host may have no screen at all.
        self.allow_button.setText(tr("Open here and allow") if remote_browser(entry) else tr("Allow"))
        self.allow_button.setAccessibleName(self.allow_button.text())
        set_enabled(self.allow_button, rules["allow"] is None, rules["allow"])
        set_enabled(self.deny_button, rules["deny"] is None, rules["deny"])
        set_enabled(self.deny_once_button, rules["deny"] is None, rules["deny"])
        set_enabled(self.block_button, rules["block"] is None, rules["block"])
        self.scope.setEnabled(rules["allow"] is None and not backend.question_url(entry[1]))
        # The scopes the selected question may get (another host's policy is that host's; None: no allow at all).
        limit = backend.SCOPES.index(widest) if widest in backend.SCOPES else 0
        for index in range(self.scope.count()):
            self.scope.model().item(index).setEnabled(backend.SCOPES.index(self.scope.itemData(index)) <= limit)
        if backend.SCOPES.index(self.scope.currentData()) > limit:
            self.scope.setCurrentIndex(self.scope.findData("once"))
        if self.questions_window is not None:
            self.questions_window.update_buttons()

    def eventFilter(self, watched, event):  # noqa: N802
        """Ctrl+Return (allow), Ctrl+Shift+D (deny for the run) and Ctrl+Shift+K (block always, asked first) while the
        focus is in the open questions (on this page, in their panel wherever it is docked, or in the Questions
        window): taken before any shortcut of the window. The application's filter is on only while one of them is
        shown (update_key_filter)."""
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.Hide) and (
                watched is self.open_box or watched is self.questions_window):
            QTimer.singleShot(0, self.update_key_filter)  # once the change of visibility is done
            return False
        if kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress) and isinstance(watched, QWidget):
            action = shortcut_of(event)
            target = self.key_target(watched) if action else None
            if target is not None:
                event.accept()
                if kind == QEvent.Type.KeyPress:
                    target.shortcut(action)
                return True
        return False

    def key_target(self, widget):
        """Who answers a key pressed in WIDGET: the Questions window, this page (its open questions), or None."""
        window = self.questions_window
        if window is not None and window.isVisible() and (widget is window or window.isAncestorOf(widget)):
            return window
        box = self.open_box
        if box.isVisible() and (widget is box or box.isAncestorOf(widget)):
            return self
        return None

    def update_key_filter(self):
        app = QApplication.instance()
        window = self.questions_window
        try:
            wanted = self.open_box.isVisible() or (window is not None and window.isVisible())
        except RuntimeError:  # the page is being deleted
            return
        if app is None or wanted == self.keys_on:
            return
        self.keys_on = wanted
        if wanted:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)

    def shortcut(self, action):
        button = {"allow": self.allow_button, "deny": self.deny_button, "block": self.block_button}[action]
        if button.isEnabled():
            button.click()

    def selection_changed(self):
        entry = self.current()
        url = backend.question_url(entry[1]) if entry else None
        destination = entry[1].get("url_host") if entry and entry[2] != LOCAL and entry[1].get("kind") == "browser" \
            else None
        self.url.setVisible(bool(url or destination))
        if url and entry[2] != LOCAL:
            self.url.setText(tr("{host} asks to open: {url}", host=printable(entry[2]), url=url)
                             + (" " + tr("(shortened)") if entry[1].get("url_truncated") else ""))
        elif destination:  # the page itself stays on that host (url_refused): where it leads is said
            self.url.setText(tr("{host} asks to open a page on {destination}", host=printable(entry[2]),
                                destination=printable(destination)))
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

    # -- the history of another host --

    def load_history(self):
        """The history of the host chosen: this machine's in the table (from the scans); a host's as `sandbox-ai
        approvals history` there says it, read in the background."""
        self.history_token += 1
        token, host = self.history_token, self.history_host.currentData() or LOCAL
        self.history.setVisible(host == LOCAL)
        self.history_text.setVisible(host != LOCAL)
        if host == LOCAL:
            return
        self.history_text.setPlainText(tr("Asking {host}…", host=printable(host)))
        background(self.sai.host_approvals_history, host, timeout=90,
                   done=lambda text, error: self.history_ready(token, host, text, error))

    def history_ready(self, token, host, text, error):
        if token != self.history_token:
            return
        if error is not None:
            self.history_text.setPlainText(tr("{host}: no answer", host=printable(host)) + f" ({printable(error)})")
            return
        self.history_text.setPlainText(text or tr("No answers on {host} yet.", host=printable(host)))

    # -- allowed always --

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.update_key_filter()
        if not self.always_loaded:
            self.load_always()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        QTimer.singleShot(0, self.update_key_filter)

    def load_always(self, note=None):
        """What was allowed "always", per project (read from sandbox-ai's file in a thread); NOTE is shown
        when it is there (e.g. "Forgotten.")."""
        self.always_loaded = True
        self.always_token += 1
        token = self.always_token
        host = self.always_host.currentData() or LOCAL
        if host == LOCAL:
            background(self.sai.remembered, timeout=30,
                       done=lambda rows, error: self.always_ready(token, rows, error, note, host))
        else:  # `sandbox-ai approvals remembered --json` there, over SSH
            self.always_message.setText(tr("Asking {host}…", host=printable(host)))
            background(lambda: self.sai.host_remembered(host), timeout=90,
                       done=lambda rows, error: self.always_ready(token, rows, error, note, host))

    def always_ready(self, token, rows, error, note=None, host=LOCAL):
        if token != self.always_token:
            return
        self.always_shown = host
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
        host = getattr(self, "always_shown", LOCAL)  # the host whose list is shown, never the one chosen meanwhile
        text = tr("Forget {entry} for {project}? The project's next sandboxes ask again. Sandboxes running now keep "
                  "what they already allowed until they end (take it back there with Sandboxes › Revoke…, sandbox-ai "
                  "net revoke).", entry=printable(entry), project=printable(project))
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "approval-forget", tr("Forget"), text, tr("Forget")):
            return
        if host == LOCAL:
            background(self.sai.forget_remembered, project, entry, owner=self, source=self.forget_button, timeout=90,
                       work=True, done=self.forgotten)
        else:
            background(lambda: self.sai.forget_remembered(project, entry, host=host), owner=self, work=True,
                       source=self.forget_button, timeout=120, done=self.forgotten)

    def forgotten(self, result, error):
        status, out, err = outcome(result, error)
        self.load_always(note=printable((out + err).strip()) if status != 0 else tr("Forgotten."))

    def answer(self, decision, scope=None, entry=None, parent=None, open_here=False):
        """Send the answer in the background (`sandbox-ai approvals` may take up to a minute; on another host through
        `sandbox-ai hosts run HOST --`), one per question: a slow host's answer holds up no other question. Allow asks
        first, naming the sandbox, its host, the question and the scope (the scope box, capped as sandbox-ai caps it);
        Deny takes one click and lasts for the run (as the web interface's and the command line's), SCOPE "once" (Deny
        once) this time only; Block always is a deny with the scope "always", asked first. A question whose time is up
        is not answered. ENTRY: the (run, question, host) to answer (default: the one highlighted); PARENT: the window
        asking; OPEN_HERE (a browser question of another host, always so for Allow): allowed there with --no-open,
        the page opened here once that host took the answer."""
        entry = entry or self.current()
        if not entry or self.key_of(entry) in self.answers or self.key_of(entry) in self.answered_there:
            return
        run, question, host = entry
        key = self.key_of(entry)
        if decision == "deny":
            # A deny is for the run unless "once" was chosen (the Deny once button); "always" is Block always. The
            # policy caps allows only: a deny is never narrowed.
            scope = scope if scope in backend.SCOPES else "run"
        else:
            refused = self.answer_rules(entry)["allow"] if self.key_of(entry) not in self.answers else None
            if refused:
                self.message.setText(refused)
                return
            scope = scope if scope in backend.SCOPES else self.scope.currentData()
            # Refused here above what the policy allows (another host: what it said); the caps of the run's mode and
            # of the kind narrow the answer in sandbox-ai itself, which says so (the box offers no more anyway).
            widest = self.widest(entry) if host != LOCAL else \
                self.max_scope if self.max_scope in backend.SCOPES else "once"
            if backend.SCOPES.index(scope) > backend.SCOPES.index(widest):
                self.message.setText(tr("Allowed at most “{scope}” here (the policy, the run's security mode or the "
                                        "kind of question).", scope=widest))
                return
            # Another host's page is opened here (that host may have no screen): allowed there with --no-open.
            open_here = open_here or remote_browser(entry)
        url = openable_url(question.get("url")) if open_here and question.get("kind") == "browser" else None
        if open_here and url is None:
            self.message.setText(tr("Only http and https pages are opened here."))
            return
        if self.expired(entry):
            self.message.setText(tr("Expired: sandbox-ai refused the question already."))
            return
        self.confirming += 1
        try:
            if decision == "allow" and not self.confirm_allow(entry, scope, parent, open_here=url is not None):
                return
            if decision == "deny" and scope == "always" and not self.confirm_block(entry, parent):
                return
        finally:
            self.confirming -= 1
        # Answered meanwhile (in the other window), or its time ran out while the question was open: not sent.
        if key in self.answers or key in self.answered_there or key not in self.questions:
            return
        if self.expired(entry):
            self.message.setText(tr("Expired: sandbox-ai refused the question already."))
            self.update_buttons()
            return
        self.scope.setCurrentIndex(self.scope.findData("once"))  # a wider scope is chosen for one answer only
        self.message.setText(tr("Sending the answer…"))
        ident = question.get("id", "")
        done = lambda result, error: self.answered(result, error, decision, scope, host, url, key)  # noqa: E731
        if host == LOCAL:
            # sandbox-ai answers within 60 s; a call that hangs past that (e.g. on a stuck pipe) frees the buttons.
            self.answers[key] = background(self.sai.answer, ident, decision, scope, timeout=ANSWER_WAIT, work=True,
                                           done=done)
        elif not self.sai.question_id.fullmatch(str(ident)):
            self.message.setText(tr("Not a question of sandbox-ai: {ident}", ident=printable(ident)[:60]))
            return
        else:
            argv = ["approvals", decision, ident, "--scope", scope] + (["--no-open"] if url is not None else [])
            self.answers[key] = background(lambda: self.sai.host_answer(host, argv, timeout=REMOTE_ANSWER_WAIT - 30),
                                           timeout=REMOTE_ANSWER_WAIT, done=done)
        self.update_buttons()

    def open_here(self):
        """A browser question of another host: allowed there without opening it (--no-open), opened here."""
        self.answer("allow", "once", open_here=True)

    def confirm_block(self, entry, parent=None):
        """Block always outlives the run: every later start of the project is refused it without a question."""
        run, question, host = entry
        lines = [tr("Sandbox: {sandbox}", sandbox=self.labels.get(self.key_of(entry), printable(run)))]
        if host != LOCAL:
            lines.append(tr("Host: {host}", host=printable(host)))
        lines += [tr("Asks to: {question}", question=describe(question)),
                  tr("Refused now and at every later start of this project, without a question. Forget… on this "
                     "page lifts it.")]
        return ask_first(parent or self, "approval-deny-always", tr("Block always?"), "\n".join(lines),
                         tr("Block always"))

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
        # The safe choice is the default when the answer outlives this run (the app's one policy: ask_first).
        if scope == "always" and not url:
            return ask_first(parent or self, "approval-allow-always", tr("Allow this request?"), "\n".join(lines),
                             tr("Allow"))
        return confirm(parent or self, tr("Allow this request?"), "\n".join(lines), tr("Allow"))

    def answered(self, result, error, decision="deny", scope="once", host=LOCAL, url=None, key=None):
        self.answers.pop(key, None)
        status, out, err = outcome(result, error)
        text = printable((out + err).strip())
        if status != 0:
            self.message.setText(tr("Not answered"))
            warn(self, tr("Not answered"), text or tr("sandbox-ai refused the answer."), modal=False)
        else:
            self.message.setText(text)
            if url is not None and decision == "allow":
                QDesktopServices.openUrl(url)
            if host != LOCAL and key is not None:
                self.answered_there[key] = time.monotonic()  # until a read begun from now on no longer lists it
        if host == LOCAL:
            self.poll()
            if status == 0 and scope == "always" and self.always_host.currentData() == LOCAL:
                self.load_always()  # it is listed there now
        else:
            self.poll_remote([host])  # read again now, also when a read of it runs already
            self.update_buttons()
            if status == 0 and scope == "always" and self.always_host.currentData() == host:
                self.load_always()


class StatisticsPage(QWidget):
    """What the logs of a project say: model calls and tokens, hosts, credentials, pushes, refusals,
    processes, MCP tool calls. On a host of `sandbox-ai hosts`: `sandbox-ai logs DIR --json` (or --project PATH)
    there, over SSH, for a folder of that host typed in."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The logs of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        top = QHBoxLayout()
        self.directory = QComboBox()
        self.directory.setAccessibleName(tr("Logs"))
        compact_combo(self.directory, 30)  # long log folders: elided here, whole in the list
        self.directory.currentIndexChanged.connect(self.load)
        browse = QPushButton(tr("Other folder…"))
        browse.clicked.connect(self.browse)
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(lambda: self.refresh_directories())
        from PySide6.QtWidgets import QCheckBox
        self.project_folder = QCheckBox(tr("a project folder (its default log folder)"))
        self.project_folder.setToolTip(tr("sandbox-ai logs --project PATH: the log folder the wizard gives that "
                                          "project"))
        self.project_folder.setVisible(False)
        self.project_folder.toggled.connect(lambda _=False: self.load())
        for widget in (QLabel(tr("Logs")), self.directory, browse, refresh, self.project_folder):
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
            # The bars take the rest of the width; a table without bars: its first text column.
            stretch = len(headers) - 1 if not headers[-1] else 1 if len(headers) > 2 else 0
            widget = table(headers, stretch=stretch, compact=[c for c in range(1, len(headers)) if c != stretch])
            widget.setAccessibleName(title)
            widget.setMinimumHeight(160)
            box_layout.addWidget(widget)
            grid.addWidget(box, index // 2, index % 2)
            self.tables[key] = widget
        self.data, self.generation = None, 0
        self.refresh_directories()

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self):
        """This machine: its log folders listed; another host: a folder of that host typed in (Enter reads it)."""
        remote = self.host() != LOCAL
        self.directory.blockSignals(True)
        self.directory.clear()
        self.directory.setEditable(remote)
        if remote:
            self.directory.lineEdit().setPlaceholderText(tr("a log folder or project folder on {host}",
                                                            host=self.hosts.label()))
            edit = self.directory.lineEdit()
            if edit is not getattr(self, "typed_folder", None):  # a new line edit each time it becomes editable
                edit.returnPressed.connect(self.load)
                self.typed_folder = edit
        self.directory.blockSignals(False)
        self.project_folder.setVisible(remote)
        if remote:
            self.generation += 1
            self.data = None
            self.summary.setText(tr("Type a folder on {host} and press Enter.", host=self.hosts.label()))
            for widget in self.tables.values():
                fill(widget, [])
        else:
            self.refresh_directories()

    def refresh_directories(self, select=None):
        if self.host() != LOCAL:
            if select:
                self.directory.setEditText(str(select))
            self.load()
            return
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
        if self.host() != LOCAL:
            from PySide6.QtWidgets import QInputDialog
            path, ok = QInputDialog.getText(self, tr("Log folder"), shown_as_text(tr(
                "A project folder or a log folder on {host} (absolute path):", host=self.hosts.label())))
            if ok and path.strip():
                self.refresh_directories(select=path.strip())
            return
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
        if self.host() != LOCAL:
            self.load_remote()
            return
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

    def load_remote(self):
        """`sandbox-ai logs DIR --json` (or --project DIR) on the host chosen, in the background."""
        host, folder = self.host(), self.directory.currentText().strip()
        self.generation += 1
        token = self.generation
        if not backend.valid_directory(folder):
            if folder:
                self.summary.setText(tr("Not an absolute path: {path}", path=printable(folder)))
            return
        project = self.project_folder.isChecked()
        self.summary.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.host_statistics(host, None if project else folder, folder if project else None),
                   owner=self, timeout=330, done=lambda data, error: self.loaded(token, data, error))

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


from .credentials_page import CredentialsPage  # noqa: E402,F401 - the Credentials page lives in its own module
from .images_page import ImagesPage  # noqa: E402,F401 - the Images page lives in its own module
