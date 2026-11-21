"""A folder of another machine for a sandbox: sshfs://[USER@]HOST[:PORT]/PATH, as sandbox-ai takes it in --workspace,
--mount and `workspace add` (sandbox/remotefs.py). The start's host mounts it with sshfs - its SSH keys, ssh-agent and
~/.ssh/config, or a host of `sandbox-ai hosts` - and gives the sandbox only the files; the launcher checks the source
again. Here: the syntax (valid_source, the same rules as remotefs.parse), and the dialog that builds a source from
its parts (Board fields, the Add workspace dialog)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import posixpath
import re

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox)

from .files import printable
from .i18n import tr
from .widgets import explanation, plain_label

SCHEMES = ("sshfs", "nfs", "smb")
SOURCE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:(?P<user>[^@/\s]*)@)?"
                    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^:/@\s\[\]]*)(?::(?P<port>[^/]*))?(?P<path>/.*)?", re.S)
USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}")
DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# What NFS and SMB sources may say after "?" (sandbox-ai's remotefs.QUERY): None takes a secret's name.
QUERY = {"nfs": {"mount": ("fuse", "sudo"), "vers": ("3", "4", "4.0", "4.1", "4.2")},
         "smb": {"mount": ("sudo",), "vers": ("2.0", "2.1", "3.0", "3.02", "3.1.1"), "secret": None}}


def is_remote(value):
    """True if VALUE names a folder of another machine (sshfs://, nfs://, smb://)."""
    return isinstance(value, str) and value[:16].lower().startswith(tuple(f"{s}://" for s in SCHEMES))


def parse(value):
    """VALUE checked as sandbox-ai checks it (remotefs.parse): {"scheme", "user", "domain", "host", "port", "path",
    "query"} (sshfs: PATH "/abs", "~/rel" or "~"), or ValueError with what is wrong."""
    if not is_remote(value):
        raise ValueError(tr("A folder of another machine is sshfs://USER@MACHINE/PATH"))
    if not value.isprintable() or any(c in value for c in ",\n\r\0"):
        raise ValueError(tr("No commas or control characters"))
    scheme = value.split(":", 1)[0].lower()
    query = {}
    if scheme in QUERY and "?" in value:
        value, _, given = value.partition("?")
        for item in given.split("&") if given else []:
            key, separator, setting = item.partition("=")
            allowed = QUERY[scheme].get(key, ())
            if key not in QUERY[scheme] or not separator or key in query or \
                    (allowed is None and not SECRET.fullmatch(setting)) or (allowed and setting not in allowed):
                raise ValueError(tr("?{option} is not an option of {scheme}://", option=printable(item),
                                    scheme=scheme))
            query[key] = setting
    found = SOURCE.fullmatch(value)
    if not found:
        raise ValueError(tr("A folder of another machine is sshfs://USER@MACHINE/PATH"))
    user, host, port, path = found["user"], found["host"], found["port"], found["path"] or ""
    domain = None
    if scheme == "smb" and user is not None and ";" in user:
        domain, _, user = user.partition(";")
        if not DOMAIN.fullmatch(domain):
            raise ValueError(tr("The domain is 1-64 letters, digits, _ . -"))
    if scheme == "nfs" and user is not None:
        raise ValueError(tr("An NFS source names no user"))
    if user is not None and not USER.fullmatch(user):
        raise ValueError(tr("The user is 1-64 letters, digits, _ . -"))
    if not (host.startswith("[") and host.count(":") >= 2) and not HOST.fullmatch(host or ""):
        raise ValueError(tr("Which machine? A name or an address"))
    if port is not None and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError(tr("The port is 1-65535"))
    home = scheme == "sshfs" and (path in ("", "/~") or path.startswith("/~/"))
    rest = path[3:] if home and path.startswith("/~/") else "" if home else path
    if home and rest and (posixpath.normpath(rest) == ".." or posixpath.normpath(rest).startswith("../")):
        raise ValueError(tr("~/PATH lies in the remote HOME (no '..' out of it)"))
    if scheme in ("nfs", "smb"):
        normal = posixpath.normpath(rest) if rest else ""
        if not normal.startswith("/") or normal == "/" or ".." in normal.split("/"):
            raise ValueError(tr("Name the export (NFS) or the share (SMB)"))
    return {"scheme": scheme, "user": user, "domain": domain, "host": host, "port": int(port) if port else None,
            "path": ("~/" + rest if rest else "~") if home else rest, "query": query}


def valid_source(value):
    """True if VALUE is a folder of another machine sandbox-ai takes (the launcher checks it again)."""
    try:
        parse(value)
    except ValueError:
        return False
    return True


def build(host, path, user="", port=None, scheme="sshfs"):
    """The source for HOST (a machine, an SSH alias or a host of `sandbox-ai hosts`), PATH (absolute, ~ or ~/...),
    USER and PORT (optional)."""
    host, path, user = str(host or "").strip(), str(path or "").strip(), str(user or "").strip()
    if path in ("", "~"):
        path = "/~"
    elif path.startswith("~/"):
        path = "/" + path
    elif not path.startswith("/"):
        path = "/~/" + path  # (a relative path is the remote HOME's, as ssh reads it)
    return f"{scheme}://{user + '@' if user else ''}{host}{':' + str(port) if port else ''}{path}"


def label(value):
    """What /workspaces/NAME a source gets: its folder's name (as sandbox-ai names its mount point)."""
    try:
        path = parse(value)["path"]
    except ValueError:
        return ""
    name = posixpath.basename(path.rstrip("/")) if path not in ("~", "/") else ("home" if path == "~" else "root")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64].lstrip(".-") or "remote"


def warning(value, read_only):
    """The risk of giving VALUE to a sandbox, in words (said under the field)."""
    try:
        found = parse(value)
    except ValueError:
        return ""
    machine = printable(found["host"])
    if found["scheme"] != "sshfs":
        kind = "NFS" if found["scheme"] == "nfs" else "SMB"
        if read_only:
            return tr("A {kind} folder of {machine}: the start's host mounts it (fuse-nfs, or the kernel's client "
                      "through sudo, which asks for your password in a terminal); the agent reads its files, never the "
                      "password or the connection.", kind=kind, machine=machine)
        return tr("A {kind} folder of {machine}, writable: the agent changes and deletes the files on that machine. "
                  "The start's host mounts it (fuse-nfs, or the kernel's client through sudo), its traffic does not "
                  "pass the sandbox's proxy; read-only is the safer choice.", kind=kind, machine=machine)
    if read_only:
        return tr("A folder of {machine}: the start's host mounts it with sshfs (its SSH keys and ~/.ssh/config); the "
                  "agent reads its files, never the key or the connection.", machine=machine)
    return tr("A folder of {machine}, writable: the agent changes and deletes the files on that machine. The start's "
              "host mounts it with sshfs (its SSH keys and ~/.ssh/config), its traffic does not pass the sandbox's "
              "proxy; read-only is the safer choice.", machine=machine)


def known_hosts():
    """The names of `sandbox-ai hosts` (its small local file, no SSH): a source of HOST without USER@ and :PORT is
    reached as that host."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "hosts.json"
    try:
        hosts = json.loads(path.read_text()).get("hosts")
    except (OSError, ValueError, AttributeError, RecursionError):
        return []
    return sorted(name for name in hosts if isinstance(name, str) and HOST.fullmatch(name)) \
        if isinstance(hosts, dict) else []


class RemoteSourceDialog(QDialog):
    """Builds sshfs://[USER@]HOST[:PORT]/PATH from its parts: the machine (an SSH name or address, or a host of
    `sandbox-ai hosts`), the user and port (optional), the folder there. OK only for a source sandbox-ai takes."""

    def __init__(self, parent=None, value=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Folder of another machine (sshfs)"))
        form = QFormLayout(self)
        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.addItems(known_hosts())
        self.host.setCurrentText("")
        self.host.setAccessibleName(tr("Machine"))
        self.host.setToolTip(tr("An SSH name (~/.ssh/config applies), an address, or a host of sandbox-ai hosts"))
        self.user = QLineEdit()
        self.user.setPlaceholderText(tr("from ~/.ssh/config"))
        self.user.setAccessibleName(tr("User"))
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText(tr("from ~/.ssh/config"))
        self.port.setAccessibleName(tr("Port"))
        self.path = QLineEdit()
        self.path.setPlaceholderText(tr("/srv/project or ~/project"))
        self.path.setAccessibleName(tr("Folder there"))
        form.addRow(tr("Machine"), self.host)
        form.addRow(tr("User"), self.user)
        form.addRow(tr("Port"), self.port)
        form.addRow(tr("Folder there"), self.path)
        self.shown = plain_label(selectable=True)
        form.addRow(tr("Source"), self.shown)
        self.problem = plain_label(role="notice-warn", selectable=True)
        form.addRow(self.problem)
        form.addRow(explanation(tr("This computer (or the start's host) mounts it with sshfs as you, with your SSH "
                                   "keys; the host key is always checked. The sandbox gets only the files: never "
                                   "the key, the SSH agent or the connection. It is unmounted when the run ends.")))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        if is_remote(value):
            try:
                given = parse(value)
            except ValueError:
                given = None
            if given:
                self.host.setCurrentText(given["host"])
                self.user.setText(given["user"] or "")
                self.port.setValue(given["port"] or 0)
                self.path.setText(given["path"])
        for signal in (self.host.currentTextChanged, self.user.textChanged, self.path.textChanged):
            signal.connect(lambda *_: self.check())
        self.port.valueChanged.connect(lambda *_: self.check())
        self.check()
        self.resize(520, 0)

    def source(self):
        return build(self.host.currentText(), self.path.text(), self.user.text(), self.port.value() or None)

    def check(self):
        value = self.source()
        try:
            parse(value)
            problem = ""
        except ValueError as exc:
            problem = str(exc)
        if not self.host.currentText().strip():
            problem = tr("Which machine? A name or an address")
        self.shown.setText(printable(value))
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problem)
        return not problem
