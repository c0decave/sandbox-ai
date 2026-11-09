#!/usr/bin/env python3
"""sandbox-ai activity: what the agents did in a sandbox, as one timeline.

  sandbox-ai activity [RUN]                 a run's activity (running or ended)
  sandbox-ai activity --session NAME        a named session's activity
  sandbox-ai activity --project PATH        every run of a project (its default log directory)
  sandbox-ai activity --log-dir DIR         every run that logged into DIR
  sandbox-ai activity --home DIR            read the agents' transcripts in a sandbox HOME directly
  sandbox-ai activity --runs                the runs a log directory holds

  --follow (-f) shows new events as they come, --since TIME and --kind KIND narrow the timeline, -v adds
  each event's details, --json prints JSON lines.

While a sandbox runs, its launcher collects the timeline into activity.jsonl in the run's log directory: the
agents' own transcripts in the sandbox HOME (Claude Code, Codex, OpenCode) and sandbox-ai's own logs (file
changes, refused and allowed connections, approvals, the session's start, pause, resume and end). It reads
them once more when the agent has ended, before a throwaway HOME is deleted.

Everything in a sandbox HOME is written by the agent: it is read without following a symbolic link, in
bounded amounts, and parsed as untrusted data. Tokens, keys and passwords in commands and arguments are
redacted (the patterns of the connection log, and the values of --env and --secret-env); environment
values are never copied. Standard library only.
"""
import argparse
import calendar
import contextlib
from collections import deque
import hashlib
import importlib.util
import json
import math
import mmap
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
ACTIVITY_FILE = "activity.jsonl"
SCHEMA = 1
KINDS = ("prompt", "reply", "tool", "command", "file", "network", "approval", "mcp", "session", "tokens")
ACTIVITY_LIMIT = 256 << 20   # activity.jsonl per log directory; then one note and no more records
TEXT_LIMIT = 2000            # characters of a prompt, a reply or a command kept
VALUE_LIMIT = 500            # characters of any other text in the details
SUMMARY_LIMIT = 240
RECORD_LIMIT = 64 << 10      # one record, as JSON
READ_LIMIT = 16 << 20        # bytes read from one transcript per poll (a longer line is skipped)
POLL_BUDGET = 64 << 20       # bytes read per poll in all
JSON_FILE_LIMIT = 4 << 20    # one JSON file of OpenCode's storage
FILES_LIMIT = 5000           # files looked at per source and poll
DEPTH_LIMIT = 6
INTERVAL = 2.0               # seconds between two polls while the sandbox runs
FINAL_SECONDS = 20           # the last read after the run, at most
RUNNING_AFTER = 5.0          # a tool call still open after this many seconds is shown as running
REPEAT_WINDOW = 60.0         # repeated network events are counted and shown at most once a minute
GROUP_FILES = 5              # more file events than this in one folder and poll become one record

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

_modules = {}
_modules_lock = threading.Lock()


def module(name):
    """sandbox-ai's module NAME (runtime, textout, approvals, agent_sandbox, net, sessions), loaded by path
    like the other commands do."""
    with _modules_lock:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_activity", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


# -- time --

ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,9}))?\s*(Z|z|[+-]\d\d:?\d\d)?$")


def epoch(value):
    """Seconds since the epoch of VALUE (ISO 8601 text, or a number of seconds or milliseconds), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str):
        return None
    match = ISO.match(value.strip()[:64])
    if not match:
        return None
    try:
        seconds = calendar.timegm(tuple(int(part) for part in match.groups()[:6]) + (0, 0, 0))
    except (ValueError, OverflowError):
        return None
    zone = match.group(8)
    if zone and zone not in "Zz":
        digits = zone[1:].replace(":", "")
        seconds -= (1 if zone[0] == "+" else -1) * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return seconds + (float("0." + match.group(7)) if match.group(7) else 0.0)


def stamp(seconds):
    """'YYYY-MM-DDTHH:MM:SS.mmmZ' (UTC) of SECONDS since the epoch."""
    seconds = min(max(float(seconds), 0.0), 253402300799.0)
    whole = int(seconds)
    millis = min(999, int(round((seconds - whole) * 1000)))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{millis:03d}Z"


def when(value, fallback=None):
    """The record time of VALUE (see epoch), else FALLBACK, else now."""
    seconds = epoch(value)
    if seconds is None:
        seconds = fallback if fallback is not None else time.time()
    return stamp(seconds)


def since_value(text, now=None):
    """Seconds since the epoch for --since: an ISO time, HH:MM (today, local time), or an age such as
    30s, 10m, 2h, 3d."""
    now = time.time() if now is None else now
    text = text.strip()
    match = re.fullmatch(r"(\d+)\s*([smhd])", text)
    if match:
        return now - int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    match = re.fullmatch(r"(\d{1,2}):(\d\d)(?::(\d\d))?", text)
    if match:
        today = time.localtime(now)
        return time.mktime((today.tm_year, today.tm_mon, today.tm_mday, int(match.group(1)), int(match.group(2)),
                            int(match.group(3) or 0), 0, 0, -1))
    if re.fullmatch(r"\d{4}-\d\d-\d\d", text):
        return time.mktime(time.strptime(text, "%Y-%m-%d"))
    seconds = epoch(text if re.search(r"(Z|z|[+-]\d\d:?\d\d)$", text) else None)
    if seconds is None and ISO.match(text):  # without a zone: local time
        try:
            seconds = time.mktime(time.strptime(text.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            seconds = None
    if seconds is None:
        raise ValueError(f"--since: not a time: {text!r} (examples: 10m, 2h, 14:30, 2026-11-04, "
                         "2026-11-04T14:30:00Z)")
    return seconds


# -- text --

def text_of(value):
    """VALUE as str with what UTF-8 cannot hold (lone surrogates) replaced."""
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "replace").decode("utf-8")


def cut(text, limit):
    text = text_of(text)
    return text if len(text) <= limit else text[:limit] + "…"


def one_line(text, limit=SUMMARY_LIMIT):
    return cut(" ".join(text_of(text).split()), limit)


# Beyond the connection log's patterns (runtime.SECRET_TEXT): variables assigned in a command, header lines,
# private keys.
EXTRA_SECRETS = [
    (re.compile(r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|APIKEY|ACCESS_?KEY|PRIVATE_?KEY"
                r"|CREDENTIALS?)[A-Za-z0-9_]*\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s\"';&|)]+)", re.I), r"\1[redacted]"),
    (re.compile(r"((?:x-api-key|api-key|x-auth-token|private-token|authorization|proxy-authorization|cookie)"
                r"\s*:\s*)(?:(?:Bearer|Basic|token)\s+)?[^\s\"',;]+", re.I), r"\1[redacted]"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[redacted private key]"),
]
SECRET_NAME = re.compile(r"token|secret|passw|api_?key|apikey|auth|credential|private|cookie|session_?key", re.I)
SECRET_KEY = re.compile(r"^(?:.*(?:token|secret|password|passwd|api_?key|apikey|authorization|credential"
                        r"|cookie|private_?key).*)$", re.I)


class Redactor:
    """Text without the secrets we know of: the exact VALUES (--env, --secret-env), the token formats and
    credential patterns of the connection log, and EXTRA_SECRETS."""

    def __init__(self, values=()):
        self.values = sorted({v for v in values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text, limit=VALUE_LIMIT):
        # Cut with a margin first (bounded work), redact, then cut: a secret across the cut is still found.
        text = text_of(text)
        if len(text) > limit + 1000:
            text = text[:limit + 1000]
        for value in self.values:
            if value in text:
                text = text.replace(value, "[redacted]")
        for pattern, replacement in EXTRA_SECRETS:
            text = pattern.sub(replacement, text)
        text = module("runtime").redact_text(text)
        return cut(text, limit)


LONG_KEYS = {"text", "command", "arguments", "patch", "query", "url", "description", "prompt", "error"}


def clean(value, redact, limit=VALUE_LIMIT, budget=None, depth=0):
    """VALUE for the details of a record: strings redacted and cut, at most 150 items in all, 6 levels;
    values under names of secrets are left out, environments keep only their variable names."""
    budget = budget if budget is not None else [150]
    if budget[0] <= 0:
        return "…"
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 1 << 63 else str(value)[:40]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact(value, limit)
    if depth >= 6:
        return "…"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:60]:
            key = cut(key, 80)
            if key.lower() in ("env", "environment", "envs") and isinstance(item, dict):
                result[key] = sorted(cut(name, 80) for name in list(item)[:60])  # names only, never values
            elif SECRET_KEY.match(key) and not isinstance(item, (bool, int, float)) and item is not None:
                result[key] = "[redacted]"
            else:
                result[key] = clean(item, redact, TEXT_LIMIT if key in LONG_KEYS else limit, budget, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [clean(item, redact, limit, budget, depth + 1) for item in list(value)[:60]]
    return redact(str(value), limit)


# -- records --

def raw(kind, summary, time=None, details=None, key=None, agent=None, source=None, conversation=None,
        subagent=None, update=False):
    """An event as a parser reports it; finalize() makes it a record of the timeline."""
    return {"kind": kind, "summary": summary, "time": time, "details": details or {}, "key": key, "agent": agent,
            "source": source, "conversation": conversation, "subagent": subagent, "update": update}


def finalize(event, run, session, agent, redact, counter=None):
    """The activity.jsonl record of EVENT (see raw()): the common fields, everything the agent chose
    redacted and bounded, an id (equal for a later record that replaces an earlier one: "update")."""
    details = clean(event.get("details") or {}, redact)
    source = event.get("source") or "sandbox-ai"
    key = event.get("key")
    if key is None:
        key = f"#{next(counter) if counter is not None else time.monotonic_ns()}"
    record = {"v": SCHEMA, "time": event.get("time") or stamp(time.time()), "run": run, "session": session,
              "agent": event.get("agent") or agent, "kind": event["kind"],
              "summary": one_line(redact(event.get("summary") or "", SUMMARY_LIMIT + 200)),
              "details": details, "source": source,
              "id": hashlib.sha256(f"{run}\0{source}\0{key}".encode("utf-8", "replace")).hexdigest()[:16]}
    for name in ("conversation", "subagent"):
        if event.get(name):
            record[name] = cut(event[name], 120)
    if event.get("update"):
        record["update"] = True
    if len(json.dumps(record, ensure_ascii=False)) > RECORD_LIMIT:
        record["details"] = {"note": "details left out: too large"}
    return record


def counter():
    number = 0
    while True:
        number += 1
        yield number


def append(path, records, limit=ACTIVITY_LIMIT):
    """Append RECORDS to the activity log PATH (never through a link); False once it holds LIMIT bytes."""
    if not records:
        return True
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_size >= limit:
            return False
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8", "replace")
        if info.st_size + len(data) > limit:
            note = {**records[-1], "kind": "session", "summary": f"activity log full ({limit >> 20} MiB); "
                    "no more events recorded", "details": {"event": "log-full"}, "id": "log-full"}
            note.pop("update", None)
            data = (json.dumps(note, ensure_ascii=False) + "\n").encode()
        os.write(fd, data)
        return info.st_size + len(data) < limit
    finally:
        os.close(fd)


# -- reading what the agent wrote, safely --

def open_dir(base, parts=()):
    """A descriptor of the directory BASE/PARTS; no part may be a symbolic link."""
    fd = os.open(base, DIR_FLAGS)
    for part in parts:
        if part in ("", ".", "..") or "/" in part:
            os.close(fd)
            raise OSError(f"bad path part {part!r}")
        try:
            new = os.open(part, DIR_FLAGS, dir_fd=fd)
        finally:
            os.close(fd)
        fd = new
    return fd


def open_file(base, relative):
    """(descriptor, stat) of the regular file BASE/RELATIVE, opened without following any link."""
    parts = relative.split("/")
    directory = open_dir(base, parts[:-1])
    try:
        fd = os.open(parts[-1], FILE_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{relative} is not a regular file")
        os.set_blocking(fd, True)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def read_at(fd, offset, size):
    chunks, left = [], size
    os.lseek(fd, offset, os.SEEK_SET)
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_small(base, relative, limit=JSON_FILE_LIMIT):
    """The bytes of the regular file BASE/RELATIVE if it has at most LIMIT bytes, else None."""
    try:
        fd, info = open_file(base, relative)
    except OSError:
        return None
    try:
        return read_at(fd, 0, info.st_size) if info.st_size <= limit else None
    except OSError:
        return None
    finally:
        os.close(fd)


def json_object(data):
    """DATA (bytes or text) parsed as a JSON object, or None (anything else, damaged, too deeply nested)."""
    try:
        value = json.loads(data)
    except (ValueError, RecursionError, UnicodeDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def regular_files(base, parts, accept, descend=lambda relative: True, depth=DEPTH_LIMIT, limit=FILES_LIMIT):
    """{relative path: stat} of the regular files under BASE/PARTS that ACCEPT(relative path) wants;
    DESCEND(relative path) picks the directories to enter. Links are never followed; at most LIMIT files,
    DEPTH levels and 20000 entries per directory."""
    found = {}
    try:
        top = open_dir(base, parts)
    except OSError:
        return found

    def walk(fd, prefix, level):
        try:
            with os.scandir(fd) as entries:
                listed = []
                for entry in entries:
                    listed.append(entry)
                    if len(listed) >= 20000:
                        break
        except OSError:
            return
        for entry in sorted(listed, key=lambda e: e.name):
            if len(found) >= limit:
                return
            relative = prefix + entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if level < depth and descend(relative):
                        try:
                            sub = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
                        except OSError:
                            continue
                        try:
                            walk(sub, relative + "/", level + 1)
                        finally:
                            os.close(sub)
                elif entry.is_file(follow_symlinks=False) and accept(relative):
                    found[relative] = entry.stat(follow_symlinks=False)
            except OSError:
                continue

    try:
        walk(top, "", 1)
    finally:
        os.close(top)
    return found


class Follow:
    """The read position in one append-only file (a transcript or a log) and its parser."""

    def __init__(self, inode=None, offset=0, parser=None):
        self.inode, self.offset, self.parser, self.skipping = inode, offset, parser, False


def new_lines(base, relative, follow, budget, final=False):
    """[(offset, line)]: the complete lines BASE/RELATIVE got since FOLLOW.offset (up to READ_LIMIT bytes
    read, nothing without BUDGET left; the rest waits for the next read); FOLLOW moves on. A replaced or truncated file is read from its
    start, a line longer than READ_LIMIT is skipped. FINAL: a last line without its newline counts too."""
    fd, info = open_file(base, relative)
    try:
        if follow.inode != info.st_ino or info.st_size < follow.offset:
            follow.inode, follow.offset, follow.skipping = info.st_ino, 0, False
            if follow.parser is not None:
                follow.parser.restart()
        if info.st_size <= follow.offset:
            return []
        start = follow.offset
        # BUDGET only says whether to read: one read is up to READ_LIMIT, so a long line always fits.
        data = read_at(fd, start, min(READ_LIMIT, info.st_size - start)) if budget > 0 else b""
    finally:
        os.close(fd)
    if not data:
        return []
    if follow.skipping:
        end = data.find(b"\n")
        if end < 0:
            follow.offset += len(data)
            return []
        follow.skipping = False
        follow.offset += end + 1
        start, data = start + end + 1, data[end + 1:]
    end = data.rfind(b"\n")
    if end < 0 and len(data) >= READ_LIMIT:  # one line longer than READ_LIMIT: skipped
        follow.offset += len(data)
        follow.skipping = True
        return []
    complete = data[:end + 1] if end >= 0 else b""
    if final:
        complete = data
    lines, position = [], start
    for line in complete.split(b"\n"):
        if line:
            lines.append((position, line))
        position += len(line) + 1
    follow.offset = start + len(complete)
    return lines


# -- the agents' transcripts --

def arguments_summary(name, arguments):
    """The most telling argument of a tool call, for its one-line summary."""
    if not isinstance(arguments, dict):
        return one_line(arguments, 160) if isinstance(arguments, str) else ""
    for key in ("command", "cmd", "file_path", "filePath", "notebook_path", "pattern", "path", "url", "query",
                "description", "skill", "prompt", "title", "subject", "name"):
        value = arguments.get(key)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = " ".join(value)
        if isinstance(value, str) and value.strip():
            extra = arguments.get("path") if key in ("pattern",) and isinstance(arguments.get("path"), str) else None
            return one_line(value, 160) + (f" in {one_line(extra, 80)}" if extra else "")
    return ""


def tool_details(arguments):
    """Tool arguments for the details: large contents are left out (only their size is kept)."""
    if not isinstance(arguments, dict):
        return {"input": arguments} if arguments not in (None, "", {}) else {}
    kept = {}
    for key, value in list(arguments.items())[:40]:
        if key in ("content", "old_string", "new_string", "oldString", "newString", "edits", "new_source",
                   "contents", "data", "patch", "input") and isinstance(value, (str, list)):
            kept[f"{key}_size"] = len(value)
        else:
            kept[key] = value
    return {"arguments": kept}


FILE_TOOLS = {"Write": "created or replaced", "Edit": "modified", "MultiEdit": "modified",
              "NotebookEdit": "modified", "write": "created or replaced", "edit": "modified",
              "multiedit": "modified"}
SHELL_TOOLS = {"Bash", "bash", "shell", "local_shell", "exec_command", "shell_command", "container.exec",
               "run_shell_command"}


def mcp_name(name, separator="__"):
    """(server, tool) of an MCP tool's name (mcp__server__tool), or None."""
    if isinstance(name, str) and name.startswith("mcp" + separator):
        server, _, tool = name[len("mcp" + separator):].partition(separator)
        if server and tool:
            return server, tool
    return None


class ToolCalls:
    """Tool calls whose result has not come yet; one shown as running when it takes a while."""

    def __init__(self):
        self.open = {}

    def begin(self, call_id, event, started):
        self.open[call_id] = {"event": event, "started": started, "shown": False}

    def end(self, call_id):
        return self.open.pop(call_id, None)

    def running(self, now, final=False):
        """Events for the calls open for RUNNING_AFTER seconds (once each) or, FINAL, every open call."""
        events = []
        for call_id, call in list(self.open.items()):
            if final:
                event = dict(call["event"], details={**call["event"]["details"], "status": "no result recorded"},
                             summary=call["event"]["summary"] + " · no result recorded", update=call["shown"])
                events.append(event)
                del self.open[call_id]
            elif not call["shown"] and call["started"] is not None and now - call["started"] >= RUNNING_AFTER:
                call["shown"] = True
                events.append(dict(call["event"], details={**call["event"]["details"], "status": "running"},
                                   summary=call["event"]["summary"] + " · running"))
        return events


def finish_tool(call, status, ended=None, extra=None):
    """The event of a tool call that ended (CALL from ToolCalls.end) with STATUS."""
    event = call["event"]
    details = {**event["details"], "status": status, **(extra or {})}
    if ended is not None and call["started"] is not None and ended >= call["started"]:
        details["duration_ms"] = int((ended - call["started"]) * 1000)
    summary = event["summary"] + (f" · {status}" if status != "ok" and event["kind"] != "command" else "")
    return dict(event, details=details, summary=summary, update=call["shown"])


def status_text(details):
    """' · exit 2 · 1.4 s' for a summary line."""
    parts = []
    status = details.get("status")
    if isinstance(details.get("exit_code"), int):
        parts.append(f"exit {details['exit_code']}")
    elif status and status != "ok":
        parts.append(str(status))
    if isinstance(details.get("duration_ms"), int):
        parts.append(f"{details['duration_ms'] / 1000:.1f} s")
    return "".join(f" · {part}" for part in parts)


class Turn:
    """Token usage of the model calls of one turn (one prompt to the end of the answer)."""

    def __init__(self):
        self.calls = {}  # call key: (usage, model, time)

    def add(self, key, usage, model=None, at=None):
        """A model call's usage; a call reported again (KEY seen before) replaces its earlier numbers."""
        numbers = {name: value for name, value in usage.items()
                   if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 40}
        if numbers:
            self.calls[key] = (numbers, model if isinstance(model, str) and model else None, at)

    def flush(self, agent, source, conversation, key, at=None, subagent=None, extra=None):
        """The tokens event of the turn so far, or None; starts a new turn."""
        if not self.calls:
            return None
        usage, model, last = {}, None, None
        for numbers, call_model, call_at in self.calls.values():
            for name, value in numbers.items():
                usage[name] = usage.get(name, 0) + value
            model = call_model or model
            last = call_at if call_at is not None else last
        calls = len(self.calls)
        self.calls = {}
        total = sum(v for k, v in usage.items() if k in TOKEN_TOTAL)
        parts = ", ".join(f"{name} {value}" for name, value in usage.items())
        return raw("tokens", f"{calls} model call(s){' to ' + model if model else ''}: {parts}",
                   time=when(at if at is not None else last),
                   details={"model": model, "calls": calls, "usage": usage, "total": total, **(extra or {})},
                   key=key, agent=agent, source=source, conversation=conversation, subagent=subagent)


TOKEN_TOTAL = {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "input",
               "output", "reasoning", "cache_read", "cache_write"}


class ClaudeParser:
    """One Claude Code transcript (~/.claude/projects/PROJECT/SESSION.jsonl; a subagent's in
    SESSION/subagents/agent-ID.jsonl). Lines: {"type": "user"|"assistant"|"system"|"attachment"|...,
    "timestamp", "sessionId", "isSidechain", "agentId", "message": {"role", "content": str | [blocks],
    "model", "usage"}, "toolUseResult", "permissionDecision", "toolDenialKind", ...}."""

    agent = "claude"

    def __init__(self, relative, meta=None):
        self.relative, self.meta = relative, meta
        self.subagent = None
        match = re.search(r"/subagents/agent-([A-Za-z0-9_-]{1,64})\.jsonl$", relative)
        if match:
            self.subagent = match.group(1)
        self.restart()

    def restart(self):
        self.tools, self.turn, self.conversation = ToolCalls(), Turn(), None

    def label(self):
        meta = self.meta() if callable(self.meta) else None
        kind = meta.get("agentType") if isinstance(meta, dict) else None
        return f"{kind}:{self.subagent}" if isinstance(kind, str) and kind else self.subagent

    def event(self, kind, summary, at, details=None, key=None, update=False):
        return raw(kind, summary, time=when(at), details=details, key=key and f"claude:{key}", agent=self.agent,
                   source="claude", conversation=self.conversation,
                   subagent=self.label() if self.subagent else None, update=update)

    def line(self, data, offset):
        events = []
        if not isinstance(data, dict):
            return events
        if isinstance(data.get("sessionId"), str):
            self.conversation = data["sessionId"][:80]
        kind, at = data.get("type"), data.get("timestamp")
        seconds = epoch(at)
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = message.get("content")
        if kind == "user":
            events += self.user(data, content, at, seconds, offset)
        elif kind == "assistant":
            events += self.assistant(data, message, content, at, seconds, offset)
        elif kind == "system" and data.get("subtype") == "turn_duration":
            flushed = self.turn.flush(self.agent, "claude", self.conversation, f"claude:tokens:{self.relative}:{offset}",
                                      at=seconds, subagent=self.label() if self.subagent else None,
                                      extra={"duration_ms": data.get("durationMs")}
                                      if isinstance(data.get("durationMs"), int) else None)
            events += [flushed] if flushed else []
        elif kind == "attachment" and isinstance(data.get("attachment"), dict):
            attachment = data["attachment"]
            if attachment.get("type") == "queued_command" and isinstance(attachment.get("prompt"), str):
                events.append(self.event("prompt", attachment["prompt"], at,
                                         {"text": cut(attachment["prompt"], TEXT_LIMIT), "queued": True},
                                         key=f"prompt:{self.relative}:{offset}"))
        return events

    def user(self, data, content, at, seconds, offset):
        events = []
        texts, results = [], []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content[:200]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_result":
                    results.append(block)
        for block in results:
            events += self.result(data, block, at, seconds, offset)
        text = "\n".join(texts).strip()
        if not text or data.get("isMeta"):
            return events
        origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
        if text.startswith("[Request interrupted by user"):
            events.append(self.event("session", "interrupted by the user", at, {"event": "interrupt"},
                                     key=f"interrupt:{self.relative}:{offset}"))
            return events
        if text.startswith(("<local-command-stdout>", "<local-command-stderr>", "<bash-stdout>", "<bash-stderr>",
                            "<local-command-caveat>")):
            return events
        if text.startswith("<task-notification>") or origin.get("kind") not in (None, "human"):
            summary, state = tagged(text, "summary"), tagged(text, "status")
            events.append(self.event("tool", "notification: " + one_line(summary if summary is not None else text, 200),
                                     at, {"event": "notification", "origin": origin.get("kind"),
                                          "status": state[:40] if state is not None else None},
                                     key=f"note:{self.relative}:{offset}"))
            return events
        bash = re.match(r"<bash-input>(.*?)</bash-input>", text, re.S)
        if bash:
            events.append(self.event("command", "! " + one_line(bash.group(1), 200), at,
                                     {"command": bash.group(1), "by": "user"}, key=f"user-bash:{self.relative}:{offset}"))
            return events
        command = tagged(text, "command-name")
        if command is not None:
            arguments = tagged(text, "command-args")
            text = command.strip() + (" " + arguments.strip() if arguments is not None else "")
        by = "agent" if data.get("isSidechain") and self.subagent else "user"
        if by == "user":  # a new turn: the tokens of the last one, if its end was not recorded
            flushed = self.turn.flush(self.agent, "claude", self.conversation, f"claude:tokens:{self.relative}:{offset}")
            events += [flushed] if flushed else []
        events.append(self.event("prompt", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text), "by": by},
                                 key=f"prompt:{self.relative}:{offset}"))
        return events

    def result(self, data, block, at, seconds, offset):
        call_id = block.get("tool_use_id")
        call = self.tools.end(call_id) if isinstance(call_id, str) else None
        if call is None:
            return []
        denied = data.get("toolDenialKind")
        error = bool(block.get("is_error"))
        status = "denied" if denied else "error" if error else "ok"
        extra = {}
        output = block.get("content")
        if isinstance(output, list):
            output = " ".join(b.get("text", "") for b in output[:20] if isinstance(b, dict) and isinstance(b.get("text"), str))
        if error and isinstance(output, str):
            code = re.match(r"Exit code (\d{1,3})\b", output)
            if code:
                extra["exit_code"] = int(code.group(1))
            extra["error"] = one_line(output, 300)
        elif call["event"]["kind"] == "command" and not error:
            extra["exit_code"] = 0
        result = data.get("toolUseResult")
        if isinstance(result, dict):
            for name in ("interrupted", "returnCodeInterpretation", "backgroundTaskId", "timedOutAfterMs", "type",
                         "status", "agentId"):
                if isinstance(result.get(name), (str, int, bool)) and result.get(name) not in (False, ""):
                    extra[name] = result[name]
            if isinstance(result.get("gitOperation"), dict):
                extra["git"] = result["gitOperation"]
        events = [finish_tool(call, status, seconds, extra)]
        if call["event"]["kind"] == "file" and isinstance(result, dict) and result.get("type") == "create":
            events[0]["details"]["action"] = "created"
            events[0]["summary"] = events[0]["summary"].replace("created or replaced", "created", 1)
        if call["event"]["kind"] == "command":
            events[0]["summary"] += status_text(events[0]["details"])
        changed = result.get("bashEditDiff") if isinstance(result, dict) else None
        if isinstance(changed, dict) and isinstance(changed.get("files"), list):
            for index, item in enumerate(changed["files"][:50]):
                if isinstance(item, dict) and isinstance(item.get("filePath"), str):
                    events.append(self.event("file", f"modified {one_line(item['filePath'], 200)} (by a command)", at,
                                             {"action": "modified", "path": item["filePath"], "by": "command",
                                              "tool_call": call_id}, key=f"bash-file:{call_id}:{index}"))
        decision = data.get("permissionDecision")
        if denied or (isinstance(decision, dict) and (decision.get("source") not in (None, "config")
                                                       or decision.get("decision") not in (None, "accept"))):
            answer = "denied" if denied else str(decision.get("decision") if isinstance(decision, dict) else "?")
            events.append(self.event("approval", f"{answer}: {call['event']['details'].get('tool')} "
                                                 f"{arguments_summary(None, call['event']['details'].get('arguments'))}",
                                     at, {"question": f"use the tool {call['event']['details'].get('tool')}",
                                          "answer": answer, "denial": denied,
                                          "decision": decision if isinstance(decision, dict) else None,
                                          "tool_call": call_id}, key=f"approval:{call_id}"))
        return events

    def assistant(self, data, message, content, at, seconds, offset):
        events = []
        usage, message_id = message.get("usage"), message.get("id")
        if isinstance(usage, dict):
            # A message is written once per content block, each time with the usage so far: counted once.
            self.turn.add(message_id if isinstance(message_id, str) else offset, usage, message.get("model"), seconds)
        if not isinstance(content, list):
            content = [{"type": "text", "text": content}] if isinstance(content, str) else []
        for index, block in enumerate(content[:200]):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                text = block["text"].strip()
                events.append(self.event("reply", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text),
                                                             "model": message.get("model")},
                                         key=f"reply:{self.relative}:{offset}:{index}"))
            elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                events += self.tool_use(block, at, seconds)
        return events

    def tool_use(self, block, at, seconds):
        name, arguments = block["name"][:200], block.get("input")
        call_id = block.get("id") if isinstance(block.get("id"), str) else f"{name}:{at}"
        details = {"tool": name, **tool_details(arguments), "call_id": call_id}
        server = mcp_name(name)
        if server:
            kind, summary = "mcp", f"{server[0]}: {server[1]}"
            details.update(server=server[0], mcp_tool=server[1])
            arguments_text = arguments_summary(name, arguments)
            summary += f" {arguments_text}" if arguments_text else ""
        elif name in SHELL_TOOLS:
            command = arguments.get("command") if isinstance(arguments, dict) else None
            kind, summary = "command", one_line(command or "", 200)
            details["command"] = command if isinstance(command, str) else None
            if isinstance(details.get("arguments"), dict):
                details["arguments"] = {k: v for k, v in details["arguments"].items() if k != "command"}
        elif name in FILE_TOOLS:
            path = arguments.get("file_path") or arguments.get("notebook_path") if isinstance(arguments, dict) else None
            kind, summary = "file", f"{FILE_TOOLS[name]} {one_line(path or '?', 200)} ({name})"
            details.update(action=FILE_TOOLS[name], path=path)
        else:
            kind = "tool"
            if name in ("Task", "Agent") and isinstance(arguments, dict):
                summary = f"subagent {arguments.get('subagent_type') or ''}: {one_line(arguments.get('description') or '', 160)}"
            else:
                summary = f"{name} {arguments_summary(name, arguments)}".strip()
        event = self.event(kind, summary, at, details, key=f"tool:{call_id}")
        self.tools.begin(call_id, event, seconds)
        return []

    def tick(self, now, final=False):
        events = self.tools.running(now, final)
        if final:
            flushed = self.turn.flush(self.agent, "claude", self.conversation, f"claude:tokens:{self.relative}:end",
                                      subagent=self.label() if self.subagent else None)
            events += [flushed] if flushed else []
        return events


class TranscriptSource:
    """The JSONL transcripts of one agent under HOME/PARTS: what they got since the collector started (or,
    with FROM_START, all of it)."""

    def __init__(self, home, parts, accept, descend, parser, from_start=False):
        self.home, self.parts, self.accept, self.descend, self.parser = home, parts, accept, descend, parser
        self.from_start, self.follows, self.started = from_start, {}, False

    def files(self):
        return regular_files(self.home, self.parts, self.accept, self.descend)

    def snapshot(self):
        """Remember where every transcript ends now: older conversations are not this run's."""
        if not self.from_start:
            for relative, info in self.files().items():
                self.follows[relative] = Follow(info.st_ino, info.st_size, None)
        self.started = True

    def poll(self, budget, now, final=False):
        events = []
        for relative, info in self.files().items():
            follow = self.follows.get(relative)
            if follow is None:
                follow = self.follows[relative] = Follow(None, 0, None)
            if follow.parser is None:
                follow.parser = self.parser(relative, self)
            while (info.st_size != follow.offset or info.st_ino != follow.inode) and budget[0] > 0:
                before = (follow.inode, follow.offset)
                try:
                    lines = new_lines(self.home, "/".join(self.parts + [relative]), follow, budget[0], final)
                except OSError:
                    break
                budget[0] -= max(1, follow.offset - before[1]) if follow.inode == before[0] else 1
                for offset, line in lines:
                    data = json_object(line) if line.strip() else None
                    if data is None:
                        continue
                    try:
                        events += follow.parser.line(data, offset)
                    except Exception:  # noqa: BLE001 - garbage the parser did not expect: skipped
                        continue
                if (follow.inode, follow.offset) == before:
                    break  # nothing complete to read yet
            try:
                events += follow.parser.tick(now, final)
            except Exception:  # noqa: BLE001 - as above
                pass
        return events


def claude_source(home, from_start=False):
    def accept(relative):
        return bool(re.fullmatch(r"[^/]+/[^/]+\.jsonl|[^/]+/[^/]+/subagents/agent-[^/]+\.jsonl", relative))

    def descend(relative):
        depth = relative.count("/")
        return depth == 0 or (depth == 1 and not relative.endswith(("/memory", "/tool-results"))) or \
            (depth == 2 and relative.endswith("/subagents"))

    def parser(relative, owner):
        meta_cache = {}

        def meta():
            if "value" not in meta_cache:
                data = read_small(home, "/".join(owner.parts + [relative[:-len(".jsonl")] + ".meta.json"]), 1 << 16)
                value = json_object(data) if data else None
                if value is not None:
                    meta_cache["value"] = value
                return value
            return meta_cache["value"]
        return ClaudeParser(relative, meta if "/subagents/" in relative else None)
    return TranscriptSource(home, [".claude", "projects"], accept, descend, parser, from_start)


def shell_text(command):
    """The command line of a shell call: [bash, -lc, TEXT] gives TEXT, other lists are joined."""
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command[:200]):
        if len(command) >= 3 and command[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh", "dash") and \
                command[1] in ("-lc", "-c", "-lic", "-ic"):
            return command[2]
        return " ".join(shlex.quote(c) for c in command[:200])
    return command if isinstance(command, str) else None


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.{1,1000})$", re.M)
PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.{1,1000})$", re.M)
PATCH_ACTIONS = {"Add": "created", "Update": "modified", "Delete": "deleted", "add": "created", "update": "modified",
                 "delete": "deleted"}


def patch_files(text):
    """[(action, path)] of an apply_patch text (*** Add File: / *** Update File: / *** Delete File:)."""
    if not isinstance(text, str):
        return []
    found = [(PATCH_ACTIONS[m.group(1)], m.group(2).strip()) for m in PATCH_FILE.finditer(text[:1 << 20])]
    moved = [m.group(1).strip() for m in PATCH_MOVE.finditer(text[:1 << 20])]
    return found[:200] + [("moved here", path) for path in moved[:50]]


def files_event(files, at, tool, key, agent, source, conversation=None, subagent=None, details=None):
    """One file event for the files a tool changed ([(action, path)])."""
    files = [(a, p) for a, p in files if isinstance(p, str) and p]
    if not files:
        return None
    if len(files) == 1:
        action, path = files[0]
        summary, extra = f"{action} {one_line(path, 200)} ({tool})", {"action": action, "path": path}
    else:
        names = ", ".join(one_line(p, 80) for _, p in files[:5]) + (", …" if len(files) > 5 else "")
        summary, extra = f"changed {len(files)} files ({tool}): {names}", {"files": [{"action": a, "path": p}
                                                                                     for a, p in files[:100]]}
    return raw("file", summary, time=at, details={"tool": tool, **extra, **(details or {})}, key=key, agent=agent,
               source=source, conversation=conversation, subagent=subagent)


def output_text(output):
    """The text of a tool output: a string, [{type: input_text|output_text|text, text}] or {content|output}."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(item.get("text") for item in output[:50]
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    if isinstance(output, dict):
        for key in ("content", "output", "text"):
            if key in output:
                return output_text(output[key])
    return ""


def codex_result(output):
    """(status, details) of a Codex tool output: the exit code, wall time and rejections it reports."""
    text = output_text(output)
    details = {}
    legacy = json_object(text) if text.startswith("{") and len(text) < 1 << 20 else None
    if legacy is not None and isinstance(legacy.get("metadata"), dict):  # {"output", "metadata": {exit_code, ...}}
        metadata = legacy["metadata"]
        if isinstance(metadata.get("exit_code"), int):
            details["exit_code"] = metadata["exit_code"]
        if isinstance(metadata.get("duration_seconds"), (int, float)):
            details["duration_ms"] = int(metadata["duration_seconds"] * 1000)
        text = output_text(legacy.get("output"))
    match = re.search(r"Process exited with code (-?\d{1,4})|Exit code: (-?\d{1,4})", text[:4000])
    if match:
        details["exit_code"] = int(match.group(1) or match.group(2))
    match = re.search(r"Wall time: ([0-9]{1,7}(?:\.[0-9]+)?) seconds", text[:4000])
    if match:
        details["duration_ms"] = int(float(match.group(1)) * 1000)
    match = re.search(r"Process running with session ID (\d{1,12})", text[:4000])
    if match:
        details["session_id"] = int(match.group(1))
        return "running in the background", details
    if re.search(r"\b(?:exec command|patch) rejected by user|rejected by the user", text[:4000], re.I):
        return "denied", details
    if isinstance(details.get("exit_code"), int):
        return ("ok" if details["exit_code"] == 0 else "error"), details
    if re.match(r"(?:error|failed|execution error)\b", text[:200].strip(), re.I):
        details["error"] = one_line(text, 300)
        return "error", details
    return "ok", details


CODEX_SHELLS = {"exec_command", "shell", "shell_command", "container.exec", "local_shell"}


class CodexParser:
    """One Codex rollout (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, archived_sessions/). Lines:
    {"timestamp", "type": "session_meta"|"turn_context"|"response_item"|"event_msg"|..., "payload"}; new
    threads (rust-v0.158.0 on) keep "paginated" history: the prompts and the commands' results are
    item_completed events there, in "legacy" history user_message events and the tool outputs' text."""

    agent = "codex"

    def __init__(self, relative, owner=None):
        self.relative = relative
        self.restart()

    def restart(self):
        self.tools, self.turn, self.conversation, self.subagent = ToolCalls(), Turn(), None, None
        self.model, self.ended, self.last_total = None, {}, None

    def event(self, kind, summary, at, details=None, key=None, update=False):
        return raw(kind, summary, time=when(at), details=details, key=key and f"codex:{key}", agent=self.agent,
                   source="codex", conversation=self.conversation, subagent=self.subagent, update=update)

    def remember(self, call_id):
        self.ended[call_id] = True
        if len(self.ended) > 5000:
            self.ended.pop(next(iter(self.ended)))

    def line(self, data, offset):
        kind, payload = data.get("type"), data.get("payload")
        if not isinstance(payload, dict):
            if isinstance(kind, str) and kind in ("message", "function_call", "function_call_output", "local_shell_call",
                                                  "custom_tool_call", "custom_tool_call_output", "web_search_call"):
                kind, payload = "response_item", data  # rollouts from before the envelope
            else:
                return []
        at = data.get("timestamp") if data.get("timestamp") is not None else payload.get("timestamp")
        seconds = epoch(at)
        if kind == "session_meta":
            return self.session_meta(payload, at, offset)
        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                self.model = payload["model"][:100]
            return []
        if kind == "response_item":
            return self.response_item(payload, at, seconds, offset)
        if kind == "event_msg":
            return self.event_msg(payload, at, seconds, offset)
        return []

    def session_meta(self, meta, at, offset):
        ident = meta.get("id") or meta.get("session_id")
        if isinstance(ident, str):
            self.conversation = ident[:80]
        source = meta.get("source")
        spawn = source.get("subagent") if isinstance(source, dict) else None
        if isinstance(spawn, dict) or meta.get("parent_thread_id"):
            spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) and isinstance(spawn.get("thread_spawn"), dict) else {}
            label = spawn.get("agent_nickname") or spawn.get("agent_role") or meta.get("agent_nickname") or \
                meta.get("agent_role") or "subagent"
            self.subagent = str(label)[:60]
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        return [self.event("session", f"codex conversation in {one_line(meta.get('cwd') or '?', 160)}", at,
                           {"event": "conversation", "cwd": meta.get("cwd"), "cli_version": meta.get("cli_version"),
                            "originator": meta.get("originator"), "model_provider": meta.get("model_provider"),
                            "history_mode": meta.get("history_mode") or "legacy", "branch": git.get("branch"),
                            "parent": meta.get("parent_thread_id")}, key=f"meta:{self.relative}:{offset}")]

    def response_item(self, item, at, seconds, offset):
        kind = item.get("type")
        if kind == "message" and item.get("role") == "assistant":
            text = output_text(item.get("content")).strip()
            if not text:
                return []
            return [self.event("reply", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text),
                                                   "phase": item.get("phase"), "model": self.model},
                               key=f"reply:{self.relative}:{offset}")]
        if kind in ("function_call", "custom_tool_call", "local_shell_call"):
            return self.call(item, kind, at, seconds, offset)
        if kind in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            call = self.tools.end(call_id) if isinstance(call_id, str) else None
            if call is None:
                return []
            self.remember(call_id)
            status, extra = codex_result(item.get("output"))
            event = finish_tool(call, status, seconds, {k: v for k, v in extra.items()
                                                        if k != "duration_ms" or call["started"] is None})
            if "duration_ms" in extra and "duration_ms" not in event["details"]:
                event["details"]["duration_ms"] = extra["duration_ms"]
            if event["kind"] == "command":
                event["summary"] += status_text(event["details"])
            events = [event]
            if status == "denied":
                events.append(self.event("approval", f"denied: {event['summary']}", at,
                                         {"question": f"run the tool {event['details'].get('tool')}", "answer": "denied",
                                          "tool_call": call_id}, key=f"approval:{call_id}"))
            return events
        if kind == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            what = action.get("query") or action.get("url") or ", ".join(
                q for q in (action.get("queries") or [])[:5] if isinstance(q, str))
            return [self.event("tool", f"web search {one_line(what or '', 160)}".strip(), at,
                               {"tool": "web_search", "action": action, "status": item.get("status")},
                               key=f"search:{self.relative}:{offset}")]
        return []

    def call(self, item, kind, at, seconds, offset):
        call_id = item.get("call_id") or item.get("id") or f"{self.relative}:{offset}"
        call_id = str(call_id)[:120]
        name = str(item.get("name") or ("local_shell" if kind == "local_shell_call" else "?"))[:200]
        namespace = item.get("namespace") if isinstance(item.get("namespace"), str) else None
        if kind == "local_shell_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            arguments = {"command": action.get("command"), "workdir": action.get("working_directory"),
                         "timeout_ms": action.get("timeout_ms")}
        elif kind == "custom_tool_call":
            arguments = {"input": item.get("input")}
        else:
            text = item.get("arguments")
            arguments = json_object(text) if isinstance(text, str) and len(text) < 1 << 20 else None
            if arguments is None:
                arguments = {"input": text} if isinstance(text, str) else {}
        full = f"{namespace}__{name}" if namespace else name
        details = {"tool": full, "call_id": call_id}
        server = mcp_name(full)
        if name == "apply_patch" or (kind == "custom_tool_call" and name in ("apply_patch", "patch")):
            text = arguments.get("input") if isinstance(arguments.get("input"), str) else arguments.get("patch")
            event = files_event(patch_files(text), when(at), "apply_patch", f"codex:tool:{call_id}", self.agent,
                                "codex", self.conversation, self.subagent,
                                {"call_id": call_id, "patch_size": len(text) if isinstance(text, str) else None})
            if event is None:
                event = self.event("tool", "apply_patch", at, {**details, "patch_size": len(text or "")},
                                   key=f"tool:{call_id}")
        elif server:
            details.update(server=server[0], mcp_tool=server[1], **tool_details(arguments))
            event = self.event("mcp", f"{server[0]}: {server[1]} {arguments_summary(name, arguments)}".strip(), at,
                               details, key=f"tool:{call_id}")
        elif name in CODEX_SHELLS:
            command = shell_text(arguments.get("cmd") if "cmd" in arguments else arguments.get("command"))
            details.update(command=command, workdir=arguments.get("workdir"),
                           escalated=arguments.get("sandbox_permissions") not in (None, "use_default") or None,
                           justification=arguments.get("justification"))
            event = self.event("command", one_line(command or "", 200), at, details, key=f"tool:{call_id}")
        else:
            details.update(tool_details(arguments))
            event = self.event("tool", f"{name} {arguments_summary(name, arguments)}".strip(), at, details,
                               key=f"tool:{call_id}")
        self.tools.begin(call_id, event, seconds)
        return []

    def event_msg(self, message, at, seconds, offset):
        kind = message.get("type")
        if kind == "user_message":
            return self.prompt(message.get("message"), at, seconds, offset)
        if kind == "token_count":
            info = message.get("info") if isinstance(message.get("info"), dict) else None
            usage = info.get("last_token_usage") if info else None
            total = info.get("total_token_usage") if info else None
            if isinstance(usage, dict) and total != self.last_total:  # a repeat (rate limits only) adds nothing
                self.last_total = total
                self.turn.add(offset, usage, self.model, seconds)
            return []
        if kind in ("task_complete", "turn_complete", "turn_aborted"):
            events = []
            extra = {"duration_ms": message["duration_ms"]} if isinstance(message.get("duration_ms"), int) else None
            flushed = self.turn.flush(self.agent, "codex", self.conversation, f"codex:tokens:{self.relative}:{offset}",
                                      at=seconds, subagent=self.subagent, extra=extra)
            events += [flushed] if flushed else []
            if kind == "turn_aborted":
                events.append(self.event("session", f"turn aborted ({one_line(message.get('reason') or '?', 60)})",
                                         at, {"event": "interrupt", "reason": message.get("reason")},
                                         key=f"abort:{self.relative}:{offset}"))
            return events
        if kind == "item_completed" and isinstance(message.get("item"), dict):
            return self.item(message["item"], at, seconds, offset)
        return []

    def prompt(self, text, at, seconds, offset):
        if not isinstance(text, str) or not text.strip():
            return []
        events = []
        flushed = self.turn.flush(self.agent, "codex", self.conversation, f"codex:tokens:{self.relative}:{offset}:before",
                                  subagent=self.subagent)
        events += [flushed] if flushed else []
        text = text.strip()
        events.append(self.event("prompt", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text),
                                                      "by": "agent" if self.subagent else "user"},
                                 key=f"prompt:{self.relative}:{offset}"))
        return events

    def item(self, item, at, seconds, offset):
        """A completed turn item (paginated history)."""
        kind, ident = item.get("type"), str(item.get("id") or "")[:120]
        if kind == "UserMessage":
            return self.prompt(output_text(item.get("content")), at, seconds, offset)
        status = item.get("status")
        known = ident in self.ended or ident in self.tools.open
        events = []
        if kind == "CommandExecution":
            duration = item.get("duration")
            extra = {"exit_code": item["exit_code"]} if isinstance(item.get("exit_code"), int) else {}
            if isinstance(duration, dict) and isinstance(duration.get("secs"), int):
                extra["duration_ms"] = duration["secs"] * 1000 + int(duration.get("nanos") or 0) // 1000000
            state = {"completed": "ok", "failed": "error", "declined": "denied"}.get(status, str(status or "ok"))
            if state == "ok" and extra.get("exit_code") not in (None, 0):
                state = "error"
            if ident in self.tools.open:
                call = self.tools.end(ident)
                self.remember(ident)
                event = finish_tool(call, state, seconds, extra)
                event["summary"] += status_text(event["details"])
                events.append(event)
            elif not known:
                command = shell_text(item.get("command"))
                by = "user" if item.get("source") == "user_shell" else "agent"
                details = {"command": command, "status": state, "by": by, "cwd": item.get("cwd"), **extra}
                events.append(self.event("command", ("! " if by == "user" else "") + one_line(command or "", 200)
                                         + status_text(details), at, details, key=f"tool:{ident}"))
        elif kind == "FileChange" and not known:
            changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
            files = [(PATCH_ACTIONS.get(c.get("type"), "modified") if isinstance(c, dict) else "modified", path)
                     for path, c in list(changes.items())[:200]]
            event = files_event(files, when(at), "patch", f"codex:tool:{ident}", self.agent, "codex",
                                self.conversation, self.subagent, {"status": status})
            events += [event] if event else []
        elif kind == "McpToolCall" and not known:
            details = {"server": item.get("server"), "mcp_tool": item.get("tool"), "status": status,
                       **tool_details(item.get("arguments"))}
            events.append(self.event("mcp", f"{one_line(item.get('server') or '?', 60)}: {one_line(item.get('tool') or '?', 80)}",
                                     at, details, key=f"tool:{ident}"))
        if status == "declined" and kind in ("CommandExecution", "FileChange") and \
                f"codex:approval:{ident}" not in self.ended:
            self.remember(f"codex:approval:{ident}")
            events.append(self.event("approval", f"declined: {kind}", at, {"question": kind, "answer": "denied",
                                                                           "tool_call": ident},
                                     key=f"approval:{ident}"))
        return events

    def tick(self, now, final=False):
        events = self.tools.running(now, final)
        if final:
            flushed = self.turn.flush(self.agent, "codex", self.conversation, f"codex:tokens:{self.relative}:end",
                                      subagent=self.subagent)
            events += [flushed] if flushed else []
        return events


def codex_source(home, from_start=False):
    def accept(relative):
        return bool(re.fullmatch(r"sessions/\d{4}/\d\d/\d\d/rollout-[^/]+\.jsonl|archived_sessions/rollout-[^/]+\.jsonl",
                                 relative))

    def descend(relative):
        return bool(re.fullmatch(r"sessions(?:/\d{4}(?:/\d\d(?:/\d\d)?)?)?|archived_sessions", relative))
    return TranscriptSource(home, [".codex"], accept, descend, CodexParser, from_start)


# -- OpenCode --

OPENCODE_TOOLS = {"bash", "read", "glob", "grep", "edit", "write", "apply_patch", "patch", "task", "todowrite", "todoread",
                  "skill", "question", "webfetch", "websearch", "invalid", "execute", "lsp", "plan_exit", "plan_enter",
                  "codesearch", "batch", "list", "ls", "multiedit"}
OPENCODE_DATA = [".local", "share", "opencode"]
DATABASE_LIMIT = 512 << 20   # an OpenCode database (and its WAL) copied for reading
DATABASE_INTERVAL = 10.0     # seconds between two reads of a database while the sandbox runs
ROW_LIMIT = 5000             # rows read per table and read
def tagged(text, name):
    """The text between the first <NAME> and the next </NAME>, or None: what re.search(r"<NAME>(.*?)</NAME>", re.S)
    finds, in linear time (that regex is quadratic on a line of many "<NAME>" without its end, and the agent
    writes the transcripts: a long one would hold the launcher's threads, the canaries' and the memory watch's)."""
    start = text.find(f"<{name}>")
    if start < 0:
        return None
    start += len(name) + 2
    end = text.find(f"</{name}>", start)
    return text[start:end] if end >= 0 else None


REJECTED = re.compile(r"user rejected permission|user has specified a rule which prevents|permission.*denied", re.I)


class OpenCodeState:
    """What OpenCode's sessions, messages and parts say, whichever way they are stored: an event once a
    part is complete (a tool call ended, a text written) and tokens once an answer is complete."""

    agent = "opencode"

    def __init__(self):
        self.sessions, self.messages, self.done, self.running = {}, {}, set(), {}

    def remember(self, key):
        self.done.add(key)
        if len(self.done) > 200000:
            self.done.clear()

    def session(self, info):
        ident = info.get("id")
        if isinstance(ident, str):
            self.sessions[ident] = {"parent": info.get("parentID") or info.get("parent_id"),
                                    "agent": info.get("agent"), "title": info.get("title")}

    def labels(self, session_id):
        info = self.sessions.get(session_id) or {}
        subagent = None
        if info.get("parent"):
            subagent = str(info.get("agent") or "subagent")[:60]
        return (str(session_id)[:80] if session_id else None), subagent

    def event(self, kind, summary, at, details, key, session_id, update=False):
        conversation, subagent = self.labels(session_id)
        return raw(kind, summary, time=when(at), details=details, key=f"opencode:{key}", agent=self.agent,
                   source="opencode", conversation=conversation, subagent=subagent, update=update)

    def message(self, info, final=False):
        ident = info.get("id")
        if not isinstance(ident, str):
            return []
        self.messages[ident] = info
        if len(self.messages) > 50000:
            self.messages.pop(next(iter(self.messages)))
        times = info.get("time") if isinstance(info.get("time"), dict) else {}
        events = []
        if info.get("role") == "assistant" and (times.get("completed") or final) and f"tokens:{ident}" not in self.done:
            tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            usage = {k: tokens[k] for k in ("input", "output", "reasoning") if isinstance(tokens.get(k), int)}
            usage.update({f"cache_{k}": cache[k] for k in ("read", "write") if isinstance(cache.get(k), int)})
            if usage and any(usage.values()):
                self.remember(f"tokens:{ident}")
                turn = Turn()
                turn.add(ident, usage, info.get("modelID"))
                cost = info.get("cost") if isinstance(info.get("cost"), (int, float)) else None
                flushed = turn.flush(self.agent, "opencode", None, f"opencode:tokens:{ident}",
                                     at=epoch(times.get("completed") or times.get("created")),
                                     extra={"provider": info.get("providerID"), "cost": cost})
                conversation, subagent = self.labels(info.get("sessionID"))
                flushed.update(conversation=conversation, subagent=subagent)
                events.append(flushed)
            error = info.get("error")
            if isinstance(error, dict) and f"error:{ident}" not in self.done:
                self.remember(f"error:{ident}")
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                events.append(self.event("session", f"model error: {one_line(error.get('name') or '?', 60)} "
                                                    f"{one_line(data.get('message') or '', 200)}".strip(),
                                         times.get("completed") or times.get("created"),
                                         {"event": "error", "error": error.get("name"), "message": data.get("message")},
                                         f"error:{ident}", info.get("sessionID")))
        return events

    def part(self, part, now, final=False):
        ident, kind = part.get("id"), part.get("type")
        if not isinstance(ident, str) or ident in self.done:
            return []
        message = self.messages.get(part.get("messageID")) or {}
        session_id = part.get("sessionID") or message.get("sessionID")
        times = message.get("time") if isinstance(message.get("time"), dict) else {}
        if kind == "tool":
            return self.tool(part, ident, session_id, now, final)
        if kind == "text" and isinstance(part.get("text"), str) and not part.get("synthetic") and not part.get("ignored"):
            text = part["text"].strip()
            role = message.get("role")
            part_time = part.get("time") if isinstance(part.get("time"), dict) else {}
            if not text or role not in ("user", "assistant"):
                return [] if not final else []
            if role == "assistant" and not (part_time.get("end") or times.get("completed") or final):
                return []  # still being written
            self.remember(ident)
            at = part_time.get("start") or times.get("created")
            if role == "user":
                return [self.event("prompt", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text),
                                                        "by": "agent" if self.labels(session_id)[1] else "user"},
                                   f"prompt:{ident}", session_id)]
            return [self.event("reply", text, at, {"text": cut(text, TEXT_LIMIT), "chars": len(text),
                                                   "model": message.get("modelID")}, f"reply:{ident}", session_id)]
        if kind == "patch" and isinstance(part.get("files"), list):
            self.remember(ident)
            event = files_event([("modified", f) for f in part["files"][:200] if isinstance(f, str)],
                                when(times.get("completed") or times.get("created")), "snapshot",
                                f"opencode:patch:{ident}", self.agent, "opencode", *self.labels(session_id))
            return [event] if event else []
        if kind == "subtask":
            self.remember(ident)
            return [self.event("tool", f"subagent {one_line(part.get('agent') or '', 40)}: "
                                       f"{one_line(part.get('description') or '', 160)}",
                               times.get("created"), {"tool": "subtask", "agent": part.get("agent"),
                                                      "description": part.get("description"),
                                                      "prompt": part.get("prompt")}, f"subtask:{ident}", session_id)]
        return []

    def tool(self, part, ident, session_id, now, final):
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        name = str(part.get("tool") or "?")[:200]
        arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
        times = state.get("time") if isinstance(state.get("time"), dict) else {}
        started = epoch(times.get("start"))
        ended = epoch(times.get("end"))
        details = {"tool": name, "call_id": part.get("callID"), **tool_details(arguments)}
        if status == "completed":
            details["status"] = "ok"
        elif status == "error":
            error = state.get("error")
            details.update(status="denied" if isinstance(error, str) and REJECTED.search(error[:4000]) else "error",
                           error=one_line(error or "", 300))
        elif final:
            details["status"] = "no result recorded"
        elif status == "running" and started is not None and now - started >= RUNNING_AFTER and ident not in self.running:
            details["status"] = "running"
        else:
            return []
        if started is not None and ended is not None and ended >= started:
            details["duration_ms"] = int((ended - started) * 1000)
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        if name == "bash":
            command = arguments.get("command")
            details.update(command=command if isinstance(command, str) else None, workdir=arguments.get("workdir"))
            if isinstance(details.get("arguments"), dict):
                details["arguments"] = {k: v for k, v in details["arguments"].items() if k != "command"}
            if isinstance(metadata.get("exit"), int):
                details["exit_code"] = metadata["exit"]
                if details["status"] == "ok" and metadata["exit"] != 0:
                    details["status"] = "error"
            kind, summary = "command", one_line(command or state.get("title") or "", 200)
            summary += status_text(details)
        elif name in ("edit", "write", "multiedit"):
            path = arguments.get("filePath")
            action = "modified" if name != "write" else "created or replaced"
            kind, summary = "file", f"{action} {one_line(path or '?', 200)} ({name})"
            details.update(action=action, path=path)
        elif name in ("apply_patch", "patch"):
            files = patch_files(arguments.get("patchText") or arguments.get("patch"))
            kind = "file"
            if len(files) == 1:
                details.update(action=files[0][0], path=files[0][1])
                summary = f"{files[0][0]} {one_line(files[0][1], 200)} ({name})"
            else:
                details["files"] = [{"action": a, "path": p} for a, p in files[:100]]
                summary = f"changed {len(files)} files ({name})"
        elif name not in OPENCODE_TOOLS and "_" in name:
            server, _, tool = name.partition("_")
            kind, summary = "mcp", f"{server}: {tool} {arguments_summary(name, arguments)}".strip()
            details.update(server=server, mcp_tool=tool)
        else:
            kind = "tool"
            if name == "task":
                summary = f"subagent {one_line(arguments.get('subagent_type') or '', 40)}: " \
                          f"{one_line(arguments.get('description') or '', 160)}"
            else:
                summary = f"{name} {arguments_summary(name, arguments)}".strip()
        if details["status"] not in ("ok", "running") and kind != "command":
            summary += f" · {details['status']}"
        update = ident in self.running
        if details["status"] == "running":
            self.running[ident] = True
        else:
            self.remember(ident)
            self.running.pop(ident, None)
        events = [self.event(kind, summary, times.get("start"), details, f"tool:{ident}", session_id, update)]
        if details["status"] == "denied":
            events.append(self.event("approval", f"denied: {name} {arguments_summary(name, arguments)}".strip(),
                                     times.get("end") or times.get("start"),
                                     {"question": f"use the tool {name}", "answer": "denied",
                                      "reason": details.get("error"), "tool_call": part.get("callID")},
                                     f"approval:{ident}", session_id))
        return events


class OpenCodeSource:
    """OpenCode's sessions: the SQLite database (~/.local/share/opencode/opencode*.db, v1.2.0 on) and the JSON
    files of older versions (storage/session|message|part/...). A database is copied (bounded, never through
    a link) into a private directory and read there with SQLite's defensive settings; every
    DATABASE_INTERVAL seconds at most while the sandbox runs."""

    def __init__(self, home, from_start=False):
        self.home, self.from_start = home, from_start
        self.state, self.marks, self.seen_rows, self.copied = OpenCodeState(), {}, {}, {}
        self.files, self.since, self.next_read, self.scratch = {}, 0.0, 0.0, None

    def snapshot(self):
        self.since = 0.0 if self.from_start else time.time()
        if not self.from_start:
            for name in self.databases():
                marks = self.read_database(name, marks_only=True)
                if marks:
                    self.marks[name] = marks
        self.started = True

    def databases(self):
        found = regular_files(self.home, OPENCODE_DATA, lambda r: bool(re.fullmatch(r"opencode(?:-[A-Za-z0-9_.-]{1,40})?\.db", r)),
                              lambda r: False, limit=20)
        return sorted(found)

    def poll(self, budget, now, final=False):
        events = []
        if final or now >= self.next_read:
            began = time.monotonic()
            for name in self.databases():
                events += self.read_database(name, now=now, final=final) or []
            # A large database takes a while to copy: read it less often then.
            took = time.monotonic() - began
            self.next_read = now + max(DATABASE_INTERVAL, 10 * took if took > 1 else 0)
        events += self.read_files(now, final)
        if final:
            for info in list(self.state.messages.values()):
                events += self.state.message(info, final=True)
            self.close()
        return events

    def close(self):
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None

    def copy(self, name):
        """A private copy of the database NAME and its WAL, or None (missing, a link, too large, unchanged
        since the last copy: then the old copy)."""
        sources, state = {}, []
        for suffix in ("", "-wal"):
            try:
                fd, info = open_file(self.home, "/".join(OPENCODE_DATA + [name + suffix]))
            except OSError:
                if suffix == "":
                    return None
                continue
            sources[suffix] = (fd, info)
            state.append((suffix, info.st_ino, info.st_size, info.st_mtime_ns))
        try:
            if sum(info.st_size for _, info in sources.values()) > DATABASE_LIMIT:
                return None
            if self.scratch is None:
                self.scratch = tempfile.mkdtemp(prefix="sandbox-ai-activity-")
            target = Path(self.scratch) / re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            if self.copied.get(name) == state and target.exists():
                return target
            for suffix in ("-wal", "-shm", ""):
                with contextlib.suppress(OSError):
                    os.unlink(f"{target}{suffix}")
            for suffix, (fd, info) in sources.items():
                out = os.open(f"{target}{suffix}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600)
                try:
                    offset = 0
                    while offset < info.st_size:
                        chunk = read_at(fd, offset, min(4 << 20, info.st_size - offset))
                        if not chunk:
                            break
                        os.write(out, chunk)
                        offset += len(chunk)
                finally:
                    os.close(out)
            self.copied[name] = state
            return target
        finally:
            for fd, _ in sources.values():
                os.close(fd)

    def read_database(self, name, marks_only=False, now=None, final=False):
        try:
            import sqlite3
        except ImportError:
            return None
        try:
            path = self.copy(name)
        except OSError:
            return None
        if path is None:
            return None
        deadline = time.monotonic() + 10
        try:
            connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=1, isolation_level=None)
        except sqlite3.Error:
            return None
        events, marks = [], dict(self.marks.get(name) or {})
        try:
            if hasattr(connection, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            for pragma in ("trusted_schema=OFF", "cell_size_check=ON", "mmap_size=0", "query_only=ON"):
                connection.execute(f"PRAGMA {pragma}")
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            kinds = dict(connection.execute("SELECT name, type FROM sqlite_master WHERE name IN "
                                            "('session', 'message', 'part')").fetchall())
            if any(kinds.get(table) != "table" for table in ("message", "part")):
                return None
            queries = {"session": "SELECT id, time_updated, parent_id, agent, title FROM session",
                       "message": "SELECT id, time_updated, session_id, data FROM message",
                       "part": "SELECT id, time_updated, session_id, message_id, data FROM part"}
            for table in ("session", "message", "part"):
                if kinds.get(table) != "table":
                    continue
                if marks_only:  # where the table ends now; rows of that very millisecond count as seen
                    value = connection.execute(f"SELECT max(time_updated) FROM {table}").fetchone()[0]
                    marks[table] = value if isinstance(value, int) else 0
                    for (ident,) in connection.execute(f"SELECT id FROM {table} WHERE time_updated = ? LIMIT ?",
                                                       (marks[table], ROW_LIMIT)):
                        self.seen_rows[(name, table, ident)] = marks[table]
                    continue
                rows = connection.execute(f"{queries[table]} WHERE time_updated >= ? ORDER BY time_updated LIMIT ?",
                                          (int(marks.get(table, 0)), ROW_LIMIT)).fetchall()
                for row in rows:
                    ident, updated = row[0], row[1]
                    if not isinstance(ident, str) or not isinstance(updated, int):
                        continue
                    seen_key = (name, table, ident)
                    if self.seen_rows.get(seen_key, -1) >= updated:
                        continue
                    self.seen_rows[seen_key] = updated
                    marks[table] = max(marks.get(table, 0), updated)
                    if table == "session":
                        self.state.session({"id": ident, "parent_id": row[2], "agent": row[3], "title": row[4]})
                        continue
                    data = json_object(row[-1]) if isinstance(row[-1], (str, bytes)) and len(row[-1]) < JSON_FILE_LIMIT else None
                    if data is None:
                        continue
                    if table == "message":
                        events += self.state.message({**data, "id": ident, "sessionID": row[2]}, final)
                        continue
                    if isinstance(row[3], str) and row[3] not in self.state.messages:
                        # A new part of a message from before the start: its role says what a text is.
                        found = connection.execute("SELECT session_id, data FROM message WHERE id = ?",
                                                   (row[3],)).fetchone()
                        message = json_object(found[1]) if found and isinstance(found[1], str) else None
                        if message is not None:
                            self.state.messages[row[3]] = {**message, "id": row[3], "sessionID": found[0]}
                    events += self.state.part({**data, "id": ident, "sessionID": row[2], "messageID": row[3]},
                                              now if now is not None else time.time(), final)
                if len(self.seen_rows) > 400000:
                    self.seen_rows.clear()
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            return events
        finally:
            connection.close()
        self.marks[name] = marks
        return marks if marks_only else events

    def changed_files(self, parts, depth):
        """{relative path: stat} of the JSON files under storage/PARTS changed since the start."""
        since_ns = int(self.since * 1e9) - 5 * 10 ** 9 if self.since else 0
        found = {}
        try:
            top = open_dir(self.home, OPENCODE_DATA + ["storage"] + parts)
        except OSError:
            return found
        try:
            with os.scandir(top) as entries:
                folders = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime_ns >= since_ns:
                            folders.append(entry.name)
                    except OSError:
                        continue
                    if len(folders) >= FILES_LIMIT:
                        break
        except OSError:
            folders = []
        finally:
            os.close(top)
        for folder in sorted(folders):
            for relative, info in regular_files(self.home, OPENCODE_DATA + ["storage"] + parts + [folder],
                                                lambda r: r.endswith(".json") and "/" not in r, lambda r: False,
                                                limit=2000).items():
                if info.st_mtime_ns >= since_ns:
                    found[f"{folder}/{relative}"] = info
        return found

    def read_files(self, now, final):
        """The JSON files of OpenCode before v1.2.0 (storage/session/PROJECT/ID.json, message/SESSION/ID.json,
        part/MESSAGE/ID.json): those written since the start, each again when it changed."""
        events = []
        for kind, parts in (("session", ["session"]), ("message", ["message"]), ("part", ["part"])):
            for relative, info in self.changed_files(parts, 2).items():
                key = (kind, relative)
                if self.files.get(key) == (info.st_mtime_ns, info.st_size):
                    continue
                self.files[key] = (info.st_mtime_ns, info.st_size)
                data = read_small(self.home, "/".join(OPENCODE_DATA + ["storage"] + parts + [relative]))
                value = json_object(data) if data else None
                if value is None:
                    continue
                if kind == "session":
                    self.state.session(value)
                elif kind == "message":
                    events += self.state.message(value, final)
                else:
                    events += self.state.part(value, now, final)
        return events


def opencode_source(home, from_start=False):
    return OpenCodeSource(home, from_start)


# -- sandbox-ai's own logs --

class LogFile:
    """One JSONL log of the log directory, followed from where it ended when the run started (several
    runs may share processes.jsonl or files.jsonl) or, with FROM_START, from its beginning."""

    def __init__(self, directory, name, from_start=False):
        self.directory, self.name, self.follow = Path(directory), name, Follow()
        if not from_start:
            try:
                info = os.stat(self.directory / name, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    self.follow = Follow(info.st_ino, info.st_size)
            except OSError:
                pass

    def records(self, budget, final=False):
        found = []
        while budget[0] > 0:
            before = (self.follow.inode, self.follow.offset)
            try:
                lines = new_lines(self.directory, self.name, self.follow, budget[0], final)
            except OSError:
                break
            budget[0] -= max(1, self.follow.offset - before[1]) if self.follow.inode == before[0] else 1
            for offset, line in lines:
                record = json_object(line)
                if record is not None:
                    found.append((offset, record))
            if (self.follow.inode, self.follow.offset) == before:
                break
        return found


FILE_ACTIONS = {"write": "modified", "create": "created", "delete": "deleted", "move-from": "moved away",
                "move-to": "moved here"}
# The agents' own bookkeeping (transcripts, caches, settings): not what they did to your files.
OWN_FILES = ("/root/.claude/", "/root/.claude.json", "/root/.codex/", "/root/.local/share/opencode/",
             "/root/.local/state/", "/root/.cache/", "/root/.npm/", "/root/.config/opencode/", "/root/.bun/",
             "/root/.local/share/claude/", "/tmp/claude-", "/tmp/.X", "/tmp/tmux-")


class FileChanges:
    """files.jsonl (inotify, with --log-dir): changes, not reads; many in one folder become one event."""

    source = "files"

    def __init__(self, directory, from_start=False):
        self.log = LogFile(directory, "files.jsonl", from_start)

    def poll(self, budget, now, final=False):
        events, groups = [], {}
        for offset, record in self.log.records(budget, final):
            if isinstance(record.get("error"), str):
                events.append(raw("file", f"file log: {one_line(record['error'], 200)}", when(record.get("time")),
                                  {"error": record["error"]}, key=f"files:{offset}", source=self.source))
                continue
            action, path = record.get("action"), record.get("path")
            if action not in FILE_ACTIONS or not isinstance(path, str) or path.startswith(OWN_FILES):
                continue
            folder = path.rsplit("/", 1)[0] or "/"
            groups.setdefault((FILE_ACTIONS[action], folder), []).append((offset, path, record.get("time")))
        for (action, folder), items in groups.items():
            created = {path for (a, _), entries in groups.items() if a == "created" for _, path, _ in entries}
            if action == "modified":
                items = [item for item in items if item[1] not in created]  # created and written: "created"
            if not items:
                continue
            if len(items) > GROUP_FILES:
                names = sorted({path.rsplit("/", 1)[-1] for _, path, _ in items})
                events.append(raw("file", f"{action} {len(names)} files in {one_line(folder, 200)}",
                                  when(items[0][2]), {"action": action, "folder": folder, "count": len(names),
                                                      "names": names[:20]},
                                  key=f"files:{items[0][0]}", source=self.source))
                continue
            seen = set()
            for offset, path, at in items:
                if path in seen:
                    continue
                seen.add(path)
                events.append(raw("file", f"{action} {one_line(path, 200)}", when(at), {"action": action, "path": path},
                                  key=f"files:{offset}", source=self.source))
        return events


class EgressDenied:
    """egress-denied.jsonl: destinations the firewall dropped and name lookups that were refused."""

    source = "egress"

    def __init__(self, directory, from_start=False):
        self.log = LogFile(directory, "egress-denied.jsonl", from_start)

    def poll(self, budget, now, final=False):
        events = []
        for offset, record in self.log.records(budget, final):
            if isinstance(record.get("host"), str):
                summary = f"denied: lookup of {one_line(record['host'], 200)}"
            elif record.get("address") is not None:
                summary = f"denied: {record.get('protocol', '?')} to {record.get('address')}:{record.get('port', '-')}"
            else:
                continue
            details = {"verdict": "denied", "by": "firewall",
                       **{k: record[k] for k in ("host", "address", "protocol", "port") if k in record}}
            events.append(raw("network", summary, when(record.get("time")), details, key=f"egress:{offset}",
                              source=self.source))
        return events


REFUSED_EVENTS = ("denied", "tls-refused", "tls-upstream-failed", "error")


def attribution(record):
    """(details, text) of the rule and process a connection record names, when the proxy recorded them."""
    details, parts = {}, []
    rule = record.get("rule") if record.get("rule") is not None else record.get("rule_id")
    if isinstance(rule, dict):
        rule = rule.get("id") or rule.get("name") or rule.get("rule")
    if isinstance(rule, (str, int)) and not isinstance(rule, bool) and str(rule):
        details["rule"] = rule
        parts.append(f"rule {one_line(rule, 60)}")
    process = record.get("process")
    if isinstance(process, dict):
        details["process"] = {k: process[k] for k in ("pid", "name", "comm", "exe", "command", "cmdline", "ppid")
                              if k in process}
        name = process.get("name") or process.get("comm") or process.get("exe") or process.get("command")
    else:
        name = process if isinstance(process, str) else record.get("program") or record.get("command")
        if isinstance(name, str):
            details["process"] = name
        if isinstance(record.get("pid"), int):
            details["pid"] = record["pid"]
    if isinstance(name, str) and name:
        parts.append(one_line(name.split()[0].rsplit("/", 1)[-1], 60))
    return details, (f" ({', '.join(parts)})" if parts else "")


class Connections:
    """The run's connections-*.jsonl (the proxy): every refusal and push, the first request to each host;
    repeats are counted and shown at most once a minute and when the run ends."""

    source = "proxy"

    def __init__(self, directory, name, from_start=True):
        self.log = LogFile(directory, name, from_start) if name else None
        self.repeats = {}  # (verdict, target): {"count", "since", "methods"}

    def target(self, record):
        host = record.get("host")
        if not isinstance(host, str) or not host:
            url = record.get("url")
            match = re.match(r"[a-z][a-z0-9+.-]*://([^/?#]+)", url) if isinstance(url, str) else None
            host = match.group(1) if match else record.get("destination")
        return str(host or "?")

    def poll(self, budget, now, final=False):
        if self.log is None:
            return []
        events = []
        for offset, record in self.log.records(budget, final):
            event = record.get("event")
            if not isinstance(event, str):
                continue
            at = when(record.get("time"))
            if event in ("taint", "untaint"):  # the run's taint: untrusted content entered it, or the human took it back
                state = record.get("taint") if event == "taint" else record.get("was")
                source = state.get("source") if isinstance(state, dict) and isinstance(state.get("source"), dict) else {}
                summary = f"TAINTED: untrusted content from {one_line(str(source.get('what', '?')), 200)} entered the run" \
                    if event == "taint" else f"taint taken back by {one_line(str(record.get('by', 'the human')), 80)}"
                events.append(raw("network", summary, at, {"verdict": "tainted" if event == "taint" else "allowed",
                                                            "event": event, "taint": state},
                                  key=f"proxy:{offset}", source=self.source))
                continue
            target = self.target(record)
            extra, attributed = attribution(record)
            push = record.get("push")
            if isinstance(push, dict):
                verdict = "allowed" if push.get("ok") else "denied"
                refs = ", ".join(str(r) for r in (push.get("refs") or [])[:5] if isinstance(r, str))
                events.append(raw("network", f"push {'checked' if verdict == 'allowed' else 'REFUSED'}: "
                                             f"{one_line(refs or record.get('url') or target, 200)}{attributed}", at,
                                  {"verdict": verdict, "event": "push", "url": record.get("url"), "refs": refs,
                                   "findings": push.get("findings") if isinstance(push.get("findings"), list) else None,
                                   **extra}, key=f"proxy:{offset}", source=self.source))
                continue
            verdict = "denied" if event in REFUSED_EVENTS else "allowed"
            key = (verdict, event if verdict == "denied" else "", target)
            entry = self.repeats.get(key)
            if entry is not None:
                entry["count"] += 1
                if isinstance(record.get("method"), str):
                    entry["methods"][record["method"][:10]] = entry["methods"].get(record["method"][:10], 0) + 1
                continue
            self.repeats[key] = {"count": 0, "since": now, "methods": {}, "event": event}
            why = record.get("reason") or record.get("error")
            what = f"{record.get('method')} {record.get('url')}" if record.get("method") and record.get("url") else \
                target if event in ("denied", "http") else f"{event} {target}"
            summary = f"{verdict}: {one_line(what, 200)}"
            if verdict == "denied" and why:
                summary += f" ({one_line(why, 120)})"
            elif record.get("status"):
                summary += f" → {record.get('status')}"
            details = {"verdict": verdict, "event": event, "target": target,
                       **{k: record[k] for k in ("destination", "method", "url", "status", "reason", "error",
                                                 "protocol", "bytes_in", "bytes_out", "approval") if k in record},
                       **extra}
            events.append(raw("network", summary + attributed, at, details, key=f"proxy:{offset}", source=self.source))
        for key, entry in list(self.repeats.items()):
            if entry["count"] and (final or now - entry["since"] >= REPEAT_WINDOW):
                verdict, _, target = key
                methods = ", ".join(f"{m} {n}" for m, n in sorted(entry["methods"].items()))
                events.append(raw("network", f"{verdict}: {entry['count']} more {'request' if methods else 'connection'}"
                                             f"(s) to {one_line(target, 200)}" + (f" ({methods})" if methods else ""),
                                  stamp(now), {"verdict": verdict, "event": entry["event"], "target": target,
                                               "count": entry["count"], "methods": entry["methods"], "repeats": True},
                                  source=self.source))
                entry.update(count=0, since=now, methods={})
        return events


class Approvals:
    """The questions of this run's sandbox (CONTROL_DIR/approvals: ID.json, answered: ID.done)."""

    source = "approvals"

    def __init__(self, control_dir):
        self.directory = Path(control_dir) / "approvals" if control_dir else None
        self.done = set()

    def poll(self, budget, now, final=False):
        if self.directory is None:
            return []
        found = regular_files(self.directory, [], lambda r: r.endswith((".json", ".done")), lambda r: False,
                              limit=2000)
        events = []
        for name in sorted(found):
            ident = name[:-5]
            if not name.endswith(".json") or ident in self.done or not re.fullmatch(r"[0-9-]{1,40}", ident):
                continue
            answered = f"{ident}.done" in found
            if not answered and not final:
                continue
            question = json_object(read_small(self.directory, name, 1 << 16) or b"")
            if question is None:
                continue
            result = json_object(read_small(self.directory, f"{ident}.done", 1 << 16) or b"") if answered else None
            result = result or {}
            self.done.add(ident)
            try:
                text = module("approvals").describe(question)
            except Exception:  # noqa: BLE001 - an approvals module without describe(): the kind is enough
                text = str(question.get("kind", "question"))
            answer = result.get("decision") if isinstance(result.get("decision"), str) else \
                "unanswered" if not answered else "?"
            scope = result.get("scope") if isinstance(result.get("scope"), str) else None
            # --ask allow answered it, not a human (approvals.AUTO_REASON, AUTO_REFUSED).
            auto = str(result.get("reason") or "").startswith(("allowed automatically", "refused automatically"))
            events.append(raw("approval", f"{answer}{' (' + scope + ')' if scope else ''}"
                              f"{' automatically (--ask allow)' if auto else ''}: {text}",
                              when(result.get("time") or question.get("time")),
                              {"question": text, "kind": question.get("kind"), "answer": answer, "scope": scope,
                               "reason": result.get("reason"), "asked": question.get("time"), "automatic": auto,
                               **{k: question[k] for k in ("destination", "host", "name", "url", "refs") if k in question}},
                              key=f"approval:{ident}", source=self.source))
        return events


# -- the collector --

class Collector:
    """Follows the agents' transcripts in HOME and the run's logs in LOG_DIR while a sandbox runs, and
    appends the timeline to LOG_DIR/activity.jsonl (or hands it to SINK)."""

    def __init__(self, log_dir=None, home=None, run=None, agent=None, session=None, control_dir=None,
                 connection_log=None, secrets=(), sink=None, from_start=False, interval=INTERVAL, clock=time.time):
        self.path = Path(log_dir) / ACTIVITY_FILE if log_dir else None
        self.run, self.agent, self.session, self.sink, self.clock = run, agent, session, sink, clock
        self.redact, self.interval, self.numbers = Redactor(secrets), interval, counter()
        self.sources = transcript_sources(home, from_start) if home else []
        if log_dir:
            self.sources += [FileChanges(log_dir, from_start), EgressDenied(log_dir, from_start),
                             Connections(log_dir, connection_log)]
        if control_dir:
            self.sources.append(Approvals(control_dir))
        self.stopping, self.lock, self.thread, self.full, self.started = threading.Event(), threading.Lock(), None, False, None
        self.errors = 0

    def snapshot(self):
        for source in self.sources:
            if hasattr(source, "snapshot"):
                source.snapshot()

    def start(self, details=None):
        """Note where the transcripts end now, write the session's start and poll in a thread."""
        self.started = self.clock()
        self.snapshot()
        if details is not None:
            self.emit([raw("session", details.pop("summary", f"{self.agent} started"), stamp(self.started),
                           {"event": "start", **details}, key="start")])
        self.thread = threading.Thread(target=self.collect, name="sandbox-ai-activity", daemon=True)
        self.thread.start()
        return self

    def collect(self):
        """The polling thread: until finish()."""
        while not self.stopping.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - the timeline must never take the sandbox down
                self.errors += 1
                if self.errors > 20:
                    return

    def poll(self, final=False, budget=POLL_BUDGET):
        with self.lock:
            events, left = [], [budget]
            now = self.clock()
            for source in self.sources:
                try:
                    events += source.poll(left, now, final)
                except Exception:  # noqa: BLE001 - one damaged source does not stop the others
                    continue
            self.emit(events)
            return len(events), left[0] <= 0

    def emit(self, events):
        if not events:
            return
        records = [finalize(event, self.run, self.session, self.agent, self.redact, self.numbers) for event in events]
        records.sort(key=lambda r: r["time"])
        if self.sink is not None:
            self.sink(records)
        elif self.path is not None and not self.full:
            try:
                self.full = not append(self.path, records)
            except OSError:
                self.errors += 1

    def finish(self, status=None, details=None):
        """Stop polling, read what is left (bounded by FINAL_SECONDS) and write the session's end."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(10)
        deadline = time.monotonic() + FINAL_SECONDS
        while True:
            _, more = self.poll(final=True)
            if not more or time.monotonic() > deadline:
                break
        ended = self.clock()
        info = {"event": "stop", **({"status": status} if isinstance(status, int) else {}), **(details or {})}
        if self.started is not None:
            info["duration_s"] = round(ended - self.started, 1)
        text = f"{self.agent or 'sandbox'} ended" + (f" (exit status {status})" if isinstance(status, int) else "")
        if self.started is not None:
            text += f" after {duration_text(ended - self.started)}"
        self.emit([raw("session", text, stamp(ended), info, key="stop")])


def duration_text(seconds):
    seconds = int(max(0, seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def transcript_sources(home, from_start=False):
    return [claude_source(home, from_start), codex_source(home, from_start), opencode_source(home, from_start)]


# -- hooks of the launcher and of sessions --

def session_name(a):
    """The id of the start a launch belongs to (sessions start the launcher with --start-id ID, a kept task has
    a.start_key; versions before 0.1.30 only with --pod-name agent-session-NAME), or None."""
    if getattr(a, "start_id", None) and getattr(a, "tmux_socket_dir", None):
        return a.start_id
    if getattr(a, "start_key", None):
        return a.start_key  # a kept task: its start's id
    pod = getattr(a, "pod_name", None) or ""
    if getattr(a, "tmux_socket_dir", None) and pod.startswith("agent-session-"):
        return pod[len("agent-session-"):]
    return getattr(a, "session", None)


def start(a, home, connection_log=None, env=None, secret_names=(), workspaces=None):
    """The collector of a launch (agent_sandbox.launch), started; None without a log directory. ENV: the
    agent's environment (the values of secret variables are redacted wherever they appear); WORKSPACES:
    [{"host", "sandbox"}] (the app maps paths with it). Never fails the launch: a problem is reported and
    the sandbox runs without a timeline."""
    try:
        if not getattr(a, "log_dir", None):
            return None
        log_dir = Path(a.log_dir).expanduser()
        names = set(secret_names)
        values = [value for name, value in (env or {}).items()
                  if isinstance(value, str) and (name in names or SECRET_NAME.search(name))]
        workspaces = list(workspaces or [])
        collector = Collector(log_dir, home, a.run_id, a.agent, session=session_name(a),
                              control_dir=getattr(a, "control_dir", None), connection_log=connection_log,
                              secrets=values)
        place = workspaces[0]["host"] if workspaces else "?"
        collector.start({"summary": f"{a.agent} started in {place} ({a.backend})", "agent": a.agent,
                         "backend": a.backend, "workspaces": workspaces, "log_dir": str(log_dir),
                         "home": str(home) if getattr(a, "state_dir", None) else None,
                         "pid": os.getpid(), "proxy": bool(connection_log)})
        return collector
    except Exception as exc:  # noqa: BLE001 - see above
        print(f"agent-sandbox: activity: not recorded: {one_line(exc, 300)}", file=sys.stderr)
        return None


def finish(collector, status=None):
    """The end of a launch: the last read and the session's end (never raises)."""
    if collector is None:
        return
    try:
        collector.finish(status)
    except Exception as exc:  # noqa: BLE001 - the agent's exit status matters more
        print(f"agent-sandbox: activity: the last read failed: {exc!r}"[:300], file=sys.stderr)


SESSION_EVENTS = {"pause": "paused", "resume": "resumed", "attach": "terminal attached", "detach": "terminal detached",
                  "stop": "stop requested"}


def session_event(name, event, runs_dir=None):
    """Note EVENT (pause, resume, attach, detach, stop) of the running session NAME in its activity log
    (found through the run registry RUNS_DIR); True if written. Never raises."""
    try:
        try:
            sessions = module("sessions")
            run = sessions.current_run(name, sessions.load(name))
        except (OSError, ValueError):
            run = f"session-{name}"
        runs_dir = Path(runs_dir) if runs_dir else module("agent_sandbox").runs_dir()
        record = json.loads((runs_dir / run / "run.json").read_text())
        log_dir = record.get("log_dir") if isinstance(record, dict) else None
        if not isinstance(log_dir, str) or not log_dir:
            return False
        event_record = finalize(raw("session", f"session {name} {SESSION_EVENTS.get(event, event)}",
                                    stamp(time.time()), {"event": event}, source="sandbox-ai"),
                                run, name, record.get("agent"), Redactor())
        return append(Path(log_dir) / ACTIVITY_FILE, [event_record])
    except Exception:  # noqa: BLE001 - a session command must not fail over its timeline
        return False


# -- reading the timeline --

def read_records(path, keep=None, tail=0, position=None):
    """The records of activity.jsonl PATH that KEEP(record) wants (all without KEEP); with TAIL, only the
    last TAIL of them. Damaged lines are skipped. POSITION (a Follow) is set to the end of the last complete line
    read, so that --follow goes on exactly there: a record appended meanwhile is neither lost nor shown twice."""
    found = deque(maxlen=tail) if tail else []
    try:
        fd = os.open(path, FILE_FLAGS)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        os.set_blocking(fd, True)
        if position is not None:
            position.inode, position.offset = os.fstat(fd).st_ino, 0
        with os.fdopen(os.dup(fd), "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # still being written: --follow reads it once it is complete
                if position is not None:
                    position.offset += len(line)
                record = json_object(line)
                if record is not None and isinstance(record.get("kind"), str) and (keep is None or keep(record)):
                    found.append(record)
    finally:
        os.close(fd)
    return list(found)


def matcher(run=None, session=None, kinds=None, since=None, text=None):
    """KEEP(record) for the filters given."""
    kinds = set(kinds or ())
    text = text.lower() if text else None

    def keep(record):
        if run and record.get("run") != run:
            return False
        if session and record.get("session") != session:
            return False
        if kinds and record.get("kind") not in kinds:
            return False
        if since is not None:
            seconds = epoch(record.get("time"))
            if seconds is None or seconds < since:
                return False
        if text and text not in json.dumps(record, ensure_ascii=False).lower():
            return False
        return True
    return keep


def runs_in(path):
    """[{run, session, agent, first, last, events, kinds, status}] of the runs in activity.jsonl PATH."""
    runs = {}
    for record in read_records(path):
        run = record.get("run") or "-"
        entry = runs.setdefault(run, {"run": run, "session": record.get("session"), "agent": record.get("agent"),
                                      "first": record.get("time"), "last": record.get("time"), "events": 0,
                                      "kinds": {}, "status": None, "running": None})
        entry["events"] += 1
        entry["kinds"][record["kind"]] = entry["kinds"].get(record["kind"], 0) + 1
        if isinstance(record.get("time"), str):
            entry["first"] = min(filter(None, [entry["first"], record["time"]]))
            entry["last"] = max(filter(None, [entry["last"], record["time"]]))
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        if record["kind"] == "session" and details.get("event") == "start":
            entry["running"], entry["agent"] = True, record.get("agent") or entry["agent"]
            entry["workspaces"] = details.get("workspaces")
        if record["kind"] == "session" and details.get("event") == "stop":
            entry["running"], entry["status"] = False, details.get("status")
    return sorted(runs.values(), key=lambda r: r.get("first") or "")


def logs_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"


def running_record(run):
    """run.json of the running sandbox RUN, or None."""
    try:
        return module("net").running().get(run)
    except Exception:  # noqa: BLE001 - no registry: not running
        return None


def find_run(run):
    """The log directory whose activity.jsonl holds RUN: the running sandbox's, else one of the default
    log directories (searched); None if none does."""
    record = running_record(run)
    if record and isinstance(record.get("log_dir"), str):
        return Path(record["log_dir"])
    needle = json.dumps(run).encode()
    try:
        directories = sorted((p for p in logs_root().iterdir() if p.is_dir() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for directory in directories:
        try:
            fd = os.open(directory / ACTIVITY_FILE, FILE_FLAGS)
        except OSError:
            continue
        try:
            if os.fstat(fd).st_size and stat.S_ISREG(os.fstat(fd).st_mode):
                with mmap.mmap(fd, 0, prot=mmap.PROT_READ) as data:
                    if data.find(b'"run": ' + needle) >= 0:
                        return directory
        except (OSError, ValueError):
            continue
        finally:
            os.close(fd)
    return None


def session_key(name):
    """The id of the session called NAME (its name, or its id: renamed sessions keep their id); NAME itself
    when there is no such session."""
    try:
        return module("sessions").resolve(name)
    except (OSError, ValueError):
        return name


def session_log_dir(name):
    """The log directory of the session NAME: the running sandbox's, else the one its options choose."""
    sessions = module("sessions")
    name = session_key(name)
    try:
        run = sessions.current_run(name, sessions.load(name))
    except (OSError, ValueError):
        run = f"session-{name}"
    record = running_record(run)
    if record and isinstance(record.get("log_dir"), str):
        return Path(record["log_dir"])
    data = sessions.load(name)
    a, _ = sessions.effective(data)
    if a is None:
        raise ValueError(f"cannot read the options of session {name}")
    if a.log_dir:
        return (Path(data["cwd"]) / Path(a.log_dir).expanduser()).resolve()
    workspace = (Path(data["cwd"]) / a.workspaces[0]).resolve()
    return module("agent_sandbox").default_log_dir(workspace)


# -- the human-readable timeline --

KIND_STYLES = {"prompt": "accent", "reply": "bold", "tool": None, "command": "warn", "file": "ok", "network": "muted",
               "approval": "warn", "mcp": "accent", "session": "muted", "tokens": "dim"}


def safe(value):
    return module("agent_sandbox").printable(value).replace("\n", "\\n").replace("\t", " ")


def record_style(record):
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    if details.get("verdict") == "denied" or details.get("status") in ("error", "denied") or \
            details.get("answer") in ("deny", "denied"):
        return "bad"
    return KIND_STYLES.get(record.get("kind"))


def local_time(record):
    seconds = epoch(record.get("time"))
    return (time.strftime("%Y-%m-%d", time.localtime(seconds)), time.strftime("%H:%M:%S", time.localtime(seconds))) \
        if seconds is not None else ("?", "??:??:??")


SANDBOX_SOURCES = {"files", "egress", "proxy", "approvals", "sandbox-ai"}


def agent_label(record):
    """Who an event is about: the agent (and its subagent) for what its transcript says, "sandbox" for what
    sandbox-ai observed (files, network, approvals, the session)."""
    if record.get("source") in SANDBOX_SOURCES:
        return "sandbox"
    label = str(record.get("agent") or "-")
    if record.get("subagent"):
        label += "/" + str(record["subagent"]).split(":")[0]
    return safe(label)[:16]


class TextOutput:
    """The timeline for a terminal: a date line when the day changes, then time, kind, agent and summary
    (the summary wraps under itself); with VERBOSE the details below each event."""

    def __init__(self, out=print, verbose=False, colour=None):
        self.t = module("textout")
        self.out, self.verbose = out, verbose
        self.colour = self.t.colour_enabled() if colour is None else colour
        self.day = None

    def show(self, record):
        t, day_time = self.t, local_time(record)
        if day_time[0] != self.day:
            self.day = day_time[0]
            t.section(day_time[0], self.out, self.colour)
        kind = str(record.get("kind"))[:8]
        style = record_style(record)
        lead = f"  {day_time[1]}  {t.style(kind.ljust(8), style, self.colour) if style else kind.ljust(8)}  " \
               f"{agent_label(record).ljust(14)}  "
        room = max(20, t.width() - t.visible_len(lead))
        summary = safe(record.get("summary") or "")
        if record.get("update"):
            summary += t.style("  (update)", "muted", self.colour)
        lines = t.wrap(summary, room)
        self.out(lead + lines[0])
        for more in lines[1:]:
            self.out(" " * t.visible_len(lead) + more)
        if self.verbose:
            details = record.get("details") if isinstance(record.get("details"), dict) else {}
            items = [(key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                     for key, value in details.items() if value not in (None, "", [], {})]
            items += [(name, record[name]) for name in ("source", "conversation", "id") if record.get(name)]
            indent = " " * t.visible_len(lead)
            width = max((len(safe(key)) for key, _ in items), default=0)
            room = max(20, t.width() - len(indent) - width - 2)
            for key, value in items:
                lines = t.wrap(safe(cut(value, 600)), room)
                self.out(indent + t.style(safe(key).ljust(width), "muted", self.colour) + "  " + lines[0])
                for more in lines[1:]:
                    self.out(indent + " " * (width + 2) + more)


# -- the command --

def follow_file(path, keep, show, interval=0.5, stop=None, start_at_end=True, position=None):
    """Show (SHOW(record)) every new record of PATH that KEEP wants until STOP is set or Ctrl-C; from POSITION
    (where read_records stopped) when given, else from the end (START_AT_END) or the start."""
    follow = position or Follow()
    if start_at_end and position is None:
        try:
            info = os.stat(path, follow_symlinks=False)
            follow = Follow(info.st_ino, info.st_size)
        except OSError:
            pass
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            lines = new_lines(Path(path).parent, Path(path).name, follow, READ_LIMIT)
        except OSError:
            lines = []
        for _, line in lines:
            record = json_object(line)
            if record is not None and isinstance(record.get("kind"), str) and keep(record):
                show(record)
        if not lines:
            stop.wait(interval)


def kinds_value(values):
    kinds = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip().lower()
            if part:
                if part not in KINDS:
                    raise ValueError(f"--kind: no kind {part!r}; kinds: {', '.join(KINDS)}")
                kinds.append(part)
    return kinds


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai activity", description="What the agents did in a sandbox: prompts, "
                                "replies, tool calls, commands, file changes, network, approvals, tokens.",
                                epilog=f"Kinds: {', '.join(KINDS)}. TIME: 10m, 2h, 3d, 14:30, 2026-11-04 or an ISO "
                                "time.")
    p.add_argument("run", nargs="?", help="A run (see sandbox-ai net list, or --runs); default: every run")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-s", "--session", metavar="NAME", help="The activity of a named session")
    where.add_argument("-p", "--project", type=Path, metavar="PATH", help="Every run of a project (its default "
                       "log directory)")
    where.add_argument("-l", "--log-dir", type=Path, metavar="DIR", help="Every run that logged into DIR")
    where.add_argument("--home", type=Path, metavar="DIR", help="Read the agents' transcripts in a sandbox HOME "
                       "(a session's home/ or a --state-dir) directly")
    p.add_argument("-f", "--follow", action="store_true", help="Go on showing new events (Ctrl-C ends)")
    p.add_argument("--since", metavar="TIME", help="Only events from TIME on")
    p.add_argument("-k", "--kind", action="append", metavar="KIND[,KIND]", help="Only these kinds; repeatable")
    p.add_argument("-g", "--grep", metavar="TEXT", help="Only events that contain TEXT")
    p.add_argument("-n", "--tail", type=int, default=0, metavar="N", help="Only the last N events (before --follow)")
    p.add_argument("-v", "--verbose", action="store_true", help="Each event's details too")
    p.add_argument("-j", "--json", action="store_true", help="JSON lines, one record per event")
    p.add_argument("--runs", action="store_true", help="List the runs of the log directory instead")
    return p


def run_id(name):
    """The run NAME stands for: a run id as it is, the name of a running sandbox (sandbox-ai list), or the last
    run of a kept start of that name; NAME itself when it is none of them."""
    try:
        net = module("net")
        runs = net.running()
        if name not in runs:
            return net.choose(runs, name).get("run") or name
    except ValueError:
        try:
            sessions = module("sessions")
            key = sessions.resolve(name)
            return sessions.current_run(key, sessions.load(key))
        except (OSError, ValueError):
            pass
    return name


def locate(a):
    """(log directory, run filter, session filter) for the arguments A."""
    if a.run:
        a.run = run_id(a.run)  # a name works as well as a run id
    if a.log_dir:
        return a.log_dir.expanduser(), a.run, None
    if a.session:
        return session_log_dir(a.session), a.run, session_key(a.session)
    if a.project:
        return module("agent_sandbox").default_log_dir(a.project.expanduser().resolve()), a.run, None
    if a.run:
        directory = find_run(a.run)
        if directory is None:
            raise ValueError(f"no activity of a run {a.run!r} found (a run with --log-dir elsewhere: add --log-dir)")
        return directory, a.run, None
    return module("agent_sandbox").default_log_dir(Path(os.getcwd()).resolve()), None, None


def print_runs(runs, out=print, as_json=False):
    if as_json:
        out(json.dumps(runs, indent=2, ensure_ascii=False))
        return
    t = module("textout")
    rows = []
    for entry in runs:
        state = "running" if entry.get("running") else "ended" if entry.get("running") is False else "?"
        if isinstance(entry.get("status"), int):
            state += f" ({entry['status']})"
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(entry["kinds"].items(), key=lambda kv: -kv[1])[:5])
        rows.append([safe(entry["run"]), safe(entry.get("agent") or "-"), safe(entry.get("session") or "-"),
                     safe(entry.get("first") or "-")[:19].replace("T", " "),
                     t.style(state, "ok" if entry.get("running") else "muted"), str(entry["events"]), safe(kinds)])
    t.table(["RUN", "AGENT", "SESSION", "STARTED (UTC)", "STATE", "EVENTS", "MOST"], rows, out, indent=0,
            empty="no activity recorded here")


def home_timeline(a, keep, show):
    """--home: the transcripts of a sandbox HOME, read directly (from their start)."""
    home = a.home.expanduser()
    if not home.is_dir() or home.is_symlink():
        raise ValueError(f"no directory {home}")
    shown = []

    def sink(records):
        for record in records:
            if keep(record):
                shown.append(record)
    collector = Collector(home=home, sink=sink, from_start=True)
    while collector.poll(final=not a.follow)[1]:
        pass
    collector.poll(final=not a.follow)
    shown.sort(key=lambda r: r["time"])
    for record in shown[-a.tail:] if a.tail else shown:
        show(record)
    if a.follow:
        collector.sink = lambda records: [show(r) for r in records if keep(r)]
        while True:
            time.sleep(INTERVAL)
            collector.poll()


def cli(argv, out=None):
    a = parser().parse_args(argv)
    out = out or (lambda text: print(text, flush=True))
    kinds = kinds_value(a.kind)
    since = since_value(a.since) if a.since else None
    if a.tail < 0:
        raise ValueError("--tail must not be negative")
    printer = TextOutput(out, a.verbose)

    def show(record):
        if a.json:
            out(json.dumps(record, ensure_ascii=False))
        else:
            printer.show(record)
    if a.home:
        keep = matcher(a.run, None, kinds, since, a.grep)
        home_timeline(a, keep, show)
        return 0
    directory, run, session = locate(a)
    path = Path(directory) / ACTIVITY_FILE
    if a.runs:
        print_runs(runs_in(path), out, a.json)
        return 0
    if not path.exists() and not a.follow:
        print(f"sandbox-ai activity: nothing recorded in {safe(directory)} yet (the timeline is written while a "
              "sandbox with a log directory runs)", file=sys.stderr)
        return 1
    keep = matcher(run, session, kinds, since, a.grep)
    position = Follow()
    records = read_records(path, keep, a.tail, position)
    records.sort(key=lambda r: str(r.get("time")))
    for record in records:
        show(record)
    if not records and not a.json and not a.follow:
        out(module("textout").style("no events" + (" match" if kinds or since or a.grep else ""), "muted"))
    if a.follow:
        # From where the listing stopped; without a file yet, from its start once it appears.
        follow_file(path, keep, show, start_at_end=False, position=position if position.inode is not None else None)
    return 0


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
        print(f"sandbox-ai activity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
