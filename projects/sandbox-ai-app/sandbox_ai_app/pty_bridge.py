"""A command in a pseudo-terminal, and its bridge to a terminal web page (xterm.js over QWebChannel).

Pty needs no Qt: it starts ARGV (a list, never a shell line) as the session leader of a new pseudo-
terminal, reads and writes its master side without blocking, resizes it (TIOCSWINSZ, the kernel sends
SIGWINCH) and ends it (SIGHUP to its process group, SIGKILL if it does not go).

PtyBridge needs only QtCore: a QSocketNotifier on the master descriptor, output and input as base64
text (the web page's QWebChannel carries strings), and a backlog until the page says it is ready. The page
acknowledges every chunk once it has shown it (ack); while more than ACK_LIMIT characters are not
acknowledged, or the backlog is full, the terminal is not read: the command waits (its writes block), and
nothing is lost or queued without bound.

Output goes to the page in frames (Frames): what the command writes in one go reaches the terminal in pieces (a
pseudo-terminal hands over 4 KiB per read; tmux passes an interface's redraw on in pieces of its own), and xterm.js
paints whatever it has when the browser draws its next frame. A full-screen interface redraws by erasing its rows and
writing them again (Ink: Qwen Code, Gemini CLI, Claude Code), so a paint between two pieces showed erased rows - the
flicker. The bridge sends what it has once the output has been quiet for FRAME_QUIET (at most FRAME_HOLD after its first
byte, FRAME_LIMIT bytes at most), and holds an unfinished synchronized update (DEC mode 2026, which xterm.js also
honours) for up to SYNC_HOLD: each frame is one write, parsed whole and painted once.
"""
from __future__ import annotations

import base64
import binascii
import errno
import fcntl
import os
import signal
import struct
import subprocess
import sys
import termios
import time

# The child makes the new terminal its controlling terminal and then runs the command: a tiny Python
# program instead of preexec_fn (which is unsafe in a process with threads, as every Qt application is).
CONTROLLING_TTY = ("import fcntl, os, sys, termios\n"
                   "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
                   "try:\n"
                   "    os.execvp(sys.argv[1], sys.argv[1:])\n"
                   "except OSError as exc:\n"
                   "    sys.stderr.write(f'cannot run {sys.argv[1]!r}: {exc.strerror}\\r\\n')\n"
                   "    sys.exit(127)\n")
INPUT_LIMIT = 1 << 20  # bytes per write from the page
BACKLOG_LIMIT = 4 << 20  # output kept until the page is ready (base64 characters)
ACK_LIMIT = 1 << 20  # output sent to the page and not yet acknowledged (base64 characters) before reading stops
MAX_SIZE = 1000  # columns and rows
FRAME_QUIET = 0.003  # s without output: the frame is complete (its pieces follow one another within 1-2 ms)
FRAME_HOLD = 0.02  # s at most from a frame's first byte (steady output still reaches the page 50 times a second)
SYNC_HOLD = 0.1  # s at most while a synchronized update is open (xterm.js itself waits up to 1 s for its end)
FRAME_LIMIT = 64 << 10  # bytes: sent at once
SYNC_BEGIN, SYNC_END = b"\x1b[?2026h", b"\x1b[?2026l"


def set_size(fd, cols, rows):
    cols, rows = max(1, min(int(cols), MAX_SIZE)), max(1, min(int(rows), MAX_SIZE))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return cols, rows


def get_size(fd):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return cols, rows


class Frames:
    """Output gathered into frames for the page: add() what was read, due() the seconds until it goes (None: nothing
    waits; 0: now), take() it. No Qt: the bridge runs the timer."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.chunks, self.size = clock, [], 0
        self.first = self.last = 0.0
        self.synced, self.tail = False, b""  # a synchronized update is open; the end of what came, for a split mark

    def add(self, data):
        now = self.clock()
        if not self.chunks:
            self.first = now
        self.chunks.append(data)
        self.size += len(data)
        self.last = now
        window = self.tail + data
        begin, end = window.rfind(SYNC_BEGIN), window.rfind(SYNC_END)
        if begin >= 0 or end >= 0:
            self.synced = begin > end
        self.tail = window[-(len(SYNC_BEGIN) - 1):]

    def due(self):
        if not self.chunks:
            return None
        if self.size >= FRAME_LIMIT:
            return 0
        deadline = self.first + SYNC_HOLD if self.synced else min(self.last + FRAME_QUIET, self.first + FRAME_HOLD)
        return max(0, deadline - self.clock())

    def take(self):
        if self.synced and self.clock() - self.first >= SYNC_HOLD:
            self.synced = False  # never ended: over, as xterm.js ends it after its own wait
        data = b"".join(self.chunks)
        self.chunks, self.size = [], 0
        return data


class Pty:
    """ARGV in a new pseudo-terminal of COLS x ROWS."""

    def __init__(self, argv, cols=80, rows=24, env=None, cwd=None):
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("the command is a non-empty list of strings")
        master, slave = os.openpty()
        try:
            set_size(slave, cols, rows)
            environment = dict(os.environ if env is None else env)
            environment["TERM"] = "xterm-256color"
            environment.setdefault("COLORTERM", "truecolor")
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-c", CONTROLLING_TTY, *argv], stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True, env=environment, cwd=cwd)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.fd, self.pending, self.eof = master, b"", False

    @property
    def pid(self):
        return self.process.pid

    def read(self, size=65536):
        """Bytes the command wrote; b"" when there is nothing now, None at the end."""
        if self.fd is None or self.eof:
            return None
        try:
            data = os.read(self.fd, size)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:  # Linux: every process on the terminal has closed it
                self.eof = True
                return None
            raise
        if not data:
            self.eof = True
            return None
        return data

    def write(self, data):
        """Queue DATA for the command; returns how many bytes are still waiting (the terminal is full)."""
        if self.fd is None:
            return 0
        self.pending += data
        return self.flush()

    def flush(self):
        while self.pending and self.fd is not None:
            try:
                written = os.write(self.fd, self.pending)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    self.pending = b""
                    break
                raise
            self.pending = self.pending[written:]
        return len(self.pending)

    def resize(self, cols, rows):
        if self.fd is not None:
            return set_size(self.fd, cols, rows)
        return None

    def size(self):
        return get_size(self.fd)

    def poll(self):
        """The exit status once the command has ended (negative: killed by that signal), else None."""
        return self.process.poll()

    def signal(self, number):
        try:
            os.killpg(self.process.pid, number)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self):
        """Hang up: SIGHUP to the process group and close the terminal. Call kill() if it stays."""
        if self.poll() is None:
            self.signal(signal.SIGHUP)
            self.signal(signal.SIGCONT)
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def kill(self):
        if self.poll() is None:
            self.signal(signal.SIGKILL)

    def wait(self, timeout=None):
        return self.process.wait(timeout)


try:
    from PySide6.QtCore import Property, QObject, QSocketNotifier, Qt, QTimer, Signal, Slot
except ImportError:  # pragma: no cover - the app needs Qt; Pty alone does not
    QObject = None


if QObject is not None:
    class PtyBridge(QObject):
        """The page's view of a Pty: output (base64) and exited as signals; write, resize, ready and ack as
        slots."""
        output = Signal(str)
        exited = Signal(int)

        def __init__(self, argv, cols=80, rows=24, env=None, cwd=None, theme="dark", exit_text=None,
                     kill_after=3000, parent=None):
            super().__init__(parent)
            from . import applog
            self.began, self.shown = time.monotonic(), applog.argv_text(argv, 1500)
            try:
                self.pty = Pty(argv, cols, rows, env, cwd)
            except Exception as exc:
                applog.log("WARNING", "terminal", f"the terminal's command could not start: "
                                                  f"{applog.error_text(exc)}: {self.shown}")
                raise
            applog.log("INFO", "terminal", f"command started in a terminal tab (pid {self.pty.pid}"
                                           + (f", in {cwd}" if cwd else "") + f"): {self.shown}")
            self._theme, self.exit_text, self.kill_after = theme, exit_text, kill_after
            self.status, self.ready_seen, self.backlog, self.backlog_size = None, False, [], 0
            self.unacked, self.paused, self.closed = 0, False, False  # characters sent the page has not shown yet
            self.reader = QSocketNotifier(self.pty.fd, QSocketNotifier.Type.Read, self)
            self.reader.activated.connect(self._readable)
            self.writer = QSocketNotifier(self.pty.fd, QSocketNotifier.Type.Write, self)
            self.writer.setEnabled(False)
            self.writer.activated.connect(self._writable)
            self.reaper = QTimer(self)
            self.reaper.setInterval(100)
            self.reaper.timeout.connect(self._reap)
            self.killer = QTimer(self)
            self.killer.setSingleShot(True)
            self.killer.timeout.connect(self._kill)
            self.frames = Frames()
            self.framer = QTimer(self)
            self.framer.setSingleShot(True)
            self.framer.setTimerType(Qt.TimerType.PreciseTimer)
            self.framer.timeout.connect(self._flush)

        def _kill(self):
            from . import applog
            if self.pty.poll() is None:
                applog.log("INFO", "terminal", f"the terminal's command (pid {self.pty.pid}) is still there "
                                               f"{self.kill_after} ms after the hang-up: killed")
            self.pty.kill()

        def _get_theme(self):
            return self._theme

        theme = Property(str, _get_theme, constant=True)

        @property
        def running(self):
            return self.status is None

        # -- from the page --

        @Slot()
        def ready(self):
            self.ready_seen = True
            backlog, self.backlog, self.backlog_size = self.backlog, [], 0
            for chunk in backlog:
                self.unacked += len(chunk)
                self.output.emit(chunk)
            self._throttle()

        @Slot(int)
        def ack(self, count):
            """The page has shown COUNT characters of output: reading goes on once few enough are pending."""
            self.unacked = max(0, self.unacked - max(0, int(count)))
            self._throttle()

        def _throttle(self):
            """Read the terminal only while the page keeps up (and the backlog has room); output still gathered
            into a frame counts as sent."""
            waiting = (self.frames.size + 2) // 3 * 4
            full = self.unacked + waiting > ACK_LIMIT if self.ready_seen else self.backlog_size >= BACKLOG_LIMIT
            if full == self.paused or self.closed or self.status is not None or self.pty.eof:
                return
            self.paused = full
            self.reader.setEnabled(not full)

        @Slot(str)
        def write(self, encoded):
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                return
            if len(data) > INPUT_LIMIT or self.pty.fd is None:
                return
            if self.pty.write(data):
                self.writer.setEnabled(True)

        @Slot(int, int)
        def resize(self, cols, rows):
            try:
                self.pty.resize(cols, rows)
            except OSError:
                pass

        # -- from the terminal --

        def _send(self, data):
            encoded = base64.b64encode(data).decode("ascii")
            if self.ready_seen:
                self.unacked += len(encoded)
                self.output.emit(encoded)
            else:  # kept whole: reading stops while the backlog is full (_throttle)
                self.backlog.append(encoded)
                self.backlog_size += len(encoded)

        def _readable(self, *_):
            for _ in range(16):  # bounded: the event loop stays responsive under heavy output
                if self.paused:
                    break
                data = self.pty.read()
                if data is None:
                    self._flush()
                    self._ended()
                    return
                if not data:
                    break
                if self.ready_seen:
                    self.frames.add(data)
                else:
                    self._send(data)
                self._throttle()
            self._frame()

        def _frame(self):
            """Send the frame when it is due (or the reading has stopped: the page is behind), else wake up then."""
            due = self.frames.due()
            if due is None:
                self.framer.stop()
            elif due <= 0 or self.paused:
                self._flush()
            else:
                self.framer.start(max(1, round(due * 1000)))

        def _flush(self):
            self.framer.stop()
            if self.frames.size:
                self._send(self.frames.take())
                self._throttle()

        def _writable(self, *_):
            if not self.pty.flush():
                self.writer.setEnabled(False)

        def _ended(self):
            self.reader.setEnabled(False)
            self.writer.setEnabled(False)
            if not self.reaper.isActive():
                self.reaper.start()
                self._reap()

        def _reap(self):
            status = self.pty.poll()
            if status is None:
                return
            self.reaper.stop()
            self.killer.stop()
            if self.status is None:
                self.status = status
                from . import applog
                applog.log("INFO", "terminal", f"the terminal's command ended (pid {self.pty.pid}): "
                           + (f"killed by signal {-status}" if status < 0 else f"exit status {status}")
                           + f" after {applog.seconds(time.monotonic() - self.began)}"
                           + (" (its tab was closed)" if self.closed else "") + f": {self.shown}")
                if self.exit_text:
                    self._send(("\r\n" + self.exit_text.format(status=status) + "\r\n").encode())
                self.exited.emit(status)

        def close(self):
            """End the command: hang up, and kill it if it is still there after KILL_AFTER ms."""
            self._flush()
            self.closed = True
            self.reader.setEnabled(False)
            self.writer.setEnabled(False)
            if self.pty.poll() is None:
                from . import applog
                applog.log("INFO", "terminal", f"the terminal tab is closed while its command runs (pid "
                                               f"{self.pty.pid}): hang-up sent")
                self.pty.close()
                self.killer.start(self.kill_after)
                self.reaper.start()
            else:
                self.pty.close()
                self._reap()
