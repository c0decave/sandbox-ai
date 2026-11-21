#!/usr/bin/env python3
"""sandbox-ai checkpoint: the workspaces of a start as they were, and everything an agent did undone.

  sandbox-ai checkpoint RUN [--label TEXT] [--json]
                                      a checkpoint of RUN's workspaces now (also: checkpoint create RUN)
  sandbox-ai checkpoint list [RUN] [--project DIR] [--json]
                                      the checkpoints, newest first (of RUN, of the project DIR, or all)
  sandbox-ai checkpoint show ID [--files] [--json]
                                      one checkpoint: when, why, its folders, sizes (--files: every path)
  sandbox-ai checkpoint diff ID [ID2] [--path P]... [--json]
                                      what changed since ID (the folders now, or the checkpoint ID2)
  sandbox-ai checkpoint restore RUN|ID [--path P]... [--dry-run] [--yes] [--json]
                                      put the folders (or the paths P) back as they were: RUN its last start's
                                      checkpoint, ID that one; what changes is shown and asked first, and a checkpoint
                                      of the state now is taken before (a restore can be undone)
  sandbox-ai checkpoint delete ID... [--yes]
                                      remove checkpoints (what only they hold goes too)

When a start begins, its writable workspaces (and with --checkpoint-mounts or "checkpoint_mounts": true its rw
--mount folders) are checkpointed - on the host, by the launcher, before the sandbox gets them: the checkpoint is
taken while the start prepares everything else and finished before the agent runs (an agent never writes into a
folder whose checkpoint is not complete). --checkpoint-every MINUTES (or "checkpoint_every") adds one every so many
minutes while it runs, when something changed; `sandbox-ai checkpoint RUN` one whenever you like. --no-checkpoint
(or "checkpoint_start": false) takes none at the start.

The store: one per project (its main workspace) in $XDG_STATE_HOME/sandbox-ai/checkpoints/NAME-HASH/ - never
mounted into a sandbox, so an agent can neither read, change nor delete a checkpoint. Content-addressed: every file's
content once (objects/AB/CDEF..., its SHA-256), however many checkpoints and folders hold it; a checkpoint is a
manifest (manifests/ID.json.gz: every path with its type, mode, mtime and content, a symbolic link's target) and a
small record (meta/ID.json). A file unchanged since the last checkpoint (the same size, mtime, ctime and inode) is not
read again; a changed one is read once - copied with its hash taken on the way, or cloned (FICLONE reflinks on btrfs
and XFS, clonefile on APFS) where the store's file system can. Never a hard link into a workspace: an agent writing
into the file would change the checkpoint.

What a checkpoint holds: files, folders (empty ones too), symbolic links (as links: never followed, also not on the
way to a file - every folder is opened relative to its parent without following a link), the permission bits and
modification times, and the project's own .git (a destructive git command is undone like any other change). Not:
owners, extended attributes, hard links between files (restored as separate files), special files (sockets, FIFOs,
devices: skipped and named), folders of another file system (a mount inside: skipped and named), and the names of
the exclude list (default: node_modules, .venv, target and build caches; "checkpoint_exclude" in your defaults) -
those are neither checkpointed nor touched by a restore.

A restore writes only inside the folder it restores: paths in a manifest are checked (relative, no '..'), every
folder on the way is opened without following a link (a link where a folder was is replaced by the folder), and a
file is written beside its place and renamed onto it. What stays as it is: what the checkpoint being restored did not
hold (its excluded and skipped paths), a mount inside and sandbox-ai's own state. Not what is only now something a
checkpoint cannot hold: a FIFO or socket where a file or folder was is removed and the file or folder put back, a
folder whose owner may no longer read or write it (chmod 000) is restored as its owner's and gets the checkpoint's
permissions back - an agent takes no path out of a restore that way.

Kept per project: "checkpoint_days" (default 30; 0: no age limit), "checkpoint_count" (default 50 checkpoints;
periodic ones go first), "checkpoint_gb" (default 10 GiB of content; 0: no limit) - applied after every new
checkpoint and by sandbox-ai gc. The newest checkpoint of a project always stays. A checkpoint never fills the disk:
it stops while RESERVE stays free (said; what it wrote is removed at once; a folder's first one that cannot fit is
said before anything is copied: room_for), and one ended mid-way (Ctrl-C, a failed start) leaves nothing behind once
the next prune comes (the mark taking.json). sandbox-ai gc also forgets a start's record of its folders
(runs/KEY.json) a month after its last checkpoint went.

Standard library only (Python 3.9).
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import datetime
import errno
import fnmatch
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
SCHEMA = "sandbox-ai-checkpoint"
VERSION = 1
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
HASH = re.compile(r"[0-9a-f]{64}")
RUN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KINDS = ("start", "manual", "periodic", "restore")
# Build output and downloaded dependencies: neither checkpointed nor touched by a restore (checkpoint_exclude).
DEFAULT_EXCLUDE = ("node_modules", ".venv", "target", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                   ".tox", ".gradle", ".next", ".parcel-cache", ".turbo")
# Your defaults (~/.config/sandbox-ai/defaults.json): key -> sandbox-ai's own default.
SETTINGS = {"checkpoint_start": True, "checkpoint_every": 0, "checkpoint_mounts": False, "checkpoint_days": 30,
            "checkpoint_count": 50, "checkpoint_gb": 10, "checkpoint_exclude": list(DEFAULT_EXCLUDE)}
EVERY_LIMIT = 1440          # minutes between two periodic checkpoints at most
SMALL = 1 << 20             # files up to this size are read whole (one read: hashed, written only when new)
CHUNK = 1 << 20
DEPTH_LIMIT = 256           # folders deeper than this are not looked into (named as skipped)
WORKERS = 8                 # large files read (a checkpoint) or written (a restore) at once: the disk's queue
LARGE = 4 << 20             # files from this size on go to the pool; smaller ones are done in place (a thread per
                            # small file costs more in hand-offs than its read)
IN_FLIGHT = 64              # files opened for the readers at most (descriptors held)
LOCK_TIMEOUT = 1800         # seconds a checkpoint, restore or prune waits for another one of the same project
LISTED = 2000               # paths per kind of change in a JSON answer (the counts are complete)
SHOWN = 20                  # paths per kind of change in the text
FICLONE = 0x40049409        # Linux: ioctl(dest, FICLONE, src) - a reflink copy (btrfs, XFS)
TEMP_PREFIX = ".sandbox-ai-restore-"
SPACE_STEP = 64 << 20       # bytes read into the store between two looks at its disk's free space
RESERVE = (1 << 30, 100, 4 << 30)  # a checkpoint leaves free: a 100th of the disk, at least 1 GiB, at most 4 GiB
_modules = {}
_LOADING = threading.RLock()


def module(name):
    """sandbox-ai's NAME.py (starts, filelock), loaded once by path."""
    with _LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_checkpoints", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def human(size):
    size = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def iso(when):
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- settings --------------------------------------------------------------------------------------------------------

def check_setting(key, value, origin):
    """VALUE of your defaults' KEY (one of SETTINGS), checked; ValueError (ORIGIN: where it was read) when wrong."""
    def whole(low, high, what):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{origin}: {key} is {what}")
    if key in ("checkpoint_start", "checkpoint_mounts"):
        if not isinstance(value, bool):
            raise ValueError(f"{origin}: {key} is true or false")
    elif key == "checkpoint_every":
        whole(0, EVERY_LIMIT, f"the minutes between two checkpoints of a running start, 1 to {EVERY_LIMIT} (0: none)")
    elif key == "checkpoint_days":
        whole(0, 36500, "the days a checkpoint is kept (0: no age limit)")
    elif key == "checkpoint_count":
        whole(0, 100000, "the checkpoints kept per project (0: no limit)")
    elif key == "checkpoint_gb":
        whole(0, 1 << 20, "the GiB of content kept per project (0: no limit)")
    elif key == "checkpoint_exclude":
        check_exclude(value, f"{origin}: {key}")
    else:
        raise ValueError(f"{origin}: {key} is no checkpoint setting")
    return value


def check_exclude(value, origin):
    """The exclude list VALUE: names (any folder or file of that name, fnmatch patterns such as *.o) or paths
    relative to a workspace (with a '/': build/cache); at most 200 of them."""
    if not isinstance(value, list) or len(value) > 200 or not all(
            isinstance(item, str) and 0 < len(item) <= 200 and "\0" not in item and item.strip("/") and
            ".." not in item.split("/") for item in value):
        raise ValueError(f"{origin} is a list of names or relative paths (at most 200), such as "
                         "[\"node_modules\", \".venv\", \"build/cache\"]")
    return value


def defaults_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"


def settings(document=None):
    """{key: value} of SETTINGS from your defaults file (DOCUMENT: as read already); a key that is missing or wrong
    has sandbox-ai's own value (the launcher refuses a wrong one at every start)."""
    if document is None:
        try:
            document = json.loads(defaults_path().read_text())
        except (OSError, ValueError, RecursionError):
            document = {}
    found = {}
    for key, default in SETTINGS.items():
        value = document.get(key, default) if isinstance(document, dict) else default
        try:
            found[key] = check_setting(key, value, "defaults")
        except ValueError:
            found[key] = default
    return found


# --- where the checkpoints are kept ----------------------------------------------------------------------------------

def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def store_root():
    """Every project's store: $XDG_STATE_HOME/sandbox-ai/checkpoints (never mounted into a sandbox)."""
    return state_root() / "checkpoints"


def project_key(project):
    """The store's folder name of PROJECT (its main workspace): NAME-HASH, as its log folder."""
    project = Path(project)
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", project.name or "root")[:60].lstrip(".") or "root"
    return f"{name}-{digest}"


def own_dir(path):
    """PATH made (0700, with its parents) and checked: a folder of this user, no symbolic link."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{path}: not a folder of yours (a link?) - remove it")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


class Store:
    """One project's checkpoints: meta/ID.json, manifests/ID.json.gz, objects/AB/REST (each content once, read-only),
    project.json (its main workspace), usage.json (the content's size), store.lock."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.objects = self.dir / "objects"
        self.base = str(self.objects)
        self.reflink = None  # unknown; then True or False (tried once per store)
        self.prefixes = set()  # objects/AB folders known to be there

    @classmethod
    def of(cls, project, create=True):
        directory = store_root() / project_key(project)
        if create:
            own_dir(store_root())
            own_dir(directory)
            for sub in ("meta", "manifests", "objects", "objects/tmp"):
                own_dir(directory / sub)
            record = directory / "project.json"
            if not record.exists():
                write_json(record, {"project": str(project)})
        return cls(directory)

    def project(self):
        try:
            found = json.loads((self.dir / "project.json").read_text()).get("project")
        except (OSError, ValueError, RecursionError, AttributeError):
            return None
        return found if isinstance(found, str) else None

    @contextlib.contextmanager
    def locked(self):
        with module("filelock").locked(self.dir / "store", timeout=LOCK_TIMEOUT):
            yield self

    def object_path(self, digest):
        return f"{self.base}/{digest[:2]}/{digest[2:]}"

    def has(self, digest):
        try:
            return stat.S_ISREG(os.lstat(self.object_path(digest)).st_mode)
        except OSError:
            return False

    def temp(self):
        """(fd, path) of a new file in objects/tmp, 0400 (written through this descriptor only)."""
        while True:
            path = f"{self.base}/tmp/{secrets.token_hex(8)}"
            try:
                return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400), path
            except FileExistsError:
                continue

    def keep(self, temp, digest):
        """The written TEMP as the object DIGEST (dropped when the store has it already). Its bytes, 0 if known."""
        target = self.object_path(digest)
        prefix = digest[:2]
        if prefix not in self.prefixes:
            own_dir(f"{self.base}/{prefix}")
            self.prefixes.add(prefix)
        size = os.lstat(temp).st_size
        try:
            os.link(temp, target)  # (within the store only) taken by one writer: another one's same content counts 0
        except FileExistsError:
            os.unlink(temp)
            return 0
        except OSError:  # a file system without hard links
            os.replace(temp, target)
            return size
        os.unlink(temp)
        return size

    def put(self, fd, size_hint):
        """The content of the open regular file FD into the store: (digest, bytes new to the store). Read once; a
        small file whole (written only when new), a large one cloned (reflink) where the file system can, else
        copied with its hash taken on the way."""
        if size_hint > SMALL and self.reflink is not False:
            done = self.put_clone(fd)
            if done is not None:
                return done
        buf = buffer()
        view = memoryview(buf)
        filled = 0
        while filled < len(buf):  # up to SMALL bytes into the buffer: a small file whole
            got = os.readv(fd, [view[filled:]])
            if not got:
                head = hashlib.sha256(view[:filled])
                digest = head.hexdigest()
                if self.has(digest):
                    return digest, 0
                out, temp = self.temp()
                try:
                    write_all(out, view[:filled])
                finally:
                    os.close(out)
                return digest, self.keep(temp, digest)
            filled += got
        head = hashlib.sha256(view)
        out, temp = self.temp()
        try:
            try:
                write_all(out, view)
                while True:
                    got = os.readv(fd, [buf])
                    if not got:
                        break
                    head.update(view[:got])
                    write_all(out, view[:got])
            finally:
                os.close(out)
            digest = head.hexdigest()
            return digest, self.keep(temp, digest)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def put_clone(self, fd):
        """FD cloned into the store (FICLONE, clonefile), its hash taken from the clone: (digest, bytes) or None when
        this file system cannot (then never tried again in this store)."""
        out, temp = self.temp()
        try:
            try:
                clone(fd, out, temp)
            except OSError:
                self.reflink = False
                os.close(out)
                out = None
                os.unlink(temp)
                return None
            self.reflink = True
            head = hashlib.sha256()
            with open(temp, "rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    head.update(chunk)
            digest = head.hexdigest()
            os.close(out)
            out = None
            return digest, self.keep(temp, digest)
        except BaseException:
            if out is not None:
                os.close(out)
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def open_object(self, digest):
        return os.open(self.object_path(digest), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)

    # The checkpoints of this store.
    def meta_path(self, ident):
        return self.dir / "meta" / f"{ident}.json"

    def manifest_path(self, ident):
        return self.dir / "manifests" / f"{ident}.json.gz"

    def metas(self):
        """Every checkpoint's record, oldest first."""
        found = []
        try:
            names = sorted(os.listdir(self.dir / "meta"))
        except OSError:
            return found
        for name in names:
            if not name.endswith(".json") or not ID.fullmatch(name[:-5]):
                continue
            meta = read_meta(self.dir / "meta" / name)
            if meta is not None:
                meta["store"] = self.dir.name
                found.append(meta)
        return sorted(found, key=lambda m: (m.get("created_ts") or 0, m["id"]))

    def manifest(self, ident):
        return read_manifest(self.manifest_path(ident), ident)

    def usage(self):
        try:
            value = json.loads((self.dir / "usage.json").read_text()).get("bytes")
        except (OSError, ValueError, RecursionError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def set_usage(self, value):
        write_json(self.dir / "usage.json", {"bytes": int(value), "at": iso(time.time())})

    def taking(self):
        """The mark of a checkpoint being written (taking.json): one that is there while the store is not locked was
        ended mid-way - its content (and objects/tmp) is swept by the next prune."""
        return self.dir / "taking.json"


class NoSpace(ValueError):
    """A checkpoint stopped before the disk of the store ran full."""


def disk_space(path):
    """(bytes free for you, bytes in all) of the file system of PATH."""
    found = os.statvfs(path)
    return found.f_bavail * found.f_frsize, found.f_blocks * found.f_frsize


def sweep(store):
    """What no checkpoint of STORE holds removed (the content of one that failed or was ended mid-way, objects/tmp),
    its usage counted anew and the mark of a checkpoint under way gone. The store is locked."""
    refs, sizes, _holds = object_references(store, store.metas())
    remove_objects(store, {h for h, n in refs.items() if n > 0})
    with contextlib.suppress(FileNotFoundError):
        store.taking().unlink()
    store.set_usage(sum(sizes[h] for h, n in refs.items() if n > 0))


def reserve_of(total):
    """The bytes a checkpoint leaves free on a disk of TOTAL bytes (RESERVE)."""
    return min(max(RESERVE[0], total // RESERVE[1]), RESERVE[2])


def room_for(store, root, exclude, stop=None):
    """Before the first checkpoint of the folder ROOT ({"host", "sandbox"}), which copies every file: NoSpace at once
    when its files hold more than the store's disk can take with its reserve kept - not after copying most of it (at
    every start again). Where the store's file system clones (tried with one large file: reflink), only the small
    files need room."""
    try:
        free, total = disk_space(store.base)
    except OSError:
        return
    room = free - reserve_of(total)
    if store.reflink:
        return
    survey = Walk(root["host"], exclude, None, stop=stop).run()
    files = [entry for entry in survey.entries if entry[0] == "f"]
    needed = sum(entry[4] for entry in files)
    if needed <= room:
        return
    large = next((entry for entry in files if entry[4] > SMALL), None)
    if store.reflink is None and large is not None:
        with contextlib.suppress(OSError):
            parent = open_parent(root["host"], large[1], survey.kind)
            try:
                opened = os.open(large[1].rsplit("/", 1)[-1] if large[1] else os.path.basename(root["host"]),
                                 FILE_FLAGS, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                if stat.S_ISREG(os.fstat(opened).st_mode):
                    store.put_clone(opened)  # (kept: the checkpoint holds it; sets store.reflink)
            finally:
                os.close(opened)
    if store.reflink:
        needed = sum(entry[4] for entry in files if entry[4] <= SMALL)
        if needed <= room:
            return
    raise NoSpace(f"{printable(root.get('sandbox') or root['host'])} holds {human(needed)}, only {human(max(free, 0))} "
                  f"free on the disk of the checkpoints (it leaves {human(reserve_of(total))}): no checkpoint; make "
                  f"room, leave large folders out (checkpoint_exclude) or use --no-checkpoint ({store.dir})")


_buffers = threading.local()


def buffer():
    """This thread's read buffer (SMALL bytes): read into, never allocated per read."""
    found = getattr(_buffers, "buffer", None)
    if found is None:
        found = _buffers.buffer = bytearray(SMALL)
    return found


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def clone(src_fd, out_fd, out_path):
    """OUT a clone of SRC (copy on write): FICLONE on Linux, fclonefileat on macOS; OSError where it cannot."""
    if sys.platform.startswith("linux"):
        import fcntl
        fcntl.ioctl(out_fd, FICLONE, src_fd)
        return
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        os.unlink(out_path)  # fclonefileat makes the file itself
        if libc.fclonefileat(src_fd, -2, os.fsencode(str(out_path)), 0) != 0:  # -2: AT_FDCWD on macOS
            number = ctypes.get_errno()
            with contextlib.suppress(OSError):  # put the placeholder back for the caller's cleanup
                os.close(os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400))
            raise OSError(number, os.strerror(number))
        return
    raise OSError(errno.EOPNOTSUPP, "no clones here")


def write_json(path, document, compress=False):
    """DOCUMENT as PATH, atomically (0600)."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    data = json.dumps(document, separators=(",", ":")).encode()
    if compress:
        data = gzip.compress(data, compresslevel=3, mtime=0)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        write_all(fd, data)
    finally:
        os.close(fd)
    os.replace(temp, path)


def read_meta(path):
    try:
        meta = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(meta, dict) or meta.get("schema") != SCHEMA or not ID.fullmatch(str(meta.get("id"))) or \
            not isinstance(meta.get("roots"), list):
        return None
    return meta


def safe_relative(path):
    """PATH of a manifest (relative, '/'-separated) checked: no absolute path, no '..' or '.', no empty part, no NUL;
    '' is the root itself. ValueError otherwise."""
    if not isinstance(path, str) or "\0" in path or len(path) > 4096:
        raise ValueError("a path of the checkpoint is damaged")
    if path == "":
        return path
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"the checkpoint names a path outside its folder: {printable(path)[:200]}")
    return path


def read_manifest(path, ident=None):
    """The manifest at PATH, every entry checked (types, paths: safe_relative, digests). ValueError when damaged."""
    try:
        document = json.loads(gzip.decompress(Path(path).read_bytes()))
    except (OSError, ValueError, EOFError, RecursionError, gzip.BadGzipFile) as exc:
        raise ValueError(f"the checkpoint {ident or path} cannot be read ({exc})") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or not isinstance(document.get("roots"),
                                                                                             list):
        raise ValueError(f"the checkpoint {ident or path} is damaged")
    for root in document["roots"]:
        if not isinstance(root, dict) or not isinstance(root.get("host"), str) or \
                not isinstance(root.get("entries"), list):
            raise ValueError(f"the checkpoint {ident or path} is damaged")
        for entry in root["entries"]:
            if not isinstance(entry, list) or not entry or entry[0] not in ("d", "f", "l"):
                raise ValueError(f"the checkpoint {ident or path} is damaged")
            safe_relative(entry[1])
            if entry[0] == "f" and (len(entry) != 8 or not HASH.fullmatch(str(entry[5])) or not all(
                    isinstance(entry[i], int) for i in (2, 3, 4))):
                raise ValueError(f"the checkpoint {ident or path} is damaged")
            if entry[0] == "d" and (len(entry) != 4 or not isinstance(entry[2], int)):
                raise ValueError(f"the checkpoint {ident or path} is damaged")
            if entry[0] == "l" and (len(entry) != 4 or not isinstance(entry[2], str) or "\0" in entry[2]):
                raise ValueError(f"the checkpoint {ident or path} is damaged")
        root.setdefault("excluded", [])
        root.setdefault("skipped", [])
    return document


# --- walking a folder (never following a link) -----------------------------------------------------------------------

DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)


class Excluder:
    def __init__(self, patterns):
        self.names = [p for p in patterns if "/" not in p.strip("/")]
        self.paths = [p.strip("/") for p in patterns if "/" in p.strip("/")]

    def __call__(self, name, rel):
        return any(fnmatch.fnmatchcase(name, p) for p in self.names) or \
            any(fnmatch.fnmatchcase(rel, p) for p in self.paths)


def protected_dirs():
    """(device, inode) of sandbox-ai's own state folder (the stores among it): a workspace that holds it (your HOME)
    never has it in a checkpoint."""
    found = set()
    for path in (state_root(), Path.home() / ".local/state/sandbox-ai"):
        with contextlib.suppress(OSError):
            info = os.stat(path)
            found.add((info.st_dev, info.st_ino))
    return found


class Done:
    """A result at hand, as a future's."""

    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


class Walk:
    """One folder (or file) ROOT as it is now: entries (d, f, l), excluded and skipped paths. With STORE every file's
    content goes into it (unchanged ones by PREVIOUS: {rel: entry} of the last checkpoint, not read again); without,
    files have no digest (diffs hash them when they need to)."""

    def __init__(self, host, exclude=(), store=None, previous=None, protect=None, stop=None):
        self.host = str(host)
        self.excluded_by = Excluder(list(exclude))
        self.store, self.previous = store, previous or {}
        self.protect = protected_dirs() if protect is None else protect
        self.stop = stop  # a threading.Event: set, the walk ends (ValueError)
        self.entries, self.excluded, self.skipped, self.torn = [], [], [], []
        self.files = self.dirs = self.links = self.bytes = self.new_bytes = 0
        self.kind = None
        self.waiting, self.slots, self.pool = [], threading.BoundedSemaphore(IN_FLIGHT), None
        self.unchecked = SPACE_STEP  # bytes read since the store's free space was looked at (at the first file too)
        self.queued, self.queue_lock = 0, threading.Lock()  # bytes of the files the pool reads now (not written yet)

    def run(self):
        if self.store is None:
            return self.walk()
        with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="sandbox-ai-checkpoint-read") as pool:
            self.pool = pool
            try:
                self.walk()
            finally:
                self.collect()
        return self

    def walk(self):
        path = Path(self.host)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            self.kind = "missing"
            return self
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{printable(path)} is a symbolic link now: not checkpointed (give the folder itself)")
        if stat.S_ISREG(info.st_mode):  # a workspace that is a file
            self.kind = "file"
            parent = os.open(path.parent, DIR_FLAGS)
            try:
                self.file(parent, path.name, "", info)
            finally:
                os.close(parent)
            return self
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{printable(path)} is neither a folder nor a file")
        self.kind = "dir"
        fd = os.open(path, DIR_FLAGS)
        try:
            info = os.fstat(fd)
            self.device = info.st_dev
            self.entries.append(["d", "", stat.S_IMODE(info.st_mode), info.st_mtime_ns])
            self.dirs += 1
            self.folder(fd, "", 0)
        finally:
            os.close(fd)
        return self

    def folder(self, fd, rel, depth):
        if self.stop is not None and self.stop.is_set():
            raise ValueError("stopped")
        try:
            with os.scandir(fd) as found:
                names = sorted(entry.name for entry in found)
        except OSError as exc:
            self.skipped.append([rel, f"cannot be read ({exc.strerror})"])
            return
        for name in names:
            child = f"{rel}/{name}" if rel else name
            if self.excluded_by(name, child):
                self.excluded.append(child)
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # gone meanwhile
            except OSError as exc:
                self.skipped.append([child, f"cannot be read ({exc.strerror})"])
                continue
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                try:
                    target = os.readlink(name, dir_fd=fd)
                except OSError:
                    continue
                self.entries.append(["l", child, target, info.st_mtime_ns])
                self.links += 1
            elif stat.S_ISDIR(mode):
                if info.st_dev != self.device:
                    self.skipped.append([child, "another file system (a mount inside the folder)"])
                elif (info.st_dev, info.st_ino) in self.protect:
                    self.skipped.append([child, "sandbox-ai's own state (the checkpoints among it)"])
                elif depth >= DEPTH_LIMIT:
                    self.skipped.append([child, f"deeper than {DEPTH_LIMIT} folders"])
                else:
                    try:
                        sub = os.open(name, DIR_FLAGS, dir_fd=fd)
                    except OSError as exc:
                        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                            self.torn.append(child)  # replaced while taken (by a link, say): not followed
                        else:
                            self.skipped.append([child, f"cannot be read ({exc.strerror})"])
                        continue
                    try:
                        opened = os.fstat(sub)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            self.torn.append(child)
                            continue
                        self.entries.append(["d", child, stat.S_IMODE(mode), info.st_mtime_ns])
                        self.dirs += 1
                        self.folder(sub, child, depth + 1)
                    finally:
                        os.close(sub)
            elif stat.S_ISREG(mode):
                self.file(fd, name, child, info)
            else:
                kind = ("a socket" if stat.S_ISSOCK(mode) else "a FIFO" if stat.S_ISFIFO(mode) else
                        "a device" if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) else "a special file")
                self.skipped.append([child, f"{kind}: not checkpointed"])

    def file(self, fd, name, rel, info):
        self.files += 1
        self.bytes += info.st_size
        if self.store is None:
            self.entries.append(["f", rel, stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size, None,
                                 info.st_ino, info.st_ctime_ns])
            return
        earlier = self.previous.get(rel)
        if earlier is not None and earlier[0] == "f" and earlier[4] == info.st_size and \
                earlier[3] == info.st_mtime_ns and earlier[6] == info.st_ino and earlier[7] == info.st_ctime_ns and \
                earlier[7] and self.store.has(earlier[5]):
            self.entries.append(["f", rel, stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size, earlier[5],
                                 info.st_ino, info.st_ctime_ns])
            return
        self.room(info.st_size)
        try:
            opened = os.open(name, FILE_FLAGS, dir_fd=fd)  # here: relative to its folder, never through a link
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ELOOP):
                self.torn.append(rel)  # gone or replaced by a link meanwhile: not followed
            else:
                self.skipped.append([rel, f"cannot be read ({exc.strerror})"])
            self.files -= 1
            self.bytes -= info.st_size
            return
        if info.st_size < LARGE:
            done = self.read(opened, info, pooled=False)
            self.entries.append(None)
            self.waiting.append((len(self.entries) - 1, rel, info, Done(done)))
            return
        # Read by the pool (the disk serves several reads at once); its entry filled in by collect().
        self.entries.append(None)
        self.slots.acquire()
        with self.queue_lock:
            self.queued += info.st_size
        try:
            future = self.pool.submit(self.read, opened, info)
        except BaseException:
            with self.queue_lock:
                self.queued -= info.st_size
            self.slots.release()
            os.close(opened)
            raise
        self.waiting.append((len(self.entries) - 1, rel, info, future))

    def room(self, size):
        """Before a file of SIZE is read into the store: NoSpace when the store's disk would keep less than its reserve
        free (looked at every SPACE_STEP bytes; a file cloned costs no room)."""
        if size > SMALL and self.store.reflink:
            return
        self.unchecked += size
        if self.unchecked < SPACE_STEP:
            return
        self.unchecked = 0
        try:
            free, total = disk_space(self.store.base)
        except OSError:
            return
        reserve = reserve_of(total)
        with self.queue_lock:
            queued = self.queued
        if free - queued - size < reserve:
            raise NoSpace(f"only {human(max(free, 0))} free on the disk of the checkpoints: stopped before it was full "
                          f"(it leaves {human(reserve)}); make room, leave large folders out (checkpoint_exclude) or "
                          f"use --no-checkpoint ({self.store.dir})")

    def read(self, opened, info, pooled=True):
        """The open file's content into the store (in the pool, or in place: POOLED False). (before, after, digest,
        new bytes) or None when it is no longer the file that was listed."""
        try:
            before = os.fstat(opened)
            if not stat.S_ISREG(before.st_mode) or before.st_ino != info.st_ino:
                return None
            digest, new = self.store.put(opened, before.st_size)
            return before, os.fstat(opened), digest, new
        finally:
            os.close(opened)
            if pooled:
                with self.queue_lock:
                    self.queued -= info.st_size
                self.slots.release()

    def collect(self):
        """The pool's reads into the entries (in their places), in order."""
        failed = None
        for index, rel, info, future in self.waiting:
            try:
                done = future.result()
            except BaseException as exc:  # noqa: BLE001 - every read is waited for, the first failure raised
                failed = failed or exc
                continue
            if done is None:
                self.torn.append(rel)
                self.files -= 1
                self.bytes -= info.st_size
                continue
            before, after, digest, new = done
            self.new_bytes += new
            changed = (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != \
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            if changed:
                self.torn.append(rel)  # written while read: kept as read, read again next time (ctime 0)
            self.entries[index] = ["f", rel, stat.S_IMODE(before.st_mode), before.st_mtime_ns, before.st_size,
                                   digest, before.st_ino, 0 if changed else before.st_ctime_ns]
        self.waiting = []
        self.entries = [entry for entry in self.entries if entry is not None]
        if failed is not None:
            raise failed

    def document(self, root):
        return {**root, "type": self.kind, "entries": self.entries, "excluded": self.excluded,
                "skipped": self.skipped, "torn": self.torn}

    def summary(self, root):
        return {**root, "type": self.kind, "files": self.files, "dirs": self.dirs, "links": self.links,
                "bytes": self.bytes, "new_bytes": self.new_bytes, "excluded": len(self.excluded),
                "skipped": self.skipped[:LISTED], "torn": len(self.torn)}


# --- taking a checkpoint ---------------------------------------------------------------------------------------------

def new_id(now=None):
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time())) + "-" + secrets.token_hex(3)


def checked_roots(roots):
    """ROOTS: [{"host": absolute path, "sandbox": its path in the sandbox, "kind": workspace|mount}], deduplicated."""
    found, seen = [], set()
    for root in roots:
        host = str(root.get("host") or "")
        if not os.path.isabs(host) or "\0" in host:
            raise ValueError(f"not an absolute folder: {printable(host)[:200]}")
        host = os.path.normpath(host)
        if host in seen:
            continue
        seen.add(host)
        found.append({"host": host, "sandbox": str(root.get("sandbox") or host),
                      "kind": root.get("kind") if root.get("kind") in ("workspace", "mount") else "workspace"})
    if not found:
        raise ValueError("no folder to checkpoint")
    return found


def previous_entries(store, host):
    """{rel: entry} of the files of folder HOST in the store's newest checkpoint that has it (unchanged files are not
    read again); {} when there is none or it cannot be read."""
    for meta in reversed(store.metas()):
        if any(root.get("host") == host for root in meta["roots"]):
            try:
                document = store.manifest(meta["id"])
            except ValueError:
                return {}
            for root in document["roots"]:
                if root["host"] == host:
                    return {entry[1]: entry for entry in root["entries"] if entry[0] == "f"}
    return {}


def live_runs():
    """The runs of this user whose launcher runs now (their run.json, read only: nothing is cleaned up here) - their
    start checkpoints stay whatever the limits say. [] when that cannot be told."""
    found = []
    try:
        net = module("net")
        for directory in net.run_dirs():
            with contextlib.suppress(OSError):
                for entry in os.scandir(directory):
                    with contextlib.suppress(OSError, ValueError, RecursionError):
                        record = json.loads(Path(entry.path, "run.json").read_text())
                        if isinstance(record, dict) and net.alive(record.get("pid"), record.get("pid_start")):
                            found.append(str(record.get("run") or entry.name))
    except Exception:  # noqa: BLE001 - not known: the run that takes the checkpoint is protected all the same
        return found
    return found


def take(project, roots, kind="manual", label=None, run=None, start=None, name=None, agent=None, exclude=None,
         stop=None, prune_after=True):
    """A checkpoint of ROOTS (checked_roots) in PROJECT's store: its record (meta) once written."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    roots = checked_roots(roots)
    exclude = list(settings()["checkpoint_exclude"] if exclude is None else exclude)
    project = str(project)
    store = Store.of(project)
    begun, now = time.monotonic(), time.time()
    with store.locked():
        interrupted = store.taking().exists()  # an earlier one ended mid-way: its mark stays for the prune
        # Marked until its record is written: a checkpoint ended mid-way (Ctrl-C, a failed start, the machine off)
        # leaves content no checkpoint holds, which the next prune removes (prune: the mark).
        write_json(store.taking(), {"at": iso(now), "pid": os.getpid()})
        documents, summaries = [], []
        try:
            for root in roots:
                previous = previous_entries(store, root["host"])
                if not previous:
                    room_for(store, root, exclude, stop)
                walk = Walk(root["host"], exclude, store, previous, stop=stop).run()
                documents.append(walk.document(root))
                summaries.append(walk.summary(root))
        except (OSError, NoSpace):
            # The disk full (or nearly), an I/O error: what it wrote goes now - the next start does not find the
            # disk full again. (Ended otherwise - stopped, Ctrl-C -: the mark has the next prune sweep.)
            with contextlib.suppress(OSError, ValueError):
                sweep(store)
            raise
        ident = new_id(now)
        while store.meta_path(ident).exists():
            ident = new_id(now)
        meta = {"schema": SCHEMA, "version": VERSION, "id": ident, "created": iso(now), "created_ts": now,
                "kind": kind, "label": (label or "")[:500] or None, "project": project, "run": run, "start": start,
                "name": name, "agent": agent, "exclude": exclude, "roots": summaries,
                "files": sum(s["files"] for s in summaries), "bytes": sum(s["bytes"] for s in summaries),
                "new_bytes": sum(s["new_bytes"] for s in summaries),
                "seconds": round(time.monotonic() - begun, 3), "reflink": bool(store.reflink)}
        write_json(store.manifest_path(ident), {"schema": SCHEMA, "version": VERSION, "id": ident,
                                                "roots": documents}, compress=True)
        write_json(store.meta_path(ident), meta)
        usage = store.usage()
        if usage is not None and not interrupted:
            store.set_usage(usage + meta["new_bytes"])
        if not interrupted:
            with contextlib.suppress(FileNotFoundError):
                store.taking().unlink()
        if prune_after:
            with contextlib.suppress(OSError, ValueError):
                meta["pruned"] = prune(store, keep={ident}, live=[*([run] if run else []), *live_runs()])["removed"]
    meta["store"] = store.dir.name
    return meta


class Pending:
    """A checkpoint taken in the background (the start's: while the start prepares the rest); finish() waits for it
    and returns its record (or raises what failed)."""

    def __init__(self, project, roots, **kwargs):
        self.result, self.error, self.ran = None, None, False
        self.begun = time.monotonic()
        self.stop = threading.Event()
        self.args = (project, roots, {**kwargs, "stop": self.stop})
        self.thread = threading.Thread(target=self.checkpoint, args=(project, roots), kwargs={**kwargs, "stop": self.stop},
                                       name="sandbox-ai-checkpoint", daemon=True)
        self.thread.start()

    def checkpoint(self, project, roots, **kwargs):
        self.ran = True
        try:
            self.result = take(project, roots, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - said by finish()
            self.error = exc

    def done(self):
        return not self.thread.is_alive()

    def finish(self, **changes):
        """Wait for the checkpoint; CHANGES (start=..., name=...) go into its record. Its record."""
        self.thread.join()
        if not self.ran:  # its thread never ran its work (a stand-in for threads): taken here, now
            project, roots, kwargs = self.args
            self.checkpoint(project, roots, **kwargs)
        if self.error is not None:
            raise self.error
        meta = self.result
        if changes:
            meta.update({key: value for key, value in changes.items() if value is not None})
            store = store_by_name(meta["store"])
            write_json(store.meta_path(meta["id"]), {k: v for k, v in meta.items() if k not in ("store", "pruned")})
        return meta

    def cancel(self):
        self.stop.set()


# --- runs: which folders a start's checkpoints hold ------------------------------------------------------------------

def runs_dir():
    return store_root() / "runs"


def record_run(key, document):
    """What a start's checkpoints hold (runs/KEY.json: its run, start, name, project, folders, start checkpoint), for
    `checkpoint RUN` and `restore RUN` - also when the start was not kept."""
    if not RUN_KEY.fullmatch(str(key)):
        raise ValueError("not a start's id")
    own_dir(store_root())
    own_dir(runs_dir())
    write_json(runs_dir() / f"{key}.json", {**document, "key": key, "recorded": iso(time.time())})


def run_records():
    found = []
    try:
        names = os.listdir(runs_dir())
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json") or not RUN_KEY.fullmatch(name[:-5]):
            continue
        try:
            record = json.loads((runs_dir() / name).read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get("project"), str) and isinstance(record.get("roots"),
                                                                                              list):
            found.append(record)
    return sorted(found, key=lambda r: str(r.get("recorded") or ""), reverse=True)


def resolve_run(name):
    """The run record of the start NAME (a running or kept start's name or id - sandbox-ai list -, else the name a
    start had when its checkpoint was taken); ValueError when none has checkpoints."""
    records = run_records()
    entry = None
    with contextlib.suppress(ValueError, OSError, ImportError, AttributeError):
        entry = module("starts").find(name)
    if entry is not None:
        ids = {str(entry.get(key)) for key in ("id", "run") if entry.get(key)}
        for record in records:
            if record.get("key") in ids or record.get("start") in ids or record.get("run") in ids:
                return record, entry
        workspace = entry.get("workspace")
        if isinstance(workspace, str) and os.path.isabs(workspace):
            # A start with no checkpoint yet (taken without, or started before checkpoints): its main workspace.
            host = os.path.realpath(workspace)
            return {"key": str(entry.get("id")), "start": entry.get("id"), "run": entry.get("run"),
                    "name": entry.get("name"), "agent": entry.get("agent"), "project": host,
                    "roots": [{"host": host, "sandbox": "/work", "kind": "workspace"}], "checkpoint": None}, entry
    for record in records:
        if record.get("name") == name or record.get("key") == name or record.get("run") == name:
            return record, None
    raise ValueError(f"no start named {printable(name)} has checkpoints (sandbox-ai checkpoint list)")


# --- finding checkpoints ---------------------------------------------------------------------------------------------

def stores():
    try:
        names = sorted(os.listdir(store_root()))
    except OSError:
        return []
    return [Store(store_root() / name) for name in names
            if name not in ("runs",) and not name.startswith(".") and (store_root() / name / "meta").is_dir()]


def store_by_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(name)) or str(name) in (".", ".."):
        raise ValueError("not a store")
    return Store(store_root() / name)


def find(ident):
    """(store, meta) of the checkpoint IDENT; ValueError when there is none."""
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError(f"{printable(ident)[:80]} is no checkpoint id (sandbox-ai checkpoint list)")
    for store in stores():
        meta = read_meta(store.meta_path(ident))
        if meta is not None:
            meta["store"] = store.dir.name
            return store, meta
    raise ValueError(f"no checkpoint {ident} (sandbox-ai checkpoint list)")


def listing(run=None, project=None):
    """The checkpoints (records), newest first: of RUN's start, of PROJECT's store, or every one."""
    if run is not None:
        record, _ = resolve_run(run)
        chosen = [Store(store_root() / project_key(record["project"]))]
        keys = {str(record.get(k)) for k in ("key", "start", "run") if record.get(k)}
        found = [meta for store in chosen for meta in store.metas()
                 if str(meta.get("start")) in keys or str(meta.get("run")) in keys]
    elif project is not None:
        found = Store(store_root() / project_key(os.path.realpath(os.path.expanduser(project)))).metas()
    else:
        found = [meta for store in stores() for meta in store.metas()]
    return sorted(found, key=lambda m: (m.get("created_ts") or 0, m["id"]), reverse=True)


# --- what changed ----------------------------------------------------------------------------------------------------

def under(path, chosen):
    """PATH (relative) is one of CHOSEN or inside one of them ('' : the whole folder)."""
    return any(c == "" or path == c or path.startswith(c + "/") for c in chosen)


def inside_any(path, folders):
    """PATH is one of FOLDERS or below one (the excluded and skipped paths of a checkpoint)."""
    return any(path == f or path.startswith(f + "/") for f in folders)


def file_digest(host_root, rel, kind):
    """The SHA-256 of the file REL of the folder HOST_ROOT now, read without following a link (None: gone, changed
    into something else)."""
    try:
        fd = open_parent(host_root, rel, kind)
    except OSError:
        return None
    try:
        name = rel.rsplit("/", 1)[-1] if rel else os.path.basename(host_root)
        try:
            opened = os.open(name, FILE_FLAGS, dir_fd=fd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(opened).st_mode):
                return None
            head, buf = hashlib.sha256(), buffer()
            view = memoryview(buf)
            while True:
                got = os.readv(opened, [buf])
                if not got:
                    return head.hexdigest()
                head.update(view[:got])
        finally:
            os.close(opened)
    finally:
        os.close(fd)


def open_parent(host_root, rel, kind):
    """A descriptor of REL's folder within HOST_ROOT (a file root: its parent), every part opened without following
    a link (OSError when one is a link, a file or missing)."""
    if kind == "file":
        return os.open(os.path.dirname(host_root), DIR_FLAGS)
    fd = os.open(host_root, DIR_FLAGS)
    try:
        for part in rel.split("/")[:-1] if rel else []:
            sub = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = sub
        return fd
    except BaseException:
        os.close(fd)
        raise


def compare(old_root, new_root, chosen=("",), hash_now=None):
    """What differs between OLD_ROOT (a checkpoint's) and NEW_ROOT (a later checkpoint's or the folder now, files
    without digests: HASH_NOW(rel) gives one when their stat differs): {"added", "deleted", "changed": [rel or
    [rel, what]]} - paths below what OLD excluded or skipped are none of these."""
    old = {e[1]: e for e in old_root["entries"] if under(e[1], chosen)}
    new = {e[1]: e for e in new_root["entries"] if under(e[1], chosen)}
    # Held back (never touched): what OLD left out - it does not hold it -, the names excluded now, and what NEW
    # left out because it is not the folder's own (kept_out: a mount inside, sandbox-ai's state, too deep). Not what
    # is only now something NEW cannot hold - a FIFO or socket where a file or folder was, a folder its owner may
    # no longer read: an agent would take any path out of a restore that way. Those are another kind of thing now
    # ("type": removed, and put back as OLD has it), or new (added).
    held_back = list(old_root.get("excluded") or []) + [p for p, _ in old_root.get("skipped") or []] + \
        list(new_root.get("excluded") or []) + [p for p, why in new_root.get("skipped") or [] if kept_out(why)]
    other_now = [p for p, why in new_root.get("skipped") or []
                 if not kept_out(why) and under(p, chosen) and not inside_any(p, held_back)]
    added, deleted, changed = [], [], []
    for rel, entry in new.items():
        if rel in old or inside_any(rel, held_back):
            continue
        added.append(rel)
    for rel, entry in old.items():
        now = new.get(rel)
        if now is None:
            if rel in other_now:
                changed.append([rel, "type"])
            elif not inside_any(rel, held_back):
                deleted.append(rel)
            continue
        what = difference(entry, now, rel, hash_now)
        if what:
            changed.append([rel, what])
    added += [rel for rel in other_now if rel not in old and rel not in added]
    return {"added": sorted(added), "deleted": sorted(deleted), "changed": sorted(changed)}


# Why a walk left a path out that a restore never touches either (the other reasons - a special file, a folder that
# cannot be read - say what the path is now, not that it is none of the folder's own).
KEPT_OUT = ("another file system", "sandbox-ai's own state", "deeper than")


def kept_out(why):
    return str(why).startswith(KEPT_OUT)


def difference(old, new, rel, hash_now=None):
    """How the entry NEW differs from OLD: "type", "content", "link", "mode", or None."""
    if old[0] != new[0]:
        return "type"
    if old[0] == "l":
        return "link" if old[2] != new[2] else None
    if old[0] == "d":
        return "mode" if old[2] != new[2] else None
    if new[5] is None:  # the folder now: its content read only when its stat differs
        same_stat = old[4] == new[4] and old[3] == new[3] and old[6] == new[6] and old[7] == new[7] and old[7]
        if not same_stat:
            if old[4] != new[4]:
                return "content"
            if hash_now is not None and hash_now(rel) != old[5]:
                return "content"
    elif old[5] != new[5]:
        return "content"
    return "mode" if old[2] != new[2] else None


def chosen_paths(root, paths):
    """The relative paths of ROOT that PATHS name (each: relative to the main folder, its path in the sandbox such
    as /work/src, or its host path); ('',) for all. ValueError for a path outside every folder."""
    if not paths:
        return ("",)
    found = []
    for path in paths:
        rel = relative_in(root, path)
        if rel is not None:
            found.append(rel)
    return tuple(found)


def relative_in(root, path):
    """PATH within ROOT ({"host", "sandbox"}) as a relative path, '' the folder itself, None when it is not in it."""
    path = str(path)
    if "\0" in path:
        raise ValueError("a path has no NUL")
    for base in (root.get("sandbox"), root.get("host")):
        if base and os.path.isabs(path):
            normal = os.path.normpath(path)
            if normal == base:
                return ""
            if normal.startswith(base.rstrip("/") + "/"):
                rel = normal[len(base.rstrip("/")) + 1:]
                return safe_relative(rel)
    if not os.path.isabs(path) and root.get("first"):  # relative: to the main folder (/work)
        normal = os.path.normpath(path)
        if normal == ".":
            return ""
        if normal.startswith("../") or normal == "..":
            raise ValueError(f"{printable(path)[:200]}: outside the folder")
        return safe_relative(normal)
    return None


def check_paths(roots, paths):
    """Every path of PATHS lies in one of ROOTS; ValueError naming one that does not."""
    for path in paths or ():
        if not any(relative_in({**root, "first": index == 0}, path) is not None for index, root in enumerate(roots)):
            raise ValueError(f"{printable(path)[:200]} is in none of the checkpoint's folders "
                             f"({', '.join(printable(r.get('sandbox') or r['host']) for r in roots)})")


def diff(ident, other=None, paths=()):
    """What changed since the checkpoint IDENT: in its folders now (OTHER None), or in the checkpoint OTHER.
    {"id", "against", "roots": [{"host", "sandbox", "added", "deleted", "changed"}], "counts"}."""
    store, meta = find(ident)
    with store.locked():
        document = store.manifest(ident)
        later = None
        if other is not None:
            other_store, _ = find(other)
            later = other_store.manifest(other)
    check_paths(document["roots"], paths)
    result = {"id": ident, "against": other or "now", "roots": []}
    for index, root in enumerate(document["roots"]):
        chosen = chosen_paths({**root, "first": index == 0}, paths)
        if not chosen or root.get("type") == "missing":
            continue
        if later is not None:
            now_root = next((r for r in later["roots"] if r["host"] == root["host"]), None)
            if now_root is None:
                now_root = {"entries": [], "excluded": [], "skipped": []}
            changes = compare(root, now_root, chosen)
        else:
            walk = Walk(root["host"], meta.get("exclude") or [], None).run()
            changes = compare(root, walk.document({}), chosen,
                              lambda rel, r=root, kind=walk.kind: file_digest(r["host"], rel, kind))
        result["roots"].append({"host": root["host"], "sandbox": root.get("sandbox"), **changes})
    result["counts"] = counts(result["roots"])
    return result


def counts(roots):
    return {key: sum(len(root[key]) for root in roots) for key in ("added", "deleted", "changed")}


def trimmed(result, limit=LISTED):
    """RESULT with at most LIMIT paths per kind of change and folder (the counts stay whole; "more": what was left)."""
    roots = []
    for root in result["roots"]:
        shown = dict(root)
        for key in ("added", "deleted", "changed"):
            shown[key] = root[key][:limit]
            if len(root[key]) > limit:
                shown.setdefault("more", {})[key] = len(root[key]) - limit
        roots.append(shown)
    return {**result, "roots": roots}


# --- restoring -------------------------------------------------------------------------------------------------------

def rmtree_at(parent_fd, name):
    """Remove NAME in the folder PARENT_FD - a file, a link, or a folder with everything in it - never following a
    link (a link is removed itself)."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = open_own_dir(name, parent_fd)
    try:
        with os.scandir(fd) as found:
            names = [entry.name for entry in found]
        for child in names:
            rmtree_at(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def chmod_nofollow(name, mode, dir_fd):
    """NAME's mode set by name without following a link (one swapped in is never followed to what it names: a file
    of the host would get the mode). False: not changed (a link, or this system cannot)."""
    try:
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except (OSError, NotImplementedError, ValueError):
        return False


def writable(fd):
    """The open folder FD made one its owner may list and write into (a restore writes there; its mode is put back
    as the checkpoint has it afterwards). Whether it had to be changed."""
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    if mode & 0o700 == 0o700:
        return False
    with contextlib.suppress(OSError):
        os.fchmod(fd, mode | 0o700)
    return True


def open_own_dir(name, parent_fd):
    """A descriptor of the folder NAME in PARENT_FD (no link followed), its owner's also when its permission bits
    say otherwise (an agent's chmod 000): made listable and writable for the owner first."""
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except PermissionError:
        if not chmod_nofollow(name, 0o700, parent_fd):
            raise
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    writable(fd)
    return fd


class Restorer:
    """One folder of a checkpoint put back: every folder on the way opened without following a link, from the
    folder's own descriptor (nothing outside it is ever written)."""

    def __init__(self, store, host, kind):
        self.store, self.host, self.kind = store, host, kind
        self.written = self.removed = 0
        self.made, self.made_lock = set(), threading.Lock()  # folders made on the way to a path (rel)

    def root_fd(self, create=True):
        if self.kind == "file":
            return os.open(os.path.dirname(self.host), DIR_FLAGS)
        try:
            try:
                fd = os.open(self.host, DIR_FLAGS)
            except PermissionError:  # (its owner may not read it now: a chmod of the agent's)
                os.chmod(self.host, 0o700, follow_symlinks=False) if os.chmod in os.supports_follow_symlinks \
                    else os.lchmod(self.host, 0o700)
                fd = os.open(self.host, DIR_FLAGS)
            if writable(fd):
                with self.made_lock:
                    self.made.add("")
            return fd
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(self.host, 0o700)
            return os.open(self.host, DIR_FLAGS)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError(f"{printable(self.host)} is no folder now (a link or a file): restore refused - "
                                 "move it away first") from None
            raise

    def folder_fd(self, rel_dir, make=True):
        """A descriptor of the folder REL_DIR (relative; '' the root), made where missing; a link or file in the way
        is replaced by a folder (MAKE) - never followed."""
        fd = self.root_fd()
        try:
            done = []
            for part in rel_dir.split("/") if rel_dir else []:
                done.append(part)
                try:
                    try:
                        sub = os.open(part, DIR_FLAGS, dir_fd=fd)
                    except PermissionError:  # (a folder its owner may not read now: opened as its owner's)
                        if not chmod_nofollow(part, 0o700, fd):
                            raise
                        sub = os.open(part, DIR_FLAGS, dir_fd=fd)
                    if writable(sub):  # (its mode goes back to the checkpoint's at the end: made)
                        with self.made_lock:
                            self.made.add("/".join(done))
                except OSError as exc:
                    if not make or exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                        raise
                    if exc.errno != errno.ENOENT:
                        os.unlink(part, dir_fd=fd)  # a link or file where the folder belongs
                        self.removed += 1
                    os.mkdir(part, 0o700, dir_fd=fd)
                    with self.made_lock:
                        self.made.add("/".join(done))
                    sub = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = sub
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def split(rel):
        return (rel.rsplit("/", 1) if "/" in rel else ("", rel))

    def name_of(self, rel):
        return os.path.basename(self.host) if self.kind == "file" else self.split(rel)[1]

    def remove(self, rel):
        if rel == "" and self.kind != "file":
            fd = self.root_fd()
            try:
                with os.scandir(fd) as found:
                    names = [entry.name for entry in found]
                for name in names:
                    rmtree_at(fd, name)
            finally:
                os.close(fd)
        else:
            parent, name = self.split(rel)
            try:
                fd = self.folder_fd(parent if self.kind != "file" else "", make=False)
            except FileNotFoundError:
                return
            try:
                rmtree_at(fd, self.name_of(rel))
            finally:
                os.close(fd)
        self.removed += 1

    def make_dir(self, rel, mode):
        if rel == "":
            fd = self.root_fd()
            os.close(fd)
            return
        parent, name = self.split(rel)
        fd = self.folder_fd(parent)
        try:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    os.unlink(name, dir_fd=fd)
                    raise FileNotFoundError
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=fd)
                self.written += 1
        finally:
            os.close(fd)

    def write_file(self, rel, mode, mtime_ns, digest):
        parent, _ = self.split(rel)
        return self.write_files(parent if self.kind != "file" else "", [(rel, mode, mtime_ns, digest)])

    def write_files(self, parent, files):
        """FILES ([(rel, mode, mtime_ns, digest)]) of the folder PARENT written (one descriptor of it for all of
        them; several folders at once in the pool). The number written."""
        fd = self.folder_fd(parent)
        try:
            for rel, mode, mtime_ns, digest in files:
                self.write_into(fd, self.name_of(rel), rel, mode, mtime_ns, digest)
            return len(files)
        finally:
            os.close(fd)

    def write_into(self, fd, name, rel, mode, mtime_ns, digest):
        """The object DIGEST as NAME in the folder FD: written beside it, checked against its digest, renamed onto
        it (a folder in its place is removed first)."""
        if True:
            temp = TEMP_PREFIX + secrets.token_hex(6)
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                          dir_fd=fd)
            try:
                source = self.store.open_object(digest)
                try:
                    head, buf = hashlib.sha256(), buffer()
                    view = memoryview(buf)
                    while True:
                        got = os.readv(source, [buf])
                        if not got:
                            break
                        head.update(view[:got])
                        write_all(out, view[:got])
                finally:
                    os.close(source)
                if head.hexdigest() != digest:
                    raise ValueError(f"the checkpoint's copy of {printable(rel) or printable(self.host)} is damaged "
                                     "(its content does not match its digest)")
                os.fchmod(out, mode & 0o7777)
                os.utime(out, ns=(mtime_ns, mtime_ns))
            except BaseException:
                os.close(out)
                out = None
                with contextlib.suppress(OSError):
                    os.unlink(temp, dir_fd=fd)
                raise
            finally:
                if out is not None:
                    os.close(out)
            with contextlib.suppress(FileNotFoundError):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    rmtree_at(fd, name)
            os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)

    def write_link(self, rel, target):
        parent, name = self.split(rel)
        fd = self.folder_fd(parent)
        try:
            rmtree_at(fd, name)
            os.symlink(target, name, dir_fd=fd)
            self.written += 1
        finally:
            os.close(fd)

    def chmod(self, rel, mode, mtime_ns=None, folder=False):
        if rel == "" and self.kind != "file":
            fd = self.root_fd()
            try:
                os.fchmod(fd, mode & 0o7777)
                if mtime_ns is not None:
                    os.utime(fd, ns=(mtime_ns, mtime_ns))
            finally:
                os.close(fd)
            return
        parent, name = self.split(rel)
        name = self.name_of(rel)
        fd = self.folder_fd(parent if self.kind != "file" else "", make=False)
        try:
            try:
                target = os.open(name, DIR_FLAGS if folder else FILE_FLAGS, dir_fd=fd)
            except PermissionError:  # no read permission for you now: changed by name (checked to be no link)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if folder != stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    return
                if not chmod_nofollow(name, mode & 0o7777, fd):  # (a link swapped in meanwhile: never followed)
                    return
                if mtime_ns is not None:
                    os.utime(name, ns=(mtime_ns, mtime_ns), dir_fd=fd, follow_symlinks=False)
                return
            try:
                info = os.fstat(target)
                if folder != stat.S_ISDIR(info.st_mode):
                    return
                os.fchmod(target, mode & 0o7777)
                if mtime_ns is not None:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
            finally:
                os.close(target)
        finally:
            os.close(fd)


def apply_root(store, target_root, current_root, chosen):
    """Put the folder of TARGET_ROOT (a checkpoint's) back as it was, within CHOSEN paths; CURRENT_ROOT: the same
    folder's checkpoint taken just before (what is there now). {"written", "removed"}."""
    kind = target_root.get("type") or "dir"
    restorer = Restorer(store, target_root["host"], kind)
    changes = compare(target_root, current_root, chosen)
    target = {e[1]: e for e in target_root["entries"]}
    current = {e[1]: e for e in current_root["entries"]}
    # What is there and was not then (or is of another kind now): removed, the outermost of each tree.
    gone = sorted(changes["added"] + [rel for rel, what in changes["changed"] if what == "type"])
    removed = []
    for rel in gone:
        if any(rel.startswith(done + "/") for done in removed if done) or ("" in removed):
            continue
        restorer.remove(rel)
        removed.append(rel)
    # Folders first (parents before children), then files and links.
    wanted = [e for rel, e in sorted(target.items()) if under(rel, chosen)]
    missing = set(changes["deleted"]) | {rel for rel, what in changes["changed"] if what == "type"}
    for entry in wanted:
        if entry[0] == "d" and (entry[1] in missing or entry[1] == ""):
            restorer.make_dir(entry[1], entry[2])
    changed = {rel: what for rel, what in changes["changed"]}
    by_folder, large = {}, []
    for entry in wanted:
        rel = entry[1]
        if entry[0] == "f" and (rel in missing or changed.get(rel) == "content"):
            parent = restorer.split(rel)[0] if kind != "file" else ""
            if entry[4] >= LARGE:
                large.append((parent, [(rel, entry[2], entry[3], entry[5])]))
            else:
                by_folder.setdefault(parent, []).append((rel, entry[2], entry[3], entry[5]))
    for parent, files in by_folder.items():  # small files in place (a thread each costs more than it saves)
        restorer.written += restorer.write_files(parent, files)
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="sandbox-ai-restore") as pool:
        jobs = [pool.submit(restorer.write_files, parent, files) for parent, files in large]
        failed = None
        for job in jobs:
            try:
                restorer.written += job.result()
            except BaseException as exc:  # noqa: BLE001 - every folder is waited for, the first failure raised
                failed = failed or exc
        if failed is not None:
            raise failed
    for entry in wanted:
        rel = entry[1]
        if entry[0] == "f" and (rel in missing or changed.get(rel) == "content"):
            continue  # (written above)
        elif entry[0] == "f" and changed.get(rel) == "mode":
            restorer.chmod(rel, entry[2])
        elif entry[0] == "l" and (rel in missing or changed.get(rel) == "link"):
            restorer.write_link(rel, entry[2])
    # The folders' modes and times last, the deepest first (writing into a folder changes its time) - those made on
    # the way to a chosen path too (src restored for src/pkg: as the checkpoint holds it, not 0700 and now).
    folders = {e[1]: e for e in wanted if e[0] == "d"}
    folders.update({rel: target[rel] for rel in restorer.made if rel in target and target[rel][0] == "d"})
    for entry in sorted(folders.values(), key=lambda e: e[1].count("/") + (e[1] != ""), reverse=True):
        with contextlib.suppress(OSError):
            restorer.chmod(entry[1], entry[2], entry[3], folder=True)
    return {"written": restorer.written, "removed": restorer.removed}


def plan_restore(target):
    """RUN|ID: (store, meta) of the checkpoint a restore puts back - an id as it is, a start's name its newest
    start checkpoint (the state before its agent began)."""
    if isinstance(target, str) and ID.fullmatch(target):
        return find(target)
    record, _ = resolve_run(target)
    keys = {str(record.get(k)) for k in ("key", "start", "run") if record.get(k)}
    store = Store(store_root() / project_key(record["project"]))
    metas = [m for m in store.metas() if str(m.get("start")) in keys or str(m.get("run")) in keys]
    starts = [m for m in metas if m.get("kind") == "start"]
    if record.get("checkpoint") and any(m["id"] == record["checkpoint"] for m in metas):
        meta = next(m for m in metas if m["id"] == record["checkpoint"])
    elif starts:
        meta = starts[-1]
    else:
        raise ValueError(f"{printable(target)} has no checkpoint of its start (sandbox-ai checkpoint list "
                         f"{printable(target)}; restore one by its id)")
    return store, meta


def unlock(root, exclude=()):
    """Before a restore of ROOT (a checkpoint's folder: its "host", what it excluded and skipped): the folders in it
    whose owner may not read them now (an agent's chmod 000) are made their owner's again - by name, never through
    a link -, so that the checkpoint of the state now holds what is in them and the restore compares it (an
    excluded folder inside stays, what was added goes). What the checkpoint itself did not hold - a folder that was
    unreadable then - stays as it is. The restore puts the checkpoint's permissions back. The folders unlocked."""
    host = root["host"]
    kept = list(root.get("excluded") or []) + [p for p, _ in root.get("skipped") or []]
    done = []
    with contextlib.suppress(OSError, NotImplementedError, ValueError):
        info = os.lstat(host)
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o500 != 0o500:
            os.chmod(host, stat.S_IMODE(info.st_mode) | 0o700, follow_symlinks=False)
            done.append("")
    for _ in range(DEPTH_LIMIT):
        try:
            walk = Walk(host, exclude, None).run()
        except (OSError, ValueError):
            break
        locked = [rel for rel, why in walk.skipped
                  if str(why).startswith("cannot be read") and rel and not inside_any(rel, kept) and rel not in done]
        if not locked:
            break
        for rel in locked:
            done.append(rel)
            with contextlib.suppress(OSError):
                parent = open_parent(host, rel, walk.kind)
                try:
                    name = rel.rsplit("/", 1)[-1]
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        chmod_nofollow(name, stat.S_IMODE(info.st_mode) | 0o700, parent)
                finally:
                    os.close(parent)
    return done


def restore(ident, paths=(), label=None):
    """Put the checkpoint IDENT back (its folders, or only PATHS): first a checkpoint of the state now (kind
    restore), then every change since undone. {"id", "before", "roots": [...changes, written, removed], "counts"}."""
    store, meta = find(ident)
    with store.locked():
        document = store.manifest(ident)
        check_paths(document["roots"], paths)
        roots = [{"host": r["host"], "sandbox": r.get("sandbox"), "kind": r.get("kind")} for r in document["roots"]]
        for index, root in enumerate(document["roots"]):
            if root.get("type") in (None, "dir") and chosen_paths({**root, "first": index == 0}, paths):
                unlock(root, meta.get("exclude") or [])
        before = take(meta["project"], roots, kind="restore", label=label or f"before restoring {ident}",
                      run=meta.get("run"), start=meta.get("start"), name=meta.get("name"), agent=meta.get("agent"),
                      exclude=meta.get("exclude") or [], prune_after=False)
        now = store.manifest(before["id"])
        result = {"id": ident, "before": before["id"], "roots": []}
        for index, root in enumerate(document["roots"]):
            chosen = chosen_paths({**root, "first": index == 0}, paths)
            if not chosen or root.get("type") == "missing":
                continue  # (a folder that was not there then: left as it is)
            current = next(r for r in now["roots"] if r["host"] == root["host"])
            changes = compare(root, current, chosen)
            done = apply_root(store, root, current, chosen)
            result["roots"].append({"host": root["host"], "sandbox": root.get("sandbox"), **changes, **done})
    result["counts"] = counts(result["roots"])
    return result


# --- keeping the stores small ----------------------------------------------------------------------------------------

def prune(store, limits=None, now=None, dry_run=False, keep=(), live=()):
    """Apply the limits (settings: checkpoint_days, checkpoint_count, checkpoint_gb) to STORE: the oldest checkpoints
    beyond them go (periodic ones first for the count), the newest one and KEEP (ids) never; LIVE (run ids): the
    start checkpoints of those runs stay too. Then every object no checkpoint holds goes. {"removed": [ids],
    "freed": bytes, "bytes": what stays}."""
    limits = limits or settings()
    now = time.time() if now is None else now
    with store.locked():
        metas = store.metas()
        if not metas:
            return {"removed": [], "freed": 0, "bytes": 0}
        protect = {metas[-1]["id"], *keep}
        protect |= {m["id"] for m in metas if m.get("kind") == "start" and m.get("run") in set(live)}
        drop = []
        days = limits["checkpoint_days"]
        if days:
            drop += [m["id"] for m in metas if m["id"] not in protect and (m.get("created_ts") or 0) < now - days * 86400]
        count = limits["checkpoint_count"]
        left = [m for m in metas if m["id"] not in drop]
        if count and len(left) > count:
            order = sorted((m for m in left if m["id"] not in protect),
                           key=lambda m: (m.get("kind") != "periodic", m.get("created_ts") or 0))
            drop += [m["id"] for m in order[:len(left) - count]]
        references = None
        limit = limits["checkpoint_gb"] << 30
        usage = store.usage()
        swept = store.taking().exists()  # a checkpoint ended mid-way (the store is locked: none is written now)
        if drop or swept or usage is None or (limit and usage > limit):
            references = object_references(store, metas)
        freed = 0
        if references is not None:
            refs, sizes, holds = references
            for ident in drop:
                freed += release(refs, sizes, holds, ident)
            total = sum(sizes[h] for h, n in refs.items() if n > 0)
            if limit and total > limit:
                for meta in metas:
                    if total <= limit:
                        break
                    if meta["id"] in protect or meta["id"] in drop:
                        continue
                    released = release(refs, sizes, holds, meta["id"])
                    drop.append(meta["id"])
                    freed += released
                    total -= released
            if not dry_run:
                for ident in drop:
                    with contextlib.suppress(FileNotFoundError):
                        store.meta_path(ident).unlink()
                    with contextlib.suppress(FileNotFoundError):
                        store.manifest_path(ident).unlink()
                remove_objects(store, {h for h, n in refs.items() if n > 0})
                store.set_usage(total)
                if swept:
                    with contextlib.suppress(FileNotFoundError):
                        store.taking().unlink()
        else:
            total = usage
        return {"removed": drop, "freed": freed, "bytes": total}


def object_references(store, metas):
    """({digest: number of checkpoints holding it}, {digest: size}, {id: its digests}) of STORE's checkpoints METAS
    (their manifests read one at a time)."""
    refs, sizes, holds = {}, {}, {}
    for meta in metas:
        try:
            document = store.manifest(meta["id"])
        except ValueError:
            holds[meta["id"]] = set()
            continue
        held = {e[5]: e[4] for root in document["roots"] for e in root["entries"] if e[0] == "f"}
        holds[meta["id"]] = set(held)
        for digest, size in held.items():
            refs[digest] = refs.get(digest, 0) + 1
            sizes[digest] = size
    return refs, sizes, holds


def release(refs, sizes, holds, ident):
    """The checkpoint IDENT dropped from REFS: the bytes of what only it held."""
    freed = 0
    for digest in holds.get(ident, ()):
        refs[digest] -= 1
        if refs[digest] == 0:
            freed += sizes.get(digest, 0)
    return freed


def remove_objects(store, live):
    """Every object of STORE not in LIVE, and what an interrupted checkpoint left in objects/tmp (the store is
    locked: nothing writes there now)."""
    with contextlib.suppress(OSError):
        for name in os.listdir(store.objects / "tmp"):
            with contextlib.suppress(OSError):
                os.unlink(store.objects / "tmp" / name)
    try:
        prefixes = os.listdir(store.objects)
    except OSError:
        return
    for prefix in prefixes:
        if not re.fullmatch(r"[0-9a-f]{2}", prefix):
            continue
        folder = store.objects / prefix
        with contextlib.suppress(OSError):
            for name in os.listdir(folder):
                if prefix + name not in live:
                    with contextlib.suppress(OSError):
                        os.unlink(folder / name)
            with contextlib.suppress(OSError):
                os.rmdir(folder)  # (only when empty)


def prune_all(dry_run=False, live=()):
    """sandbox-ai gc: every store pruned (prune). [{"store", "project", "removed", "freed", "bytes"}]."""
    found = []
    limits = settings()
    for store in stores():
        try:
            done = prune(store, limits, dry_run=dry_run, live=live)
        except (OSError, ValueError) as exc:
            found.append({"store": store.dir.name, "project": store.project(), "error": printable(exc)[:300]})
            continue
        found.append({"store": store.dir.name, "project": store.project(), **done})
    if not dry_run:
        with contextlib.suppress(OSError):
            forget_runs(live)
    return found


RECORD_DAYS = 30  # a run record no checkpoint needs is forgotten (gc) once it is this old


def forget_runs(live=(), now=None):
    """The run records (runs/KEY.json) older than RECORD_DAYS whose start and run no checkpoint of any store names
    any more removed - never one of a run in LIVE. [keys removed]."""
    now = time.time() if now is None else now
    named = set()
    for store in stores():
        for meta in store.metas():
            named |= {str(meta.get(key)) for key in ("start", "run") if meta.get(key)}
    removed = []
    for record in run_records():
        key = record.get("key")
        if not isinstance(key, str) or not RUN_KEY.fullmatch(key) or record.get("run") in set(live):
            continue
        ids = {str(record.get(k)) for k in ("key", "start", "run") if record.get(k)}
        path = runs_dir() / f"{key}.json"
        with contextlib.suppress(OSError):
            if ids & named or os.lstat(path).st_mtime > now - RECORD_DAYS * 86400:
                continue
            path.unlink()
            removed.append(key)
    return removed


def delete(idents):
    """Remove the checkpoints IDENTS (and the content only they held). [ids removed]."""
    by_store = {}
    for ident in idents:
        store, meta = find(ident)
        by_store.setdefault(store.dir, (store, []))[1].append(ident)
    removed = []
    for store, chosen in by_store.values():
        with store.locked():
            for ident in chosen:
                with contextlib.suppress(FileNotFoundError):
                    store.meta_path(ident).unlink()
                with contextlib.suppress(FileNotFoundError):
                    store.manifest_path(ident).unlink()
                removed.append(ident)
            metas = store.metas()
            refs, sizes, _ = object_references(store, metas)
            remove_objects(store, {h for h, n in refs.items() if n > 0})
            store.set_usage(sum(sizes[h] for h, n in refs.items() if n > 0))
    return removed


def usage():
    """sandbox-ai gc --usage: {"bytes": the content kept (as recorded), "stores": number, "checkpoints": number}."""
    total, count, number = 0, 0, 0
    for store in stores():
        number += 1
        count += len(store.metas())
        total += store.usage() or 0
    return {"bytes": total, "stores": number, "checkpoints": count, "path": str(store_root())}


# --- a running start's periodic checkpoints --------------------------------------------------------------------------

def changed_since(meta, store):
    """Whether any folder of the checkpoint META differs from it now (by stat; a file's content is not read)."""
    try:
        document = store.manifest(meta["id"])
    except ValueError:
        return True
    for root in document["roots"]:
        try:
            walk = Walk(root["host"], meta.get("exclude") or [], None).run()
        except (OSError, ValueError):
            return True
        # A file whose stat differs counts as changed without reading it (lambda: no digest is any digest).
        if any(compare(root, walk.document({}), hash_now=lambda rel: None).values()):
            return True
    return False


class Periodic:
    """Every MINUTES a checkpoint of the start's folders, when something changed since the newest one of its run."""

    def __init__(self, minutes, project, roots, stop=None, **names):
        self.minutes, self.project, self.roots, self.names = minutes, project, roots, names
        self.stopped = stop or threading.Event()
        self.taken, self.errors = [], []
        self.thread = threading.Thread(target=self.loop, name="sandbox-ai-checkpoints", daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stopped.wait(self.minutes * 60):
            self.once()

    def once(self):
        try:
            store = Store.of(self.project)
            own = [m for m in store.metas() if m.get("run") == self.names.get("run")]
            if own and not changed_since(own[-1], store):
                return None
            meta = take(self.project, self.roots, kind="periodic", stop=self.stopped, **self.names)
            self.taken.append(meta["id"])
            return meta
        except Exception as exc:  # noqa: BLE001 - the next try comes; the start goes on
            self.errors.append(printable(exc)[:300])
            return None

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=30)


# --- the command -----------------------------------------------------------------------------------------------------

def confirm(question, yes=False):
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("nothing can ask here: add --yes to confirm (after a look with --dry-run)")
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def shown_root(root):
    return printable(root.get("sandbox") or root["host"])


def joined(base, rel):
    return f"{base.rstrip('/')}/{rel}" if rel else base


def changes_text(result, out, verb="since"):
    """The changes of RESULT (diff, restore's preview), SHOWN paths per kind and folder."""
    total = result["counts"]
    for root in result["roots"]:
        base = shown_root(root)
        size = sum(len(root[k]) for k in ("added", "deleted", "changed"))
        out(f"{base} ({printable(root['host'])}): " + (", ".join(
            f"{len(root[k])} {k}" for k in ("changed", "added", "deleted") if root[k]) or "no change"))
        if not size:
            continue
        for key, mark in (("changed", "M"), ("added", "A"), ("deleted", "D")):
            items = root[key]
            for item in items[:SHOWN]:
                rel, what = (item if isinstance(item, list) else (item, None))
                out(f"  {mark} {printable(joined(base, rel))}" + (f"  ({what})" if what and what != "content" else ""))
            more = len(items) - SHOWN + (root.get("more") or {}).get(key, 0)
            if more > 0:
                out(f"  ... and {more} more {key}")
    out(f"{total['changed']} changed, {total['added']} added, {total['deleted']} deleted")


def meta_row(meta):
    """A checkpoint's record as list --json and show --json give it."""
    row = {key: meta.get(key) for key in ("id", "created", "kind", "label", "name", "run", "start", "agent",
                                          "project", "files", "bytes", "new_bytes", "seconds", "store")}
    row["roots"] = [{key: root.get(key) for key in ("host", "sandbox", "kind", "type", "files", "dirs", "links",
                                                    "bytes", "excluded", "skipped", "torn")}
                    for root in meta.get("roots", []) if isinstance(root, dict)]
    return row


def create_main(a, out):
    record, entry = resolve_run(a.run)
    roots = record["roots"]
    meta = take(record["project"], roots, kind="manual", label=a.label, run=record.get("run"),
                start=record.get("start"), name=(entry or {}).get("name") or record.get("name"),
                agent=record.get("agent"), exclude=record.get("exclude"))
    if a.json:
        print(json.dumps(meta_row(meta), indent=2))
    else:
        out(f"sandbox-ai checkpoint: {meta['id']} of {printable(meta.get('name') or a.run)}: {meta['files']} files, "
            f"{human(meta['bytes'])} ({human(meta['new_bytes'])} new) in {meta['seconds']:.1f} s")
        for root in meta["roots"]:
            for path, why in root.get("skipped") or []:
                out(f"  skipped {printable(joined(shown_root(root), path))}: {printable(why)}")
            if root.get("torn"):
                out(f"  {root['torn']} file(s) changed while taken: kept as read")
    return 0


def list_main(a, out):
    rows = [meta_row(meta) for meta in listing(run=a.run, project=a.project)]
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        out("No checkpoints yet (one is taken when a start begins; sandbox-ai checkpoint RUN takes one now).")
        return 0
    out(f"{'ID':<23} {'CREATED':<20} {'KIND':<8} {'START':<24} {'FILES':>7} {'SIZE':>10}  LABEL / FOLDERS")
    for row in rows:
        where = ", ".join(printable(r.get("sandbox") or r.get("host")) for r in row["roots"])
        out(f"{row['id']:<23} {(row['created'] or '')[:19]:<20} {row['kind'] or '':<8} "
            f"{printable(row.get('name') or row.get('run') or '-')[:24]:<24} {row['files'] or 0:>7} "
            f"{human(row['bytes']):>10}  {printable(row['label'] or where)[:60]}")
    return 0


def show_main(a, out):
    store, meta = find(a.id)
    row = meta_row(meta)
    if a.files:
        document = store.manifest(a.id)
        row["paths"] = [{"root": root["host"], "entries": [
            {"path": e[1], "type": {"d": "dir", "f": "file", "l": "link"}[e[0]],
             **({"mode": oct(e[2]), "size": e[4], "sha256": e[5]} if e[0] == "f" else
                {"mode": oct(e[2])} if e[0] == "d" else {"target": e[2]})} for e in root["entries"]],
            "excluded": root.get("excluded"), "skipped": root.get("skipped")} for root in document["roots"]]
    if a.json:
        print(json.dumps(row, indent=2))
        return 0
    out(f"{row['id']}  {row['kind']}  {row['created']}" + (f"  {printable(row['label'])}" if row["label"] else ""))
    out(f"  start: {printable(row.get('name') or '-')} (run {printable(row.get('run') or '-')}, "
        f"agent {printable(row.get('agent') or '-')})")
    out(f"  project: {printable(row['project'])}; {row['files']} files, {human(row['bytes'])} "
        f"({human(row['new_bytes'])} new), taken in {row['seconds']} s")
    for root in row["roots"]:
        out(f"  {printable(root.get('sandbox') or '-')} = {printable(root['host'])}: {root.get('files')} files, "
            f"{root.get('dirs')} folders, {root.get('links')} links, {root.get('excluded')} excluded")
        for path, why in root.get("skipped") or []:
            out(f"    skipped {printable(path)}: {printable(why)}")
    for root in row.get("paths") or []:
        for entry in root["entries"]:
            out(f"    {entry['type']:<4} {printable(joined(root['root'], entry['path']))}")
    return 0


def diff_main(a, out):
    result = diff(a.id, a.other, a.path)
    if a.json:
        print(json.dumps(trimmed(result), indent=2))
        return 0
    out(f"Since {a.id}" + (f" (to {a.other})" if a.other else " (to now)") + ":")
    changes_text(result, out)
    return 0


def restore_main(a, out):
    store, meta = plan_restore(a.target)
    preview = diff(meta["id"], None, a.path)
    if a.json and a.dry_run:
        print(json.dumps({**trimmed(preview), "restore": meta["id"], "kind": meta.get("kind"),
                          "created": meta.get("created"), "dry_run": True}, indent=2))
        return 0
    if not a.json or a.dry_run:
        out(f"Restore {meta['id']} ({meta.get('kind')}, {meta.get('created')}"
            + (f", {printable(meta['label'])}" if meta.get("label") else "") + ") - what changed since then, which "
            "the restore undoes (changed and deleted: written again as they were, added: removed):")
        changes_text(preview, out)
    if a.dry_run:
        return 0
    running = running_note(meta)
    if running and not a.json:
        out(running)
    if not sum(preview["counts"].values()):
        if a.json:
            print(json.dumps({**trimmed(preview), "restore": meta["id"], "restored": False, "before": None}, indent=2))
        else:
            out("Nothing to restore: the folders are as they were.")
        return 0
    if not confirm(f"Put these back as they were at {meta['created']}? (a checkpoint of the state now is taken "
                   "first)", a.yes):
        out("Nothing changed.")
        return 1
    result = restore(meta["id"], a.path)
    if a.json:
        print(json.dumps({**trimmed(result), "restore": meta["id"], "restored": True, "running": running},
                         indent=2))
    else:
        written = sum(r["written"] for r in result["roots"])
        removed = sum(r["removed"] for r in result["roots"])
        out(f"sandbox-ai checkpoint: restored {meta['id']} ({written} written, {removed} removed); undo it with: "
            f"sandbox-ai checkpoint restore {result['before']}")
    return 0


def running_note(meta):
    """A note when the start of META runs now: its agent may write while the restore runs (pause it first)."""
    with contextlib.suppress(Exception):
        entry = module("starts").find(meta.get("name") or meta.get("start") or meta.get("run"))
        if entry and entry.get("state") == "running":
            return (f"note: {printable(entry['name'])} runs now: its agent may write while this restore runs - "
                    f"sandbox-ai pause {printable(entry['name'])} first, resume it after")
    return None


def delete_main(a, out):
    metas = [find(ident)[1] for ident in a.ids]
    for meta in metas:
        out(f"  {meta['id']}  {meta.get('kind')}  {meta.get('created')}  {printable(meta.get('label') or '')}")
    if not confirm(f"Delete {len(metas)} checkpoint(s)?", a.yes):
        out("Nothing changed.")
        return 1
    removed = delete([meta["id"] for meta in metas])
    out(f"sandbox-ai checkpoint: deleted {', '.join(removed)}")
    return 0


SUBCOMMANDS = ("create", "list", "show", "diff", "restore", "delete")


def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai checkpoint", formatter_class=help_style, allow_abbrev=False,
                                description="Checkpoints of a start's workspaces, taken on the host (never reachable "
                                            "from the sandbox): one when a start begins, others when you ask or "
                                            "every few minutes; a restore puts everything back as it was, after "
                                            "showing what changes. `sandbox-ai checkpoint RUN` takes one now.")
    commands = p.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="A checkpoint of a start's workspaces now (also: checkpoint RUN)",
                                 formatter_class=help_style,
                                 description="A checkpoint of the start's workspaces (those its start checkpoint "
                                             "holds) now - while it runs or not.")
    create.add_argument("run", metavar="RUN", help="the start (sandbox-ai list)")
    create.add_argument("--label", metavar="TEXT", help="Why it was taken, shown in the list")
    create.add_argument("--json", action="store_true", help="Its record as JSON")
    listing_ = commands.add_parser("list", help="The checkpoints, newest first", formatter_class=help_style,
                                   description="The checkpoints, newest first: of a start, of a project, or all.")
    listing_.add_argument("run", metavar="RUN", nargs="?", help="only this start's")
    listing_.add_argument("--project", metavar="DIR", help="Only those of this project (its main workspace)")
    listing_.add_argument("--json", action="store_true", help="As JSON")
    show = commands.add_parser("show", help="One checkpoint", formatter_class=help_style,
                               description="One checkpoint: when and why it was taken, its folders, their sizes.")
    show.add_argument("id", metavar="ID", help="the checkpoint (sandbox-ai checkpoint list)")
    show.add_argument("--files", action="store_true", help="Every path it holds")
    show.add_argument("--json", action="store_true", help="As JSON")
    diffing = commands.add_parser("diff", help="What changed since a checkpoint", formatter_class=help_style,
                                  description="What changed since the checkpoint ID: in its folders now, or in the "
                                              "later checkpoint ID2 (changed, added, deleted).")
    diffing.add_argument("id", metavar="ID", help="the checkpoint")
    diffing.add_argument("other", metavar="ID2", nargs="?", help="a later checkpoint (default: the folders now)")
    diffing.add_argument("--path", action="append", default=[], metavar="P",
                         help="Only this path (relative to /work, or /work/..., /workspaces/NAME/..., or the host "
                              "path; repeatable)")
    diffing.add_argument("--json", action="store_true", help="As JSON (at most 2000 paths per kind and folder)")
    restoring = commands.add_parser("restore", help="Put a checkpoint back (asked first)", formatter_class=help_style,
                                    description="Put the workspaces back as they were: RUN the checkpoint its last "
                                                "start took (undo everything its agent did), ID that checkpoint. What "
                                                "changes is shown and asked first; a checkpoint of the state now is "
                                                "taken before, so the restore itself can be undone.")
    restoring.add_argument("target", metavar="RUN|ID", help="a start, or a checkpoint's id")
    restoring.add_argument("--path", action="append", default=[], metavar="P",
                           help="Only this file or folder (as checkpoint diff --path; repeatable)")
    restoring.add_argument("--dry-run", action="store_true", help="Only show what would change")
    restoring.add_argument("-y", "--yes", action="store_true", help="Confirmed: do not ask")
    restoring.add_argument("--json", action="store_true", help="What changes (and changed) as JSON")
    deleting = commands.add_parser("delete", help="Remove checkpoints", formatter_class=help_style,
                                   description="Remove checkpoints; what only they held goes from the store too.")
    deleting.add_argument("ids", metavar="ID", nargs="+", help="the checkpoints")
    deleting.add_argument("-y", "--yes", action="store_true", help="Confirmed: do not ask")
    return p


def main(argv, out=print):
    argv = list(argv)
    if argv and not argv[0].startswith("-") and argv[0] not in SUBCOMMANDS:
        argv.insert(0, "create")  # sandbox-ai checkpoint RUN
    a = parser().parse_args(argv)
    try:
        return {"create": create_main, "list": list_main, "show": show_main, "diff": diff_main,
                "restore": restore_main, "delete": delete_main}[a.command](a, out)
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai checkpoint {a.command}: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
