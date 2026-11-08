"""The Hosts page: other machines (Proxmox LXC containers, servers) whose sandbox-ai this app manages over SSH,
with the list `sandbox-ai hosts` keeps (the web interface uses the same). One row per host: whether it answered,
its sandbox-ai version, how many starts run there and how many questions wait; Show starts and Show questions lead
to the Sandboxes and Approvals pages, filtered by that host (their actions live there, once). Here: add, edit,
remove and test a host, update it, its doctor and its defaults for every start (security mode, canaries, keep_days,
the agents' telemetry). Everything runs as `sandbox-ai hosts run HOST -- ARGS`, each argument quoted on its own,
over SSH with your keys (no password prompt, the host key learnt once); nothing listens on the hosts but sshd.

Each host shows its sandbox-ai version and whether an update is there (hosts list's status, kept 10 minutes).
Update and Update all run `sandbox-ai hosts update` (each host's own signed update); a sudo that wants a password
opens the host's update in a terminal tab, where sudo asks. Adding and testing a host make a key file that others
can read private (ssh refuses it) and say so.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .files import printable
from .i18n import tr
from .widgets import MONO, background, confirm, fill, plain_label, row_key, selected_rows, set_enabled, table, warn

# Security modes in rising strictness (sandbox-ai's modes.NAMES) and its default when defaults.json names none.
MODES = ("lax", "low", "medium", "high", "paranoid")
DEFAULT_MODE = "medium"
TELEMETRY_RANK = {"off": 2, "strict": 1, "on": 0}  # the more private, the higher
DEFAULT_CANARIES = ("credentials", "home")  # canary.DEFAULT_CATEGORIES, when the host does not say
CANARY_LIST = "list"  # the canaries choice "these categories"


def is_remote(entry):
    """A host of the list other than this machine (as SandboxAI.remote_hosts counts them)."""
    return entry.get("kind") != "local" and entry.get("name") != "local"


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


def reach_text(info, now=None):
    """Whether a host answered (the Sandboxes page's last look, {"seen", "problem"}), in a few words."""
    import time
    now = time.time() if now is None else now
    if not info:
        return tr("not asked yet")
    seen = info.get("seen")
    if info.get("problem"):
        return tr("no answer (last seen {seconds} s ago)", seconds=int(now - seen)) if seen else tr("no answer")
    return tr("yes") if seen else tr("not asked yet")


def security_steps_down(old, new):
    """What of the defaults OLD -> NEW ({key: value}, a key missing: sandbox-ai's own default) lowers the security
    of every later start: [text] (empty: nothing does)."""
    found = []
    old_mode, new_mode = old.get("mode", DEFAULT_MODE), new.get("mode", DEFAULT_MODE)
    if old_mode in MODES and new_mode in MODES and MODES.index(new_mode) < MODES.index(old_mode):
        found.append(tr("the security mode from {old} down to {new}", old=old_mode, new=new_mode))
    def canaries(value):
        return set() if value is False else set(value) if isinstance(value, list) else set(DEFAULT_CANARIES)
    lost = canaries(old.get("canaries")) - canaries(new.get("canaries"))
    if lost:
        found.append(tr("no canaries of {categories} any more", categories=", ".join(sorted(lost))))
    if TELEMETRY_RANK.get(new.get("telemetry", "off"), 0) < TELEMETRY_RANK.get(old.get("telemetry", "off"), 0):
        found.append(tr("the agents' telemetry from {old} to {new}", old=old.get("telemetry", "off"),
                        new=new.get("telemetry", "off")))
    old_days, new_days = old.get("keep_days"), new.get("keep_days")
    if isinstance(new_days, int) and (not isinstance(old_days, int) or new_days < old_days):
        found.append(tr("kept starts unused for {days} days removed with their HOME", days=new_days))
    return found


class EditHostDialog(QDialog):
    """What `sandbox-ai hosts set NAME` changes: whether its update runs with sudo. The rest is shown: changing the
    address or the key is Remove and Add (its host key is learnt anew)."""

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit {host}", host=printable(entry.get("name", ""))))
        form = QFormLayout(self)
        for label, key in ((tr("SSH"), "ssh"), (tr("Port"), "port"), (tr("Key"), "identity"),
                           (tr("Description"), "description")):
            form.addRow(label, plain_label(printable(str(entry.get(key) or "-"))))
        self.sudo = QCheckBox(tr("Its sandbox-ai is a system installation: update it with sudo"))
        self.sudo.setChecked(bool(entry.get("update_sudo")))
        form.addRow(self.sudo)
        form.addRow(plain_label(tr("To change the address, the port or the key: remove the host and add it again (its "
                                   "host key is learnt anew)."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


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
    """The hosts of `sandbox-ai hosts` with what the other pages last saw of them (set_fleet, set_questions): the
    Sandboxes page reads every host's starts, the Approvals page their questions; this page adds no SSH of its own for
    that. Show starts and Show questions switch to those pages, filtered by the host."""
    host_removed = Signal(str)  # a host left the list: the other pages drop its rows at once
    show_starts = Signal(str)  # Sandboxes, filtered by that host
    show_questions = Signal(str)  # Approvals, filtered by that host
    refresh_requested = Signal()  # look at every host now (the Sandboxes and Approvals pages do)

    def __init__(self, sai, in_terminal):
        super().__init__()
        self.sai, self.in_terminal = sai, in_terminal
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Other machines whose sandbox-ai this app manages over SSH (the web "
                                        "interface, sandbox-ai web, uses the same list)."), role="secondary"))
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.table = table([tr("Host"), tr("SSH"), tr("Answers"), tr("sandbox-ai"), tr("Running"), tr("Questions")],
                           stretch=1)
        self.table.setAccessibleName(tr("Hosts"))
        self.table.itemSelectionChanged.connect(self.host_changed)
        left_layout.addWidget(self.table, 1)

        def button(text, tip, slot, name=None):
            widget = QPushButton(text)
            widget.setToolTip(tip)
            widget.setAccessibleName(name or text.rstrip("…"))
            widget.clicked.connect(lambda _=False: slot())
            return widget
        self.starts_button = button(tr("Show starts"), tr("The Sandboxes page with this host's starts only"),
                                    lambda: self.host() and self.show_starts.emit(self.host()))
        self.questions_button = button(tr("Show questions"), tr("The Approvals page with this host's questions only"),
                                       lambda: self.host() and self.show_questions.emit(self.host()))
        self.refresh_button = button(tr("Refresh"), tr("Look at every host now (their starts and questions)"),
                                     self.refresh, tr("Refresh the hosts"))
        self.add_button = button(tr("Add…"), tr("Add a host reached over SSH (sandbox-ai hosts add)"), self.add,
                                 tr("Add a host"))
        self.edit_button = button(tr("Edit…"), tr("Change the host's settings (sandbox-ai hosts set)"), self.edit,
                                  tr("Edit the host"))
        self.remove_button = button(tr("Remove…"), tr("Forget the host (sandbox-ai hosts remove); asked first"),
                                    self.remove, tr("Remove the host"))
        self.test_button = button(tr("Test"), tr("SSH, the host's sandbox-ai and its doctor (sandbox-ai hosts test)"),
                                  self.test, tr("Test the host"))
        self.update_button = button(tr("Update"), tr("sandbox-ai update on this host (its own signed update; sudo for "
                                                     "a system installation)"), self.update_host, tr("Update the host"))
        self.update_all_button = button(tr("Update all"), tr("sandbox-ai update on every host of the list, one after "
                                                             "the other"), self.update_all)
        self.versions_button = button(tr("Check versions"), tr("Ask every host for its sandbox-ai version now"),
                                      lambda: self.load_versions(refresh=True))
        for group in ((self.starts_button, self.questions_button, self.refresh_button),
                      (self.add_button, self.edit_button, self.remove_button, self.test_button),
                      (self.update_button, self.update_all_button, self.versions_button)):
            row = QHBoxLayout()
            for widget in group:
                row.addWidget(widget)
            row.addStretch(1)
            left_layout.addLayout(row)
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.pane_title = plain_label(role="section")
        right_layout.addWidget(self.pane_title)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)
        # doctor
        from .doctor_view import DoctorView
        self.doctor = DoctorView()  # sandbox-ai doctor --json of the host, by area, the fix commands copyable
        self.tabs.addTab(self.doctor, tr("Doctor"))
        # updates
        self.updates = QPlainTextEdit()
        self.updates.setReadOnly(True)
        self.updates.setFont(MONO)
        self.updates.setAccessibleName(tr("Updates"))
        self.updates.setPlaceholderText(tr("Update or Update all: each host's own sandbox-ai update, signed, as "
                                           "install.sh installed it there."))
        self.tabs.addTab(self.updates, tr("Updates"))
        # defaults (sandbox-ai defaults there): what every start on the host uses unless it says otherwise
        defaults = QWidget()
        form = QFormLayout(defaults)
        self.mode = QComboBox()
        self.mode.setAccessibleName(tr("Security mode"))
        self.telemetry = QComboBox()
        self.telemetry.setAccessibleName(tr("Telemetry"))
        for level, text in (("off", tr("off: no usage statistics, error reports, feedback or update checks "
                                       "(recommended)")),
                            ("strict", tr("strict: also no other calls than the model's")),
                            ("on", tr("on: the agents' own settings"))):
            self.telemetry.addItem(text, level)
        self.canaries = QComboBox()
        self.canaries.setAccessibleName(tr("Canaries"))
        self.canary_list = QLineEdit()
        self.canary_list.setAccessibleName(tr("Canary categories"))
        self.canary_list.setPlaceholderText(", ".join(DEFAULT_CANARIES))
        self.canaries.currentIndexChanged.connect(lambda _=0: self.canary_list.setEnabled(
            self.canaries.currentData() == CANARY_LIST))
        canary_row = QHBoxLayout()
        canary_row.addWidget(self.canaries)
        canary_row.addWidget(self.canary_list, 1)
        self.keep = QCheckBox(tr("Remove kept starts unused for"))
        self.keep.setAccessibleName(tr("Remove kept starts after a while"))
        self.keep_days = QSpinBox()
        self.keep_days.setAccessibleName(tr("Days"))
        self.keep_days.setRange(1, 3650)
        self.keep_days.setValue(90)
        self.keep_days.setSuffix(" " + tr("days"))
        self.keep.toggled.connect(self.keep_days.setEnabled)
        keep_row = QHBoxLayout()
        keep_row.addWidget(self.keep)
        keep_row.addWidget(self.keep_days)
        keep_row.addStretch(1)
        form.addRow(tr("Security mode"), self.mode)
        form.addRow(tr("Canaries"), canary_row)
        form.addRow(tr("Kept starts"), keep_row)
        form.addRow(tr("Telemetry of Claude Code, Codex and OpenCode"), self.telemetry)
        self.save_defaults_button = button(tr("Save defaults…"), tr("Write the defaults of every later start on the "
                                                                    "host (sandbox-ai defaults set/unset there); a "
                                                                    "step down in security is asked first"),
                                           self.save_defaults)
        form.addRow(self.save_defaults_button)
        self.defaults_note = plain_label(role="secondary")
        form.addRow(self.defaults_note)
        self.tabs.addTab(defaults, tr("Defaults"))
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        self.status = plain_label(role="secondary")
        layout.addWidget(self.status)
        self.entries, self.versions, self.queue, self.results = [], {}, [], []
        self.fleet, self.question_counts = {}, {}
        self.defaults, self.defaults_host, self.defaults_token = None, None, 0
        self.set_defaults_enabled(False)
        self.load_hosts()

    # -- the list --

    def host(self):
        rows = selected_rows(self.table)
        return row_key(self.table, rows[0]) if rows else None

    def entry(self, name):
        return next((e for e in self.entries if e.get("name") == name), {})

    def load_hosts(self, select=None):
        if not self.sai.hosts_supported():
            self.status.setText(tr("This sandbox-ai has no hosts yet: update it."))
            for widget in (self.add_button, self.remove_button, self.test_button, self.edit_button, self.tabs,
                           self.update_button, self.update_all_button, self.versions_button):
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
        self.show_hosts()
        if current is not None and current in self.table.row_keys:
            self.table.clearSelection()
            self.table.selectRow(self.row_of(current))
        if not selected_rows(self.table) and self.table.rowCount():
            self.table.selectRow(0)
        self.host_changed()

    def row_of(self, name):
        from .widgets import find_row
        return find_row(self.table, name)

    def show_hosts(self):
        """One row per host: whether it answered, its version, starts running, questions waiting."""
        rows, colors = [], []
        for entry in self.entries:
            name = entry["name"]
            local = entry.get("kind") == "local" or name == "local"
            info = self.fleet.get(name)
            count = info.get("running") if info else None
            questions = self.question_counts.get(name)
            rows.append([tr("{name} (this machine)", name=printable(name)) if local else printable(name),
                         "" if local else printable(f"{entry.get('ssh', '')}:{entry.get('port', 22)}"),
                         tr("yes") if local else reach_text(info), version_text(self.versions.get(name)),
                         "" if count is None else str(count), "" if questions is None else str(questions)])
            colors.append("bad" if info and info.get("problem") else "warn" if questions else None)
        # The selection stays on its host (fill() keeps it by key) without a change of host on the way: the
        # defaults of the host shown are not asked again at every look at the fleet.
        self.table.blockSignals(True)
        try:
            fill(self.table, rows, colors, tooltips=[printable(e.get("description") or "") or None
                                                     for e in self.entries], keys=[e["name"] for e in self.entries])
        finally:
            self.table.blockSignals(False)

    def set_fleet(self, fleet):
        """{host: {"running", "seen", "problem"}} from the Sandboxes page's last look at every host."""
        self.fleet = dict(fleet or {})
        self.show_hosts()

    def set_questions(self, counts):
        """{host: open questions} from the Approvals page."""
        counts = dict(counts or {})
        if counts != self.question_counts:
            self.question_counts = counts
            self.show_hosts()

    def refresh(self):
        self.refresh_requested.emit()
        self.load_hosts()

    def label(self, name):
        entry = self.entry(name)
        return tr("{name} (this machine)", name=name) if entry.get("kind") == "local" or name == "local" else \
            f"{name}  {entry.get('ssh', '')}".strip()

    def host_changed(self):
        name = self.host()
        remote = name not in (None, "local")
        set_enabled(self.remove_button, remote, tr("This machine is always in the list.") if name else None)
        set_enabled(self.edit_button, remote, tr("This machine has no SSH settings.") if name else None)
        for widget in (self.starts_button, self.questions_button, self.test_button):
            widget.setEnabled(name is not None)
        self.pane_title.setText(printable(self.label(name)) if name else "")
        if name != self.defaults_host:
            self.defaults, self.defaults_host = None, None
            self.set_defaults_enabled(False)
            self.defaults_note.setText("")
            if self.isVisible() and name is not None:
                self.load_defaults()

    def showEvent(self, event):  # noqa: N802
        """The chosen host's defaults are asked when the page is shown, not when the window is built (no SSH at
        every start of the app for a page nobody opened)."""
        super().showEvent(event)
        if self.host() is not None and self.defaults_host != self.host() and not event.spontaneous():
            self.load_defaults()

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
            self.refresh_requested.emit()
            # A key ssh would refuse (others could read it) was made private, and the connection tried: said here.
            said = [line.strip() for line in str(result or "").splitlines() if line.strip().startswith(("key:", "note:",
                                                                                                    "connection:"))]
            if said:
                self.status.setText(printable("\n".join([name, *said])))
        background(self.sai.host_add, name, ssh, port, identity, description, owner=self, source=self.add_button,
                   timeout=60, done=done)

    def edit(self):
        name = self.host()
        if name in (None, "local"):
            return
        dialog = EditHostDialog(self.entry(name), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        sudo = dialog.sudo.isChecked()
        if sudo == bool(self.entry(name).get("update_sudo")):
            return
        background(self.sai.host_set, name, sudo, owner=self, source=self.edit_button, timeout=60,
                   done=lambda result, error: warn(self, tr("Edit {host}", host=name), printable(str(error)))
                   if error else self.load_hosts(name))

    def remove(self):
        name = self.host()
        if name in (None, "local"):
            return
        if not confirm(self, tr("Remove host"), tr(
                "Remove {host} from the list of hosts? Its sandbox-ai and its starts stay there; this app and sandbox-ai "
                "web no longer reach it (its approvals are no longer answered from here).", host=self.label(name)),
                tr("Remove"), destructive=True):
            return

        def done(result, error):
            if error:
                warn(self, tr("Remove"), printable(str(error)))
                return
            self.fleet.pop(name, None)
            self.question_counts.pop(name, None)
            self.host_removed.emit(name)  # its starts and questions leave the other pages at once
            self.load_hosts()
        background(self.sai.host_remove, name, owner=self, source=self.remove_button, timeout=60, done=done)

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

    # -- the chosen host's defaults --

    def set_defaults_enabled(self, enabled):
        for widget in (self.mode, self.canaries, self.canary_list, self.keep, self.keep_days, self.telemetry,
                       self.save_defaults_button):
            widget.setEnabled(enabled)
        if enabled:
            self.canary_list.setEnabled(self.canaries.currentData() == CANARY_LIST)
            self.keep_days.setEnabled(self.keep.isChecked())

    def load_defaults(self):
        """`sandbox-ai defaults show --json` of the chosen host (this machine: here), in the background."""
        name = self.host()
        if name is None:
            return
        self.defaults_token += 1
        token = self.defaults_token
        self.defaults_note.setText(tr("Asking {host}…", host=name))
        background(lambda: self.sai.host_json(name, "defaults", "show", "--json", timeout=60), owner=self, timeout=90,
                   done=lambda result, error: self.defaults_read(token, name, result, error))

    def defaults_read(self, token, name, result, error):
        if token != self.defaults_token or name != self.host():
            return  # another host was chosen meanwhile: only its own answer counts
        if error is not None or not isinstance(result, dict):
            self.defaults_note.setText(printable(tr("{host}: no answer", host=name) + f" ({error})"))
            return
        values = result.get("values") if isinstance(result.get("values"), dict) else {}
        self.defaults, self.defaults_host = dict(values), name
        modes = [m for m in result.get("modes") or MODES if isinstance(m, str)]
        self.mode.clear()
        self.mode.addItem(tr("sandbox-ai's default ({mode})", mode=DEFAULT_MODE), None)
        for mode in modes:
            self.mode.addItem(printable(mode), mode)
        self.mode.setCurrentIndex(max(0, self.mode.findData(values.get("mode"))) if values.get("mode") else 0)
        default = [c for c in result.get("canary_default") or DEFAULT_CANARIES if isinstance(c, str)]
        self.canaries.clear()
        self.canaries.addItem(tr("sandbox-ai's default ({categories})", categories=", ".join(default)), None)
        self.canaries.addItem(tr("off"), False)
        self.canaries.addItem(tr("these categories:"), CANARY_LIST)
        canaries = values.get("canaries")
        self.canaries.setCurrentIndex(1 if canaries is False else 2 if isinstance(canaries, list) else 0)
        self.canary_list.setText(", ".join(printable(c) for c in canaries) if isinstance(canaries, list) else "")
        self.canary_categories = [c for c in result.get("canary_categories") or () if isinstance(c, str)]
        days = values.get("keep_days")
        self.keep.setChecked(isinstance(days, int) and days > 0)
        if isinstance(days, int) and days > 0:
            self.keep_days.setValue(days)
        self.telemetry.setCurrentIndex(max(0, self.telemetry.findData(values.get("telemetry", "off"))))
        self.set_defaults_enabled(True)
        self.telemetry.setEnabled(bool(result.get("telemetry_levels")))
        self.defaults_note.setText(printable(tr("For every start on {host}, in {path}", host=name,
                                                path=result.get("path", "?"))))

    def chosen_defaults(self):
        """{key: value} as the form says (a key left out: sandbox-ai's own default), or a ValueError."""
        chosen = {}
        if self.mode.currentData():
            chosen["mode"] = self.mode.currentData()
        if self.canaries.currentData() is False:
            chosen["canaries"] = False
        elif self.canaries.currentData() == CANARY_LIST:
            categories = [c.strip() for c in self.canary_list.text().replace(",", " ").split() if c.strip()]
            known = getattr(self, "canary_categories", []) or list(DEFAULT_CANARIES) + ["workspace"]
            unknown = [c for c in categories if c not in known]
            if unknown or not categories:
                raise ValueError(tr("Canary categories: {known}", known=", ".join(known)))
            chosen["canaries"] = categories
        if self.keep.isChecked():
            chosen["keep_days"] = self.keep_days.value()
        level, before = self.telemetry.currentData(), (self.defaults or {}).get("telemetry")
        if not self.telemetry.isEnabled():  # a sandbox-ai there without telemetry levels: left as it is
            if before is not None:
                chosen["telemetry"] = before
        elif level != "off" or before == "off":  # "off" is the default: written only where it was written
            chosen["telemetry"] = level
        return chosen

    def save_defaults(self):
        name = self.host()
        if name is None or self.defaults is None or name != self.defaults_host:
            return
        try:
            chosen = self.chosen_defaults()
        except ValueError as exc:
            warn(self, tr("Defaults"), str(exc))
            return
        old = {key: value for key, value in self.defaults.items() if key in ("mode", "canaries", "keep_days",
                                                                              "telemetry")}
        steps = [("set", key, value) for key, value in chosen.items() if old.get(key, object()) != value] + \
                [("unset", key, None) for key in old if key not in chosen]
        if not steps:
            self.defaults_note.setText(tr("Nothing changed."))
            return
        lower = security_steps_down(old, chosen)
        if lower and not confirm(self, tr("Defaults of {host}", host=self.label(name)), tr(
                "Every later start on {host} gets less protection: {changes}. Save anyway?", host=self.label(name),
                changes="; ".join(lower)), tr("Save"), destructive=True):
            return
        argv = [("defaults", "set", key, json.dumps(value)) if op == "set" else ("defaults", "unset", key)
                for op, key, value in steps]

        def work():
            said = []
            for args in argv:
                status, out, err = self.sai.host_run(name, *args, timeout=60)
                said.append((out + err).strip())
                if status != 0:
                    raise ValueError("\n".join(said))
            return "\n".join(said)

        def done(result, error):
            if error:
                warn(self, tr("Defaults"), printable(str(error)))
            self.defaults_host = None
            self.load_defaults()
        background(work, owner=self, source=self.save_defaults_button, timeout=60 * len(argv) + 30, done=done)

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
        names = [entry["name"] for entry in self.entries if is_remote(entry)]
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
