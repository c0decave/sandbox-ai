"""Files below a root directory, for the file browser: listing, copying out to the host and in from it.
No Qt.

Everything below a root (a workspace, a sandbox HOME) may have been written by an agent, so nothing here
follows a symbolic link, not even one in the middle of a path:

- a path is walked one component at a time with openat(O_NOFOLLOW | O_DIRECTORY) relative to the directory
  before it; '..' and absolute paths are refused before anything is opened;
- the directory reached is checked through /proc/self/fd to still lie below the root (a directory the
  agent moved away while it was walked is refused);
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
"""
from __future__ import annotations

import atexit
import contextlib
from dataclasses import dataclass, field
import errno
import fnmatch
import os
import secrets
import stat
import threading
import time

from .i18n import tr

LIMIT = 1 << 30  # bytes per copy
MAX_ENTRIES = 20_000  # files and folders per copy, entries per listing
MAX_DEPTH = 64  # nested folders per copy
CHUNK = 1 << 20
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NOCTTY = getattr(os, "O_NOCTTY", 0)
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | O_NOCTTY | O_CLOEXEC
CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | O_NOCTTY | O_CLOEXEC
# Files that tools execute or read as configuration when you use the copied folder on the host.
RISKY = (".git/config", ".git/hooks/*", "*/.git/config", "*/.git/hooks/*", ".envrc", "*/.envrc", ".direnv/*",
         ".bashrc", ".bash_profile", ".profile", ".zshrc", ".npmrc", ".pypirc", ".vscode/*", "*/.vscode/*",
         ".idea/*", ".pre-commit-config.yaml", ".gitattributes", ".gitmodules", "Makefile", "*.desktop")


class Refused(ValueError):
    """A path or copy the browser does not do (the message is in the app's language)."""


class Cancelled(Refused):
    pass


def printable(text):
    """Text for the screen: names are chosen by the agent, so control characters are shown escaped."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


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


class Budget:
    def __init__(self, limit, entries, cancelled=None):
        self.limit, self.entries, self.used, self.count = limit, entries, 0, 0
        self.cancelled = cancelled or (lambda: False)

    def check(self):
        if _closing or self.cancelled():
            raise Cancelled(tr("Cancelled."))

    def entry(self, count=1):
        self.check()
        self.count += count
        if self.count > self.entries:
            raise Refused(tr("More than {count} files and folders; copy a smaller part.", count=self.entries))

    def take(self, size):
        self.check()
        self.used += size
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
                    raise Refused(tr("More than {count} files and folders; copy a smaller part.", count=budget.entries))
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

    def __init__(self, path, label=None):
        # The root itself is chosen on the host (a workspace, a session's HOME): its own path is resolved
        # once; everything below it is walked without following links.
        self.path = os.path.realpath(os.fspath(path))
        self.label = label or self.path
        fd = os.open(self.path, DIRECTORY)
        os.close(fd)
        self.hook = None  # tests: called between the steps of a walk

    # -- walking --

    def _inside(self, fd):
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return  # no /proc: the walk with O_NOFOLLOW alone keeps to the root
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
                    found.append(Entry(entry.name, kind, info.st_size, info.st_mtime, target))
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
