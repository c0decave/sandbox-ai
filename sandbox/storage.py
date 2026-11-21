"""The kept starts' storage: transcripts compacted in place, the storage check (a plan of what can be freed), the
automatic rules and the growth history (user, 2026-11-19).

  sandbox-ai gc --compact-transcripts DAYS [--home NAME] [--dry-run] [--yes]   (starts.py)
  sandbox-ai gc --check [--home NAME] [--dry-run] [--json]                     (starts.py)

Transcripts. The agents' conversation files (Claude Code's ~/.claude/projects/*/*.jsonl and its subagents', Codex's
~/.codex/sessions and archived_sessions, Pi's ~/.pi/agent/sessions) last written more than DAYS days ago are
compressed in place: FILE.jsonl becomes FILE.jsonl.gz beside it - its marker: the suffix, the original's time and
mode kept, and a line in the start directory's transcripts.compacted.json. Never the newest file of a conversation
group (what resume continues: Claude's newest per project folder, Codex's newest of all, Pi's per folder) nor the
newest of its folder, never one that is not a regular file, never through a link. Each is written aside, read back
whole and compared (length and SHA-256) with what was read, then renamed into place - and only when the original is
still the same file, unchanged (inode, length, time), it goes; else the copy goes and the original stays. That makes
it safe for a running start too: its agent only appends to the newest file of a conversation, which is never taken,
and one written meanwhile is left as it was. sandbox-ai's readers (session files, its export, resume) read a
compacted one as if it were not; `gunzip FILE.jsonl.gz` puts one back by hand (resume does it for the newest of a
group when only a compacted one is left). OpenCode keeps its conversations in a SQLite database (or, in older
versions, thousands of small JSON files): neither is compacted - a database in use cannot be compressed in place and
small files take their blocks either way.

Why gzip: it is in every Python's standard library (the app's, an older host's) and zcat reads it; on real Claude
transcripts level 6 makes them 4.7 times smaller at 36 MB/s and reads them back at about 300 MB/s. xz (lzma, preset 1)
makes them 6.2 times smaller but writes at 14 MB/s and reads at 90 MB/s - the session file views read them back - and
zstd is in the standard library only from Python 3.14 on, so a host with an older Python could not read them.

The check measures what sandbox-ai keeps (starts.usage), names each thing that can be freed - what, how much, the
exact command, whether it is safe while starts run - and keeps a small history of each start's size per check
(state/sandbox-ai/storage-history.json) for the growth since the last one. Above the threshold (storage_threshold_gb,
5 GB) it says so in one line, and names a start that grew more than storage_growth_gb (2 GB) since the check before.

The automatic rules (defaults.json, each off unless set; Settings > Storage in the app): storage_pack_days (pack the
stopped kept starts unused for that many days), storage_caches_days (empty the download caches of the stopped kept
starts unused for that long), storage_compact_days (compact the transcripts older than that, also of a running start)
and storage_logs_days (remove the log folders nothing was written into for that long). The check applies them, and
gc when it runs; each action goes into launcher.log and is said in the next check's notice (storage.json).
Standard library only.
"""
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

LEVEL = 6                      # gzip's level for transcripts (see above)
SUFFIX = ".gz"
CHUNK = 1 << 20
MARK = "transcripts.compacted.json"   # in the start directory: what was compacted
MARK_LIMIT = 5000                     # entries kept in it (the newest)
HISTORY = "storage-history.json"      # in the state directory: each start's size per check
HISTORY_KEEP = 60                     # checks kept
STATE = "storage.json"                # in the state directory: the last check, what the rules did and was not said
COMPACT_DAYS = 30                     # the check recommends compacting transcripts older than this
REMOVE_DAYS = 90                      # a start's Storage view offers removing transcripts older than this
PACK_DAYS = 30                        # ... packing a stopped kept start unused for this long
LOG_FILE_DAYS = 14                    # ... removing its project's log files not written for this long
LOG_FOLDER_DAYS = 180                 # the check names the log folders not written for this long
PACK_RATIO = 0.5                      # what packing a HOME is said to save, at least (tar.zst/xz of mixed files)
CHECK_CHOICES = ("off", "weekly", "daily")
DEFAULTS = {"storage_check": "weekly", "storage_threshold_gb": 5, "storage_growth_gb": 2}
RULES = ("storage_caches_days", "storage_compact_days", "storage_pack_days", "storage_logs_days")
KEYS = (*DEFAULTS, *RULES)

# {folder below the HOME: (what, the depth below it whose folders are each one conversation group (0: all of it one),
# the files compacted: a regular expression on the path below the folder, without the .gz)}
PLACES = {
    ".claude/projects": ("Claude transcripts", 1, re.compile(r"[^/]+/[^/]+\.jsonl|[^/]+/[^/]+/subagents/agent-[^/]+\.jsonl")),
    ".codex/sessions": ("Codex transcripts", 0, re.compile(r"\d{4}/\d\d/\d\d/rollout-[^/]+\.jsonl")),
    ".codex/archived_sessions": ("Codex transcripts (archived)", 0, re.compile(r"rollout-[^/]+\.jsonl")),
    ".pi/agent/sessions": ("Pi transcripts", 1, re.compile(r"(?:[^/]+/)?[^/]+\.jsonl")),
}


class CompactError(Exception):
    """A transcript that was not compacted (or expanded): it is as it was."""


def human(size):
    """SIZE in bytes as 1.2 GiB, 340 MiB, 12 KiB or 5 B (statedirs.human)."""
    size = int(size or 0)
    for unit, shift in (("TiB", 40), ("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            value = size / (1 << shift)
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{size} B"


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def seconds(value):
    try:
        import calendar  # (only here: a polled `list --json` loads no calendar - test_performance_cli)
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def used(info):
    blocks = getattr(info, "st_blocks", None)
    return blocks * 512 if blocks is not None else info.st_size


def state_root(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def defaults_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"


# -- settings ------------------------------------------------------------------------------------------------------

def check_value(key, value):
    """Why VALUE is no value of the storage setting KEY (None: it is one)."""
    if key == "storage_check":
        return None if value in CHECK_CHOICES else f"{key} is one of {', '.join(CHECK_CHOICES)}"
    if key in ("storage_threshold_gb", "storage_growth_gb"):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 100_000
        return None if ok else f"{key} is a number of GB above 0"
    if key in RULES:
        ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 36500
        return None if ok else f"{key} is a number of days, 1 or more (leave it out: the rule is off)"
    return f"{key} is no storage setting"


def settings(env=None):
    """The storage settings of your defaults (defaults.json), each checked: a broken value counts as not set (the
    launcher refuses it at the next start). {key: value}, the rules None when off."""
    try:
        document = json.loads(defaults_path(env).read_text())
    except (OSError, ValueError, RecursionError):
        document = {}
    document = document if isinstance(document, dict) else {}
    found = {**DEFAULTS, **{key: None for key in RULES}}
    for key in KEYS:
        if key in document and check_value(key, document[key]) is None:
            found[key] = document[key]
    return found


# -- reading a transcript, compacted or not ----------------------------------------------------------------------

def compacted(name):
    """Whether the file NAME is a compacted transcript (its marker: the suffix)."""
    return str(name).endswith(".jsonl" + SUFFIX)


def plain_name(name):
    return str(name)[:-len(SUFFIX)] if compacted(name) else str(name)


def read_fd(fd, info, limit, name):
    """(bytes up to LIMIT, size) of the open file FD (stat INFO) named NAME: a compacted one decompressed - its size
    then the uncompressed one (gzip's trailer; read whole when it says more than 4 GiB may be there)."""
    if not compacted(name):
        os.lseek(fd, 0, os.SEEK_SET)
        chunks, left = [], min(info.st_size, limit)
        while left > 0:
            chunk = os.read(fd, min(left, CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks), info.st_size
    size = None
    if 18 <= info.st_size < 1 << 32:
        with contextlib.suppress(OSError):
            size = int.from_bytes(os.pread(fd, 4, info.st_size - 4), "little")
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.fdopen(os.dup(fd), "rb")
    try:
        with gzip.GzipFile(fileobj=raw, mode="rb") as unpacked:
            data = unpacked.read(limit)
            if size is None or size < len(data):
                size = len(data) + sum(len(chunk) for chunk in iter(lambda: unpacked.read(CHUNK), b""))
    except (OSError, EOFError, ValueError) as exc:
        raise OSError(f"{name}: the compacted transcript cannot be read ({exc})") from None
    finally:
        raw.close()
    return data, size


def copy_fd(fd, name, write):
    """The whole content of the open file FD named NAME to WRITE(chunk) (a compacted one decompressed)."""
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.fdopen(os.dup(fd), "rb")
    try:
        source = gzip.GzipFile(fileobj=raw, mode="rb") if compacted(name) else raw
        for chunk in iter(lambda: source.read(CHUNK), b""):
            write(chunk)
    except (EOFError, ValueError) as exc:
        raise OSError(f"{name}: the compacted transcript cannot be read ({exc})") from None
    finally:
        raw.close()


# -- compacting -----------------------------------------------------------------------------------------------------

def inside(home, relative):
    """HOME/RELATIVE when no part of it is a link (else None)."""
    path = Path(home)
    if path.is_symlink() or not path.is_dir():
        return None
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink() or not os.path.lexists(path):
            return None
    return path


def transcripts(home, relative):
    """{group: [(mtime, path, stat)]} of the transcripts below HOME/RELATIVE (PLACES), plain and compacted; never
    through a link."""
    top = inside(home, relative)
    groups = {}
    if top is None or not top.is_dir():
        return groups
    _, depth, pattern = PLACES[relative]
    for folder, dirs, names in os.walk(top, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(folder, name))]
        below = Path(folder).relative_to(top).parts
        for name in names:
            path = os.path.join(folder, name)
            if not pattern.fullmatch("/".join([*below, plain_name(name)])):
                continue
            with contextlib.suppress(OSError):
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode):
                    group = below[:depth] if depth else ()
                    groups.setdefault(group, []).append((info.st_mtime, path, info))
    return groups


def candidates(home, days, now=None, places=PLACES):
    """[{"place", "what", "path" (absolute), "bytes"}] of the plain transcripts of HOME last written more than DAYS
    days ago that compacting takes: never the newest of its group, nor (Claude, Pi) of its folder - a subagent's."""
    cutoff = (time.time() if now is None else now) - days * 86400
    found = []
    for relative in places:
        for members in transcripts(home, relative).values():
            members.sort(key=lambda member: (member[0], member[1]))
            newest_of_folder = {}
            for when, path, _ in members:
                newest_of_folder[os.path.dirname(path)] = path
            by_folder = PLACES[relative][1] > 0  # (Codex: one group of date folders - only its newest of all)
            for when, path, info in members[:-1]:
                if when < cutoff and not compacted(path) and not (
                        by_folder and newest_of_folder[os.path.dirname(path)] == path):
                    found.append({"place": relative, "what": PLACES[relative][0], "path": path, "bytes": used(info)})
    return found


def same(before, after):
    return (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) == \
        (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)


def compact_file(path, level=LEVEL, written=None):
    """PATH (a plain transcript) compressed in place: PATH.gz written aside, read back whole and compared, renamed
    into place - and PATH removed only while it is still the same file, unchanged. (bytes it took, bytes the
    compacted one takes). CompactError: nothing changed. WRITTEN(): called once the copy is written (tests: an agent
    that appends meanwhile)."""
    path = Path(path)
    target = path.with_name(path.name + SUFFIX)
    temp = path.with_name(f".{path.name}{SUFFIX}.{os.getpid()}.tmp")
    if os.path.lexists(target):
        raise CompactError(f"{target.name} is there already")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise CompactError(f"cannot be read: {exc}") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise CompactError("not a regular file")
        digest, length = hashlib.sha256(), 0
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                      0o600)
        try:
            with os.fdopen(out, "wb") as raw:
                with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw, compresslevel=level,
                                   mtime=int(before.st_mtime)) as packed:
                    while True:
                        chunk = os.read(fd, CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                        length += len(chunk)
                        packed.write(chunk)
                raw.flush()
                os.fsync(raw.fileno())
            if length != before.st_size:
                raise CompactError("it changed while it was read")
            check, back = hashlib.sha256(), 0
            with gzip.open(temp, "rb") as reread:
                for chunk in iter(lambda: reread.read(CHUNK), b""):
                    check.update(chunk)
                    back += len(chunk)
            if back != length or check.digest() != digest.digest():
                raise CompactError("the compressed copy did not read back the same")
            if written is not None:
                written()
            if not same(before, os.lstat(path)):
                raise CompactError("it was written meanwhile: left as it was")
            os.chmod(temp, stat.S_IMODE(before.st_mode))
            os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.link(temp, target)  # (never over a file that is there)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        os.unlink(temp)
        try:
            if not same(before, os.lstat(path)):
                raise CompactError("it was written meanwhile: left as it was")
        except (CompactError, OSError):
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
        os.unlink(path)
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        return used(before), used(os.lstat(target))
    except OSError as exc:
        raise CompactError(str(exc)) from None
    finally:
        os.close(fd)


def expand_file(path):
    """The compacted transcript PATH (FILE.jsonl.gz) put back as FILE.jsonl: written aside, compared, renamed into
    place, then the compacted one removed. The path of the plain one. CompactError: nothing changed."""
    path = Path(path)
    target = path.with_name(plain_name(path.name))
    temp = path.with_name(f".{target.name}.{os.getpid()}.tmp")
    if not compacted(path.name) or os.path.lexists(target):
        raise CompactError(f"{target.name} is there already" if os.path.lexists(target) else "not compacted")
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise CompactError("not a regular file")
        digest, length = hashlib.sha256(), 0
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, "wb") as raw, gzip.open(path, "rb") as source:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    digest.update(chunk)
                    length += len(chunk)
                    raw.write(chunk)
                raw.flush()
                os.fsync(raw.fileno())
            check = hashlib.sha256()
            with open(temp, "rb") as reread:
                for chunk in iter(lambda: reread.read(CHUNK), b""):
                    check.update(chunk)
            if check.digest() != digest.digest() or os.path.getsize(temp) != length:
                raise CompactError("the copy did not read back the same")
            os.chmod(temp, stat.S_IMODE(before.st_mode))
            os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.link(temp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        os.unlink(temp)
        os.unlink(path)
        return target
    except (OSError, EOFError, ValueError) as exc:
        raise CompactError(str(exc)) from None


def compact_home(home, days, dry_run=False, now=None, deadline=None, written=None):
    """The transcripts of HOME older than DAYS days compacted (candidates; DRY_RUN: only listed). {"files", "bytes"
    (what they take now), "saved" (DRY_RUN: about, by a sample), "items": [{"what", "path" (in the HOME), "bytes"}],
    "errors": [{"path", "reason"}], "unfinished"}; each one compacted goes into the start directory's MARK."""
    home = Path(home)
    found = candidates(home, days, now)
    result = {"files": 0, "bytes": 0, "saved": 0, "items": [], "errors": [], "unfinished": False}
    if dry_run:
        ratio = sample_ratio([item["path"] for item in found])
        result.update(files=len(found), bytes=sum(item["bytes"] for item in found),
                      items=[{"what": item["what"], "path": os.path.relpath(item["path"], home),
                              "bytes": item["bytes"]} for item in found])
        result["saved"] = int(result["bytes"] * (1 - 1 / ratio)) if ratio else 0
        return result
    done = []
    for item in found:
        if deadline is not None and time.monotonic() > deadline:
            result["unfinished"] = True
            break
        relative = os.path.relpath(item["path"], home)
        try:
            before, after = compact_file(item["path"], written=written)
        except CompactError as exc:
            result["errors"].append({"path": relative, "reason": str(exc)})
            continue
        result["files"] += 1
        result["bytes"] += before
        result["saved"] += max(0, before - after)
        result["items"].append({"what": item["what"], "path": relative, "bytes": before})
        done.append({"path": relative + SUFFIX, "bytes": before, "compacted_bytes": after, "time": iso(time.time())})
    if done:
        record(home.parent, done)
    return result


def sample_ratio(paths, sample=1 << 20):
    """How many times smaller gzip makes the transcripts PATHS, measured on a sample of the biggest; None: none."""
    best = None
    for path in paths:
        with contextlib.suppress(OSError):
            size = os.lstat(path).st_size
            if best is None or size > best[0]:
                best = (size, path)
    if best is None:
        return None
    try:
        with open(best[1], "rb") as source:
            source.seek(max(0, (best[0] - sample) // 2))
            data = source.read(sample)
    except OSError:
        return None
    return len(data) / max(1, len(gzip.compress(data, LEVEL))) if data else None


def record(directory, done):
    """DONE (compacted transcripts) added to the start directory's MARK."""
    path = Path(directory) / MARK
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        found = {}
    files = found.get("files") if isinstance(found, dict) and isinstance(found.get("files"), list) else []
    files = (files + done)[-MARK_LIMIT:]
    document = {"format": "gzip", "level": LEVEL, "files": files,
                "saved": sum(max(0, e.get("bytes", 0) - e.get("compacted_bytes", 0)) for e in files
                             if isinstance(e, dict) and isinstance(e.get("bytes"), int)
                             and isinstance(e.get("compacted_bytes"), int))}
    write_json(path, document)


def marked(directory):
    """{"files" (count), "saved"} of the start directory's MARK ({} without one)."""
    try:
        found = json.loads((Path(directory) / MARK).read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(found, dict) or not isinstance(found.get("files"), list):
        return {}
    return {"files": len(found["files"]), "saved": found.get("saved") if isinstance(found.get("saved"), int) else 0}


def ensure_continuable(home):
    """Before a start continues its conversation (resume): the newest transcript of each group plain again where only
    a compacted one is left (the agent continues the newest). [path in the HOME] of those put back."""
    home = Path(home)
    restored = []
    for relative in PLACES:
        for members in transcripts(home, relative).values():
            newest = max(members, key=lambda member: (member[0], member[1]))
            if compacted(newest[1]):
                with contextlib.suppress(CompactError):
                    restored.append(os.path.relpath(expand_file(newest[1]), home))
    return restored


def write_json(path, document):
    path = Path(path)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(document, indent=1))
    os.replace(temp, path)


def read_state(name, env=None):
    try:
        found = json.loads((state_root(env) / name).read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return found if isinstance(found, dict) else {}


# -- the growth history ---------------------------------------------------------------------------------------------

def history(env=None):
    """[{"time", "starts": {id: {"name", "bytes"}}}] of the checks, oldest first."""
    checks = read_state(HISTORY, env).get("checks")
    return [entry for entry in checks if isinstance(entry, dict) and isinstance(entry.get("starts"), dict)] \
        if isinstance(checks, list) else []


def growth(sizes, before):
    """{id: {"name", "bytes", "previous", "growth", "since"}} of SIZES ({id: (name, bytes)}) against the check
    BEFORE (None: no growth known)."""
    old = (before or {}).get("starts") or {}
    found = {}
    for key, (name, size) in sizes.items():
        entry = old.get(key) if isinstance(old.get(key), dict) else {}
        previous = entry.get("bytes") if isinstance(entry.get("bytes"), int) else None
        found[key] = {"name": name, "bytes": size, "previous": previous,
                      "growth": size - previous if previous is not None else None,
                      "since": (before or {}).get("time") if previous is not None else None}
    return found


def remember(sizes, now=None, env=None):
    """This check's SIZES ({id: (name, bytes)}) added to the history (the oldest beyond HISTORY_KEEP go)."""
    checks = history(env) + [{"time": iso(time.time() if now is None else now),
                              "starts": {key: {"name": name, "bytes": size} for key, (name, size) in sizes.items()}}]
    root = state_root(env)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / HISTORY, {"checks": checks[-HISTORY_KEEP:]})


def last_sizes(env=None):
    """{id: {"bytes", "growth", "measured"}} of the last check - what `sandbox-ai list --json` says of each start
    ("storage"), cheap: one small file."""
    checks = history(env)
    if not checks:
        return {}
    last, before = checks[-1], checks[-2] if len(checks) > 1 else None
    found = {}
    for key, entry in last["starts"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("bytes"), int):
            continue
        old = ((before or {}).get("starts") or {}).get(key)
        previous = old.get("bytes") if isinstance(old, dict) and isinstance(old.get("bytes"), int) else None
        found[key] = {"bytes": entry["bytes"], "growth": entry["bytes"] - previous if previous is not None else None,
                      "measured": last.get("time"), "since": (before or {}).get("time") if previous is not None
                      else None}
    return found


# -- what the rules did, said at the next check ----------------------------------------------------------------------

def note_actions(actions, env=None):
    """ACTIONS (the rules') kept until a check says them."""
    if not actions:
        return
    found = read_state(STATE, env)
    unsaid = found.get("unsaid") if isinstance(found.get("unsaid"), list) else []
    found["unsaid"] = (unsaid + actions)[-200:]
    root = state_root(env)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / STATE, found)


def take_unsaid(env=None, keep=False):
    """The rules' actions not said yet (KEEP: they stay unsaid)."""
    found = read_state(STATE, env)
    unsaid = found.get("unsaid") if isinstance(found.get("unsaid"), list) else []
    if unsaid and not keep:
        found["unsaid"] = []
        write_json(state_root(env) / STATE, found)
    return [entry for entry in unsaid if isinstance(entry, dict)]


def remember_check(summary, env=None):
    found = read_state(STATE, env)
    found["last_check"] = summary
    root = state_root(env)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / STATE, found)


def notice(total, threshold, grew, actions, where="here"):
    """The check's one line (None: nothing to say): what could be freed above the threshold, the starts that grew,
    what the rules did."""
    parts = []
    if total >= threshold:
        parts.append(f"{human(total)} could be freed {where} - sandbox-ai gc --check shows how")
    if grew:
        parts.append("grew since the last check: " + ", ".join(f"{name} +{human(size)}" for name, size in grew[:5]))
    if actions:
        parts.append("the storage rules " + "; ".join(action_text(action) for action in actions[:6])
                     + (f" and {len(actions) - 6} more" if len(actions) > 6 else ""))
    return "; ".join(parts) or None


ACTION_VERBS = {"caches": "emptied the download caches of {name}", "compact": "compacted {files} transcript(s) of "
                "{name}", "pack": "packed the HOME of {name}", "logs": "removed the log folder {name}"}


def action_text(action):
    text = ACTION_VERBS.get(action.get("rule"), "{name}").format(name=action.get("name", "?"),
                                                                 files=action.get("files", "?"))
    return text + (f" ({human(action['bytes'])})" if isinstance(action.get("bytes"), int) and action["bytes"] else "")
