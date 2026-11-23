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
can read private (ssh refuses it) and say so. After an update the host's version is read again at once (hosts update
keeps it): the column never goes empty.

A host may have several installations (the user's own, a system one): the column names the one the host's commands run
(the user's own first, as a terminal there finds it) and the others; Update asks which when there are two. Uninstall…
(hosts uninstall) removes the one chosen with its own sandbox-ai uninstall there, after its table of what goes, with
--purge, --secrets and --mcp-data as choices (unticked); the host stays in the list unless ticked. This machine's row
names its own version and is never uninstalled from here.

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

from . import applog
from . import backend, theme, widgets
from .files import printable, printable_text
from .host_choice import on_host
from .i18n import tr
from .widgets import (
    MONO, Task, ask_first, background, compact_combo, confirm, explanation, fill, flow, inform, multi_select,
    plain_label, report_each, row_key, run_each, safe_tooltip, selected_rows, set_enabled, several, table, warn,
)

# Security modes in rising strictness (sandbox-ai's modes.NAMES) and its default when defaults.json names none; the
# orders of protection are backend's (the web interface's): one policy for what counts as a step down.
MODES = backend.MODE_ORDER
DEFAULT_MODE = backend.DEFAULT_MODE
DEFAULT_CANARIES = backend.DEFAULT_CANARIES  # canary.DEFAULT_CATEGORIES, when the host does not say
CANARY_LIST = "list"  # the canaries choice "these categories"
HOSTS_LIST_WIDTH, HOSTS_TABS_WIDTH = 550, 650  # px the hosts list and its tabs ask for (shared out pro rata)


def is_remote(entry):
    """A host of the list other than this machine (as SandboxAI.remote_hosts counts them)."""
    return entry.get("kind") != "local" and entry.get("name") != "local"


UPDATE_MODES = backend.UPDATE_MODES


def kind_text(kind):
    """A kind of installation in words: user, system (translated), pip, pipx, package, checkout as they are."""
    if kind == "user":
        return tr("user")
    if kind == "system":
        return tr("system")
    return printable(kind) if kind in ("pip", "pipx", "package", "checkout") else tr("unknown")


def installations_of(info):
    """The installations a host's status names (hosts list: user, system, pip, ...), each a dict; [] when not read."""
    found = (info or {}).get("installations")
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def others_text(info):
    """The host's other installations beside the one its commands run ("; system 0.1.236"), and a warning when a
    terminal there runs another one than the app's commands (user report 2026-11-23, coding-101)."""
    items = installations_of(info)
    text = "".join("; " + tr("{kind} {version}", kind=kind_text(item.get("kind")),
                             version=printable(item.get("version") or "?"))
                   for item in items if not item.get("commands"))
    if info and info.get("differ"):
        terminal = next((item for item in items if item.get("terminal")), {})
        commands = next((item for item in items if item.get("commands")), {})
        text += "; " + tr("a terminal there runs {terminal}, the app's commands {commands}",
                          terminal=tr("{kind} {version}", kind=kind_text(terminal.get("kind")),
                                      version=printable(terminal.get("version") or "?")),
                          commands=tr("{kind} {version}", kind=kind_text(commands.get("kind")),
                                      version=printable(commands.get("version") or info.get("version") or "?")))
    return text


def version_text(info):
    """A host's sandbox-ai in a few words: its version and whether an update is there ("" when not asked yet), the
    kind when it has several installations, and the others."""
    if not info:
        return ""
    others = others_text(info)
    if not info.get("version"):
        if info.get("missing"):
            return tr("not installed") + others
        return tr("unreachable") if info.get("error") else ""
    # its own update mode (sandbox-ai update mode; 0.1.122 on, read from its version --verbose)
    mode = "; " + tr("updates: {mode}", mode=info["update_mode"]) if info.get("update_mode") in UPDATE_MODES else ""
    if info.get("kind") in ("checkout", "pip", "package"):
        return f"{info['version']} ({info['kind']})" + mode + others
    version = f"{info['version']} ({kind_text(info['kind'])})" if others and info.get("kind") in ("user", "system") \
        else info["version"]
    if info.get("update"):
        return tr("{version}, update: {available}", version=version,
                  available=info.get("available") or "?") + mode + others
    return (tr("{version}, up to date", version=version) if info.get("update") is False
            else version) + mode + others


def uninstall_text(name, result):
    """One host's uninstall in a line or two."""
    where = result.get("installation") or {}
    what = tr("{kind} installation {version}", kind=kind_text(where.get("kind")),
              version=printable(where.get("version") or "?"))
    if result.get("error"):
        lines = [tr("{host}: not removed: {error}", host=name, error=printable(result["error"]))]
    else:
        lines = [tr("{host}: removed the {what}", host=name, what=what)]
        if result.get("removed_from_list"):
            lines.append("  " + tr("{host} is no longer in the list", host=name))
        elif result.get("remaining") is not None:
            left = ", ".join(tr("{kind} {version}", kind=kind_text(item.get("kind")),
                                version=printable(item.get("version") or "?")) for item in result["remaining"])
            lines.append("  " + (tr("there now: {left}", left=left) if left else
                                 tr("no sandbox-ai is left there (install it again: Install…)")))
    return "\n".join([*lines, *("  " + printable(note) for note in result.get("notes") or [])])


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
    where = result.get("installation") or {}
    if where.get("kind") in ("user", "system") and result.get("after"):
        # (which one: a host may have two)
        line += " (" + tr("{kind} installation", kind=kind_text(where["kind"])) + ")"
    return "\n".join([line, *("  " + text for text in key_lines(result)),
                      *("  " + note for note in result.get("notes") or [])])


def storage_cell(found, on=True):
    """A host's Storage cell: what its last storage check found to free, "no answer", "off", or "" (not yet)."""
    if not on:
        return tr("off")
    if not isinstance(found, dict):
        return ""
    if found.get("error"):
        return tr("no answer")
    from .storage_view import size_text
    return tr("{size} to free", size=size_text(found.get("total") or 0))


def storage_words(found, now=None):
    """What the last storage check of a host said, in a sentence (its tooltip, the line below the list)."""
    import time
    if not isinstance(found, dict) or not isinstance(found.get("time"), (int, float)):
        return tr("not checked yet")
    ago = widgets.ago((time.time() if now is None else now) - found["time"])
    if found.get("error"):
        return tr("no answer {ago}: {error}", ago=ago, error=printable(found["error"])[:300])
    from .storage_view import size_text
    text = tr("{ago}: {size} could be freed", ago=ago, size=size_text(found.get("total") or 0))
    if isinstance(found.get("threshold"), int):
        text += " " + (tr("(above the threshold of {size})", size=size_text(found["threshold"])) if found.get("above")
                       else tr("(below the threshold of {size})", size=size_text(found["threshold"])))
    grew = [e for e in found.get("grew") or [] if isinstance(e, dict)]
    if grew:
        text += "; " + tr("grew: {starts}", starts=", ".join(
            f"{printable(str(e.get('name')))} +{size_text(e.get('bytes') or 0)}" for e in grew))
    if found.get("said"):
        text += "; " + tr("the storage rules acted {count} time(s)", count=found["said"])
    return text


def reach_text(info, now=None):
    """Whether a host answered (the Sandboxes page's last look, {"seen", "problem"}), in a few words."""
    import time
    now = time.time() if now is None else now
    if not info:
        return tr("not asked yet")
    seen = info.get("seen")
    if info.get("problem"):
        return tr("no answer (last seen {ago})", ago=widgets.ago(now - seen)) if seen else tr("no answer")
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
        form.addRow(explanation(tr("To change the address, the port or the key: remove the host and add it again (its "
                                   "host key is learnt anew).")))
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


class UninstallDialog(QDialog):
    """Uninstall… of a host: which installation (the user's own or the system one, when it has both), whether also
    the SSH user's data (--purge), with the secrets and logins (--secrets, which cannot be recovered) and the MCP
    servers' own folders (--mcp-data), and whether the host leaves the list afterwards - with the host's own table of
    what its uninstall removes (its --dry-run), asked again at each change. Nothing is ticked at first."""

    def __init__(self, name, items, plan, parent=None):
        """ITEMS: the host's install.sh installations (its status: installations with an executable); PLAN(values,
        done): asks the host's table in the background, DONE(text) shows it."""
        super().__init__(parent)
        self.name, self.plan, self.asked = name, plan, 0
        self.setWindowTitle(tr("Uninstall sandbox-ai on {host}", host=printable(name)))
        form = QFormLayout(self)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.which = QButtonGroup(self)
        self.choices = []
        for item in items:
            words = tr("{kind} installation {version} ({path})", kind=kind_text(item.get("kind")),
                       version=printable(item.get("version") or "?"), path=printable(item.get("path") or "?"))
            if item.get("terminal"):
                words += " - " + tr("a terminal there runs it")
            radio = QRadioButton(words)
            radio.setAccessibleName(tr("Uninstall the {kind} installation", kind=kind_text(item.get("kind"))))
            self.which.addButton(radio)
            self.choices.append((radio, item))
            form.addRow(tr("Which") if len(self.choices) == 1 else "", radio)
            radio.toggled.connect(lambda on, _=None: on and self.changed())
        if len(self.choices) == 1:
            self.choices[0][0].setChecked(True)
        self.purge = QCheckBox(tr("Also the SSH user's sandbox-ai data there (kept starts, logs, kits, settings, "
                                  "caches, its Podman images)"))
        self.secrets = QCheckBox(tr("With it, also the secrets and logins there - they cannot be recovered"))
        self.mcp_data = QCheckBox(tr("With it, also the MCP servers' own folders there"))
        self.remove = QCheckBox(tr("Remove {host} from the list afterwards", host=printable(name)))
        self.remove.setToolTip(tr("Unticked, the host stays in the list: install sandbox-ai there again with "
                                  "Install… or Update from here…"))
        for box in (self.purge, self.secrets, self.mcp_data, self.remove):
            box.setAccessibleName(box.text())
            form.addRow("", box)
        for box in (self.purge, self.secrets, self.mcp_data):
            box.toggled.connect(lambda _=False: self.changed())
        self.table = QPlainTextEdit()
        self.table.setReadOnly(True)
        self.table.setFont(MONO)
        self.table.setAccessibleName(tr("What its uninstall removes there"))
        self.table.setMinimumHeight(160)
        form.addRow(tr("What goes"), self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.addButton(tr("Uninstall"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setProperty("role", "danger")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        form.addRow(buttons)
        self.changed()

    def chosen(self):
        return next((item for radio, item in self.choices if radio.isChecked()), None)

    def values(self):
        """{installation, purge, secrets, mcp_data, remove} for SandboxAI.host_uninstall."""
        item = self.chosen() or {}
        purge = self.purge.isChecked()
        return {"installation": item.get("kind") if item.get("kind") in ("user", "system") else None,
                "purge": purge, "secrets": purge and self.secrets.isChecked(),
                "mcp_data": purge and self.mcp_data.isChecked(), "remove": self.remove.isChecked()}

    def changed(self):
        self.secrets.setEnabled(self.purge.isChecked())
        self.mcp_data.setEnabled(self.purge.isChecked())
        chosen = self.chosen() is not None or not self.choices
        self.ok.setEnabled(False)
        if not chosen:
            self.table.setPlainText(tr("Choose the installation to remove."))
            return
        self.asked += 1
        asked = self.asked
        self.table.setPlainText(tr("Asking {host} what its uninstall would remove…", host=printable(self.name)))
        values = {key: value for key, value in self.values().items() if key != "remove"}
        self.plan(values, lambda text, ok: asked == self.asked and self.planned(text, ok))

    def planned(self, text, ok):
        self.table.setPlainText(printable_text(text))
        self.ok.setEnabled(bool(ok))


class AddHostDialog(QDialog):
    def __init__(self, parent=None, draft=None):
        """DRAFT: the key of what was typed and not added (drafts.py): filled in again."""
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
        form.addRow(explanation(tr("Your user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed "
                                   "there. The host key is learnt at the first contact and checked from then on.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        from . import drafts
        self.draft = None if draft is None else drafts.Draft(self, draft, {
            "name": self.name, "ssh": self.ssh, "port": self.port, "identity": self.identity,
            "description": self.description, "command": self.command, "update_sudo": self.update_sudo})

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
        layout.addWidget(explanation(tr("The host's web interface (sandbox-ai web there): who may log in, its "
                                        "certificate and the service itself. Passwords and TOTP codes are typed in a "
                                        "terminal, never through the app.")))
        self.users = table([tr("Login"), tr("Since")], stretch=0, compact=(1,))
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
        widgets.list_menu(self.users, lambda: [self.reset_button, self.remove_button, None, self.add_button,
                                               self.refresh_button])
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
        # the address to open, once the service runs (through an SSH tunnel for another host's loopback)
        self.open_button = button(tr("Open in the browser"), tr("The web interface's address in your browser"),
                                  self.open_service)
        self.open_button.setVisible(False)
        self.service_url = None
        layout.addWidget(flow(self.cert_button, self.renew_button, self.vendor_button, self.serve_button,
                              self.open_button))
        self.status = plain_label(role="secondary", selectable=True, log="hosts")
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
            self.show_service(None, "")  # the address of the host chosen before is not this one's
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
            # The logins are not known now: none of the old rows (nor Remove or New password for one) stay on screen
            # as if they were current (review 6).
            self.loaded_for = None
            fill(self.users, [])
            self.status.setText(tr("{host}: no answer", host=self.label()) + f" ({printable_text(str(error))[:300]})")
            self.update_buttons()
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
            warn(self, title, printable_text(str(exc)))
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
        background(self.sai.host_web_user_remove, host, name, owner=self, work=True, source=[self.remove_button],
                   timeout=90,
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
        background(lambda: self.sai.host_web_cert(host, listen, renew), owner=self, work=True,
                   source=[self.cert_button, self.renew_button], timeout=150,
                   done=lambda result, error: self.certified(host, title, result, error))

    def certified(self, host, title, result, error):
        if host != self.host:
            return
        status, out, err = result if error is None else (1, "", str(error))
        if status != 0:
            warn(self, title, printable_text((err or out).strip()[-1500:]) or tr("Failed (exit status {status}).",
                                                                          status=status))
            return
        self.cert.setText(printable_text(out.strip()[-600:]))

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
        # asked only beyond the loopback (on the loopback only that host itself reaches it: nothing to ask)
        if not ask_first(self, "web-listen", title, on_host(host, tr(
                "Run the web interface on {listen}? Other machines that reach that address can open its login page "
                "(a password and a TOTP code are still asked).", listen=printable(listen or backend.WEB_LISTEN))), title,
                detail={"beyond": beyond}):
            return
        self.terminal(lambda: self.sai.host_web_argv(host, "serve", listen=listen), tr("web serve"))
        self.say_where(host, listen or backend.WEB_LISTEN, beyond)

    def say_where(self, host, listen, beyond):
        """Where the service just started can be opened, and what it takes: another host's loopback through an SSH
        tunnel the app starts in a terminal tab of its own (review 6 N4: the URL the service prints is that host's
        localhost, which in this machine's browser is this machine)."""
        fingerprint = tr("Compare the certificate's fingerprint (Show fingerprint) with what the browser shows.")
        if host == "local" or beyond:
            entry = {} if host == "local" else next(
                (e for e in self.sai.host_entries() if isinstance(e, dict) and e.get("name") == host), {})
            url = backend.web_service_url(entry.get("ssh"), listen, local=host == "local")
            where = tr("Open {url} in a browser, as long as the terminal tab \"web serve\" runs.", url=url) \
                if host == "local" else tr("It listens on {listen} of {host}: open {url} from a machine that reaches "
                                           "that address, as long as the terminal tab \"web serve\" runs.",
                                           listen=printable(listen), host=printable(host), url=url)
            self.show_service(url, where + " " + fingerprint)
            return
        try:
            argv, port = self.sai.host_web_tunnel_argv(host, listen)
        except (OSError, ValueError) as exc:
            self.show_service(None, tr("It listens on {listen} of {host}: only {host} itself reaches it. Forward it "
                                       "with: ssh -N -L 8443:{listen} {host} - then open https://localhost:8443/. "
                                       "(No tunnel from here: {error})", listen=printable(listen),
                                       host=printable(host), error=printable_text(str(exc))))
            return
        self.terminal(lambda: argv, tr("web tunnel"))
        url = f"https://localhost:{port}/"
        self.show_service(url, tr("It listens on {listen} of {host}, which only {host} itself reaches: the terminal "
                                  "tab \"web tunnel\" forwards it through SSH. Open {url} on this machine, as long as "
                                  "both tabs (\"web serve\" and \"web tunnel\") run.", listen=printable(listen),
                                  host=printable(host), url=url) + " " + fingerprint)

    def show_service(self, url, text):
        self.service_url = url
        self.status.setText(text)
        self.open_button.setVisible(url is not None)

    def open_service(self):
        if self.service_url:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(self.service_url))

    def answered(self, title, result, error, again=False):
        status, out, err = result if error is None else (1, "", str(error))
        text = printable_text((out + err).strip())[-1500:]
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
        layout.addWidget(explanation(tr("Other machines whose sandbox-ai this app manages over SSH (the web "
                                        "interface, sandbox-ai web, uses the same list).")))
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.table = table([tr("Host"), tr("SSH"), tr("Answers"), tr("sandbox-ai"), tr("Running"), tr("Questions"),
                            tr("Storage")], stretch=0, compact=(2, 3, 4, 5, 6))
        self.table.setAccessibleName(tr("Hosts"))
        multi_select(self.table)  # several at once (Ctrl, Shift): Remove… takes them all, the rest acts on one
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
        # The storage check of the host (sandbox-ai gc --check there): what could be freed, ticked and applied
        self.optimize_button = button(tr("Optimize…"), tr(
            "What could be freed on this host - each with how much, its command and whether it is safe while starts "
            "run; tick and apply, asked first (sandbox-ai gc --check)"), self.optimize, tr("Optimize the host"))
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
        # sandbox-ai hosts uninstall: the host's own sandbox-ai uninstall of the installation chosen (asked first, with
        # its own table of what goes); the host stays in the list unless ticked
        self.uninstall_button = button(tr("Uninstall…"), tr(
            "Remove sandbox-ai on this host - the user's own installation or the system one, when it has both - "
            "with its own sandbox-ai uninstall there, after its list of what goes (sandbox-ai hosts uninstall)"),
            self.uninstall, tr("Uninstall sandbox-ai on the host"))
        # With sudo or not, for Update and Update all (default: as set for each host, hosts set --update-sudo).
        self.update_sudo = QComboBox()
        self.update_sudo.setAccessibleName(tr("Update with sudo"))
        self.update_sudo.setToolTip(tr("Whether the update runs with sudo (a system installation needs it)"))
        for text, value in ((tr("sudo as set for the host"), None), (tr("with sudo"), True),
                            (tr("without sudo"), False)):
            self.update_sudo.addItem(text, value)
        self.versions_button = button(tr("Check versions"), tr("Ask every host for its sandbox-ai version now"),
                                      lambda: self.load_versions(refresh=True))
        # sandbox-ai hosts update NAME --mode MODE: the host's own update mode (asked first: update-mode)
        self.update_mode_button = button(tr("Update mode…"), tr(
            "What the host's sandbox-ai does by itself: off, notify (announce a newer signed release), download "
            "(also fetch and check it), auto (also install it in its window while nothing runs) - sandbox-ai update "
            "mode there"), self.set_update_mode, tr("Set the host's update mode"))
        compact_combo(self.update_sudo, 24)  # (18 cut "sudo wie für den Host eingestellt" short)
        # Rows that wrap when the window is narrow: the page never makes the window wider than the screen.
        for group in ((self.starts_button, self.questions_button, self.refresh_button),
                      (self.add_button, self.edit_button, self.remove_button, self.test_button,
                       self.optimize_button),
                      (self.update_button, self.from_here_button, self.update_sudo, self.update_mode_button),
                      (self.install_button, self.uninstall_button, self.update_all_button, self.versions_button)):
            left_layout.addWidget(flow(*group))
        # The app's storage check of the host (storage_hosts.py): on or off for it, and what its last one said
        self.storage_box = QCheckBox(tr("Check its storage in the background"))
        self.storage_box.setAccessibleName(tr("Check the host's storage in the background"))
        self.storage_box.setToolTip(safe_tooltip(tr(
            "The app runs the storage check on this host (sandbox-ai gc --check there, over SSH) in the background, "
            "as for this machine: at its start and then as the host's own Storage setting says (weekly by default); "
            "it applies the host's storage rules and says above the threshold what could be freed there. On by "
            "default for the hosts the app uses (the Sandboxes page found starts there).")))
        self.storage_box.toggled.connect(self.storage_toggled)
        self.storage_note = plain_label(role="secondary", selectable=True)
        self.storage_note.setAccessibleName(tr("The host's last storage check"))
        left_layout.addWidget(flow(self.storage_box))
        left_layout.addWidget(self.storage_note)
        self.storage_checked, self.storage_used = {}, lambda: []
        widgets.list_menu(self.table, lambda: [
            self.starts_button, self.questions_button, None, self.edit_button, self.test_button, self.optimize_button,
            self.update_button, self.from_here_button, self.update_mode_button, self.install_button,
            self.uninstall_button, None,
            (tr("Stop all starts on this host…"), self.host() not in (None, "local"), tr("Choose a host first"),
             self.stop_all), self.remove_button, None,
            self.add_button, self.update_all_button, self.versions_button, self.refresh_button])
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.pane_title = plain_label(role="section")
        right_layout.addWidget(self.pane_title)
        self.tabs = QTabWidget()
        widgets.arrow_tabs(self.tabs)  # (the app's tab row: arrows at its end when the tabs do not fit)
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
                            ("strict", tr("strict: also no other calls than the model's"))):  # never on
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
        self.keep.setToolTip(tr("Unchecked: kept starts never expire (keep_days 0). Without keep_days on the host they "
                                "go after 90 days unused (sandbox-ai's default: Clean up… and the daily pass)"))
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
        form.addRow(tr("Telemetry of the agents (always off)"), self.telemetry)
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
        # The list gets a share of its own, about as wide as the host's tabs beside it (review 6: at 1280 px it was
        # 255 px wide and showed only "Host, SSH, An…"); both grow alike with the window.
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([HOSTS_LIST_WIDTH, HOSTS_TABS_WIDTH])
        self.split = split
        layout.addWidget(split, 1)
        # An update or an installation (minutes each): a busy bar naming the host, Cancel, and what sandbox-ai says
        # meanwhile in the Updates tab, as it comes.
        self.task = Task(output=self.updates)
        self.task.setAccessibleName(tr("Update or installation running"))
        layout.addWidget(self.task)
        self.status = plain_label(role="secondary", selectable=True, log="hosts")
        layout.addWidget(self.status)
        self.entries, self.versions, self.queue, self.results = [], {}, [], []
        self.fleet, self.question_counts = {}, {}
        self.defaults, self.defaults_host, self.defaults_token = None, None, 0
        self.set_defaults_enabled(False)
        self.load_hosts()
        from .host_choice import follow_hosts
        # changed elsewhere (sandbox-ai hosts, the web interface: the hosts file is watched): the list follows too
        follow_hosts(self, lambda: self.load_hosts(select=self.host(), announce=False))

    # -- the list --

    def optimize(self):
        """Optimize…: the storage check's plan of the selected host (storage_view: gc --check there)."""
        name = self.host()
        if name is None:
            return
        from .storage_view import open_dialog
        open_dialog(self, self.sai, name, owner=self)

    def host(self):
        rows = selected_rows(self.table)
        return row_key(self.table, rows[0]) if rows else None

    def hosts(self):
        """The hosts selected, top to bottom."""
        return [row_key(self.table, row) for row in selected_rows(self.table)]

    def entry(self, name):
        return next((e for e in self.entries if e.get("name") == name), {})

    def load_hosts(self, select=None, announce=True):
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
            self.status.setText(printable_text(str(exc)))
            self.entries = []
        after = [(e.get("name"), e.get("ssh")) for e in self.entries if isinstance(e, dict)]
        if announce and getattr(self, "hosts_read", False) and after != before:
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
        from . import storage_hosts
        settings, used = backend.app_settings(), self.storage_used()  # (read once per look, not per row)
        for entry in self.entries:
            name = entry["name"]
            local = entry.get("kind") == "local" or name == "local"
            info = self.fleet.get(name)
            count = info.get("running") if info else None
            questions = self.question_counts.get(name)
            rows.append([tr("{name} (this machine)", name=printable(name)) if local else printable(name),
                         "" if local else printable(f"{entry.get('ssh', '')}:{entry.get('port', 22)}"),
                         tr("yes") if local else reach_text(info),
                         self.own_version() if local else version_text(self.versions.get(name)),
                         "" if count is None else str(count), "" if questions is None else str(questions),
                         "" if local else storage_cell(self.storage_checked.get(name),
                                                       storage_hosts.is_on(name, used, settings))])
            colors.append("bad" if info and info.get("problem") else "warn" if questions or (
                not local and (self.storage_checked.get(name) or {}).get("above")) else None)
        tooltips = ["\n".join(t for t in (printable(e.get("description") or ""), storage_words(
            self.storage_checked.get(e["name"])) if is_remote(e) else "") if t) or None for e in self.entries]
        keys = [e["name"] for e in self.entries]
        # Every look at the fleet (each few seconds) says it again: the table is filled only when a row changed.
        shown = (rows, colors, tooltips, keys, theme.current(), theme.is_dark())
        if shown == getattr(self, "shown_rows", None) and self.table.rowCount() == len(rows):
            return
        self.shown_rows = shown
        # The selection stays on its host (fill() keeps it by key) without a change of host on the way: the
        # defaults of the host shown are not asked again at every look at the fleet.
        self.table.blockSignals(True)
        try:
            fill(self.table, rows, colors, tooltips=tooltips, keys=keys)
        finally:
            self.table.blockSignals(False)

    # -- the app's storage check of each host (storage_hosts.py; the window runs it) --

    def storage_on(self, name):
        """Whether the app checks host NAME's storage (its choice, else whether the app uses it)."""
        from . import storage_hosts
        return storage_hosts.is_on(name, self.storage_used(), backend.app_settings())

    def set_storage(self, checked):
        """{host: what its last storage check said} (storage_hosts.kept), from the window."""
        self.storage_checked = dict(checked or {})
        self.show_hosts()
        self.show_storage()

    def show_storage(self):
        """The selected host's storage setting and its last check, in words."""
        name = self.host()
        remote = name not in (None, "local")
        self.storage_box.blockSignals(True)
        try:
            self.storage_box.setChecked(bool(remote and self.storage_on(name)))
        finally:
            self.storage_box.blockSignals(False)
        set_enabled(self.storage_box, remote, tr("This machine's storage check is set in Settings › Storage.")
                    if name == "local" else None)
        self.storage_note.setText(tr("Storage check: {what}", what=storage_words(self.storage_checked.get(name)))
                                  if remote else "")

    def storage_toggled(self, on):
        """The selected host's storage check on or off (the app's settings: storage_hosts)."""
        from . import storage_hosts
        name = self.host()
        if name in (None, "local"):
            return
        chosen = storage_hosts.choices(backend.app_settings())
        chosen[name] = bool(on)
        try:
            self.sai.save_setting(storage_hosts.SETTING, chosen)
        except (OSError, ValueError) as exc:
            warn(self, tr("Not saved"), printable_text(exc))
        self.show_hosts()
        self.show_storage()

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
        for widget in (self.starts_button, self.questions_button, self.test_button, self.doctor_button,
                       self.optimize_button):
            widget.setEnabled(name is not None)
        # Install… stands out where the host has no sandbox-ai that answers (missing, or not reachable).
        set_enabled(self.install_button, remote and self.sai.host_install_supported(),
                    tr("This machine installs with install.sh (or: sandbox-ai update).") if name == "local" else
                    tr("This sandbox-ai cannot install on hosts yet: update it.") if remote else None)
        set_enabled(self.uninstall_button, remote and self.sai.host_uninstall_supported(),
                    tr("This machine's sandbox-ai is the one the app runs from: remove it in a terminal with "
                       "sandbox-ai uninstall.") if name == "local" else
                    tr("This sandbox-ai cannot uninstall on hosts yet: update it.") if remote else None)
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
        self.show_storage()
        if name != self.defaults_host:
            self.defaults, self.defaults_host = None, None
            self.set_defaults_enabled(False)
            self.defaults_note.setText("")
            if self.isVisible() and name is not None:
                self.load_defaults()
        names = self.hosts()
        if len(names) > 1:  # several: Remove… takes those it can, the rest acts on one host
            why = tr("Acts on one host: {count} are selected.", count=len(names))
            for widget in (self.edit_button, self.starts_button, self.questions_button, self.test_button,
                           self.doctor_button, self.install_button, self.from_here_button, self.update_button,
                           self.update_mode_button, self.uninstall_button):
                set_enabled(widget, False, why)
            set_enabled(self.remove_button, any(n != "local" for n in names), tr("This machine is always in the list."))

    def showEvent(self, event):  # noqa: N802
        """The chosen host's defaults are asked when the page is shown, not when the window is built (no SSH at
        every start of the app for a page nobody opened)."""
        super().showEvent(event)
        if self.host() is not None and self.defaults_host != self.host() and not event.spontaneous():
            self.load_defaults()

    def add(self):
        from . import drafts
        key = ("host-add",)  # what was typed and not added (Cancel, a refusal): filled in again (drafts.py)
        dialog = AddHostDialog(self, draft=key)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        name, ssh, port, identity, description = dialog.values()
        extra = dialog.extra()
        if extra["command"] and not backend.valid_directory(extra["command"]):
            warn(self, tr("Add a host"), tr("sandbox-ai's path there is an absolute path, e.g. "
                                            "/opt/sandbox-ai/bin/sandbox-ai."))
            return

        def done(result, error):
            if error:
                warn(self, tr("Add a host"), printable_text(str(error)))
                return
            drafts.drop(key)
            self.load_hosts(name)
            self.refresh_requested.emit()
            # A key ssh would refuse (others could read it) was made private, and the connection tried: said here.
            said = [line.strip() for line in str(result or "").splitlines() if line.strip().startswith(("key:", "note:",
                                                                                                    "connection:"))]
            if said:
                self.status.setText(printable_text("\n".join([name, *said])))
        background(lambda: self.sai.host_add(name, ssh, port, identity, description, **extra), owner=self, work=True,
                   source=self.add_button, timeout=60, done=done)

    def edit(self):
        name = self.host()
        if name in (None, "local"):
            return
        dialog = EditHostDialog(self.entry(name), self)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        sudo = dialog.sudo.isChecked()
        if sudo == bool(self.entry(name).get("update_sudo")):
            return
        if not ask_first(self, "host-edit", tr("Edit {host}", host=name), tr(
                "Update sandbox-ai on {host} with sudo from now on?", host=self.label(name)) if sudo else tr(
                "Update sandbox-ai on {host} without sudo from now on?", host=self.label(name)), tr("Change")):
            return
        background(self.sai.host_set, name, sudo, owner=self, work=True, source=self.edit_button, timeout=60,
                   done=lambda result, error: warn(self, tr("Edit {host}", host=name), printable_text(str(error)))
                   if error else self.load_hosts(name))

    def remove(self):
        names = [name for name in self.hosts() if name not in (None, "local")]
        if len(names) > 1:
            self.remove_several(names)
            return
        name = names[0] if names else None
        if name in (None, "local"):
            return
        if not ask_first(self, "host-remove", tr("Remove host"), tr(
                "Remove {host} from the list of hosts? Its sandbox-ai and its starts stay there; this app and sandbox-ai "
                "web no longer reach it (its approvals are no longer answered from here).", host=self.label(name)),
                tr("Remove")):
            return

        def done(result, error):
            if error:
                warn(self, tr("Remove"), printable_text(str(error)))
                return
            self.fleet.pop(name, None)
            self.question_counts.pop(name, None)
            self.host_removed.emit(name)  # its starts and questions leave the other pages at once
            self.load_hosts()
        background(self.sai.host_remove, name, owner=self, work=True, source=self.remove_button, timeout=60, done=done)

    def remove_several(self, names):
        """Remove… with several hosts selected: one question naming them, then each (sandbox-ai hosts remove)."""
        if not ask_first(self, "host-remove", tr("Remove host"), several(tr(
                "Remove these {count} hosts from the list of hosts? Their sandbox-ai and their starts stay there; this "
                "app and sandbox-ai web no longer reach them (their approvals are no longer answered from here).",
                count=len(names)), [self.label(name) for name in names]), tr("Remove")):
            return

        def done(outcomes):
            gone = [name for name, _, error in outcomes if error is None]
            for name in gone:
                self.fleet.pop(name, None)
                self.question_counts.pop(name, None)
                self.host_removed.emit(name)  # its starts and questions leave the other pages at once
            report_each(self, tr("Remove"), outcomes, self.label)
            self.load_hosts()
        run_each(self.sai.host_remove, names, owner=self, source=self.remove_button, timeout=60 * len(names), done=done)

    def stop_all(self):
        """Stop all starts on the chosen host: what runs there read first (`list --json` there, in the background),
        asked about with that list (start-stop; Cancel the default), then `hosts run HOST -- stop --all --yes`."""
        name = self.host()
        if name in (None, "local"):
            return False
        self.status.setText(tr("Asking {host} what runs…", host=name))

        def listed(entries, error):
            if error is not None or not isinstance(entries, list):
                self.status.setText(printable_text(str(error or tr("not a list"))))
                return
            running = [e for e in entries if isinstance(e, dict) and e.get("kind") != "mcp"
                       and e.get("state") in ("running", "paused", "orphaned")]
            if not running:
                self.status.setText(tr("Nothing runs on {host} that could be stopped.", host=name))
                return
            names = ", ".join(printable(str(e.get("name"))) for e in running)
            if not ask_first(self, "start-stop", tr("Stop all"), on_host(name, tr(
                    "Stop these {count} starts? Each ends as Stop ends it; what a session keeps stays.",
                    count=len(running))) + "\n\n" + names, tr("Stop all")):
                self.status.setText("")
                return
            self.status.setText(tr("Stopping every start on {host}…", host=name))

            def stopped(result, failed):
                status, out, err = result if failed is None else (1, "", str(failed))
                self.status.setText(printable_text(((out or "") + (err or "")).strip())[-500:]
                                    or tr("exit status {status}", status=status))
                if status != 0:
                    widgets.warn(self, tr("Stop all"), printable_text(((out or "") + (err or "")).strip())[-1500:]
                                 or tr("exit status {status}", status=status), modal=False)
            background(lambda: self.sai.host_run(name, "stop", "--all", "--yes", timeout=900), owner=self,
                       work=True, timeout=930, done=stopped)
        background(self.sai.host_json, name, "list", "--json", owner=self, timeout=90, done=listed)
        return True

    def test(self):
        name = self.host()
        if name is None:
            return
        self.status.setText(tr("Testing {host}…", host=name))

        def done(result, error):
            if error:
                self.status.setText(printable_text(str(error)))
                return
            keys = key_lines(result)
            if result.get("error"):
                self.status.setText(printable_text("\n".join([f"{name}: {result['error']}", *keys])))
            else:
                self.status.setText(printable_text("\n".join([f"{name}: {result.get('version')}", *keys])))
            self.show_doctor(result.get("doctor") or [], name)
        background(self.sai.host_test, name, owner=self, work=True, source=self.test_button, timeout=180, done=done)

    def check_host(self):
        """sandbox-ai doctor --backend B --json on the chosen host (this machine too), shown on the Doctor tab."""
        name = self.host()
        if name is None:
            return
        engine = self.doctor_backend.currentText()
        self.status.setText(tr("Checking {host} for {backend}…", host=name, backend=engine))

        def done(result, error):
            if error:
                self.status.setText(f"{printable(name)}: {printable_text(error)}")
                return
            self.status.setText(tr("{host}: checked for {backend}", host=name, backend=engine))
            self.show_doctor(result, name)
        background(self.sai.host_doctor, name, engine, owner=self, work=True, source=self.doctor_button, timeout=330,
                   done=done)

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
            self.defaults_note.setText(tr("{host}: no answer", host=printable(name)) + f" ({printable_text(error)})")
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
        # No keep_days there: kept starts unused for 90 days go (the default); 0: never (the box unchecked).
        days = values.get("keep_days")
        self.keep.setChecked(days != 0)
        self.keep_days.setValue(days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else
                                backend.DEFAULT_KEEP_DAYS)
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
        if not self.keep.isChecked():
            chosen["keep_days"] = 0  # never (without keep_days: the default, 90 days)
        elif self.keep_days.value() != backend.DEFAULT_KEEP_DAYS or "keep_days" in (self.defaults or {}):
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
                                              error=printable_text(str(error))), modal=False)
            else:
                applied, refused, said = result
                if refused is not None:
                    warn(self, tr("Defaults"), tr(
                        "Saved on {host}: {applied}. Refused: {refused} ({reason}). Not tried: {rest}.",
                        host=self.label(name), applied=", ".join(default_step(*s) for s in applied) or tr("nothing"),
                        refused=default_step(*refused), reason=printable_text(said)[-500:] or "?",
                        rest=", ".join(default_step(*s) for s in steps[len(applied) + 1:]) or tr("nothing")),
                        modal=False)
            self.defaults_host = None
            self.load_defaults()
        background(work, owner=self, work=True, source=self.save_defaults_button, timeout=60 * len(argv) + 30,
                   done=done)

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
            self.status.setText(printable_text(str(error)))
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
        which = self.which_installation(name)
        if which is False:
            return
        self.results, self.queue = [], [name] if which != "all" else [name, name]
        self.queue_installations = [which] if which != "all" else ["user", "system"]
        self.updates.clear()
        self.tabs.setCurrentWidget(self.updates)
        self.queue_sudo, self.queue_from_here = self.update_sudo.currentData(), False
        self.next_update()

    def which_installation(self, name):
        """Which installation of host NAME Update takes when it has several (its status: the user's own and a system
        one): None (the one its commands run: the first choice), "user", "system", "all" - or False (Cancel).
        One installation (or none known): None, nothing asked."""
        items = [item for item in installations_of((getattr(self, "versions", None) or {}).get(name))
                 if item.get("executable") and item.get("kind") in ("user", "system")]
        if len(items) < 2:
            return None
        items.sort(key=lambda item: not item.get("commands"))  # (the one its commands run first: the default)
        choices = [(item["kind"], tr("{kind} installation {version}", kind=kind_text(item["kind"]),
                                     version=printable(item.get("version") or "?"))
                    + (" - " + tr("a terminal there runs it") if item.get("terminal") else ""))
                   for item in items] + [("all", tr("Both"))]
        answer = widgets.choose(self, tr("Update"), tr(
            "{host} has {count} installations of sandbox-ai. Which one is updated?", host=self.label(name),
            count=len(items)), choices)
        return False if answer is None else answer

    def set_update_mode(self):
        """The host's own update mode: chosen in a small dialog, asked first (update-mode), set in the background
        (`sandbox-ai update mode MODE --json` there, through SSH for another host); the versions are read again."""
        name = self.host()
        if name is None:
            return
        from .updates_view import MODES
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Update mode of {host}", host=self.label(name)))
        form = QFormLayout(dialog)
        mode = QComboBox()
        for value, text in MODES:
            mode.addItem(tr(text), value)
        known = (self.versions.get(name) or {}).get("update_mode")
        mode.setCurrentIndex(max(0, mode.findData(known or "notify")))
        window = QLineEdit()
        window.setPlaceholderText("03:00-05:00")
        form.addRow(tr("Update mode"), mode)
        form.addRow(tr("Window of mode auto"), window)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        chosen, at = mode.currentData(), window.text().strip() if mode.currentData() == "auto" else ""
        if not ask_first(self, "update-mode", tr("Update mode"), on_host(name, tr(
                "Set the update mode to {mode}? What the sandbox-ai there does by itself follows it (a timer as that "
                "host's user).", mode=chosen)), tr("Set")):
            return
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.set_update_mode(chosen, at or None, name), owner=self, work=True,
                   source=self.update_mode_button, timeout=240, what=f"update mode {chosen} on {name}",
                   done=lambda result, error: self.update_mode_set(name, result, error))

    def update_mode_set(self, name, result, error):
        if error is not None:
            warn(self, tr("Update mode"), printable_text(error))
            self.status.setText(tr("Not done."))
            return
        self.status.setText(tr("{host}: update mode {mode}", host=self.label(name), mode=printable(result.get("mode"))))
        if name in self.versions and isinstance(self.versions[name], dict):
            self.versions[name]["update_mode"] = result.get("mode")
        self.load_versions(refresh=True)

    def own_version(self):
        """This machine's sandbox-ai version (the copy the app runs from), for its row: read once."""
        if getattr(self, "_own_version", None) is None:
            try:
                self._own_version = printable(self.sai.version())
            except Exception as _exc:  # noqa: BLE001 - only shown
                applog.swallowed("hosts_page", "HostsPage.own_version: not read", _exc)
                self._own_version = ""
        return self._own_version

    def update_all(self):
        names = [entry["name"] for entry in self.entries if is_remote(entry)]
        if not names or self.busy_updating():
            return
        self.results, self.queue, self.queue_installations = [], list(names), []
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
        self.results, self.queue, self.queue_installations = [], [name], []
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
        from_here = getattr(self, "queue_from_here", False)  # host_update's FROM_HERE
        chosen = getattr(self, "queue_installations", None) or []
        which = chosen[len(self.results)] if len(self.results) < len(chosen) else None
        extra = {"installation": which} if which else {}
        self.task.call_watched(self.sai, lambda: self.sai.host_update(name, sudo, *([True] if from_here else []),
                                                                      **extra),
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
                # the kind and prefix the update itself found there (hosts.update: "mode", "prefix"), as the web
                # takes them; the cache only when it said none (review 6: no cache, a second copy in /usr/local)
                info = (getattr(self, "versions", None) or {}).get(name) or {}
                mode = result.get("mode") if result.get("mode") in ("system", "user") else \
                    "system" if result.get("kind") == "system" else "user"
                prefix = (result.get("prefix") or info.get("prefix")) if mode == "system" else None
                chosen = getattr(self, "queue_installations", None) or []
                which = chosen[len(self.results) - 1] if len(self.results) - 1 < len(chosen) else None
                argv = self.sai.host_install_terminal(name, mode, prefix, True, "here") if from_here else \
                    self.sai.host_update_terminal(name, *([which] if which else []))
                self.in_terminal(argv, tr("{host}: update", host=name))
                self.updates.appendPlainText(printable(tr("  {host}: the update runs in a terminal tab, where sudo "
                                                          "asks for the password", host=name)))
            except (OSError, ValueError) as exc:
                self.updates.appendPlainText(printable_text(f"  {exc}"))
        self.next_update()

    def updates_done(self):
        results = self.results
        updated = sum(1 for r in results if r.get("changed"))
        failed = sum(1 for r in results if r.get("error") and not r.get("after"))
        summary = tr("{count} hosts: {updated} updated, {current} up to date, {failed} not updated",
                     count=len(results), updated=updated, current=len(results) - updated - failed, failed=failed)
        self.updates.appendPlainText(summary)
        self.status.setText(summary)
        # (hosts update read each host's version again and kept it: no SSH here, and never an empty column)
        try:
            self.versions = self.sai.host_statuses(cached=True)
        except (OSError, ValueError):
            pass
        self.load_hosts()

    # -- removing sandbox-ai on a host --

    def uninstall(self):
        """`sandbox-ai hosts uninstall NAME ...` after UninstallDialog (the installation, --purge and the rest chosen
        there, with the host's own table of what goes) and a question naming the host; sudo that wants a password:
        the same in a terminal tab, where it asks."""
        name = self.host()
        if name is None or self.busy_updating():
            return
        if name == "local":
            warn(self, tr("Uninstall"), tr("This machine's sandbox-ai is the one the app runs from: remove it in a "
                                           "terminal with sandbox-ai uninstall (--purge: also its data)."))
            return
        if not self.sai.host_uninstall_supported():
            return
        items = [item for item in installations_of((getattr(self, "versions", None) or {}).get(name))
                 if item.get("executable")]

        def plan(values, done):
            def answered(result, error):
                if error is not None:
                    return done(str(error), False)
                text = result.get("plan") or ""
                if result.get("error"):
                    return done(f"{text}\n\n{result['error']}".strip(), False)
                if result.get("sudo"):
                    text += "\n\n" + tr("(with sudo there: the installation is root's)")
                return done(text, True)
            background(lambda: self.sai.host_uninstall_plan(name, **values), owner=self, timeout=300, done=answered)
        dialog = UninstallDialog(name, items, plan, self)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        item = dialog.chosen() or {}
        what = tr("{kind} installation {version}", kind=kind_text(item.get("kind")),
                  version=printable(item.get("version") or "?")) if item else tr("its installation")
        if not ask_first(self, "host-uninstall", tr("Uninstall sandbox-ai on {host}", host=name), tr(
                "Remove the {what} on {host}?", what=what, host=self.label(name))
                + (" " + tr("Also the SSH user's sandbox-ai data there.") if values["purge"] else "")
                + (" " + tr("With the secrets and logins there - they cannot be recovered.") if values["secrets"]
                   else ""), tr("Uninstall")):
            return
        self.tabs.setCurrentWidget(self.updates)
        self.updates.appendPlainText(printable(tr("{host}: removing the {what}…", host=name, what=what)))
        self.status.setText(tr("Uninstalling sandbox-ai on {host}…", host=name))
        self.task.call_watched(self.sai, lambda: self.sai.host_uninstall(name, **values),
                               lambda result, error: self.uninstalled(name, values, result, error),
                               tr("Uninstalling sandbox-ai on {host}…", host=name),
                               source=[self.uninstall_button, self.install_button, self.update_button,
                                       self.from_here_button, self.update_all_button], errors_only=True)

    def uninstalled(self, name, values, result, error):
        if self.task.cancelled:
            self.updates.appendPlainText(printable(tr("{host}: cancelled", host=name)))
            self.load_versions(refresh=True)
            return
        if error is not None:
            result = {"name": name, "error": str(error)}
        text = uninstall_text(name, result)
        self.updates.appendPlainText(text)
        self.status.setText(text.splitlines()[0])
        if result.get("needs_password"):
            # sudo there wants a password: the same uninstall in a terminal tab, where sudo asks for it.
            try:
                self.in_terminal(self.sai.host_uninstall_terminal(name, values.get("installation"), values["purge"],
                                                                  values["secrets"], values["mcp_data"]),
                                 tr("{host}: uninstall", host=name))
                self.updates.appendPlainText(printable(tr("  {host}: the uninstall runs in a terminal tab, where "
                                                          "sudo asks for the password", host=name)))
            except (OSError, ValueError) as exc:
                self.updates.appendPlainText(printable_text(f"  {exc}"))
            return
        if result.get("removed_from_list"):
            self.fleet.pop(name, None)
            self.question_counts.pop(name, None)
            self.host_removed.emit(name)  # its starts and questions leave the other pages at once
        # (hosts uninstall read what is left there and kept it: no SSH here)
        try:
            self.versions = self.sai.host_statuses(cached=True)
        except (OSError, ValueError):
            pass
        self.load_hosts()
        if result.get("error"):
            warn(self, tr("Uninstall sandbox-ai on {host}", host=name), text,
                 details=printable(result.get("output") or "") or None, modal=False)

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
        except Exception as _exc:  # noqa: BLE001 - only shown
            applog.swallowed("hosts_page", "HostsPage.install: only shown", _exc)
            version = ""
        dialog = InstallDialog(self.entry(name), version, self)
        if widgets.exec_dropped(dialog) != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.sai.host_install_args(name, **values)
        except ValueError as exc:
            warn(self, tr("Install"), printable_text(str(exc)))
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
                self.updates.appendPlainText(printable_text(f"  {exc}"))
        else:
            (warn if result.get("error") else inform)(self, tr("Install sandbox-ai on {host}", host=name), text,
                                                       details=printable(result.get("output") or "") or None,
                                                       modal=False)
        self.load_versions(refresh=True)

    def show_doctor(self, results, name=None):
        """The host's doctor (--json: a list of checks; an older one's text output as a string), headed by its
        sandbox-ai version (the doctor's first check; from an older doctor without it, the hosts list's)."""
        known = (getattr(self, "versions", None) or {}).get(name) if name else None
        if isinstance(results, str):
            self.doctor.set_text(results, host=name, known=known)
        else:
            self.doctor.set_results([r for r in results if isinstance(r, dict)], host=name, known=known)
        self.tabs.setCurrentWidget(self.doctor_tab)
