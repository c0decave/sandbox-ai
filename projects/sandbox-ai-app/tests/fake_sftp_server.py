"""A small SFTP version 3 server on stdin and stdout for the tests of remote.py: it serves this machine's files as
OpenSSH's sftp-server does (paths as given, "." the folder it was started in), the requests remote.Sftp sends and the
extensions posix-rename@openssh.com and statvfs@openssh.com.

Its HOME (what "." is): FAKE_SFTP_HOME. Faults for the tests (environment): FAKE_SFTP_HANG=READDIR (or another
request's name) - that request is never answered; FAKE_SFTP_NO_EXTENSIONS=1 - no extensions; FAKE_SFTP_LOG=path -
every request's name and path appended; FAKE_SFTP_HANG_FILE=path - while that file exists, the requests it names (one a
line) are never answered (a server that stalls after the connection was made); FAKE_SFTP_SHORT_READS=1 - every READ
answered with at most 1000 bytes (as servers may); FAKE_SFTP_FOLLOW_IN_LISTINGS=1 - READDIR gives the attributes of
what a link points to (stat, as some servers do), not the link's own; FAKE_SFTP_FAIL_CLOSE=1 - the CLOSE of a file
opened for writing fails (as close() does on a full NFS or quota-bound file system: what was written may not be
there); FAKE_SFTP_EXTRA_NAME=name - every folder's listing also holds that name (with the attributes of the folder's
a.txt, and a request for it is answered with that a.txt), as a malicious server may send one ("../../x", "/home/x");
FAKE_SFTP_READ_FAIL_AT=offset - a READ from that offset on fails (an I/O error); FAKE_SFTP_CASELESS=1 - names are
looked up ignoring their case (a macOS server: a.txt is A.txt), and RENAME refuses a name that "exists" so (as
OpenSSH's sftp-server: link(), or a stat() of the new name); FAKE_SFTP_NO_CHOWN=1 - a SETSTAT of the owner fails (no
root there); FAKE_SFTP_NO_CREATE=1 - an OPEN that would create a file is refused (a folder you may not write); FAKE_SFTP_FOREIGN=name - a file of that name is said to belong to UID 4242, group 4343 (another user's,
with no chown done), and a SETSTAT of an owner is only logged ("CHOWN uid:gid"). Otherwise SETSTAT sets the owner and
group it is given (as OpenSSH's sftp-server does)."""
import errno
import os
import stat
import struct
import sys
import time

NAMES = {1: "INIT", 3: "OPEN", 4: "CLOSE", 5: "READ", 6: "WRITE", 7: "LSTAT", 8: "FSTAT", 9: "SETSTAT", 11: "OPENDIR",
         12: "READDIR", 13: "REMOVE", 14: "MKDIR", 15: "RMDIR", 16: "REALPATH", 17: "STAT", 18: "RENAME",
         19: "READLINK", 200: "EXTENDED"}
STATUS, HANDLE, DATA, NAME, ATTRS, EXTENDED_REPLY = 101, 102, 103, 104, 105, 201
CODES = {errno.ENOENT: 2, errno.EACCES: 3, errno.EPERM: 3}

out = sys.stdout.buffer
read = sys.stdin.buffer
handles, counter = {}, [0]
hang = os.environ.get("FAKE_SFTP_HANG", "")
log = os.environ.get("FAKE_SFTP_LOG")
short_reads = bool(os.environ.get("FAKE_SFTP_SHORT_READS"))
follow_in_listings = bool(os.environ.get("FAKE_SFTP_FOLLOW_IN_LISTINGS"))
fail_close = bool(os.environ.get("FAKE_SFTP_FAIL_CLOSE"))
extra_name = os.environ.get("FAKE_SFTP_EXTRA_NAME", "")
read_fail_at = int(os.environ.get("FAKE_SFTP_READ_FAIL_AT") or -1)
caseless = bool(os.environ.get("FAKE_SFTP_CASELESS"))
no_chown = bool(os.environ.get("FAKE_SFTP_NO_CHOWN"))
foreign = os.environ.get("FAKE_SFTP_FOREIGN", "")
no_create = bool(os.environ.get("FAKE_SFTP_NO_CREATE"))


def folded(path):
    """PATH as a file system that ignores the case finds it: each component that does not exist as it is, the entry of
    its folder of the same name in another case (FAKE_SFTP_CASELESS)."""
    if not caseless or os.path.lexists(path):
        return path
    head, name = os.path.split(path)
    head = folded(head) if head and head != path else head
    try:
        for entry in os.listdir(head or "."):
            if entry.casefold() == name.casefold():
                return os.path.join(head, entry)
    except OSError:
        pass
    return os.path.join(head, name)


def stalled():
    """The requests not answered now (FAKE_SFTP_HANG_FILE, while it exists)."""
    path = os.environ.get("FAKE_SFTP_HANG_FILE")
    try:
        with open(path) as stream:
            return set(stream.read().split())
    except (OSError, TypeError):
        return set()


def string(data):
    data = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
    return struct.pack(">I", len(data)) + data


def attrs(info, path=""):
    uid, gid = (4242, 4343) if foreign and os.path.basename(path) == foreign else (info.st_uid, info.st_gid)
    return struct.pack(">IQIIIII", 0x0F, info.st_size, uid, gid, info.st_mode, int(info.st_atime), int(info.st_mtime))


def send(kind, payload):
    out.write(struct.pack(">IB", len(payload) + 1, kind) + payload)
    out.flush()


def status(ident, code, message=""):
    send(STATUS, struct.pack(">II", ident, code) + string(message) + string(""))


class Parse:
    def __init__(self, data):
        self.data, self.at = data, 0

    def u32(self):
        value = struct.unpack(">I", self.data[self.at:self.at + 4])[0]
        self.at += 4
        return value

    def u64(self):
        value = struct.unpack(">Q", self.data[self.at:self.at + 8])[0]
        self.at += 8
        return value

    def string(self):
        length = self.u32()
        value = self.data[self.at:self.at + length]
        self.at += length
        return value

    def path(self):
        path = os.fsdecode(self.string())
        planted = extra_name.lstrip("/")
        if planted and path.endswith("/" + planted):  # (FAKE_SFTP_EXTRA_NAME: the folder's a.txt stands for it)
            return path[:-len(planted)] + "a.txt"
        return folded(path)

    def attrs(self):
        flags, mode = self.u32(), None
        self.times = self.owner = None
        if flags & 1:
            self.u64()
        if flags & 2:
            self.owner = (self.u32(), self.u32())
        if flags & 4:
            mode = self.u32()
        if flags & 8:
            self.times = (self.u32(), self.u32())
        return mode


def handle(ident, kind, p):
    if kind == 16:  # REALPATH
        path = os.path.realpath(p.path() or ".")
        send(NAME, struct.pack(">II", ident, 1) + string(path) + string(path) + attrs(os.stat(path)))
    elif kind in (7, 17):  # LSTAT, STAT
        path = p.path()
        info = os.lstat(path) if kind == 7 else os.stat(path)
        send(ATTRS, struct.pack(">I", ident) + attrs(info, path))
    elif kind == 11:  # OPENDIR
        path = p.path()
        names = os.listdir(path)
        counter[0] += 1
        key = str(counter[0]).encode()
        handles[key] = ("dir", path, [".", "..", *names, *([extra_name] if extra_name else [])])
        send(HANDLE, struct.pack(">I", ident) + string(key))
    elif kind == 12:  # READDIR
        key = p.string()
        _, path, names = handles[key]
        if not names:
            status(ident, 1, "EOF")
            return
        batch, handles[key] = names[:50], ("dir", path, names[50:])
        payload = b""
        for name in batch:
            if name == extra_name:
                info = os.lstat(os.path.join(path, "a.txt"))
            else:
                info = (os.stat if follow_in_listings else os.lstat)(os.path.join(path, name))
            payload += string(name) + string(name) + attrs(info, name)
        send(NAME, struct.pack(">II", ident, len(batch)) + payload)
    elif kind == 4:  # CLOSE
        entry = handles.pop(p.string(), None)
        if entry and entry[0] == "file":
            os.close(entry[1])
            if fail_close and entry[2]:
                status(ident, 4, "Disk quota exceeded")
                return
        status(ident, 0)
    elif kind == 3:  # OPEN
        path, flags, mode = p.path(), p.u32(), None
        mode = p.attrs()
        if no_create and flags & 8:
            status(ident, 3, "Permission denied")
            return
        os_flags = os.O_RDWR if flags & 3 == 3 else os.O_WRONLY if flags & 2 else os.O_RDONLY
        os_flags |= (os.O_APPEND if flags & 4 else 0) | (os.O_CREAT if flags & 8 else 0) | \
            (os.O_TRUNC if flags & 16 else 0) | (os.O_EXCL if flags & 32 else 0)
        fd = os.open(path, os_flags, 0o666 if mode is None else mode)
        counter[0] += 1
        key = str(counter[0]).encode()
        handles[key] = ("file", fd, bool(flags & 2))
        send(HANDLE, struct.pack(">I", ident) + string(key))
    elif kind == 5:  # READ
        key, offset, length = p.string(), p.u64(), p.u32()
        if 0 <= read_fail_at <= offset:
            status(ident, 4, "Input/output error")
            return
        data = os.pread(handles[key][1], min(length, 1000) if short_reads else length, offset)
        if not data:
            status(ident, 1, "EOF")
        else:
            send(DATA, struct.pack(">I", ident) + string(data))
    elif kind == 6:  # WRITE
        key, offset, data = p.string(), p.u64(), p.string()
        os.pwrite(handles[key][1], data, offset)
        status(ident, 0)
    elif kind == 14:  # MKDIR
        path = p.path()
        mode = p.attrs()
        os.mkdir(path, 0o777 if mode is None else mode)
        status(ident, 0)
    elif kind == 15:  # RMDIR
        os.rmdir(p.path())
        status(ident, 0)
    elif kind == 13:  # REMOVE
        os.unlink(p.path())
        status(ident, 0)
    elif kind == 9:  # SETSTAT
        path = p.path()
        mode = p.attrs()
        if p.owner is not None:
            if no_chown:
                status(ident, 3, "Permission denied")
                return
            if foreign:
                if log:
                    with open(log, "a") as stream:
                        stream.write(f"CHOWN {p.owner[0]}:{p.owner[1]}\n")
            else:
                os.chown(path, *p.owner)
        if mode is not None:
            os.chmod(path, mode & 0o7777)
        if p.times is not None:
            os.utime(path, p.times)
        status(ident, 0)
    elif kind == 18:  # RENAME: refuses an existing name, as OpenSSH's does (a link, then the old name removed)
        old, new = p.path(), p.path()
        if os.path.lexists(new):
            status(ident, 4, "Failure")
            return
        os.rename(old, new)
        status(ident, 0)
    elif kind == 19:  # READLINK
        target = os.readlink(p.path())
        send(NAME, struct.pack(">II", ident, 1) + string(target) + string(target) + struct.pack(">I", 0))
    elif kind == 200:  # EXTENDED
        name = p.string().decode()
        if name == "posix-rename@openssh.com":
            os.rename(p.path(), p.path())
            status(ident, 0)
        elif name == "statvfs@openssh.com":
            info = os.statvfs(p.path())
            send(EXTENDED_REPLY, struct.pack(">I", ident) + struct.pack(
                ">11Q", info.f_bsize, info.f_frsize, info.f_blocks, info.f_bfree, info.f_bavail, info.f_files,
                info.f_ffree, info.f_favail, 0, 0, 255))
        else:
            status(ident, 8, "unsupported")
    else:
        status(ident, 8, "unsupported")


def main():
    if os.environ.get("FAKE_SFTP_HOME"):
        os.chdir(os.environ["FAKE_SFTP_HOME"])  # (the remote HOME: what "." is)
    while True:
        head = read.read(4)
        if len(head) < 4:
            return
        length = struct.unpack(">I", head)[0]
        data = read.read(length)
        kind, body = data[0], data[1:]
        if kind == 1:  # INIT
            extensions = b"" if os.environ.get("FAKE_SFTP_NO_EXTENSIONS") else \
                string("posix-rename@openssh.com") + string("1") + string("statvfs@openssh.com") + string("2")
            send(2, struct.pack(">I", 3) + extensions)
            continue
        p = Parse(body)
        ident = p.u32()
        if log:
            with open(log, "a") as stream:
                start = p.at
                try:
                    shown = p.path() if kind not in (4, 5, 6, 12) else ""
                except Exception:  # noqa: BLE001
                    shown = ""
                p.at = start
                stream.write(f"{NAMES.get(kind, kind)} {shown}\n")
        if NAMES.get(kind) == hang or NAMES.get(kind) in stalled():
            time.sleep(3600)
        try:
            handle(ident, kind, p)
        except OSError as exc:
            status(ident, CODES.get(exc.errno, 4), exc.strerror or str(exc))


if __name__ == "__main__":
    main()
