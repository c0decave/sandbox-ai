"""Who answers on the app's single-instance socket: its version, the folder it runs from and its process - without Qt
(install.sh asks it too, with the new installation's copy of this file: python3 -I, the standard library only).

The words a start sends (one per connection; an app ignores every other word and closes the connection):
  show, hidden  every app since the guard exists: show the window / stay as it is (a start for the tray); no answer
  info          since 0.1.99: the app answers one line of JSON, {"app", "path", "pid", "sandbox_ai"}, and closes
  quit          since 0.1.99: the app quits as its own Quit does (its questions; the Board's changes kept as at every
                quit) and answers "quitting" or "declined"

An app older than 0.1.99 closes the connection on "info" without a word: it is an older one then, and it cannot be
asked to quit. What runs is still found out: the process at the other end of the socket (SO_PEERCRED, macOS'
LOCAL_PEERPID), its command line names the folder of the app it runs (the launcher passes it by absolute path), whose
__init__.py says its version (an update keeps a version's folder while a program uses it)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

from . import __version__

NAME = "sandbox-ai-app.sock"
CONNECT = 0.5  # seconds to reach a listening app
ANSWER = 2.0  # seconds an app has to answer "info" (its GUI thread answers it: busy for longer, nothing is known)
LIMIT = 4096  # bytes of an answer read at most
HERE = Path(__file__).resolve().parent  # this copy of the app (sandbox_ai_app/)
VERSION_LINE = re.compile(r'^__version__ = "([^"\n]{1,40})"', re.M)


def socket_path(environ=None):
    """$XDG_RUNTIME_DIR/sandbox-ai-app.sock (the user's own, 0700), else beside app.log (~/.local/state/sandbox-ai)."""
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        return Path(runtime) / NAME
    state = environ.get("XDG_STATE_HOME") or str(Path(environ.get("HOME") or Path.home()) / ".local" / "state")
    return Path(state) / "sandbox-ai" / NAME


@dataclass
class Running:
    """The app that answers on the socket. PROTOCOL: "info" (it said what it is), "old" (it closed the connection
    without a word: older than 0.1.99, knows show and hidden only) or "silent" (no answer in time)."""
    protocol: str
    pid: int | None = None
    version: str | None = None
    path: str | None = None
    sandbox_ai: str | None = None

    def named(self):
        """'sandbox-ai-app 0.1.97' or, its version unknown, 'an older sandbox-ai-app'."""
        if self.version:
            return f"sandbox-ai-app {self.version}"
        return "an older sandbox-ai-app" if self.protocol == "old" else "sandbox-ai-app"


def connect(path, timeout=CONNECT):
    """A connected socket to the app at PATH, or None when nobody listens there."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    except OSError:
        client.close()
        return None
    return client


def exchange(path, word, wait=ANSWER, connect_wait=CONNECT):
    """Send WORD to the app at PATH on a connection of its own: (None, None) when nobody listens, else (answer, pid):
    the answer's bytes (b"" when the app closed the connection without a word; None when it said nothing within WAIT
    seconds or WAIT is 0) and the app's process ID when the system says it."""
    client = connect(path, connect_wait)
    if client is None:
        return None, None
    try:
        pid = peer_pid(client)
        client.sendall(word)
        if not wait:
            return None, pid
        answer, deadline = b"", time.monotonic() + wait
        while b"\n" not in answer and len(answer) < LIMIT:
            left = deadline - time.monotonic()
            if left <= 0:
                return (answer or None), pid
            client.settimeout(left)
            try:
                chunk = client.recv(LIMIT)
            except socket.timeout:
                return (answer or None), pid
            if not chunk:
                return answer, pid  # closed: b"" from an app that ignores the word
            answer += chunk
        return answer[:LIMIT], pid
    except OSError:
        return b"", None
    finally:
        client.close()


def peer_pid(client):
    """The process ID at the other end of the connected Unix socket CLIENT, or None."""
    try:
        if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid, uid, gid}
            pid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                        struct.calcsize("3i")))[0]
        elif sys.platform == "darwin":  # SOL_LOCAL 0, LOCAL_PEERPID 2
            pid = struct.unpack("i", client.getsockopt(0, 2, struct.calcsize("i")))[0]
        else:
            return None
    except (OSError, struct.error):
        return None
    return pid if pid > 0 else None


def command_line(pid):
    """The arguments of process PID ([] when they cannot be read): /proc, else ps (macOS: one line, split at blanks)."""
    try:
        return [part for part in Path(f"/proc/{int(pid)}/cmdline").read_bytes().decode(errors="replace").split("\0")
                if part]
    except OSError:
        pass
    ps = ["ps", "-ww", "-o", "command=", "-p", str(int(pid))]
    try:
        done = subprocess.run(ps, capture_output=True, text=True, timeout=5)  # on the GUI thread on purpose: a start
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    line = done.stdout.strip()
    return line.split()


def package_of(arguments):
    """The sandbox_ai_app folder a command line runs: the launcher's app folder (an argument), the autostart's import
    folders (inside its -c script), or None."""
    candidates = []
    for argument in arguments:
        candidates.append(argument)
        candidates += re.findall(r"""['"](/[^'"]+)['"]""", argument)
    for candidate in candidates:
        if not candidate.startswith("/"):
            continue
        for folder in (Path(candidate), Path(candidate) / "sandbox_ai_app"):
            try:
                if folder.name == "sandbox_ai_app" and (folder / "__init__.py").is_file():
                    return folder.resolve()
            except OSError:
                continue
    return None


def version_in(package):
    """The __version__ the app in folder PACKAGE (sandbox_ai_app/) has, or None."""
    try:
        found = VERSION_LINE.search((Path(package) / "__init__.py").read_text(errors="replace")[:4000])
    except OSError:
        return None
    return found[1] if found else None


def text_of(value, limit=300):
    """A value an app said (or a file named), as one line of printable text."""
    text = "" if value is None else str(value)
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def ask(path):
    """The app that answers on the socket at PATH (a Running), or None when nobody listens there."""
    answer, pid = exchange(path, b"info")
    if answer is None and pid is None:
        probe = connect(path)
        if probe is None:
            return None
        probe.close()
    if answer:
        try:
            said = json.loads(answer.decode(errors="replace").strip())
        except (ValueError, RecursionError):
            said = None
        if isinstance(said, dict):
            number = said.get("pid")
            return Running("info", pid=number if isinstance(number, int) and number > 0 else pid,
                           version=text_of(said.get("app"), 40) or None, path=text_of(said.get("path")) or None,
                           sandbox_ai=text_of(said.get("sandbox_ai"), 40) or None)
    running = Running("old" if answer == b"" else "silent", pid=pid)
    if pid is not None:  # what it runs, from its command line
        package = package_of(command_line(pid))
        if package is not None:
            running.path, running.version = str(package), version_in(package)
    return running


def version_key(text):
    """(X, Y, Z) of a version 'X.Y.Z...' or None."""
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(n) for n in found.groups()) if found else None


def same_place(first, second):
    try:
        return Path(first).resolve() == Path(second).resolve()
    except (OSError, TypeError, ValueError):
        return first == second


def verdict(running, hidden, version=__version__, path=HERE):
    """What a start of the app (VERSION from folder PATH) does about RUNNING:
      "hidden"    a start for the tray (--hidden): it ends, the running app is told nothing new (as always)
      "show"      the same app runs: its window to the front, this start ends (as always)
      "newer"     a newer one runs: its window to the front, this start ends (and says so)
      "old"       an older one runs that cannot be asked to quit (before 0.1.99): its window to the front, and the
                  human is told which one runs, from where, and how to quit it
      "takeover"  an older one, or one from another installation: asked to quit, this start goes on once it has"""
    if hidden:
        return "hidden"
    if running.protocol == "silent":
        return "show"
    if running.protocol == "old":
        return "old"
    mine, theirs = version_key(version), version_key(running.version)
    if mine and theirs and theirs < mine:
        return "takeover"
    if mine and theirs and theirs > mine:
        return "newer"
    if running.version != version and not (mine and theirs):
        return "takeover"
    return "show" if same_place(running.path, path) else "takeover"


def where_of(running):
    """' (from PATH, PID N)' as far as it is known, else ''."""
    parts = ([f"from {running.path}"] if running.path else []) + ([f"PID {running.pid}"] if running.pid else [])
    return f" ({', '.join(parts)})" if parts else ""


def installed_note(sockets, version=__version__, path=HERE):
    """install.sh's line about an app that runs now (on one of the SOCKETS) and is not this copy (VERSION, from PATH:
    the one just installed); None when none runs or it is this very one."""
    for socket_file in sockets:
        running = ask(socket_file)
        if running is None:
            continue
        action = verdict(running, False, version, path)
        if action == "show" and (running.protocol == "info" or running.version == version):
            return None
        named, where = running.named(), where_of(running)
        if action == "takeover":
            return (f"{named} is running{where}, not the {version} installed now: quit it, or start sandbox-ai-app "
                    "again - it takes over (the running app quits the usual way)")
        if action == "newer":
            return f"{named} is running{where}, newer than the {version} installed now: quit it to use {version}"
        kill = f", or: kill {running.pid}" if running.pid else ""
        if action == "old":
            return (f"{named} is running{where}; it is too old to make way for {version} by itself: quit it (Quit "
                    f"in its File or tray menu{kill}), then start sandbox-ai-app again")
        return (f"{named} is running{where} and did not answer in time: quit it (Quit in its File or tray "
                f"menu{kill}), then start sandbox-ai-app again to use {version}")
    return None


def main(argv=None):
    """python3 -I -c '...' APP_DIR [SOCKET...] (install.sh, with the installed copy): prints installed_note's line,
    if any (the sockets: this user's, socket_path(), when none is named)."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    note = installed_note([Path(p) for p in arguments] or [socket_path()])
    if note:
        print(note)
    return 0
