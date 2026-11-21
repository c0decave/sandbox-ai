"""A stopped kept start's HOME packed into one compressed archive beside it, and unpacked again before it is used.

  sandbox-ai gc --pack [--older-than DAYS] [--home NAME]   (starts.py) packs; resume, `resume --review-home`,
  `resume --restore-home` and a start of the same name unpack first (sessions.py), each under the start's lock.

In the start directory (state/sandbox-ai/sessions/ID/):
  home.tar.zst (zstd when it is installed, else home.tar.xz: Python's lzma)   the HOME as a tar archive
  home.pack.json   {"archive", "format", "created", "bytes" (the HOME's size), "archive_bytes", "entries",
                    "digest" (of every entry's name, type, size, link and content, in order), "skipped"}
A start is packed when home.pack.json is there and its HOME is not.

Nothing is lost on a failure: pack writes the archive aside (.partial), reads it back whole and compares its digest
with the one taken while writing, renames it into place, moves the HOME aside, writes home.pack.json and only then
removes the HOME; unpack extracts into a folder aside, compares the digest, marks the record "unpacked", renames the
folder to home and only then removes the archive. Links are stored as links and never followed; a socket is skipped
(named in "skipped"). tidy() finishes or undoes what an interrupted pack or unpack left, and never removes an archive
whose HOME is not certainly whole: a HOME something else made beside it is left with it (said by state()).
Standard library only (zstd: its command), Python 3.9.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import time

META = "home.pack.json"
CHUNK = 1 << 20
ZSTD_LEVEL = 3
XZ_PRESET = 1  # fast: a big HOME packs in minutes, not hours
SPARE = 256 << 20  # free space kept beyond what an unpack writes


class PackError(Exception):
    """A pack or unpack that did not happen; what was there is as it was."""


def meta(directory):
    """The record of a packed HOME in the start directory DIRECTORY ({} when there is none or it is damaged)."""
    try:
        found = json.loads((Path(directory) / META).read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return found if isinstance(found, dict) and isinstance(found.get("archive"), str) and \
        found["archive"] in ("home.tar.zst", "home.tar.xz") else {}


def packed(directory):
    """The record (meta) when the HOME of DIRECTORY is packed - its archive there, no HOME -, else None."""
    directory = Path(directory)
    found = meta(directory)
    if found and not os.path.lexists(directory / "home") and (directory / found["archive"]).is_file():
        return found
    return None


def archives(directory):
    """The archives of a HOME in DIRECTORY (whole ones, by their names)."""
    return [Path(directory) / name for name in ("home.tar.zst", "home.tar.xz") if (Path(directory) / name).is_file()]


def tidy(directory):
    """Finish or undo what an interrupted pack or unpack left in the start directory DIRECTORY (its lock held):
    files and folders aside; an archive without a record (a pack that stopped before the HOME went: the HOME is
    put back if it was moved aside, the archive removed); a record marked unpacked beside its HOME (the archive
    removed); an empty HOME beside a packed one (made by something that did not unpack: removed, it stays packed).
    True if anything changed."""
    directory = Path(directory)
    changed = False
    home = directory / "home"
    found = meta(directory)
    for path in sorted(directory.glob(".home.*")):
        name = path.name
        if name.startswith(".home.removing-") and not found and not os.path.lexists(home):
            os.rename(path, home)  # a pack that stopped right after moving the HOME aside: put back
        elif name.startswith((".home.unpacking-", ".home.removing-", ".home.tar.", ".home.pack.json.")):
            remove_tree(path)
        else:
            continue
        changed = True
    if not found:
        for path in archives(directory):
            if home.is_dir() and not home.is_symlink():
                path.unlink()  # a pack that did not finish: the HOME is as it was
                changed = True
        return changed
    if home.is_dir() and not home.is_symlink():
        if found.get("unpacked"):  # an unpack that stopped before it removed the archive: the HOME is whole
            for path in archives(directory):
                path.unlink()
            (directory / META).unlink()
            return True
        with contextlib.suppress(OSError):
            if not any(os.scandir(home)):
                home.rmdir()  # made empty by something that did not unpack first: still packed
                return True
    return changed


def state(directory):
    """"packed", "unpacked" (a HOME, no archive), "both" (a HOME beside a packed one that is not certainly the
    same: both are kept, nothing is unpacked over it) or "none" (neither)."""
    directory = Path(directory)
    home = os.path.lexists(directory / "home")
    if packed(directory):
        return "packed"
    if home and meta(directory) and archives(directory):
        return "both"
    return "unpacked" if home else "none"


def remove_tree(path):
    """PATH removed (a folder: with what is in it, read-only folders made writable; links never followed)."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return

    def writable(function, failed, _):
        for target in (os.path.dirname(failed), failed):
            with contextlib.suppress(OSError):
                if not os.path.islink(target):
                    os.chmod(target, 0o700)
        function(failed)
    shutil.rmtree(path, onerror=writable)


def zstd():
    return shutil.which("zstd")


def entry_key(info):
    """What the digest takes of one tar entry: its name, type, size and link."""
    return f"{info.name}\0{info.type.decode('ascii', 'replace')}\0{info.size if info.isreg() else 0}\0" \
           f"{info.linkname if info.issym() or info.islnk() else ''}\0".encode("utf-8", "surrogateescape")


class Hashing:
    """A file read through: its SHA-256 taken on the way."""

    def __init__(self, stream):
        self.stream, self.hash = stream, hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


@contextlib.contextmanager
def writer(path, use_zstd):
    """A tar stream written to PATH, compressed with zstd (its command) or xz."""
    if use_zstd:
        with open(path, "wb") as target:
            process = subprocess.Popen([use_zstd, "-q", "-T0", f"-{ZSTD_LEVEL}", "-c"], stdin=subprocess.PIPE,
                                       stdout=target, stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=process.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    yield tar
            finally:
                with contextlib.suppress(OSError):
                    process.stdin.close()
                error = process.stderr.read().decode(errors="replace")
                status = process.wait()
            if status != 0:
                raise PackError(f"zstd failed ({status}): {error.strip()[-200:]}")
            target.flush()
            os.fsync(target.fileno())
    else:
        with tarfile.open(path, mode="w:xz", preset=XZ_PRESET, format=tarfile.PAX_FORMAT) as tar:
            yield tar
        with open(path, "rb") as target:
            os.fsync(target.fileno())


@contextlib.contextmanager
def reader(path, kind=None):
    """The tar stream of the archive PATH: KIND "zstd" or "xz" (None: by its name)."""
    if (kind or ("zstd" if str(path).endswith(".zst") else "xz")) == "zstd":
        found = zstd()
        if not found:
            raise PackError(f"{path} is a zstd archive and zstd is not installed: install zstd, then try again")
        process = subprocess.Popen([found, "-q", "-d", "-c", str(path)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
                yield tar
                while process.stdout.read(CHUNK):  # (to its end: zstd's own check of the whole frame)
                    pass
        finally:
            process.stdout.close()
            error = process.stderr.read().decode(errors="replace")
            status = process.wait()
        if status != 0:
            raise PackError(f"zstd could not read {path} ({status}): {error.strip()[-200:]}")
    else:
        with tarfile.open(path, mode="r|xz") as tar:
            yield tar


def entries(home):
    """Every file, folder and link below HOME (a folder before what it holds), never following a link or leaving
    its file system: (path, name in the archive) one after another."""
    home = Path(home)
    device = os.lstat(home).st_dev
    pending = [(str(home), "")]
    while pending:
        folder, prefix = pending.pop()
        with os.scandir(folder) as listing:
            items = sorted(((entry.path, entry.name, entry.stat(follow_symlinks=False)) for entry in listing),
                           key=lambda item: item[1])
        for path, name, info in items:
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != device:
                    raise PackError(f"{path} is another file system (a mount): not packed")
                pending.append((path, prefix + name + "/"))
            yield path, prefix + name


def pack(directory, size=None, now=None):
    """Pack the HOME of the start directory DIRECTORY (its start stopped, its lock held by the caller): the archive
    written aside, read back and its digest compared, then put in place and the HOME removed. SIZE: the HOME's size
    (recorded; unpack checks the free space for it). The record (meta). PackError: nothing changed."""
    directory = Path(directory)
    home = directory / "home"
    if home.is_symlink() or not home.is_dir():
        raise PackError(f"{home} is not a folder: nothing to pack")
    tidy(directory)
    if state(directory) == "both":
        raise PackError(f"{directory} has both a HOME and a packed one (home.pack.json) that may differ: not packed "
                        "again; look at both and remove the one not needed")
    found = zstd()
    archive = "home.tar.zst" if found else "home.tar.xz"
    partial = directory / f".home.tar.{os.getpid()}.partial"
    digest, count, skipped = hashlib.sha256(), 0, []
    try:
        with writer(partial, found) as tar:
            for path, name in entries(home):
                info = tar.gettarinfo(path, arcname=name)
                if info is None:  # a socket: nothing to keep
                    skipped.append(name)
                    continue
                if info.isreg():
                    with open(path, "rb") as stream:
                        hashing = Hashing(stream)
                        tar.addfile(info, hashing)
                    digest.update(entry_key(info) + hashing.hash.digest())
                else:
                    tar.addfile(info)
                    digest.update(entry_key(info))
                count += 1
        written = verify(partial, "zstd" if found else "xz")
        if written != (digest.hexdigest(), count):
            raise PackError("the archive read back is not what was written: the HOME stays as it was")
        record = {"archive": archive, "format": "zstd" if found else "xz",
                  "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "bytes": size,
                  "archive_bytes": partial.stat().st_size, "entries": count, "digest": digest.hexdigest(),
                  "skipped": skipped[:100]}
        os.replace(partial, directory / archive)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        if isinstance(exc, (OSError, tarfile.TarError, EOFError, ValueError)):
            raise PackError(f"not packed ({exc}); the HOME stays as it was") from None
        raise
    aside = directory / f".home.removing-{os.getpid()}"
    try:
        os.rename(home, aside)  # the HOME goes in one step; the record says it is packed; then what was aside goes
        write_meta(directory, record)
    except OSError as exc:
        if os.path.lexists(aside) and not os.path.lexists(home):
            os.rename(aside, home)
        with contextlib.suppress(OSError):
            (directory / archive).unlink()
        raise PackError(f"not packed ({exc}); the HOME stays as it was") from None
    remove_tree(aside)
    return record


def write_meta(directory, record):
    """home.pack.json as RECORD, replaced in one step."""
    partial = Path(directory) / f".{META}.{os.getpid()}"
    with open(partial, "w") as stream:
        stream.write(json.dumps(record, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, Path(directory) / META)


def verify(archive, kind=None):
    """(digest, entries) of the archive ARCHIVE (KIND: reader's) read whole: what pack compares with what it wrote."""
    digest, count = hashlib.sha256(), 0
    with reader(archive, kind) as tar:
        for info in tar:
            if info.isreg():
                stream = tar.extractfile(info)
                hashing = hashlib.sha256()
                left = info.size
                while left > 0:
                    data = stream.read(min(CHUNK, left))
                    if not data:
                        raise PackError(f"{archive}: {info.name} ends early")
                    hashing.update(data)
                    left -= len(data)
                digest.update(entry_key(info) + hashing.digest())
            else:
                digest.update(entry_key(info))
            count += 1
    return digest.hexdigest(), count


def safe_name(name):
    """NAME of an archive entry as a path below the HOME, or None for one that would leave it."""
    parts = name.split("/")
    if not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return None
    return name


def unpack(directory):
    """Unpack the packed HOME of the start directory DIRECTORY (the caller holds its start's lock): extracted aside,
    its digest compared with the record, renamed to home, then the archive removed. The record; None when it was not
    packed. PackError: the archive stays, no HOME is made."""
    directory = Path(directory)
    record = packed(directory)
    if record is None:
        tidy(directory)
        return None
    archive = directory / record["archive"]
    need = (record.get("bytes") or 0) + SPARE
    free = shutil.disk_usage(directory).free
    if isinstance(record.get("bytes"), int) and free < need:
        raise PackError(f"not unpacked: it needs about {need >> 20} MiB free beside it, {free >> 20} MiB are; free "
                        "some space and try again (the archive stays)")
    for path in directory.glob(".home.unpacking-*"):
        remove_tree(path)
    target = directory / f".home.unpacking-{os.getpid()}"
    target.mkdir(mode=0o700)
    digest, count, folders = hashlib.sha256(), 0, []
    try:
        with reader(archive) as tar:
            for info in tar:
                name = safe_name(info.name)
                if name is None:
                    raise PackError(f"{archive}: the entry {info.name!r} would leave the HOME")
                path = target / name
                parent = path.parent
                if parent != target and (parent.is_symlink() or not parent.is_dir()):
                    raise PackError(f"{archive}: the entry {info.name!r} lies below a link or nothing")
                mode = info.mode & 0o1777  # never setuid or setgid
                if info.isdir():
                    path.mkdir(mode=0o700)
                    folders.append((path, mode, info.mtime))
                    digest.update(entry_key(info))
                elif info.issym():
                    os.symlink(info.linkname, path)
                    digest.update(entry_key(info))
                elif info.islnk():
                    linked = safe_name(info.linkname)
                    if linked is None or (target / linked).is_symlink():
                        raise PackError(f"{archive}: the hard link {info.name!r} would leave the HOME")
                    os.link(target / linked, path)
                    digest.update(entry_key(info))
                elif info.isreg():
                    stream, hashing, left = tar.extractfile(info), hashlib.sha256(), info.size
                    with open(path, "xb") as out:
                        while left > 0:
                            data = stream.read(min(CHUNK, left))
                            if not data:
                                raise PackError(f"{archive}: {info.name} ends early")
                            hashing.update(data)
                            out.write(data)
                            left -= len(data)
                    os.chmod(path, mode)
                    os.utime(path, (info.mtime, info.mtime))
                    digest.update(entry_key(info) + hashing.digest())
                elif info.isfifo():
                    os.mkfifo(path, mode)
                    digest.update(entry_key(info))
                else:  # devices: never in a HOME a start of yours made
                    raise PackError(f"{archive}: {info.name!r} is a device")
                count += 1
        for path, mode, when in reversed(folders):
            os.utime(path, (when, when))
            os.chmod(path, mode)
        if (digest.hexdigest(), count) != (record.get("digest"), record.get("entries")):
            raise PackError(f"{archive} is not what was packed (its digest differs): it stays, nothing was unpacked")
        os.chmod(target, 0o700)
        write_meta(directory, {**record, "unpacked": True})  # from here on the HOME below is the whole one
        os.rename(target, directory / "home")
    except BaseException as exc:
        if not os.path.lexists(directory / "home"):
            with contextlib.suppress(OSError):
                write_meta(directory, record)
        remove_tree(target)
        if isinstance(exc, (OSError, tarfile.TarError, EOFError, ValueError)):
            raise PackError(f"not unpacked ({exc}); the archive stays") from None
        raise
    archive.unlink()
    (directory / META).unlink()
    return record
