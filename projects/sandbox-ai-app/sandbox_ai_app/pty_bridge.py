"""A command in a pseudo-terminal, and its bridge to a terminal web page (xterm.js over QWebChannel).

Pty needs no Qt: it starts ARGV (a list, never a shell line) as the session leader of a new pseudo-
terminal, reads and writes its master side without blocking, resizes it (TIOCSWINSZ, the kernel sends
SIGWINCH) and ends it (SIGHUP to its process group, SIGKILL if it does not go).

PtyBridge needs only QtCore: a QSocketNotifier on the master descriptor, output and input as base64
text (the web page's QWebChannel carries strings), and a backlog until the page says it is ready. The page
acknowledges every chunk once it has shown it (ack); while more than ACK_LIMIT characters are not
acknowledged, or the backlog is full, the terminal is not read: the command waits (its writes block), and
nothing is lost or queued without bound.
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


def set_size(fd, cols, rows):
    cols, rows = max(1, min(int(cols), MAX_SIZE)), max(1, min(int(rows), MAX_SIZE))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return cols, rows


def get_size(fd):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return cols, rows


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
    from PySide6.QtCore import Property, QObject, QSocketNotifier, QTimer, Signal, Slot
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
            self.pty = Pty(argv, cols, rows, env, cwd)
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
            self.killer.timeout.connect(self.pty.kill)

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
            """Read the terminal only while the page keeps up (and the backlog has room)."""
            full = self.unacked > ACK_LIMIT if self.ready_seen else self.backlog_size >= BACKLOG_LIMIT
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
                    return
                data = self.pty.read()
                if data is None:
                    self._ended()
                    return
                if not data:
                    return
                self._send(data)
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
                if self.exit_text:
                    self._send(("\r\n" + self.exit_text.format(status=status) + "\r\n").encode())
                self.exited.emit(status)

        def close(self):
            """End the command: hang up, and kill it if it is still there after KILL_AFTER ms."""
            self.closed = True
            self.reader.setEnabled(False)
            self.writer.setEnabled(False)
            if self.pty.poll() is None:
                self.pty.close()
                self.killer.start(self.kill_after)
                self.reaper.start()
            else:
                self.pty.close()
                self._reap()
