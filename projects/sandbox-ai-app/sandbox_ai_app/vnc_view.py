"""The live view of a sandbox's own browser (sandbox-ai's --browser): a VNC client of the app's own, in Qt.

The browser runs in its own container in the sandbox's pod, with a virtual display whose x11vnc listens only on a Unix
socket in a folder beside the run's directory that only you can open (RUN.browser-vnc/vnc.sock, recorded in run.json
and listed by `sandbox-ai list --json` as "browser": {"vnc": PATH}). This client talks to that socket directly
(QLocalSocket): no TCP port, no web view, no bridge - nothing of the app listens anywhere. A socket that is not
yours, not a socket, or in a folder others can open is refused before anything is sent to it.

The protocol is RFB 3.8 (3.3 and 3.7 too) with security type None only (the socket's folder is the access control),
a 32-bit true-colour pixel format and the encodings Raw, CopyRect and the DesktopSize pseudo-encoding - what x11vnc
sends over a socket on this machine, where bandwidth does not matter. Every size the server names is bounded (a
framebuffer at most MAX_SIZE pixels wide and high, a rectangle inside it, a clipboard text at most CUT_LIMIT bytes):
a server that says otherwise is disconnected.

The view (BrowserPanel, one per run): in the app a tab of Terminals beside its start's terminal (browser_tabs.py), or
in a window of its own (BrowserWindow; a tab opened in its own window). The display scaled to it, the mouse and the
keyboard - in a tab every key is the browser's; in a window plain Esc closes it like every extra window of the app
(Shift+Esc sends Esc to the browser) - and the clipboard only when "Share the clipboard" is ticked (off by default;
with the agent's control too, what you copy into the browser the agent can read there, and what it copies reaches
your clipboard).

Linux only for now: on macOS the socket lives in podman machine's VM, which macOS cannot connect to through a shared
folder (sandbox-ai refuses a browser for you there).
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import struct

from PySide6.QtCore import QObject, QRect, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import applog
from .i18n import N_, tr

MAX_SIZE = 8192
CUT_LIMIT = 1 << 20
NAME_LIMIT = 4096
RETRY_MS = 1000  # the socket is not there yet (the browser starts): looked for again this often
RETRIES = 120
ENCODING_RAW, ENCODING_COPYRECT, ENCODING_DESKTOP_SIZE = 0, 1, -223
ENCODINGS = (ENCODING_RAW, ENCODING_COPYRECT, ENCODING_DESKTOP_SIZE)
# 32 bits per pixel, depth 24, little endian, true colour, 255 per channel, red at bit 16: QImage.Format_RGB32.
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
BUTTONS = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.MiddleButton: 2, Qt.MouseButton.RightButton: 4}
KEYSYMS = {
    Qt.Key.Key_Backspace: 0xFF08, Qt.Key.Key_Tab: 0xFF09, Qt.Key.Key_Backtab: 0xFF09, Qt.Key.Key_Return: 0xFF0D,
    Qt.Key.Key_Enter: 0xFF8D, Qt.Key.Key_Escape: 0xFF1B, Qt.Key.Key_Insert: 0xFF63, Qt.Key.Key_Delete: 0xFFFF,
    Qt.Key.Key_Home: 0xFF50, Qt.Key.Key_End: 0xFF57, Qt.Key.Key_Left: 0xFF51, Qt.Key.Key_Up: 0xFF52,
    Qt.Key.Key_Right: 0xFF53, Qt.Key.Key_Down: 0xFF54, Qt.Key.Key_PageUp: 0xFF55, Qt.Key.Key_PageDown: 0xFF56,
    Qt.Key.Key_Shift: 0xFFE1, Qt.Key.Key_Control: 0xFFE3, Qt.Key.Key_Alt: 0xFFE9, Qt.Key.Key_AltGr: 0xFE03,
    Qt.Key.Key_Meta: 0xFFEB, Qt.Key.Key_CapsLock: 0xFFE5, Qt.Key.Key_Menu: 0xFF67, Qt.Key.Key_Print: 0xFF61,
    Qt.Key.Key_Space: 0x0020,
    **{getattr(Qt.Key, f"Key_F{n}"): 0xFFBE + n - 1 for n in range(1, 13)},
}


class RfbError(Exception):
    pass


def socket_problem(path):
    """Why the VNC socket PATH must not be used, or None: it is a socket of yours in a folder of yours that only you
    can open (what sandbox-ai makes: RUN.browser-vnc, 0700)."""
    try:
        path = Path(path)
        if not path.is_absolute() or len(str(path)) > NAME_LIMIT:
            return tr("not an absolute path")
        folder = os.lstat(path.parent)
        if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid() or folder.st_mode & 0o077:
            return tr("its folder is not yours alone")
        own = os.lstat(path)
    except FileNotFoundError:
        return tr("it is not there (yet)")
    except OSError as exc:
        return exc.strerror or str(exc)
    if not stat.S_ISSOCK(own.st_mode):
        return tr("not a socket")
    if own.st_uid != os.getuid():
        return tr("not yours")
    return None


def keysym(key, text):
    """The X keysym of a key event (Qt KEY, its TEXT), or None."""
    if key in KEYSYMS:
        return KEYSYMS[key]
    if text and len(text) == 1 and text.isprintable():
        code = ord(text)
        return code if code < 0x100 else 0x01000000 | code
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:  # with Ctrl, Qt gives no printable text
        return ord("a") + key - Qt.Key.Key_A
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return ord("0") + key - Qt.Key.Key_0
    return None


class RfbClient(QObject):
    """One RFB connection over a QLocalSocket. The protocol is a generator fed with exactly the bytes it asks for."""

    connected = Signal(int, int, str)  # width, height, the desktop's name
    resized = Signal(int, int)
    updated = Signal(QRect)
    cut_text = Signal(str)
    failed = Signal(str)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QLocalSocket(self)
        self.socket.readyRead.connect(self.read)
        self.socket.disconnected.connect(self.gone)
        self.socket.errorOccurred.connect(lambda _error: self.fail(self.socket.errorString()))
        self.image = QImage()
        self.buffer = bytearray()
        self.parser, self.need, self.ready, self.dead = None, 0, False, False
        self.protocol_error = False  # the last failure was the server's protocol (not its socket: closed, refused)

    def open(self, path):
        problem = socket_problem(path)
        if problem:
            self.fail(tr("The browser's display socket {path} is refused: {why}", path=str(path), why=problem))
            return False
        self.parser, self.buffer, self.ready, self.dead = self.protocol(), bytearray(), False, False
        self.protocol_error = False
        self.need = next(self.parser)
        applog.log("DEBUG", "browser", f"connecting to the browser's display at {path}")
        self.socket.connectToServer(str(path))
        return True

    def close(self):
        self.dead = True
        self.socket.abort()

    def fail(self, why):
        if self.dead:
            return
        applog.log("WARNING", "browser", f"the browser's display connection failed: {applog.redact(why)}")
        self.dead = True
        self.socket.abort()
        self.failed.emit(why)

    def gone(self):
        if not self.dead:
            applog.log("INFO", "browser", "the browser's display closed the connection")
            self.dead = True
            self.closed.emit()

    def send(self, data):
        if not self.dead and self.socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self.socket.write(bytes(data))

    def read(self):
        self.feed(bytes(self.socket.readAll()))

    def feed(self, data):
        """DATA from the server, into the protocol (also the tests' way in)."""
        if self.dead or self.parser is None:
            return
        self.buffer += data
        try:
            while len(self.buffer) >= self.need:
                chunk = bytes(self.buffer[:self.need])
                del self.buffer[:self.need]
                self.need = self.parser.send(chunk)
        except (RfbError, struct.error, StopIteration) as exc:
            self.protocol_error = True
            self.fail(str(exc) or tr("the server ended the protocol"))

    # -- the protocol --

    def protocol(self):
        version = yield 12
        if not version.startswith(b"RFB 003.") or not version.endswith(b"\n"):
            raise RfbError(tr("not a VNC server"))
        minor = int(version[8:11]) if version[8:11].isdigit() else 0
        if minor < 3:
            raise RfbError(tr("an RFB version this viewer does not speak"))
        minor = 8 if minor >= 8 else 7 if minor == 7 else 3
        self.send(b"RFB 003.%03d\n" % minor)
        if minor == 3:
            kind = struct.unpack(">I", (yield 4))[0]
            if kind == 0:
                raise RfbError((yield from self.reason()))
            if kind != 1:
                raise RfbError(tr("the server asks for a password; the browser's display needs none"))
        else:
            count = (yield 1)[0]
            if count == 0:
                raise RfbError((yield from self.reason()))
            types = yield count
            if 1 not in types:
                raise RfbError(tr("the server asks for a password; the browser's display needs none"))
            self.send(b"\x01")
            if minor == 8:
                result = struct.unpack(">I", (yield 4))[0]
                if result != 0:
                    raise RfbError((yield from self.reason()))
        self.send(b"\x01")  # ClientInit: shared (more viewers at once)
        width, height = struct.unpack(">HH", (yield 4))
        yield 16  # the server's pixel format: replaced by ours below
        length = struct.unpack(">I", (yield 4))[0]
        if length > NAME_LIMIT:
            raise RfbError(tr("the server's name is too long"))
        name = (yield length).decode("utf-8", "replace") if length else ""
        self.resize(width, height)
        self.send(b"\x00\x00\x00\x00" + PIXEL_FORMAT)  # SetPixelFormat
        self.send(struct.pack(">BxH", 2, len(ENCODINGS)) + b"".join(struct.pack(">i", e) for e in ENCODINGS))
        self.ready = True
        self.connected.emit(width, height, name)
        self.request(False)
        while True:
            kind = (yield 1)[0]
            if kind == 0:
                yield from self.framebuffer_update()
                self.request(True)
            elif kind == 1:  # SetColourMapEntries (never used with true colour): skipped
                _, _, count = struct.unpack(">BHH", (yield 5))
                if count:
                    yield count * 6
            elif kind == 2:  # Bell
                pass
            elif kind == 3:
                _, length = struct.unpack(">3sI", (yield 7))
                if length > CUT_LIMIT:
                    raise RfbError(tr("the server's clipboard text is too long"))
                text = (yield length) if length else b""
                self.cut_text.emit(text.decode("latin-1"))
            else:
                raise RfbError(tr("an RFB message this viewer does not know ({kind})", kind=kind))

    def reason(self):
        length = struct.unpack(">I", (yield 4))[0]
        text = (yield min(length, 4096)) if length else b""
        return tr("the server refused: {reason}", reason=text.decode("utf-8", "replace") or "?")

    def resize(self, width, height):
        if not (0 < width <= MAX_SIZE and 0 < height <= MAX_SIZE):
            raise RfbError(tr("the server names a display of {width}x{height}", width=width, height=height))
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor(0, 0, 0))
        if not self.image.isNull():
            with_painter(image, lambda p: p.drawImage(0, 0, self.image))
        self.image = image
        self.resized.emit(width, height)

    def framebuffer_update(self):
        _, count = struct.unpack(">BH", (yield 3))
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack(">HHHHi", (yield 12))
            if encoding == ENCODING_DESKTOP_SIZE:
                self.resize(width, height)
                continue
            area = QRect(x, y, width, height)
            if width == 0 or height == 0:
                if encoding == ENCODING_COPYRECT:
                    yield 4  # (its source position comes all the same: read, or the stream is out of step)
                elif encoding != ENCODING_RAW:
                    raise RfbError(tr("an encoding this viewer did not ask for ({encoding})", encoding=encoding))
                continue
            if x + width > self.image.width() or y + height > self.image.height():
                raise RfbError(tr("the server sends a rectangle outside its display"))
            if encoding == ENCODING_RAW:
                data = yield width * height * 4
                tile = QImage(data, width, height, width * 4, QImage.Format.Format_RGB32)
                with_painter(self.image, lambda p, t=tile: p.drawImage(x, y, t))
            elif encoding == ENCODING_COPYRECT:
                source_x, source_y = struct.unpack(">HH", (yield 4))
                if source_x + width > self.image.width() or source_y + height > self.image.height():
                    raise RfbError(tr("the server copies from outside its display"))
                copy = self.image.copy(source_x, source_y, width, height)
                with_painter(self.image, lambda p, c=copy: p.drawImage(x, y, c))
            else:
                raise RfbError(tr("an encoding this viewer did not ask for ({encoding})", encoding=encoding))
            self.updated.emit(area)

    # -- what the viewer sends --

    def request(self, incremental=True):
        if self.ready and not self.image.isNull():
            self.send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.image.width(),
                                  self.image.height()))

    def key(self, symbol, down):
        if self.ready:
            self.send(struct.pack(">BBxxI", 4, 1 if down else 0, symbol & 0xFFFFFFFF))

    def pointer(self, x, y, mask):
        if self.ready and not self.image.isNull():
            x = max(0, min(int(x), self.image.width() - 1))
            y = max(0, min(int(y), self.image.height() - 1))
            self.send(struct.pack(">BBHH", 5, mask & 0xFF, x, y))

    def cut(self, text):
        if self.ready:
            data = text.encode("latin-1", "replace")[:CUT_LIMIT]
            self.send(struct.pack(">BxxxI", 6, len(data)) + data)


def with_painter(image, draw):
    painter = QPainter(image)
    try:
        draw(painter)
    finally:
        painter.end()


class VncView(QWidget):
    """The display, scaled to the widget with its aspect kept; mouse and keyboard go to the browser."""

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client, self.buttons, self.pressed = client, 0, {}
        self.esc_closes = True  # in a window of its own: plain Esc closes it; in a tab every Esc is the browser's
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName(tr("The browser's display"))
        client.updated.connect(lambda _rect: self.update())
        client.resized.connect(lambda _w, _h: self.update())

    def target(self):
        """Where the display is drawn in the widget (scaled, centred)."""
        image = self.client.image
        if image.isNull():
            return QRectF()
        scale = min(self.width() / image.width(), self.height() / image.height())
        width, height = image.width() * scale, image.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def to_display(self, point):
        area = self.target()
        if area.isEmpty():
            return None
        image = self.client.image
        return ((point.x() - area.x()) * image.width() / area.width(),
                (point.y() - area.y()) * image.height() / area.height())

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(32, 32, 32))
        if not self.client.image.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.target(), self.client.image)
        painter.end()

    def send_pointer(self, event, extra=0):
        where = self.to_display(event.position())
        if where is not None:
            self.client.pointer(where[0], where[1], self.buttons | extra)

    def mousePressEvent(self, event):  # noqa: N802
        self.setFocus()
        self.buttons |= BUTTONS.get(event.button(), 0)
        self.send_pointer(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self.buttons &= ~BUTTONS.get(event.button(), 0)
        self.send_pointer(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        self.send_pointer(event)

    def wheelEvent(self, event):  # noqa: N802
        delta = event.angleDelta()
        bit = 8 if delta.y() > 0 else 16 if delta.y() < 0 else 32 if delta.x() > 0 else 64 if delta.x() < 0 else 0
        if bit:
            self.send_pointer(event, bit)  # pressed and released: one step of the wheel
            self.send_pointer(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier and \
                self.esc_closes:
            event.ignore()  # plain Esc closes the window (as every extra window's); Shift+Esc goes to the browser
            return
        symbol = keysym(event.key(), event.text())
        if symbol is None:
            event.ignore()
            return
        self.pressed[event.key()] = symbol
        self.client.key(symbol, True)
        event.accept()

    def keyReleaseEvent(self, event):  # noqa: N802
        symbol = self.pressed.pop(event.key(), None)
        if symbol is None:
            event.ignore()
            return
        self.client.key(symbol, False)
        event.accept()

    def focusNextPrevChild(self, _next):  # noqa: N802 - Tab goes to the browser, not to the next field
        return False

    def focusOutEvent(self, event):  # noqa: N802
        for symbol in self.pressed.values():  # nothing stays held in the browser
            self.client.key(symbol, False)
        self.pressed.clear()
        super().focusOutEvent(event)


CONTROL_NOTES = {
    "human": N_("Only you drive this browser; the agent cannot reach it."),
    "both": N_("You and the agent drive this browser: whatever you sign in to here, the agent can use."),
    "agent": N_("The agent drives this browser; you watch (and may take over)."),
}
# A browser of a start through Tor (run.json "browser": {"tor": true}): said in its tab and window, where you browse.
TOR_NOTE = N_("Behind Tor this is {name}, not Tor Browser: sites can fingerprint it and link its visits to each other "
              "(and to you, if you sign in). Use Tor Browser for anonymity.")
KIND_NAMES = {"chromium": "Chromium", "firefox": "Firefox ESR"}


class BrowserPanel(QWidget):
    """The live browser of one run: its display, a status line, Share the clipboard, Reconnect. A tab of the
    Terminals page beside its start's terminal (browser_tabs.py), or the whole of a window of its own (BrowserWindow,
    or a tab opened in its own window). INSTANCE_KEY and NAME: the start it shows (Sandboxes & sessions)."""

    def __init__(self, title, path, control="human", parent=None, flags=Qt.WindowType.Widget, in_window=False,
                 tor=None):
        super().__init__(parent, flags)
        self.path, self.control, self.retries, self.title = str(path), control, 0, title
        self.tor = tor  # behind Tor: the browser's kind (chromium, firefox), else None
        self.instance_key, self.name, self.ended_at, self.down = None, "", None, False
        self.address = None  # a URL to type into its address bar once connected (type_address)
        self.setObjectName("browserPanel")
        self.client = RfbClient(self)
        self.view = VncView(self.client, self)
        self.setFocusProxy(self.view)
        self.status = QLabel(tr("Connecting to the browser's display…"))
        self.status.setWordWrap(True)
        self.share = QCheckBox(tr("Share the clipboard"))
        self.share.setToolTip(tr("Off: nothing you copy reaches the browser, nothing copied there reaches your "
                                 "clipboard. On: both ways, while this window is open"))
        self.share.toggled.connect(self.share_toggled)
        self.again = QPushButton(tr("Reconnect"))
        self.again.setToolTip(tr("Connect to the browser's display again"))
        self.again.clicked.connect(lambda _=False: self.connect_now())
        # In a window of its own that was a tab: back into the Terminals' tabs (set by browser_tabs.py).
        self.back = QPushButton(tr("Back into the tabs"))
        self.back.setToolTip(tr("Show this browser as a tab of Terminals again, beside its session's terminal"))
        self.back.hide()
        self.note = QLabel()
        self.note.setWordWrap(True)
        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.share)
        bar.addWidget(self.again)
        bar.addWidget(self.back)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.note)
        layout.addWidget(self.view, 1)
        self.set_in_window(in_window)
        self.client.connected.connect(self.on_connected)
        self.client.failed.connect(self.lost)
        self.client.closed.connect(lambda: self.lost(tr("The browser's display was closed (the run ended?)")))
        self.client.cut_text.connect(self.on_cut_text)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.connect_now)
        QGuiApplication.clipboard().dataChanged.connect(self.clipboard_changed)
        self.connect_now()

    @property
    def running(self):
        """Its start runs (as far as the app knows): False once it ended (browser_tabs.py: the tab closes then)."""
        return self.ended_at is None

    def set_in_window(self, in_window):
        """In a window of its own plain Esc closes the window (Shift+Esc goes to the browser); in a tab every key is
        the browser's."""
        self.view.esc_closes = bool(in_window)
        self.note.setText(tr(CONTROL_NOTES.get(self.control, CONTROL_NOTES["human"])) + (
            " " + tr("Esc closes this window; Shift+Esc sends Esc to the browser.") if in_window else "") + (
            " " + tr(TOR_NOTE, name=KIND_NAMES.get(self.tor, "Chromium")) if self.tor else ""))

    def connect_now(self):
        if self.down:
            return
        self.timer.stop()
        self.client.close()
        problem = socket_problem(self.path)
        if problem == tr("it is not there (yet)") and self.retries < RETRIES:
            self.retries += 1
            applog.log("DEBUG", "browser", f"the browser's display {self.path} is not there yet (try {self.retries} "
                                           f"of {RETRIES})")
            self.status.setText(tr("Waiting for the browser's display (it starts)…"))
            self.timer.start(RETRY_MS)
            return
        if problem:
            self.on_failed(tr("The browser's display socket {path} is refused: {why}", path=self.path, why=problem))
            return
        self.client.open(self.path)

    def retarget(self, path):
        """The start runs again with another run (a resumed session): its display is at PATH now."""
        if str(path) != self.path:
            applog.log("INFO", "browser", f"the browser of {self.name or '?'}: its display moved to {path} (a new "
                                          "run of its start)")
            self.path, self.retries = str(path), 0
            self.connect_now()

    def on_connected(self, width, height, _name):
        applog.log("INFO", "browser", f"connected to the browser's display {self.path}: {width}x{height} (control "
                                      f"{getattr(self, 'control', '?')})")
        self.retries = 0
        self.status.setText(tr("Connected: {width}×{height}", width=width, height=height))
        if self.isVisible():
            self.view.setFocus()
        if self.share.isChecked():
            self.clipboard_changed()
        if self.address:
            self.type_address(self.address)

    def type_address(self, url):
        """URL into the browser's address bar - Ctrl+L, the URL, Enter (Chromium's and Firefox's) - now, or once
        connected: an address of the sandbox's loopback from its terminal (a web UI in it), which only this browser
        reaches."""
        if not self.client.ready:
            self.address = str(url)
            return
        self.address = None
        applog.log("INFO", "browser", f"the browser of {self.name or '?'}: an address of the sandbox typed into its "
                                      "address bar")
        control, letter = KEYSYMS[Qt.Key.Key_Control], ord("l")
        for symbol, down in ((control, True), (letter, True), (letter, False), (control, False)):
            self.client.key(symbol, down)
        for char in str(url):
            symbol = ord(char) if ord(char) < 0x100 else 0x01000000 | ord(char)
            self.client.key(symbol, True)
            self.client.key(symbol, False)
        self.client.key(KEYSYMS[Qt.Key.Key_Return], True)
        self.client.key(KEYSYMS[Qt.Key.Key_Return], False)

    def on_failed(self, why):
        self.status.setText(why)

    def lost(self, why):
        """The connection failed or was closed. While its start runs the display may come back (the browser's
        container starts it again when its x11vnc or Xvfb ended, on the same socket): connected again by itself,
        RETRIES times RETRY_MS apart at most; a server that breaks the protocol is not asked again."""
        if self.down:
            return
        if not self.running or self.client.protocol_error or self.retries >= RETRIES:
            self.on_failed(why)
            return
        self.retries += 1
        applog.log("DEBUG", "browser", f"the browser's display {self.path}: {applog.redact(why)}; connecting again "
                                       f"(try {self.retries} of {RETRIES})")
        self.status.setText(tr("{why} - connecting again…", why=why))
        self.timer.start(RETRY_MS)

    def share_toggled(self, on):
        if on:
            self.clipboard_changed()

    def on_cut_text(self, text):
        if self.share.isChecked():
            self.received = text  # (its own dataChanged must not send it back: an echo the server may answer again)
            QGuiApplication.clipboard().setText(text)

    def clipboard_changed(self):
        if self.share.isChecked() and self.client.ready:
            text = QGuiApplication.clipboard().text()
            if text and text != getattr(self, "received", None):
                self.client.cut(text)
            self.received = None

    def shutdown(self):
        """Its connection ends (the tab or the window closes); once."""
        if self.down:
            return
        self.down = True
        self.timer.stop()
        self.client.close()
        try:
            QGuiApplication.clipboard().dataChanged.disconnect(self.clipboard_changed)
        except (RuntimeError, TypeError):
            pass

    def closeEvent(self, event):  # noqa: N802
        self.shutdown()
        super().closeEvent(event)


class BrowserWindow(BrowserPanel):
    """The live browser of one run in a window of its own: an [x] at its top right, Esc closes it."""

    def __init__(self, title, path, control="human", parent=None, tor=None):
        super().__init__(title, path, control, parent, Qt.WindowType.Window, in_window=True, tor=tor)
        from .widgets import close_controls
        self.setWindowTitle(title)
        self.setObjectName("browserWindow")
        self.resize(1080, 760)
        close_controls(self)  # an [x] at its top right, Esc closes it


_open = {}  # {socket path: BrowserWindow}: one window per run's browser


def browser_record(instance):
    """The browser of a listed start (sandbox-ai list --json "browser"), or None."""
    record = getattr(instance, "record", None) or {}
    browser = record.get("browser") if isinstance(record, dict) else None
    if not isinstance(browser, dict) or browser.get("kind") not in ("chromium", "firefox") or \
            browser.get("control") not in ("human", "agent", "both"):
        return None
    vnc = browser.get("vnc")
    vnc = vnc if isinstance(vnc, str) and vnc.startswith("/") and vnc.endswith("/vnc.sock") and \
        len(vnc) < NAME_LIMIT and "\n" not in vnc else None
    return {"kind": browser["kind"], "control": browser["control"], "vnc": vnc, "tor": browser.get("tor") is True}


def tor_of(browser):
    """The browser's kind when it is behind Tor (BrowserPanel's TOR), else None."""
    return browser["kind"] if browser.get("tor") else None


def why_not(instance):
    """Why the Browser button cannot show INSTANCE's browser, or None."""
    if instance is None:
        return tr("Select a start first.")
    browser = browser_record(instance)
    if browser is None:
        return tr("This start has no browser of its own (start it with --browser).")
    if getattr(instance, "host", "local") != "local":
        return tr("Its browser's display is a socket on that host: only a browser of this machine is shown here.")
    if not browser.get("vnc"):
        return tr("Only the agent drives this browser (--browser-control agent): it has no display for you.")
    if getattr(instance, "state", None) != "running":
        return tr("It does not run.")
    return None


def open_browser(instance, parent=None):
    """The window of INSTANCE's browser (shown and raised; one per run). None when why_not says no."""
    refused = why_not(instance)
    if refused is not None:
        applog.log("INFO", "browser", f"the browser of {getattr(instance, 'name', '?')} is not opened: "
                                      f"{applog.redact(refused)}")
        return None
    browser = browser_record(instance)
    path = browser["vnc"]
    window = _open.get(path)
    try:
        if window is not None and window.isVisible():
            window.raise_()
            window.activateWindow()
            return window
    except RuntimeError:  # (deleted meanwhile)
        pass
    title = tr("Browser of {name}", name=str(getattr(instance, "name", "") or getattr(instance, "run", "")))
    window = BrowserWindow(title, path, browser.get("control") or "human", parent, tor=tor_of(browser))
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    window.destroyed.connect(lambda _=None, p=path: _open.pop(p, None))
    _open[path] = window
    window.show()
    return window
