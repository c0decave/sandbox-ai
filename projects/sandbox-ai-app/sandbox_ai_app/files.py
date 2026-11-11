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
host is Root("/", sandbox=False)), and Root.delete_entry, rename_entry, make_folder, chmod_entry and
chown_entry. A move copies first and removes the source only when the copy is complete, and then only the
files it copied unchanged (what the agent added or changed meanwhile stays, and is reported). Below a sandbox
root no set-user-ID, set-group-ID or sticky bit is ever set, and chown goes only to the user and their own
groups; files a rootless Podman sandbox gave a subordinate UID (/etc/subuid) are taken back with
take_ownership (podman unshare: inside Podman's user namespace, UID 0 is the user).

The file manager's editor reads a whole file with Root.read_file (a regular file only, at most EDIT_LIMIT bytes) and
writes it back with Root.write_file: a temporary file in the same folder renamed over the old one, never written
through a link or a hard link, refused (Changed) when the file is no longer the one that was read unless the human
said to replace it; its permission bits stay, set-ID and sticky bits do not. Root.measure counts a folder's size for
the Properties dialog (links counted, never followed; bounded like a delete).
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
import stat
import subprocess
import sys
import threading
import time

from .i18n import N_, tr

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
    temporary = _temporary_name()
    target = _create(temporary, target_dir, mode)
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


def _mode(info):
    return 0o777 if info.st_mode & 0o111 else 0o666  # the umask applies; set-id bits are never copied


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
            chunks, size = [], 0
            while size <= limit:
                chunk = os.read(fd, min(CHUNK, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        truncated = len(data) > limit
        return FileData(data[:limit], _identity(after), after.st_size, truncated, info.st_mode)

    def write_file(self, relative, data, expected=None):
        """Write DATA as the file RELATIVE: through a temporary file in its folder that is then renamed over it (never
        written through a link or a hard link the agent put there; a write cut short leaves the file as it was). Its
        permission bits stay (no set-user-ID, set-group-ID or sticky bit). EXPECTED (FileData.identity of what was
        read): if the file is no longer that (changed, replaced or removed meanwhile), Changed is raised and nothing
        is written. Returns the identity of the file written."""
        parent, name = self._parent(relative)
        try:
            try:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if now is not None and stat.S_ISLNK(now.st_mode):
                raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name)))
            if now is not None and not stat.S_ISREG(now.st_mode):
                raise Refused(tr("{name} is not a regular file.", name=printable(name)))
            if expected is not None and (now is None or _identity(now) != tuple(expected)):
                raise Changed(tr("{name} changed since it was opened.", name=printable(name)))
            temporary = _temporary_name()
            fd = _create(temporary, parent, 0o600 if now is not None else 0o666)  # (a new file: the umask applies)
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
                    if now is not None:
                        os.fchmod(fd, stat.S_IMODE(now.st_mode) & 0o777)
                finally:
                    os.close(fd)
                if expected is not None:  # (once more, right before: the agent may write meanwhile)
                    try:
                        again = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        again = None
                    if again is None or _identity(again) != tuple(expected):
                        raise Changed(tr("{name} changed since it was opened.", name=printable(name)))
                _place(temporary, name, parent, True, (written.st_dev, written.st_ino))
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                raise
            finally:
                _forget_partial(key)
            return _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        finally:
            os.close(parent)

    def measure(self, relative, entries=WALK_ENTRIES, cancelled=None):
        """The size of the file or folder RELATIVE ("" the root's folder) with all it holds: Result.bytes, .files and
        .directories. Links are counted, never followed; at most ENTRIES entries and MAX_DEPTH nested folders (then
        Refused, with what was counted so far on its .partial)."""
        budget, result = Budget(0, entries, cancelled, TOO_MANY_WALK), Result()
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
                    self._measure(parent, name, budget, result, 0)
                finally:
                    os.close(parent)
        except (OSError, ValueError) as exc:
            exc.partial = result
            raise
        return result

    def _measure(self, dir_fd, name, budget, result, depth):
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
                self._measure_folder(fd, budget, result, depth)
            finally:
                os.close(fd)
        else:
            result.files += 1
            result.bytes += info.st_size if stat.S_ISREG(info.st_mode) else 0

    def _measure_folder(self, fd, budget, result, depth):
        for child in _names(fd, budget):
            self._measure(fd, child, budget, result, depth + 1)

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
                        self._mkdir(name, target_dir)  # refuses an existing name: everything below is ours
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
    def _mkdir(name, dir_fd):
        try:
            os.mkdir(name, 0o777, dir_fd=dir_fd)
        except FileExistsError:
            raise Refused(tr("{name} already exists there.", name=printable(name))) from None

    @staticmethod
    def _copy_file_out(fd, info, target_dir, name, shown, budget, result):
        budget.entry()
        # Never executable on the host: the agent chose the contents (the umask still applies).
        _write_file(fd, target_dir, name, 0o666, False, budget)
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
                        self._mkdir(child, target)
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
            os.mkdir(name, 0o777, dir_fd=target_parent)
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
            self._count(parent, name, Budget(0, entries, cancelled, TOO_MANY_WALK), 0)
            try:
                self._delete(parent, name, Budget(0, entries, cancelled, TOO_MANY_WALK, progress), result, 0)
            except (OSError, ValueError) as exc:
                exc.partial = result
                raise
        finally:
            os.close(parent)
        return result

    def _count(self, dir_fd, name, budget, depth):
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
                for child in _names(fd, budget):
                    self._count(fd, child, budget, depth + 1)
            finally:
                os.close(fd)

    def _delete(self, dir_fd, name, budget, result, depth):
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
                for child in _names(fd):
                    self._delete(fd, child, budget, result, depth + 1)
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

    def chmod_entry(self, relative, mode, special=False):
        """Set the permission bits MODE (0..0o7777) of the file or folder RELATIVE; a link is refused (it has
        none of its own, and what it points to is not changed). Set-user-ID, set-group-ID and sticky bits only
        with SPECIAL and never below a sandbox root."""
        mode = int(mode)
        if not 0 <= mode <= 0o7777:
            raise Refused(tr("Not a permission mode: {mode}", mode=oct(mode)))
        if mode & SPECIAL_BITS and (self.sandbox or not special):
            raise Refused(tr("Set-user-ID, set-group-ID and sticky bits are not set here (never below a "
                             "sandbox's folder)."))
        parent, name = self._parent(relative)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise Refused(tr("{name} is a symbolic link; links are not followed.", name=printable(name)))
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise Refused(tr("{name} is not a regular file.", name=printable(name)))
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

    def _chown(self, dir_fd, name, shown, uid, gid, recursive, budget, result, depth):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        budget.entry()
        try:
            os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
            result.files += 1
        except OSError as exc:
            result.skipped.append((shown, exc.strerror or str(exc)))
        if recursive and stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                raise Refused(tr("Folders nested more than {count} deep are not changed.", count=MAX_DEPTH))
            fd = self._open_child_dir(dir_fd, name)
            try:
                for child in _names(fd, budget):
                    self._chown(fd, child, f"{shown}/{child}", uid, gid, True, budget, result, depth + 1)
            finally:
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


def _rename_noreplace(src_dir, name, dst_dir, new_name):
    """Rename without replacing an existing NEW_NAME: renameat2(RENAME_NOREPLACE) where libc and the file
    system have it, else checked first, then renamed."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError, ImportError):
        renameat2 = None
    if renameat2 is not None:
        if renameat2(src_dir, os.fsencode(name), dst_dir, os.fsencode(new_name), 1) == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise Refused(tr("{name} already exists there.", name=printable(new_name)))
        if error not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(error, os.strerror(error), name)
    _refuse_existing(new_name, dst_dir)
    os.rename(name, new_name, src_dir_fd=src_dir, dst_dir_fd=dst_dir)


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
    if stat.S_ISDIR(info.st_mode):
        fd = os.open(name, DIRECTORY, dir_fd=parent)
    else:
        fd, _ = _open_regular(name, parent)
    try:
        _same_file(fd, info, name)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _same_file(fd, info, name):
    now = os.fstat(fd)
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or stat.S_IFMT(now.st_mode) != stat.S_IFMT(info.st_mode):
        raise Refused(tr("{name} was replaced meanwhile; nothing was changed.", name=printable(name)))


def user_groups():
    """The IDs of the user's groups."""
    return sorted(set(os.getgroups()) | {os.getgid()})


def copy_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, name=None, limit=LIMIT,
               entries=MAX_ENTRIES, cancelled=None, progress=None):
    """Copy the file or folder RELATIVE of the Root SOURCE into the folder TARGET_DIR of the Root TARGET (as
    NAME, default its own). Both sides are walked relative to open directory descriptors without following a
    link; links inside a folder are skipped and listed. UNTRUSTED (default: SOURCE.sandbox): an agent may
    have written the source, so nothing copied is executable (listed in Result.executable) and files that
    tools run are named in Result.risky. An existing file is replaced only with OVERWRITE (renamed over, never
    written through); an existing folder is then filled. What the copy created is removed unless it completes."""
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
                                 False, frozenset())
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


def _copy_one(fd, info, target_dir, name, shown, overwrite, untrusted, budget, result):
    budget.entry()
    _write_file(fd, target_dir, name, 0o666 if untrusted else _mode(info), overwrite, budget)
    after = os.fstat(fd)
    if untrusted and info.st_mode & 0o111:
        result.executable.append(shown)
    result.files += 1
    result.bytes += info.st_size
    result.paths.append(shown)
    if _identity(after) == _identity(info):
        result.ids.add(_identity(after))
    else:
        result.changed.append(shown)


def _copy_folder(source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth,
                 fresh, avoid):
    """Copy the open folder SOURCE_FD as NAME into TARGET_PARENT (see Root._copy_tree_in: FRESH, and the outermost
    folder created is removed unless the copy completes). AVOID: (st_dev, st_ino) of the folders this copy
    writes into, never walked as a source (a folder copied into itself)."""
    _too_deep(depth)
    budget.entry()
    created = False
    try:
        os.mkdir(name, 0o777, dir_fd=target_parent)
        created = True
    except FileExistsError:
        if not overwrite:
            raise Refused(tr("{name} already exists there.", name=printable(name))) from None
    arguments = (source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth)
    if created and not fresh:
        with _undo_unless_complete(target_parent, name):
            _fill_folder(*arguments, True, avoid)
    else:
        _fill_folder(*arguments, fresh or created, avoid)


def _fill_folder(source, source_fd, target, target_parent, name, shown, overwrite, untrusted, budget, result, depth,
                 fresh, avoid):
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
                    _copy_folder(source, sub, target, target_fd, child, path, overwrite, untrusted, budget, result,
                                 depth + 1, fresh, avoid)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(info.st_mode):
                try:
                    fd, info = _open_regular(child, source_fd)
                except Refused as exc:
                    result.skipped.append((path, str(exc)))
                    continue
                try:
                    _copy_one(fd, info, target_fd, child, path, overwrite, untrusted, budget, result)
                finally:
                    os.close(fd)
            else:
                result.skipped.append((path, tr("not a regular file")))
    finally:
        os.close(target_fd)


def move_entry(source, relative, target, target_dir="", overwrite=False, untrusted=None, limit=LIMIT,
               entries=MAX_ENTRIES, cancelled=None, progress=None, target_untrusted=None):
    """Move the file or folder RELATIVE of SOURCE into TARGET_DIR of TARGET. Within one root: renamed (an
    existing name only replaced with OVERWRITE, and only a file by a file) - unless what an agent may have written
    (UNTRUSTED, default SOURCE.sandbox) goes where none writes (TARGET_UNTRUSTED False, default TARGET.sandbox: the
    host's Root("/") from a sandbox's folder to an ordinary one): that is copied as the agent's (never executable,
    the files tools run named) like a move between roots. Otherwise copied (copy_entry), and
    the source is removed only when the copy is complete - nothing skipped, nothing changed while it was
    copied, not cancelled - and then only the files copied unchanged and the folders walked (Result.left:
    what stayed because it appeared or changed meanwhile; Result.kept: why the source was kept)."""
    untrusted = source.sandbox if untrusted is None else untrusted
    target_untrusted = target.sandbox if target_untrusted is None else target_untrusted
    if source.path == target.path and source.sandbox == target.sandbox and (target_untrusted or not untrusted):
        moved = _rename_within(source, relative, target_dir, overwrite)
        if moved is not None:
            return moved
    result = copy_entry(source, relative, target, target_dir, overwrite, untrusted, limit=limit, entries=entries,
                        cancelled=cancelled, progress=progress)
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
    if stat.S_ISREG(info.st_mode) and _identity(info) in result.ids:
        os.unlink(name, dir_fd=dir_fd)
        return []
    return [shown]


def exists_in(root, relative_dir, names):
    """Those of NAMES that exist (as anything, a link too) in the folder RELATIVE_DIR of ROOT."""
    fd = root.open_dir(relative_dir)
    found = []
    try:
        for name in names:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                found.append(name)
            except (FileNotFoundError, ValueError):
                pass
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

def own(dir_fd, name, shown, depth):
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    count[0] += 1
    if count[0] > LIMIT:
        raise ValueError("more than %d files and folders" % LIMIT)
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
                process.kill()
                process.communicate()
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


def size_text(size):
    """1.5 KiB, 1 GiB: binary units, without a needless .0."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            text = f"{size:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        size /= 1024
    return str(size)
