"""One app per user: a second start (the login's autostart and a start by hand, two clicks on the menu entry) would be a
second window writing the same state file - whichever quit last overwrote the other's unapplied board changes - and the
same crash record. So the first app listens on a local socket in the user's own runtime directory; a second start
asks it to show its window (a start for the tray, --hidden, asks nothing) and ends.

The socket lies in $XDG_RUNTIME_DIR (0700, the user's own), else beside app.log (~/.local/state/sandbox-ai, 0700);
only the user can reach it. What is sent is one word, "show" or "hidden"; anything else is ignored.

The first app also holds an flock on the lock file beside the socket for as long as it runs: two starts at the same
moment (the autostart and a click, a double click) both found nobody listening, and the later one removed the
earlier one's socket as one left by a killed app - two apps. A start that cannot take the lock is a second one."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import applog

NAME = "sandbox-ai-app.sock"
CONNECT_MS = 500
MESSAGES = (b"show", b"hidden")
ASK_FOR = 5.0  # seconds a second start tries to reach the first (which may not listen yet: it started a moment ago)


def socket_path(environ=None):
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        return Path(runtime) / NAME
    return applog.log_path(environ).with_name(NAME)


class Instance(QObject):
    """The app's one instance. claim() is True when this process is the first (it listens then); else it asked the
    first to show its window. shown(word) is sent when a later start asked for it."""
    asked = Signal(str)

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = Path(path) if path else socket_path()
        self.server = None
        self.lock_fd = None

    def ask(self, hidden):
        """True when an app listens on the socket and was asked to show its window (or stay hidden)."""
        socket = QLocalSocket()
        socket.connectToServer(str(self.path))
        if not socket.waitForConnected(CONNECT_MS):
            return False
        socket.write(b"hidden" if hidden else b"show")
        socket.flush()
        socket.waitForBytesWritten(CONNECT_MS)  # on the GUI thread on purpose: at start, no window yet, bounded
        socket.disconnectFromServer()
        applog.log("INFO", "instance", f"another app runs already ({self.path}): it was asked to "
                                       f"{'stay hidden' if hidden else 'show its window'}; this one ends")
        return True

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
        """True: this is the only app (now listening). False: another one runs; it was asked to show its window
        (unless HIDDEN)."""
        if self.ask(hidden):
            return False
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            applog.swallowed("instance", f"the folder of {self.path} could not be made", exc)
        held = self.lock()
        if held is False:  # another start holds it: the first app, which may not listen yet
            deadline = time.monotonic() + ASK_FOR
            while time.monotonic() < deadline:
                if self.ask(hidden):
                    return False
                time.sleep(0.1)  # on the GUI thread on purpose: at start, no window yet, bounded by ASK_FOR
            applog.log("WARNING", "instance", f"another app holds {self.path}.lock but does not answer on its socket "
                                              f"within {ASK_FOR:g} s; this one ends")
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
        else:
            applog.log("DEBUG", "instance", f"an unknown message on the single-instance socket is ignored: {word!r}")
        client.disconnectFromServer()

    def release(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # (closing it lets the lock go)
            self.lock_fd = None
