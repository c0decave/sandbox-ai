#!/usr/bin/env python3
"""sandbox-ai llm traffic: what an agent and its model exchanged, request by request - the messages, the system
part, the tools, the tool calls and their results as they went to the model (after the relay's changes, which are
listed), and the answer put together from its stream: text, reasoning, tool calls, finish reason, tokens.

  sandbox-ai llm traffic                      the newest run of this project's log directory
  sandbox-ai llm traffic RUN                  a run (its id, or the name of a start)
  sandbox-ai llm traffic --session NAME       a session's runs
  sandbox-ai llm traffic --runs               the runs with an LLM traffic log
  sandbox-ai llm traffic --request N          one request (with --full: the whole conversation)

Each request shows what is new since the previous request of its conversation (the messages appended) and the
answer; --full shows every message. --json gives the records (with --request: the record and its readable view),
--summary one line per request, --markdown a report. The log is written by the connection proxy while a sandbox runs
(llm-traffic-TIME-RUN.jsonl beside connections-TIME-RUN.jsonl, mode 0600): by default the requests to the --llm
server; with --llm-traffic all also the agent's cloud provider. It holds the conversations themselves: credentials are
redacted, nothing else is.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import zlib

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
_modules = {}

# .gz: compressed by sandbox-ai gc once its run ended (read as it is)
FILE = re.compile(r"llm-traffic-(\d{8}-\d{6})-([A-Za-z0-9._-]{1,128}?)\.jsonl(?:\.(\d{1,3}))?(?:\.gz)?")
LINE_LIMIT = (64 << 20) + (2 << 20)   # a record longer than this is skipped (the limit of one is 64 MiB)
READ_BUDGET = 1 << 30                 # bytes read for one listing at most (the newest kept)
TAIL_CHUNK = 4 << 20
LOOK_BACK = 20                        # requests looked at for the one a request continues
CUT = 1200                            # characters of one message shown without --full
POLL = 0.5                            # seconds between two looks with --follow
CONVERSATION_KEYS = ("messages", "input", "prompt", "system", "instructions", "tools")


def module(name):
    """A sibling module of sandbox-ai, loaded once."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_llm_traffic", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def safe(value, newlines=False):
    """Text of the log for a terminal: what the model or the agent wrote may hold control characters (escape
    sequences); each is shown escaped, newlines and tabs kept with NEWLINES. A text with nothing to escape (nearly
    all of them) is known as such in one look: character by character in Python, the megabytes of a transcript were
    two thirds of the command's time."""
    text = str(value)
    if (text.replace("\n", "").replace("\t", "") if newlines else text).isprintable():
        return text
    return "".join(c if c.isprintable() or (newlines and c in "\n\t") else
                   f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}" for c in text)


# -- files and runs ------------------------------------------------------------------------------------------------

def run_files(directory):
    """{run: {"run", "started" (TIME of its name), "paths" (oldest part first), "bytes", "modified"}} of the LLM
    traffic logs in DIRECTORY."""
    found = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return {}
    for entry in entries:
        match = FILE.fullmatch(entry.name)
        if not match:
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        stamp, run, part = match.group(1), match.group(2), int(match.group(3) or 0)
        slot = found.setdefault(run, {"run": run, "started": stamp, "parts": [], "bytes": 0, "modified": 0.0})
        slot["started"] = min(slot["started"], stamp)
        slot["parts"].append((stamp, -part, Path(entry.path)))
        slot["bytes"] += info.st_size
        slot["modified"] = max(slot["modified"], info.st_mtime)
    for slot in found.values():
        slot["paths"] = [path for _, _, path in sorted(slot.pop("parts"), key=lambda p: (p[0], p[1]))]
    return found


def lifetime(path):
    """(run, TIME) of the log PATH's name - one proxy's life: its requests are numbered from 1, and a request written
    as a delta continues one of the same life only -, or None for another name."""
    match = FILE.fullmatch(Path(path).name)
    return (match.group(2), match.group(1)) if match else None


def started_text(stamp):
    """2026-11-13 10:00:01 of a file name's 20261113-100001 (UTC)."""
    if not re.fullmatch(r"\d{8}-\d{6}", str(stamp)):
        return str(stamp)
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"


def opened(path):
    """PATH to read from its start: a compressed log (NAME.gz) decompressed as it is read."""
    tail = module("jsonltail")
    return tail.open_compressed(path) if tail.compressed(path) else open(path, "rb")


def first_record(path):
    """The first record of PATH (its run's facts: session, mode), or None."""
    try:
        with opened(path) as stream:
            line = stream.readline(LINE_LIMIT)
        record = json.loads(line)
    except (OSError, ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def runs_in(directory, with_session=True):
    """The runs with an LLM traffic log in DIRECTORY, newest first: {"run", "started", "bytes", "modified",
    "files", "session", "running"}."""
    found = sorted(run_files(directory).values(), key=lambda r: (r["started"], r["modified"]), reverse=True)
    try:
        running = set(module("net").running())
    except Exception:  # noqa: BLE001 - no registry: nothing known to run
        running = set()
    rows = []
    for slot in found:
        first = first_record(slot["paths"][0]) if with_session else None
        rows.append({"run": slot["run"], "started": started_text(slot["started"]), "bytes": slot["bytes"],
                     "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(slot["modified"])),
                     "files": [str(p) for p in slot["paths"]],
                     "session": first.get("session") if isinstance(first, dict) else None,
                     "running": slot["run"] in running})
    return rows


def logs_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"


def find_run_directory(run):
    """The log directory with RUN's LLM traffic log: the running sandbox's, else one of the default log
    directories; None if none has one."""
    try:
        record = module("net").running().get(run)
    except Exception:  # noqa: BLE001
        record = None
    if isinstance(record, dict) and isinstance(record.get("log_dir"), str):
        return Path(record["log_dir"])
    try:
        directories = sorted((p for p in logs_root().iterdir() if p.is_dir() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for directory in directories:
        if run in run_files(directory):
            return directory
    return None


def locate(a):
    """(log directory, [run]) the arguments A choose: RUN, --session, --log-dir, --project, else this project's
    newest run. [] with --runs (every run of the directory listed)."""
    activity = module("activity")
    run = activity.run_id(a.run) if a.run else None
    if a.log_dir:
        directory = Path(a.log_dir).expanduser()
    elif a.session:
        directory = activity.session_log_dir(a.session)
    elif a.project:
        directory = activity.default_log_dir(Path(a.project).expanduser().resolve())
    elif run:
        directory = find_run_directory(run)
        if directory is None:
            raise ValueError(f"no LLM traffic log of a run {safe(run)!r} found (a run with --log-dir elsewhere: add "
                             "--log-dir)")
    else:
        directory = activity.default_log_dir(Path(os.getcwd()).resolve())
    if getattr(a, "runs", False):
        return directory, []
    files = run_files(directory)
    if run:
        if run not in files and not getattr(a, "follow", False):  # (--follow: it may be written yet)
            raise ValueError(f"no LLM traffic log of the run {safe(run)} in {safe(directory)}"
                             + (f"; the runs with one there: {safe(', '.join(sorted(files)[:10]))}" if files else
                                " (it is written while a sandbox with --llm runs, or with --llm-traffic all)"))
        return directory, [run]
    if a.session:
        key = activity.session_key(a.session)
        try:
            sessions = module("sessions")
            current = sessions.current_run(key, sessions.load(key))
        except (OSError, ValueError):
            current = f"session-{key}"
        chosen = [r for r in sorted(files, key=lambda r: files[r]["started"])
                  if r == current or (first_record(files[r]["paths"][0]) or {}).get("session") == key]
        if not chosen:
            raise ValueError(f"the session {safe(a.session)} has no LLM traffic log in {safe(directory)} yet")
        return directory, chosen
    if not files:
        if getattr(a, "follow", False):
            return directory, []
        raise ValueError(f"no LLM traffic log in {safe(directory)} (it is written while a sandbox with --llm runs, "
                         "or with --llm-traffic all)")
    newest = max(files.values(), key=lambda r: (r["started"], r["modified"]))
    return directory, [newest["run"]]


# -- reading ---------------------------------------------------------------------------------------------------------

def parse(line):
    try:
        record = module("jsonltail").loads(line)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) and record.get("version") == 1 else None


def paths_of(directory, runs):
    files = run_files(directory)
    return [path for run in runs if run in files for path in files[run]["paths"]]


def read_records(paths, grep=None, budget=READ_BUDGET, expander=None):
    """The records of PATHS in order (a damaged or overlong line skipped), each whole (EXPANDER: an Expander - a record
    written as a delta put together again): at most BUDGET bytes, the newest kept (the oldest files are skipped
    first). GREP: only lines that hold this text (case does not count)."""
    expander = expander if expander is not None else Expander(finder(paths))
    sizes = []
    for path in paths:
        try:
            sizes.append(os.stat(path).st_size)
        except OSError:
            sizes.append(0)
    skip, total = 0, sum(sizes)
    while total > budget and skip < len(paths) - 1:
        total -= sizes[skip]
        skip += 1
    needle = grep.lower().encode("utf-8") if grep else None
    for path in paths[skip:]:
        expander.enter(lifetime(path))
        try:
            stream = opened(path)
        except OSError:
            continue
        with stream, contextlib.suppress(EOFError, zlib.error):  # a damaged compressed log: what was read
            while True:
                line = stream.readline(LINE_LIMIT)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) >= LINE_LIMIT:  # overlong: the rest of it is skipped
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(LINE_LIMIT)
                    continue
                if needle is not None and needle not in line.lower():
                    expander.note(line)  # (parsed only for a delta after it that needs it)
                    continue
                record = parse(line)
                if record is not None:
                    yield expander.expand(record, line=line)


def tail_records(paths, count, expander=None):
    """The last COUNT records of PATHS, oldest first, read from the end, each whole (EXPANDER: an Expander): the
    records those written as deltas continue are read too (older ones, as far back as they go), not returned. A
    request continues one of its own log only (lifetime: PATHS may hold several runs, a session's - each numbers its
    requests from 1; before, a request of an earlier run was put together with the later run's request of that
    number)."""
    tail = module("jsonltail")
    found, numbers, needed, extra, life = [], set(), set(), [0], [None]

    def take(record):
        """Keep RECORD if it is one of the last COUNT or one they need; whether to read on."""
        number = record.get("n")
        if len(found) < count or number in needed:
            found.append((life[0], record))
            numbers.add(number)
            delta = record.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("of"), int) and delta["of"] not in numbers:
                needed.add(delta["of"])
        missing = needed - numbers
        if len(found) >= count and missing and isinstance(number, int) and number < min(missing):
            return False  # past them (records are written in order): not in the log any more
        return len(found) < count or bool(missing)

    going = True
    for path in reversed(paths):
        if lifetime(path) != life[0] or not found:  # another run's log: its numbers are its own
            if len(found) >= count:
                break  # (what the last ones still miss is in no other log)
            life[0], going = lifetime(path), True
            numbers.clear()
            needed.clear()
        if not going:
            continue
        if tail.compressed(path):  # read from its start: only the newest lines are kept
            lines, _, _ = tail.compressed_tail(path, READ_BUDGET, max(0, count - len(found)), LINE_LIMIT)
            older = None
            while True:
                for line in reversed(lines):
                    record = parse(line)
                    if record is not None and not take(record):
                        going = False
                        break
                if not going or older is not None or not needed - numbers:
                    break
                # a delta among them continues a record before those lines: the part again, more of it (a log of
                # whole records never reads it twice)
                older, _, _ = tail.compressed_tail(path, FIND_BYTES, None, LINE_LIMIT)
                lines = older[:max(0, len(older) - len(lines))]
            continue
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            continue
        try:
            end = tail.complete_end(fd, os.fstat(fd).st_size, TAIL_CHUNK)
            for line in tail.lines_from_end(fd, end, TAIL_CHUNK, LINE_LIMIT):
                if len(found) >= count:  # (only the records the last ones continue: those lines alone parsed)
                    extra[0] += len(line)
                    number = record_number(line)
                    if extra[0] > FIND_BYTES or (number is not None and needed - numbers and
                                                 number < min(needed - numbers)):
                        going = False
                        break
                    if number is not None and number not in needed:
                        continue
                record = parse(line)
                if record is not None and not take(record):
                    going = False
                    break
        finally:
            os.close(fd)
    found.reverse()
    expander = expander if expander is not None else Expander(finder(paths))
    whole = []
    for scope, record in found:
        expander.enter(scope)
        whole.append(expander.expand(record))
    return whole[-count:] if count else []


# -- records written as deltas (runtime.TrafficDeltas): put together again ------------------------------------------

BASES_KEPT = 32  # records an Expander keeps whole for those that continue them (the writer refers to its last 16)
FIND_BYTES = 256 << 20  # bytes of the run's parts read to find a record a delta continues


class Expander:
    """Each record of the LLM traffic log whole, as it was before it was written as a delta: a record with "delta"
    ({"of": the request it continues, "key", "kept", "same", "digests", "order", "items"}) gets back what it left out -
    the first "kept" items of that request's messages (input), those "items" names after them (laid_out), its fields
    named in "same", its first "digests" digests -
    from the records read before it (the last BASES_KEPT), else from LOOKUP(n) (a record of the run's parts, whole or
    a delta itself). One whose request cannot be found keeps only what was new, with a first item that says how many
    are missing, and "delta": {..., "missing": True}. A whole record is kept as its line until a delta needs it (a log
    of whole records - before 0.1.98's review 2 - costs no more memory than before); one put together shares the items
    of the one it continues."""

    def __init__(self, lookup=None):
        self.known, self.lookup, self.depth, self.scope = collections.OrderedDict(), lookup, 0, None

    def enter(self, scope):
        """The records that follow are of the log SCOPE (lifetime(path)): another one than before forgets the records
        kept - requests are numbered per log, and one written as a delta continues a request of its own log only."""
        if scope != self.scope:
            self.known.clear()
            self.scope = scope

    def keep(self, number, entry):
        self.known[number] = entry
        self.known.move_to_end(number)
        while len(self.known) > BASES_KEPT:
            self.known.popitem(last=False)

    def note(self, line):
        """LINE, a line of the log read but not parsed (--grep): kept for a delta after it that needs it."""
        number = record_number(line)
        if number is not None:
            self.keep(number, line)

    def remember(self, record, line=None):
        number = record.get("n")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(record.get("request"), dict):
            return
        if line is not None and "delta" not in record:
            self.keep(number, line)  # (parsed again only for a delta that continues it)
        else:
            self.keep(number, (record["request"], record.get("digests") if isinstance(record.get("digests"), list)
                               else []))

    def base(self, number):
        if number in self.known:
            entry = self.known[number]
            if not isinstance(entry, bytes):
                return entry
            found = parse(entry)
            if found is None or found.get("n") != number:
                return None
            whole = self.expand(found, remember=False)
            if whole.get("delta", {}).get("missing") or not isinstance(whole.get("request"), dict):
                return None
            entry = (whole["request"], whole.get("digests") if isinstance(whole.get("digests"), list) else [])
            self.known[number] = entry
            return entry
        if self.lookup is None or self.depth > 2 * BASES_KEPT + 64:  # (a chain is at most runtime.LLM_TRAFFIC_CHAIN)
            return None
        found = self.lookup(number, self.scope)
        if not isinstance(found, dict) or found.get("n") != number:
            return None
        self.depth += 1
        try:
            whole = self.expand(found, remember=False)
        finally:
            self.depth -= 1
        if whole.get("delta", {}).get("missing") or not isinstance(whole.get("request"), dict):
            return None
        return whole["request"], whole.get("digests") if isinstance(whole.get("digests"), list) else []

    def expand(self, record, remember=True, line=None):
        """RECORD whole (a new dict if it was a delta); remembered for the records after it (LINE: the line it was
        read from)."""
        delta = record.get("delta")
        if isinstance(delta, dict) and isinstance(record.get("request"), dict):
            record = self.joined(record, delta)
            line = None
        if remember:
            self.remember(record, line)
        return record

    def joined(self, record, delta):
        key = delta.get("key") if delta.get("key") in ("messages", "input") else "messages"
        kept = delta.get("kept") if isinstance(delta.get("kept"), int) and delta["kept"] >= 0 else 0
        same = [name for name in delta.get("same") or [] if isinstance(name, str)]
        prefix = delta.get("digests") if isinstance(delta.get("digests"), int) and delta["digests"] >= 0 else 0
        own = record["request"]
        items = own.get(key) if isinstance(own.get(key), list) else []
        digests = record.get("digests") if isinstance(record.get("digests"), list) else []
        number = record.get("n")
        earlier = not isinstance(number, int) or (isinstance(delta.get("of"), int) and delta["of"] < number)
        found = self.base(delta.get("of")) if earlier else None  # (never a later request: another log's)
        if found is None:
            request = dict(own)
            request[key] = ([{"sandbox_ai_omitted": f"{kept} earlier items: the request #{delta.get('of')} this one "
                                                    "continues is not in what was read of the log"}] if kept else []) \
                + items
            return {**record, "request": request, "delta": {**delta, "missing": True}}
        base, base_digests = found
        request = {}
        order = [name for name in delta.get("order") or [] if isinstance(name, str)] or list(own)
        for name in order + [name for name in own if name not in order]:
            if name == key:
                earlier = base.get(key) if isinstance(base.get(key), list) else []
                request[key] = earlier[:kept] + self.laid_out(delta.get("items"), earlier, items, record)
            elif name in same and name not in own:
                if name in base:
                    request[name] = base[name]
            elif name in own:
                request[name] = own[name]
        whole = {}
        for name, value in record.items():
            if name == "delta":
                continue
            whole[name] = base_digests[:prefix] + digests if name == "digests" else request if name == "request" \
                else value
        return whole


    @staticmethod
    def laid_out(layout, earlier, items, record):
        """What follows a delta's first items: ITEMS (those written in RECORD) and those of EARLIER (the request it
        continues) LAYOUT names - each entry a number of ITEMS or [where, how many] of EARLIER. ITEMS alone without a
        layout, with one that does not fit (damaged), and for a record cut to its limit (its older items left out:
        "truncated" says so)."""
        if not isinstance(layout, list):
            return items
        built, taken = [], 0
        for entry in layout:
            if isinstance(entry, int) and not isinstance(entry, bool) and entry >= 0:
                built += items[taken:taken + entry]
                taken += entry
            elif isinstance(entry, list) and len(entry) == 2 and all(
                    isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in entry) and \
                    sum(entry) <= len(earlier):
                built += earlier[entry[0]:sum(entry)]
            else:
                return items
        return built if taken == len(items) else items


def record_number(line):
    """The request number a line of the log begins with ({"version":1,"n":N,...: how the proxy writes them}), or
    None."""
    match = re.match(rb'\{"version":1,"n":(\d+),', line)
    return int(match.group(1)) if match else None


def finder(paths, budget=FIND_BYTES):
    """LOOKUP for an Expander: the record with a number in the run's PATHS (oldest part first; SCOPE: only those of
    that log, lifetime), read from the part it must be in (the last whose first record is not after it) - at most
    BUDGET bytes of it."""
    firsts = {}

    def first_number(path):
        if path not in firsts:
            record = first_record(path)
            firsts[path] = record.get("n") if isinstance(record, dict) and isinstance(record.get("n"), int) else None
        return firsts[path]

    def lookup(number, scope=None):
        candidates = [path for path in paths if (scope is None or lifetime(path) == scope)
                      and (first_number(path) or 0) <= number]
        for path in reversed(candidates[-2:]):
            try:
                stream = opened(path)
            except OSError:
                continue
            read = 0
            with stream, contextlib.suppress(EOFError, zlib.error, OSError):
                while read < budget:
                    line = stream.readline(LINE_LIMIT)
                    if not line:
                        break
                    read += len(line)
                    found = record_number(line)
                    if found == number:
                        return parse(line)
                    if found is not None and found > number:
                        break
        return None
    return lookup


class Thread:
    """Which earlier request each request continues (its messages begin with that one's), from their digests: the
    best of the last LOOK_BACK requests of the same API."""

    def __init__(self):
        self.recent = collections.deque(maxlen=LOOK_BACK)

    def new_from(self, record):
        """(index of the first message new in RECORD, the request's n it continues or None); index 0: all new."""
        digests = record.get("digests") if isinstance(record.get("digests"), list) else None
        best, number = 0, None
        if digests:
            for n, api, earlier in reversed(self.recent):
                if api != record.get("api") or not earlier or earlier[0] != digests[0]:
                    continue
                common = 0
                for mine, theirs in zip(digests[1:], earlier[1:]):
                    if mine != theirs:
                        break
                    common += 1
                if common > best:
                    best, number = common, n
            self.recent.append((record.get("n"), record.get("api"), digests))
        return best, number


# -- the readable view -----------------------------------------------------------------------------------------------

TEXT_DEPTH = 32  # levels of parts within parts read for a text; deeper (only an agent's own odd request): named


def text_of(value, depth=0):
    """The text of a content value of any of the APIs: a string, or parts (text, images and files named). Always a
    string, whatever the agent sent (the request is its own JSON: a number, parts nested hundreds of levels)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if depth >= TEXT_DEPTH and isinstance(value, (dict, list)):
        return "[nested too deep]"
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(value.get("text"), str):
            return value["text"]
        if kind in ("image", "image_url", "input_image"):
            return "[image]"
        if kind in ("document", "file", "input_file"):
            return "[file]"
        if kind == "input_audio":
            return "[audio]"
        for key in ("content", "output"):
            if key in value:
                return text_of(value[key], depth + 1)
        return compact(value)[:2000]
    if isinstance(value, list):
        return "\n".join(text for text in (text_of(part, depth + 1) for part in value) if text)
    return compact(value)


def listed(value):
    """VALUE where it is a list, else none (a request's tool_calls may be anything the agent sent)."""
    return value if isinstance(value, list) else []


def compact(value):
    """VALUE as JSON text; a value too deep for that is named, not shown."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (ValueError, RecursionError):
        return "[nested too deep]"


def arguments_text(value):
    """A tool call's arguments as compact JSON (a string of JSON parsed first)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return value
    return compact(value)


def entry(role, kind, text, index, **more):
    """One item of the view; its TEXT always a string (text_of what is none)."""
    return {"role": role, "kind": kind, "text": text if isinstance(text, str) else text_of(text), "message": index,
            **{key: value for key, value in more.items() if value not in (None, "")}}


def chat_entries(messages):
    found = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            found.append(entry("user", "text", text_of(message), index))
            continue
        role = str(message.get("role") or "?")
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            found.append(entry(role, "reasoning", reasoning, index))
        if role == "tool":
            found.append(entry(role, "tool_result", text_of(message.get("content")), index,
                               id=message.get("tool_call_id"), name=message.get("name")))
            continue
        text = text_of(message.get("content"))
        calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if text or not calls:
            found.append(entry(role, "text", text, index))
        for call in calls:
            function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) \
                else {}
            found.append(entry(role, "tool_call", arguments_text(function.get("arguments")), index,
                               name=function.get("name"), id=call.get("id") if isinstance(call, dict) else None))
    return found


def anthropic_entries(messages):
    found = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role, content = str(message.get("role") or "?"), message.get("content")
        if not isinstance(content, list):
            found.append(entry(role, "text", text_of(content), index))
            continue
        for block in content:
            if not isinstance(block, dict):
                found.append(entry(role, "text", text_of(block), index))
                continue
            kind = block.get("type")
            if kind == "tool_use" or kind == "server_tool_use":
                found.append(entry(role, "tool_call", arguments_text(block.get("input")), index,
                                   name=block.get("name"), id=block.get("id")))
            elif kind == "tool_result":
                found.append(entry(role, "tool_result", text_of(block.get("content")), index,
                                   id=block.get("tool_use_id"), error=bool(block.get("is_error")) or None))
            elif kind in ("thinking", "redacted_thinking"):
                found.append(entry(role, "reasoning", text_of(block.get("thinking")) or "[redacted thinking]", index))
            else:
                found.append(entry(role, "text", text_of(block), index))
    return found


def responses_entries(items):
    found = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            found.append(entry("user", "text", item, index))
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            found.append(entry(str(item.get("role") or "?"), "text", text_of(item.get("content")), index))
        elif kind in ("function_call", "custom_tool_call"):
            found.append(entry("assistant", "tool_call", arguments_text(item.get("arguments", item.get("input"))),
                               index, name=item.get("name"), id=item.get("call_id")))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            found.append(entry("tool", "tool_result", text_of(item.get("output")), index, id=item.get("call_id")))
        elif kind == "reasoning":
            text = text_of(item.get("summary")) or text_of(item.get("content")) or \
                ("[encrypted reasoning]" if item.get("encrypted_content") else "")
            found.append(entry("assistant", "reasoning", text, index))
        else:
            found.append(entry("assistant" if str(kind).endswith("_call") else "tool", "other",
                               json.dumps(item, ensure_ascii=False)[:4000], index, name=kind))
    return found


def view(record, new_from=0):
    """The readable view of a record: {"system", "messages" (entries {"role", "kind" text|reasoning|tool_call|
    tool_result|other, "text", "message", "name", "id", "new", "relay"}), "answer" (entries), "tools" (names),
    "params", "changes", "new_from"}."""
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    api = record.get("api")
    if api == "responses":
        items = request.get("input")
        messages = responses_entries([items] if isinstance(items, str) else items if isinstance(items, list) else [])
        system = request.get("instructions")
    elif api == "completions":
        prompt = request.get("prompt")
        messages = [entry("user", "text", text_of(p), i)
                    for i, p in enumerate(prompt if isinstance(prompt, list) else [prompt] if prompt else [])]
        system = None
    elif api == "messages":
        messages = anthropic_entries(request.get("messages") if isinstance(request.get("messages"), list) else [])
        system = request.get("system")
    else:
        messages = chat_entries(request.get("messages") if isinstance(request.get("messages"), list) else [])
        system = None
    notes = [c for c in listed(record.get("changes")) if isinstance(c, dict) and c.get("field") == "messages"]
    for item in messages:
        item["new"] = item["message"] >= new_from
        for note in notes:
            if note.get("message") == item["message"] and str(note.get("note") or "") in item["text"]:
                item["relay"] = note.get("note")
            elif note.get("where") == "a new message" and str(note.get("note") or "") == item["text"]:
                item["relay"] = note.get("note")
    tools = []
    for tool in request.get("tools") if isinstance(request.get("tools"), list) else []:
        if isinstance(tool, dict):
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            tools.append(str(function.get("name") or tool.get("type") or "?"))
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    answer = []
    if response.get("reasoning"):
        answer.append(entry("assistant", "reasoning", response["reasoning"], None))
    if response.get("text"):
        answer.append(entry("assistant", "text", response["text"], None))
    for call in listed(response.get("tool_calls")):
        if isinstance(call, dict):
            answer.append(entry("assistant", "tool_call", arguments_text(call.get("arguments")), None,
                                name=call.get("name"), id=call.get("id")))
    if isinstance(response.get("error"), dict):
        answer.append(entry("error", "text", compact(response["error"]), None))
    return {"system": text_of(system) if system is not None else "", "messages": messages, "answer": answer,
            "tools": tools, "params": {k: v for k, v in request.items() if k not in CONVERSATION_KEYS},
            "changes": listed(record.get("changes")), "new_from": new_from}


def tokens(record):
    """(in, out) tokens of a record's answer (None where not given)."""
    usage = (record.get("response") or {}).get("usage") if isinstance(record.get("response"), dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return (usage.get("input_tokens", usage.get("prompt_tokens")),
            usage.get("output_tokens", usage.get("completion_tokens")))


def loop_marks(record):
    """What the loop guard did in this request: ["note", "paused tools", "answer stopped", ...]."""
    marks = []
    loop = record.get("loop") if isinstance(record.get("loop"), dict) else None
    if loop and loop.get("action"):
        marks.append(str(loop["action"]))
    for change in listed(record.get("changes")):
        if isinstance(change, dict) and change.get("why") == "loop guard" and change.get("field") == "messages" \
                and "note" not in marks:
            marks.append("note")
    return marks


def summary(record, new_from=0, continues=None):
    """One request in a line's worth: {"n", "time", "run", "source", "endpoint", "host", "api", "model", "status",
    "ms", "first_byte_ms", "tokens_in", "tokens_out", "finish_reason", "messages", "new", "continues", "tool_calls",
    "loop", "changes", "truncated", "error", "preview"}."""
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    if isinstance(record.get("digests"), list) and record["digests"]:
        count = len(record["digests"]) - 1
    else:  # a record without digests (the request was not read): its messages as they are
        items = request.get("input") if record.get("api") == "responses" else request.get("messages")
        count = len(items) if isinstance(items, list) else 1 if isinstance(items, str) else 0
    tokens_in, tokens_out = tokens(record)
    error = record.get("error") or (response.get("error") or {}).get("message") \
        if isinstance(response.get("error"), dict) else record.get("error")
    preview = response.get("text") or ("; ".join(str(c.get("name")) for c in listed(response.get("tool_calls"))
                                                 if isinstance(c, dict)))
    return {"n": record.get("n"), "time": record.get("time"), "run": record.get("run"), "source": record.get("source"),
            "endpoint": record.get("endpoint"), "host": record.get("host"), "api": record.get("api"),
            "model": record.get("model"), "status": record.get("status"), "ms": record.get("ms"),
            "first_byte_ms": record.get("first_byte_ms"), "tokens_in": tokens_in, "tokens_out": tokens_out,
            "finish_reason": response.get("finish_reason"), "messages": count, "new": max(0, count - new_from),
            "continues": continues, "tool_calls": [str(c.get("name")) for c in listed(response.get("tool_calls"))
                                                   if isinstance(c, dict)],
            "loop": loop_marks(record), "changes": len(listed(record.get("changes"))),
            "truncated": record.get("truncated") or None, "error": error if error else None,
            "unread": request.get("unread"), "preview": str(preview or "")[:200]}


# -- text ------------------------------------------------------------------------------------------------------------

def seconds(ms):
    return "-" if not isinstance(ms, (int, float)) else f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms"


def head_line(record, info):
    """The first line of a request: number, time, where, model, status, time taken, tokens, the loop guard."""
    when = str(record.get("time") or "?")[:19].replace("T", " ")
    where = record.get("endpoint") or record.get("host") or "?"
    shape = f"{record.get('api') or '?'}{', streamed' if record.get('stream') else ''}"
    line = f"#{record.get('n')}  {when}  {where}  {record.get('model') or '?'} ({shape})  " \
           f"{record.get('status') if record.get('status') is not None else 'no answer'}  {seconds(record.get('ms'))}"
    if info["tokens_in"] is not None or info["tokens_out"] is not None:
        line += f"  tokens in {info['tokens_in'] if info['tokens_in'] is not None else '?'} / out " \
                f"{info['tokens_out'] if info['tokens_out'] is not None else '?'}"
    if info["finish_reason"]:
        line += f"  ({info['finish_reason']})"
    return safe(line)


def change_text(change):
    if change.get("field") == "messages":
        return f"loop guard note in {change.get('where')}: {change.get('note')}"
    if change.get("note"):
        return f"{change.get('field')}: {change.get('from')} {change['note']} ({change.get('why')})"
    old = json.dumps(change["from"], ensure_ascii=False) if "from" in change else "(not set)"
    new = json.dumps(change["to"], ensure_ascii=False) if "to" in change else "(removed)"
    return f"{change.get('field')}: {old} -> {new} ({change.get('why')})"


def label(item):
    kind, role = item["kind"], item["role"]
    if kind == "tool_call":
        return f"{role} -> tool call {item.get('name') or '?'}" + (f" [{item['id']}]" if item.get("id") else "")
    if kind == "tool_result":
        return "tool result" + (f" [{item['id']}]" if item.get("id") else "") + (" (error)" if item.get("error")
                                                                                 else "")
    if kind == "reasoning":
        return f"{role} (reasoning)"
    if kind == "other":
        return f"{role} ({item.get('name')})"
    return role


def cut(text, limit):
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + f" … [{len(text) - limit} more characters: --full]"


def entry_lines(item, full, colour):
    t = module("textout")
    title = label(item) + ("  [relay: loop guard note]" if item.get("relay") else "")
    kind = {"user": "accent", "assistant": "ok", "tool": "muted", "system": "magenta", "error": "bad"}.get(
        item["role"], "bold")
    lines = [t.style(f"  [{safe(title)}]", kind, colour)]
    for line in safe(cut(item["text"], None if full else CUT), newlines=True).split("\n"):
        lines.append("    " + line.replace("\t", "    "))
    return lines


def transcript(record, new_from=0, continues=None, full=False, colour=False):
    """The lines of one request as a readable chat: its head, the relay's changes, the system part and every message
    with FULL (else only what is new since the request it continues), the answer."""
    t = module("textout")
    info = summary(record, new_from, continues)
    shown = view(record, 0 if full else new_from)
    lines = [t.style(head_line(record, info), "bold", colour)]
    marks = loop_marks(record)
    if marks:
        loop = record.get("loop") if isinstance(record.get("loop"), dict) else {}
        detail = loop.get("detail") or loop.get("watchdog") or (
            f"{loop['tool']} {loop.get('count', '')} times" if loop.get("tool") else "")
        lines.append(t.style("  loop guard: " + ", ".join(marks) + (f" - {safe(detail)}" if detail else ""), "warn",
                             colour))
    for change in shown["changes"]:
        if isinstance(change, dict) and change.get("field") != "messages":
            lines.append(t.style("  relay changed " + safe(change_text(change)), "warn", colour))
    if record.get("truncated"):
        how = record["truncated"]
        lines.append(t.style("  truncated: " + safe(", ".join(map(str, how)) if isinstance(how, list) else "yes"),
                             "warn", colour))
    if info["unread"]:
        lines.append(t.style(f"  request not readable: {safe(info['unread'])}", "warn", colour))
    if full:
        if shown["system"]:
            lines += entry_lines({"role": "system", "kind": "text", "text": shown["system"]}, full, colour)
        if shown["tools"]:
            lines.append(t.style(f"  tools ({len(shown['tools'])}): " + safe(", ".join(shown["tools"])), "muted",
                                 colour))
        if shown["params"]:
            lines.append(t.style("  parameters: " + safe(json.dumps(shown["params"], ensure_ascii=False)), "muted",
                                 colour))
    else:
        new = info["new"]
        lines.append(t.style(f"  new in this request: {new} of {info['messages']} messages" +
                             (f" (continues #{continues})" if continues else " (a new conversation)"), "muted", colour))
    for item in shown["messages"]:
        if full or item["new"]:
            lines += entry_lines(item, full, colour)
    lines.append(t.style("  -- answer" + (f" ({safe(info['finish_reason'])})" if info["finish_reason"] else "") + " --",
                         "bold", colour))
    if not shown["answer"]:
        lines.append(t.style("    (no answer recorded" + (f": {safe(info['error'])}" if info["error"] else "") + ")",
                             "muted", colour))
    for item in shown["answer"]:
        lines += entry_lines(item, full, colour)
    return lines


def markdown_block(text):
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def markdown(records, title="LLM traffic", full=False):
    """A Markdown report of RECORDS: each request's head, the relay's changes, what is new (everything with FULL)
    and the answer."""
    thread, parts = Thread(), [f"# {title}", ""]
    for record in records:
        new_from, continues = thread.new_from(record)
        info = summary(record, new_from, continues)
        shown = view(record, 0 if full else new_from)
        parts += [f"## {head_line(record, info)}", ""]
        marks = loop_marks(record)
        if marks:
            parts.append(f"- loop guard: {', '.join(marks)}")
        for change in shown["changes"]:
            if isinstance(change, dict) and change.get("field") != "messages":
                parts.append(f"- relay changed {safe(change_text(change))}")
        if record.get("truncated"):
            parts.append(f"- truncated: {safe(record['truncated'])}")
        if not full:
            parts.append(f"- new in this request: {info['new']} of {info['messages']} messages"
                         + (f" (continues #{continues})" if continues else ""))
        parts.append("")
        if full and shown["system"]:
            parts += ["**system**", "", markdown_block(safe(shown["system"], True)), ""]
        for item in shown["messages"]:
            if full or item["new"]:
                parts += [f"**{safe(label(item))}**" + (" *(relay: loop guard note)*" if item.get("relay") else ""), "",
                          markdown_block(safe(item["text"], True)), ""]
        parts += ["### Answer" + (f" ({safe(info['finish_reason'])})" if info["finish_reason"] else ""), ""]
        for item in shown["answer"]:
            parts += [f"**{safe(label(item))}**", "", markdown_block(safe(item["text"], True)), ""]
        if not shown["answer"]:
            parts += ["*(no answer recorded)*", ""]
    return "\n".join(parts) + "\n"


# -- following -------------------------------------------------------------------------------------------------------

class Follow:
    """New records of a run's current file as they are written (also across a rotation: the old file's rest is
    read before the new one)."""

    def __init__(self, directory, runs):
        self.directory, self.runs = Path(directory), list(runs)
        self.fd, self.inode, self.position, self.pending = None, None, 0, b""
        self.run, self.path = runs[-1] if runs else None, None

    def current(self):
        files = run_files(self.directory)
        if self.run is None and files:  # no run yet (--follow before the first request): the newest that appears
            self.run = max(files.values(), key=lambda r: (r["started"], r["modified"]))["run"]
        slot = files.get(self.run) if self.run else None
        return slot["paths"][-1] if slot else None

    def mark(self):
        """Go on from the end of the current file as it is now (no file yet: from the start of the first)."""
        path = self.current()
        if path is None:
            return
        try:
            self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return
        self.path = path
        self.inode = os.fstat(self.fd).st_ino
        self.position = module("jsonltail").complete_end(self.fd, os.fstat(self.fd).st_size)

    def read(self):
        """The records written since the last look."""
        found = []
        if self.fd is None:
            path = self.current()
            if path is None:
                return found
            try:
                self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            except OSError:
                return found
            self.path = path
            self.inode, self.position = os.fstat(self.fd).st_ino, 0
        while True:
            data = os.pread(self.fd, 1 << 20, self.position)
            if not data:
                break
            self.position += len(data)
            self.pending += data
            *lines, self.pending = self.pending.split(b"\n")
            for line in lines:
                record = parse(line)
                if record is not None:
                    found.append(record)
            if len(self.pending) > LINE_LIMIT:
                self.pending = b""
        path = self.current()
        try:
            rotated = path is None or os.stat(path).st_ino != self.inode
        except OSError:
            rotated = True
        if rotated and path is not None:
            os.close(self.fd)
            self.fd, self.pending = None, b""
            found += self.read()
        return found

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# -- the command -----------------------------------------------------------------------------------------------------

def parser(add_help=True):
    p = argparse.ArgumentParser(prog="sandbox-ai llm traffic", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, add_help=add_help,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    p.add_argument("run", nargs="?", help="A run (see --runs) or the name of a start; default: the newest run")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-s", "--session", metavar="NAME", help="The runs of a named session")
    where.add_argument("-p", "--project", type=Path, metavar="PATH", help="The newest run of a project")
    where.add_argument("-l", "--log-dir", type=Path, metavar="DIR", help="The newest run (or RUN) of a log directory")
    p.add_argument("--runs", action="store_true", help="List the runs with an LLM traffic log instead")
    p.add_argument("-r", "--request", type=int, metavar="N", help="Only request N (its number)")
    p.add_argument("-n", "--tail", type=int, default=0, metavar="N", help="Only the last N requests")
    p.add_argument("-g", "--grep", metavar="TEXT", help="Only requests that contain TEXT (case does not count)")
    p.add_argument("-f", "--follow", action="store_true", help="Go on showing new requests (Ctrl-C ends)")
    p.add_argument("--full", action="store_true", help="Every message of each request, nothing cut (default: what is "
                   "new since the request before, long texts cut)")
    p.add_argument("-j", "--json", action="store_true", help="JSON lines: the records (with --request: the record "
                   "and its readable view; with --summary: the summaries; with --runs: the runs)")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--summary", action="store_true", help="One line per request")
    out.add_argument("--markdown", action="store_true", help="A Markdown report")
    return p


def summary_line(info, colour):
    t = module("textout")
    when = str(info.get("time") or "?")[11:19]
    tokens_text = f"{info['tokens_in'] if info['tokens_in'] is not None else '-'}/" \
                  f"{info['tokens_out'] if info['tokens_out'] is not None else '-'}"
    marks = ",".join(info["loop"]) or ""
    status = str(info["status"]) if info["status"] is not None else "-"
    number = info["n"] if info.get("n") is not None else "?"
    line = f"#{str(number):<5} {when}  {safe(info.get('model') or '?')[:32]:<32} {status:>4} {seconds(info['ms']):>8} " \
           f"{tokens_text:>13}  +{info['new']}/{info['messages']}  {safe(info['preview'])[:60]}"
    if marks:
        line += "  " + t.style(f"[loop guard: {marks}]", "warn", colour)
    if info.get("truncated"):
        line += "  " + t.style("[truncated]", "warn", colour)
    return line


def emit(records, a, out, thread, colour):
    for record in records:
        new_from, continues = thread.new_from(record)
        if a.request is not None and record.get("n") != a.request:
            continue
        if a.json and a.request is not None:
            out(json.dumps({"record": record, "view": view(record, new_from), "continues": continues,
                            "summary": summary(record, new_from, continues)}, ensure_ascii=False))
        elif a.json:
            out(json.dumps(record, ensure_ascii=False))
        elif a.summary:
            info = summary(record, new_from, continues)
            out(json.dumps(info, ensure_ascii=False) if getattr(a, "summary_json", False) else
                summary_line(info, colour))
        else:
            try:
                lines = transcript(record, new_from, continues, a.full or a.request is not None, colour)
            except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as exc:
                # One record the view cannot lay out never hides the requests after it.
                lines = [f"#{safe(record.get('n'))}  this request cannot be shown as a chat ({type(exc).__name__}); "
                         "its record: --json"]
            for line in lines:
                out(line)
            out("")


def cli(argv, out=None):
    a = parser().parse_args(argv)
    out = out or (lambda text: print(text, flush=True))
    if a.tail < 0:
        raise ValueError("--tail must not be negative")
    directory, runs = locate(a)
    if a.runs:
        rows = runs_in(directory)
        if a.json:
            out(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        t = module("textout")
        t.table(["RUN", "STARTED (UTC)", "SESSION", "SIZE", "STATE"],
                [[safe(r["run"]), r["started"], safe(r["session"] or "-"), f"{r['bytes'] / (1 << 20):.1f} MiB",
                  "running" if r["running"] else "ended"] for r in rows], out, indent=0,
                empty=f"no LLM traffic log in {safe(directory)}")
        return 0
    colour = module("textout").colour_enabled() and not (a.json or a.markdown)
    if a.markdown and (a.follow or a.json):
        raise ValueError("--markdown makes one report: without --follow or --json")
    a.summary_json = bool(a.summary and a.json)
    if a.summary:
        a.json = False
    follow = Follow(directory, runs) if a.follow else None
    if follow is not None:
        follow.mark()  # before the listing: what is written meanwhile comes once (records counted by run and n)
    paths = paths_of(directory, runs)
    thread = Thread()
    expander = Expander(finder(paths))  # (records written as deltas put together, also those --follow reads)
    if a.tail:
        # the requests before the last N too, for what is new in the first of them
        records = tail_records(paths, a.tail + LOOK_BACK, expander) if not a.grep else \
            list(collections.deque(read_records(paths, a.grep, expander=expander), maxlen=a.tail + LOOK_BACK))
        for record in records[:-a.tail]:
            thread.new_from(record)
        records = records[-a.tail:]
    else:
        # One record at a time, never the whole log in memory: a run's log of up to a GiB of conversations took five
        # times that as Python objects - for each request the app shows (--request N --json).
        records = read_records(paths, a.grep, expander=expander)
    seen = {}  # run: the highest request number read
    found = {"any": False, "request": False}

    def tracked(records):
        for record in records:
            found["any"] = True
            found["request"] = found["request"] or record.get("n") == a.request
            seen[record.get("run")] = max(seen.get(record.get("run"), 0), number(record))
            yield record

    if a.markdown:
        title = f"LLM traffic of {', '.join(runs) or safe(directory)}"
        chosen = [r for r in tracked(records) if a.request is None or r.get("n") == a.request]
        if a.request is not None and not chosen:  # (as the other views: never an empty report)
            raise ValueError(f"no request {a.request} in {', '.join(runs) or 'this log'}")
        out(markdown(chosen, title, a.full))
        return 0
    emit(tracked(records), a, out, thread, colour)
    if a.request is not None and not a.follow and not found["request"]:
        raise ValueError(f"no request {a.request} in {', '.join(runs) or 'this log'}")
    if not found["any"] and not a.follow and not a.json and not a.summary:
        out(module("textout").style("no requests" + (" match" if a.grep else ""), "muted", colour))
    if follow is not None:
        try:
            while True:
                read = follow.read()
                if follow.path is not None:
                    expander.enter(lifetime(follow.path))  # (the log followed: the last one listed)
                new = [expander.expand(r) for r in read if number(r) > seen.get(r.get("run"), 0)]
                for record in new:
                    seen[record.get("run")] = max(seen.get(record.get("run"), 0), number(record))
                if a.grep:
                    needle = a.grep.lower()
                    new = [r for r in new if needle in json.dumps(r, ensure_ascii=False).lower()]
                emit(new, a, out, thread, colour)
                time.sleep(POLL)
        finally:
            follow.close()
    return 0


def number(record):
    """The request number of RECORD, 0 when it has none that is a number (a damaged line)."""
    n = record.get("n")
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def main(argv=None):
    try:
        return cli(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai llm traffic: {safe(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
