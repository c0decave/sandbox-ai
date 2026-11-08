"""The Hosts page: other machines (Proxmox LXC containers, servers) whose sandbox-ai this app manages over SSH,
with the list `sandbox-ai hosts` keeps (the web interface uses the same). For the chosen host: its starts and
sessions (attach in a terminal tab, pause, resume, stop), its open approvals (allow, deny), its doctor and the
agents' telemetry of every start there. Everything runs as `sandbox-ai hosts run HOST -- ARGS`, each argument
quoted on its own, over SSH with your keys (no password prompt, the host key learnt once); nothing listens on
the hosts but sshd.

Each host shows its sandbox-ai version and whether an update is there (hosts list's status, kept 10 minutes).
Update and Update all run `sandbox-ai hosts update` (each host's own signed update); a sudo that wants a password
opens the host's update in a terminal tab, where sudo asks. Adding and testing a host make a key file that others
can read private (ssh refuses it) and say so.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from .files import printable
from .i18n import tr
from .widgets import MONO, background, confirm, plain_label, warn

# Seconds between two looks at the chosen host while the page is shown (one at a time; an SSH host may be slow).
REFRESH_SECONDS = 20


def version_text(info):
    """A host's sandbox-ai in a few words: its version and whether an update is there ("" when not asked yet)."""
    if not info:
        return ""
    if not info.get("version"):
        return tr("unreachable") if info.get("error") else ""
    if info.get("kind") in ("checkout", "pip", "package"):
        return f"{info['version']} ({info['kind']})"
    if info.get("update"):
        return tr("{version}, update: {available}", version=info["version"], available=info.get("available") or "?")
    return tr("{version}, up to date", version=info["version"]) if info.get("update") is False else info["version"]


def key_lines(result):
    """What was made private on the way (a key file ssh refused as readable by others) and what only named."""
    return [tr("{path} could be read by others (mode {was}), which ssh refuses: made private ({now})",
               path=change.get("path"), was=change.get("was"), now=change.get("now"))
            for change in result.get("fixed") or []] + list(result.get("warnings") or [])


def update_text(name, result):
    """One host's update in a line or two."""
    if result.get("error") and not result.get("after"):
        line = tr("{host}: not updated: {error}", host=name, error=result["error"])
    elif result.get("changed"):
        line = tr("{host}: {before} -> {after}", host=name, before=result.get("before") or "?",
                  after=result["after"])
    else:
        line = tr("{host}: {version}, up to date", host=name,
                  version=result.get("after") or result.get("before") or "?")
    return "\n".join([line, *("  " + text for text in key_lines(result)),
                      *("  " + note for note in result.get("notes") or [])])


def cell(text):
    item = QTableWidgetItem(printable(str(text if text is not None else "")))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class AddHostDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Add a host"))
        form = QFormLayout(self)
        self.name, self.ssh, self.identity, self.description = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.name.setPlaceholderText(tr("ct101"))
        self.ssh.setPlaceholderText(tr("user@192.0.2.10"))
        self.identity.setPlaceholderText(tr("~/.ssh/id_ed25519 (optional)"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("SSH"), self.ssh)
        form.addRow(tr("Port"), self.port)
        form.addRow(tr("Key"), self.identity)
        form.addRow(tr("Description"), self.description)
        form.addRow(plain_label(tr("Your user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed "
                                   "there. The host key is learnt at the first contact and checked from then on."),
                                role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (self.name.text().strip(), self.ssh.text().strip(), self.port.value(), self.identity.text().strip(),
                self.description.text().strip())


class HostsPage(QWidget):
    def __init__(self, sai, in_terminal):
        super().__init__()
        self.sai, self.in_terminal = sai, in_terminal
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Other machines whose sandbox-ai this app manages over SSH (the web "
                                        "interface, sandbox-ai web, uses the same list)."), role="secondary"))
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self.refresh_host() if self.isVisible() else None)
        left_layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.add_button, self.remove_button, self.test_button = (QPushButton(tr("Add…")), QPushButton(tr("Remove")),
                                                                 QPushButton(tr("Test")))
        self.add_button.clicked.connect(self.add)
        self.remove_button.clicked.connect(self.remove)
        self.test_button.clicked.connect(self.test)
        for button in (self.add_button, self.remove_button, self.test_button):
            buttons.addWidget(button)
        left_layout.addLayout(buttons)
        updates = QHBoxLayout()
        self.update_button, self.update_all_button, self.versions_button = (
            QPushButton(tr("Update")), QPushButton(tr("Update all")), QPushButton(tr("Check versions")))
        self.update_button.setToolTip(tr("sandbox-ai update on this host (its own signed update; sudo for a system "
                                         "installation)"))
        self.update_all_button.setToolTip(tr("sandbox-ai update on every host of the list, one after the other"))
        self.update_button.clicked.connect(self.update_host)
        self.update_all_button.clicked.connect(self.update_all)
        self.versions_button.clicked.connect(lambda: self.load_versions(refresh=True))
        for button in (self.update_button, self.update_all_button, self.versions_button):
            updates.addWidget(button)
        left_layout.addLayout(updates)
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        # Whose starts and questions the tabs show: never another host's while the chosen one is asked.
        self.pane_title = plain_label(role="section")
        right_layout.addWidget(self.pane_title)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)
        # starts
        starts = QWidget()
        starts_layout = QVBoxLayout(starts)
        self.starts = QTableWidget(0, 5)
        self.starts.setHorizontalHeaderLabels([tr("Name"), tr("Agent"), tr("State"), tr("Workspace"), tr("Started")])
        self.starts.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.starts.horizontalHeader().setStretchLastSection(True)
        starts_layout.addWidget(self.starts, 1)
        row = QHBoxLayout()
        self.attach_button = QPushButton(tr("Attach"))
        self.attach_button.clicked.connect(self.attach)
        row.addWidget(self.attach_button)
        self.action_buttons = {}
        for action, label in (("pause", tr("Pause")), ("resume", tr("Resume")), ("stop", tr("Stop"))):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, a=action: self.start_action(a))
            self.action_buttons[action] = button
            row.addWidget(button)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(self.refresh_host)
        row.addStretch(1)
        row.addWidget(self.refresh_button)
        starts_layout.addLayout(row)
        self.tabs.addTab(starts, tr("Starts and sessions"))
        # approvals
        approvals = QWidget()
        approvals_layout = QVBoxLayout(approvals)
        self.questions = QTableWidget(0, 2)
        self.questions.setHorizontalHeaderLabels([tr("Question"), tr("Sandbox")])
        self.questions.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.questions.horizontalHeader().setStretchLastSection(True)
        approvals_layout.addWidget(self.questions, 1)
        row = QHBoxLayout()
        self.scope = QComboBox()
        for scope, text in (("once", tr("once")), ("run", tr("for this run")), ("always", tr("always"))):
            self.scope.addItem(text, scope)
        self.allow_button, self.deny_button = QPushButton(tr("Allow")), QPushButton(tr("Deny"))
        self.allow_button.clicked.connect(lambda: self.answer("allow"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        for widget in (self.scope, self.allow_button, self.deny_button):
            row.addWidget(widget)
        row.addStretch(1)
        approvals_layout.addLayout(row)
        self.tabs.addTab(approvals, tr("Approvals"))
        # doctor
        from .doctor_view import DoctorView
        self.doctor = DoctorView()  # sandbox-ai doctor --json of the host, by area, the fix commands copyable
        self.tabs.addTab(self.doctor, tr("Doctor"))
        # updates
        self.updates = QPlainTextEdit()
        self.updates.setReadOnly(True)
        self.updates.setFont(MONO)
        self.updates.setPlaceholderText(tr("Update or Update all: each host's own sandbox-ai update, signed, as "
                                           "install.sh installed it there."))
        self.tabs.addTab(self.updates, tr("Updates"))
        # defaults
        defaults = QWidget()
        form = QFormLayout(defaults)
        self.telemetry = QComboBox()
        for level, text in (("off", tr("off: no usage statistics, error reports, feedback or update checks "
                                       "(recommended)")),
                            ("strict", tr("strict: also no other calls than the model's")),
                            ("on", tr("on: the agents' own settings"))):
            self.telemetry.addItem(text, level)
        self.telemetry.activated.connect(self.set_telemetry)
        form.addRow(tr("Telemetry of Claude Code, Codex and OpenCode"), self.telemetry)
        self.defaults_note = plain_label(role="secondary")
        form.addRow(self.defaults_note)
        self.tabs.addTab(defaults, tr("Defaults"))
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        self.status = plain_label(role="secondary")
        layout.addWidget(self.status)
        self.entries, self.versions, self.queue, self.results = [], {}, [], []
        self.shown_host, self.asking, self.asked = None, None, 0
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_SECONDS * 1000)
        self.timer.timeout.connect(self.tick)
        self.load_hosts()

    # -- the list --

    def host(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def load_hosts(self, select=None):
        if not self.sai.hosts_supported():
            self.status.setText(tr("This sandbox-ai has no hosts yet: update it."))
            for widget in (self.add_button, self.remove_button, self.test_button, self.tabs, self.update_button,
                           self.update_all_button, self.versions_button):
                widget.setEnabled(False)
            return
        if not self.sai.host_update_supported():
            for widget in (self.update_button, self.update_all_button, self.versions_button):
                widget.setEnabled(False)
                widget.setToolTip(tr("This sandbox-ai cannot update hosts yet: update it."))
        else:
            try:
                self.versions = self.sai.host_statuses(cached=True)  # what is kept: no SSH here
            except (OSError, ValueError):
                self.versions = {}
        try:
            self.entries = self.sai.host_entries()
        except (OSError, ValueError) as exc:
            self.status.setText(printable(str(exc)))
            self.entries = []
        current = select or self.host()
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            text = tr("{name} (this machine)", name=entry["name"]) if entry.get("kind") == "local" else \
                f"{entry['name']}  {entry.get('ssh', '')}"
            version = version_text(self.versions.get(entry["name"]))
            if version:
                text += f"  ({version})"
            item = QListWidgetItem(printable(text))
            item.setData(Qt.ItemDataRole.UserRole, entry["name"])
            item.setToolTip(printable(entry.get("description") or ""))
            self.list.addItem(item)
            if entry["name"] == current:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self.remove_button.setEnabled(self.host() not in (None, "local"))
        if self.isVisible():
            self.refresh_host()

    def showEvent(self, event):
        """The selected host is asked when the page is shown, not when the window is built: no sandbox-ai command
        (and no SSH connection) at every start of the app for a page nobody opened; then every REFRESH_SECONDS while
        it is shown (open questions time out after a minute)."""
        super().showEvent(event)
        if self.host() is not None and not event.spontaneous():
            self.refresh_host()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def tick(self):
        if self.isVisible() and self.asking is None and self.host() is not None:
            self.refresh_host(quiet=True)

    def label(self, name):
        entry = next((e for e in self.entries if e.get("name") == name), {})
        return tr("{name} (this machine)", name=name) if entry.get("kind") == "local" or name == "local" else \
            f"{name}  {entry.get('ssh', '')}".strip()

    def clear_view(self, name, text):
        """Empty the tabs of the host shown so far: what is there belongs to NAME only once it answered."""
        self.starts.setRowCount(0)
        self.questions.setRowCount(0)
        self.shown_host = None
        self.pane_title.setText(printable(text))

    def add(self):
        dialog = AddHostDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, ssh, port, identity, description = dialog.values()
        def done(result, error):
            if error:
                warn(self, tr("Add a host"), printable(str(error)))
                return
            self.load_hosts(name)
            # A key ssh would refuse (others could read it) was made private, and the connection tried: said here.
            said = [line.strip() for line in str(result or "").splitlines() if line.strip().startswith(("key:", "note:",
                                                                                                    "connection:"))]
            if said:
                self.status.setText(printable("\n".join([name, *said])))
        background(self.sai.host_add, name, ssh, port, identity, description, owner=self, source=self.add_button,
                   timeout=60, done=done)

    def remove(self):
        name = self.host()
        if name in (None, "local"):
            return
        background(self.sai.host_remove, name, owner=self, source=self.remove_button, timeout=60,
                   done=lambda result, error: warn(self, tr("Remove"), printable(str(error))) if error
                   else self.load_hosts())

    def test(self):
        name = self.host()
        if name is None:
            return
        self.status.setText(tr("Testing {host}…", host=name))

        def done(result, error):
            if error:
                self.status.setText(printable(str(error)))
                return
            keys = key_lines(result)
            if result.get("error"):
                self.status.setText(printable("\n".join([f"{name}: {result['error']}", *keys])))
            else:
                self.status.setText(printable("\n".join([f"{name}: {result.get('version')}", *keys])))
            self.show_doctor(result.get("doctor") or [])
        background(self.sai.host_test, name, owner=self, source=self.test_button, timeout=180, done=done)

    # -- the chosen host --

    def refresh_host(self, quiet=False):
        """Ask the chosen host for its starts, questions and defaults. Another host's rows go at once (a slow SSH
        host must not look like the one asked before); QUIET (the timer): the same host's rows stay until it
        answers."""
        name = self.host()
        self.remove_button.setEnabled(name not in (None, "local"))
        if name is None:
            self.clear_view(None, "")
            return
        if name != self.shown_host:
            self.clear_view(name, tr("{host}: asking…", host=self.label(name)))
        if not quiet:
            self.status.setText(tr("Asking {host}…", host=name))
        self.asked += 1
        token = self.asking = (name, self.asked)
        background(lambda: (self.sai.host_json(name, "list", "--json"),
                            self.sai.host_json(name, "approvals", "list", "--json", timeout=60),
                            self.sai.host_json(name, "defaults", "show", "--json", timeout=60)),
                   owner=self, source=self.refresh_button, timeout=270,
                   done=lambda result, error: self.shown(name, result, error, token))

    def shown(self, name, result, error, token=None):
        if token is not None and token == self.asking:
            self.asking = None
        if name != self.host() or (token is not None and token[1] != self.asked):
            return  # another host was chosen meanwhile, or asked again since: only the latest answer counts
        if error:
            self.clear_view(name, tr("{host}: no answer", host=self.label(name)))
            self.status.setText(printable(f"{name}: {error}"))
            return
        starts, questions, defaults = result
        self.shown_host = name
        self.pane_title.setText(printable(tr("{host}: {starts} starts, {questions} open questions",
                                             host=self.label(name), starts=len(starts), questions=len(questions))))
        self.status.setText(tr("{host}: {starts} starts, {questions} open questions", host=name, starts=len(starts),
                               questions=len(questions)))
        self.starts.setRowCount(0)
        for entry in starts:
            row = self.starts.rowCount()
            self.starts.insertRow(row)
            for column, key in enumerate(("name", "agent", "state", "workspace", "started")):
                item = cell(entry.get(key))
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.starts.setItem(row, column, item)
        self.questions.setRowCount(0)
        for question in questions:
            row = self.questions.rowCount()
            self.questions.insertRow(row)
            item = cell(question.get("question"))
            item.setData(Qt.ItemDataRole.UserRole, question.get("id"))
            self.questions.setItem(row, 0, item)
            self.questions.setItem(row, 1, cell(question.get("sandbox") or question.get("run")))
        level = (defaults.get("values") or {}).get("telemetry", "off")
        self.telemetry.setEnabled(bool(defaults.get("telemetry_levels")))
        self.telemetry.setCurrentIndex(max(0, self.telemetry.findData(level)))
        self.defaults_note.setText(printable(tr("For every start on {host}, in {path}", host=name,
                                                path=defaults.get("path", "?"))))

    def chosen_start(self):
        rows = self.starts.selectionModel().selectedRows() if self.starts.selectionModel() else []
        if not rows:
            return None
        return self.starts.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def attach(self):
        entry, name = self.chosen_start(), self.host()
        if entry is None or name is None:
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", str(entry.get("name"))):
            warn(self, tr("Hosts"), tr("Not a start's name: {ident}", ident=printable(str(entry.get("name")))[:60]))
            return  # the other host chose it: never an option for sandbox-ai there
        if not entry.get("attachable"):
            warn(self, tr("Attach"), tr("{start} cannot be attached (not a running session).",
                                        start=printable(entry["name"])))
            return
        self.in_terminal(self.sai.host_terminal(name, "attach", entry["name"]), printable(f"{name}: {entry['name']}"))

    def start_action(self, action):
        entry, name = self.chosen_start(), self.host()
        if entry is None or name is None:
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", str(entry.get("name"))):
            warn(self, tr("Hosts"), tr("Not a start's name: {ident}", ident=printable(str(entry.get("name")))[:60]))
            return
        background(lambda: self.sai.host_run(name, action, entry["name"], timeout=180), owner=self,
                   source=self.action_buttons[action], timeout=210,
                   done=lambda result, error: self.acted(result, error))

    def acted(self, result, error):
        if error or (result and result[0] != 0):
            text = str(error) if error else (result[2] or result[1]).strip()[-500:]
            warn(self, tr("Hosts"), printable(text))
        elif result and (result[1] or "").strip():
            self.status.setText(printable(result[1].strip()[-500:]))  # what sandbox-ai said (a narrowed scope, ...)
        self.refresh_host(quiet=True)

    def answer(self, decision):
        rows = self.questions.selectionModel().selectedRows() if self.questions.selectionModel() else []
        name = self.host()
        if not rows or name is None:
            return
        ident = self.questions.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}", ident):
            warn(self, tr("Hosts"), tr("Not a question of sandbox-ai: {ident}", ident=printable(str(ident))[:60]))
            return  # the other host chose it: never an option for sandbox-ai there
        question = self.questions.item(rows[0].row(), 0).text()
        sandbox = self.questions.item(rows[0].row(), 1).text() if self.questions.item(rows[0].row(), 1) else ""
        scope = self.scope.currentData()  # read here, on the GUI thread, never in the worker
        if decision == "allow" and not confirm(
                self, tr("Allow this request?"), "\n".join([
                    tr("Host: {host}", host=self.label(name)), tr("Sandbox: {sandbox}", sandbox=sandbox),
                    tr("Asks to: {question}", question=question),
                    tr("Scope: {scope}", scope=self.scope.currentText())]
                    + ([tr("“Always” is remembered for this project: later runs get this without asking.")]
                       if scope == "always" else [])),
                tr("Allow"), destructive=scope == "always"):
            return
        # "Always" for a connection waits for the sandbox's certificate check (up to 30 s) on top of SSH.
        background(lambda: self.sai.host_run(name, "approvals", decision, ident, "--scope", scope, timeout=100),
                   owner=self, source=[self.allow_button, self.deny_button], timeout=130,
                   done=lambda result, error: self.acted(result, error))

    def set_telemetry(self, _index=None):
        name, level = self.host(), self.telemetry.currentData()
        if name is None:
            return
        args = ("defaults", "unset", "telemetry") if level == "off" else ("defaults", "set", "telemetry", level)
        background(lambda: self.sai.host_run(name, *args, timeout=60), owner=self, source=self.telemetry, timeout=90,
                   done=lambda result, error: self.acted(result, error))

    # -- versions and updates --

    def load_versions(self, refresh=False):
        """Each host's sandbox-ai version and whether an update is there (over SSH, in the background)."""
        if not self.sai.host_update_supported():
            return
        self.status.setText(tr("Asking the hosts for their versions…"))
        background(lambda: self.sai.host_statuses(refresh=refresh, cached=False), owner=self,
                   source=self.versions_button, timeout=300, done=self.versions_read)

    def versions_read(self, result, error):
        if error:
            self.status.setText(printable(str(error)))
            return
        self.versions = result or {}
        waiting = [name for name, info in self.versions.items() if info and info.get("update")]
        self.status.setText(tr("Updates for: {hosts}", hosts=", ".join(waiting)) if waiting else
                            tr("Every host answered is up to date."))
        self.load_hosts()

    def update_host(self):
        name = self.host()
        if name is None:
            return
        if name == "local":
            warn(self, tr("Update"), tr("This machine updates on the page System (or: sandbox-ai update)."))
            return
        self.results, self.queue = [], [name]
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.next_update()

    def update_all(self):
        names = [entry["name"] for entry in self.entries if entry.get("kind") == "ssh"]
        if not names:
            return
        self.results, self.queue = [], list(names)
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.next_update()

    def next_update(self, sudo=None):
        """The next host of the queue, one after the other (each may take minutes); the summary at the end."""
        if not self.queue:
            return self.updates_done()
        name, total = self.queue[0], len(self.results) + len(self.queue)
        self.status.setText(tr("Updating {host} ({index} of {count})…", host=name, index=len(self.results) + 1,
                               count=total))
        background(self.sai.host_update, name, sudo, owner=self,
                   source=[self.update_button, self.update_all_button], timeout=1200,
                   done=lambda result, error: self.updated(name, result, error))

    def updated(self, name, result, error):
        if error:
            result = {"name": name, "error": str(error)}
        if result.get("needs_sudo") and len(self.results) + len(self.queue) == 1 and confirm(
                self, tr("Update"), tr("The installation on {host} belongs to root. Update it with sudo?", host=name),
                tr("Update with sudo")):
            return self.next_update(sudo=True)
        self.queue.pop(0)
        self.results.append(result)
        self.updates.appendPlainText(printable(update_text(name, result)))
        if result.get("needs_password"):
            # sudo there wants a password: the host's update in a terminal tab, where sudo asks for it.
            try:
                self.in_terminal(self.sai.host_update_terminal(name), tr("{host}: update", host=name))
                self.updates.appendPlainText(printable(tr("  {host}: the update runs in a terminal tab, where sudo "
                                                          "asks for the password", host=name)))
            except (OSError, ValueError) as exc:
                self.updates.appendPlainText(printable(f"  {exc}"))
        self.next_update()

    def updates_done(self):
        results = self.results
        updated = sum(1 for r in results if r.get("changed"))
        failed = sum(1 for r in results if r.get("error") and not r.get("after"))
        summary = tr("{count} hosts: {updated} updated, {current} up to date, {failed} not updated",
                     count=len(results), updated=updated, current=len(results) - updated - failed, failed=failed)
        self.updates.appendPlainText(summary)
        self.status.setText(summary)
        try:
            self.versions = self.sai.host_statuses(cached=True)
        except (OSError, ValueError):
            pass
        self.load_hosts()

    def show_doctor(self, results):
        """The host's doctor (--json: a list of checks; an older one's text output as a string)."""
        if isinstance(results, str):
            self.doctor.set_text(results)
        else:
            self.doctor.set_results([r for r in results if isinstance(r, dict)])
        self.tabs.setCurrentWidget(self.doctor)
