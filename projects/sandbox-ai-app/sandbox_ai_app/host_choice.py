"""The host chooser of the Manage pages, Activity, Statistics and Teams: this machine, or a host of `sandbox-ai hosts`
(over SSH). The same choice as the LLM servers page: the list comes from sandbox-ai's small local file of hosts (no
SSH to fill it); a page runs its commands on the host chosen through SandboxAI.host_run, host_json, host_command or
host_terminal."""
from __future__ import annotations

import os
from pathlib import Path
import re

from PySide6.QtCore import QEvent, QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget

from .files import printable, printable_text
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
    """"Host: [this machine | a host …]". CHANGED(host) when the human chooses another one (not while it is filled).
    LOCAL False: the hosts of sandbox-ai hosts only, never this machine (host() is None while there is none)."""
    changed = Signal(str)

    def __init__(self, sai, tooltip=None, parent=None, local=True):
        super().__init__(parent)
        self.sai, self.local = sai, local
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(plain_label(tr("Host"), word_wrap=False))
        self.box = QComboBox()
        self.box.setAccessibleName(tr("Host"))
        self.box.setToolTip(safe_tooltip(tooltip or tr("This machine, or a host of sandbox-ai hosts (over SSH)")))
        self.box.currentIndexChanged.connect(lambda _=0: self.changed.emit(self.host()))
        row.addWidget(self.box, 1)
        # Whose host it shows when the window's pages follow the Sessions page's host (HostFollow): hidden otherwise.
        self.note = follow_note()
        row.addWidget(self.note)
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
            self.problem = printable_text(exc)
        self.box.blockSignals(True)
        self.box.clear()
        if self.local:
            self.box.addItem(host_label(LOCAL), LOCAL)
        for entry in entries:
            name = entry.get("name")
            if entry.get("kind") == "local" or name == LOCAL or valid_host(name) is None:
                continue
            self.box.addItem(printable(f"{name}  {entry.get('ssh', '')}".strip()), name)
        index = self.box.findData(current) if current else 0
        self.box.setCurrentIndex(max(index, 0))
        self.box.blockSignals(False)
        if current and index < 0 and self.box.count():
            self.changed.emit(self.host())  # its host is gone: this machine's, never that host's rows under it

    def host(self):
        if not self.box.count():
            return LOCAL if self.local else None
        return valid_host(self.box.currentData())

    def remote(self):
        return self.host() not in (None, LOCAL)

    def set_host(self, host):
        index = self.box.findData(host)
        if index >= 0:
            self.box.setCurrentIndex(index)

    def label(self, host=None):
        return host_label(host or self.host())


# -- the pages follow the Sessions page's host --

def follow_note():
    """The line beside a page's host chooser that says whose host the page shows (HostFollow fills it)."""
    note = plain_label(role="secondary", word_wrap=False)
    note.hide()
    return note


def follow_label(host):
    return tr("All hosts") if host is None else host_label(host)


class HostFollow(QObject):
    """The host chosen on the Sessions page, for every page with a host chooser of its own (the window attaches
    them): such a page shows that host from then on, until the human chooses another one on the page itself - which
    holds until the Sessions page's host changes again. "All hosts" there changes only a page that can show all of
    them (its chooser has an entry None). A page that is hidden takes the host when it is shown next (before it
    reads, then once: no SSH for a page nobody looks at); the page's own host change drops what its reads of the
    host before still bring. The note beside each chooser says whose host the page shows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host, self.chosen = None, False  # the Sessions page's host (None: all hosts); chosen there yet
        self.pages, self.pending = {}, {}  # {key: (page, [combo], note)}, {key: host it takes when shown}
        self.applying = None  # the page given the host now (its chooser's change is not the human's)

    def attach(self, key, page, combos, note=None):
        """PAGE (KEY) follows with COMBOS (QComboBoxes whose item data are host names; the first one is the page's
        host), NOTE (a follow_note()) beside the first one."""
        combos = [combo for combo in combos if combo is not None]
        if not combos:
            return
        self.pages[key] = (page, combos, note)
        combos[0].currentIndexChanged.connect(lambda _=0, k=key: self.changed_on_page(k))
        page.destroyed.connect(lambda *_, k=key: (self.pages.pop(k, None), self.pending.pop(k, None)))
        if self.chosen:
            self.offer(key)

    def choose(self, host):
        """The Sessions page's host is HOST now (None: all hosts)."""
        self.host, self.chosen = host, True
        for key in list(self.pages):
            self.offer(key)

    def takes(self, combo, host):
        return combo.findData(host) >= 0

    def offer(self, key):
        page, combos, _note = self.pages[key]
        if not any(self.takes(combo, self.host) for combo in combos):
            self.pending.pop(key, None)  # (all hosts, or a host it does not list: its own choice stays)
            page.removeEventFilter(self)
            self.show_note(key)
            return
        self.pending[key] = self.host
        if page.isVisible():
            self.apply(key)
        else:
            page.installEventFilter(self)  # (only while a host waits for it: shown some other way, it still takes it)

    def before_show(self, key):
        """Page KEY is about to be shown: the host it was given meanwhile, while it is still hidden (its own show
        then reads that host's, once), and the line that says whose host it shows."""
        if key in self.pending:
            self.apply(key)
        elif key in self.pages:
            self.show_note(key)

    def apply(self, key):
        page, combos, _note = self.pages[key]
        host = self.pending.pop(key)
        page.removeEventFilter(self)
        self.applying = key
        try:
            for combo in combos:
                index = combo.findData(host)
                if index >= 0 and index != combo.currentIndex():
                    combo.setCurrentIndex(index)  # as the human would: the page's own host change runs
        finally:
            self.applying = None
        self.show_note(key)

    def changed_on_page(self, key):
        """Page KEY's own host changed: chosen there (or by what opened it on a host: Secrets and logins on HOST…,
        Show questions): that choice is newer than a host still waiting for the page, which then goes."""
        if self.applying != key and key in self.pending:
            del self.pending[key]
            self.pages[key][0].removeEventFilter(self)
        self.show_note(key)

    def show_note(self, key):
        entry = self.pages.get(key)
        if entry is None or entry[2] is None:
            return
        _page, combos, note = entry
        shown = combos[0].currentData()
        if not self.chosen or (self.host is None and not self.takes(combos[0], None)) or \
                (self.host is None and shown is None):
            note.hide()  # (all hosts there and here, or all hosts there and a page of one host: nothing to say)
            return
        if shown == self.host:
            text = tr("{host}, as on the Sessions page", host=follow_label(shown))
        else:
            text = tr("{host}, chosen here; the Sessions page shows {other}", host=follow_label(shown),
                      other=follow_label(self.host))
        note.setText(text)
        note.setToolTip(safe_tooltip(text))
        note.show()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt's name
        """A page shown some other way than through the window's navigation: its host before its own showEvent."""
        if event.type() == QEvent.Type.Show:
            key = next((k for k, (page, _c, _n) in self.pages.items() if page is watched), None)
            if key is not None:
                self.before_show(key)
        return False
