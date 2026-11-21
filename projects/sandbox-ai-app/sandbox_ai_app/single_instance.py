"""One app per user: a second start (the login's autostart and a start by hand, two clicks on the menu entry) would be a
second window writing the same state file - whichever quit last overwrote the other's unapplied board changes - and the
same crash record. So the first app listens on a local socket in the user's own runtime directory; a second start
asks it to show its window (a start for the tray, --hidden, asks nothing) and ends.

The socket lies in $XDG_RUNTIME_DIR (0700, the user's own), else beside app.log (~/.local/state/sandbox-ai, 0700);
only the user can reach it. What is sent is one word per connection (instance_probe.py): "show" or "hidden" (every
app), "info" and "quit" (since 0.1.99); anything else is ignored.

After an update the app that runs is the older one (its window left open, in the tray, the login's autostart): a start
used to show it, and the human kept seeing the old version (2026-11-18). Now a start first asks what runs ("info"): the
same app is shown as before; an older one, or one from another installation, is asked to quit as its own Quit does
(its questions; the Board's changes kept) and this start goes on once it has ended; if it stays (the human kept it, or
it does not end within QUIT_WAIT), or is too old to be asked (it ignores "info"), this start says which one runs, from
where and how to quit it. A start for the tray never takes over.

The first app also holds an flock on the lock file beside the socket for as long as it runs: two starts at the same
moment (the autostart and a click, a double click) both found nobody listening, and the later one removed the
earlier one's socket as one left by a killed app - two apps. A start that cannot take the lock is a second one."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer

from . import __version__, applog, instance_probe
from .i18n import tr

NAME = instance_probe.NAME
MESSAGES = (b"show", b"hidden")  # what is passed on (asked); "info" and "quit" are answered here
ASK_FOR = 5.0  # seconds a second start tries to reach the first (which may not listen yet: it started a moment ago)
QUIT_WAIT = 15.0  # seconds a start waits for an older app to quit (its questions answered) and end


def socket_path(environ=None):
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        return Path(runtime) / NAME
    return applog.log_path(environ).with_name(NAME)


class Instance(QObject):
    """The app's one instance. claim() is True when this process is the first (it listens then); else another app runs
    and was told (notice: what the human is told about it). asked(word) is sent when a later start asked to show the
    window ("show") or nothing ("hidden"); quit_handler() (the window's Quit, True when it quits) runs when a newer
    start asked this app to make way."""
    asked = Signal(str)

    def __init__(self, path=None, parent=None, version=None, location=None):
        super().__init__(parent)
        self.path = Path(path) if path else socket_path()
        self.server = None
        self.lock_fd = None
        self.version = version or __version__  # this app's (said to a later start, compared with the running one's)
        self.location = str(location or instance_probe.HERE)
        self.details = {}  # what "info" also says (main: sandbox-ai's version)
        self.quit_handler = None
        self.notice = None  # claim() False: (kind, the Running app) the human is told about, else None
        self.alert = False  # the notice is a problem (an older app stays): shown in a message box too

    def ask(self, hidden):
        """True when an app listens on the socket and was told about this start (see meet); False when none does, or
        one made way (this start is the app then)."""
        running = instance_probe.ask(self.path)  # on the GUI thread on purpose: at start, no window yet, bounded
        if running is None:
            return False
        return not self.meet(running, hidden)

    def tell(self, word):
        instance_probe.exchange(self.path, word, wait=0)  # on the GUI thread on purpose: at start, bounded

    def meet(self, running, hidden):
        """What this start does about the app RUNNING (instance_probe.verdict); True when that app made way."""
        action = instance_probe.verdict(running, hidden, self.version, self.location)
        applog.log("INFO", "instance", f"another app runs already ({self.path}): {running.named()}"
                                       f"{instance_probe.where_of(running)}, {running.protocol}; this start: {action}")
        if action != "takeover":
            self.tell(b"hidden" if hidden else b"show")
            if action in ("newer", "old"):
                self.notice, self.alert = (action, running), action == "old"
            return False
        deadline = time.monotonic() + QUIT_WAIT
        # on the GUI thread on purpose: at start, no window yet, bounded by QUIT_WAIT
        answer, _ = instance_probe.exchange(self.path, b"quit", wait=QUIT_WAIT)
        if answer is not None and answer.strip() == b"declined":
            applog.log("INFO", "instance", f"{running.named()} stays (its Quit was declined); this one ends")
            self.notice, self.alert = ("stays", running), True
            return False
        while time.monotonic() < deadline:  # "quitting" (or it ended before it could say so): until it has gone
            if self.gone():
                applog.log("INFO", "instance", f"{running.named()} made way for this start ({self.version})")
                return True
            time.sleep(0.1)  # on the GUI thread on purpose: at start, no window yet, bounded by QUIT_WAIT
        applog.log("WARNING", "instance", f"{running.named()} did not end within {QUIT_WAIT:g} s; this one ends")
        self.notice, self.alert = ("stays", running), True
        return False

    def gone(self):
        """True once the app that made way has ended: its lock is free (taken now), or - no lock possible - nobody
        listens any more."""
        held = self.lock() if self.lock_fd is None else True
        if held is None:
            return instance_probe.connect(self.path) is None
        return held

    def notice_text(self):
        """What the human is told about the app that runs (claim() False), in the language chosen; None if nothing."""
        if self.notice is None:
            return None
        kind, running = self.notice
        path = running.path or "?"
        if kind == "newer":
            return tr("sandbox-ai-app {version} runs already (from {path}), newer than this one ({mine}): its window "
                      "is brought to the front", version=running.version or "?", path=path, mine=self.version)
        if kind == "stays":
            return tr("sandbox-ai-app {version} is still running (from {path}); quit it to use {mine}: Quit in its "
                      "File or tray menu, then start sandbox-ai-app again", version=running.version or "?",
                      path=path, mine=self.version)
        named = (tr("sandbox-ai-app {version}", version=running.version) if running.version
                 else tr("An older sandbox-ai-app"))
        if running.pid:
            return tr("{named} is still running (from {path}, process {pid}); it is too old to make way by itself: "
                      "quit it to use {mine} - Quit in its File or tray menu (its window is in front now), or in a "
                      "terminal: kill {pid} - then start sandbox-ai-app again", named=named, path=path,
                      pid=running.pid, mine=self.version)
        return tr("{named} is still running; it is too old to make way by itself: quit it to use {mine} - Quit in its "
                  "File or tray menu (its window is in front now) - then start sandbox-ai-app again", named=named,
                  mine=self.version)

    def lock(self):
        """True: this process holds the lock of the first app now; False: another process holds it; None: no lock
        could be made (an odd folder) - the socket alone decides then, as before."""
        try:
            fd = os.open(self.path.with_name(self.path.name + ".lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            applog.swallowed("instance", f"the lock beside {self.path} could not be made", exc)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError as exc:
            os.close(fd)
            applog.swallowed("instance", f"the lock beside {self.path} could not be taken", exc)
            return None
        self.lock_fd = fd  # held until release() or the end of the process (never inherited: O_CLOEXEC)
        return True

    def claim(self, hidden=False):
        """True: this is the only app (now listening; an older one may have made way). False: another one runs; it
        was asked to show its window (unless HIDDEN), and notice says what the human is told about it."""
        if self.ask(hidden):
            return False
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            applog.swallowed("instance", f"the folder of {self.path} could not be made", exc)
        held = True if self.lock_fd is not None else self.lock()  # (taken already: an older app made way)
        if held is False:  # another start holds it: the first app, which may not listen yet
            deadline = time.monotonic() + ASK_FOR
            while time.monotonic() < deadline:
                if self.ask(hidden):
                    return False
                if self.lock_fd is not None:  # it made way
                    break
                time.sleep(0.1)  # on the GUI thread on purpose: at start, no window yet, bounded by ASK_FOR
            else:
                applog.log("WARNING", "instance", f"another app holds {self.path}.lock but does not answer on its "
                                                  f"socket within {ASK_FOR:g} s; this one ends")
                return False
        QLocalServer.removeServer(str(self.path))  # a socket left by an app that was killed: nobody answers on it
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(str(self.path)):
            # No guard (an odd runtime directory): the app still starts - a second one is possible then.
            applog.log("WARNING", "instance", f"no single-instance guard: {self.server.errorString()}")
            self.server = None
            return True
        self.server.newConnection.connect(self.connected)
        applog.log("DEBUG", "instance", f"the only app: listening on {self.path}")
        return True

    def connected(self):
        while self.server is not None and self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            client.readyRead.connect(lambda c=client: self.read(c))
            client.disconnected.connect(client.deleteLater)

    def read(self, client):
        word = bytes(client.readAll().data()).strip()[:16]
        if word in MESSAGES:
            applog.log("INFO", "instance", f"a second start of the app asked this one: {word.decode()}")
            self.asked.emit(word.decode())
        elif word == b"info":  # what this app is: a later start decides with it (instance_probe.verdict)
            said = {"app": self.version, "path": self.location, "pid": os.getpid(), **self.details}
            client.write(json.dumps(said).encode() + b"\n")
            client.flush()
        elif word == b"quit":  # a newer start: this app makes way as its own Quit does (after this read: it may ask)
            applog.log("INFO", "instance", "a newer start of the app asked this one to quit")
            QTimer.singleShot(0, lambda c=client: self.make_way(c))
            return  # answered there
        else:
            applog.log("DEBUG", "instance", f"an unknown message on the single-instance socket is ignored: {word!r}")
        client.disconnectFromServer()

    def make_way(self, client):
        """Quit for a newer start (quit_handler: the window's Quit with its questions); the start is told whether
        this app quits."""
        try:
            quits = bool(self.quit_handler()) if self.quit_handler is not None else False
        except Exception as exc:  # noqa: BLE001 - the app stays; the start is told so
            applog.swallowed("instance", "the Quit a newer start asked for failed", exc)
            quits = False
        applog.log("INFO", "instance", f"asked to make way for a newer start: {'quits' if quits else 'stays'}")
        try:
            client.write(b"quitting\n" if quits else b"declined\n")
            client.flush()
            client.disconnectFromServer()
        except RuntimeError:  # the start gave up waiting: its connection is gone
            pass

    def release(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # (closing it lets the lock go)
            self.lock_fd = None
