"""A folder of a remote system for the Files page, over SSH and SFTP. No Qt.

RemoteRoot has the interface of files.Root - list, stat_entry, readlink, read_file, write_file, measure, free_space,
delete_entry, rename_entry, make_folder, make_file, chmod_entry - so the panes, the editor, the Markdown viewer and
the Properties dialog have one code path; files.copy_entry, move_entry and exists_in hand a remote side to the
functions here (copy_entry, move_entry, exists_in).

The connection is the system's OpenSSH: one `ssh -s HOST sftp` per remote root (the SFTP subsystem, no shell), and
this module speaks SFTP version 3 over it (Sftp). No remote shell ever runs, so no name or path can reach one; names
with NUL bytes or line breaks are refused before anything is sent. ssh runs with BatchMode=yes (keys and the
ssh-agent only: no password is asked for or kept) and StrictHostKeyChecking=yes against the user's known_hosts: an
unknown host fails (HostKeyUnknown) - the page shows its fingerprints (fingerprints(), ssh-keyscan) and adds it
(add_known_host) only after the human said yes; a changed key is refused outright (HostKeyChanged); a key that
needs its passphrase or a password fails (AuthenticationNeeded) - the page offers a terminal with ssh to unlock it.

A path below the root is walked component by component with LSTAT: a symbolic link on the way is refused and named
with where it points ("not followed"); the last component is acted on as itself (a link is removed or renamed, never
written through). (SFTP has no descriptors to walk by, so a remote program that swaps a folder for a link between
two requests is not stopped as files.py stops a local one; the remote system is the human's own.) Everything is
bounded like files.py (Budget: LIMIT bytes, MAX_ENTRIES entries, MAX_DEPTH folders), cancellable between requests,
and a request that gets no answer within REQUEST_TIMEOUT ends the connection.

Copies: a remote file or folder comes in through a staging folder created in the target folder (removed in any
case, and at exit), from where files.copy_entry puts it in place with every local rule (temporary names, links
skipped, nothing executable and risky files named where the target is a sandbox's folder: what comes from a remote
system into a sandbox's folder counts as the agent's); a local one goes out under a temporary name renamed into
place when complete (posix-rename@openssh.com to replace, else a rename that refuses an existing name), what the
copy created is removed unless it completes. A move copies first and removes the source when the copy is complete.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import errno
import os
import re
import secrets
import select
import stat
import struct
import subprocess
import threading
import time

from . import files
from .files import (
    Budget, Cancelled, Changed, Entry, FileData, Listing, MAX_DEPTH, MAX_ENTRIES, Refused, Result, TOO_MANY_WALK,
    WALK_ENTRIES, kind_of, parts, printable, single_name,
)
from .i18n import tr

REQUEST_TIMEOUT = 30  # s: a request without an answer ends the connection
POLL = 0.2  # s: how often what waits for the server looks whether it was interrupted (Cancel)
CONNECT_TIMEOUT = 20  # s: ssh's ConnectTimeout
CHUNK = 32 << 10  # bytes per READ or WRITE request (every SFTP server takes this much)
WINDOW = 16  # requests on their way at once while a file is read or written
READLINKS = 2000  # links of one listing whose targets are read
IDLE_ANSWERS = 3  # answers to READDIR in a row that bring no name: more, and the listing is given up
PACKET_LIMIT = 1 << 20  # bytes: a larger answer from the server ends the connection
STDERR_KEPT = 8 << 10  # bytes of ssh's own messages kept (for the reason a connection failed)
SSH_OPTIONS = ("BatchMode=yes", "StrictHostKeyChecking=yes", f"ConnectTimeout={CONNECT_TIMEOUT}",
               "ServerAliveInterval=15", "ServerAliveCountMax=3", "ForwardAgent=no", "ForwardX11=no",
               "ClearAllForwardings=yes", "PermitLocalCommand=no", "RequestTTY=no")
# The text a target may have: [user@]host[:port], host an alias of ~/.ssh/config, a name, IPv4 or [IPv6].
TARGET = re.compile(r"^(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]{0,63})@)?"
                    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_][A-Za-z0-9._-]{0,252})(?::(?P<port>[0-9]{1,5}))?$")

# -- SFTP version 3 (draft-ietf-secsh-filexfer-02) --
INIT, VERSION, OPEN, CLOSE, READ, WRITE, LSTAT, FSTAT, SETSTAT = 1, 2, 3, 4, 5, 6, 7, 8, 9
OPENDIR, READDIR, REMOVE, MKDIR, RMDIR, REALPATH, STAT, RENAME, READLINK = 11, 12, 13, 14, 15, 16, 17, 18, 19
EXTENDED = 200
STATUS, HANDLE, DATA, NAME, ATTRS, EXTENDED_REPLY = 101, 102, 103, 104, 105, 201
OK, EOF, NO_SUCH_FILE, PERMISSION_DENIED, FAILURE, BAD_MESSAGE, NO_CONNECTION, CONNECTION_LOST, UNSUPPORTED = range(9)
READ_FLAG, WRITE_FLAG, APPEND_FLAG, CREAT_FLAG, TRUNC_FLAG, EXCL_FLAG = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
SIZE_ATTR, UIDGID_ATTR, PERMISSIONS_ATTR, TIMES_ATTR = 0x01, 0x02, 0x04, 0x08
ERRNO = {NO_SUCH_FILE: errno.ENOENT, PERMISSION_DENIED: errno.EACCES, FAILURE: errno.EIO,
         UNSUPPORTED: errno.EOPNOTSUPP}


class RemoteError(OSError):
    """The connection failed or ended; str() says why in words."""


class HostKeyUnknown(RemoteError):
    """The host is not in known_hosts (StrictHostKeyChecking=yes refused it)."""


class HostKeyChanged(RemoteError):
    """The host's key is not the one in known_hosts: never added from here."""


class AuthenticationNeeded(RemoteError):
    """No key the server takes is at hand without a passphrase or a password (BatchMode)."""


class ConnectionLost(RemoteError):
    """The connection ended, or a request got no answer within REQUEST_TIMEOUT."""


@dataclass
class Attrs:
    size: int = 0
    uid: int = -1
    gid: int = -1
    mode: int = 0
    atime: float = 0.0
    mtime: float = 0.0
    flags: int = 0

    @property
    def st_mode(self):
        return self.mode

    @property
    def st_size(self):
        return self.size

    @property
    def st_uid(self):
        return self.uid

    @property
    def st_gid(self):
        return self.gid

    @property
    def st_mtime(self):
        return self.mtime

    @property
    def st_atime(self):
        return self.atime

    @property
    def st_ctime(self):
        return self.mtime  # (SFTP has no status change time)


def check_name(text):
    """TEXT unless it holds NUL or a line break (refused: a name or path never carries one to the other side)."""
    text = os.fspath(text)
    if "\0" in text or "\n" in text or "\r" in text:
        raise Refused(tr("A name with a line break or a NUL byte is not sent to a remote system: {name}",
                         name=printable(text)))
    return text


@dataclass
class Target:
    """Where to connect: HOST (a name, an address or an alias of ~/.ssh/config), USER and PORT if given, IDENTITY (a
    key file: sandbox-ai's host entries name one)."""
    host: str
    user: str = ""
    port: int = 0
    identity: str = ""

    @classmethod
    def parse(cls, text, identity=""):
        """[user@]host[:port] - anything else (a leading "-", spaces, a shell's characters) is refused."""
        text = str(text).strip()
        found = TARGET.match(text)
        if not found or text.startswith("-"):
            raise Refused(tr("Not a remote system: {target} (user@host, host or host:port)", target=printable(text)))
        port = int(found.group("port") or 0)
        if port > 65535:
            raise Refused(tr("Not a port: {port}", port=port))
        return cls(found.group("host").strip("[]"), found.group("user") or "", port, identity)

    def text(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.user + '@' if self.user else ''}{host}{':' + str(self.port) if self.port else ''}"

    def ssh_args(self, known_hosts=None, config=None):
        """ssh's options for this target (before the host; the host goes after "--")."""
        args = []
        if config:
            args += ["-F", config]
        for option in SSH_OPTIONS:
            args += ["-o", option]
        if known_hosts:
            args += ["-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null"]
        if self.port:
            args += ["-p", str(self.port)]
        if self.user:
            args += ["-l", self.user]
        if self.identity:
            args += ["-i", os.path.expanduser(self.identity), "-o", "IdentitiesOnly=yes"]
        return args


def ssh_config_hosts(path=None):
    """The Host aliases of ~/.ssh/config (and the files it includes directly beside it), without patterns."""
    path = path or os.path.expanduser("~/.ssh/config")
    found = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = stream.read(1 << 20).splitlines()
    except OSError:
        return found
    for line in lines:
        words = line.strip().split()
        if len(words) >= 2 and words[0].lower() == "host":
            for name in words[1:]:
                if not any(c in name for c in "*?!") and TARGET.match(name):
                    found.append(name)
    return list(dict.fromkeys(found))


def effective(target, config=None, ssh="ssh"):
    """{option: value} of what ssh makes of TARGET with its configuration (`ssh -G`, which connects nowhere: an
    alias's HostName and Port, a HostKeyAlias, the UserKnownHostsFile), or {} where ssh cannot say."""
    if not ssh:
        return {}
    argv = [ssh, "-G", *(["-F", config] if config else []), *(["-p", str(target.port)] if target.port else []),
            *(["-l", target.user] if target.user else []), "--", target.host]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if done.returncode != 0:
        return {}
    found = {}
    for line in done.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key and value:
            found.setdefault(key.lower(), value.strip())
    return found


def endpoint_of(target, config=None, ssh="ssh"):
    """(host name in lower case, port) that ssh connects TARGET to (`ssh -G`: an alias's HostName and Port), or None
    where ssh cannot say (SSH not the name of an ssh program: tests)."""
    seen = effective(target, config, ssh) if isinstance(ssh, str) else {}
    host = seen.get("hostname", "")
    if not host:
        return None
    return host.lower(), int(seen["port"]) if seen.get("port", "").isdigit() else (target.port or 22)


def known_hosts_file(target=None, config=None, ssh="ssh"):
    """The known_hosts file a host is added to: the first UserKnownHostsFile ssh uses for TARGET (its configuration
    may name another one for a host: the key added to ~/.ssh/known_hosts was never found), else ~/.ssh/known_hosts."""
    named = effective(target, config, ssh).get("userknownhostsfile", "").split() if target is not None else []
    if named and "%" not in named[0] and named[0].lower() != "none":
        return os.path.expanduser(named[0])
    return os.path.expanduser("~/.ssh/known_hosts")


def scan_host(target, keyscan="ssh-keyscan", timeout=CONNECT_TIMEOUT, config=None, ssh="ssh"):
    """The host keys TARGET offers (ssh-keyscan: known_hosts lines), read without logging in - from where ssh
    connects (ssh -G: an alias of ~/.ssh/config is scanned at its HostName and Port; ssh-keyscan reads no
    configuration, so an alias was looked up as a name in DNS and its keys, added under the alias, were never the
    ones ssh looks for), and named as ssh looks them up (its HostKeyAlias, if it has one)."""
    seen = effective(target, config, ssh)
    host = seen.get("hostname") or target.host
    port = int(seen["port"]) if seen.get("port", "").isdigit() else target.port or 22
    if host.startswith("-") or not 0 < port < 65536:
        host, port = target.host, target.port or 22
    argv = [keyscan, "-T", str(min(timeout, 30)), *(["-p", str(port)] if port != 22 else []), "--", host]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteError(tr("Could not read the host keys of {host}: {error}", host=printable(target.text()),
                             error=printable(exc))) from None
    lines = [line for line in done.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise RemoteError(tr("{host} offered no host key.", host=printable(target.text())))
    alias = seen.get("hostkeyalias", "")
    if alias and alias.lower() != "none" and " " not in alias:
        lines = [f"{alias} {line.split(' ', 1)[1]}" for line in lines if " " in line]
    return lines


def fingerprints(lines, keygen="ssh-keygen"):
    """["ED25519 SHA256:..."] of the known_hosts LINES (ssh-keygen -lf -)."""
    try:
        done = subprocess.run([keygen, "-l", "-f", "-"], input="\n".join(lines) + "\n", capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteError(printable(exc)) from None
    found = []
    for line in done.stdout.splitlines():
        words = line.split()
        if len(words) >= 3:
            found.append(f"{words[-1].strip('()')} {words[1]}")
    return found


def add_known_host(lines, path=None, target=None):
    """Append the host keys LINES (from scan_host, after the human said yes) to known_hosts (PATH, default the one ssh
    uses for TARGET: known_hosts_file; made 0600, its folder 0700, when missing)."""
    path = path or known_hosts_file(target)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        for line in lines:
            check_name(line)
            stream.write(line.rstrip() + "\n")


def diagnose(stderr, target):
    """The RemoteError that ssh's messages STDERR mean."""
    text = stderr.strip()
    last = printable(text.splitlines()[-1] if text else tr("the connection ended"))
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text or "Host key for" in text and "has changed" in text:
        return HostKeyChanged(tr("The host key of {host} is not the one you trusted before: someone may be in "
                                 "between. Nothing is sent; check the key with whoever runs it.",
                                 host=printable(target.text())))
    if "Host key verification failed" in text or "No " in text and "host key is known" in text:
        return HostKeyUnknown(tr("{host} is not among your known hosts.", host=printable(target.text())))
    if "Permission denied" in text or "Too many authentication failures" in text:
        return AuthenticationNeeded(tr("{host} did not let you in with a key at hand ({error}). A key with a "
                                       "passphrase must be unlocked in your ssh-agent (ssh-add); passwords are "
                                       "not asked for here.", host=printable(target.text()), error=last))
    return RemoteError(tr("No connection to {host}: {error}", host=printable(target.text()), error=last))


# -- the SFTP client --

def pack_string(data):
    data = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else bytes(data)
    return struct.pack(">I", len(data)) + data


def pack_attrs(mode=None, times=None, owner=None):
    """SFTP attributes: OWNER (uid, gid), the permission bits MODE, TIMES (atime, mtime: whole seconds) - each if given
    (in the order of their flags, as the protocol has them)."""
    flags, fields = 0, b""
    if owner is not None:
        flags, fields = flags | UIDGID_ATTR, fields + struct.pack(">II", *(int(i) & 0xFFFFFFFF for i in owner))
    if mode is not None:
        flags, fields = flags | PERMISSIONS_ATTR, fields + struct.pack(">I", mode & 0o7777)
    if times is not None:
        flags, fields = flags | TIMES_ATTR, fields + struct.pack(">II", *(int(t) & 0xFFFFFFFF for t in times))
    return struct.pack(">I", flags) + fields


class Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, count):
        if self.at + count > len(self.data):
            raise ConnectionLost(tr("The remote system sent a damaged answer."))
        chunk = self.data[self.at:self.at + count]
        self.at += count
        return chunk

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def u64(self):
        return struct.unpack(">Q", self.take(8))[0]

    def string(self):
        return self.take(self.u32())

    def text(self):
        return self.string().decode("utf-8", "surrogateescape")

    def attrs(self):
        flags, found = self.u32(), Attrs()
        found.flags = flags
        if flags & SIZE_ATTR:
            found.size = self.u64()
        if flags & UIDGID_ATTR:
            found.uid, found.gid = self.u32(), self.u32()
        if flags & PERMISSIONS_ATTR:
            found.mode = self.u32()
        if flags & TIMES_ATTR:
            found.atime, found.mtime = float(self.u32()), float(self.u32())
        if flags & 0x80000000:
            for _ in range(self.u32()):
                self.string()
                self.string()
        return found


class Sftp:
    """An SFTP session over ARGV's process (`ssh ... -s HOST sftp`, or in tests any program that speaks SFTP on its
    stdin and stdout). One request at a time per session (a lock), a file's chunks pipelined (WINDOW)."""

    def __init__(self, argv, target=None, timeout=REQUEST_TIMEOUT):
        self.target, self.timeout = target or Target("?"), timeout
        self.lock, self.next_id, self.extensions, self.errors = threading.RLock(), 1, {}, bytearray()
        # Requests waiting for the lock (turn()): a long read or write lets them go first between two windows.
        self.queued, self.counting = 0, threading.Lock()
        self.interrupted = threading.Event()  # interrupt(): what waits for the server gives up at once
        try:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        except OSError as exc:
            raise RemoteError(tr("ssh could not be started: {error}", error=printable(exc))) from None
        threading.Thread(target=self.drain, daemon=True).start()
        self.buffer = b""
        try:
            self.send(INIT, struct.pack(">I", 3))
            kind, payload = self.receive()
        except ConnectionLost:
            self.close()
            raise self.reason() from None
        if kind != VERSION:
            self.close()
            raise ConnectionLost(tr("The remote system does not speak SFTP."))
        reader = Reader(payload)
        self.version = reader.u32()
        while reader.at < len(payload):
            name = reader.text()
            self.extensions[name] = reader.string()

    def drain(self):
        """ssh's own messages (why it failed), the last STDERR_KEPT bytes."""
        stream = self.process.stderr
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                return
            self.errors += chunk
            del self.errors[:-STDERR_KEPT]

    def reason(self):
        """The RemoteError of a session that ended (ssh's messages, once it has gone)."""
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.05)  # (the last of stderr)
        return diagnose(bytes(self.errors).decode("utf-8", "replace"), self.target)

    @property
    def alive(self):
        return self.process.poll() is None

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass

    # -- packets --

    def interrupt(self):
        """Give up what waits for the server now (Cancel of a job whose request stalled): the connection ends - an
        unanswered request leaves the session out of step. Never waits (the GUI thread may call it)."""
        self.interrupted.set()
        try:
            self.process.kill()
        except OSError:
            pass

    def ended(self):
        """The ConnectionLost of a connection found closed: Cancel's when interrupt() ended it."""
        if self.interrupted.is_set():
            return ConnectionLost(tr("Cancelled: the connection to {host} was ended; SSH… connects again.",
                                     host=printable(self.target.text())))
        return ConnectionLost(tr("The connection to {host} ended.", host=printable(self.target.text())))

    def waited(self, fd, writing, deadline):
        """Until FD is ready (WRITING: for writing), looking every POLL s whether interrupt() was called; a timeout
        or an interrupt ends the connection (ConnectionLost)."""
        while True:
            if self.interrupted.is_set():
                raise self.ended()
            left = deadline - time.monotonic()
            if left <= 0:
                self.close()
                raise ConnectionLost(tr("{host} did not answer within {seconds} s; the connection was ended.",
                                        host=printable(self.target.text()), seconds=self.timeout))
            ready = select.select([] if writing else [fd], [fd] if writing else [], [], min(left, POLL))
            if ready[1 if writing else 0]:
                return

    def send(self, kind, payload):
        """A packet: written as the server takes it (a server that stops reading is waited for as an answer is)."""
        data = memoryview(struct.pack(">IB", len(payload) + 1, kind) + payload)
        deadline = time.monotonic() + self.timeout
        try:
            fd = self.process.stdin.fileno()
            while data:
                self.waited(fd, True, deadline)
                data = data[os.write(fd, data[:65536]):]
        except ConnectionLost:
            raise
        except (OSError, ValueError):
            raise self.ended() from None

    def read_exactly(self, count):
        fd = self.process.stdout.fileno()
        deadline = time.monotonic() + self.timeout
        while len(self.buffer) < count:
            self.waited(fd, False, deadline)
            chunk = os.read(fd, max(65536, count - len(self.buffer)))
            if not chunk:
                raise self.ended()
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def receive(self):
        length = struct.unpack(">I", self.read_exactly(4))[0]
        if not 1 <= length <= PACKET_LIMIT:
            self.close()
            raise ConnectionLost(tr("The remote system sent a damaged answer."))
        data = self.read_exactly(length)
        return data[0], data[1:]

    @contextlib.contextmanager
    def turn(self):
        """The session's lock for one request (counted while it waits: see let_others)."""
        with self.counting:
            self.queued += 1
        try:
            self.lock.acquire()
        finally:
            with self.counting:
                self.queued -= 1
        try:
            yield
        finally:
            self.lock.release()

    def let_others(self, longest=1.0):
        """Between two windows of a long read or write: the requests waiting for the lock go first (a lock gives
        no turns - the transfer took it back at once and a listing waited for the whole file)."""
        deadline = time.monotonic() + longest
        while self.queued and time.monotonic() < deadline:
            time.sleep(0.001)

    def ask(self, kind, payload):
        """Send one request; its (kind, payload after the id)."""
        with self.turn():
            ident = self.next_id
            self.next_id = (self.next_id + 1) & 0xFFFFFFFF
            self.send(kind, struct.pack(">I", ident) + payload)
            answer, data = self.receive()
            if struct.unpack(">I", data[:4])[0] != ident:
                self.close()
                raise ConnectionLost(tr("The remote system sent a damaged answer."))
            return answer, data[4:]

    @staticmethod
    def fail(payload, path=""):
        reader = Reader(payload)
        code, message = reader.u32(), reader.text()
        text = f"{printable(path)}: {printable(message)}" if path else printable(message)
        if code == NO_SUCH_FILE:
            return FileNotFoundError(errno.ENOENT, text)
        if code == PERMISSION_DENIED:
            return PermissionError(errno.EACCES, text)
        return OSError(ERRNO.get(code, errno.EIO), text)

    def status(self, kind, payload, path=""):
        """None for STATUS OK; raise for any other status."""
        if kind == STATUS:
            if Reader(payload).u32() == OK:
                return None
            raise self.fail(payload, path)
        raise ConnectionLost(tr("The remote system sent a damaged answer."))

    def expect(self, wanted, kind, payload, path=""):
        if kind == wanted:
            return Reader(payload)
        if kind == STATUS:
            raise self.fail(payload, path)
        raise ConnectionLost(tr("The remote system sent a damaged answer."))

    # -- requests --

    def realpath(self, path):
        reader = self.expect(NAME, *self.ask(REALPATH, pack_string(check_name(path))), path)
        reader.u32()
        return reader.text()

    def lstat(self, path):
        return self.expect(ATTRS, *self.ask(LSTAT, pack_string(check_name(path))), path).attrs()

    def stat(self, path):
        return self.expect(ATTRS, *self.ask(STAT, pack_string(check_name(path))), path).attrs()

    def readlink(self, path):
        reader = self.expect(NAME, *self.ask(READLINK, pack_string(check_name(path))), path)
        reader.u32()
        return reader.text()

    def listdir(self, path, limit=None):
        """[(name, Attrs)] of the folder PATH (attributes of the entries themselves: lstat), without . and ..; at most
        LIMIT (then the list is one longer: the caller sees there were more)."""
        handle = self.expect(HANDLE, *self.ask(OPENDIR, pack_string(check_name(path))), path).string()
        found, idle = [], 0
        try:
            while limit is None or len(found) <= limit:
                kind, payload = self.ask(READDIR, pack_string(handle))
                if kind == STATUS and Reader(payload).u32() == EOF:
                    break
                reader = self.expect(NAME, kind, payload, path)
                # (answers that bring no name - none, or only "." and ".." - again and again, never EOF: a listing that
                # never ended, its thread holding the connection for good)
                before = len(found)
                for _ in range(reader.u32()):
                    name, _long, attrs = reader.text(), reader.string(), reader.attrs()
                    # One name of the folder, or nothing: a server that lists "../../x" or "/home/you/x" (malicious or
                    # broken) had a download write that file anywhere you may write - each name is joined to the
                    # folder it is copied into (scp's CVE-2019-6111; OpenSSH's sftp refuses such names too).
                    if name and name not in (".", "..") and "/" not in name and "\0" not in name:
                        found.append((name, attrs))
                idle = idle + 1 if len(found) == before else 0
                if idle > IDLE_ANSWERS:
                    raise OSError(errno.EIO, f"{printable(path)}: {tr('The remote system sent a damaged answer.')}")
        finally:
            self.close_handle(handle)
        return found

    def close_handle(self, handle):
        """Close HANDLE, whatever the server answers (a folder, a file read)."""
        try:
            self.close_written(handle)
        except OSError:
            pass

    def close_written(self, handle):
        """Close the file HANDLE that was written: a failure is raised (a CLOSE can fail as close() does - a full
        network file system, a quota: what was written may not be there)."""
        self.status(*self.ask(CLOSE, pack_string(handle)))

    def open(self, path, flags, mode=None):
        payload = pack_string(check_name(path)) + struct.pack(">I", flags) + pack_attrs(mode)
        return self.expect(HANDLE, *self.ask(OPEN, payload), path).string()

    def mkdir(self, path, mode=0o755):
        self.status(*self.ask(MKDIR, pack_string(check_name(path)) + pack_attrs(mode)), path)

    def rmdir(self, path):
        self.status(*self.ask(RMDIR, pack_string(check_name(path))), path)

    def remove(self, path):
        self.status(*self.ask(REMOVE, pack_string(check_name(path))), path)

    def chmod(self, path, mode, times=None):
        """The permission bits MODE of PATH (and TIMES: (atime, mtime), if given)."""
        self.status(*self.ask(SETSTAT, pack_string(check_name(path)) + pack_attrs(mode, times)), path)

    def chown(self, path, uid, gid):
        """PATH given to UID and GID (SETSTAT; a server that is no root's refuses another user)."""
        self.status(*self.ask(SETSTAT, pack_string(check_name(path)) + pack_attrs(owner=(uid, gid))), path)

    def rename(self, old, new, replace=False):
        """OLD renamed NEW: an existing NEW is refused (SFTP's rename), with REPLACE replaced at once
        (posix-rename@openssh.com; a server without it: refused)."""
        check_name(old), check_name(new)
        if replace:
            if "posix-rename@openssh.com" not in self.extensions:
                raise OSError(errno.EOPNOTSUPP, tr("This SFTP server cannot replace a file in one step."))
            payload = pack_string("posix-rename@openssh.com") + pack_string(old) + pack_string(new)
            self.status(*self.ask(EXTENDED, payload), new)
        else:
            self.status(*self.ask(RENAME, pack_string(old) + pack_string(new)), new)

    def statvfs(self, path):
        """(free, total) bytes of the file system of PATH (statvfs@openssh.com), or None without it."""
        if "statvfs@openssh.com" not in self.extensions:
            return None
        payload = pack_string("statvfs@openssh.com") + pack_string(check_name(path))
        reader = self.expect(EXTENDED_REPLY, *self.ask(EXTENDED, payload), path)
        bsize, frsize, blocks, _bfree, bavail = (reader.u64() for _ in range(5))
        unit = frsize or bsize
        return bavail * unit, blocks * unit

    def read_file(self, handle, size, limit, cancelled=None, budget=None, sink=None):
        """The bytes of the open file HANDLE (SIZE as it was stat'ed, at most LIMIT), WINDOW chunks on their way at
        once; SINK(chunk): each chunk handed on instead of kept. Returns (data or b"", bytes read, more left).

        A server may answer a READ with fewer bytes than asked without being at the end (the protocol allows it): the
        rest is asked for again from where it stopped - only EOF (or no data) ends the file; a short answer was taken
        for the end and the copy was cut short, silently. The session's lock is held for one window at a time, so a
        request of another thread (a listing, Properties, a rename: the window's own) goes between two windows
        instead of waiting for the end of a whole file."""
        kept, offset, done = [], 0, False
        want = min(size, limit) if size else limit
        while not done and offset < want:
            if cancelled is not None and cancelled():
                raise Cancelled(tr("Cancelled."))
            self.let_others()
            with self.lock:
                asked = []
                while len(asked) < WINDOW and offset + len(asked) * CHUNK < want:
                    ident = self.next_id
                    self.next_id = (self.next_id + 1) & 0xFFFFFFFF
                    at = offset + len(asked) * CHUNK
                    length = min(CHUNK, want - at)
                    self.send(READ, struct.pack(">I", ident) + pack_string(handle) + struct.pack(">QI", at, length))
                    asked.append((ident, length))
                short = False  # an answer of this window was short: the rest of the window is asked for again
                # A failure in the window - an error answered, Cancel or the budget (budget.take), the sink (a full
                # disk) - is raised once every answer of the window is read: raised at once, the others stayed in the
                # pipe, the next request read one of them ("a damaged answer") and the connection ended.
                failed = None
                for ident, length in asked:
                    kind, data = self.receive()
                    if struct.unpack(">I", data[:4])[0] != ident:
                        self.close()
                        raise ConnectionLost(tr("The remote system sent a damaged answer."))
                    if failed is not None:
                        continue
                    try:
                        if kind == STATUS and Reader(data[4:]).u32() == EOF:
                            done = done or not short  # (after a short answer: asked for again, from where it stopped)
                            continue
                        chunk = self.expect(DATA, kind, data[4:]).string()[:length]
                        if done or short:
                            continue
                        if not chunk:
                            done = True
                            continue
                        if budget is not None:
                            budget.take(len(chunk))
                        if sink is not None:
                            sink(chunk)
                        else:
                            kept.append(chunk)
                        offset += len(chunk)
                        short = len(chunk) < length
                    except ConnectionLost:
                        raise
                    except BaseException as exc:  # noqa: BLE001 - raised below, once the window is read
                        failed = exc
                if failed is not None:
                    raise failed
        return b"".join(kept), offset, size > offset

    def write_file(self, handle, source, cancelled=None, budget=None):
        """SOURCE (a callable giving the next chunk, b"" at the end) written to the open file HANDLE, WINDOW chunks
        on their way at once (the session's lock held for one window at a time, as read_file). Returns the bytes
        written."""
        offset, ended = 0, False
        while not ended:
            if cancelled is not None and cancelled():
                raise Cancelled(tr("Cancelled."))
            self.let_others()
            with self.lock:
                asked, failed = [], None
                try:
                    while len(asked) < WINDOW:
                        chunk = source()
                        if not chunk:
                            ended = True
                            break
                        if budget is not None:
                            budget.take(len(chunk))
                        ident = self.next_id
                        self.next_id = (self.next_id + 1) & 0xFFFFFFFF
                        self.send(WRITE, struct.pack(">I", ident) + pack_string(handle) + struct.pack(">Q", offset) +
                                  pack_string(chunk))
                        asked.append(ident)
                        offset += len(chunk)
                except ConnectionLost:
                    raise
                except BaseException as exc:  # noqa: BLE001 - Cancel, the budget, a local read: once the window's
                    failed = exc  # answers are read (left in the pipe, the next request took one: the connection ended)
                for ident in asked:
                    kind, data = self.receive()
                    if struct.unpack(">I", data[:4])[0] != ident:
                        self.close()
                        raise ConnectionLost(tr("The remote system sent a damaged answer."))
                    try:
                        self.status(kind, data[4:])
                    except OSError as exc:
                        failed = failed or exc
                if failed is not None:
                    raise failed
        return offset


# -- the root --

def join(*names):
    """The remote path of NAMES joined with "/" (a name is never absolute here)."""
    path = "/".join(name.strip("/") if index else name.rstrip("/") or "/" for index, name in enumerate(names) if name)
    return path.replace("//", "/") or "/"


class RemoteRoot:
    """A folder PATH (default: the home folder) of the remote system TARGET (a Target), over SSH and SFTP (see the
    module). SSH: the ssh program (tests: any program that speaks SFTP on stdin and stdout, given as a list);
    KNOWN_HOSTS, CONFIG: ssh's files (default: the user's)."""
    remote = True
    sandbox = False

    def __init__(self, target, path="", ssh="ssh", known_hosts=None, config=None, timeout=REQUEST_TIMEOUT):
        self.target = target if isinstance(target, Target) else Target.parse(target)
        if isinstance(ssh, (list, tuple)):
            argv = list(ssh)
        else:
            argv = [ssh, *self.target.ssh_args(known_hosts, config), "-s", "--", self.target.host, "sftp"]
        self.argv = argv
        self.sftp = Sftp(argv, self.target, timeout)
        try:
            self.home = self.sftp.realpath(".")  # (where the Files page starts: the remote HOME)
            self.path = self.sftp.realpath(check_name(path or "."))
            info = self.sftp.stat(self.path)
            if not stat.S_ISDIR(info.mode):
                raise Refused(tr("{path} is not a folder.", path=printable(self.path)))
        except BaseException:
            self.sftp.close()
            raise
        self.label = self.target.text()
        self.key = f"ssh://{self.label}{self.path}"
        self.hook = None

    def close(self):
        self.sftp.close()

    @property
    def alive(self):
        return self.sftp.alive

    def display(self, relative=""):
        """user@host:/path of RELATIVE (shown)."""
        return f"{self.label}:{self.full(relative)}"

    # -- paths --

    def full(self, relative=""):
        return join(self.path, *parts(relative))

    def walk(self, relative, last=False):
        """The remote path of RELATIVE: every folder on the way checked with LSTAT (a link is refused and named; LAST:
        the last component too, which must be a folder then)."""
        components = parts(relative)
        path = self.path
        for index, part in enumerate(components):
            check_name(part)
            path = join(path, part)
            if index == len(components) - 1 and not last:
                break
            info = self.sftp.lstat(path)
            if stat.S_ISLNK(info.mode):
                raise Refused(tr("{name} is a symbolic link to {target}; it is not followed.", name=printable(part),
                                 target=printable(self.target_of(path))))
            if not stat.S_ISDIR(info.mode):
                raise Refused(tr("{name} is not a folder (symbolic links are not followed).", name=printable(part)))
        return path

    def target_of(self, path):
        try:
            return self.sftp.readlink(path)
        except OSError:
            return "?"

    def entry(self, path, attrs):
        """ATTRS of PATH as a listing (READDIR) gave them, asked again with LSTAT where it is a folder: a server may
        give what a link points to there (OpenSSH's gives the link's own) - a delete, a copy or a count that went into
        such a "folder" went through the link (a delete emptied the folder it points to)."""
        return self.sftp.lstat(path) if stat.S_ISDIR(attrs.mode) else attrs

    def listed(self, path, budget):
        """The entries of the folder PATH, read only as far as BUDGET goes: a folder of millions was read whole into
        memory before its entries were counted ("More than ..."; a local folder stops being read at the budget)."""
        room = max(0, budget.entries - budget.count)
        found = self.sftp.listdir(path, limit=room)
        if len(found) > room:
            raise budget.too_many()
        return found

    def parent(self, relative):
        components = parts(relative)
        if not components:
            raise Refused(tr("Choose a file or folder below the folder shown."))
        return self.walk("/".join(components[:-1]), last=True), components[-1]

    # -- reading --

    def list(self, relative=""):
        """The folder RELATIVE, folders first, links described (their targets read, never followed); at most
        MAX_ENTRIES entries (then Listing.truncated)."""
        path = self.walk(relative, last=True)
        found = self.sftp.listdir(path, limit=MAX_ENTRIES)
        listing = Listing()
        listing.truncated = len(found) > MAX_ENTRIES
        links = 0
        for name, attrs in found[:MAX_ENTRIES]:
            kind = kind_of(attrs.mode)
            target = None
            if kind == "link":
                links += 1
                target = self.target_of(join(path, name)) if links <= READLINKS else "?"
            listing.append(Entry(name, kind, attrs.size, attrs.mtime, target, attrs.mode, attrs.uid, attrs.gid))
        result = Listing(sorted(listing, key=lambda e: (e.kind != "dir", e.name.lower(), e.name)))
        result.truncated = listing.truncated
        return result

    def stat_entry(self, relative):
        """The attributes of RELATIVE itself (a link: the link's)."""
        if not parts(relative):
            return self.sftp.lstat(self.path)
        return self.sftp.lstat(self.walk(relative))

    def lstat(self, relative):
        return self.stat_entry(relative)

    def readlink(self, relative):
        return self.sftp.readlink(self.walk(relative))

    def read(self, relative, limit=1 << 20):
        return self.read_file(relative, limit).data

    def read_file(self, relative, limit=None):
        """FileData of the regular file RELATIVE (at most LIMIT, default files.EDIT_LIMIT); a link or anything but a
        regular file is refused."""
        limit = files.EDIT_LIMIT if limit is None else limit
        path = self.walk(relative)
        info = self.sftp.lstat(path)
        if stat.S_ISLNK(info.mode):
            raise Refused(tr("{name} is a symbolic link to {target}; it is not followed.",
                             name=printable(parts(relative)[-1]), target=printable(self.target_of(path))))
        if not stat.S_ISREG(info.mode):
            raise Refused(tr("{name} is not a regular file.", name=printable(parts(relative)[-1])))
        handle = self.sftp.open(path, READ_FLAG)
        try:
            # (a file whose size says 0 - /proc, /sys - may hold more: one byte beyond the limit is asked for, as a
            # local read does; read up to the limit, it was taken as whole and the editor saved it cut short)
            data, _count, more = self.sftp.read_file(handle, info.size, limit if info.size else limit + 1)
        finally:
            self.sftp.close_handle(handle)
        return FileData(data[:limit], identity(info), info.size, more or info.size > len(data) or len(data) > limit,
                        info.mode)

    def measure(self, relative, entries=WALK_ENTRIES, cancelled=None, progress=None, seen=None):
        """Result.bytes, .files, .directories of RELATIVE with all it holds (links counted, never followed), bounded
        as files.Root.measure (Refused with .partial). (SEEN: as files.Root.measure takes it - SFTP tells no inode
        numbers, so a file of several names is counted as each of them.)"""
        budget, result = Budget(0, entries, cancelled, TOO_MANY_WALK, progress), Result()
        try:
            path = self.walk(relative, last=True) if not parts(relative) else self.walk(relative)
            info = self.sftp.lstat(path)
            self._measure(path, info, budget, result, 0, top=not parts(relative))
        except (OSError, ValueError) as exc:
            exc.partial = result
            raise
        return result

    def _measure(self, path, info, budget, result, depth, top=False):
        if not top:
            budget.entry()
        if stat.S_ISDIR(info.mode):
            if depth > MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not counted.", count=MAX_DEPTH))
            result.directories += 1
            for name, attrs in self.listed(path, budget):
                self._measure(join(path, name), self.entry(join(path, name), attrs), budget, result, depth + 1)
        else:
            size = attrs_size(info)
            result.files += 1
            result.bytes += size
            if budget.progress is not None:
                budget.progress.bytes += size

    def free_space(self, relative=""):
        found = self.sftp.statvfs(self.walk(relative, last=True))
        if found is None:
            raise OSError(errno.EOPNOTSUPP, tr("This SFTP server does not tell its free space."))
        return found

    def exists(self, relative_dir, names):
        return self.exists_at(self.walk(relative_dir, last=True), names)

    def exists_at(self, folder, names):
        """Those of NAMES that exist (as anything, a link too) in the remote FOLDER."""
        found = []
        for name in names:
            try:
                self.sftp.lstat(join(folder, single_name(name)))
                found.append(name)
            except FileNotFoundError:
                pass
        return found

    # -- writing --

    def write_file(self, relative, data, expected=None, in_place=False, mode=None):
        """DATA as the file RELATIVE: written under a temporary name beside it and renamed over it (its permission
        bits kept, never set-ID or sticky); refused (Changed) when it is not the file EXPECTED (identity) any more.
        Its owner and group stay: the new file is given to them; where the server refuses (it is another user's),
        files.NotKept is raised - IN_PLACE (after the human said yes) writes the file itself then (not in one step).
        MODE: the bits of the file as it was read, which a file no longer there is made with again (else 0644)."""
        folder, name = self.parent(relative)
        path = join(folder, name)
        if in_place:
            return self.write_in_place(path, name, data, expected)
        mode = 0o644 if mode is None else stat.S_IMODE(mode) & 0o777
        try:
            info = self.sftp.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.mode) or not stat.S_ISREG(info.mode):
                raise Refused(tr("{name} is not a regular file.", name=printable(name)))
            if expected is not None and identity(info) != tuple(expected):
                raise Changed(tr("{name} changed on disk since it was opened.", name=printable(name)))
            mode = stat.S_IMODE(info.mode) & 0o777
        elif expected is not None:
            raise Changed(tr("{name} changed on disk since it was opened.", name=printable(name)))
        chunks = iter([bytes(data[at:at + CHUNK]) for at in range(0, len(data), CHUNK)])
        owner = (info.uid, info.gid) if info is not None and info.flags & UIDGID_ATTR else None

        def unchanged():
            """Once more right before the rename (as files.Root.write_file): the new contents took their time to go up,
            and what another program - an agent of a host of sandbox-ai's - wrote meanwhile was replaced unasked."""
            if expected is None:
                return
            try:
                again = self.sftp.lstat(path)
            except FileNotFoundError:
                again = None
            if again is None or identity(again) != tuple(expected):
                raise Changed(tr("{name} changed on disk since it was opened.", name=printable(name)))
        self.put(folder, name, lambda: next(chunks, b""), mode, replace=True, owner=owner, in_place=info is not None,
                 before=unchanged)
        return self.saved_identity(path, data)

    def write_in_place(self, path, name, data, expected):
        """DATA written into the remote file PATH itself (write_file's IN_PLACE: asked first - a write cut short leaves
        it half written): it must still be the regular file EXPECTED; its owner, group and bits stay."""
        info = self.sftp.lstat(path)
        if stat.S_ISLNK(info.mode) or not stat.S_ISREG(info.mode):
            raise Refused(tr("{name} is not a regular file.", name=printable(name)))
        if expected is not None and identity(info) != tuple(expected):
            raise Changed(tr("{name} changed on disk since it was opened.", name=printable(name)))
        chunks = iter([bytes(data[at:at + CHUNK]) for at in range(0, len(data), CHUNK)])
        handle = self.sftp.open(path, WRITE_FLAG | TRUNC_FLAG)
        try:
            self.sftp.write_file(handle, lambda: next(chunks, b""))
        except BaseException:
            self.sftp.close_handle(handle)
            raise
        self.sftp.close_written(handle)
        return self.saved_identity(path, data)

    def saved_identity(self, path, data):
        """What a save of DATA to PATH saved (write_file's result, the next save's EXPECTED): the file as it is now -
        unless it is no longer as long as DATA (a program wrote it right after: that write was taken for this save's,
        and the next save replaced it unasked, as files.Root.write_file says); then one that matches no file, so that
        the next save finds it changed."""
        now = self.sftp.lstat(path)
        if stat.S_ISREG(now.mode) and now.size == len(data):
            return identity(now)
        return (len(data), -1, now.mode)

    def put(self, folder, name, source, mode, replace=False, cancelled=None, budget=None, times=None, owner=None,
            in_place=False, before=None):
        """SOURCE's chunks as NAME in the remote FOLDER: a temporary name first, renamed when complete (REPLACE:
        over an existing NAME; else an existing one is refused); the temporary file is removed on any failure.
        TIMES: (atime, mtime) the file gets (a copy keeps the times of the file copied). OWNER: (uid, gid) the file
        must keep (the file it replaces: renamed over it, it became yours) - files.NotKept where the server refuses.
        IN_PLACE: the file may be written in place instead (the editor's) - files.NoTemporary where no temporary file
        may be made in FOLDER (as files.Root.write_file: a local one was offered that, a remote one failed). BEFORE():
        called right before the rename (it raises to stop it: the temporary file is removed)."""
        if replace:  # (before anything is sent: a folder of that name was found only by the rename, "Is a directory")
            try:
                there = self.sftp.lstat(join(folder, name))
            except FileNotFoundError:
                there = None
            if there is not None and stat.S_ISDIR(there.mode):
                raise Refused(tr("{name} is a folder there.", name=printable(name)))
        temporary = join(folder, f".sandbox-ai-app-{os.getpid()}-{secrets.token_hex(8)}.part")
        try:
            handle = self.sftp.open(temporary, WRITE_FLAG | CREAT_FLAG | EXCL_FLAG | TRUNC_FLAG, 0o600)
        except PermissionError:
            if not in_place:
                raise PermissionError(errno.EACCES, f"{printable(join(folder, name))}: {os.strerror(errno.EACCES)}") \
                    from None
            raise files.NoTemporary(tr("{name} cannot be saved through a temporary file: you may not write its folder. "
                                       "It can be written in place.", name=printable(name))) from None
        except RemoteError:
            raise
        except OSError as exc:  # (named as the file: the temporary name is one the human never saw)
            why = exc.strerror or os.strerror(exc.errno or errno.EIO)
            why = why[len(printable(temporary)) + 2:] if why.startswith(printable(temporary) + ": ") else why
            raise OSError(exc.errno, f"{printable(join(folder, name))}: {why}") from None
        placed = False
        try:
            try:
                written = self.sftp.write_file(handle, source, cancelled, budget)
            except BaseException:
                self.sftp.close_handle(handle)
                raise
            # (a CLOSE that failed was not looked at: what may be incomplete was renamed over the file all the same)
            self.sftp.close_written(handle)
            if owner is not None:
                made = self.sftp.lstat(temporary)
                if (made.uid, made.gid) != tuple(owner):
                    try:
                        self.sftp.chown(temporary, *owner)
                    except OSError:
                        raise files.NotKept(tr(
                            "{name} belongs to UID {uid} (group {gid}): saved through a new file renamed over it, it "
                            "would become yours. It can be written in place (it stays theirs).", name=printable(name),
                            uid=owner[0], gid=owner[1])) from None
            self.sftp.chmod(temporary, mode & 0o777, times)
            if before is not None:
                before()
            try:
                self.sftp.rename(temporary, join(folder, name), replace=replace)
            except OSError as exc:
                if not replace and name in self.exists_at(folder, [name]):
                    raise Refused(tr("{name} already exists there.", name=printable(name))) from None
                raise exc
            placed = True
            return written
        finally:
            if not placed:
                try:
                    self.sftp.remove(temporary)
                except OSError:
                    pass

    def delete_entry(self, relative, entries=WALK_ENTRIES, cancelled=None, progress=None):
        """Remove RELATIVE with everything in it (a link is removed, never followed); Result.removed counts."""
        if not parts(relative):
            raise Refused(tr("The folder shown at the top of a pane is not deleted here."))
        budget, result = Budget(0, entries, cancelled, TOO_MANY_WALK, progress), Result()
        path = self.walk(relative)
        # Counted first, as files.Root.delete_entry: too many entries or folders nested too deep are refused before
        # anything is deleted (the delete went on until the budget ran out - part of the folder gone).
        self._count(path, self.sftp.lstat(path), Budget(0, entries, cancelled, TOO_MANY_WALK), 0)
        try:
            self._delete(path, self.sftp.lstat(path), budget, result, 0)
        except (OSError, ValueError) as exc:
            exc.partial = result
            raise
        return result

    def _count(self, path, info, budget, depth):
        budget.entry()
        if stat.S_ISDIR(info.mode):
            if depth > MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not deleted here.", count=MAX_DEPTH))
            for name, attrs in self.listed(path, budget):
                if "\n" in name or "\r" in name:  # (refused before anything goes: the delete stopped there, half way)
                    raise Refused(tr("{name} in {folder} has a line break in its name, which is not sent to a remote "
                                     "system: nothing was deleted.", name=printable(name), folder=printable(path)))
                self._count(join(path, name), self.entry(join(path, name), attrs), budget, depth + 1)

    def _delete(self, path, info, budget, result, depth):
        budget.entry()
        if stat.S_ISDIR(info.mode):
            if depth > MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not deleted here.", count=MAX_DEPTH))
            for name, attrs in self.listed(path, budget):
                self._delete(join(path, name), self.entry(join(path, name), attrs), budget, result, depth + 1)
            self.sftp.rmdir(path)
        else:
            self.sftp.remove(path)
        result.removed += 1

    def remove_fetched(self, relative, seen):
        """Remove from RELATIVE what a move fetched unchanged (SEEN: download's record of it): the files whose
        attributes are still those read, the folders walked once they are empty. The relative paths of what stays
        (added or changed after it was read)."""
        path = self.walk(relative)
        return self._remove_fetched(path, self.sftp.lstat(path), seen, parts(relative)[-1], 0)

    def _remove_fetched(self, path, info, seen, shown, depth):
        if stat.S_ISDIR(info.mode):
            if path not in seen["folders"] or depth > MAX_DEPTH:
                return [shown]
            left = []
            # (bounded as every listing: a server that lists without end was read into memory without end)
            listed = self.sftp.listdir(path, limit=WALK_ENTRIES)
            if len(listed) > WALK_ENTRIES:
                return [shown]
            for name, attrs in listed:
                child = join(path, name)
                left += self._remove_fetched(child, self.entry(child, attrs), seen, f"{shown}/{name}", depth + 1)
            if not left:
                try:
                    self.sftp.rmdir(path)
                except OSError:
                    left.append(shown)
            return left
        if stat.S_ISREG(info.mode) and seen["files"].get(path) == identity(info):
            self.sftp.remove(path)
            return []
        return [shown]

    def rename_entry(self, relative, new_name):
        """RELATIVE renamed NEW_NAME in its folder; an existing name is refused (and said so: the server only says
        "Failure"). A new name that is the old one in other letter case (or Unicode normalisation) on a server whose
        file system ignores that (macOS) "exists" there - it is the file itself, and OpenSSH's server refused such a
        rename: it goes through a temporary name."""
        folder, name = self.parent(relative)
        new_name = single_name(check_name(new_name))
        if new_name == name:
            return
        old, new = join(folder, name), join(folder, new_name)
        try:
            self.sftp.rename(old, new)
            return
        except OSError as exc:
            failed = exc
        listed = {entry for entry, _attrs in self.sftp.listdir(folder, limit=MAX_ENTRIES)}
        if new_name in listed or name not in listed or not files.same_name_folded(name, new_name):
            if new_name in listed or self.exists_at(folder, [new_name]):
                raise Refused(tr("{name} already exists there.", name=printable(new_name))) from None
            raise failed
        temporary = join(folder, f".sandbox-ai-app-{os.getpid()}-{secrets.token_hex(8)}.part")
        self.sftp.rename(old, temporary)
        try:
            self.sftp.rename(temporary, new)
        except OSError:
            try:
                self.sftp.rename(temporary, old)  # (back as it was)
            except OSError:
                raise Refused(tr("{name} could not be renamed and is named {temporary} now.", name=printable(name),
                                 temporary=printable(temporary.rsplit("/", 1)[-1]))) from None
            raise

    def make_folder(self, relative_dir, name):
        folder = self.walk(relative_dir, last=True)
        name = single_name(check_name(name))
        try:
            self.sftp.mkdir(join(folder, name), 0o755)
        except OSError:
            if name in self.exists(relative_dir, [name]):
                raise Refused(tr("{name} already exists there.", name=printable(name))) from None
            raise

    def make_file(self, relative_dir, name, untrusted=None):
        """A new empty file NAME (O_CREAT | O_EXCL on the server: an existing name, a link too, is refused)."""
        folder = self.walk(relative_dir, last=True)
        name = single_name(check_name(name))
        try:
            handle = self.sftp.open(join(folder, name), WRITE_FLAG | CREAT_FLAG | EXCL_FLAG, 0o644)
        except OSError:
            if name in self.exists(relative_dir, [name]):
                raise Refused(tr("{name} already exists there.", name=printable(name))) from None
            raise
        self.sftp.close_handle(handle)

    def chmod_entry(self, relative, mode, special=False):
        if not 0 <= mode <= 0o7777:
            raise Refused(tr("Not a permission mode: {mode}", mode=f"{mode:o}"))
        path = self.walk(relative) if parts(relative) else self.path
        if stat.S_ISLNK(self.sftp.lstat(path).mode):
            raise Refused(tr("A symbolic link has no permissions of its own; what it points to is not changed here."))
        self.sftp.chmod(path, mode if special else mode & 0o777)

    def chown_entry(self, *_args, **_kwargs):
        raise Refused(tr("Owners are not changed on a remote system from here."))

    def open_dir(self, relative=""):
        raise Refused(tr("A remote folder has no local descriptor."))


def identity(info):
    """What write_file compares: (size, mtime, mode) - SFTP has no inode numbers."""
    return (info.size, int(info.mtime), info.mode)


def attrs_size(info):
    return info.size if stat.S_ISREG(info.mode) else 0


# -- copies and moves with a remote side (files.copy_entry, move_entry and exists_in hand them here) --

def is_remote(root):
    return getattr(root, "remote", False)


def exists_in(root, relative_dir, names):
    return root.exists(relative_dir, names)


def copy_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, name=None,
               limit=files.LIMIT, entries=MAX_ENTRIES, cancelled=None, progress=None, agents=frozenset()):
    """files.copy_entry with a remote SOURCE or TARGET (see the module). UNTRUSTED (default: what files.copy_entry
    takes: the source is a sandbox's): nothing copied is executable, risky files are named. AGENTS: the sandboxes'
    folders (files.copy_entry: one met in a local folder copied out is copied as UNTRUSTED)."""
    if is_remote(source) and is_remote(target):
        raise Refused(tr("Copies from one remote folder to another are not made here; move works within one "
                         "remote system."))
    if is_remote(source):
        # (what comes into a sandbox's folder counts as the agent's: the caller says so for a host folder in one)
        return download(source, relative, target, target_dir, overwrite, bool(untrusted) or target.sandbox, name,
                        limit, entries, cancelled, progress)
    untrusted = source.sandbox if untrusted is None else untrusted
    return upload(source, relative, target, target_dir, overwrite, untrusted, name, limit, entries, cancelled,
                  progress, agents)


def move_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, limit=files.LIMIT,
               entries=MAX_ENTRIES, cancelled=None, progress=None, target_untrusted=False, agents=frozenset()):
    """A move with a remote side: within one remote system a rename; else a copy, and the source removed once the
    copy is complete."""
    if is_remote(source) and is_remote(target) and source.sftp is target.sftp:
        return move_within(source, relative, target, target_dir, overwrite, entries, cancelled)
    # As files.move_entry: the source goes only when the copy is complete - nothing skipped (a link, a special file),
    # nothing changed while it was copied, not cancelled - and then only what was copied unchanged (what was added
    # or changed meanwhile stays). The whole source was deleted: a link in a folder moved, never copied, was lost.
    if is_remote(source) and is_remote(target):
        raise Refused(tr("Copies from one remote folder to another are not made here; move works within one "
                         "remote system."))
    seen = {"files": {}, "folders": set(), "changed": []}
    if is_remote(source):
        # (what comes into a sandbox's folder counts as the agent's)
        result = download(source, relative, target, target_dir, overwrite, bool(target_untrusted) or target.sandbox,
                          None, limit, entries, cancelled, progress, seen)
    else:
        untrusted = source.sandbox if untrusted is None else untrusted
        result = upload(source, relative, target, target_dir, overwrite, untrusted, None, limit, entries, cancelled,
                        progress, agents)
    if result.skipped or result.changed:
        result.kept = tr("The source was kept: {count} item(s) were not copied or changed while they were copied.",
                         count=len(result.skipped) + len(result.changed))
        return result
    if (cancelled is not None and cancelled()) or files._closing:
        result.kept = tr("Cancelled after the copy: the source was kept.")
        return result
    if is_remote(source):
        result.left = source.remove_fetched(relative, seen)
    else:
        parent, name = source._parent(relative)
        try:
            result.left = files._remove_copied(source, parent, name, name, result, 0)
        finally:
            os.close(parent)
    result.moved = not result.left
    if result.left:
        result.kept = tr("Kept in the source (added or changed after it was copied): {list}",
                         list=", ".join(printable(path) for path in result.left[:20]))
    return result


def move_within(source, relative, target, target_dir, overwrite, entries=MAX_ENTRIES, cancelled=None):
    """A move within one remote system, by renames (as files.move_entry within one root): an existing name is replaced
    only with OVERWRITE, and only a file by a file; a folder onto an existing folder is merged into it, entry by entry
    (what cannot go there - a file onto a folder, a folder onto a file - stays, said in Result.left and .kept). (A move
    onto a folder failed with the server's "Failure", where a local one merges.)"""
    folder, name = source.parent(relative)
    into = target.walk(target_dir, last=True)
    old, new = join(folder, name), join(into, name)
    if new == old:
        raise Refused(tr("{name} is already there: the source and the target are the same folder.",
                         name=printable(name)))
    if new.startswith(old.rstrip("/") + "/"):
        raise Refused(tr("A folder cannot be copied or moved into itself."))
    info = source.sftp.lstat(old)
    budget, result = Budget(0, entries, cancelled, TOO_MANY_WALK), Result()
    result.left = _move_renamed(source, old, info, new, name, overwrite, budget, result, 0, top=True)
    result.moved = not result.left
    if result.left:
        result.kept = tr("Kept in the source (it could not replace what is there): {list}",
                         list=", ".join(printable(path) for path in result.left[:20]))
    return result


def _move_renamed(root, old, info, new, shown, overwrite, budget, result, depth, top=False):
    """OLD (INFO: its lstat) renamed NEW, a folder merged into one there (see move_within); the relative paths of what
    stays."""
    budget.entry()
    folder = stat.S_ISDIR(info.mode)
    try:
        existing = root.sftp.lstat(new)
    except FileNotFoundError:
        existing = None
    if existing is None:
        root.sftp.rename(old, new)
    elif not overwrite:
        raise Refused(tr("{name} already exists there.", name=printable(shown)))
    elif folder and stat.S_ISDIR(existing.mode):
        if depth >= MAX_DEPTH:
            raise Refused(tr("Folders nested more than {count} deep are not changed.", count=MAX_DEPTH))
        left = []
        for child, attrs in root.listed(old, budget):
            here = f"{shown}/{child}"
            if "\n" in child or "\r" in child:
                left.append(here)
                continue
            inner = join(old, child)
            left += _move_renamed(root, inner, root.entry(inner, attrs), join(new, child), here, overwrite, budget,
                                  result, depth + 1)
        if not left:
            root.sftp.rmdir(old)
        return left
    elif folder or stat.S_ISDIR(existing.mode):
        if top:
            raise Refused(tr("{name} is a folder there.", name=printable(shown)) if stat.S_ISDIR(existing.mode) else
                          tr("{name} exists there and is not a folder.", name=printable(shown)))
        return [shown]
    else:
        root.sftp.rename(old, new, replace=True)
    if folder:
        result.directories += 1
    else:
        result.files += 1
        result.bytes += attrs_size(info)
    result.paths.append(shown)
    return []


def download(source, relative, target, target_dir, overwrite, untrusted, name, limit, entries, cancelled, progress,
             seen=None):
    """The remote RELATIVE of SOURCE into the folder TARGET_DIR of the local Root TARGET: read into a staging folder
    in the target folder (links and special files skipped, within the budget), from there files.copy_entry. SEEN (a
    move): what was read unchanged is recorded there (_fetch), for remove_fetched.

    The staging folder lies in the target folder - in a sandbox's folder the agent can rename it and put a link to a
    host folder in its place: the Root it is copied from is opened by its path, so it must still be the folder this
    copy made and filled (refused otherwise; a host folder's files were copied into the sandbox)."""
    folder, source_name = source.parent(relative)
    top = join(folder, source_name)
    info = source.sftp.lstat(top)
    if stat.S_ISLNK(info.mode):
        raise Refused(tr("{name} is a symbolic link; links are not copied.", name=printable(source_name)))
    if not (stat.S_ISDIR(info.mode) or stat.S_ISREG(info.mode)):
        raise Refused(tr("{name} is not a regular file.", name=printable(source_name)))
    budget, skipped = Budget(limit, entries, cancelled, progress=progress), []
    target_fd = target.open_dir(target_dir)
    staging = f".sandbox-ai-app-{os.getpid()}-{secrets.token_hex(8)}.part"
    try:
        if not overwrite:  # (refused before anything is read: a whole folder was read into staging first)
            files._refuse_existing(single_name(name or source_name), target_fd)
        try:
            os.mkdir(staging, 0o700, dir_fd=target_fd)
        except OSError as exc:  # (named as what is copied: the staging folder's name is one the human never saw)
            raise type(exc)(exc.errno, exc.strerror, name or source_name) from None
        key = files._register_partial(target_fd, staging, folder=True)
        try:
            stage_fd = os.open(staging, files.DIRECTORY, dir_fd=target_fd)
            seen = {"files": {}, "folders": set(), "changed": []} if seen is None else seen
            try:
                made = os.fstat(stage_fd)
                staged = single_name(check_name(source_name))
                _fetch(source, top, info, stage_fd, staged, staged, budget, skipped, 0, seen)
            finally:
                os.close(stage_fd)
            stage_root = files.Root(os.path.join(target.path, *parts(target_dir), staging), sandbox=untrusted)
            opened = stage_root.open_dir("")
            try:
                now = os.fstat(opened)
            finally:
                os.close(opened)
            if (now.st_dev, now.st_ino) != (made.st_dev, made.st_ino):
                raise Refused(tr("The folder the copy was staged in was replaced while it was filled (by a link to "
                                 "another folder?); nothing was copied from it."))
            result = files.copy_entry(stage_root, staged, target, target_dir, overwrite=overwrite,
                                      untrusted=untrusted, name=name or source_name, limit=limit, entries=entries,
                                      cancelled=cancelled)
            result.skipped = skipped + result.skipped
            result.changed = seen["changed"] + result.changed
            return result
        finally:
            try:
                files._remove(target_fd, staging)
            except OSError:
                pass
            files._forget_partial(key)
    finally:
        os.close(target_fd)


def _fetch(source, path, info, dir_fd, name, shown, budget, skipped, depth, seen=None):
    """The remote PATH (INFO: its lstat) as NAME in the local open folder DIR_FD (a staging folder). SEEN: the folders
    walked and the files read whole and unchanged ({path: identity}; the others in "changed": shorter or longer than
    listed, or changed while they were read)."""
    seen = {"files": {}, "folders": set(), "changed": []} if seen is None else seen
    budget.entry()
    if stat.S_ISLNK(info.mode):
        skipped.append((shown, tr("symbolic link to {target}; not copied", target=source.target_of(path))))
        return
    name = single_name(name)  # (never a path: written relative to DIR_FD - see Sftp.listdir)
    if stat.S_ISDIR(info.mode):
        if depth >= MAX_DEPTH:
            raise Refused(tr("Folders nested more than {count} deep are not copied.", count=MAX_DEPTH))
        os.mkdir(name, 0o700, dir_fd=dir_fd)
        fd = os.open(name, files.DIRECTORY, dir_fd=dir_fd)
        try:
            seen["folders"].add(path)
            for child, attrs in source.listed(path, budget):
                if "\n" in child or "\r" in child:  # (it cannot be named to the server: the whole copy ended there)
                    skipped.append((f"{shown}/{child}", tr("a line break in its name (not sent to a remote system)")))
                    continue
                inner = join(path, child)
                _fetch(source, inner, source.entry(inner, attrs), fd, child, f"{shown}/{child}", budget, skipped,
                       depth + 1, seen)
            os.fchmod(fd, stat.S_IMODE(info.mode) & 0o777 | 0o700)
        finally:
            os.close(fd)
        return
    if not stat.S_ISREG(info.mode):
        skipped.append((shown, tr("not a regular file")))
        return
    handle = source.sftp.open(path, READ_FLAG)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | files.O_CLOEXEC, 0o600, dir_fd=dir_fd)
        try:
            # (one byte beyond the budget is asked for: a file larger than a copy may be is refused by the budget, as a
            # local one - read up to files.LIMIT, it was taken as copied and put in place cut short)
            _data, count, _more = source.sftp.read_file(handle, info.size, budget.limit + 1, budget.cancelled, budget,
                                                        sink=lambda chunk: _write_all(fd, chunk))
            os.fchmod(fd, stat.S_IMODE(info.mode) & 0o777 | 0o600)  # (staged: readable for the copy from it)
            if info.flags & TIMES_ATTR:  # (the copy keeps the times of the file copied)
                try:
                    os.utime(fd, (info.atime, info.mtime))
                except OSError:
                    pass
        finally:
            os.close(fd)
    finally:
        source.sftp.close_handle(handle)
    try:
        after = source.sftp.lstat(path)
    except FileNotFoundError:
        after = None
    if count != info.size or after is None or identity(after) != identity(info):
        seen["changed"].append(shown)
    else:
        seen["files"][path] = identity(info)


def _write_all(fd, chunk):
    """All of CHUNK to FD (os.write may write less: what it left out was missing from the copy)."""
    view = memoryview(chunk)
    while view:
        view = view[os.write(fd, view):]


def upload(source, relative, target, target_dir, overwrite, untrusted, name, limit, entries, cancelled, progress,
           agents=frozenset()):
    """The local RELATIVE of the Root SOURCE into the remote folder TARGET_DIR of TARGET: walked relative to open
    folders without following a link (links skipped and named), each file under a temporary name renamed into place;
    what the copy created is removed unless it completes. UNTRUSTED: no file arrives executable (listed). AGENTS:
    (st_dev, st_ino) of the sandboxes' folders - one met in the folder sent is sent as UNTRUSTED."""
    budget, result = Budget(limit, entries, cancelled, progress=progress), Result()
    parent, source_name = source._parent(relative)
    created = []  # remote folders this copy made (removed unless it completes)
    try:
        name = single_name(check_name(name or source_name))
        info = os.stat(source_name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise Refused(tr("{name} is a symbolic link; links are not copied.", name=printable(source_name)))
        folder = target.walk(target_dir, last=True)
        existing = target.exists(target_dir, [name])
        if existing and not overwrite:
            raise Refused(tr("{name} already exists there.", name=printable(name)))
        if stat.S_ISDIR(info.st_mode):
            fd = source._open_child_dir(parent, source_name)
            try:
                _send_folder(source, fd, info, target, folder, name, name, overwrite, untrusted, budget, result,
                             created, 0, frozenset(agents or ()))
            finally:
                os.close(fd)
        elif stat.S_ISREG(info.st_mode):
            fd, info = files._open_regular(source_name, parent)
            try:
                _send_file(fd, info, target, folder, name, name, overwrite, untrusted, budget, result)
            finally:
                os.close(fd)
        else:
            raise Refused(tr("{name} is not a regular file.", name=printable(source_name)))
        created.clear()  # complete: what was made stays
        return result
    except OSError as exc:
        stayed = _undo_created(target, created)
        if not stayed:
            raise
        # (the connection ended - Cancel of a stalled request, a network gone: what the copy made could not be removed,
        # and stayed there, part filled, under its real name; the error said only that the connection ended)
        raise RemoteError(tr("{error} What was copied so far stayed on {host}, not complete: {folder} - delete it "
                             "there or copy again.", error=printable(exc), host=printable(target.label),
                             folder=printable(stayed[-1]))) from None  # (the outermost: it holds the others)
    finally:
        os.close(parent)
        _undo_created(target, created)


def _undo_created(target, created):
    """Remove the remote folders CREATED (and empty the list); those that could not be removed."""
    stayed = []
    for path in reversed(created):
        try:
            target._delete(path, target.sftp.lstat(path), Budget(0, WALK_ENTRIES), Result(), 0)
        except OSError:
            stayed.append(path)
    created.clear()
    return stayed


def _send_file(fd, info, target, folder, name, shown, overwrite, untrusted, budget, result):
    budget.entry()
    mode = stat.S_IMODE(info.st_mode) & (0o644 if untrusted else 0o777)  # (never writable by others there)
    if untrusted and info.st_mode & 0o111:
        result.executable.append(shown)

    def chunk():
        return os.read(fd, CHUNK)
    target.put(folder, name, chunk, mode, replace=overwrite, cancelled=budget.cancelled, budget=budget,
               times=(info.st_atime, info.st_mtime))
    result.files += 1
    result.bytes += info.st_size
    result.paths.append(shown)
    after = os.fstat(fd)  # (a move removes only the files copied unchanged: files._remove_copied)
    if files._identity(after) == files._identity(info):
        result.ids.add(files._identity(after))
    else:
        result.changed.append(shown)


def _send_folder(source, fd, info, target, folder, name, shown, overwrite, untrusted, budget, result, created, depth,
                 agents=frozenset()):
    budget.entry()
    if depth >= MAX_DEPTH:
        raise Refused(tr("Folders nested more than {count} deep are not copied.", count=MAX_DEPTH))
    walked = os.fstat(fd)
    if (walked.st_dev, walked.st_ino) in agents:  # (a sandbox's folder: the agent's, with all it holds)
        untrusted = True
    path = join(folder, name)
    try:
        existing = target.sftp.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is None:
        target.sftp.mkdir(path, 0o700)
        created.append(path)
    elif not stat.S_ISDIR(existing.mode) or stat.S_ISLNK(existing.mode):
        raise Refused(tr("{name} exists there and is not a folder.", name=printable(shown)))
    result.directories += 1
    result.folders.add((walked.st_dev, walked.st_ino))
    for child in files._names(fd, budget):
        child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
        here = f"{shown}/{child}"
        if "\n" in child or "\r" in child:  # (it cannot be named to the server: the whole copy ended there)
            result.skipped.append((here, tr("a line break in its name (not sent to a remote system)")))
            continue
        if stat.S_ISLNK(child_info.st_mode):
            result.skipped.append((here, tr("symbolic link; not copied")))
            continue
        if stat.S_ISDIR(child_info.st_mode):
            inner = source._open_child_dir(fd, child)
            try:
                if files._other_mount(fd, inner):  # (a move keeps its source then: something was skipped)
                    result.skipped.append((here, tr(files.MOUNTED)))
                    continue
                _send_folder(source, inner, child_info, target, path, child, here, overwrite, untrusted, budget,
                             result, created, depth + 1, agents)
            finally:
                os.close(inner)
        elif stat.S_ISREG(child_info.st_mode):
            inner, child_info = files._open_regular(child, fd)
            try:
                _send_file(inner, child_info, target, path, child, here, overwrite, untrusted, budget, result)
            finally:
                os.close(inner)
        else:
            result.skipped.append((here, tr("not a regular file")))
    if existing is None:  # (a folder that was there keeps its bits, as in a local merge: a 0755 one became 0700)
        # (an agent's folder never arrives writable by others, as files._folder_mode makes a local copy: its 0777
        # folder was one anybody on the remote system could put files in)
        target.sftp.chmod(path, stat.S_IMODE(info.st_mode) & (0o755 if untrusted else 0o777) | 0o700)


def ssh_command(target, known_hosts=None, config=None, ssh="ssh"):
    """The command line of an interactive ssh to TARGET (a terminal: to unlock a key, to see what fails): the options
    of ssh_args without BatchMode and RequestTTY (a passphrase may be asked for there)."""
    args, cleaned = target.ssh_args(known_hosts, config), []
    index = 0
    while index < len(args):
        if args[index] == "-o" and args[index + 1].startswith(("BatchMode=", "RequestTTY=")):
            index += 2
            continue
        cleaned.append(args[index])
        index += 1
    return [ssh, *cleaned, "--", target.host]
