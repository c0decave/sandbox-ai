"""The host chooser of the Manage pages, Activity, Statistics and Teams: this machine, or a host of `sandbox-ai hosts`
(over SSH). The same choice as the LLM servers page: the list comes from sandbox-ai's small local file of hosts (no
SSH to fill it); a page runs its commands on the host chosen through SandboxAI.host_run, host_json, host_command or
host_terminal."""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget

from .files import printable
from .i18n import tr
from .widgets import plain_label

LOCAL = "local"  # this machine, as sandbox-ai hosts names it
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def valid_host(name):
    """NAME if it may go back to sandbox-ai as a host's name (never one that reads as an option), else None."""
    return name if name == LOCAL or (isinstance(name, str) and not name.startswith("-") and HOST.fullmatch(name)) \
        else None


def on_host(host, text):
    """TEXT (a question or a result) with the host it is about: "On this computer: …" or "On HOST: …" (a confirmation
    always says where it acts)."""
    if host in (None, LOCAL):
        return tr("On this computer: {text}", text=text)
    return tr("On {host}: {text}", host=printable(host), text=text)


def host_label(host):
    return tr("{name} (this machine)", name=LOCAL) if host in (None, LOCAL) else printable(host)


class HostChooser(QWidget):
    """"Host: [this machine | a host …]". CHANGED(host) when the human chooses another one (not while it is filled)."""
    changed = Signal(str)

    def __init__(self, sai, tooltip=None, parent=None):
        super().__init__(parent)
        self.sai = sai
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(plain_label(tr("Host"), word_wrap=False))
        self.box = QComboBox()
        self.box.setAccessibleName(tr("Host"))
        self.box.setToolTip(tooltip or tr("This machine, or a host of sandbox-ai hosts (over SSH)"))
        self.box.currentIndexChanged.connect(lambda _=0: self.changed.emit(self.host()))
        row.addWidget(self.box, 1)
        self.problem = None
        self.load()

    def load(self):
        """This machine, and each host of sandbox-ai hosts (a small local file: no SSH here); the choice stays while
        its host does."""
        current = self.host()
        entries, self.problem = [], None
        try:
            if self.sai.hosts_supported():
                entries = [e for e in self.sai.host_entries() if isinstance(e, dict)]
        except Exception as exc:  # noqa: BLE001 - an older sandbox-ai or a broken file: this machine only
            self.problem = printable(exc)
        self.box.blockSignals(True)
        self.box.clear()
        self.box.addItem(host_label(LOCAL), LOCAL)
        for entry in entries:
            name = entry.get("name")
            if entry.get("kind") == "local" or name == LOCAL or valid_host(name) is None:
                continue
            self.box.addItem(printable(f"{name}  {entry.get('ssh', '')}".strip()), name)
        index = self.box.findData(current) if current else 0
        self.box.setCurrentIndex(max(index, 0))
        self.box.blockSignals(False)
        if current and index < 0:
            self.changed.emit(self.host())  # its host is gone: this machine's, never that host's rows under it

    def host(self):
        return valid_host(self.box.currentData()) if self.box.count() else LOCAL

    def remote(self):
        return self.host() not in (None, LOCAL)

    def set_host(self, host):
        index = self.box.findData(host)
        if index >= 0:
            self.box.setCurrentIndex(index)

    def label(self, host=None):
        return host_label(host or self.host())
