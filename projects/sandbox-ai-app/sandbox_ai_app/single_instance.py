"""One app per user: a second start (the login's autostart and a start by hand, two clicks on the menu entry) would be a
second window writing the same state file - whichever quit last overwrote the other's unapplied board changes - and the
same crash record. So the first app listens on a local socket in the user's own runtime directory; a second start
asks it to show its window (a start for the tray, --hidden, asks nothing) and ends.

The socket lies in $XDG_RUNTIME_DIR (0700, the user's own), else beside app.log (~/.local/state/sandbox-ai, 0700);
only the user can reach it. What is sent is one word, "show" or "hidden"; anything else is ignored."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import applog

NAME = "sandbox-ai-app.sock"
CONNECT_MS = 500
MESSAGES = (b"show", b"hidden")


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

    def claim(self, hidden=False):
        """True: this is the only app (now listening). False: another one runs; it was asked to show its window
        (unless HIDDEN)."""
        socket = QLocalSocket()
        socket.connectToServer(str(self.path))
        if socket.waitForConnected(CONNECT_MS):
            socket.write(b"hidden" if hidden else b"show")
            socket.flush()
            socket.waitForBytesWritten(CONNECT_MS)  # on the GUI thread on purpose: at start, no window yet, bounded
            socket.disconnectFromServer()
            return False
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
        QLocalServer.removeServer(str(self.path))  # a socket left by an app that was killed: nobody answers on it
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(str(self.path)):
            # No guard (an odd runtime directory): the app still starts - a second one is possible then.
            applog.log("WARNING", "instance", f"no single-instance guard: {self.server.errorString()}")
            self.server = None
            return True
        self.server.newConnection.connect(self.connected)
        return True

    def connected(self):
        while self.server is not None and self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            client.readyRead.connect(lambda c=client: self.read(c))
            client.disconnected.connect(client.deleteLater)

    def read(self, client):
        word = bytes(client.readAll().data()).strip()[:16]
        if word in MESSAGES:
            self.asked.emit(word.decode())
        client.disconnectFromServer()

    def release(self):
        if self.server is not None:
            self.server.close()
            self.server = None
