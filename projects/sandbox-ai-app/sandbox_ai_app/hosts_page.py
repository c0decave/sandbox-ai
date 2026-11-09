"""The Hosts page: other machines (Proxmox LXC containers, servers) whose sandbox-ai this app manages over SSH,
with the list `sandbox-ai hosts` keeps (the web interface uses the same). One row per host: whether it answered,
its sandbox-ai version, how many starts run there and how many questions wait; Show starts and Show questions lead
to the Sandboxes and Approvals pages, filtered by that host (their actions live there, once). Here: add, edit,
remove and test a host, update it, its doctor and its defaults for every start (security mode, canaries, keep_days,
the agents' telemetry). Everything runs as `sandbox-ai hosts run HOST -- ARGS`, each argument quoted on its own,
over SSH with your keys (no password prompt, the host key learnt once); nothing listens on the hosts but sshd.

Each host shows its sandbox-ai version and whether an update is there (hosts list's status, kept 10 minutes).
Update and Update all run `sandbox-ai hosts update` (each host's own signed update); a sudo that wants a password
opens the host's update in a terminal tab, where sudo asks. Update from here (hosts update --from-here) puts this
machine's copy there instead, as the same kind of installation, asked first like an install; an update that finds no
published release offers it at once. Adding and testing a host make a key file that others
can read private (ssh refuses it) and say so.

The Web interface tab manages the host's `sandbox-ai web` (WebPanel): its logins (web user list; Add login… and New
password… run in a terminal, where the password and the TOTP code are typed; Remove… asked first), its certificate's
fingerprint (web cert; Renew… asked first), xterm.js for its terminals (web vendor) and the service itself (web serve
in a terminal tab, as long as that runs; an address beyond the loopback asked first).
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import backend
from .files import printable
from .host_choice import on_host
from .i18n import tr
from .widgets import (
    MONO, Task, ask_first, background, compact_combo, confirm, fill, flow, inform, plain_label, row_key, safe_tooltip,
    selected_rows, set_enabled, table, warn,
)

# Security modes in rising strictness (sandbox-ai's modes.NAMES) and its default when defaults.json names none; the
# orders of protection are backend's (the web interface's): one policy for what counts as a step down.
MODES = backend.MODE_ORDER
DEFAULT_MODE = backend.DEFAULT_MODE
DEFAULT_CANARIES = backend.DEFAULT_CANARIES  # canary.DEFAULT_CATEGORIES, when the host does not say
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


def security_steps_down(old, new, canary_default=None):
    """What of the defaults OLD -> NEW ({key: value}, a key missing: sandbox-ai's own default) lowers the security
    of every later start, as backend.defaults_lowered decides it (CANARY_DEFAULT: that host's own): [text] (empty:
    nothing does)."""
    found = []
    for key, before, after in backend.defaults_lowered(old, new, canary_default):
        if key == "mode":
            found.append(tr("the security mode from {old} down to {new}", old=printable(before), new=printable(after)))
        elif key == "telemetry":
            found.append(tr("the agents' telemetry from {old} to {new}", old=printable(before), new=printable(after)))
        elif key == "canaries":
            found.append(tr("no canaries of {categories} any more", categories=", ".join(map(printable, before))))
        elif key in ("subnet", "subnet6"):
            found.append(tr("the network of every later start: {setting} from {old} to {new}", setting=key,
                            old=printable(before) if before else tr("sandbox-ai's default"),
                            new=printable(after) if after else tr("sandbox-ai's default")))
        elif key == "preset_dirs":
            added = after if isinstance(after, (list, tuple)) else [after] if after else []
            found.append(tr("presets read from {dirs} too", dirs=", ".join(map(printable, added))))
        elif isinstance(before, int):
            found.append(tr("kept starts unused for {days} days removed with their HOME (instead of {before})",
                            days=after, before=before))
        else:
            found.append(tr("kept starts unused for {days} days removed with their HOME", days=after))
    return found


EDITED_DEFAULTS = ("mode", "canaries", "keep_days", "telemetry", "subnet", "subnet6")  # what the Defaults tab writes


def subnet_value(text, version):
    """TEXT as a subnet of IP VERSION (4 or 6) in CIDR form (as sandbox-ai's defaults take it), or a ValueError."""
    import ipaddress
    try:
        network = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        network = None
    if network is None or network.version != version or "/" not in text:
        raise ValueError(tr("Not an IPv{version} subnet (ADDRESS/PREFIX, e.g. {example}): {text}", version=version,
                            example="10.77.0.0/24" if version == 4 else "fd77::/64", text=printable(text)[:60]))
    return str(network)


def default_step(op, key, value):
    """A change of the defaults in words: "mode = high", "canaries: sandbox-ai's default"."""
    if op == "unset":
        return tr("{setting}: sandbox-ai's default", setting=key)
    return f"{key} = {printable(json.dumps(value))}"


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


def ssh_user(entry):
    """The user of a host's SSH target (USER@HOST), or "" when it names none."""
    target = str(entry.get("ssh") or "")
    return target.split("@", 1)[0] if "@" in target else ""


class InstallDialog(QDialog):
    """How sandbox-ai is installed on a host (`sandbox-ai hosts install`): for the SSH user or for every user (under
    a prefix), as root, with sudo or without, from this machine's copy or the signed release the host downloads."""

    def __init__(self, entry, version="", parent=None):
        super().__init__(parent)
        name, user = printable(entry.get("name", "")), printable(ssh_user(entry))
        self.root = user == "root"
        self.setWindowTitle(tr("Install sandbox-ai on {host}", host=name))
        form = QFormLayout(self)
        self.for_user = QRadioButton(tr("For the SSH user ({user}): in its ~/.local", user=user or "?"))
        self.for_system = QRadioButton(tr("For every user, under:"))
        self.prefix = QLineEdit("/usr/local")
        self.prefix.setAccessibleName(tr("Prefix"))
        where = QButtonGroup(self)
        for button in (self.for_user, self.for_system):
            where.addButton(button)
        (self.for_system if self.root else self.for_user).setChecked(True)
        system_row = QHBoxLayout()
        system_row.addWidget(self.for_system)
        system_row.addWidget(self.prefix, 1)
        form.addRow(tr("Where"), self.for_user)
        form.addRow("", system_row)
        self.how = QComboBox()
        self.how.setAccessibleName(tr("As whom"))
        if self.root:
            self.how.addItem(tr("as root (the SSH user is root)"), None)
        else:
            self.how.addItem(tr("with sudo (asked there when sudo wants a password)"), True)
        self.how.addItem(tr("without sudo (refused where root is needed)"), False)
        form.addRow(tr("As whom"), self.how)
        self.here = QRadioButton(tr("This machine's copy ({version})", version=printable(version) or "?"))
        self.download = QRadioButton(tr("The signed release, downloaded by the host"))
        self.version = QLineEdit()
        self.version.setPlaceholderText(tr("the newest (or X.Y.Z)"))
        self.version.setAccessibleName(tr("Version"))
        source = QButtonGroup(self)
        for button in (self.here, self.download):
            source.addButton(button)
        self.here.setChecked(True)
        download_row = QHBoxLayout()
        download_row.addWidget(self.download)
        download_row.addWidget(self.version, 1)
        form.addRow(tr("From"), self.here)
        form.addRow("", download_row)
        for button in (self.for_user, self.for_system, self.here, self.download):
            button.toggled.connect(lambda _=False: self.follow())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.follow()

    def follow(self):
        self.prefix.setEnabled(self.for_system.isChecked())
        self.how.setEnabled(self.for_system.isChecked())  # an installation for the user needs no root
        self.version.setEnabled(self.download.isChecked())

    def values(self):
        """{mode, prefix, sudo, source, version} for SandboxAI.host_install."""
        system = self.for_system.isChecked()
        download = self.download.isChecked()
        return {"mode": "system" if system else "user",
                "prefix": (self.prefix.text().strip() or None) if system else None,
                "sudo": self.how.currentData() if system else None,
                "source": "download" if download else "here",
                "version": (self.version.text().strip() or None) if download else None}

    def words(self):
        """The choice in words, for the question before it runs."""
        values = self.values()
        where = tr("for every user under {prefix}", prefix=printable(values["prefix"] or "/usr/local")) \
            if values["mode"] == "system" else tr("for the SSH user")
        how = "" if values["mode"] != "system" else (" " + (tr("as root") if self.root else tr("with sudo")
                                                            if values["sudo"] else tr("without sudo")))
        source = tr("from the signed release ({version})", version=printable(values["version"] or tr("the newest"))) \
            if values["source"] == "download" else tr("from this machine's copy")
        return f"{where}{how}, {source}"


def install_text(name, result):
    """One host's installation in a line or two, as sandbox-ai hosts install says it."""
    if result.get("error"):
        line = tr("{host}: not installed: {error}", host=name, error=printable(result["error"]))
    else:
        line = tr("{host}: sandbox-ai {before} -> {after}", host=name, before=printable(result.get("before") or "-"),
                  after=printable(result.get("after") or "?"))
    return "\n".join([line, *("  " + printable(note) for note in result.get("notes") or [])])


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
        self.command = QLineEdit()
        self.command.setPlaceholderText(tr("/opt/sandbox-ai/bin/sandbox-ai (optional)"))
        self.command.setToolTip(tr("sandbox-ai's path there, if it is not on the PATH or in ~/.local/bin (--command)"))
        form.addRow(tr("sandbox-ai there"), self.command)
        self.update_sudo = QCheckBox(tr("Its sandbox-ai is a system installation: update it with sudo"))
        form.addRow(self.update_sudo)
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

    def extra(self):
        """{"command", "update_sudo"} of backend.host_add: sandbox-ai's path there, whether it updates with sudo."""
        return {"command": self.command.text().strip(), "update_sudo": self.update_sudo.isChecked()}


class WebPanel(QWidget):
    """The web interface of the chosen host (`sandbox-ai web` there): logins, certificate, xterm.js, the service.
    IN_TERMINAL(argv, title) opens a terminal tab: what asks for a password or runs on stays there."""

    def __init__(self, sai, in_terminal, parent=None):
        super().__init__(parent)
        self.sai, self.in_terminal, self.host, self.token, self.loaded_for = sai, in_terminal, None, 0, None
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("The host's web interface (sandbox-ai web there): who may log in, its "
                                        "certificate and the service itself. Passwords and TOTP codes are typed in a "
                                        "terminal, never through the app."), role="secondary"))
        self.users = table([tr("Login"), tr("Since")], stretch=0)
        self.users.setAccessibleName(tr("Logins of the web interface"))
        self.users.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.users, 1)

        def button(text, tip, slot):
            widget = QPushButton(text)
            widget.setToolTip(safe_tooltip(tip))
            widget.setAccessibleName(text.rstrip("…"))
            widget.clicked.connect(lambda _=False: slot())
            return widget
        self.refresh_button = button(tr("Refresh"), tr("sandbox-ai web user list on the host"), self.refresh)
        self.add_button = button(tr("Add login…"), tr("sandbox-ai web user add NAME in a terminal: a password (12 or "
                                                       "more characters) and a TOTP code from your app"), self.add)
        self.reset_button = button(tr("New password…"), tr("sandbox-ai web user reset NAME in a terminal: a new "
                                                          "password and TOTP secret; asked first"), self.reset)
        self.remove_button = button(tr("Remove…"), tr("sandbox-ai web user remove NAME: its sessions end; asked "
                                                     "first"), self.remove)
        layout.addWidget(flow(self.refresh_button, self.add_button, self.reset_button, self.remove_button))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.listen = QLineEdit()
        self.listen.setAccessibleName(tr("Listen on"))
        self.listen.setPlaceholderText(backend.WEB_LISTEN)
        self.listen.setToolTip(safe_tooltip(tr("ADDRESS:PORT the service listens on (--listen); empty: {listen}, "
                                               "this host only", listen=backend.WEB_LISTEN)))
        form.addRow(tr("Listen on"), self.listen)
        self.cert = plain_label(role="secondary")
        self.cert.setAccessibleName(tr("Certificate"))
        self.cert.setWordWrap(True)
        self.cert.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(tr("Certificate"), self.cert)
        layout.addLayout(form)
        self.cert_button = button(tr("Show fingerprint"), tr("sandbox-ai web cert: the certificate's SHA-256 "
                                                             "fingerprint, to compare with what the browser shows"),
                                  lambda: self.certificate(False))
        self.renew_button = button(tr("Renew certificate…"), tr("sandbox-ai web cert --renew: a new certificate with "
                                                               "another fingerprint; asked first"),
                                   lambda: self.certificate(True))
        self.vendor_button = button(tr("Fetch xterm.js"), tr("sandbox-ai web vendor in a terminal: the terminals' "
                                                            "xterm.js from registry.npmjs.org, pinned and checked"),
                                    self.vendor)
        self.serve_button = button(tr("Run the service…"), tr("sandbox-ai web serve in a terminal tab: it runs as "
                                                             "long as that terminal does"), self.serve)
        layout.addWidget(flow(self.cert_button, self.renew_button, self.vendor_button, self.serve_button))
        self.status = plain_label(role="secondary")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.set_host(None)

    def label(self):
        return tr("{name} (this machine)", name="local") if self.host == "local" else printable(self.host or "")

    def set_host(self, name, load=False):
        """NAME chosen on the page (None: none); LOAD: ask it for its logins now (the tab is shown)."""
        if name != self.host:
            self.host, self.loaded_for = name, None
            self.users.setRowCount(0)
            self.cert.setText("")
            self.status.setText("")
        for widget in (self.refresh_button, self.add_button, self.cert_button, self.renew_button, self.vendor_button,
                       self.serve_button):
            widget.setEnabled(name is not None)
        self.update_buttons()
        if load and name is not None and self.loaded_for != name:
            self.refresh()

    def chosen(self):
        rows = selected_rows(self.users)
        return row_key(self.users, rows[0]) if rows else None

    def update_buttons(self):
        chosen = self.host is not None and self.chosen() is not None
        for widget in (self.reset_button, self.remove_button):
            set_enabled(widget, chosen, tr("Select a login first."))

    def refresh(self):
        host = self.host
        if host is None:
            return
        self.token += 1
        token, self.loaded_for = self.token, host
        self.status.setText(tr("Asking {host}…", host=self.label()))
        background(self.sai.host_web_users, host, owner=self, source=[self.refresh_button, self.remove_button],
                   timeout=90, done=lambda result, error: self.listed(token, host, result, error))

    def listed(self, token, host, result, error):
        if token != self.token or host != self.host:
            return
        if error is not None:
            self.loaded_for = None
            self.status.setText(tr("{host}: no answer", host=self.label()) + f" ({printable(str(error))[:300]})")
            return
        fill(self.users, [[name, since] for name, since in result])
        self.status.setText(tr("{count} login(s) on {host}.", count=len(result), host=self.label()) if result else
                            tr("No login on {host} yet: Add login… makes the first.", host=self.label()))
        self.update_buttons()

    def ask_name(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, tr("Add login…").rstrip("…"), tr(
            "Name of the login (lowercase letters, digits, '_', '.' or '-', starting with a letter):"))
        name = (name or "").strip()
        if not ok or not name:
            return None
        if not backend.WEB_USER.fullmatch(name):
            warn(self, tr("Add login…").rstrip("…"), tr("A login's name is 1-32 lowercase letters, digits, '_', '.' or "
                                                         "'-', starting with a letter."))
            return None
        return name

    def terminal(self, argv_of, title):
        try:
            argv = argv_of()
        except (OSError, ValueError) as exc:
            warn(self, title, printable(str(exc)))
            return
        self.in_terminal(argv, f"{printable(self.host)}: {title}")
        self.loaded_for = None  # what the terminal changes is read again with Refresh

    def add(self):
        host, name = self.host, self.ask_name()
        if host is None or name is None:
            return
        self.terminal(lambda: self.sai.host_web_argv(host, "add", name), tr("web login {name}", name=name))

    def reset(self):
        host, name = self.host, self.chosen()
        if host is None or name is None:
            return
        if not ask_first(self, "web-user-reset", tr("New password…").rstrip("…"), on_host(host, tr(
                "Give the login {name} a new password and TOTP secret? The old ones stop working.",
                name=printable(name))), tr("New password")):
            return
        self.terminal(lambda: self.sai.host_web_argv(host, "reset", name), tr("web login {name}", name=name))

    def remove(self):
        host, name = self.host, self.chosen()
        if host is None or name is None:
            return
        if not ask_first(self, "web-user-remove", tr("Remove…").rstrip("…"), on_host(host, tr(
                "Remove the login {name} of the web interface? Its sessions end at their next request.",
                name=printable(name))), tr("Remove")):
            return
        background(self.sai.host_web_user_remove, host, name, owner=self, source=[self.remove_button], timeout=90,
                   done=lambda result, error: self.answered(tr("Remove…").rstrip("…"), result, error, again=True))

    def listen_value(self):
        """The address typed (None: the default); ValueError when it is no ADDRESS:PORT."""
        value = self.listen.text().strip()
        if value and backend.web_listen_problem(value):
            raise ValueError(backend.web_listen_problem(value))
        return value or None

    def certificate(self, renew):
        host = self.host
        title = tr("Renew certificate…").rstrip("…") if renew else tr("Certificate")
        try:
            listen = self.listen_value()
        except ValueError as exc:
            warn(self, title, str(exc))
            return
        if renew and not ask_first(self, "web-cert-renew", title, on_host(host, tr(
                "Make a new certificate for the web interface? Its fingerprint changes: every browser that trusted "
                "the old one asks again, and the running service uses it after its restart.")), tr("Renew")):
            return
        background(lambda: self.sai.host_web_cert(host, listen, renew), owner=self,
                   source=[self.cert_button, self.renew_button], timeout=150,
                   done=lambda result, error: self.certified(host, title, result, error))

    def certified(self, host, title, result, error):
        if host != self.host:
            return
        status, out, err = result if error is None else (1, "", str(error))
        if status != 0:
            warn(self, title, printable((err or out).strip()[-1500:]) or tr("Failed (exit status {status}).",
                                                                          status=status))
            return
        self.cert.setText(printable(out.strip()[-600:]))

    def vendor(self):
        if self.host is not None:
            self.terminal(lambda: self.sai.host_web_argv(self.host, "vendor"), tr("web vendor"))

    def serve(self):
        host = self.host
        if host is None:
            return
        title = tr("Run the service…").rstrip("…")
        try:
            listen = self.listen_value()
        except ValueError as exc:
            warn(self, title, str(exc))
            return
        beyond = backend.web_listen_beyond(listen or backend.WEB_LISTEN)
        if not ask_first(self, "web-listen", title, on_host(host, tr(
                "Run the web interface on {listen}? Other machines that reach that address can open its login page "
                "(a password and a TOTP code are still asked).", listen=printable(listen or backend.WEB_LISTEN))), title,
                detail={"beyond": beyond}):
            return
        self.terminal(lambda: self.sai.host_web_argv(host, "serve", listen=listen), tr("web serve"))

    def answered(self, title, result, error, again=False):
        status, out, err = result if error is None else (1, "", str(error))
        text = printable((out + err).strip())[-1500:]
        if status != 0:
            warn(self, title, text or tr("Failed (exit status {status}).", status=status))
        else:
            self.status.setText(text)
        if again:
            self.refresh()


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
            widget.setToolTip(safe_tooltip(tip))
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
        self.update_button = button(tr("Update sandbox-ai"), tr("sandbox-ai update on this host (its own signed "
                                                                "update; sudo for a system installation)"),
                                    self.update_host, tr("Update the host"))
        self.update_all_button = button(tr("Update all"), tr("sandbox-ai update on every host of the list, one after "
                                                             "the other"), self.update_all)
        # hosts update --from-here: this machine's copy instead of the signed release (asked first, like an install)
        self.from_here_button = button(tr("Update from here…"), tr(
            "hosts update --from-here: this machine's sandbox-ai, as the installation the host has (the user's own or "
            "the system's): for a host that cannot reach the release server, or before a release is published"),
            self.update_from_here, tr("Update the host from this machine's copy"))
        self.install_button = button(tr("Install…"), tr("Install sandbox-ai on this host: for the SSH user or every "
                                                        "user, as root, with sudo or without, from this machine's copy "
                                                        "or the signed release (sandbox-ai hosts install)"),
                                     self.install, tr("Install sandbox-ai on the host"))
        # With sudo or not, for Update and Update all (default: as set for each host, hosts set --update-sudo).
        self.update_sudo = QComboBox()
        self.update_sudo.setAccessibleName(tr("Update with sudo"))
        self.update_sudo.setToolTip(tr("Whether the update runs with sudo (a system installation needs it)"))
        for text, value in ((tr("sudo as set for the host"), None), (tr("with sudo"), True),
                            (tr("without sudo"), False)):
            self.update_sudo.addItem(text, value)
        self.versions_button = button(tr("Check versions"), tr("Ask every host for its sandbox-ai version now"),
                                      lambda: self.load_versions(refresh=True))
        compact_combo(self.update_sudo, 18)
        # Rows that wrap when the window is narrow: the page never makes the window wider than the screen.
        for group in ((self.starts_button, self.questions_button, self.refresh_button),
                      (self.add_button, self.edit_button, self.remove_button, self.test_button),
                      (self.update_button, self.from_here_button, self.update_sudo),
                      (self.install_button, self.update_all_button, self.versions_button)):
            left_layout.addWidget(flow(*group))
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
        doctor_tab = QWidget()
        doctor_layout = QVBoxLayout(doctor_tab)
        doctor_layout.setContentsMargins(0, 0, 0, 0)
        doctor_row = QHBoxLayout()
        self.doctor_backend = QComboBox()
        self.doctor_backend.addItems(["podman", "bwrap", "both"])
        self.doctor_backend.setAccessibleName(tr("Backend"))
        self.doctor_backend.setToolTip(tr("Which backend the host is checked for (sandbox-ai doctor --backend)"))
        self.doctor_button = button(tr("Check"), tr("sandbox-ai doctor on the host, for the backend chosen"),
                                    self.check_host, tr("Check the host"))
        doctor_row.addWidget(self.doctor_backend)
        doctor_row.addWidget(self.doctor_button)
        doctor_row.addStretch(1)
        doctor_layout.addLayout(doctor_row)
        doctor_layout.addWidget(self.doctor, 1)
        self.doctor_tab = doctor_tab
        self.tabs.addTab(doctor_tab, tr("Doctor"))
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
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # a long label above its field, not beside it
        self.mode = QComboBox()
        self.mode.setAccessibleName(tr("Security mode"))
        self.telemetry = QComboBox()
        self.telemetry.setAccessibleName(tr("Telemetry"))
        for level, text in (("off", tr("off: no usage statistics, error reports, feedback or update checks "
                                       "(recommended)")),
                            ("strict", tr("strict: also no other calls than the model's")),
                            ("on", tr("on: the agents' own settings"))):
            self.telemetry.addItem(text, level)
        compact_combo(self.telemetry, 22)  # "off: no usage statistics, …" alone was 560 px wide
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
        # The sandboxes' own networks there (--subnet, --subnet6 of every start that names none): empty, sandbox-ai's.
        self.subnet, self.subnet6 = QLineEdit(), QLineEdit()
        for field, example, name in ((self.subnet, "10.77.0.0/24", tr("Subnet (IPv4)")),
                                     (self.subnet6, "fd77::/64", tr("Subnet (IPv6)"))):
            field.setAccessibleName(name)
            field.setPlaceholderText(tr("empty: sandbox-ai's default (e.g. {example})", example=example))
            field.setToolTip(tr("The network of every later start on the host that names none (--subnet, --subnet6); "
                                "a change is asked first"))
        form.addRow(tr("Subnet (IPv4)"), self.subnet)
        form.addRow(tr("Subnet (IPv6)"), self.subnet6)
        self.save_defaults_button = button(tr("Save defaults…"), tr("Write the defaults of every later start on the "
                                                                    "host (sandbox-ai defaults set/unset there); a "
                                                                    "step down in security is asked first"),
                                           self.save_defaults)
        form.addRow(self.save_defaults_button)
        self.defaults_note = plain_label(role="secondary")
        form.addRow(self.defaults_note)
        self.tabs.addTab(defaults, tr("Defaults"))
        # sandbox-ai web there: its logins, certificate, xterm.js and the service (asked when the tab is shown)
        self.web = WebPanel(sai, in_terminal)
        self.tabs.addTab(self.web, tr("Web interface"))
        self.tabs.currentChanged.connect(lambda _index: self.web.set_host(
            self.host(), load=self.tabs.currentWidget() is self.web))
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)
        # An update or an installation (minutes each): a busy bar naming the host, Cancel, and what sandbox-ai says
        # meanwhile in the Updates tab, as it comes.
        self.task = Task(output=self.updates)
        self.task.setAccessibleName(tr("Update or installation running"))
        layout.addWidget(self.task)
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
                           self.update_button, self.from_here_button, self.update_all_button, self.versions_button):
                widget.setEnabled(False)
            return
        if not self.sai.host_update_supported():
            for widget in (self.update_button, self.from_here_button, self.update_all_button, self.versions_button):
                widget.setEnabled(False)
                widget.setToolTip(tr("This sandbox-ai cannot update hosts yet: update it."))
        else:
            try:
                self.versions = self.sai.host_statuses(cached=True)  # what is kept: no SSH here
            except (OSError, ValueError):
                self.versions = {}
        before = [(e.get("name"), e.get("ssh")) for e in getattr(self, "entries", None) or [] if isinstance(e, dict)]
        try:
            self.entries = self.sai.host_entries()
        except (OSError, ValueError) as exc:
            self.status.setText(printable(str(exc)))
            self.entries = []
        after = [(e.get("name"), e.get("ssh")) for e in self.entries if isinstance(e, dict)]
        if getattr(self, "hosts_read", False) and after != before:
            # Added, edited, renamed or removed (here or meanwhile): every host chooser of the app follows at once.
            from .host_choice import hosts_changed
            hosts_changed()
        self.hosts_read = True
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
        for widget in (self.starts_button, self.questions_button, self.test_button, self.doctor_button):
            widget.setEnabled(name is not None)
        # Install… stands out where the host has no sandbox-ai that answers (missing, or not reachable).
        set_enabled(self.install_button, remote and self.sai.host_install_supported(),
                    tr("This machine installs with install.sh (or: sandbox-ai update).") if name == "local" else
                    tr("This sandbox-ai cannot install on hosts yet: update it.") if remote else None)
        set_enabled(self.from_here_button, remote and self.sai.host_update_supported()
                    and self.sai.host_update_from_here_supported(),
                    tr("This machine updates on the page System (or: sandbox-ai update).") if name == "local" else
                    tr("This sandbox-ai cannot update hosts from its own copy yet: update it.") if remote else None)
        info = (getattr(self, "versions", None) or {}).get(name) if remote else None
        missing = remote and isinstance(info, dict) and not info.get("version")
        self.install_button.setProperty("role", "primary" if missing else "")
        self.install_button.style().unpolish(self.install_button)
        self.install_button.style().polish(self.install_button)
        entry = self.entry(name) if remote else {}
        self.update_sudo.setItemText(0, tr("sudo as set for the host ({how})", how=tr("with sudo") if entry.get(
            "update_sudo") else tr("without sudo")) if remote else tr("sudo as set for the host"))
        self.pane_title.setText(printable(self.label(name)) if name else "")
        self.web.set_host(name, load=self.isVisible() and self.tabs.currentWidget() is self.web)
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
        extra = dialog.extra()
        if extra["command"] and not backend.valid_directory(extra["command"]):
            warn(self, tr("Add a host"), tr("sandbox-ai's path there is an absolute path, e.g. "
                                            "/opt/sandbox-ai/bin/sandbox-ai."))
            return

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
        background(lambda: self.sai.host_add(name, ssh, port, identity, description, **extra), owner=self,
                   source=self.add_button, timeout=60, done=done)

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
        if not ask_first(self, "host-edit", tr("Edit {host}", host=name), tr(
                "Update sandbox-ai on {host} with sudo from now on?", host=self.label(name)) if sudo else tr(
                "Update sandbox-ai on {host} without sudo from now on?", host=self.label(name)), tr("Change")):
            return
        background(self.sai.host_set, name, sudo, owner=self, source=self.edit_button, timeout=60,
                   done=lambda result, error: warn(self, tr("Edit {host}", host=name), printable(str(error)))
                   if error else self.load_hosts(name))

    def remove(self):
        name = self.host()
        if name in (None, "local"):
            return
        if not ask_first(self, "host-remove", tr("Remove host"), tr(
                "Remove {host} from the list of hosts? Its sandbox-ai and its starts stay there; this app and sandbox-ai "
                "web no longer reach it (its approvals are no longer answered from here).", host=self.label(name)),
                tr("Remove")):
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

    def check_host(self):
        """sandbox-ai doctor --backend B --json on the chosen host (this machine too), shown on the Doctor tab."""
        name = self.host()
        if name is None:
            return
        engine = self.doctor_backend.currentText()
        self.status.setText(tr("Checking {host} for {backend}…", host=name, backend=engine))

        def done(result, error):
            if error:
                self.status.setText(printable(f"{name}: {error}"))
                return
            self.status.setText(tr("{host}: checked for {backend}", host=name, backend=engine))
            self.show_doctor(result)
        background(self.sai.host_doctor, name, engine, owner=self, source=self.doctor_button, timeout=330, done=done)

    # -- the chosen host's defaults --

    def set_defaults_enabled(self, enabled):
        for widget in (self.mode, self.canaries, self.canary_list, self.keep, self.keep_days, self.telemetry,
                       self.subnet, self.subnet6, self.save_defaults_button):
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
        self.canary_default = default  # what "sandbox-ai's default" plants there: what fewer canaries are counted from
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
        for key, field in (("subnet", self.subnet), ("subnet6", self.subnet6)):
            field.setText(printable(values[key]) if isinstance(values.get(key), str) else "")
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
        for key, field, version in (("subnet", self.subnet, 4), ("subnet6", self.subnet6, 6)):
            text = field.text().strip()
            if text:
                chosen[key] = subnet_value(text, version)
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
        old = {key: value for key, value in self.defaults.items() if key in EDITED_DEFAULTS}
        steps = [("set", key, value) for key, value in chosen.items() if old.get(key, object()) != value] + \
                [("unset", key, None) for key in old if key not in chosen]
        if not steps:
            self.defaults_note.setText(tr("Nothing changed."))
            return
        canary_default = getattr(self, "canary_default", None)
        lower = security_steps_down(old, chosen, canary_default)
        if not ask_first(self, "defaults-lower", tr("Defaults of {host}", host=self.label(name)), tr(
                "Every later start on {host} gets less protection: {changes}. Save anyway?", host=self.label(name),
                changes="; ".join(lower)), tr("Save"), detail={"old": old, "new": chosen,
                                                               "canary_default": canary_default}):
            return
        argv = [("defaults", "set", key, json.dumps(value)) if op == "set" else ("defaults", "unset", key)
                for op, key, value in steps]

        def work():
            """Each change in turn; the first one refused stops it: (applied, refused step or None, its words)."""
            applied = []
            for step, args in zip(steps, argv):
                status, out, err = self.sai.host_run(name, *args, timeout=60)
                if status != 0:
                    return applied, step, (err or out).strip()
                applied.append(step)
            return applied, None, ""

        def done(result, error):
            if error is not None:  # not known what was applied: the defaults read again say it
                warn(self, tr("Defaults"), tr("Not known how much was saved on {host}: {error}. The values read "
                                              "again show what it has now.", host=self.label(name),
                                              error=printable(str(error))), modal=False)
            else:
                applied, refused, said = result
                if refused is not None:
                    warn(self, tr("Defaults"), tr(
                        "Saved on {host}: {applied}. Refused: {refused} ({reason}). Not tried: {rest}.",
                        host=self.label(name), applied=", ".join(default_step(*s) for s in applied) or tr("nothing"),
                        refused=default_step(*refused), reason=printable(said)[-500:] or "?",
                        rest=", ".join(default_step(*s) for s in steps[len(applied) + 1:]) or tr("nothing")),
                        modal=False)
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

    def busy_updating(self):
        """True (and said) while an update or installation runs: one at a time."""
        if self.task.running:
            self.status.setText(tr("An update or installation is running: wait for it, or cancel it."))
            return True
        return False

    def update_host(self):
        name = self.host()
        if name is None or self.busy_updating():
            return
        if name == "local":
            warn(self, tr("Update"), tr("This machine updates on the page System (or: sandbox-ai update)."))
            return
        self.results, self.queue = [], [name]
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.queue_sudo, self.queue_from_here = self.update_sudo.currentData(), False
        self.next_update()

    def update_all(self):
        names = [entry["name"] for entry in self.entries if is_remote(entry)]
        if not names or self.busy_updating():
            return
        self.results, self.queue = [], list(names)
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.queue_sudo, self.queue_from_here = self.update_sudo.currentData(), False
        self.next_update()

    def ask_from_here(self, name, why=""):
        """True when the human says yes to this machine's copy on host NAME (as an install is asked: host-install)."""
        text = tr("Put this machine's sandbox-ai on {host}, as the installation it has there? It runs there with the "
                  "rights of that installation.", host=self.label(name))
        return ask_first(self, "host-install", tr("Update from this machine's copy"), f"{why}\n\n{text}" if why else
                         text, tr("Update from here"))

    def update_from_here(self):
        """`sandbox-ai hosts update NAME --from-here`: this machine's copy on the chosen host, as the same kind of
        installation (sudo as chosen beside Update), after a question naming the host."""
        name = self.host()
        if name is None or self.busy_updating():
            return
        if name == "local":
            warn(self, tr("Update"), tr("This machine updates on the page System (or: sandbox-ai update)."))
            return
        if not self.sai.host_update_from_here_supported() or not self.ask_from_here(name):
            return
        self.results, self.queue = [], [name]
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.queue_sudo, self.queue_from_here = self.update_sudo.currentData(), True
        self.next_update()

    def next_update(self, sudo=None):
        """The next host of the queue, one after the other (each may take minutes); the summary at the end."""
        if not self.queue:
            return self.updates_done()
        sudo = getattr(self, "queue_sudo", None) if sudo is None else sudo  # the choice beside Update
        name, total = self.queue[0], len(self.results) + len(self.queue)
        self.status.setText(tr("Updating {host} ({index} of {count})…", host=name, index=len(self.results) + 1,
                               count=total))
        from_here = [True] if getattr(self, "queue_from_here", False) else []  # host_update's FROM_HERE
        self.task.call_watched(self.sai, lambda: self.sai.host_update(name, sudo, *from_here),
                               lambda result, error: self.updated(name, result, error),
                               tr("Updating {host} ({index} of {count})…", host=name, index=len(self.results) + 1,
                                  count=total),
                               source=[self.update_button, self.from_here_button, self.update_all_button,
                                       self.install_button], errors_only=True)

    def updated(self, name, result, error):
        if self.task.cancelled:  # Cancel: this host's update was ended, the rest of the queue does not run
            self.updates.appendPlainText(printable(tr("{host}: cancelled", host=name)))
            self.results.append({"name": name, "error": tr("cancelled")})
            self.queue = []
            return self.updates_done()
        if error:
            result = {"name": name, "error": str(error)}
        if result.get("needs_sudo") and len(self.results) + len(self.queue) == 1 and confirm(
                self, tr("Update"), tr("The installation on {host} belongs to root. Update it with sudo?", host=name),
                tr("Update with sudo")):
            return self.next_update(sudo=True)
        from_here, shown = getattr(self, "queue_from_here", False), False
        if not from_here and "no published release" in str(result.get("error") or "") and \
                len(self.results) + len(self.queue) == 1 and self.sai.host_update_from_here_supported():
            # Nothing to download (none published yet, or the host cannot reach the release server): offered at once.
            self.updates.appendPlainText(printable(update_text(name, result)))
            shown = True
            if self.ask_from_here(name, tr("No release of sandbox-ai is published for {host} to download.",
                                           host=self.label(name))):
                self.queue_from_here = True
                return self.next_update()
        self.queue.pop(0)
        self.results.append(result)
        if not shown:
            self.updates.appendPlainText(printable(update_text(name, result)))
        if result.get("needs_password"):
            # sudo there wants a password: the host's update in a terminal tab, where sudo asks for it (this machine's
            # copy: its install, as the kind of installation the host has).
            try:
                info = (getattr(self, "versions", None) or {}).get(name) or {}
                mode = "system" if result.get("kind") == "system" else "user"
                argv = self.sai.host_install_terminal(name, mode, info.get("prefix") if mode == "system" else None,
                                                      True, "here") if from_here else \
                    self.sai.host_update_terminal(name)
                self.in_terminal(argv, tr("{host}: update", host=name))
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

    # -- installing sandbox-ai on a host --

    def install(self):
        """`sandbox-ai hosts install NAME ...` on this machine (up to a quarter of an hour, in the background) after
        the choice and a question naming the host, where and as whom; sudo that wants a password: the same in a
        terminal tab, where it asks."""
        name = self.host()
        if name in (None, "local") or not self.sai.host_install_supported() or self.busy_updating():
            return
        try:
            version = self.sai.version()
        except Exception:  # noqa: BLE001 - only shown
            version = ""
        dialog = InstallDialog(self.entry(name), version, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.sai.host_install_args(name, **values)
        except ValueError as exc:
            warn(self, tr("Install"), printable(str(exc)))
            return
        if not ask_first(self, "host-install", tr("Install sandbox-ai on {host}", host=name), tr(
                "Install sandbox-ai on {host} {how}? An installation there is replaced.", host=self.label(name),
                how=dialog.words()), tr("Install")):
            return
        self.tabs.setCurrentWidget(self.updates)
        self.updates.appendPlainText(printable(tr("{host}: installing {how}…", host=name, how=dialog.words())))
        self.status.setText(tr("Installing sandbox-ai on {host}…", host=name))
        self.task.call_watched(self.sai, lambda: self.sai.host_install(name, **values),
                               lambda result, error: self.installed(name, values, result, error),
                               tr("Installing sandbox-ai on {host}…", host=name),
                               source=[self.install_button, self.update_button, self.from_here_button,
                                       self.update_all_button], errors_only=True)

    def installed(self, name, values, result, error):
        if self.task.cancelled:
            text = tr("{host}: cancelled", host=name)
            self.updates.appendPlainText(printable(text))
            self.status.setText(text)
            self.load_versions(refresh=True)
            return
        if error is not None:
            result = {"name": name, "error": str(error)}
        text = install_text(name, result)
        self.updates.appendPlainText(text)
        self.status.setText(text.splitlines()[0])
        if result.get("needs_password"):
            # sudo there wants a password: the same installation in a terminal tab, where sudo asks for it.
            try:
                self.in_terminal(self.sai.host_install_terminal(name, **values), tr("{host}: install", host=name))
                self.updates.appendPlainText(printable(tr("  {host}: the installation runs in a terminal tab, where "
                                                          "sudo asks for the password", host=name)))
            except (OSError, ValueError) as exc:
                self.updates.appendPlainText(printable(f"  {exc}"))
        else:
            (warn if result.get("error") else inform)(self, tr("Install sandbox-ai on {host}", host=name), text,
                                                       details=printable(result.get("output") or "") or None,
                                                       modal=False)
        self.load_versions(refresh=True)

    def show_doctor(self, results):
        """The host's doctor (--json: a list of checks; an older one's text output as a string)."""
        if isinstance(results, str):
            self.doctor.set_text(results)
        else:
            self.doctor.set_results([r for r in results if isinstance(r, dict)])
        self.tabs.setCurrentWidget(self.doctor_tab)
