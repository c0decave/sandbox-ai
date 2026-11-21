#!/usr/bin/env python3
"""What sandbox-ai keeps on this computer, how big each part is, and the compaction of its logs.

  sandbox-ai gc --usage [--json]     the size of every part (starts and their HOMEs, the logs of each project, MCP
                                     servers, histories, launcher.log, the data, cache and configuration folders,
                                     Podman's images and volumes), the biggest items, and what gc would free

Where sandbox-ai writes (XDG_STATE_HOME, default ~/.local/state):
  sandbox-ai/sessions/ID/        a start: session.json, supervisor.log, and home/ - the kept HOME the agent fills
                                 (its transcripts, caches, scratch trees); kept starts never expire on their own
  sandbox-ai/logs/PROJECT-HASH/  the logs of a project's runs: per run connections-TIME-RUN.jsonl (the proxy),
                                 llm-traffic-TIME-RUN.jsonl[.N] and monitor[-1m|-1h]-TIME-RUN.jsonl (the system
                                 monitor's series); shared by every run: activity.jsonl, files.jsonl,
                                 dns.jsonl, egress-denied.jsonl, processes.jsonl, safeguards.jsonl ...
  sandbox-ai/mcp/NAME/           an MCP server: supervisor.log, mcp-calls.jsonl
  sandbox-ai/history/NAME.jsonl  what happened to each name
  sandbox-ai/checkpoints/PROJECT-HASH/
                                 the workspace checkpoints of a project (checkpoints.py: content once, manifests)
  sandbox-ai/launcher.log[.N]    rotated at 2 MiB
and XDG_DATA_HOME/sandbox-ai (logins, kits, MCP root file systems, VPN profiles, videos), XDG_CACHE_HOME/sandbox-ai,
XDG_CONFIG_HOME/sandbox-ai, the run directories (XDG_RUNTIME_DIR) and Podman's storage.

Compaction (sandbox-ai gc, and once a day in the background after a start: auto()): the logs of runs that ended
are compressed (NAME.gz beside where they were; sandbox-ai's readers read them as they are), and a shared log of a
project larger than SHARED_LIMIT keeps its newest SHARED_KEEP bytes - what every reader reads of it - while the
older part goes compressed into NAME.TIME.gz (at most ARCHIVE_LIMIT of those per log, the oldest dropped). Nothing
of a running start is touched: a log directory a run uses, and every file written within QUIET seconds, wait.

Sizes are what the files take on disk (as du), measured with os.scandir without reading any file, never following a
link or crossing into another file system, within a time and entry budget ("partial" when it ran out); a folder that
cannot be read is counted ("unreadable") and passed. Standard library only, Python 3.9.
"""
import argparse
import collections
import contextlib
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import zlib

HERE = Path(__file__).resolve().parent

SCAN_SECONDS = 120          # a measurement stops looking after this long (the sizes are then "partial")
SCAN_ENTRIES = 5_000_000    # nor does it look at more files and folders than this
DEPTH_LIMIT = 200           # folders deeper than this are not looked into
TOP = 10                    # the biggest items listed per part
HOME_TOP = 8                # the biggest folders of a HOME listed (each with its own biggest three)
# Caches in a HOME that the tools fill again on their own (downloads and build output only): gc --caches empties
# them in the stopped kept starts. Never transcripts, settings, logins or the agent's own work.
CACHES = (".cache/pip", ".cache/uv", ".npm/_cacache", ".cache/go-build", ".cache/yarn", ".bun/install/cache",
          ".cache/pnpm", ".cache/node-gyp", ".cache/puppeteer-tmp")
# monitor-*: the system monitor's series of a run (monitor.py: samples, minutes, hours; trimmed while it runs).
PER_RUN = re.compile(r"(connections|llm-traffic|spend|monitor-1h|monitor-1m|monitor)-(\d{8}-\d{6})-([A-Za-z0-9._-]{1,128}?)"
                     r"\.jsonl(\.\d{1,3})?(\.gz)?")
SHARED = ("activity.jsonl", "files.jsonl", "dns.jsonl", "egress-denied.jsonl", "processes.jsonl", "tools.jsonl",
          "mcp-calls.jsonl", "safeguards.jsonl", "canaries.jsonl", "devices.jsonl", "workspaces.jsonl", "vpn.jsonl",
          "memory.jsonl")
ARCHIVE = re.compile(r"(.+\.(?:jsonl|log))\.(\d{8}-\d{6})\.gz")
# An older part a run rotated (runtime.rotate_shared_log, activity.append, rotate) that is not compressed yet.
ARCHIVE_RAW = re.compile(r"(.+\.(?:jsonl|log))\.(\d{8}-\d{6})")
SHARED_LIMIT = 96 << 20     # a shared log larger than this is split
SHARED_KEEP = 32 << 20      # what it keeps of its newest lines (the readers read the newest 32 MiB of a log)
ARCHIVE_LIMIT = 1 << 30     # compressed older parts kept per shared log (runtime.LOG_ARCHIVE_LIMIT); beyond, the oldest go
QUIET = 3600                # seconds since a file was last written before it is compressed or split
LEVEL = 3                   # gzip's level: JSON lines shrink to a quarter or less, at about 100 MB/s
CHUNK = 1 << 20
# An MCP server's supervisor.log and mcp-calls.jsonl (state/mcp/NAME/: mcp.split_log writes them while it runs, and
# rotates them at this size; gc those of a server whose log reader does not run, MCP_LOCK not held), the older parts
# compressed beside them, at most MCP_ARCHIVE_LIMIT of them per log; decided approvals go after APPROVALS_DAYS.
MCP_LOGS = ("supervisor.log", "mcp-calls.jsonl")
MCP_LOG_PART = int(os.environ.get("SANDBOX_AI_MCP_LOG_PART") or 32 << 20)
MCP_ARCHIVE_LIMIT = 256 << 20
MCP_LOCK = ".log.lock"
APPROVALS_DAYS = 30
# The age limit of the logs (log_days in your defaults; 0: never): sandbox-ai gc and its daily pass remove the log files
# nothing was written into for longer (old_logs) - the connection log's bodies, file and process records among them.
# Without log_days, from LOG_DAYS_GRACE days after the first pass found such logs on (LOG_DAYS_FILE).
LOG_DAYS = 30
LOG_DAYS_GRACE = 14
LOG_DAYS_FILE = "log-days.json"
ROOTFS_DAYS = 7             # an MCP root file system no server names is removed once it is this old (a new install)
AUTO_EVERY = 86400         # the automatic pass runs at most once a day
AUTO_SECONDS = 600          # and stops starting new work after this long
AUTO_STAMP = "compact.json"  # in the state directory: when it ran last and what it did
AUTO_VARIABLE = "SANDBOX_AI_AUTO_COMPACT"  # "0": no automatic pass
PODMAN_SECONDS = 30


def state_root(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def roots(env=None):
    """{part: folder} of every place sandbox-ai keeps data (the run directories: under XDG_RUNTIME_DIR when set)."""
    env = os.environ if env is None else env
    home = Path.home()
    found = {"state": state_root(env),
             "data": Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "sandbox-ai",
             "cache": Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "sandbox-ai",
             "config": Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "sandbox-ai"}
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime:
        found["runtime"] = Path(runtime) / "sandbox-ai"
    return found


def human(size):
    """SIZE in bytes as 1.2 GiB, 340 MiB, 12 KiB or 5 B."""
    size = int(size or 0)
    for unit, shift in (("TiB", 40), ("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            value = size / (1 << shift)
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{size} B"


def mount_of(path, mountinfo="/proc/self/mountinfo"):
    """(mount point, file system type) of the file system PATH lies on (Linux: the longest mount point of mountinfo
    that holds it), else (None, None)."""
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as stream:
            lines = stream.read().splitlines()
        real = os.path.realpath(path)
    except OSError:
        return None, None
    best = (None, None)
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 10 or "-" not in fields[6:]:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        kind = fields[fields.index("-", 6) + 1]
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best[0] or ""):
            best = (point, kind)
    return best


def space(path):
    """{"path", "mount", "type", "free", "total", "files_free", "files"} of the file system PATH (or the nearest folder
    above it that is there) lies on - bytes an ordinary user can still write, inodes that can still be made (None
    where the file system counts none, as btrfs) - else None."""
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    mount, kind = mount_of(path)
    return {"path": str(path), "mount": mount, "type": kind, "free": info.f_bavail * info.f_frsize,
            "total": info.f_blocks * info.f_frsize, "files_free": info.f_favail if info.f_files else None,
            "files": info.f_files or None}


def space_text(found):
    """FOUND (space) in words: "PATH (tmpfs at /run/user/1000): 12 MiB of 1.6 GiB free, 120 of 400000 inodes free"."""
    where = f"{found['type']} at {found['mount']}" if found.get("type") and found.get("mount") else \
        found.get("type") or "its file system"
    inodes = (f", {found['files_free']} of {found['files']} inodes free" if found.get("files_free") is not None
              else ", inodes not counted there")
    return f"{found['path']} ({where}): {human(found['free'])} of {human(found['total'])} free{inodes}"


def space_short(paths, need=0, files=0):
    """[(space, text)] of the file systems behind PATHS (each once) with less than NEED bytes or FILES inodes left."""
    short, seen = [], set()
    for path in paths:
        found = space(path)
        if found is None:
            continue
        key = (found["mount"], found["total"], found["files"])
        if key in seen:
            continue
        seen.add(key)
        if found["free"] < need or files and found["files_free"] is not None and found["files_free"] < files:
            short.append((found, space_text(found)))
    return short


# -- measuring ----------------------------------------------------------------------------------------------------

def mount_points(path="/proc/self/mountinfo"):
    """The mount points this process sees (Linux: mountinfo), or None where that cannot be read (macOS)."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return None
    points = set()
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
    return points


class Scan:
    """One measurement's budget (time and entries) and what it could not see."""

    def __init__(self, seconds=SCAN_SECONDS, entries=SCAN_ENTRIES, clock=time.monotonic):
        self.clock, self.deadline, self.left = clock, clock() + seconds, entries
        self.partial, self.unreadable, self.seen = False, 0, set()
        # Mount points are never looked at (not even lstat): a remote folder whose host is gone holds every look
        # into it for as long as the host stays away (an NFS mount is hard by default), past any time budget.
        self.mounts = mount_points() or set()

    def spent(self):
        if self.left <= 0 or self.clock() > self.deadline:
            self.partial = True
            return True
        return False

    @staticmethod
    def used(info):
        """What a file takes on disk (its blocks; its length where there are none)."""
        blocks = getattr(info, "st_blocks", None)
        return blocks * 512 if blocks is not None else info.st_size

    def size(self, path, depth=0, device=None, keep=0, into=None, prefix=""):
        """Bytes under PATH (a folder, a file; a link counts as itself). KEEP > 0: the size of each folder down to
        KEEP levels below PATH goes into INTO ({relative path: bytes})."""
        if depth > 0 and os.path.abspath(path) in self.mounts:
            self.partial = True  # (another file system, never counted: not looked at either)
            return 0
        try:
            info = os.lstat(path)
        except OSError:
            self.unreadable += 1
            return 0
        device = info.st_dev if device is None else device
        if not stat.S_ISDIR(info.st_mode):
            return self.file(info)
        if info.st_dev != device or depth > DEPTH_LIMIT:
            self.partial = self.partial or depth > DEPTH_LIMIT
            return 0
        total = self.used(info)
        try:
            listing = os.scandir(path)
        except OSError:
            self.unreadable += 1
            return total
        with listing:
            for entry in listing:
                if self.spent():
                    break
                self.left -= 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = f"{prefix}{entry.name}"
                        found = self.size(entry.path, depth + 1, device, keep - 1, into, name + "/")
                        if keep > 0 and into is not None:
                            into[name] = found
                        total += found
                    else:
                        total += self.file(entry.stat(follow_symlinks=False))
                except OSError:
                    self.unreadable += 1
        return total

    def file(self, info):
        if info.st_nlink > 1:  # a hard link: counted once
            key = (info.st_dev, info.st_ino)
            if key in self.seen:
                return 0
            self.seen.add(key)
        return self.used(info)


def children(scan, folder, skip=()):
    """[(name, bytes, is a folder)] of what FOLDER holds (but SKIP), the biggest first ([] when it cannot be read)."""
    found = []
    try:
        with os.scandir(folder) as listing:
            entries = list(listing)
    except OSError:
        return found
    for entry in entries:
        if entry.name in skip:
            continue
        if scan.spent():
            break
        try:
            folder_ = entry.is_dir(follow_symlinks=False)
        except OSError:
            folder_ = False
        found.append((entry.name, scan.size(entry.path), folder_))
    return sorted(found, key=lambda item: (-item[1], item[0]))


def newest_write(folder):
    """The time a file directly in FOLDER was last written (0: none)."""
    newest = 0
    with contextlib.suppress(OSError), os.scandir(folder) as listing:
        for entry in listing:
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False):
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)) if when else None


def log_kind(name):
    """What a file of a log directory is: connections, llm-traffic (with " (compressed)"), a shared log's name,
    "older parts" (the compressed parts split off a shared log), or "other"."""
    match = PER_RUN.fullmatch(name)
    if match:
        return match.group(1) + (" (compressed)" if match.group(5) else "")
    if ARCHIVE.fullmatch(name):
        return "older parts (compressed)"
    if ARCHIVE_RAW.fullmatch(name):
        return "older parts"
    return name if name in SHARED else "other"


# What gzip (LEVEL) makes of each kind of log, measured on a sample of SAMPLE bytes of its biggest file (gc --usage); a
# kind whose sample cannot be read counts with RATIO_FALLBACK. Measured (review 2): connection logs 2-3x (with downloads
# and answers that do not shrink), files.jsonl 10-16x, processes.jsonl 8-9x.
SAMPLE = 1 << 20
RATIO_FALLBACK = 3.0


def log_dir_usage(scan, folder, live_runs=(), in_use=True, aged_before=None):
    """The parts of one log directory: {"bytes", "kinds": {kind: {"bytes", "files"}}, "runs", "last_written",
    "compressible": bytes of ended runs' logs and of older parts not yet compressed, "splittable": bytes of shared logs
    over the limit (only in a folder no run logs into: IN_USE False - gc never splits the others), "compress_kinds":
    {kind: {"bytes", "sample": the biggest such file}} of both}. A file last written before AGED_BEFORE (seconds:
    removed by log_days now) is never counted as compressible."""
    kinds = collections.defaultdict(lambda: {"bytes": 0, "files": 0})
    runs, compressible, splittable, total = set(), 0, 0, 0
    compress_kinds = {}

    def candidate(kind, path, size, counted):
        entry = compress_kinds.setdefault(kind, {"bytes": 0, "sample": None, "sample_bytes": 0})
        entry["bytes"] += counted
        if size > entry["sample_bytes"]:
            entry["sample"], entry["sample_bytes"] = path, size
    try:
        with os.scandir(folder) as listing:
            entries = list(listing)
    except OSError:
        scan.unreadable += 1
        entries = []
    for entry in entries:
        if scan.spent():
            break
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            scan.unreadable += 1
            continue
        size = scan.size(entry.path) if stat.S_ISDIR(info.st_mode) else scan.file(info)
        total += size
        kind = log_kind(entry.name) if stat.S_ISREG(info.st_mode) else "other"
        kinds[kind]["bytes"] += size
        kinds[kind]["files"] += 1
        match = PER_RUN.fullmatch(entry.name)
        aged = aged_before is not None and info.st_mtime < aged_before
        if match:
            runs.add(match.group(3))
            if not match.group(5) and match.group(3) not in live_runs and stat.S_ISREG(info.st_mode) and not aged:
                compressible += size
                candidate(match.group(1), entry.path, info.st_size, size)
        elif ARCHIVE_RAW.fullmatch(entry.name) and stat.S_ISREG(info.st_mode) and not aged:
            compressible += size  # an older part a run rotated that is not compressed yet (gc compresses it)
            candidate("older parts", entry.path, info.st_size, size)
        elif entry.name in SHARED and info.st_size > SHARED_LIMIT and not in_use and not aged:
            splittable += max(0, size - SHARED_KEEP)
            candidate(entry.name, entry.path, info.st_size, max(0, size - SHARED_KEEP))
    return {"path": str(folder), "name": Path(folder).name, "bytes": total,
            "kinds": dict(sorted(kinds.items(), key=lambda item: -item[1]["bytes"])), "runs": len(runs),
            "last_written": iso(newest_write(folder)), "compressible": compressible, "splittable": splittable,
            "compress_kinds": compress_kinds}


def compression_ratio(path, sample=SAMPLE, level=LEVEL):
    """How many times smaller gzip (LEVEL) makes the log PATH, measured on SAMPLE bytes from its middle; None when it
    cannot be read (or is empty)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        data = os.pread(fd, sample, max(0, (size - sample) // 2))
    except OSError:
        return None
    finally:
        os.close(fd)
    if not data:
        return None
    return round(len(data) / max(1, len(gzip.compress(data, level))), 2)


# What is in a kept HOME, by kind (gc --usage --detail): {path relative to the HOME: (kind, what)}. A path with known
# paths below it (.cache, .claude, .cargo) is looked into; the rest of it is its own. Files and folders no path names
# are the agent's work ("other"), except the build output and temporary folders found by name anywhere in them.
HOME_KINDS = {"caches": "download caches", "toolchains": "toolchains", "agent": "agent state",
              "build": "build output and temp", "other": "other (the agent's work)"}
HOME_PATHS = {
    ".cache/pip": ("caches", "pip"), ".cache/uv": ("caches", "uv"), ".npm/_cacache": ("caches", "npm"),
    ".npm/_npx": ("caches", "npx"), ".npm": ("caches", "npm (other)"), ".cache/yarn": ("caches", "Yarn"),
    ".yarn/berry/cache": ("caches", "Yarn Berry"), ".cache/pnpm": ("caches", "pnpm"),
    ".local/share/pnpm/store": ("caches", "pnpm store"), ".cargo/registry": ("caches", "Cargo registry"),
    ".cargo/git": ("caches", "Cargo git checkouts"), "go/pkg/mod": ("caches", "Go modules"),
    ".cache/go-build": ("caches", "Go build cache"), ".bun/install/cache": ("caches", "Bun"),
    ".cache/huggingface": ("caches", "Hugging Face"), ".cache/ms-playwright": ("caches", "Playwright browsers"),
    ".cache/puppeteer": ("caches", "Puppeteer browsers"), ".cache/puppeteer-tmp": ("caches", "Puppeteer temp"),
    ".cache/node-gyp": ("caches", "node-gyp"), ".gradle/caches": ("caches", "Gradle"),
    ".m2/repository": ("caches", "Maven"), ".cache/bazel": ("caches", "Bazel"),
    ".cache/pre-commit": ("caches", "pre-commit"), ".cache/torch": ("caches", "PyTorch"),
    ".nuget/packages": ("caches", "NuGet"), ".cache/deno": ("caches", "Deno"),
    ".cache/composer": ("caches", "Composer"),
    ".cache/Cypress": ("caches", "Cypress"), ".cache/electron": ("caches", "Electron"),
    ".cache/selenium": ("caches", "Selenium"), ".cache/pypoetry": ("caches", "Poetry"),
    ".cache/typescript": ("caches", "TypeScript"), ".cache": ("caches", ".cache (other)"),
    ".rustup": ("toolchains", "rustup (Rust)"), ".cargo": ("toolchains", "Cargo (bin, other)"),
    ".nvm": ("toolchains", "nvm (Node.js)"), ".sdkman": ("toolchains", "SDKMAN"), ".pyenv": ("toolchains", "pyenv"),
    ".local/share/uv": ("toolchains", "uv (Pythons, tools)"), ".local/share/mise": ("toolchains", "mise"),
    ".asdf": ("toolchains", "asdf"), ".volta": ("toolchains", "Volta"), ".bun": ("toolchains", "Bun"),
    ".deno": ("toolchains", "Deno"), "go": ("toolchains", "Go (GOPATH)"), "sdk": ("toolchains", "Go SDKs"),
    ".local/share/fnm": ("toolchains", "fnm"), ".juliaup": ("toolchains", "juliaup"),
    ".julia": ("toolchains", "Julia"), ".ghcup": ("toolchains", "GHCup"), ".dotnet": ("toolchains", ".NET"),
    ".local/share/pnpm": ("toolchains", "pnpm"), ".local/lib": ("toolchains", "Python packages (pip --user)"),
    ".gradle": ("toolchains", "Gradle (other)"), ".m2": ("toolchains", "Maven (other)"),
    ".claude/projects": ("agent", "Claude transcripts"), ".claude/todos": ("agent", "Claude todos"),
    ".claude/shell-snapshots": ("agent", "Claude shell snapshots"),
    ".claude/file-history": ("agent", "Claude file history"), ".claude": ("agent", "Claude (other)"),
    ".codex/sessions": ("agent", "Codex transcripts"), ".codex/log": ("agent", "Codex logs"),
    ".codex": ("agent", "Codex (other)"), ".local/share/opencode": ("agent", "OpenCode"),
    ".hermes": ("agent", "Hermes"), ".qwen": ("agent", "Qwen Code"), ".config/goose": ("agent", "Goose"),
    ".local/share/goose": ("agent", "Goose"), ".local/state/goose": ("agent", "Goose"),
    ".openhands": ("agent", "OpenHands"), ".nanobot": ("agent", "nanobot"), ".letta": ("agent", "Letta"),
    ".pi": ("agent", "Pi"), ".gemini": ("agent", "Gemini CLI"),
}
HOME_PREFIXES = {"/".join(path.split("/")[:end]) for path in HOME_PATHS for end in range(1, path.count("/") + 1)}
# Build output and temporary folders, by name, wherever they are (a known path's folders excepted): (kind, what, a
# file one of them must hold to be it - None: the name says it).
BUILD_NAMES = {"node_modules": ("node_modules", None), ".venv": ("Python virtualenv", "pyvenv.cfg"),
               "venv": ("Python virtualenv", "pyvenv.cfg"), "target": ("Rust target", "CACHEDIR.TAG"),
               "__pycache__": ("Python bytecode", None), ".pytest_cache": ("pytest cache", None),
               ".mypy_cache": ("mypy cache", None), ".ruff_cache": ("Ruff cache", None), ".tox": ("tox", None),
               ".nox": ("nox", None), ".next": ("Next.js build", None), ".turbo": ("Turborepo cache", None),
               ".parcel-cache": ("Parcel cache", None), ".svelte-kit": ("SvelteKit build", None),
               ".gradle": ("Gradle build", None), "tmp": ("temp", None), ".tmp": ("temp", None),
               "temp": ("temp", None)}


def build_kind(path, name, depth):
    """What the folder PATH (NAME, DEPTH below the HOME) is as build output or temp, or None. A temp folder counts only
    in the HOME's first two levels; .gradle only below the HOME (at its top it is Gradle's own)."""
    found = BUILD_NAMES.get(name)
    if found is None or (found[0] == "temp" and depth > 2) or (name == ".gradle" and depth == 1):
        return None
    what, mark = found
    if mark is not None:
        marks = (mark, ".rustc_info.json") if name == "target" else (mark,)
        if not any(os.path.lexists(os.path.join(path, one)) for one in marks):
            return None
    return what


def home_detail(scan, home, top=HOME_TOP):
    """What a HOME holds, by kind (HOME_KINDS), in one walk that never follows a link or crosses into another file
    system: {"bytes", "kinds": {kind: bytes}, "items": [{"kind", "what", "path", "bytes"}] the biggest first,
    "other": [{"path", "bytes"}] what no kind takes, by its folder in the HOME, the biggest first; "biggest" and
    "caches" as home_usage says them}, each "other" with its own biggest three. Every byte is counted once: the kinds
    add up to "bytes"."""
    home = Path(home)
    sizes, claimed, other, below = {}, collections.Counter(), collections.Counter(), collections.Counter()
    empty = {"bytes": 0, "kinds": {}, "items": [], "other": [], "biggest": [], "caches": {}}
    try:
        info = os.lstat(home)
    except OSError:
        scan.unreadable += 1
        return empty
    if not stat.S_ISDIR(info.st_mode):
        return empty
    device = info.st_dev

    def owned(owner, relative, size):
        """SIZE to OWNER, else to the HOME's folder RELATIVE is in ("": directly in the HOME)."""
        if owner is not None:
            claimed[owner] += size
        else:
            parts = relative.split("/") if relative else []
            other[parts[0] if parts else "(files directly in HOME)"] += size
            if len(parts) > 2:  # (a folder's own files: in it, not in a folder below it)
                below["/".join(parts[:2])] += size

    def walk(path, rel, depth, owner, info):
        total = scan.used(info)
        owned(owner, rel, total)
        if depth > DEPTH_LIMIT:
            scan.partial = True
            return total
        try:
            listing = os.scandir(path)
        except OSError:
            scan.unreadable += 1
            return total
        with listing:
            for entry in listing:
                if scan.spent():
                    break
                scan.left -= 1
                relative = rel + entry.name
                try:
                    found = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(found.st_mode):
                        if found.st_dev != device:
                            continue
                        total += place(entry.path, entry.name, relative, depth + 1, owner, found)
                    else:
                        size = scan.file(found)
                        known = HOME_PATHS.get(relative)
                        if known:
                            claimed[(*known, relative)] += size
                        else:
                            owned(owner, rel, size)
                        total += size
                except OSError:
                    scan.unreadable += 1
        return total

    def whole(path, relative, depth):
        return scan.size(path, depth, device, keep=max(0, 2 - depth), into=sizes, prefix=relative + "/")

    def place(path, name, relative, depth, owner, info):
        known = HOME_PATHS.get(relative)
        deeper = relative in HOME_PREFIXES
        build = None if owner is not None or known or deeper else build_kind(path, name, depth)
        if known and not deeper:
            size = whole(path, relative, depth)
            claimed[(*known, relative)] += size
        elif known or deeper:
            size = walk(path, relative + "/", depth, (*known, relative) if known else owner, info)
        elif owner is not None:
            size = whole(path, relative, depth)
            claimed[owner] += size
        elif build:
            size = whole(path, relative, depth)
            claimed[("build", build, relative)] += size
        else:
            size = walk(path, relative + "/", depth, None, info)
        if depth <= 2:
            sizes[relative] = size
        return size
    total = walk(home, "", 0, None, info)
    kinds = collections.Counter()
    for (kind, _, _), size in claimed.items():
        kinds[kind] += size
    kinds["other"] += sum(other.values())
    items = sorted(({"kind": kind, "what": what, "path": path, "bytes": size}
                    for (kind, what, path), size in claimed.items() if size), key=lambda item: -item["bytes"])
    tops = sorted(((name, size) for name, size in sizes.items() if "/" not in name), key=lambda item: -item[1])
    biggest = []
    for name, size in tops[:top]:
        inner = sorted(((child, value) for child, value in sizes.items()
                        if child.startswith(name + "/") and "/" not in child[len(name) + 1:]), key=lambda item: -item[1])
        biggest.append({"path": name, "bytes": size,
                        "biggest": [{"path": child, "bytes": value} for child, value in inner[:3] if value]})
    caches = {}
    for relative in CACHES:
        found = next((item["bytes"] for item in items if item["path"] == relative), 0)
        if found:
            caches[relative] = found
    return {"bytes": total, "kinds": {kind: kinds[kind] for kind in HOME_KINDS if kinds[kind]}, "items": items,
            "other": [{"path": path, "bytes": size,
                       "biggest": [{"path": child, "bytes": value} for child, value in below.most_common()
                                   if child.startswith(path + "/")][:3]}
                      for path, size in other.most_common() if size],
            "biggest": biggest, "caches": caches}


def home_usage(scan, home):
    """A HOME's size, its biggest folders (each with its own biggest three), its caches gc --caches empties and its
    size by kind (home_detail's "kinds"), and the build output and toolchains it holds ("items": what gc
    --build-output and --toolchains would take, for the storage check's plan)."""
    found = home_detail(scan, home)
    return {"bytes": found["bytes"], "biggest": found["biggest"], "caches": found["caches"], "kinds": found["kinds"],
            "items": [item for item in found["items"] if item["kind"] in ("build", "toolchains")]}


# What gc removes of a stopped kept HOME on request, by kind (gc --build-output, --toolchains, --transcripts DAYS):
# the build output and temp home_detail finds; these toolchains (installed again by their tools; never "go", the
# GOPATH with the agent's sources, nor .cargo, .local/lib, .gradle, .m2: they hold more than a toolchain); the
# transcripts in these folders older than DAYS - never the newest of a conversation's group (what resume continues:
# Claude's newest per project folder, Codex's newest of all).
REMOVABLE_TOOLCHAINS = (".rustup", ".nvm", ".sdkman", ".pyenv", ".local/share/uv", ".local/share/mise", ".asdf",
                        ".volta", ".bun", ".deno", "sdk", ".local/share/fnm", ".juliaup", ".ghcup", ".dotnet")
# {folder: (what, the depth below it whose folders are each one group: 0 - all of it one group)}
TRANSCRIPTS = {".claude/projects": ("Claude transcripts", 1), ".codex/sessions": ("Codex transcripts", 0),
               ".codex/archived_sessions": ("Codex transcripts (archived)", 0)}


def inside(home, relative):
    """HOME/RELATIVE when no part of it is a link (else None): what gc removes is never reached through a link."""
    path = Path(home)
    if path.is_symlink() or not path.is_dir():
        return None
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink() or not os.path.lexists(path):
            return None
    return path


def old_transcripts(home, relative, days, now=None):
    """[(path, bytes)] of the transcripts (*.jsonl, and compacted ones: *.jsonl.gz) below HOME/RELATIVE last written
    more than DAYS days ago, but the newest file of each group (TRANSCRIPTS); never through a link, never another
    file there (Claude's auto memory, projects/*/memory/*.md)."""
    top = inside(home, relative)
    if top is None or not top.is_dir():
        return []
    depth = TRANSCRIPTS.get(relative, ("", 1))[1]
    cutoff = (time.time() if now is None else now) - days * 86400
    groups = collections.defaultdict(list)
    for folder, dirs, names in os.walk(top, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(folder, name))]
        below = Path(folder).relative_to(top).parts
        for name in names:
            if not name.endswith((".jsonl", ".jsonl.gz")):  # transcripts only: never an agent's memory (*.md) there
                continue
            with contextlib.suppress(OSError):
                info = os.lstat(os.path.join(folder, name))
                if stat.S_ISREG(info.st_mode):
                    group = below[:depth] if len(below) >= depth else ("(files)",)
                    groups[group].append((info.st_mtime, os.path.join(folder, name), Scan.used(info)))
    found = []
    for members in groups.values():
        members.sort()
        found += [(path, size) for when, path, size in members[:-1] if when < cutoff]
    return sorted(found)


def home_removable(scan, home, kinds, days=None, now=None):
    """What gc would remove of HOME by KINDS ("build", "toolchains", "transcripts" - those older than DAYS days):
    [{"kind", "what", "path", "bytes", "files"}] the biggest first."""
    found = []
    detail = home_detail(scan, home) if {"build", "toolchains"} & set(kinds) else {"items": []}
    for item in detail["items"]:
        if (item["kind"] == "build" and "build" in kinds) or \
                (item["kind"] == "toolchains" and "toolchains" in kinds and item["path"] in REMOVABLE_TOOLCHAINS):
            found.append({**item, "files": None})
    if "transcripts" in kinds and days is not None:
        for relative, (what, _) in TRANSCRIPTS.items():
            old = old_transcripts(home, relative, days, now)
            if old:
                found.append({"kind": "transcripts", "what": what, "path": relative,
                              "bytes": sum(size for _, size in old), "files": len(old)})
    return sorted(found, key=lambda item: -item["bytes"])


def remove_home_items(home, items, days=None, now=None):
    """Remove ITEMS (home_removable's) of HOME: a folder whole, transcripts only those still older than DAYS (and
    still not the newest of their folder) - each checked again now, never through a link. [{"kind", "what", "path",
    "bytes"}] of what went."""
    removed = []
    for item in items:
        if item["kind"] == "transcripts":
            gone = 0
            for path, size in old_transcripts(home, item["path"], days, now):
                with contextlib.suppress(OSError):
                    os.unlink(path)
                    gone += size
            top = inside(home, item["path"])
            for folder, _, _ in os.walk(top, topdown=False) if top is not None else []:
                if Path(folder) != top and not os.path.islink(folder):
                    with contextlib.suppress(OSError):
                        os.rmdir(folder)  # only an empty one: emptied now, or before
            if gone:
                removed.append({**item, "bytes": gone})
            continue
        path = inside(home, item["path"])
        if path is None or path.is_symlink() or not path.is_dir():
            continue
        size = Scan().size(path)
        remove_tree(path)
        if os.path.lexists(path):
            size -= Scan().size(path)
        removed.append({**item, "bytes": size})
    return removed


def remove_tree(path):
    """The folder PATH removed with what it holds - read-only folders made writable on the way (Go's module cache),
    links removed as links, never followed."""
    def writable(function, failed, _):
        for target in (os.path.dirname(failed), failed):
            with contextlib.suppress(OSError):
                if not os.path.islink(target):
                    os.chmod(target, 0o700)
        with contextlib.suppress(OSError):
            function(failed)
    shutil.rmtree(path, onerror=writable)


def podman_usage(run=subprocess.run, timeout=PODMAN_SECONDS):
    """`podman system df` as {"images"|"containers"|"volumes": {"total", "active", "bytes", "reclaimable"}}, and the
    images sandbox-ai built that no name points to any more ("dangling": left by a rebuild); None without Podman or
    an answer."""
    if not shutil.which("podman"):
        return None
    try:
        result = run(["podman", "system", "df", "--format", "json"], capture_output=True, text=True,
                     timeout=timeout, stdin=subprocess.DEVNULL)
        rows = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        return None
    if not isinstance(rows, list):
        return None
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Type"), str):
            continue
        kind = row["Type"].lower().replace("local ", "").rstrip("s") + "s"
        found[kind] = {"total": number(row.get("Total")), "active": number(row.get("Active")),
                       "bytes": number(row.get("RawSize", row.get("Size"))),
                       "reclaimable": number(row.get("RawReclaimable", row.get("Reclaimable")))}
    dangling = dangling_images(run, timeout)
    if dangling is not None:
        found["dangling"] = {"total": len(dangling), "bytes": sum(image["bytes"] for image in dangling),
                             "images": dangling}
    return found


def number(value):
    """An int from Podman's JSON (a number, or text such as "1.2GB (40%)": None for text)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def dangling_images(run=subprocess.run, timeout=PODMAN_SECONDS):
    """[{"id", "bytes", "created"}] of the images sandbox-ai built (a label sandbox-ai.*) that no name points to any
    more: an image rebuilt leaves its predecessor so. None when Podman does not answer."""
    try:
        result = run(["podman", "images", "--filter", "dangling=true", "--format", "json"], capture_output=True,
                     text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        rows = json.loads(result.stdout or "[]") if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        return None
    if not isinstance(rows, list):
        return None
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        labels = row.get("Labels") if isinstance(row.get("Labels"), dict) else {}
        ident = row.get("Id") or row.get("ID")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{12,64}", ident) or \
                not any(isinstance(key, str) and key.startswith("sandbox-ai.") for key in labels):
            continue
        found.append({"id": ident[:12], "bytes": number(row.get("Size")) or 0,
                      "created": row.get("CreatedAt") if isinstance(row.get("CreatedAt"), str) else None})
    return found


def remove_images(images, run=subprocess.run, timeout=120):
    """`podman image rm` of each image id of IMAGES (never forced: one a container still uses stays); the ids
    removed and {id: why} of those that stayed."""
    removed, stayed = [], {}
    for ident in images:
        if not re.fullmatch(r"[0-9a-f]{12,64}", str(ident)):
            continue
        try:
            result = run(["podman", "image", "rm", ident], capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            stayed[ident] = str(exc)[-200:]
            continue
        if result.returncode == 0:
            removed.append(ident)
        else:
            stayed[ident] = (result.stderr or "").strip()[-200:]
    return removed, stayed


# Podman's images by what they are to sandbox-ai (image_groups): why Podman's "reclaimable" (every image no container
# uses, every software's) is far more than gc --images frees. {group: (what, how)}; LEVELS: what gc --images=LEVEL
# removes, each level with the ones before it.
IMAGE_GROUPS = {
    "in_use": ("a container uses them", "kept"),
    "sandbox_used": ("sandbox-ai's, kept starts use them", "kept"),
    "sandbox_roles": ("sandbox-ai's browser, VPN, Cairn, team; your services",
                      "kept (--browser, --vpn, --cairn, --with, --service)"),
    "sandbox_unused": ("sandbox-ai's, no kept start uses them", "sandbox-ai gc --images=old (built again when needed)"),
    "sandbox_unnamed": ("sandbox-ai's without a name (rebuilds)", "sandbox-ai gc --images"),
    "other_unnamed": ("other software's without a name", "sandbox-ai gc --images=prune (as podman image prune)"),
    "intermediate": ("layers images are built on", "freed with the image on them"),
    "other": ("other software's (base images, yours)",
              "not sandbox-ai's: podman image rm NAME (podman image prune -a: every unused one)"),
}
IMAGE_LEVELS = {"unnamed": ("sandbox_unnamed",), "old": ("sandbox_unnamed", "sandbox_unused"),
                "prune": ("sandbox_unnamed", "sandbox_unused", "other_unnamed")}
SANDBOX_ROLE_LABELS = ("sandbox-ai.role", "sandbox-ai.user-image")
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/:@-]{0,254}")


def podman_json(arguments, run=subprocess.run, timeout=PODMAN_SECONDS):
    """`podman ARGUMENTS` as JSON, or None (no Podman, no answer, not JSON)."""
    try:
        result = run(["podman", *arguments], capture_output=True, text=True, timeout=timeout,
                     stdin=subprocess.DEVNULL)
        return json.loads(result.stdout or "null") if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        return None


def image_names(row):
    """The names of an image row of `podman images --format json` ("<none>" ones left out)."""
    names = []
    for key in ("Names", "RepoTags"):
        value = row.get(key)
        for name in value if isinstance(value, list) else []:
            if isinstance(name, str) and "<none>" not in name and name not in names:
                names.append(name)
    return names


def repository(name):
    """An image NAME without its tag or digest (localhost/claude-sandbox:base: localhost/claude-sandbox)."""
    name = name.split("@", 1)[0]
    head, _, tail = name.rpartition(":")
    return head if head and "/" not in tail else name


def image_groups(wanted=(), run=subprocess.run, timeout=PODMAN_SECONDS):
    """Podman's images by group (IMAGE_GROUPS): WANTED the image names the kept starts use. {"groups": {group:
    {"count", "bytes", "images": [{"id", "names", "bytes", "created", "containers"}] the biggest first}}, "bytes",
    "count"}; a size is what removing the image frees (`podman system df -v`'s unique size; else its size, which
    counts layers it shares). None without Podman or an answer."""
    if not shutil.which("podman"):
        return None
    rows = podman_json(["images", "--all", "--format", "json"], run, timeout)
    if not isinstance(rows, list):
        return None
    unique = {}
    found = podman_json(["system", "df", "-v", "--format", "json"], run, timeout)
    for row in (found.get("Images") if isinstance(found, dict) else None) or []:
        if isinstance(row, dict) and isinstance(row.get("ImageID"), str) and number(row.get("UniqueSize")) is not None:
            unique[row["ImageID"][:12]] = number(row["UniqueSize"])
    rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get("Id") or row.get("ID"), str)]
    parents = {str(row.get("ParentId") or "")[:12] for row in rows if row.get("ParentId")}
    wanted_repos = {repository(name) for name in wanted}
    groups = {key: {"count": 0, "bytes": 0, "images": []} for key in IMAGE_GROUPS}
    seen = set()
    for row in rows:
        ident = (row.get("Id") or row.get("ID"))[:12]
        if not re.fullmatch(r"[0-9a-f]{12}", ident) or ident in seen:
            continue
        seen.add(ident)
        names = image_names(row)
        labels = row.get("Labels") if isinstance(row.get("Labels"), dict) else {}
        ours = any(isinstance(key, str) and key.startswith("sandbox-ai.") for key in labels)
        containers = number(row.get("Containers")) or 0
        if containers:
            group = "in_use"
        elif not names:
            group = "intermediate" if ident in parents else "sandbox_unnamed" if ours else "other_unnamed"
        elif not ours:
            group = "other"
        elif any(key in labels for key in SANDBOX_ROLE_LABELS):
            group = "sandbox_roles"
        elif any(repository(name) in wanted_repos for name in names):
            group = "sandbox_used"
        else:
            group = "sandbox_unused"
        size = unique.get(ident, number(row.get("Size")) or 0)
        groups[group]["count"] += 1
        groups[group]["bytes"] += size
        groups[group]["images"].append({"id": ident, "names": names[:5], "bytes": size, "containers": containers,
                                        "created": row.get("CreatedAt") if isinstance(row.get("CreatedAt"), str)
                                        else None})
    for value in groups.values():
        value["images"].sort(key=lambda image: -image["bytes"])
    return {"groups": groups, "bytes": sum(value["bytes"] for value in groups.values()), "count": len(seen),
            "unique_sizes": bool(unique)}


def remove_image_refs(refs, run=subprocess.run, timeout=120):
    """`podman image rm` of each image id or name of REFS (never forced: one a container uses stays; a name of an
    image with more names only goes from it): the refs removed and {ref: why} of those that stayed."""
    removed, stayed = [], {}
    for ref in refs:
        ref = str(ref)
        if not IMAGE_REF.fullmatch(ref) or ref.startswith("-"):
            continue
        try:
            result = run(["podman", "image", "rm", ref], capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            stayed[ref] = str(exc)[-200:]
            continue
        if result.returncode == 0:
            removed.append(ref)
        else:
            stayed[ref] = (result.stderr or "").strip()[-200:]
    return removed, stayed


def usage(starts=None, live_runs=(), podman=True, scan=None, env=None, run=subprocess.run, busy_dirs=None,
          aged_before=None):
    """What sandbox-ai keeps here, part by part. STARTS: {id: entry} of the start directories (starts.start_entries:
    state, keep, last_used, name); LIVE_RUNS: the ids of the runs that run now (their logs are not compressible);
    BUSY_DIRS: the log folders they log into (their shared logs are never split; None: not known, every folder counts
    as one); AGED_BEFORE: the logs last written before (seconds) go by log_days, not compressed."""
    scan = scan or Scan()
    started = time.monotonic()
    places = roots(env)
    state = places["state"]
    starts = starts or {}
    result = {"roots": {name: str(path) for name, path in places.items()}, "total": 0, "parts": {}}

    # the starts: each start directory, its HOME apart
    entries = []
    for directory in sorted(state.joinpath("sessions").glob("*")) if state.joinpath("sessions").is_dir() else []:
        if scan.spent():
            break
        if directory.is_symlink() or not directory.is_dir():
            continue
        entry = starts.get(directory.name) or {}
        home = directory / "home"
        found = home_usage(scan, home) if home.is_dir() and not home.is_symlink() else \
            {"bytes": 0, "biggest": [], "caches": {}, "kinds": {}}
        size = found["bytes"] + sum(value for name, value, _ in children(scan, directory, skip={"home"}))
        entries.append({"id": directory.name, "name": entry.get("name") or directory.name,
                        "state": entry.get("state"), "keep": entry.get("keep"), "agent": entry.get("agent"),
                        "last_used": entry.get("last_used"), "workspace": entry.get("workspace"),
                        "bytes": size, "home": found})
    entries.sort(key=lambda item: -item["bytes"])
    result["parts"]["starts"] = {"path": str(state / "sessions"), "bytes": sum(e["bytes"] for e in entries),
                                 "count": len(entries), "items": entries}

    # the logs of each project
    logs = []
    log_root = state / "logs"
    busy = None
    if busy_dirs is not None:
        busy = set()
        for folder in busy_dirs:
            with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                busy.add(os.path.realpath(folder))
    for folder in sorted(log_root.glob("*")) if log_root.is_dir() else []:
        if scan.spent():
            break
        if folder.is_dir() and not folder.is_symlink():
            logs.append(log_dir_usage(scan, folder, set(live_runs),
                                      in_use=busy is None or os.path.realpath(folder) in busy,
                                      aged_before=aged_before))
    logs.sort(key=lambda item: -item["bytes"])
    kinds = collections.defaultdict(int)
    for folder in logs:
        for kind, value in folder["kinds"].items():
            kinds[kind] += value["bytes"]
    # what compressing saves, per kind: measured on a sample (the biggest file of each kind)
    compress_kinds = {}
    for folder in logs:
        for kind, entry in folder.pop("compress_kinds", {}).items():
            into = compress_kinds.setdefault(kind, {"bytes": 0, "sample": None, "sample_bytes": 0})
            into["bytes"] += entry["bytes"]
            if entry["sample_bytes"] > into["sample_bytes"]:
                into["sample"], into["sample_bytes"] = entry["sample"], entry["sample_bytes"]
    for kind, entry in compress_kinds.items():
        ratio = compression_ratio(entry.pop("sample")) if entry["sample"] else None
        entry.pop("sample_bytes")
        entry["ratio"] = max(1.0, ratio) if ratio else RATIO_FALLBACK
        entry["measured"] = ratio is not None
        entry["saves"] = int(entry["bytes"] * (1 - 1 / entry["ratio"]))
    result["parts"]["logs"] = {"path": str(log_root), "bytes": sum(f["bytes"] for f in logs), "count": len(logs),
                               "kinds": dict(sorted(kinds.items(), key=lambda item: -item[1])), "items": logs,
                               "compressible": sum(f["compressible"] for f in logs),
                               "splittable": sum(f["splittable"] for f in logs),
                               "compress_kinds": compress_kinds}

    # the MCP servers, the histories, launcher.log and the rest of the state directory
    named = {"sessions", "logs"}
    for part, folder in (("mcp", state / "mcp"), ("history", state / "history"),
                         ("checkpoints", state / "checkpoints")):  # (the workspace checkpoints' stores, by project)
        named.add(folder.name)
        items = [{"name": name, "bytes": size} for name, size, _ in children(scan, folder)] \
            if folder.is_dir() and not folder.is_symlink() else []
        result["parts"][part] = {"path": str(folder), "bytes": sum(i["bytes"] for i in items), "count": len(items),
                                 "items": items[:TOP]}
    launcher_logs, others = [], []
    for name, size, _ in children(scan, state, skip=named) if state.is_dir() else []:
        (launcher_logs if re.fullmatch(r"launcher\.(?:log(?:\.\d+)?|states\.json)", name) else others).append(
            {"name": name, "bytes": size})
    result["parts"]["launcher_log"] = {"path": str(state / "launcher.log"),
                                       "bytes": sum(i["bytes"] for i in launcher_logs), "items": launcher_logs}
    result["parts"]["state_other"] = {"path": str(state), "bytes": sum(i["bytes"] for i in others),
                                      "items": others[:TOP], "count": len(others)}

    # the other folders: data, cache, configuration, run directories
    for part in ("data", "cache", "config", "runtime"):
        folder = places.get(part)
        if folder is None:
            continue
        items = [{"name": name, "bytes": size} for name, size, _ in children(scan, folder)] \
            if folder.is_dir() and not folder.is_symlink() else []
        result["parts"][part] = {"path": str(folder), "bytes": sum(i["bytes"] for i in items),
                                 "items": items[:TOP], "count": len(items)}
    result["total"] = sum(part["bytes"] for part in result["parts"].values())
    result["podman"] = podman_usage(run) if podman else None
    result["scan"] = {"seconds": round(time.monotonic() - started, 2), "partial": scan.partial,
                      "unreadable": scan.unreadable}
    return result


# -- compaction ---------------------------------------------------------------------------------------------------

class Budget:
    """When a compaction stops starting new work (None: never)."""

    def __init__(self, seconds=None, clock=time.monotonic):
        self.clock, self.deadline = clock, None if seconds is None else clock() + seconds
        self.out = False

    def over(self):
        if self.deadline is not None and self.clock() > self.deadline:
            self.out = True
        return self.out


def compress(path, level=LEVEL):
    """PATH compressed into PATH.gz (its time and mode kept), then PATH removed; the bytes saved. Left as it was
    when it changed meanwhile (a writer still at it) or the disk could not take the compressed copy (OSError)."""
    path = Path(path)
    target = path.with_name(path.name + ".gz")
    temporary = path.with_name(f".{path.name}.gz.{os.getpid()}.tmp")
    source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"{path} is not a regular file")
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                      0o600)
        try:
            with open(out, "wb", closefd=False) as raw, \
                    gzip.GzipFile(filename=path.name, mode="wb", compresslevel=level, fileobj=raw,
                                  mtime=int(before.st_mtime)) as packed:
                while True:
                    data = os.read(source, CHUNK)
                    if not data:
                        break
                    packed.write(data)
            os.fsync(out)
            after = os.fstat(source)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise OSError(f"{path} changed while it was compressed")
            os.utime(out, ns=(before.st_atime_ns, before.st_mtime_ns))
            saved = Scan.used(before) - Scan.used(os.fstat(out))
        finally:
            os.close(out)
        os.replace(temporary, target)
        os.unlink(path)
        return saved
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    finally:
        os.close(source)


def packed_prefix(raw, packed, chunk=CHUNK):
    """How many bytes the compressed log PACKED holds when they are the beginning of the file RAW, byte for byte; None
    when they are not (or more than RAW holds), PACKED is empty, or either cannot be read (a link, damage)."""
    try:
        source = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        fd = os.open(packed, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with gzip.GzipFile(fileobj=os.fdopen(fd, "rb"), mode="rb") as stream:
            if not stat.S_ISREG(os.fstat(source).st_mode) or not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            size = 0
            while True:
                data = stream.read(chunk)
                if not data:
                    return size or None
                if os.pread(source, len(data), size) != data:
                    return None
                size += len(data)
    except (OSError, EOFError, zlib.error):
        return None
    finally:
        os.close(source)


def finish_part(raw, packed, level=LEVEL):
    """The log RAW whose compressed copy PACKED (RAW.gz) is there already - a rotation or a compression killed before
    its last step, the removal of RAW: gc skipped such a pair for good. When PACKED holds RAW's beginning, what RAW
    got after it (a late writer's record) is added to PACKED as another gzip member, PACKED takes RAW's time and RAW
    is removed: True. Anything else (another content under that name): both stay, False."""
    size = packed_prefix(raw, packed)
    if size is None:
        return False
    source = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(source)
        if info.st_size > size:
            with open(packed, "ab") as stream:
                offset = size
                while offset < info.st_size:
                    data = os.pread(source, min(CHUNK, info.st_size - offset), offset)
                    if not data:
                        break
                    stream.write(gzip.compress(data, level))
                    offset += len(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.utime(packed, ns=(info.st_atime_ns, info.st_mtime_ns))
    finally:
        os.close(source)
    os.unlink(raw)
    return True


def undo_cut_rotation(log):
    """The run's connection log LOG (the run ended) whose newest older part LOG.1.gz holds LOG's own beginning: its
    rotation (runtime.rotate_held_log) put the part in place and was killed before it emptied the log - the part's
    records were in both, counted twice by every reader. The part is removed (LOG has all of it) and the parts before
    it get their numbers back (LOG.2.gz -> LOG.1.gz ...: the timeline's keys count the rotations). The name removed,
    or None (the usual case: the part is older records)."""
    log = Path(log)
    part = log.with_name(log.name + ".1.gz")
    if packed_prefix(log, part) is None:
        return None
    os.unlink(part)
    numbered = re.compile(re.escape(log.name) + r"\.(\d{1,3})\.gz")
    with contextlib.suppress(OSError):
        older = sorted(int(numbered.fullmatch(entry.name).group(1)) for entry in os.scandir(log.parent)
                       if numbered.fullmatch(entry.name))
        for number in older:
            target = log.with_name(f"{log.name}.{number - 1}.gz")
            if number > 1 and not os.path.lexists(target):
                os.rename(log.with_name(f"{log.name}.{number}.gz"), target)
    return part.name


def split(path, keep=SHARED_KEEP, level=LEVEL, when=None):
    """The shared log PATH keeps its newest lines (about KEEP bytes, from the start of a line); the older part goes
    compressed into PATH.TIME.gz. The bytes it shrank by (on disk, the compressed part counted)."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(when if when is not None else time.time()))
    archive = path.with_name(f"{path.name}.{stamp}.gz")
    rest = path.with_name(f".{path.name}.rest.{os.getpid()}.tmp")
    part = path.with_name(f".{archive.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= keep:
            return 0
        # where the kept part starts: just after a newline at or before size - keep
        cut, position = 0, before.st_size - keep
        while position > 0:
            start = max(0, position - CHUNK)
            data = os.pread(source, position - start, start)
            index = data.rfind(b"\n")
            if index >= 0:
                cut = start + index + 1
                break
            position = start
        if cut <= 0:
            return 0
        archived = os.open(part, flags, 0o600)
        kept = None
        try:
            with open(archived, "wb", closefd=False) as raw, \
                    gzip.GzipFile(filename=path.name, mode="wb", compresslevel=level, fileobj=raw,
                                  mtime=int(before.st_mtime)) as packed:
                offset = 0
                while offset < cut:
                    data = os.pread(source, min(CHUNK, cut - offset), offset)
                    if not data:
                        raise OSError(f"{path} became shorter while it was split")
                    packed.write(data)
                    offset += len(data)
            os.fsync(archived)
            kept = os.open(rest, flags, 0o600)
            offset = cut
            while True:
                data = os.pread(source, CHUNK, offset)
                if not data:
                    break
                os.write(kept, data)
                offset += len(data)
            os.fsync(kept)
            after = os.fstat(source)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise OSError(f"{path} changed while it was split")
            os.fchmod(kept, stat.S_IMODE(before.st_mode))
            os.utime(kept, ns=(before.st_atime_ns, before.st_mtime_ns))
            # (the part too: no record in it is newer than the log's last write - the age limit goes by this time)
            os.utime(archived, ns=(before.st_atime_ns, before.st_mtime_ns))
            saved = Scan.used(before) - Scan.used(os.fstat(kept)) - Scan.used(os.fstat(archived))
        finally:
            os.close(archived)
            if kept is not None:
                os.close(kept)
        os.replace(part, archive)
        os.replace(rest, path)
        return saved
    except BaseException:
        for temporary in (rest, part):
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise
    finally:
        os.close(source)


def drop_archives(folder, name, limit=ARCHIVE_LIMIT):
    """The oldest compressed parts of the shared log NAME in FOLDER go while they hold more than LIMIT bytes; the
    names removed."""
    found = []
    with contextlib.suppress(OSError), os.scandir(folder) as listing:
        for entry in listing:
            match = ARCHIVE.fullmatch(entry.name)
            if match and match.group(1) == name and entry.is_file(follow_symlinks=False):
                found.append((entry.name, entry.stat(follow_symlinks=False).st_size))
    found.sort()
    total, removed = sum(size for _, size in found), []
    for archive, size in found:
        if total <= limit:
            break
        with contextlib.suppress(OSError):
            os.unlink(Path(folder) / archive)
            total -= size
            removed.append(archive)
    return removed


def rotate(path, limit=ARCHIVE_LIMIT, when=None):
    """The log PATH - one its writer opens again after this (mcp.split_log, the web interface's audit log), or opens
    for each record - renamed to PATH.TIME and compressed into PATH.TIME.gz; the oldest such parts go beyond LIMIT
    bytes. Left uncompressed as PATH.TIME when that fails (a writer still at it, a full disk: gc compresses it later).
    The name of the older part, or None (no PATH, not a file, or one of that second exists already)."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if when is None else when))
    kept = path.with_name(f"{path.name}.{stamp}")
    if os.path.lexists(kept) or os.path.lexists(kept.with_name(kept.name + ".gz")):
        return None
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        os.rename(path, kept)
    except FileNotFoundError:
        return None
    try:
        compress(kept)
    except OSError:
        return kept.name
    drop_archives(path.parent, path.name, limit)
    return kept.name + ".gz"


def mcp_lock(folder):
    """A lock on the logs of the MCP server FOLDER that nothing else holds (no log reader of it runs: mcp.split_log
    holds it shared), as an open descriptor; None when one holds it, or it cannot be had."""
    import fcntl
    try:
        fd = os.open(Path(folder) / MCP_LOCK, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def compact_mcp(mcp_root=None, dry_run=False, now=None, quiet=QUIET, part=None):
    """The logs of the MCP servers under MCP_ROOT (default: the state directory's mcp/): of a server whose log reader
    does not run (mcp_lock), a log over PART (MCP_LOG_PART) not written for QUIET seconds is rotated (rotate: renamed
    and compressed); older parts left uncompressed are compressed; decided approvals (ID.json with its ID.done) older
    than APPROVALS_DAYS go. DRY_RUN: only say so. {"rotated": [paths], "compressed": [paths], "approvals": count,
    "errors": [...]}."""
    mcp_root = Path(mcp_root) if mcp_root is not None else state_root() / "mcp"
    now = time.time() if now is None else now
    part = MCP_LOG_PART if part is None else part
    result = {"rotated": [], "compressed": [], "approvals": 0, "errors": []}
    try:
        folders = sorted(p for p in mcp_root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return result
    for folder in folders:
        lock = mcp_lock(folder)
        if lock is None:
            continue  # its log reader runs: it rotates its logs itself
        try:
            for entry in sorted(os.scandir(folder), key=lambda item: item.name):
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < quiet:
                    continue
                raw = ARCHIVE_RAW.fullmatch(entry.name)
                if entry.name in MCP_LOGS and info.st_size > part:
                    result["rotated"].append(entry.path)
                    if not dry_run and rotate(entry.path, MCP_ARCHIVE_LIMIT, now) is None:
                        result["errors"].append(f"{entry.path}: not rotated")
                elif raw and raw.group(1) in MCP_LOGS and not os.path.lexists(entry.path + ".gz"):
                    result["compressed"].append(entry.path)
                    if not dry_run:
                        try:
                            compress(entry.path)
                        except OSError as exc:
                            result["errors"].append(f"{entry.path}: {exc}")
            approvals = folder / "approvals"
            if approvals.is_dir() and not approvals.is_symlink():
                for done in sorted(approvals.glob("*.done")):
                    with contextlib.suppress(OSError):
                        if now - done.lstat().st_mtime > APPROVALS_DAYS * 86400:
                            result["approvals"] += 1
                            if not dry_run:
                                done.with_suffix(".json").unlink(missing_ok=True)
                                done.unlink()
        except OSError as exc:
            result["errors"].append(f"{folder}: {exc}")
        finally:
            os.close(lock)
    return result


def unused_rootfs(referenced, store=None, now=None, days=ROOTFS_DAYS, scan=None):
    """The MCP root file systems in STORE (default: the data directory's mcp-rootfs/) whose SHA-256 no definition or
    server state names (REFERENCED: every 64-digit hex string found there) and that are older than DAYS days (one
    unpacked just now may be a start's that is about to use it): [{"sha256", "path", "bytes"}]."""
    store = Path(store) if store is not None else roots()["data"] / "mcp-rootfs"
    now = time.time() if now is None else now
    scan = scan or Scan()
    found = []
    try:
        entries = sorted(store.iterdir())
    except OSError:
        return found
    for path in entries:
        if not re.fullmatch(r"[0-9a-f]{64}", path.name) or path.name in referenced or path.is_symlink() or \
                not path.is_dir():
            continue
        with contextlib.suppress(OSError):
            if now - path.lstat().st_mtime > days * 86400:
                found.append({"sha256": path.name, "path": str(path), "bytes": scan.size(path)})
    return found


# The temporary files of compress() and split() (.NAME.gz.PID.tmp, .NAME.TIME.gz.PID.tmp, .NAME.rest.PID.tmp).
TEMPORARY = re.compile(r"\..+\.(?:gz|rest)\.(\d{1,10})\.tmp")


def left_behind(entry, pid, now, quiet=QUIET):
    """The temporary file ENTRY (TEMPORARY) of a compaction that was killed: its process PID is gone and it was not
    written for QUIET seconds (a compaction that runs writes its own all along)."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < quiet:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False  # (another user's process: it may be one)
    return False


def compact(log_root=None, live_runs=(), busy_dirs=(), dry_run=False, budget=None, now=None, quiet=QUIET,
            caps=None):
    """Compact every log directory under LOG_ROOT (default: the state directory's logs/): the logs of runs that
    ended (not in LIVE_RUNS, not written for QUIET seconds) are compressed; a shared log over SHARED_LIMIT of a
    directory no run uses (BUSY_DIRS: those running runs log into) is split. CAPS(folder, dry_run): the per-directory
    limits of the runs' logs (the launcher's rotate_*), applied after. DRY_RUN: only say what would be done.
    {"compressed": [{"path", "bytes"}], "split": [...], "dropped": [names], "saved": bytes, "errors": [...],
    "unfinished": the budget ran out}."""
    log_root = Path(log_root) if log_root is not None else state_root() / "logs"
    budget = budget or Budget()
    now = time.time() if now is None else now
    live, busy = set(live_runs), set()
    for folder in busy_dirs:
        with contextlib.suppress(OSError, RuntimeError, TypeError):
            busy.add(os.path.realpath(folder))
    result = {"compressed": [], "split": [], "dropped": [], "saved": 0, "errors": [], "unfinished": False}
    try:
        folders = sorted(p for p in log_root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        folders = []
    for folder in folders:
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError as exc:
            result["errors"].append(f"{folder}: {exc}")
            continue
        in_use = os.path.realpath(folder) in busy
        for entry in entries:
            if budget.over():
                result["unfinished"] = True
                return result
            leftover = TEMPORARY.fullmatch(entry.name)
            if leftover:
                if not dry_run and left_behind(entry, int(leftover.group(1)), now, quiet):
                    with contextlib.suppress(OSError):
                        size = Scan.used(entry.stat(follow_symlinks=False))
                        os.unlink(entry.path)
                        result["dropped"].append(entry.name)
                        result["saved"] += size
                continue
            match = PER_RUN.fullmatch(entry.name) or ARCHIVE_RAW.fullmatch(entry.name)
            wanted = (match and match.re is ARCHIVE_RAW) or \
                (match and not match.group(5) and match.group(3) not in live) or \
                (entry.name in SHARED and not in_use)
            if not wanted:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < quiet:
                continue
            if match:
                if Path(folder, entry.name + ".gz").exists():
                    # compressed already and still there (a rotation killed before its last step): finished here -
                    # another content under that name is never overwritten, both stay
                    if not dry_run:
                        try:
                            if finish_part(entry.path, Path(folder, entry.name + ".gz")):
                                result["dropped"].append(entry.name)
                                result["saved"] += Scan.used(info)
                        except OSError as exc:
                            result["errors"].append(f"{entry.path}: {exc}")
                    continue
                if dry_run:
                    result["compressed"].append({"path": entry.path, "bytes": Scan.used(info)})
                    continue
                if match.re is PER_RUN and match.group(1) == "connections" and not match.group(4):
                    with contextlib.suppress(OSError):  # (a rotation that never emptied the log: no duplicates)
                        removed = undo_cut_rotation(entry.path)
                        if removed:
                            result["dropped"].append(removed)
                try:
                    saved = compress(entry.path)
                except OSError as exc:
                    result["errors"].append(f"{entry.path}: {exc}")
                    if getattr(exc, "errno", None) == 28:  # ENOSPC: nothing more fits
                        return result
                    continue
                result["compressed"].append({"path": entry.path, "bytes": Scan.used(info)})
                result["saved"] += saved
                if match.re is ARCHIVE_RAW:  # an older part of a shared log: its parts capped here too (a busy
                    # folder's shared logs are never split, and activity.jsonl rotates itself: its parts grew on)
                    result["dropped"] += drop_archives(folder, match.group(1), ARCHIVE_LIMIT)
            elif info.st_size > SHARED_LIMIT:
                if dry_run:
                    result["split"].append({"path": entry.path, "bytes": Scan.used(info) - SHARED_KEEP})
                    continue
                try:
                    saved = split(entry.path, keep=SHARED_KEEP, when=now)
                except OSError as exc:
                    result["errors"].append(f"{entry.path}: {exc}")
                    if getattr(exc, "errno", None) == 28:
                        return result
                    continue
                result["split"].append({"path": entry.path, "bytes": Scan.used(info) - SHARED_KEEP})
                result["saved"] += saved
                result["dropped"] += drop_archives(folder, entry.name)
        if caps is not None and not dry_run:
            with contextlib.suppress(OSError, ValueError):
                result["dropped"] += caps(folder)
    return result


def folder_written(folder, limit=100_000):
    """The time anything below FOLDER (a log folder: its files, and those of its subfolders) was last written, the
    folder itself counted (0: nothing could be read). Links are never followed; at most LIMIT entries are looked at."""
    newest, left, stack = 0, limit, [str(folder)]
    while stack and left > 0:
        current = stack.pop()
        with contextlib.suppress(OSError):
            newest = max(newest, os.lstat(current).st_mtime)
            with os.scandir(current) as listing:
                for entry in listing:
                    left -= 1
                    with contextlib.suppress(OSError):
                        info = entry.stat(follow_symlinks=False)
                        newest = max(newest, info.st_mtime)
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(entry.path)
    return newest


def old_log_folders(log_root=None, days=90, busy_dirs=(), now=None, scan=None):
    """The log folders under LOG_ROOT (default: the state directory's logs/) nothing was written into for more than
    DAYS days - projects that are gone, or not used for that long -, never one of BUSY_DIRS (those a registered run
    logs into) nor a link: [{"path", "name", "bytes", "last_written"}], the oldest first."""
    log_root = Path(log_root) if log_root is not None else state_root() / "logs"
    now = time.time() if now is None else now
    scan = scan or Scan()
    busy = set()
    for folder in busy_dirs:
        with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
            busy.add(os.path.realpath(folder))
    try:
        folders = sorted(p for p in log_root.iterdir() if p.is_dir() and not p.is_symlink()
                         and not p.name.startswith("."))
    except OSError:
        return []
    found = []
    for folder in folders:
        if os.path.realpath(folder) in busy:
            continue
        written = folder_written(folder)
        if not written or now - written <= days * 86400:
            continue
        found.append({"path": str(folder), "name": folder.name, "bytes": scan.size(folder),
                      "last_written": iso(written)})
    return sorted(found, key=lambda item: (item["last_written"], item["name"]))


def old_logs(log_root=None, days=LOG_DAYS, live_runs=(), busy_dirs=(), now=None):
    """The log files under LOG_ROOT (default: the state directory's logs/) nothing was written into for more than DAYS
    days - a file's last write is its newest record, so all of it is that old: the older parts of the logs (NAME.TIME,
    NAME.TIME.gz; a run's NAME.N.gz, also of a run that still runs), the logs of runs that ended (not in LIVE_RUNS:
    NAME, NAME.N, NAME.gz) and the shared logs of a folder no run logs into (BUSY_DIRS). Never the log a running run
    writes, a link, or anything in a hidden folder (one being removed); a spend log written in this month neither (a
    month's spend limit counts it). [{"path", "bytes", "last_written"}], the oldest first."""
    log_root = Path(log_root) if log_root is not None else state_root() / "logs"
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    # A spend log stays while its month goes on: a month's limit (sandbox-ai spend limit --month) counts it.
    month = time.localtime(now)
    spend_cutoff = min(cutoff, time.mktime((month.tm_year, month.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    live, busy = set(live_runs), set()
    for folder in busy_dirs:
        with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
            busy.add(os.path.realpath(folder))
    try:
        folders = sorted(p for p in log_root.iterdir() if p.is_dir() and not p.is_symlink()
                         and not p.name.startswith("."))
    except OSError:
        return []
    found = []
    for folder in folders:
        in_use = os.path.realpath(folder) in busy
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            run = PER_RUN.fullmatch(entry.name)
            wanted = ARCHIVE.fullmatch(entry.name) or ARCHIVE_RAW.fullmatch(entry.name) or \
                (run and (run.group(4) or run.group(5) or run.group(3) not in live)) or \
                (entry.name in SHARED and not in_use)
            if not wanted:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime < (spend_cutoff if run and run.group(1) == "spend"
                                                                else cutoff):
                found.append({"path": entry.path, "bytes": Scan.used(info), "last_written": iso(info.st_mtime)})
    return sorted(found, key=lambda item: (item["last_written"], item["path"]))


def remove_old_logs(found, days, now=None):
    """The log files FOUND (old_logs) removed - each only while it is still older than DAYS days (not written
    meanwhile) and a file; {"removed": [paths], "bytes", "errors"}."""
    now = time.time() if now is None else now
    result = {"removed": [], "bytes": 0, "errors": []}
    for item in found:
        try:
            info = os.lstat(item["path"])
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= now - days * 86400:
                continue
            os.unlink(item["path"])
        except FileNotFoundError:
            continue
        except OSError as exc:
            result["errors"].append(f"{item['path']}: {exc}")
            continue
        result["removed"].append(item["path"])
        result["bytes"] += Scan.used(info)
    return result


def remove_log_folder(path, busy=None):
    """The log folder PATH removed for good: first renamed beside itself (a run that starts logging there meanwhile
    makes itself a new one), then - unless BUSY() now names it (a run registered meanwhile: it is put back) -
    removed. True if it went."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return False
    aside = path.with_name(f".{path.name}.removing-{os.getpid()}")
    os.rename(path, aside)
    try:
        taken = busy is not None and os.path.realpath(path) in {os.path.realpath(p) for p in busy()}
    except (OSError, RuntimeError, TypeError, ValueError):
        taken = True  # which runs run could not be told: put back
    if taken:
        if not os.path.lexists(path):
            os.rename(aside, path)
        return False
    shutil.rmtree(aside, ignore_errors=True)
    return not os.path.lexists(aside)


def clear_caches(homes, dry_run=False):
    """Empty the caches (CACHES) of each HOME of HOMES (stopped kept starts): [{"home", "path", "bytes"}] of what
    went (or would go). A cache that is a link is left alone."""
    scan, found = Scan(), []
    for home in homes:
        home = Path(home)
        if home.is_symlink() or not home.is_dir():
            continue
        for relative in CACHES:
            path = home / relative
            if any(part.is_symlink() for part in [path, *list(path.parents)[:relative.count("/")]]) or \
                    not path.is_dir():
                continue
            size = scan.size(path)
            if not size:
                continue
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
                if path.exists():
                    size -= scan.size(path)
            found.append({"home": str(home), "path": relative, "bytes": size})
    return found


# -- the automatic pass -----------------------------------------------------------------------------------------

def auto_due(now=None, env=None):
    """Whether the daily pass is due: not switched off (SANDBOX_AI_AUTO_COMPACT=0) and not run within AUTO_EVERY."""
    env = os.environ if env is None else env
    if env.get(AUTO_VARIABLE, "") == "0":
        return False
    now = time.time() if now is None else now
    try:
        return now - os.stat(state_root(env) / AUTO_STAMP).st_mtime >= AUTO_EVERY
    except OSError:
        return state_root(env).is_dir()


def auto(spawn=subprocess.Popen, now=None, env=None):
    """After a start: the daily compaction in a process of its own (nice, in the background: the start never waits
    for it); True if one was started. The stamp is written first, so starts at the same time start only one."""
    if not auto_due(now, env):
        return False
    stamp = state_root(env) / AUTO_STAMP
    try:
        stamp.write_text(json.dumps({"started": iso(time.time() if now is None else now)}))
        spawn([sys.executable, "-I", str(HERE / "statedirs.py"), "--auto"], stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False


def auto_main():
    """`statedirs.py --auto`: the daily pass itself (gc's compaction, bounded to AUTO_SECONDS), its outcome in the
    stamp file and launcher.log."""
    with contextlib.suppress(OSError):
        os.nice(19)
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_ai_starts_for_statedirs", HERE / "starts.py")
    starts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(starts)
    result = starts.compact_logs(dry_run=False, budget=Budget(AUTO_SECONDS))
    try:  # the logs of the MCP servers whose log reader does not run (a running one rotates its own)
        mcp = compact_mcp()
    except OSError as exc:
        mcp = {"rotated": [], "compressed": [], "approvals": 0, "errors": [str(exc)]}
    result["compressed"] += [{"path": path} for path in mcp["rotated"] + mcp["compressed"]]
    result["errors"] += mcp["errors"]
    # the age limit of the logs (log_days; the default's grace counted - in the background it never starts it)
    try:
        aged = starts.expire_logs(shown=False)
    except (OSError, ValueError, RuntimeError) as exc:
        aged = {"removed": [], "freed": 0, "errors": [str(exc)]}
    result["errors"] += aged["errors"]
    # the default expiry of kept starts (none with keep_days 0; keep_days itself is applied at every start)
    try:
        expired = starts.expire_by_default()
    except (OSError, ValueError, RuntimeError) as exc:
        expired = {"removed": [], "expiring": [], "skipped": [{"name": "-", "reason": str(exc)}]}
    with contextlib.suppress(OSError):
        (state_root() / AUTO_STAMP).write_text(json.dumps(
            {"started": iso(time.time()), "compressed": len(result["compressed"]), "split": len(result["split"]),
             "dropped": len(result["dropped"]), "saved": result["saved"], "errors": result["errors"][:20],
             "unfinished": result["unfinished"], "old_logs": len(aged["removed"]), "old_logs_freed": aged["freed"],
             "expired": [e["name"] for e in expired["removed"]],
             "expiring": [e["name"] for e in expired.get("expiring") or []],
             "expiry_skipped": expired.get("skipped") or []}))
    if result["compressed"] or result["split"] or result["errors"]:
        starts.LOG.info("statedirs", f"daily compaction of the logs: {len(result['compressed'])} compressed, "
                                     f"{len(result['split'])} split, {human(result['saved'])} freed"
                        + (f"; {len(result['errors'])} left as they were" if result["errors"] else "")
                        + ("; the rest tomorrow" if result["unfinished"] else ""))
    if aged["removed"]:
        starts.LOG.info("statedirs", f"daily pass: removed {len(aged['removed'])} log file(s) older than "
                                     f"{aged.get('days')} days ({human(aged['freed'])}; log_days)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--auto", action="store_true", help="the daily compaction (started after a start)")
    if parser.parse_args().auto:
        sys.exit(auto_main())
    parser.print_help()
