"""Files below a root directory, for the file browser: listing, copying out to the host and in from it.
No Qt.

Everything below a root (a workspace, a sandbox HOME) may have been written by an agent, so nothing here
follows a symbolic link, not even one in the middle of a path:

- a path is walked one component at a time with openat(O_NOFOLLOW | O_DIRECTORY) relative to the directory
  before it; '..' and absolute paths are refused before anything is opened;
- the directory reached is checked through /proc/self/fd (macOS: fcntl F_GETPATH) to still lie below the root
  (a directory the agent moved away while it was walked is refused);
- files are opened with O_NOFOLLOW | O_NONBLOCK and must be regular files (a FIFO, device or socket is
  never read, and opening one cannot hang);
- new files and directories are created relative to an open directory with O_EXCL | O_NOFOLLOW, so a name
  the agent turned into a symlink in the meantime is refused, never written through;
- symbolic links inside a copied folder are skipped and reported, never copied or followed;
- a walk stays on one file system (_other_mount: Linux's mount ID, macOS's st_dev): a delete refuses a mount point in
  what it deletes before it deletes anything (it emptied a mounted USB stick, share or bind mount, as rm -r) - a
  folder, or one file mounted there (_mounted_file) -, a copy, a move, a count and a recursive owner change skip one
  in a folder and say so (a move then keeps its source; a file mounted there stays in it too);
- a copy stops at LIMIT bytes, MAX_ENTRIES entries and MAX_DEPTH nested folders (counted while a folder is
  read, before anything is collected), can be cancelled, and what it created is removed again on failure;
- files that tools run or read as configuration (.git/config, .envrc, ...) are named in the result, so the
  app can warn that the agent wrote them; files copied out to the host are never executable there (the
  executable bits are dropped and the files listed in Result.executable);
- a file is written under a temporary name in its target folder and gets its name only when it is complete
  (a link, so an existing name is never replaced unless asked): a copy cut short - cancelled, failed, or the
  app quitting - never leaves a truncated file under the real name. A folder a copy created is removed
  with all it holds unless the copy completes (never a folder that was there before). Temporary files and
  created folders of copies still running when the interpreter exits are removed by an atexit handler,
  which also stops those copies.

The two-pane file manager (files_page.py) does everything else through the same primitives, relative to
open directory descriptors and never through a link: copy_entry and move_entry between any two roots (the
host is Root("/", sandbox=False)), and Root.delete_entry, rename_entry, make_folder, make_file (an empty file,
O_EXCL, 0600 where an agent may write), chmod_entry and chown_entry. A move copies first and removes the
source only when the copy is complete, and then only the files it copied unchanged (what the agent added or
changed meanwhile stays, and is reported). Below a sandbox root no set-user-ID, set-group-ID or sticky bit is ever set, and chown goes only to the user and their own
groups; files a rootless Podman sandbox gave a subordinate UID (/etc/subuid) are taken back with
take_ownership (podman unshare: inside Podman's user namespace, UID 0 is the user).

A remote system's folder (remote.RemoteRoot, SSH and SFTP) has the same interface; copy_entry, move_entry and exists_in
hand a remote side to remote.py.

The file manager's editor reads a whole file with Root.read_file (a regular file only, at most EDIT_LIMIT bytes) and
writes it back with Root.write_file: a temporary file in the same folder renamed over the old one, never written
through a link or a hard link, refused (Changed) when the file is no longer the one that was read unless the human
said to replace it; its permission bits stay, set-ID and sticky bits do not. Root.measure counts a folder's size for
the Properties dialog (links counted, never followed; bounded like a delete; its progress readable while it counts).
Root.free_space gives the free and total bytes of a folder's file system (os.fstatvfs of the open folder: Linux and
macOS) for the panes' status lines.
"""
from __future__ import annotations

import atexit
import contextlib
from dataclasses import dataclass, field
import errno
import fnmatch
import os
import json
import pwd
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from .i18n import N_, language, tr

LIMIT = 1 << 30  # bytes per copy
MAX_ENTRIES = 20_000  # files and folders per copy, entries per listing
MAX_DEPTH = 64  # nested folders per copy
CHUNK = 1 << 20
EDIT_LIMIT = 16 << 20  # bytes the editor reads (the hex editor; more: the first part, read-only)
TEXT_LIMIT = 5 << 20  # bytes the text editor takes (more: the hex editor)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NOCTTY = getattr(os, "O_NOCTTY", 0)
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_NOCTTY | O_CLOEXEC
CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | O_NOCTTY | O_CLOEXEC
O_PATH = getattr(os, "O_PATH", 0)
SPECIAL_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
F_GETPATH = 50  # macOS's fcntl F_GETPATH (fcntl has the name from Python 3.9 on, there only)
MAXPATHLEN = 1024


def fd_location(fd):
    """Where the open directory or file FD is now: /proc/self/fd on Linux, fcntl F_GETPATH on macOS; None where
    neither tells."""
    if sys.platform == "darwin":
        import fcntl
        try:
            found = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", F_GETPATH), bytes(MAXPATHLEN))
        except OSError:
            return None
        return os.fsdecode(found.split(b"\0", 1)[0]) or None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
def _mount_of(fd):
    """Which mount the open folder FD lies on: Linux's mount ID (/proc/self/fdinfo: a bind mount of the same file system
    has the same st_dev, a btrfs subvolume another one on the same mount), else its st_dev (macOS: every mount has its
    own)."""
    if sys.platform.startswith("linux"):
        try:
            info = os.open(f"/proc/self/fdinfo/{fd}", os.O_RDONLY | O_CLOEXEC)
        except OSError:
            info = None
        if info is not None:
            try:
                text = os.read(info, 4096)
            except OSError:
                text = b""
            finally:
                os.close(info)
            for line in text.splitlines():
                if line.startswith(b"mnt_id:"):
                    try:
                        return ("mount", int(line.split()[1]))
                    except (IndexError, ValueError):
                        break
    return ("device", os.fstat(fd).st_dev)


def _other_mount(parent_fd, fd):
    """Whether the open folder FD (a folder of the open folder PARENT_FD) is a mount point: another file system (or a
    bind mount) is mounted there. A delete, a move's removal, a copy, a count and a recursive chown stay on one file
    system: a folder that held a mounted USB stick, a network share or a bind mount of the host's emptied it, as rm -r
    does."""
    return _mount_of(fd) != _mount_of(parent_fd)


def _here(fd):
    """(st_dev, mount) of the open folder FD: what _mounted_file compares the entries of the folder with (read once a
    folder)."""
    return os.fstat(fd).st_dev, _mount_of(fd) if O_PATH else None


def _mounted_file(dir_fd, name, info, here=None):
    """Whether NAME of the open folder DIR_FD - INFO its lstat, no folder - is a mount point: a file mounted there (a
    bind mount of one file, as containers have them; another file system's file). A delete refused only a folder that
    was one: one file mounted in a folder was found by the delete itself (EBUSY), half way - what came before it was
    gone. Linux: the mount ID of the entry opened with O_PATH | O_NOFOLLOW (nothing is read, a FIFO never blocks; a
    bind mount of the same file system has the same st_dev); elsewhere (macOS: no file is mounted alone, every mount
    has its own st_dev) its st_dev. A link is never one (a mount goes where a link points). HERE: _here(DIR_FD)."""
    if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False
    device, mount = _here(dir_fd) if here is None else here
    if not O_PATH:
        return info.st_dev != device
    try:
        fd = os.open(name, O_PATH | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return False  # (gone meanwhile: nothing mounted there to empty)
    try:
        return _mount_of(fd) != mount
    finally:
        os.close(fd)


def _mounted_refused(shown):
    return Refused(tr("{path} is a mount point (another file system is mounted there): it is not deleted. Unmount it "
                      "first, or delete what is in it from inside it.", path=printable(shown)))


MOUNTED = N_("another file system is mounted there (a mount point): not walked")
WALK_ENTRIES = 200_000  # files and folders a delete or chown walks
OWNERSHIP_TIMEOUT = 600  # seconds podman unshare may take
TOO_MANY_COPY = N_("More than {count} files and folders; copy a smaller part.")
TOO_MANY_WALK = N_("More than {count} files and folders; choose a smaller part.")
# Files that tools execute or read as configuration when you use the copied folder on the host.
RISKY = (".git/config", ".git/hooks/*", "*/.git/config", "*/.git/hooks/*", ".envrc", "*/.envrc", ".direnv/*",
         ".bashrc", ".bash_profile", ".profile", ".zshrc", ".npmrc", ".pypirc", ".vscode/*", "*/.vscode/*",
         ".idea/*", ".pre-commit-config.yaml", ".gitattributes", ".gitmodules", "Makefile", "*.desktop")


class Refused(ValueError):
    """A path or copy the browser does not do (the message is in the app's language)."""


class Cancelled(Refused):
    pass


class Changed(Refused):
    """The file is no longer the one that was read (Root.write_file with expected)."""


class NoTemporary(Refused):
    """Root.write_file: no temporary file can be made beside the file (a folder you may not write) - the file itself
    may be written in place (write_file(in_place=True), after a question: not atomic)."""


class NotKept(NoTemporary):
    """Root.write_file: a new file renamed over the file would not keep what it has - its owner (another user's file
    you may write), its group, its extended attributes or ACL; the file itself may be written in place (after a
    question), which keeps them."""


class Busy(NoTemporary):
    """Root.write_file: the file cannot be replaced by a new file renamed over it (EBUSY: a file mounted there - a bind
    mount of one file, as containers have them); it may be written in place (after a question)."""


@dataclass
class FileData:
    """A file read for the editor (Root.read_file)."""
    data: bytes
    identity: tuple  # (st_dev, st_ino, size, mtime_ns): what Root.write_file(expected=...) checks
    size: int  # the file's whole size
    truncated: bool  # it holds more than was read (shown, never saved)
    mode: int = 0


def text_of(data, truncated=False):
    """DATA decoded as text (str) when it is valid UTF-8 without NUL bytes, else None (binary: the hex editor). A
    file cut at LIMIT may end inside a character: those last bytes do not count."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(data) - 3 and exc.reason in ("unexpected end of data",):
            return data[:exc.start].decode("utf-8")
        return None


def printable(text):
    """Text for the screen: names are chosen by the agent, so control characters are shown escaped."""
    text = str(text)
    if text.isprintable():  # (most texts: as they are, without a look at each character in Python)
        return text
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in text)


def printable_text(text):
    """Text of several lines for the screen (a file, a diff): its line breaks and tabs kept, every other control
    character shown escaped as printable() does."""
    text = str(text)
    if text.replace("\n", "").replace("\t", "").isprintable():  # (nearly every text: as it is, no look at each character)
        return text
    return "".join(c if c in "\n\t" else printable(c) for c in text)


def parts(relative):
    """The components of a path relative to a root; '..', absolute paths and NUL bytes are refused."""
    if relative is None:
        return []
    relative = os.fspath(relative)
    if "\0" in relative:
        raise Refused(tr("A path must not contain NUL bytes."))
    if relative.startswith("/"):
        raise Refused(tr("Not a path below the folder: {path}", path=printable(relative)))
    result = []
    for part in relative.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise Refused(tr("“..” is not followed: paths stay below the folder."))
        result.append(part)
    return result


def risky(paths):
    """Those of PATHS (relative, with /) that tools run or read as configuration."""
    return [path for path in paths if any(fnmatch.fnmatchcase(path, pattern) or
                                          fnmatch.fnmatchcase(path.split("/", 1)[-1], pattern) for pattern in RISKY)]


@dataclass
class Entry:
    name: str
    kind: str  # "dir", "file", "link" or "other"
    size: int
    mtime: float
    target: str | None = None  # where a symbolic link points (shown, never followed)
    mode: int = 0  # st_mode (of the link itself for a link)
    uid: int = -1
    gid: int = -1


class Listing(list):
    """The entries of a folder; truncated when it holds more than MAX_ENTRIES."""
    truncated = False


@dataclass
class Result:
    files: int = 0
    directories: int = 0
    bytes: int = 0
    skipped: list = field(default_factory=list)  # [(relative path, why)]
    paths: list = field(default_factory=list)  # relative paths of the files copied
    executable: list = field(default_factory=list)  # copied out without the executable bits they had
    # For a move: the files copied unchanged ((st_dev, st_ino, size, mtime_ns) of the source) and the folders
    # walked ((st_dev, st_ino)); only those are removed from the source.
    ids: set = field(default_factory=set)
    folders: set = field(default_factory=set)
    changed: list = field(default_factory=list)  # relative paths of files that changed while they were copied
    moved: bool = False  # a move: the source is gone
    kept: str = ""  # a move: why the source was kept
    left: list = field(default_factory=list)  # a move: relative paths left in the source (added or changed meanwhile)
    removed: int = 0  # files and folders deleted
    # The relative paths of the files copied from a sandbox's folder met inside what was copied (copy_entry's AGENTS):
    # the agent's, wherever the copy goes (their risky ones are named).
    agent_paths: list = field(default_factory=list)

    @property
    def risky(self):
        return risky(self.paths)


# What running copies created and have not completed ({key: (directory fd, name, folder?)}): temporary
# files, and folders made by a copy; removed at interpreter exit.
_partial, _partial_lock, _closing = {}, threading.Lock(), False


def _register_partial(dir_fd, name, folder=False):
    with _partial_lock:
        if _closing:
            raise Cancelled(tr("Cancelled."))
        key = object()
        _partial[key] = (dir_fd, name, folder)
        return key


def _forget_partial(key):
    with _partial_lock:
        _partial.pop(key, None)


@atexit.register
def remove_partial_files():
    """Stop every copy (at its next chunk) and remove the temporary files of those still running. Copies
    run in daemon threads, which the interpreter abandons at exit; their directory descriptors are still
    open while they are registered."""
    global _closing
    with _partial_lock:
        _closing = True
        for dir_fd, name, folder in sorted(_partial.values(), key=lambda entry: entry[2]):  # files first
            try:
                if folder:
                    _remove(dir_fd, name)
                else:
                    os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
        _partial.clear()


@contextlib.contextmanager
def _undo_unless_complete(dir_fd, name):
    """NAME, a folder this copy has just created in DIR_FD, is removed with everything in it unless the
    block completes - when the copy fails or is cancelled, and at interpreter exit while it runs."""
    try:
        key = _register_partial(dir_fd, name, folder=True)
    except BaseException:
        os.rmdir(name, dir_fd=dir_fd)
        raise
    try:
        yield
    except BaseException:
        try:
            _remove(dir_fd, name)
        except OSError:
            pass
        raise
    finally:
        _forget_partial(key)


class Progress:
    """What running operations have done so far (summed over the items of one job): written in its thread,
    read by the page's timer."""

    def __init__(self):
        self.count, self.bytes = 0, 0


class Budget:
    def __init__(self, limit, entries, cancelled=None, too_many=TOO_MANY_COPY, progress=None):
        self.limit, self.entries, self.used, self.count = limit, entries, 0, 0
        self.cancelled = cancelled or (lambda: False)
        self.too_many_text, self.progress = too_many, progress

    def too_many(self):
        return Refused(tr(self.too_many_text, count=self.entries))

    def check(self):
        if _closing or self.cancelled():
            raise Cancelled(tr("Cancelled."))

    def entry(self, count=1):
        self.check()
        self.count += count
        if self.progress is not None:
            self.progress.count += count
        if self.count > self.entries:
            raise self.too_many()

    def take(self, size):
        self.check()
        self.used += size
        if self.progress is not None:
            self.progress.bytes += size
        if self.used > self.limit:
            raise Refused(tr("More than {size}; copy a smaller part.", size=size_text(self.limit)))


def kind_of(mode):
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _names(fd, budget=None):
    """The names in the open folder FD, counted against BUDGET while they are read."""
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if budget is not None:
                budget.entry(0)
                if budget.count + len(names) >= budget.entries:
                    raise budget.too_many()
            names.append(entry.name)
    return sorted(names)


def _remove(dir_fd, name, depth=0):
    """Remove NAME (a file, a link or a whole folder) in the directory DIR_FD without following links."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    if depth > MAX_DEPTH + 1:
        return  # deeper than any copy goes: not made by the app
    fd = os.open(name, DIRECTORY, dir_fd=dir_fd)
    try:
        for child in _names(fd):
            _remove(fd, child, depth + 1)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)


def _open_regular(name, dir_fd=None):
    """A descriptor of the regular file NAME, opened without following a link and without blocking on a FIFO."""
    try:
        fd = os.open(name, READ, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name))) from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(tr("{name} is not a regular file.", name=printable(name)))
        os.set_blocking(fd, True)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _create(name, dir_fd, mode):
    try:
        return os.open(name, CREATE, mode, dir_fd=dir_fd)
    except FileExistsError:
        raise Refused(tr("{name} already exists there.", name=printable(name))) from None


def _temporary_name():
    """A short name of its own: never derived from (and never longer than allowed by) the target's name."""
    return f".sandbox-ai-app-{os.getpid()}-{secrets.token_hex(8)}.part"


def _create_temporary(temporary, dir_fd, mode, name):
    """The temporary file of NAME (_create): an error names NAME - the temporary name ("Permission denied:
    '.sandbox-ai-app-1234-....part'") is one the human never saw."""
    try:
        return _create(temporary, dir_fd, mode)
    except OSError as exc:
        raise type(exc)(exc.errno, exc.strerror, name) from None


def _refuse_existing(name, dir_fd):
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise Refused(tr("{name} already exists there.", name=printable(name)))


def _place(temporary, name, dir_fd, overwrite, identity=None):
    """Give the complete file TEMPORARY its NAME in DIR_FD. Without OVERWRITE an existing NAME is refused,
    atomically (a hard link fails if the name exists); with it, the file is renamed over NAME (never written
    through, so a link the agent put there is replaced, not followed). IDENTITY (st_dev, st_ino) of the file
    written: the agent can swap the temporary name meanwhile (the folder is its), so it must still be that file;
    and a link there is never followed (a hard link to what it points at would hand over a host file)."""
    if identity is not None:
        try:
            now = os.stat(temporary, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != identity:
            raise Refused(tr("{name}: the copy was replaced while it was written; nothing was placed.",
                             name=printable(name)))
    if overwrite:
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISDIR(existing.st_mode):
            raise Refused(tr("{name} is a folder there.", name=printable(name)))
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return
    try:
        os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        raise Refused(tr("{name} already exists there.", name=printable(name))) from None
    except OSError as exc:
        if exc.errno == errno.ENOENT:  # the temporary file is gone (removed at exit)
            raise
        # No hard links here (EPERM, ENOTSUP, EMLINK; some FUSE file systems say EIO or EACCES): checked,
        # then renamed (the rename never writes through a link).
        _refuse_existing(name, dir_fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return
    os.unlink(temporary, dir_fd=dir_fd)


def _write_file(fd, target_dir, name, mode, overwrite, budget):
    """Copy the open file FD to NAME in TARGET_DIR through a temporary file (see _place); on any failure,
    or when the interpreter exits meanwhile, the temporary file is removed and NAME is untouched."""
    if not overwrite:
        _refuse_existing(name, target_dir)  # before anything is copied
    before = os.fstat(fd)
    temporary = _temporary_name()
    target = _create_temporary(temporary, target_dir, mode, name)
    try:
        key = _register_partial(target_dir, temporary)
    except BaseException:
        os.close(target)
        os.unlink(temporary, dir_fd=target_dir)
        raise
    try:
        try:
            written = os.fstat(target)
            _copy_data(fd, target, budget)
            _keep_times(target, before)
        finally:
            os.close(target)
        budget.check()
        _place(temporary, name, target_dir, overwrite, (written.st_dev, written.st_ino))
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=target_dir)
        except FileNotFoundError:
            pass
        raise
    finally:
        _forget_partial(key)


def _write_in_place(parent, name, data, expected):
    """DATA written into the file NAME of PARENT itself (Root.write_file's IN_PLACE): opened without following a link
    or blocking on a FIFO, checked on the descriptor to be the regular file EXPECTED (identity) with no other name (a
    hard link would carry the write elsewhere), written, cut to its length and synced. Not atomic: a write cut short
    leaves it half written - asked first."""
    try:
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_NOCTTY | O_CLOEXEC, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name))) from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(tr("{name} is not a regular file.", name=printable(name)))
        if expected is not None and _identity(info) != tuple(expected):
            raise Changed(tr("{name} changed since it was opened.", name=printable(name)))
        if info.st_nlink > 1:
            raise Refused(tr("{name} has other names (hard links): it is not written in place.",
                             name=printable(name)))
        os.set_blocking(fd, True)
        view, at = memoryview(bytes(data)), 0
        while at < len(view):
            at += os.pwrite(fd, view[at:], at)
        os.ftruncate(fd, len(view))
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
        return _identity(os.fstat(fd))
    finally:
        os.close(fd)


def _keep_owner(fd, made, info, name):
    """The new file FD (MADE: its fstat) gets the owner and group of the file INFO it replaces: renamed over a file of
    another user (one you may write: its group's, or root's of yours), it became yours - its group the folder's or
    yours. Where that cannot be done (only root gives a file to another user), NotKept: the human is asked to write it
    in place."""
    uid = info.st_uid if info.st_uid != made.st_uid else -1
    gid = info.st_gid if info.st_gid != made.st_gid else -1
    if uid == -1 and gid == -1:
        return
    try:
        os.fchown(fd, uid, gid)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES, errno.EINVAL):
            raise
        raise NotKept(tr("{name} belongs to UID {uid} (group {gid}): saved through a new file renamed over it, it "
                         "would become yours. It can be written in place (it stays theirs).", name=printable(name),
                         uid=info.st_uid, gid=info.st_gid)) from None


XATTRS_SKIPPED = ("security.", "trusted.")  # (the system's labels and capabilities; root's own: never carried over)


def _keep_attributes(parent, name, fd):
    """The new file FD gets the extended attributes - an ACL among them - of the file NAME of PARENT it replaces (an
    ACL that let others in was lost, quietly). Linux: each one copied (not security.*, never a file's capabilities,
    nor trusted.*); macOS: fcopyfile with COPYFILE_ACL | COPYFILE_XATTR. One that cannot be copied: NotKept (the
    human is asked to write the file in place, which keeps them)."""
    if sys.platform == "darwin":
        _keep_attributes_macos(parent, name, fd)
        return
    if not hasattr(os, "listxattr"):
        return
    where = f"/proc/self/fd/{parent}/{name}"  # (the folder by its descriptor, the name itself: never followed)
    try:
        found = os.listxattr(where, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOENT):
            return  # (none here, or no /proc: nothing that could be kept)
        raise
    for attribute in found:
        if attribute.startswith(XATTRS_SKIPPED):
            continue
        try:
            os.setxattr(fd, attribute, os.getxattr(where, attribute, follow_symlinks=False))
        except OSError:
            raise NotKept(tr("{name} has the extended attribute {attribute} (an ACL, a tag), which a new file renamed "
                             "over it would not keep. It can be written in place (it keeps it).", name=printable(name),
                             attribute=printable(attribute))) from None


# Attributes macOS sets itself and no process may set on another file (com.apple.provenance: every file an app of a
# downloaded or ad-hoc signed program writes has it): one missing on the new file is no loss - every save asked to be
# written in place for it.
XATTRS_SKIPPED_MACOS = ("com.apple.provenance", "com.apple.rootless", "com.apple.decmpfs", "com.apple.system.")


def _xattr_names_macos(libc, fd):
    """The names of the extended attributes of the open file FD (flistxattr), or None where they cannot be read."""
    import ctypes
    try:
        size = libc.flistxattr(fd, None, 0, 0)
        if size <= 0:
            return set() if size == 0 else None
        buffer = ctypes.create_string_buffer(size)
        size = libc.flistxattr(fd, buffer, size, 0)
    except (OSError, AttributeError, TypeError):
        return None
    if size < 0:
        return None
    return {os.fsdecode(name) for name in buffer.raw[:size].split(b"\0") if name}


def _keep_attributes_macos(parent, name, fd, libc=None):
    """macOS: fcopyfile COPYFILE_ACL, then COPYFILE_XATTR. One the system keeps for itself (XATTRS_SKIPPED_MACOS) that
    the new file did not get is left out; any other one not copied: NotKept."""
    try:
        if libc is None:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
        copy = libc.fcopyfile
    except (OSError, AttributeError, ImportError):
        return
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_CLOEXEC, dir_fd=parent)
    except OSError:
        raise NotKept(tr("{name}: its ACL and extended attributes cannot be read to be kept. It can be written in place "
                         "(it keeps them).", name=printable(name))) from None
    try:
        failed = copy(source, fd, None, 1) != 0  # COPYFILE_ACL
        if not failed and copy(source, fd, None, 4) != 0:  # COPYFILE_XATTR
            had, got = _xattr_names_macos(libc, source), _xattr_names_macos(libc, fd)
            failed = had is None or got is None or any(
                not attribute.startswith(XATTRS_SKIPPED_MACOS) for attribute in had - got)
        if failed:
            raise NotKept(tr("{name}: its ACL and extended attributes could not be kept. It can be written in place "
                             "(it keeps them).", name=printable(name)))
    finally:
        os.close(source)


def _keep_times(fd, info):
    """The copy FD gets the times of the file INFO describes: a copy - and a move between two roots, which copies - made
    every file look changed at the time of the copy (make, rsync and backups took it so). Where the file system
    refuses, the copy keeps its own."""
    try:
        os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
    except (OSError, NotImplementedError):
        pass


def _mode(info, untrusted=False):
    """The mode a copy of the file INFO is made with: its own permission bits, so a private file stays private (every
    copy was 0o666 or 0o777 less the umask: a key of 0600 became 0644), the umask on top; never set-ID or sticky
    bits; never executable when the agent chose the contents (UNTRUSTED); its owner may always read and write it."""
    return (stat.S_IMODE(info.st_mode) & (0o666 if untrusted else 0o777)) | 0o600


def _folder_mode(fd, untrusted=False):
    """The mode a copy of the open folder FD is made with (as _mode: a private folder stays private)."""
    return (stat.S_IMODE(os.fstat(fd).st_mode) & (0o755 if untrusted else 0o777)) | 0o700


def _copy_data(source, target, budget):
    while True:
        chunk = os.read(source, CHUNK)
        if not chunk:
            return
        budget.take(len(chunk))
        view = memoryview(chunk)
        while view:
            view = view[os.write(target, view):]


def _too_deep(depth):
    if depth >= MAX_DEPTH:
        raise Refused(tr("Folders nested more than {count} deep are not copied.", count=MAX_DEPTH))


class Root:
    """One directory whose contents may be shown and copied, e.g. a sandbox's workspace."""

    def __init__(self, path, label=None, sandbox=True):
        # The root itself is chosen on the host (a workspace, a session's HOME): its own path is resolved
        # once; everything below it is walked without following links. SANDBOX: an agent may have written
        # what is below it (copies out of it are never executable, no set-ID or sticky bits are set in it,
        # take_ownership works only there); Root("/", sandbox=False) is the host.
        self.path = os.path.realpath(os.fspath(path))
        self.label = label or self.path
        self.sandbox = sandbox
        fd = os.open(self.path, DIRECTORY)
        if sys.platform == "darwin":
            # As F_GETPATH names it (the case on disk): what _inside compares the folders reached with.
            self.path = fd_location(fd) or self.path
        os.close(fd)
        self.hook = None  # tests: called between the steps of a walk

    # -- walking --

    def _inside(self, fd):
        where = fd_location(fd)
        if where is None:
            return  # neither /proc nor F_GETPATH: the walk with O_NOFOLLOW alone keeps to the root
        if where != self.path and not where.startswith(self.path.rstrip("/") + "/"):
            raise Refused(tr("That folder is no longer below the folder shown (moved while it was opened)."))

    def open_dir(self, relative=""):
        """A descriptor of the directory RELATIVE below the root (the caller closes it)."""
        fd = os.open(self.path, DIRECTORY)
        try:
            for part in parts(relative):
                if self.hook:
                    self.hook(fd, part)
                try:
                    child = os.open(part, DIRECTORY, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise Refused(tr("{name} is not a folder (symbolic links are not followed).",
                                         name=printable(part))) from None
                    raise
                os.close(fd)
                fd = child
            self._inside(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _parent(self, relative):
        components = parts(relative)
        if not components:
            raise Refused(tr("Choose a file or folder below the folder shown."))
        return self.open_dir("/".join(components[:-1])), components[-1]

    # -- reading --

    def list(self, relative=""):
        """Listing of the folder RELATIVE, folders first; links are described, not followed. At most
        MAX_ENTRIES entries (then Listing.truncated is set)."""
        fd = self.open_dir(relative)
        found = Listing()
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(found) >= MAX_ENTRIES:
                        found.truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    kind = kind_of(info.st_mode)
                    target = None
                    if kind == "link":
                        try:
                            target = os.readlink(entry.name, dir_fd=fd)
                        except OSError:
                            target = "?"
                    found.append(Entry(entry.name, kind, info.st_size, info.st_mtime, target, info.st_mode,
                                       info.st_uid, info.st_gid))
        finally:
            os.close(fd)
        result = Listing(sorted(found, key=lambda e: (e.kind != "dir", e.name.lower(), e.name)))
        result.truncated = found.truncated
        return result

    def lstat(self, relative):
        parent, name = self._parent(relative)
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)

    def read(self, relative, limit=1 << 20):
        """Up to LIMIT bytes of the regular file RELATIVE (for a preview)."""
        parent, name = self._parent(relative)
        try:
            fd, _ = _open_regular(name, parent)
        finally:
            os.close(parent)
        try:
            return os.read(fd, limit)
        finally:
            os.close(fd)

    def stat_entry(self, relative):
        """lstat of RELATIVE (a link itself, never what it points to); "" is the root's own folder."""
        if not parts(relative):
            fd = self.open_dir("")
            try:
                return os.fstat(fd)
            finally:
                os.close(fd)
        return self.lstat(relative)

    def readlink(self, relative):
        """Where the link RELATIVE points (read, never followed)."""
        parent, name = self._parent(relative)
        try:
            return os.readlink(name, dir_fd=parent)
        finally:
            os.close(parent)

    # -- the editor: a whole file in, a whole file back --

    def read_file(self, relative, limit=None):
        """FileData of the regular file RELATIVE (opened without following a link, never a FIFO or device): at most
        LIMIT (default EDIT_LIMIT) bytes (FileData.truncated when it holds more)."""
        limit = EDIT_LIMIT if limit is None else limit
        parent, name = self._parent(relative)
        try:
            fd, info = _open_regular(name, parent)
        finally:
            os.close(parent)
        try:
            # Written meanwhile (the agent writes in place): the bytes read may be half old, half new, and the
            # identity taken afterwards matched the new file - a save replaced the agent's write with that mix,
            # unasked. Read again; still changing, the identity is the one before the read (a save asks first).
            for _attempt in range(3):
                before = _identity(os.fstat(fd))
                os.lseek(fd, 0, os.SEEK_SET)
                chunks, size = [], 0
                while size <= limit:
                    chunk = os.read(fd, min(CHUNK, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                data = b"".join(chunks)
                after = os.fstat(fd)
                if _identity(after) == before:
                    break
            identity = _identity(after) if _identity(after) == before else before
        finally:
            os.close(fd)
        truncated = len(data) > limit
        return FileData(data[:limit], identity, after.st_size, truncated, info.st_mode)

    def write_file(self, relative, data, expected=None, in_place=False, mode=None):
        """Write DATA as the file RELATIVE: through a temporary file in its folder that is then renamed over it (never
        written through a link or a hard link the agent put there; a write cut short leaves the file as it was). Its
        permission bits stay (no set-user-ID, set-group-ID or sticky bit). EXPECTED (FileData.identity of what was
        read): if the file is no longer that (changed, replaced or removed meanwhile), Changed is raised and nothing
        is written. Returns the identity of the file written. In a folder you may not write no temporary file can be
        made: NoTemporary; IN_PLACE (after the human said yes) writes the file itself then (_write_in_place). MODE: the
        permission bits of the file as it was read (FileData.mode), which a file no longer there is made with again
        (without: 0666 less the umask - a private file removed meanwhile came back readable by everyone)."""
        parent, name = self._parent(relative)
        if in_place:
            try:
                return _write_in_place(parent, name, data, expected)
            finally:
                os.close(parent)
        try:
            try:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if now is not None and stat.S_ISLNK(now.st_mode):
                raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name)))
            if now is not None and not stat.S_ISREG(now.st_mode):
                raise Refused(tr("{name} is not a regular file.", name=printable(name)))
            # Replaced by a rename, a file you may not write (read-only, another user's) was written over all the same,
            # and became yours: as every editor, only a file you may write.
            if now is not None and not os.access(name, os.W_OK, dir_fd=parent):
                if os.fstatvfs(parent).f_flag & getattr(os, "ST_RDONLY", 1):  # (no change of permissions helps there)
                    raise Refused(tr("{name} is on a read-only file system: it was not written.", name=printable(name)))
                raise Refused(tr("{name} is read-only for you (its permissions or its owner): it was not written. "
                                 "Change its permissions first.", name=printable(name)))
            if expected is not None and (now is None or _identity(now) != tuple(expected)):
                raise Changed(tr("{name} changed since it was opened.", name=printable(name)))
            temporary = _temporary_name()
            try:
                # (a new file: the umask applies - unless it is one that was read and is gone now: its own bits)
                fd = _create_temporary(temporary, parent, 0o600 if now is not None or mode is not None else 0o666, name)
            except PermissionError:
                if now is None:
                    raise
                raise NoTemporary(tr("{name} cannot be saved through a temporary file: you may not write its folder. "
                                     "It can be written in place.", name=printable(name))) from None
            try:
                key = _register_partial(parent, temporary)
            except BaseException:
                os.close(fd)
                os.unlink(temporary, dir_fd=parent)
                raise
            try:
                try:
                    written = os.fstat(fd)
                    view = memoryview(bytes(data))
                    while view:
                        view = view[os.write(fd, view):]
                    if now is not None:  # (renamed over it, the file became yours, without its attributes: kept, or asked)
                        _keep_owner(fd, written, now, name)
                        _keep_attributes(parent, name, fd)
                    if now is not None or mode is not None:
                        os.fchmod(fd, stat.S_IMODE(now.st_mode if now is not None else mode) & 0o777)
                    try:  # (on the disk before it takes the name: after a crash, never an empty file there)
                        os.fsync(fd)
                    except OSError as exc:
                        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                            raise
                    final = os.fstat(fd)
                finally:
                    os.close(fd)
                if expected is not None:  # (once more, right before: the agent may write meanwhile)
                    try:
                        again = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        again = None
                    if again is None or _identity(again) != tuple(expected):
                        raise Changed(tr("{name} changed since it was opened.", name=printable(name)))
                try:
                    _place(temporary, name, parent, True, (written.st_dev, written.st_ino))
                except OSError as exc:
                    if exc.errno != errno.EBUSY:
                        raise
                    # (a file mounted there - a bind mount of one file: no rename replaces it; Save failed with "Device
                    # or resource busy" and no other way was offered)
                    raise Busy(tr("{name} is a mount point (a file mounted there): a new file cannot be renamed over "
                                  "it. It can be written in place.", name=printable(name))) from None
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                raise
            finally:
                _forget_partial(key)
            # What was saved: the file as it is now - unless it is no longer the one written or no longer as long (the
            # agent wrote it or put another there right after: that write was taken for this save's, and the next save
            # replaced it unasked); then the file as it was written, so that the next save finds it changed. (Its
            # modification time alone may be another after the close on a network file system: not compared.)
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (now.st_dev, now.st_ino, now.st_size) == (final.st_dev, final.st_ino, final.st_size):
                return _identity(now)
            return _identity(final)
        finally:
            os.close(parent)

    def measure(self, relative, entries=WALK_ENTRIES, cancelled=None, progress=None, seen=None):
        """The size of the file or folder RELATIVE ("" the root's folder) with all it holds: Result.bytes, .files and
        .directories. Links are counted, never followed; a file of several names (hard links) is counted once in
        bytes (SEEN: a set of (st_dev, st_ino) already counted, shared by the counts of one Properties dialog; each
        name counts as a file); at most ENTRIES entries and MAX_DEPTH nested folders (then Refused, with what was
        counted so far on its .partial). PROGRESS (a Progress): the entries and bytes counted so far, while it counts
        (the Properties dialog shows them)."""
        budget, result = Budget(0, entries, cancelled, TOO_MANY_WALK, progress), Result()
        budget.seen = set() if seen is None else seen
        try:
            if not parts(relative):
                fd = self.open_dir("")
                try:
                    result.directories += 1
                    self._measure_folder(fd, budget, result, 0)
                finally:
                    os.close(fd)
            else:
                parent, name = self._parent(relative)
                try:
                    self._measure(parent, name, budget, result, 0, name)
                finally:
                    os.close(parent)
        except (OSError, ValueError) as exc:
            exc.partial = result
            raise
        return result

    def _measure(self, dir_fd, name, budget, result, depth, shown=None):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        budget.entry()
        if stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not counted.", count=MAX_DEPTH))
            result.directories += 1
            fd = self._open_child_dir(dir_fd, name)
            try:
                # (a file system mounted in it - a network share, a USB stick: not counted, as du -x; the folder chosen
                # itself may be one)
                if depth and _other_mount(dir_fd, fd):
                    result.skipped.append((shown or name, tr(MOUNTED)))
                else:
                    self._measure_folder(fd, budget, result, depth, shown or name)
            finally:
                os.close(fd)
        else:
            size = info.st_size if stat.S_ISREG(info.st_mode) else 0
            if size and info.st_nlink > 1:  # (another name of a file counted already: its bytes are there once)
                if (info.st_dev, info.st_ino) in budget.seen:
                    size = 0
                else:
                    budget.seen.add((info.st_dev, info.st_ino))
            result.files += 1
            result.bytes += size
            if budget.progress is not None:
                budget.progress.bytes += size

    def free_space(self, relative=""):
        """(free, total) bytes of the file system that holds the folder RELATIVE: os.fstatvfs of the folder opened
        as every other (never through a link); free is what an unprivileged user may still write (f_bavail)."""
        fd = self.open_dir(relative)
        try:
            info = os.fstatvfs(fd)
        finally:
            os.close(fd)
        unit = info.f_frsize or info.f_bsize
        return info.f_bavail * unit, info.f_blocks * unit

    def _measure_folder(self, fd, budget, result, depth, shown=""):
        for child in _names(fd, budget):
            self._measure(fd, child, budget, result, depth + 1, f"{shown}/{child}" if shown else child)

    # -- copying out --

    def copy_out(self, relative, destination, limit=LIMIT, entries=MAX_ENTRIES, cancelled=None):
        """Copy the file or folder RELATIVE into the host folder DESTINATION (which must exist); nothing
        there is overwritten. Links inside a folder are skipped and listed in Result.skipped."""
        budget, result = Budget(limit, entries, cancelled), Result()
        components = parts(relative)
        target_dir = os.open(os.fspath(destination), os.O_RDONLY | os.O_DIRECTORY | O_CLOEXEC)
        try:
            if components:
                parent, name = self._parent(relative)
            else:
                parent, name = None, os.path.basename(self.path) or "root"
            try:
                if parent is None:
                    source = self.open_dir("")
                    info = os.fstat(source)
                else:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    source = None
                if stat.S_ISLNK(info.st_mode):
                    raise Refused(tr("{name} is a symbolic link; links are not copied.", name=printable(name)))
                try:
                    if stat.S_ISDIR(info.st_mode):
                        if source is None:
                            source = self._open_child_dir(parent, name)
                        # refuses an existing name: everything below is ours
                        self._mkdir(name, target_dir, _folder_mode(source, untrusted=True))
                        with _undo_unless_complete(target_dir, name):
                            self._copy_tree_out(source, target_dir, name, name, budget, result, 0)
                    else:
                        fd, info = _open_regular(name, parent)
                        try:
                            self._copy_file_out(fd, info, target_dir, name, name, budget, result)
                        finally:
                            os.close(fd)
                finally:
                    if source is not None:
                        os.close(source)
            finally:
                if parent is not None:
                    os.close(parent)
        finally:
            os.close(target_dir)
        return result

    def _open_child_dir(self, parent, name):
        try:
            fd = os.open(name, DIRECTORY, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused(tr("{name} is not a folder (symbolic links are not followed).",
                                 name=printable(name))) from None
            raise
        self._inside(fd)
        return fd

    @staticmethod
    def _mkdir(name, dir_fd, mode=0o777):
        try:
            os.mkdir(name, mode, dir_fd=dir_fd)
        except FileExistsError:
            raise Refused(tr("{name} already exists there.", name=printable(name))) from None

    @staticmethod
    def _copy_file_out(fd, info, target_dir, name, shown, budget, result):
        budget.entry()
        # Never executable on the host: the agent chose the contents (the umask still applies).
        _write_file(fd, target_dir, name, _mode(info, untrusted=True), False, budget)
        if info.st_mode & 0o111:
            result.executable.append(shown)
        result.files += 1
        result.bytes += info.st_size
        result.paths.append(shown)

    def _copy_tree_out(self, source, target_parent, name, shown, budget, result, depth):
        _too_deep(depth)
        budget.entry()
        result.directories += 1
        target = os.open(name, DIRECTORY, dir_fd=target_parent)
        try:
            for child in _names(source, budget):
                path = f"{shown}/{child}"
                info = os.stat(child, dir_fd=source, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    result.skipped.append((path, tr("symbolic link")))
                elif stat.S_ISDIR(info.st_mode):
                    sub = self._open_child_dir(source, child)
                    try:
                        if _other_mount(source, sub):
                            result.skipped.append((path, tr(MOUNTED)))
                            continue
                        self._mkdir(child, target, _folder_mode(sub, untrusted=True))
                        self._copy_tree_out(sub, target, child, path, budget, result, depth + 1)
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(info.st_mode):
                    try:
                        fd, info = _open_regular(child, source)
                    except Refused as exc:  # replaced by a link or a FIFO since it was listed
                        result.skipped.append((path, str(exc)))
                        continue
                    try:
                        self._copy_file_out(fd, info, target, child, path, budget, result)
                    finally:
                        os.close(fd)
                else:
                    result.skipped.append((path, tr("not a regular file")))
        finally:
            os.close(target)

    # -- copying in --

    def copy_in(self, source, relative_dir="", overwrite=False, limit=LIMIT, entries=MAX_ENTRIES, cancelled=None):
        """Copy the host file or folder SOURCE into the folder RELATIVE_DIR below the root. An existing
        file is replaced only with OVERWRITE (by renaming a new file over it: never written through, so a
        hard or symbolic link the agent put there is replaced, not followed). Links in SOURCE are skipped."""
        budget, result = Budget(limit, entries, cancelled), Result()
        source = os.path.abspath(os.fspath(source))
        name = os.path.basename(source.rstrip("/"))
        if not name or name in (".", ".."):
            raise Refused(tr("Choose a file or folder to copy."))
        info = os.lstat(source)
        if stat.S_ISLNK(info.st_mode):
            raise Refused(tr("{name} is a symbolic link; links are not copied.", name=printable(source)))
        target_dir = self.open_dir(relative_dir)
        try:
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(source, DIRECTORY)
                try:
                    self._copy_tree_in(fd, target_dir, name, name, overwrite, budget, result, 0)
                finally:
                    os.close(fd)
            else:
                fd, info = _open_regular(source)
                try:
                    self._copy_file_in(fd, info, target_dir, name, name, overwrite, budget, result)
                finally:
                    os.close(fd)
        finally:
            os.close(target_dir)
        return result

    @staticmethod
    def _copy_file_in(fd, info, target_dir, name, shown, overwrite, budget, result):
        budget.entry()
        _write_file(fd, target_dir, name, _mode(info), overwrite, budget)
        result.files += 1
        result.bytes += info.st_size
        result.paths.append(shown)

    def _copy_tree_in(self, source, target_parent, name, shown, overwrite, budget, result, depth, fresh=False):
        """Copy the folder SOURCE as NAME into TARGET_PARENT. FRESH: an enclosing folder was created by this
        copy (and is removed as a whole if it does not complete). The outermost folder this copy creates is
        removed unless the copy completes; folders that were there before (OVERWRITE) are never removed."""
        _too_deep(depth)
        budget.entry()
        created = False
        try:
            os.mkdir(name, _folder_mode(source), dir_fd=target_parent)
            created = True
        except FileExistsError:
            if not overwrite:
                raise Refused(tr("{name} already exists there.", name=printable(name))) from None
        if created and not fresh:
            with _undo_unless_complete(target_parent, name):
                self._fill_tree_in(source, target_parent, name, shown, overwrite, budget, result, depth, True)
        else:
            self._fill_tree_in(source, target_parent, name, shown, overwrite, budget, result, depth,
                               fresh or created)

    def _fill_tree_in(self, source, target_parent, name, shown, overwrite, budget, result, depth, fresh):
        target = self._open_child_dir(target_parent, name)
        result.directories += 1
        try:
            for child in _names(source, budget):
                path = f"{shown}/{child}"
                info = os.stat(child, dir_fd=source, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    result.skipped.append((path, tr("symbolic link")))
                elif stat.S_ISDIR(info.st_mode):
                    sub = os.open(child, DIRECTORY, dir_fd=source)
                    try:
                        if _other_mount(source, sub):
                            result.skipped.append((path, tr(MOUNTED)))
                            continue
                        self._copy_tree_in(sub, target, child, path, overwrite, budget, result, depth + 1, fresh)
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(info.st_mode):
                    try:
                        fd, info = _open_regular(child, source)
                    except Refused as exc:
                        result.skipped.append((path, str(exc)))
                        continue
                    try:
                        self._copy_file_in(fd, info, target, child, path, overwrite, budget, result)
                    finally:
                        os.close(fd)
                else:
                    result.skipped.append((path, tr("not a regular file")))
        finally:
            os.close(target)


    # -- changing what is there (the two-pane file manager) --

    def delete_entry(self, relative, entries=WALK_ENTRIES, cancelled=None, progress=None):
        """Delete the file, link or folder (with all it holds) RELATIVE; a link is removed, never followed.
        Counted first (too many entries or folders nested too deep: refused before anything is deleted);
        CANCELLED stops it at the next entry (what was deleted stays deleted: Result.removed counts it, also
        on the exception's .partial)."""
        parent, name = self._parent(relative)
        result = Result()
        try:
            # (counted first: a mount point anywhere in it is refused before anything is deleted)
            self._count(parent, name, name, Budget(0, entries, cancelled, TOO_MANY_WALK), 0)
            try:
                self._delete(parent, name, name, Budget(0, entries, cancelled, TOO_MANY_WALK, progress), result, 0)
            except (OSError, ValueError) as exc:
                exc.partial = result
                raise
        finally:
            os.close(parent)
        return result

    def _count(self, dir_fd, name, shown, budget, depth, here=None):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        budget.entry()
        if stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not changed.", count=MAX_DEPTH))
            fd = self._open_child_dir(dir_fd, name)
            try:
                if _other_mount(dir_fd, fd):
                    raise _mounted_refused(shown)
                mine = _here(fd)
                for child in _names(fd, budget):
                    self._count(fd, child, f"{shown}/{child}", budget, depth + 1, mine)
            finally:
                os.close(fd)
        elif _mounted_file(dir_fd, name, info, here):  # (one file mounted in a folder: refused before anything is deleted)
            raise _mounted_refused(shown)

    def _delete(self, dir_fd, name, shown, budget, result, depth):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        budget.entry()
        if stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not changed.", count=MAX_DEPTH))
            fd = self._open_child_dir(dir_fd, name)
            try:
                if _other_mount(dir_fd, fd):  # (mounted after it was counted)
                    raise _mounted_refused(shown)
                for child in _names(fd):
                    self._delete(fd, child, f"{shown}/{child}", budget, result, depth + 1)
            finally:
                os.close(fd)
            os.rmdir(name, dir_fd=dir_fd)
            result.directories += 1
        else:
            os.unlink(name, dir_fd=dir_fd)  # a link itself, never what it points to
            result.files += 1
        result.removed += 1

    def rename_entry(self, relative, new_name):
        """Give the file, link or folder RELATIVE the name NEW_NAME in the same folder; an existing name is
        never replaced (atomically where the kernel can: renameat2 RENAME_NOREPLACE)."""
        new_name = single_name(new_name)
        parent, name = self._parent(relative)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
            if name != new_name:
                _rename_noreplace(parent, name, parent, new_name)
        finally:
            os.close(parent)

    def make_folder(self, relative_dir, name):
        """A new folder NAME in the folder RELATIVE_DIR (an existing name, a link too, is refused)."""
        name = single_name(name)
        fd = self.open_dir(relative_dir)
        try:
            self._mkdir(name, fd)
        finally:
            os.close(fd)

    def make_file(self, relative_dir, name, untrusted=None):
        """A new empty file NAME in the folder RELATIVE_DIR, created relative to the open folder with O_CREAT | O_EXCL
        | O_NOFOLLOW (an existing name, a link too, is refused; nothing is ever written through one). Its mode: 0600
        where an agent may have written (UNTRUSTED; default: a sandbox's root), else 0644 less the umask."""
        name = single_name(name)
        untrusted = self.sandbox if untrusted is None else untrusted
        fd = self.open_dir(relative_dir)
        try:
            os.close(_create(name, fd, 0o600 if untrusted else 0o644))
        finally:
            os.close(fd)

    def chmod_entry(self, relative, mode, special=False):
        """Set the permission bits MODE (0..0o7777) of the file or folder RELATIVE; a link is refused (it has
        none of its own, and what it points to is not changed). Set-user-ID, set-group-ID and sticky bits only
        with SPECIAL and never below a sandbox root; one the item has already may stay (a folder below a set-group-ID
        one inherits it: no bit of such a folder in a sandbox could be changed) or go."""
        mode = int(mode)
        if not 0 <= mode <= 0o7777:
            raise Refused(tr("Not a permission mode: {mode}", mode=oct(mode)))
        parent, name = self._parent(relative)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name)))
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise Refused(tr("{name} is not a regular file.", name=printable(name)))
            if mode & SPECIAL_BITS & ~stat.S_IMODE(info.st_mode) and (self.sandbox or not special):
                raise Refused(tr("Set-user-ID, set-group-ID and sticky bits are not set here (never below a "
                                 "sandbox's folder)."))
            _chmod_at(parent, name, info, mode)
        finally:
            os.close(parent)

    def chown_entry(self, relative, uid=-1, gid=-1, recursive=False, entries=WALK_ENTRIES, cancelled=None,
                    progress=None):
        """Give the file, link or folder RELATIVE (with RECURSIVE: and all it holds) the owner UID and the
        group GID (-1: unchanged). Only to the user and one of the user's groups; links themselves are changed,
        never what they point to. Entries that cannot be changed (owned by someone else) are listed in
        Result.skipped; Result.files counts those changed."""
        if uid not in (-1, os.getuid()):
            raise Refused(tr("Files can only be given to you (UID {uid}).", uid=os.getuid()))
        if gid != -1 and gid not in user_groups():
            raise Refused(tr("Group {gid} is not one of your groups.", gid=gid))
        parent, name = self._parent(relative)
        result = Result()
        try:
            self._chown(parent, name, name, uid, gid, recursive,
                        Budget(0, entries, cancelled, TOO_MANY_WALK, progress), result, 0)
        finally:
            os.close(parent)
        return result

    def _chown(self, dir_fd, name, shown, uid, gid, recursive, budget, result, depth, here=None):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        budget.entry()
        fd = None
        if recursive and stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not changed.", count=MAX_DEPTH))
            fd = self._open_child_dir(dir_fd, name)
        try:
            # (a file system mounted in a folder - neither its top, which the name leads to, nor what it holds: the
            # folder chosen itself may be one)
            if fd is not None and depth and _other_mount(dir_fd, fd) or \
                    fd is None and depth and _mounted_file(dir_fd, name, info, here):  # (a file mounted there alone)
                result.skipped.append((shown, tr(MOUNTED)))
                return
            try:
                os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
                result.files += 1
            except OSError as exc:
                result.skipped.append((shown, exc.strerror or str(exc)))
            if fd is not None:
                mine = _here(fd)
                for child in _names(fd, budget):
                    self._chown(fd, child, f"{shown}/{child}", uid, gid, True, budget, result, depth + 1, mine)
        finally:
            if fd is not None:
                os.close(fd)


# -- between two roots (and within one) --

def single_name(name):
    """NAME if it is the name of one file or folder (no '/', not '.' or '..', no NUL, at most 255 bytes)."""
    name = os.fspath(name)
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        raise Refused(tr("Not a name for a file or folder: {name}", name=printable(name)))
    if len(os.fsencode(name)) > 255:
        raise Refused(tr("That name is too long."))
    return name


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _where(fd):
    return fd_location(fd)


def _refuse_into_itself(source_fd, target_fd):
    source, target = _where(source_fd), _where(target_fd)
    if source and target and (target == source or target.startswith(source.rstrip("/") + "/")):
        raise Refused(tr("A folder cannot be copied or moved into itself."))


def _refuse_same_place(source_parent, name, target_fd, new_name):
    one, other = os.fstat(source_parent), os.fstat(target_fd)
    if (one.st_dev, one.st_ino) == (other.st_dev, other.st_ino) and name == new_name:
        raise Refused(tr("{name} is already there: the source and the target are the same folder.",
                         name=printable(name)))


def _renameat2(src_dir, name, dst_dir, new_name):
    """A rename that never replaces, in one step: renameat2(RENAME_NOREPLACE) (Linux), else renameatx_np(RENAME_EXCL)
    (macOS 10.12 on; without it the new name was looked at first, then renamed - a name made in between was replaced).
    0 when renamed, else its errno; None where libc has neither."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
    except (OSError, ImportError):
        return None
    for function, flag in (("renameat2", 1), ("renameatx_np", 4)):  # RENAME_NOREPLACE, RENAME_EXCL
        call = getattr(libc, function, None)
        if call is None:
            continue
        if call(src_dir, os.fsencode(name), dst_dir, os.fsencode(new_name), flag) == 0:
            return 0
        return ctypes.get_errno()
    return None


def _rename_noreplace(src_dir, name, dst_dir, new_name):
    """Rename without replacing an existing NEW_NAME: renameat2(RENAME_NOREPLACE) where libc and the file
    system have it, else checked first, then renamed. A new name that is the old one in other letter case on a file
    system that ignores the case (Linux: FAT and exFAT of a USB stick, a casefolded folder) "exists" for renameat2 -
    the file itself - and a plain rename does nothing there: renamed through a temporary name (a.txt -> A.txt was
    refused as "already exists")."""
    error = _renameat2(src_dir, name, dst_dir, new_name)
    if error == 0:
        return
    if error == errno.EEXIST:
        if not _same_name_but_case(src_dir, name, dst_dir, new_name):
            raise Refused(tr("{name} already exists there.", name=printable(new_name)))
        temporary = _temporary_name()
        if _renameat2(src_dir, name, src_dir, temporary) != 0:
            raise Refused(tr("{name} already exists there.", name=printable(new_name)))
        again = _renameat2(src_dir, temporary, dst_dir, new_name)
        if again != 0:
            if _renameat2(src_dir, temporary, src_dir, name) != 0:  # (back as it was)
                raise Refused(tr("{name} could not be renamed and is named {temporary} now.", name=printable(name),
                                 temporary=temporary))
            if again == errno.EEXIST:
                raise Refused(tr("{name} already exists there.", name=printable(new_name)))
            raise OSError(again, os.strerror(again), name)
        return
    if error is not None and error not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise OSError(error, os.strerror(error), name)
    if not _same_name_but_case(src_dir, name, dst_dir, new_name):
        _refuse_existing(new_name, dst_dir)
    os.rename(name, new_name, src_dir_fd=src_dir, dst_dir_fd=dst_dir)


def same_name_folded(name, new_name):
    """Whether NEW_NAME is NAME in other letter case or another Unicode normalisation (what macOS's file systems take
    for one name)."""
    import unicodedata
    return unicodedata.normalize("NFC", name).casefold() == unicodedata.normalize("NFC", new_name).casefold()


def _same_name_but_case(src_dir, name, dst_dir, new_name):
    """True when NEW_NAME is NAME itself in other letter case (a.txt -> A.txt on a file system that ignores the case,
    as macOS's: the new name "exists" - it is the file being renamed - and every such rename was refused there), or in
    another Unicode normalisation (APFS ignores that too: "é" typed is NFC, the name listed from HFS+ NFD)."""
    import unicodedata
    if unicodedata.normalize("NFC", name).casefold() != unicodedata.normalize("NFC", new_name).casefold():
        return False
    try:
        source = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
        target = os.stat(new_name, dir_fd=dst_dir, follow_symlinks=False)
    except OSError:
        return False
    return (source.st_dev, source.st_ino) == (target.st_dev, target.st_ino)


def _chmod_at(parent, name, info, mode):
    """chmod of NAME in PARENT, the file or folder INFO describes, without following a link: opened with
    O_PATH | O_NOFOLLOW (also a file nobody may read), checked to be that file, changed through /proc."""
    if O_PATH:
        fd = os.open(name, O_PATH | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=parent)
        try:
            _same_file(fd, info, name)
            try:
                os.chmod(f"/proc/self/fd/{fd}", mode)
                return
            except FileNotFoundError:
                pass  # no /proc: opened for real below
        finally:
            os.close(fd)
    try:
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(name, DIRECTORY, dir_fd=parent)
        else:
            fd, _ = _open_regular(name, parent)
    except PermissionError:
        # A file nobody may read (mode 000, 200) or a folder nobody may list cannot be opened to be changed (macOS has
        # no O_PATH): its permissions could never be changed here. fchmodat with AT_SYMLINK_NOFOLLOW (macOS: never
        # through a link - a link put there meanwhile has its own bits changed, nothing else), on what is still the
        # entry checked.
        if os.chmod not in os.supports_follow_symlinks or os.chmod not in os.supports_dir_fd:
            raise
        _same_file_at(parent, name, info)
        os.chmod(name, mode, dir_fd=parent, follow_symlinks=False)
        return
    try:
        _same_file(fd, info, name)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _same_file(fd, info, name):
    now = os.fstat(fd)
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or stat.S_IFMT(now.st_mode) != stat.S_IFMT(info.st_mode):
        raise Refused(tr("{name} was replaced meanwhile; nothing was changed.", name=printable(name)))


def _same_file_at(parent, name, info):
    """NAME in PARENT is still the entry INFO describes (lstat: never through a link)."""
    now = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or stat.S_IFMT(now.st_mode) != stat.S_IFMT(info.st_mode):
        raise Refused(tr("{name} was replaced meanwhile; nothing was changed.", name=printable(name)))


def user_groups():
    """The IDs of the user's groups."""
    return sorted(set(os.getgroups()) | {os.getgid()})


def copy_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, name=None, limit=LIMIT,
               entries=MAX_ENTRIES, cancelled=None, progress=None, agents=frozenset()):
    """Copy the file or folder RELATIVE of the Root SOURCE into the folder TARGET_DIR of the Root TARGET (as
    NAME, default its own). Both sides are walked relative to open directory descriptors without following a
    link; links inside a folder are skipped and listed. UNTRUSTED (default: SOURCE.sandbox): an agent may
    have written the source, so nothing copied is executable (listed in Result.executable) and files that
    tools run are named in Result.risky. AGENTS: (st_dev, st_ino) of the sandboxes' folders - one met in what is
    copied (the folder copied itself, or one inside it: ~/projects with the workspace ~/projects/ws) is copied as
    UNTRUSTED with all it holds, its files listed in Result.agent_paths. An existing file is replaced only with
    OVERWRITE (renamed over, never written through); an existing folder is then filled. What the copy created is
    removed unless it completes."""
    if getattr(source, "remote", False) or getattr(target, "remote", False):  # (a remote system: remote.py)
        from . import remote
        return remote.copy_entry(source, relative, target, target_dir, overwrite, untrusted, name, limit, entries,
                                 cancelled, progress, agents)
    untrusted = source.sandbox if untrusted is None else untrusted
    budget, result = Budget(limit, entries, cancelled, progress=progress), Result()
    parent, source_name = source._parent(relative)
    try:
        name = single_name(name or source_name)
        info = os.stat(source_name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise Refused(tr("{name} is a symbolic link; links are not copied.", name=printable(source_name)))
        target_fd = target.open_dir(target_dir)
        try:
            _refuse_same_place(parent, source_name, target_fd, name)
            if stat.S_ISDIR(info.st_mode):
                fd = source._open_child_dir(parent, source_name)
                try:
                    _refuse_into_itself(fd, target_fd)
                    _copy_folder(source, fd, target, target_fd, name, name, overwrite, untrusted, budget, result, 0,
                                 False, frozenset(), frozenset(agents or ()))
                finally:
                    os.close(fd)
            elif stat.S_ISREG(info.st_mode):
                fd, info = _open_regular(source_name, parent)
                try:
                    _copy_one(fd, info, target_fd, name, name, overwrite, untrusted, budget, result)
                finally:
                    os.close(fd)
            else:
                raise Refused(tr("{name} is not a regular file.", name=printable(source_name)))
        finally:
            os.close(target_fd)
    finally:
        os.close(parent)
    return result


def _copy_one(fd, info, target_dir, name, shown, overwrite, untrusted, budget, result, inside=False):
    budget.entry()
    _write_file(fd, target_dir, name, _mode(info, untrusted), overwrite, budget)
    after = os.fstat(fd)
    if untrusted and info.st_mode & 0o111:
        result.executable.append(shown)
    result.files += 1
    result.bytes += info.st_size
    result.paths.append(shown)
    if inside:  # (in a sandbox's folder met inside what is copied: _copy_folder's AGENTS)
        result.agent_paths.append(shown)
    if _identity(after) == _identity(info):
        result.ids.add(_identity(after))
    else:
        result.changed.append(shown)


def _copy_folder(source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth,
                 fresh, avoid, agents=frozenset(), inside=False):
    """Copy the open folder SOURCE_FD as NAME into TARGET_PARENT (see Root._copy_tree_in: FRESH, and the outermost
    folder created is removed unless the copy completes). AVOID: (st_dev, st_ino) of the folders this copy
    writes into, never walked as a source (a folder copied into itself). AGENTS: the sandboxes' folders (copy_entry);
    INSIDE: this folder lies in one of them."""
    _too_deep(depth)
    if agents and not inside:
        mine = os.fstat(source_fd)
        if (mine.st_dev, mine.st_ino) in agents:  # (a sandbox's folder: the agent's, with all it holds)
            untrusted = inside = True
    budget.entry()
    created = False
    try:
        os.mkdir(name, _folder_mode(source_fd, untrusted), dir_fd=target_parent)
        created = True
    except FileExistsError:
        if not overwrite:
            raise Refused(tr("{name} already exists there.", name=printable(name))) from None
    arguments = (source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth)
    if created and not fresh:
        with _undo_unless_complete(target_parent, name):
            _fill_folder(*arguments, True, avoid, agents, inside)
    else:
        _fill_folder(*arguments, fresh or created, avoid, agents, inside)


def _fill_folder(source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth,
                 fresh, avoid, agents=frozenset(), inside=False):
    target_fd = target._open_child_dir(target_parent, name)
    try:
        here, mine = os.fstat(target_fd), os.fstat(source_fd)
        avoid = avoid | {(here.st_dev, here.st_ino)}
        result.directories += 1
        result.folders.add((mine.st_dev, mine.st_ino))
        for child in _names(source_fd, budget):
            path = f"{shown}/{child}"
            try:
                info = os.stat(child, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                result.skipped.append((path, tr("removed while it was copied")))
                continue
            if stat.S_ISLNK(info.st_mode):
                result.skipped.append((path, tr("symbolic link")))
            elif stat.S_ISDIR(info.st_mode):
                if (info.st_dev, info.st_ino) in avoid:
                    result.skipped.append((path, tr("the copy itself")))
                    continue
                sub = source._open_child_dir(source_fd, child)
                try:
                    if _other_mount(source_fd, sub):  # (a move keeps its source then: something was skipped)
                        result.skipped.append((path, tr(MOUNTED)))
                        continue
                    _copy_folder(source, sub, target, target_fd, child, path, overwrite, untrusted, budget, result,
                                 depth + 1, fresh, avoid, agents, inside)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(info.st_mode):
                try:
                    fd, info = _open_regular(child, source_fd)
                except Refused as exc:
                    result.skipped.append((path, str(exc)))
                    continue
                try:
                    _copy_one(fd, info, target_fd, child, path, overwrite, untrusted, budget, result, inside)
                finally:
                    os.close(fd)
            else:
                result.skipped.append((path, tr("not a regular file")))
    finally:
        os.close(target_fd)


def move_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, limit=LIMIT,
               entries=MAX_ENTRIES, cancelled=None, progress=None, target_untrusted=None, agents=frozenset()):
    """Move the file or folder RELATIVE of SOURCE into TARGET_DIR of TARGET. Within one root: renamed (an
    existing name only replaced with OVERWRITE, and only a file by a file) - unless what an agent may have written
    (UNTRUSTED, default SOURCE.sandbox) goes where none writes (TARGET_UNTRUSTED False, default TARGET.sandbox: the
    host's Root("/") from a sandbox's folder to an ordinary one): that is copied as the agent's (never executable,
    the files tools run named) like a move between roots. Otherwise copied (copy_entry), and
    the source is removed only when the copy is complete - nothing skipped, nothing changed while it was
    copied, not cancelled - and then only the files copied unchanged and the folders walked (Result.left:
    what stayed because it appeared or changed meanwhile; Result.kept: why the source was kept). AGENTS: the
    sandboxes' folders, as copy_entry takes them."""
    if getattr(source, "remote", False) or getattr(target, "remote", False):  # (a remote system: remote.py)
        from . import remote
        return remote.move_entry(source, relative, target, target_dir, overwrite, untrusted, limit, entries,
                                 cancelled, progress, bool(target_untrusted), agents)
    untrusted = source.sandbox if untrusted is None else untrusted
    target_untrusted = target.sandbox if target_untrusted is None else target_untrusted
    if source.path == target.path and source.sandbox == target.sandbox and (target_untrusted or not untrusted):
        moved = _rename_within(source, relative, target_dir, overwrite)
        if moved is not None:
            return moved
    result = copy_entry(source, relative, target, target_dir, overwrite, untrusted, limit=limit, entries=entries,
                        cancelled=cancelled, progress=progress, agents=agents)
    if result.skipped or result.changed:
        result.kept = tr("The source was kept: {count} item(s) were not copied or changed while they were copied.",
                         count=len(result.skipped) + len(result.changed))
        return result
    if (cancelled and cancelled()) or _closing:
        result.kept = tr("Cancelled after the copy: the source was kept.")
        return result
    parent, name = source._parent(relative)
    try:
        result.left = _remove_copied(source, parent, name, name, result, 0)
    finally:
        os.close(parent)
    result.moved = not result.left
    if result.left:
        result.kept = tr("Kept in the source (added or changed after it was copied): {list}",
                         list=", ".join(printable(path) for path in result.left[:20]))
    return result


def _rename_within(root, relative, target_dir, overwrite):
    """A move inside ROOT by rename (Result), or None when it must be copied (another file system, or a folder
    to be merged into an existing one)."""
    parent, name = root._parent(relative)
    try:
        target_fd = root.open_dir(target_dir)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _refuse_same_place(parent, name, target_fd, name)
            if stat.S_ISDIR(info.st_mode):
                fd = root._open_child_dir(parent, name)
                try:
                    _refuse_into_itself(fd, target_fd)
                finally:
                    os.close(fd)
            try:
                existing = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            try:
                if existing is None:
                    _rename_noreplace(parent, name, target_fd, name)
                elif not overwrite:
                    raise Refused(tr("{name} already exists there.", name=printable(name)))
                elif stat.S_ISDIR(existing.st_mode) or stat.S_ISDIR(info.st_mode):
                    return None
                else:
                    os.replace(name, name, src_dir_fd=parent, dst_dir_fd=target_fd)  # the name, never through it
                    # Two names of one file (hard links): rename(2) does nothing and succeeds - the source stayed, and
                    # the move said it had moved. The name is removed then (the file is there under the target's).
                    try:
                        left = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        there = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        left = there = None
                    if left is not None and (left.st_dev, left.st_ino) == (there.st_dev, there.st_ino) == \
                            (info.st_dev, info.st_ino):
                        os.unlink(name, dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    return None
                raise
        finally:
            os.close(target_fd)
    finally:
        os.close(parent)
    result = Result(moved=True, paths=[name])
    if stat.S_ISDIR(info.st_mode):
        result.directories = 1
    else:
        result.files, result.bytes = 1, info.st_size
    return result


def _remove_copied(root, dir_fd, name, shown, result, depth):
    """Remove from the source what RESULT copied unchanged; the relative paths of what stays."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return []
    if stat.S_ISDIR(info.st_mode):
        if (info.st_dev, info.st_ino) not in result.folders or depth > MAX_DEPTH:
            return [shown]
        fd = root._open_child_dir(dir_fd, name)
        left = []
        try:
            if _other_mount(dir_fd, fd):  # (a mount point moved as a whole: copied; what is mounted there stays)
                return [shown]
            for child in _names(fd):
                left += _remove_copied(root, fd, child, f"{shown}/{child}", result, depth + 1)
        finally:
            os.close(fd)
        if not left:
            try:
                os.rmdir(name, dir_fd=dir_fd)
            except OSError:
                left.append(shown)
        return left
    # (a file mounted there - its copy made - stays: its unlink failed, "Device or resource busy", half way through the
    # removal, and the move said only that)
    if stat.S_ISREG(info.st_mode) and _identity(info) in result.ids and not _mounted_file(dir_fd, name, info):
        os.unlink(name, dir_fd=dir_fd)
        return []
    return [shown]


def entry_infos(root, items):
    """([(name, relative, lstat, link target or None)], [problems]) of ITEMS [(name, relative)] of ROOT (the
    Properties dialog's; read in a thread for a remote root: files_page.on_root)."""
    infos, problems = [], []
    for name, relative in items:
        try:
            info = root.stat_entry(relative)
            target = root.readlink(relative) if stat.S_ISLNK(info.st_mode) else None
            infos.append((name, relative, info, target))
        except (OSError, ValueError) as exc:
            problems.append(f"{printable(name or relative or '/')}: {printable(exc)}")
    return infos, problems


def exists_in(root, relative_dir, names):
    """Those of NAMES that exist (as anything, a link too) in the folder RELATIVE_DIR of ROOT."""
    if getattr(root, "remote", False):
        return root.exists(relative_dir, names)
    fd = root.open_dir(relative_dir)
    found = []
    try:
        for name in names:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                found.append(name)
            except (FileNotFoundError, ValueError):
                pass
            except OSError as exc:  # (a name too long to be one is none there: the others are still looked at)
                if exc.errno != errno.ENAMETOOLONG:
                    raise
    finally:
        os.close(fd)
    return found


# -- files a rootless Podman sandbox gave subordinate IDs --

def subordinate_ranges(path="/etc/subuid", uid=None):
    """[(first, count)] of the subordinate IDs /etc/subuid (or /etc/subgid) delegates to the user (by name
    or by UID)."""
    uid = os.getuid() if uid is None else uid
    names = {str(uid)}
    try:
        names.add(pwd.getpwuid(uid).pw_name)
    except KeyError:
        pass
    ranges = []
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                fields = line.strip().split(":")
                if len(fields) == 3 and fields[0] in names:
                    try:
                        first, count = int(fields[1]), int(fields[2])
                    except ValueError:
                        continue
                    if first > 0 and count > 0:
                        ranges.append((first, count))
    except OSError:
        pass
    return ranges


def ownership_problem(uid, ranges, user=None):
    """None if a file owned by UID may be taken over (the user's own, or a subordinate ID of the user's: a file
    a rootless Podman sandbox wrote as a UID other than its root); else why not."""
    user = os.getuid() if user is None else user
    if uid == user or any(first <= uid < first + count for first, count in ranges):
        return None
    return tr("It belongs to UID {uid}: neither you nor one of your subordinate IDs (/etc/subuid), so no rootless "
              "Podman sandbox of yours made it.", uid=uid)


# Run by `podman unshare` inside rootless Podman's user namespace (sandbox-ai's pods use --userns=host: that
# namespace itself, where UID 0 is the user and 1.. are the subordinate IDs). Standard library only; walks
# like Root (dir fds, O_NOFOLLOW, below the root, checked to be the entry checked before) and changes links
# themselves (lchown), never what they point to. Prints a JSON summary.
OWNERSHIP_HELPER = r'''
import json, os, stat, sys
root, relative = sys.argv[1], sys.argv[2]
dev, ino, uid, gid = (int(value) for value in sys.argv[3:7])
D = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LIMIT, DEPTH = 200000, 64
done = {"changed": 0, "skipped": [], "error": None}
count = [0]

def inside(fd):
    try:
        where = os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return
    if where != root and not where.startswith(root.rstrip("/") + "/"):
        raise ValueError("no longer below the folder")

def mount(fd):
    try:
        with open("/proc/self/fdinfo/%d" % fd) as lines:
            for line in lines:
                if line.startswith("mnt_id:"):
                    return ("mount", int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        pass
    return ("device", os.fstat(fd).st_dev)

def own(dir_fd, name, shown, depth):
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    count[0] += 1
    if count[0] > LIMIT:
        raise ValueError("more than %d files and folders" % LIMIT)
    if depth and not stat.S_ISLNK(info.st_mode):  # (a file system mounted there: neither its top nor what it holds; a
        # file mounted there alone neither)
        try:
            probe = os.open(name, D if stat.S_ISDIR(info.st_mode) else os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=dir_fd)
        except OSError:
            if stat.S_ISDIR(info.st_mode):
                raise
            probe = None
        try:
            other = probe is not None and mount(probe) != mount(dir_fd)
        finally:
            if probe is not None:
                os.close(probe)
        if other:
            done["skipped"].append([shown, "another file system is mounted there (a mount point): not walked"])
            return
    if (info.st_uid, info.st_gid) != (uid, gid):
        try:
            os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
            done["changed"] += 1
        except OSError as exc:
            if len(done["skipped"]) < 50:
                done["skipped"].append([shown, exc.strerror or str(exc)])
    if stat.S_ISDIR(info.st_mode):
        if depth >= DEPTH:
            done["skipped"].append([shown, "nested too deep"])
            return
        fd = os.open(name, D, dir_fd=dir_fd)
        try:
            inside(fd)
            for child in sorted(os.listdir(fd)):
                own(fd, child, shown + "/" + child, depth + 1)
        finally:
            os.close(fd)

try:
    names = [part for part in relative.split("/") if part not in ("", ".")]
    if not names or ".." in names or relative.startswith("/"):
        raise ValueError("not a path below the folder")
    fd = os.open(root, D)
    try:
        for part in names[:-1]:
            child = os.open(part, D, dir_fd=fd)
            os.close(fd)
            fd = child
        inside(fd)
        top = os.stat(names[-1], dir_fd=fd, follow_symlinks=False)
        if (top.st_dev, top.st_ino) != (dev, ino) or stat.S_ISLNK(top.st_mode):
            raise ValueError("replaced since it was checked")
        own(fd, names[-1], names[-1], 0)
    finally:
        os.close(fd)
except (OSError, ValueError) as exc:
    done["error"] = str(exc)
print(json.dumps(done))
'''


def ownership_command(podman, root_path, relative, info, owner=(0, 0)):
    """The command that makes OWNER (in Podman's user namespace; 0:0 is the user) own RELATIVE below
    ROOT_PATH, which INFO (its lstat) describes: `podman unshare chown -R 0:0`, done by OWNERSHIP_HELPER so
    that no link is followed on the way or below."""
    return [podman, "unshare", sys.executable, "-I", "-c", OWNERSHIP_HELPER, root_path, relative,
            str(info.st_dev), str(info.st_ino), str(owner[0]), str(owner[1])]


def take_ownership(root, relative, cancelled=None, podman=None, subuid="/etc/subuid", owner=(0, 0),
                   timeout=OWNERSHIP_TIMEOUT):
    """Make the user own RELATIVE below the sandbox root ROOT and all it holds, also what a rootless Podman
    sandbox wrote as one of the user's subordinate UIDs. Only below a sandbox root, never through a link (the
    entry is checked with lstat right before, and the helper checks it is still that entry); its owner must be
    the user or a subordinate UID of the user's. Result.files: entries changed; Result.skipped: those not."""
    if not getattr(root, "sandbox", False):
        raise Refused(tr("Taking ownership works only below a sandbox's folder."))
    components = parts(relative)
    if not components:
        raise Refused(tr("Choose a file or folder below the folder shown."))
    relative = "/".join(components)
    info = root.lstat(relative)
    if stat.S_ISLNK(info.st_mode):
        raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(components[-1])))
    problem = ownership_problem(info.st_uid, subordinate_ranges(subuid))
    if problem:
        raise Refused(f"{printable(components[-1])}: {problem}")
    podman = podman or shutil.which("podman")
    if not podman:
        raise Refused(tr("Podman was not found: taking ownership needs rootless Podman (podman unshare)."))
    process = subprocess.Popen(ownership_command(podman, root.path, relative, info, owner), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if (cancelled and cancelled()) or _closing or time.monotonic() > deadline:
                # The whole process group (start_new_session): podman unshare runs the helper as a child of its own (and
                # re-executes itself in the user namespace first) - podman alone was killed, the helper went on changing
                # owners after Cancel, and the job waited for it (it held the output pipe).
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # (one that left the group still holds the pipes: not waited for)
                    for stream in (process.stdout, process.stderr):
                        stream.close()
                if time.monotonic() > deadline and not (cancelled and cancelled()) and not _closing:
                    # (its time limit, not Cancel: it said "Cancelled." to someone who had cancelled nothing)
                    raise Cancelled(tr("podman unshare took longer than {seconds} s and was stopped; what it changed "
                                       "so far stays changed.", seconds=f"{timeout:g}")) from None
                raise Cancelled(tr("Cancelled.")) from None
    try:
        summary = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        summary = None
    if process.returncode != 0 or not isinstance(summary, dict):
        raise Refused(tr("podman unshare failed: {error}", error=printable((err or out).strip()[-500:])))
    if summary.get("error"):
        raise Refused(tr("Taking ownership failed: {error}", error=printable(summary["error"])))
    result = Result(files=int(summary.get("changed") or 0))
    result.skipped = [(str(path), str(why)) for path, why in summary.get("skipped") or []]
    return result


def time_text(seconds, form="%Y-%m-%d %H:%M"):
    """SECONDS (a file's time) as local time in FORM; a time localtime() cannot take (before or after what the system
    can say: an agent may set it, touch -d @4611686018427387904 - one such file made its whole folder fail to list)
    as its number of seconds."""
    try:
        return time.strftime(form, time.localtime(seconds))
    except (OverflowError, OSError, ValueError):
        return tr("{seconds} s after 1970", seconds=f"{seconds:.0f}")


def size_text(size):
    """1.5 KiB, 1 GiB: binary units, without a needless .0 (German: 1,5 KiB - "1.5" is no decimal there)."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            text = f"{size:.1f}".rstrip("0").rstrip(".")
            return f"{text.replace('.', ',') if language() == 'de' else text} {unit}"
        size /= 1024
    return str(size)
