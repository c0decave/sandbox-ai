"""The host chooser of the Manage pages, Activity, Statistics and Teams: this machine, or a host of `sandbox-ai hosts`
(over SSH). The same choice as the LLM servers page: the list comes from sandbox-ai's small local file of hosts (no
SSH to fill it); a page runs its commands on the host chosen through SandboxAI.host_run, host_json, host_command or
host_terminal."""
from __future__ import annotations

import os
from pathlib import Path
import re

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget

from .files import printable
from .i18n import tr
from .widgets import plain_label, safe_tooltip

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


class _Hosts(QObject):
    """The one signal that the list of hosts changed (a host added, edited, renamed or removed: the Hosts page says
    so): every host chooser of the app reads the hosts file again (no SSH) and keeps its choice while that host is
    there; one that left falls back to this machine."""
    changed = Signal()


_HOSTS = []


def hosts_signal():
    if not _HOSTS:
        _HOSTS.append(_Hosts())
    return _HOSTS[0]


def hosts_changed():
    """Tell every host chooser that the hosts file changed."""
    hosts_signal().changed.emit()


def _file_signature(path):
    try:
        found = os.stat(path)
    except OSError:
        return None
    return found.st_ino, found.st_size, found.st_mtime_ns


def watch_hosts_file(path, settle_ms=250):
    """Every host chooser follows the hosts file PATH also when it changes outside the app (sandbox-ai hosts, the web
    interface, an editor): a QFileSystemWatcher on the file and on the nearest folder above it that exists (a file
    replaced by rename, as sandbox-ai writes it, or made later, is seen there); a change is told once it settled
    (SETTLE_MS) and only when the file differs from what was last told. No polling: nothing runs while it is quiet.
    Called again (another window, another HOME in the tests): the watch moves to that PATH."""
    hub = hosts_signal()
    path = Path(path)
    if getattr(hub, "watcher", None) is None:
        hub.watcher = QFileSystemWatcher(hub)
        hub.settle = QTimer(hub)
        hub.settle.setSingleShot(True)
        hub.watcher.fileChanged.connect(lambda _path: hub.settle.start())
        hub.watcher.directoryChanged.connect(lambda _path: hub.settle.start())

        def settled():
            _rearm(hub)
            signature = _file_signature(hub.watched)
            if signature != hub.signature:
                hub.signature = signature
                hub.changed.emit()
        hub.settle.timeout.connect(settled)
    hub.settle.setInterval(settle_ms)
    hub.watched, hub.signature = path, _file_signature(path)
    _rearm(hub)
    return hub.watcher


def _rearm(hub):
    """Watch the file (when it is there) and the nearest existing folder above it, and nothing else."""
    path = hub.watched
    folder = next((p for p in path.parents if p.is_dir()), None)
    wanted = [str(p) for p in (path if path.is_file() else None, folder) if p is not None]
    held = hub.watcher.files() + hub.watcher.directories()
    stale = [p for p in held if p not in wanted]
    if stale:
        hub.watcher.removePaths(stale)
    new = [p for p in wanted if p not in held]
    if new:
        hub.watcher.addPaths(new)


def follow_hosts(widget, reload):
    """RELOAD() whenever the list of hosts changes, as long as WIDGET lives."""
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover
        isValid = lambda _w: True  # noqa: E731
    signal = hosts_signal().changed

    def again():
        if isValid(widget):
            reload()
        else:
            signal.disconnect(again)
    signal.connect(again)
    widget.destroyed.connect(lambda *_: _disconnect(signal, again))


def _disconnect(signal, slot):
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


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
        self.box.setToolTip(safe_tooltip(tooltip or tr("This machine, or a host of sandbox-ai hosts (over SSH)")))
        self.box.currentIndexChanged.connect(lambda _=0: self.changed.emit(self.host()))
        row.addWidget(self.box, 1)
        self.problem = None
        self.load()
        follow_hosts(self, self.load)  # a host added, renamed or removed on the Hosts page: here at once

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
