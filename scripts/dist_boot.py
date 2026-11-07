#!/usr/bin/env python3
"""The start of sandbox-ai's ready-made packages: standard library only, Python 3.9 or later.

  sandbox-ai.pyz      this file is its __main__.py; the package is a zip archive
  the AppImage        AppRun runs this file (usr/lib/sandbox-ai/boot.py) with the Python inside the AppImage

A package carries a tree and manifest.json: every file of the tree with its SHA-256, size and mode, every link,
the version and what to run. sandbox-ai cannot run from inside a zip archive or from the AppImage's mount (it
loads its modules, scripts, Containerfiles, presets and skills from disk, mounts sandbox/runtime.py into the
sandboxes, and sessions outlive the command that started them), so the first start copies the tree into

  ${XDG_CACHE_HOME:-~/.cache}/sandbox-ai/<kind>/<version>-<manifest digest>[-<python tag>]/

atomically (a private temporary folder, checked against the manifest, compiled, made read-only, then renamed
into place) and every start checks that copy cheaply before it runs anything from it: each file and folder must
still be the one written then (inode, size, mode and change time, recorded in .sandbox-ai-extract.json; a
change of content, mode or name changes the change time; nothing may be added beside the tree). A copy that
differs is refused, not repaired; one that lost its record (a cache cleaner's `rm -rf` stops at the read-only
folders) is set aside and extracted again. The folders are created without following links and are private
(0700); the files and folders of a copy are read-only; a cache in a tree sandboxes could write (sandbox-ai's
exposed.json) is refused, and the launcher never gives a sandbox ~/.cache/sandbox-ai (a private location). The
record certifies itself: the check finds damaged, partial or accidentally changed copies, not a writer with your
user's rights (who could rewrite the record, or the package itself, too).

Then it executes `python3 -I <copy>/sandbox/main.py ARGS` (the .pyz: with the Python that runs it; the AppImage:
with the copy's own Python), or, in the AppImage, the app (sandbox-ai-app) with PySide6 from the AppImage.
"""
import os
import sys

# Isolated from PYTHON* variables before anything else is imported (PYTHONPATH comes before the standard library
# on the import path). `./sandbox-ai.pyz` already runs `python3 -I` (the .pyz starts with a shell line); this is
# for `python3 sandbox-ai.pyz`, where Python's own start (site, sitecustomize) has run before: use -I there.
if not sys.flags.isolated and any(name.startswith("PYTHON") for name in os.environ):
    os.execv(sys.executable, [sys.executable, "-I", os.path.abspath(sys.argv[0]), *sys.argv[1:]])

if sys.version_info < (3, 9):
    sys.exit("sandbox-ai needs Python 3.9 or later; this is " + sys.version.split()[0])

import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
# Only the first start needs errno, shutil, subprocess, tempfile and time (imported there: every start counts),
# only the .pyz zipfile.

FORMAT = 1
MARKER = ".sandbox-ai-extract.json"
KINDS = ("pyz", "appimage")
CLI_NAMES = ("sandbox-ai",)  # an AppImage called (linked) by this name runs the command line, not the app
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
STALE = 3600  # seconds after which a temporary folder of an interrupted first start is removed
# The app, started like the sandbox-ai-app launcher does: the app's folder and PySide6 by absolute path, -I.
APP_STARTER = """import runpy, sys
sys.path[0:0] = [sys.argv[2], sys.argv[1]]
sys.argv = ["sandbox-ai-app", *sys.argv[3:]]
runpy.run_module("sandbox_ai_app", run_name="__main__", alter_sys=True)"""


class BootError(RuntimeError):
    """A problem the user must see; the message says what to do."""


# -- the package --

def stored_manifest(archive, limit=4 << 20):
    """manifest.json of a .pyz without the zipfile module (its import costs more than the rest of a start):
    make_pyz.py writes it stored, as the second entry after __main__.py. None when the archive is not laid out
    so (then zipfile reads it)."""
    import struct
    import zlib
    try:
        with open(archive, "rb") as stream:
            head = stream.read(limit)
    except OSError:
        return None
    offset = head.find(b"PK\x03\x04")
    for expected in ("__main__.py", "manifest.json"):
        if offset < 0 or offset + 30 > len(head):
            return None
        (signature, _version, flags, method, _time, _date, crc, size, _usize, name_length,
         extra_length) = struct.unpack_from("<IHHHHHIIIHH", head, offset)
        start = offset + 30 + name_length + extra_length
        name = head[offset + 30:offset + 30 + name_length]
        if signature != 0x04034B50 or name != expected.encode() or flags & 0x8 or start + size > len(head):
            return None
        if expected == "manifest.json":
            data = head[start:start + size]
            return data if method == 0 and zlib.crc32(data) == crc else None
        offset = start + size
    return None


class ZipSource:
    """The .pyz: manifest.json and tree/... in the zip archive this file runs from (zipfile is loaded only when
    the tree is extracted)."""

    def __init__(self, archive):
        self.path, self.zip = archive, None

    def _read(self, name):
        import zipfile
        import zlib
        bad = (zipfile.BadZipFile, zlib.error, EOFError)
        try:
            if self.zip is None:
                self.zip = zipfile.ZipFile(self.path)
            return self.zip.read(name)
        except (KeyError, *bad) as exc:  # missing, a CRC error, damaged compressed data
            raise BootError(f"{self.path} is damaged ({name}: {exc}). Download it again") from None

    def manifest(self):
        return stored_manifest(self.path) or self._read("manifest.json")

    def read(self, name):
        return self._read("tree/" + name)

    def extra(self, name):
        return None


class DirSource:
    """The AppImage: manifest.json and tree/ beside this file (usr/lib/sandbox-ai/ in the AppImage)."""

    def __init__(self, base):
        self.path = base
        problem = check_package_dir(base)
        if problem:
            raise BootError(f"{problem}; the AppImage does not run from there (extract-and-run: "
                            "TMPDIR=$(mktemp -d) ./sandbox-ai-ARCH.AppImage --appimage-extract-and-run ...)")

    def manifest(self):
        return read_file(os.path.join(self.path, "manifest.json"))

    def read(self, name):
        return read_file(os.path.join(self.path, "tree", name))

    def extra(self, name):
        path = os.path.join(self.path, name)
        return path if os.path.isdir(path) else None


def check_package_dir(path):
    """None if PATH and every folder above it belong to this user or root and nobody else can write them (a sticky
    folder of root's, like /tmp, is fine), else the problem: an AppImage extracted into a folder another user
    prepared or can change must not be copied from (AppRun checks the same before Python runs)."""
    path = os.path.realpath(path)
    while True:
        try:
            info = os.lstat(path)
        except OSError as exc:
            return f"{path}: {exc}"
        if info.st_uid not in (os.getuid(), 0):
            return f"{path} belongs to another user"
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            return f"{path} is writable by others"
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def read_file(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


def safe_name(name):
    """NAME if it is a relative path inside the tree (no '..', no absolute path, no empty part), else BootError."""
    parts = name.split("/") if isinstance(name, str) else []
    if (not parts or len(name) > 4096 or "\\" in name or "\0" in name
            or any(part in ("", ".", "..") for part in parts)):
        raise BootError(f"the package is damaged: bad path {name!r} in its manifest")
    return name


def load_manifest(raw):
    """The manifest as a dict, checked for what the start relies on."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BootError("the package is damaged: its manifest.json is not readable") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("kind") not in KINDS \
            or not isinstance(data.get("version"), str) or not VERSION.fullmatch(data["version"]):
        raise BootError("the package's manifest.json has an unknown form (a newer package? use its own Python)")
    names = set()
    for entry in data.get("files", []):
        if not (isinstance(entry, list) and len(entry) == 4 and isinstance(entry[1], str)
                and re.fullmatch(r"[0-9a-f]{64}", entry[1]) and isinstance(entry[2], int)
                and entry[3] in (0o644, 0o755)):
            raise BootError("the package is damaged: a bad file entry in its manifest")
        names.add(safe_name(entry[0]))
    for entry in data.get("links", []):
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], str)):
            raise BootError("the package is damaged: a bad link entry in its manifest")
        name, target = safe_name(entry[0]), entry[1]
        resolved = os.path.normpath(os.path.join(os.path.dirname(name), target))
        if target.startswith("/") or "\0" in target or resolved == ".." or resolved.startswith("../"):
            raise BootError(f"the package is damaged: link {name!r} points outside it")
        names.add(name)
    for key in ("home", "python"):
        if data.get(key) not in (None, ""):
            safe_name(data[key])
    if not isinstance(data.get("compile", []), list):
        raise BootError("the package is damaged: a bad compile entry in its manifest")
    for folder in data.get("compile", []):
        safe_name(folder)
    if len(names) != len(data.get("files", [])) + len(data.get("links", [])):
        raise BootError("the package is damaged: a path is listed twice in its manifest")
    return data


# -- the cache folder --

def cache_root(kind, environ=None):
    """${XDG_CACHE_HOME:-~/.cache}/sandbox-ai/KIND, created private, never through a link."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or ""
    if not os.path.isabs(base):  # the XDG rules: a relative value is ignored
        home = environ.get("HOME") or os.path.expanduser("~")
        if not os.path.isabs(home):
            raise BootError("cannot find your home folder (HOME) for the cache")
        base = os.path.join(home, ".cache")
    os.makedirs(base, mode=0o700, exist_ok=True)  # the cache itself may be a link (a home on another disk)
    info = os.stat(base)
    if info.st_uid not in (os.getuid(), 0) or (info.st_mode & 0o022 and not (info.st_uid == 0 and
                                                                             info.st_mode & stat.S_ISVTX)):
        raise BootError(f"the cache {base} is writable by others or belongs to another user; not used. "
                        "Set XDG_CACHE_HOME to a folder of yours")
    path = base
    for part, private in (("sandbox-ai", False), (kind, True)):
        path = os.path.join(path, part)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BootError(f"{path} is a link or not a folder; not used. Remove it and start again")
        if info.st_uid != os.getuid():
            raise BootError(f"{path} belongs to another user; not used")
        if private and stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
        elif info.st_mode & 0o022:  # sandbox-ai's own folder there: nobody else writes in it
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)
    return path


def exposed_trees(environ):
    """Host trees sandboxes can or could write: sandbox-ai's registry of what earlier runs exposed (workspaces, rw
    mounts, --state-dir; ${XDG_STATE_HOME:-~/.local/state}/sandbox-ai/exposed.json), read as the app reads it."""
    state = environ.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(state):
        state = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".local", "state")
    try:
        data = json.loads(read_file(os.path.join(state, "sandbox-ai", "exposed.json")).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return []
    return [os.path.realpath(p) for p in data if isinstance(p, str) and os.path.isabs(p)] if isinstance(data, list) else []


def check_outside_sandboxes(root, environ):
    """BootError if the cache folder ROOT lies in, is or holds a tree a sandbox could write: the copy's record
    proves nothing against a writer there (it would rewrite the record, too)."""
    root = os.path.realpath(root)
    for tree in exposed_trees(environ):
        if root == tree or root.startswith(tree.rstrip("/") + "/") or tree.startswith(root + "/"):
            raise BootError(f"the cache {root} lies in or holds {tree}, which a sandbox could write (sandbox-ai's "
                            "exposed.json); nothing run from there. Set XDG_CACHE_HOME to a folder no sandbox gets")


def copy_name(manifest, digest):
    name = f"{manifest['version']}-{digest[:16]}"
    if manifest.get("compile"):  # compiled here: one copy per Python version
        name += "-" + (sys.implementation.cache_tag or "python")
    return name


def entry_record(info):
    kind = "d" if stat.S_ISDIR(info.st_mode) else "l" if stat.S_ISLNK(info.st_mode) else \
        "f" if stat.S_ISREG(info.st_mode) else "?"
    return [kind, info.st_ino, info.st_size if kind != "d" else 0, info.st_ctime_ns, stat.S_IMODE(info.st_mode)]


def walk(top):
    """Relative paths of everything below TOP (folders first), never following links; the marker left out."""
    found = []
    for folder, dirs, files in os.walk(top, followlinks=False):
        rel = os.path.relpath(folder, top)
        for name in sorted(dirs) + sorted(files):
            path = name if rel == "." else f"{rel}/{name}"
            if path != MARKER:
                found.append(path)
    return found


def record_readable(top):
    """True if TOP's record is there and is JSON (whatever it says)."""
    try:
        json.loads(read_file(os.path.join(top, MARKER)).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return False
    return True


def verify(top, digest, manifest):
    """None if the copy at TOP is still the one written for DIGEST, else what differs."""
    try:
        info = os.lstat(top)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return f"{top} is a link, not a folder or not yours"
        marker = json.loads(read_file(os.path.join(top, MARKER)).decode("utf-8"))
    except FileNotFoundError:
        return f"{os.path.join(top, MARKER)} is missing"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"{os.path.join(top, MARKER)} is not readable ({exc})"
    if not isinstance(marker, dict) or marker.get("digest") != digest or not isinstance(marker.get("entries"), dict):
        return f"{os.path.join(top, MARKER)} belongs to another package"
    entries = marker["entries"]
    expected = {name for name in entries if "/" not in name} | {MARKER}
    try:
        present = set(os.listdir(top))
    except OSError as exc:
        return f"{top} is not readable ({exc})"
    if present != expected:
        return f"{', '.join(sorted(present ^ expected)[:3])} added or removed at the top"
    for name in [entry[0] for entry in manifest.get("files", [])] + [entry[0] for entry in manifest.get("links", [])]:
        if name not in entries:
            return f"{name} was not recorded"
    for name, recorded in entries.items():
        try:
            now = entry_record(os.lstat(os.path.join(top, name)))
        except OSError:
            return f"{name} is missing"
        if now != recorded:
            return f"{name} was changed"
    return None


def make_private_tree_writable(top):
    """u+w on every folder below TOP (links are never followed), so that it can be removed."""
    for folder, dirs, _files in os.walk(top, followlinks=False):
        for name in dirs:
            path = os.path.join(folder, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)
    if not os.path.islink(top):
        os.chmod(top, 0o700)


def remove_tree(top):
    import shutil
    try:
        make_private_tree_writable(top)
        shutil.rmtree(top)
    except OSError:
        pass


def remove_stale(root, name, now=None):
    """Temporary folders of interrupted first starts (older than STALE)."""
    import time
    now = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except OSError:
        return
    for entry in names:
        if entry.startswith(f".{name}.") and entry.endswith(".tmp"):
            path = os.path.join(root, entry)
            try:
                if now - os.lstat(path).st_mtime > STALE:
                    remove_tree(path)
            except OSError:
                pass


def write_file(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())  # on disk before the copy is renamed into place
    os.chmod(path, mode)


def sync_folder(path):
    """fsync a folder (its entries: a rename into it, the files created in it)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def make_dirs(top, rel, made):
    """The folders of REL below TOP, created by this start (so never links)."""
    parts = rel.split("/")[:-1]
    for index in range(1, len(parts) + 1):
        sub = "/".join(parts[:index])
        if sub not in made:
            os.mkdir(os.path.join(top, sub), 0o700)
            made.add(sub)


def extract(source, manifest, digest, tmp, final):
    """The tree into the fresh folder TMP, each file checked against the manifest; compiled; read-only; recorded."""
    made = set()
    for name, sha256, size, mode in manifest.get("files", []):
        data = source.read(name)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise BootError(f"the package is damaged: {name} does not match its manifest. Download it again")
        make_dirs(tmp, name, made)
        write_file(os.path.join(tmp, name), data, 0o600)
    for name, target in manifest.get("links", []):  # after the files: no file is written through a link
        make_dirs(tmp, name, made)
        os.symlink(target, os.path.join(tmp, name))
    folders = [os.path.join(tmp, part) for part in manifest.get("compile", []) if os.path.isdir(os.path.join(tmp, part))]
    if folders:
        import subprocess
        # The precompiled modules name the final place (tracebacks); a module that does not compile with this
        # Python is left to fail when it is loaded, as in an installation.
        # In parallel first; what that leaves (its worker start can fail: no /dev/shm, a TMPDIR too long for
        # the forkserver's socket) one by one.
        for workers in ("0", "1"):
            done = subprocess.run([sys.executable, "-I", "-m", "compileall", "-q", "-j", workers, "-x",
                                   r"[/\\]tests[/\\]", "-s", tmp, "-p", final, *folders],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if done.returncode == 0:
                break
    modes = {entry[0]: entry[3] for entry in manifest.get("files", [])}
    paths = walk(tmp)
    for rel in paths:  # files read-only (the manifest's executables stay executable)
        path = os.path.join(tmp, rel)
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            if rel not in modes:  # compileall's .pyc: on disk too (the manifest's files were synced when written)
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.chmod(path, 0o555 if modes.get(rel, 0o644) & 0o111 else 0o444)
    for rel in reversed(paths):  # then the folders, deepest first
        path = os.path.join(tmp, rel)
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.chmod(path, 0o500)
    for rel in [""] + [rel for rel in paths if stat.S_ISDIR(os.lstat(os.path.join(tmp, rel)).st_mode)]:
        sync_folder(os.path.join(tmp, rel))  # the folders' entries
    entries = {rel: entry_record(os.lstat(os.path.join(tmp, rel))) for rel in paths}
    write_file(os.path.join(tmp, MARKER), json.dumps({"digest": digest, "entries": entries}, sort_keys=True,
                                                    separators=(",", ":")).encode(), 0o400)


def ensure_copy(source, manifest, digest, environ=None):
    """The folder of the verified copy for this package: made on the first start, checked on every start."""
    root = cache_root(manifest["kind"], environ)
    check_outside_sandboxes(root, environ)
    name = copy_name(manifest, digest)
    final = os.path.join(root, name)
    if os.path.lexists(final):
        problem = verify(final, digest, manifest)
        if problem is None:
            remove_unused_copies(root, f"{manifest['version']}-{digest[:16]}")
            return final
        if os.path.islink(final) or record_readable(final):
            raise tampered(final, problem)
        # Without a readable record the copy is incomplete, not changed: `rm -rf ~/.cache` stops at the read-only
        # folders after removing the record; a crash or a full disk cuts it short. Set aside (a rename in the same folder) and extracted again from the
        # package, which is checked against the manifest anyway.
        import time
        aside = os.path.join(root, f".{name}.{os.getpid()}-{time.time_ns()}.tmp")
        try:
            os.rename(final, aside)
            remove_tree(aside)
        except FileNotFoundError:  # another start set it aside first
            pass
        print(f"sandbox-ai: {final} was incomplete ({problem}); extracting it again", file=sys.stderr)
    import errno
    import tempfile
    remove_stale(root, name)
    tmp = tempfile.mkdtemp(prefix=f".{name}.", suffix=".tmp", dir=root)
    try:
        extract(source, manifest, digest, tmp, final)
        try:
            os.rename(tmp, final)
            os.chmod(final, 0o500)  # nothing is added beside the tree (verify checks the top, too)
            sync_folder(final)
            sync_folder(root)
        except OSError as exc:  # another start was first: use its copy
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) and not os.path.lexists(final):
                raise
    finally:
        if os.path.lexists(tmp):
            remove_tree(tmp)
    problem = verify(final, digest, manifest)
    if problem is not None:
        raise tampered(final, problem)
    remove_unused_copies(root, f"{manifest['version']}-{digest[:16]}")
    return final


def in_use(paths, proc="/proc"):
    """The folders of PATHS some process runs from: its program, current folder or command line names one."""
    prefixes = [path.rstrip("/") + "/" for path in paths]
    busy = set()
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        names = []
        for link in ("exe", "cwd"):
            try:
                names.append(os.readlink(os.path.join(proc, pid, link)))
            except OSError:
                pass
        try:
            names += read_file(os.path.join(proc, pid, "cmdline")).decode("utf-8", "replace").split("\0")
        except OSError:
            pass
        for name in names:
            for path, prefix in zip(paths, prefixes):
                if name == path or prefix in name:
                    busy.add(path)
    return busy


def remove_unused_copies(root, package, proc="/proc"):
    """Copies of other versions in ROOT that nothing runs from any more (about 11 MB per .pyz, 68 MB per AppImage):
    set aside and removed. This package's copies (PACKAGE: VERSION-DIGEST) for other Pythons stay. Only where /proc
    shows what runs (Linux); elsewhere they stay until removed by hand."""
    if not os.path.isdir(os.path.join(proc, "self")):
        return
    try:
        others = [os.path.join(root, entry) for entry in os.listdir(root)
                  if not entry.startswith(".") and not entry.startswith(package)]
    except OSError:
        return
    if not others:
        return
    import time
    busy = in_use(others, proc)
    for path in others:
        if path in busy or os.path.islink(path):
            continue
        aside = os.path.join(root, f".{os.path.basename(path)}.{os.getpid()}-{time.time_ns()}.tmp")
        try:
            os.rename(path, aside)
        except OSError:
            continue
        remove_tree(aside)


def tampered(final, problem):
    return BootError(f"the extracted copy in {final} differs from what was written there ({problem}); "
                     f"refused, nothing run. If you did not change it, something else did: check, then remove it "
                     f"with: chmod -R u+w '{final}' && rm -rf '{final}' (the next start extracts it again)")


# -- what runs --

def name_package(environ, source, top):
    """SANDBOX_AI_PACKAGE (the package file: the .pyz, $APPIMAGE, else the AppDir) and SANDBOX_AI_PACKAGE_COPY (the
    copy): the launcher refuses workspaces, rw mounts and --state-dir that hold either. Set here only; whatever the
    environment had is replaced (the launcher checks both again)."""
    if isinstance(source, ZipSource):
        package = os.path.realpath(source.path)
    elif environ.get("APPIMAGE") and os.path.isfile(environ["APPIMAGE"]):
        package = os.path.realpath(environ["APPIMAGE"])
    else:
        package = os.path.realpath(os.path.join(source.path, "..", "..", ".."))  # AppDir/usr/lib/sandbox-ai
    environ["SANDBOX_AI_PACKAGE"] = package
    environ["SANDBOX_AI_PACKAGE_COPY"] = os.path.realpath(top)


def command_line(manifest, top, args, source, environ, argv0):
    """(executable, argv) to execute: the command line, or in the AppImage the app."""
    home = os.path.join(top, manifest["home"]) if manifest.get("home") else top
    python = os.path.join(top, manifest["python"]) if manifest.get("python") else sys.executable
    main_py = os.path.join(home, "sandbox", "main.py")
    if manifest["kind"] == "appimage":
        cli = os.path.basename(argv0 or "") in CLI_NAMES
        if not cli and args[:1] == ["cli"]:
            cli, args = True, args[1:]
        elif not cli and args[:1] == ["app"]:
            args = args[1:]
        if not cli:
            pyside = source.extra("pyside")
            if pyside is None:
                raise BootError("this AppImage carries no PySide6; run the command line with: AppImage cli ARGS")
            environ.setdefault("SANDBOX_AI_HOME", home)  # the sandbox-ai inside (yours, if you set one)
            if environ.get("APPIMAGE"):  # a start at login runs the AppImage (PySide6 is inside), not the copy
                environ["SANDBOX_AI_APP_PACKAGE_LAUNCHER"] = environ["APPIMAGE"]
            app = os.path.join(home, "projects", "sandbox-ai-app")
            return python, [python, "-I", "-c", APP_STARTER, pyside, app, *args]
    return python, [python, "-I", main_py, *args]


def package_source():
    """The package this file belongs to: the zip it runs from, else the folder it lies in."""
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    if archive:
        return ZipSource(archive)
    return DirSource(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None, environ=None, execv=os.execve):
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ.copy() if environ is None else environ
    try:
        source = package_source()
        raw = source.manifest()
        manifest = load_manifest(raw)
        digest = hashlib.sha256(raw).hexdigest()
        top = ensure_copy(source, manifest, digest, environ)
        name_package(environ, source, top)
        executable, command = command_line(manifest, top, args, source, environ, environ.get("ARGV0"))
    except BootError as exc:
        print(f"sandbox-ai: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"sandbox-ai: cannot start from {getattr(globals().get('__loader__'), 'archive', None) or __file__}: "
              f"{exc}", file=sys.stderr)
        return 1
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        execv(executable, command, environ)
    except OSError as exc:
        print(f"sandbox-ai: cannot run {executable}: {exc}" + (
            " (a cache on a file system mounted noexec? set XDG_CACHE_HOME to another folder)"
            if exc.errno == 13 else ""), file=sys.stderr)
        return 1
    return 0  # only when EXECV is a stand-in (tests)


if __name__ == "__main__":
    sys.exit(main())
