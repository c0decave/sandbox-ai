#!/usr/bin/env python3
"""sandbox-ai report: one report of what a sandbox was and did - its options, statistics, every destination it
tried and why it was allowed or refused, and a security report of everything it tried or did that was not
allowed - as Markdown, HTML or PDF, with a table of contents and links.

  sandbox-ai report NAME [NAME ...]       a run or a start, running or stopped (a run id, or the name `sandbox-ai
                                          list` shows): a start with all its runs and its parts - its MCP servers,
                                          helpers and team (--no-parts: without them); several: one report
  sandbox-ai report --session NAME        the same for a session, by its name or id
  sandbox-ai report --all                 every run: the running ones and those of the default log directories
  sandbox-ai report --project PATH        every run of a project (its default log directory)
  sandbox-ai report --log-dir DIR         every run that logged into DIR

  --format md|html|pdf   the format (default: the suffix of -o, else md)
  -o FILE                write the report to FILE (mode 0600); without it Markdown and HTML go to standard output
  --since TIME, --until TIME   only what happened from / up to TIME (10m, 2h, 3d, 14:30, 2026-11-04 or an ISO time)
  --json                 the gathered data as JSON instead of a report

The report is made only from what sandbox-ai records: the run registry (run.json of a running sandbox, and through
its supervisor what was allowed during the run), the start directories and their histories, and the log directory
of each run: activity.jsonl, safeguards.jsonl, the proxy's connections-*.jsonl, egress-denied.jsonl, dns.jsonl,
canaries.jsonl, vpn.jsonl, tools.jsonl, mcp-calls.jsonl, devices.jsonl, workspaces.jsonl, files.jsonl and
processes.jsonl, and the MCP servers' own mcp-calls.jsonl. Each log is read from its end and only so far (the logs
are not rotated): the appendix names every file read, how much of it, and what could not be read. Secrets never
appear: every text goes through the activity log's redaction (the token formats, credential patterns and the run's
canary tokens).

The PDF is written by pdfdoc.py (no dependency): its bookmarks are the report's sections and findings, and the
table of contents and the links from the summary to each finding are links inside the document.

Standard library only.
"""
import argparse
import collections
import contextlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
FORMATS = ("md", "html", "pdf")
SUFFIXES = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".pdf": "pdf"}
TAIL_BYTES = 32 << 20      # the newest part of one log that is read
TOTAL_BYTES = 256 << 20    # all logs of one report together
SMALL_TAIL = 8 << 20       # files.jsonl and processes.jsonl: only counted
FINDINGS_LIMIT = 500       # groups of findings shown one by one; the rest are counted
DESTINATIONS_LIMIT = 400
LOOKUPS_LIMIT = 300
APPROVALS_LIMIT = 300
SHOW_SECONDS = 5           # a running sandbox's answer to `net show`, at most
TEXT_LIMIT = 600
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_TITLES = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Information"}
# kind: (severity, title). A canary's and a VPN check's severity depend on the event (see severity()).
KINDS = {
    "canary": ("high", "a canary went off"),
    "vpn": ("high", "a VPN or Tor check failed"),
    "guard": ("high", "a safety check failed"),
    "push": ("high", "push refused by the privacy check"),
    "publish": ("medium", "publishing refused"),
    "rule": ("medium", "refused by a rule"),
    "policy": ("medium", "refused by the administrator's policy"),
    "mode": ("medium", "refused by the security mode"),
    "taint": ("medium", "untrusted content entered the run"),
    "device": ("medium", "device refused"),
    "approval-denied": ("medium", "a question answered with no"),
    "killswitch": ("medium", "cut by the kill switch"),
    "approval-timeout": ("low", "refused without an answer"),
    "network": ("low", "connection refused by the proxy"),
    "tls": ("low", "TLS refused"),
    "firewall": ("low", "connection dropped by the firewall"),
    "dns": ("low", "name lookup refused"),
    "oom": ("low", "out of memory: the kernel ended a process"),
    "memory": ("info", "process ended: memory nearly full"),
    "supervisor-memory": ("info", "the supervisor dropped its caches"),
    "untaint": ("info", "taint taken back"),
}
ALLOWED_EVENTS = ("http", "tls", "tcp", "tls-passthrough", "upgraded")
REFUSED_EVENTS = ("denied", "tls-refused", "cut")
FAILED_EVENTS = ("error", "tls-upstream-failed")
CONNECTION_LOG = re.compile(r"connections-(\d{8}-\d{6})-(.+)\.jsonl")
CUT_MARK = "cut by the kill switch"
# What differs between equal refusals: left out when they are counted as one (derive()).
VOLATILE = {"time", "connection", "source", "bytes_in", "bytes_out", "ms", "request", "response", "id", "status",
            "arguments", "sandbox", "client_hello", "alpn", "url", "process"}
URL_PATH = re.compile(r"^([a-z][a-z0-9+.-]*://[^/?#\s]+)\S*")  # a URL to its scheme and host
URL_QUERY = re.compile(r"([a-z][a-z0-9+.-]*://[^/?#\s]+/?)[^\s)]+")  # every URL in a text, to its scheme and host
DERIVED_LIMIT = 20000      # kinds of refusals of one run kept for its findings
NETWORK_KINDS = ("network", "firewall", "dns", "tls")  # counted per host, not per request
SECRET_OPTION = re.compile(r"env|token|key|secret|pass|cred|header|auth|cookie", re.I)

_modules = {}
_modules_lock = threading.Lock()


def module(name):
    """sandbox-ai's module NAME, loaded by path as the other commands do."""
    with _modules_lock:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_report", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def version():
    try:
        for line in (HERE / "agent_sandbox.py").read_text().splitlines():
            if line.startswith("__version__ = "):
                return line.split('"')[1]
    except OSError:
        pass
    return "unknown"


# -- text and time --

def printable(value, limit=TEXT_LIMIT):
    """VALUE on one line: control characters as escapes, at most LIMIT characters."""
    text = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(value if value is not None else ""))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def epoch(value):
    return module("activity").epoch(value)


def utc(seconds):
    """'YYYY-MM-DD HH:MM:SSZ' of SECONDS, or '-'."""
    if seconds is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(seconds))


def utc_text(value):
    return utc(epoch(value))


def duration(seconds):
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def size_text(count):
    count = int(count or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


class Scrubber:
    """Every text of the report through the activity log's redaction (the connection log's token formats and
    credential patterns, header lines, private keys, and the exact VALUES given: the run's canary tokens), on one
    line and bounded."""

    def __init__(self, values=()):
        self.values = [v for v in values if isinstance(v, str) and len(v) >= 6]
        try:
            self.inner = module("activity").Redactor(self.values)
        except Exception:  # noqa: BLE001 - without the module: the values are still taken out
            self.inner = None

    def __call__(self, text, limit=TEXT_LIMIT):
        text = str(text if text is not None else "")
        if self.inner is not None:
            text = self.inner(text, limit + 200)
        else:
            for value in self.values:
                text = text.replace(value, "[redacted]")
        return printable(text, limit)

    def deep(self, value, depth=0):
        """VALUE (the gathered report) with every string scrubbed; keys stay (they are the report's own)."""
        if isinstance(value, str):
            return self(value, 2000)
        if isinstance(value, dict):
            return {key: self.deep(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.deep(item, depth + 1) for item in value]
        return value


# -- the logs, read bounded --

class Sources:
    """The log files a report reads, each once, from its end and bounded (logs.iter_records: the newest TAIL bytes
    of a file, TOTAL bytes of all); what was read and left out of each, for the appendix."""

    def __init__(self, tail=None, total=None):
        logs = module("logs")
        self.tail = TAIL_BYTES if tail is None else tail
        self.budget = logs.new_budget(TOTAL_BYTES if total is None else total)
        self.used, self.unreadable, self.seen = [], [], set()

    def records(self, path, kind, tail=None):
        """The JSON objects of the newest part of PATH; nothing (and a note) for a file that cannot be read."""
        path = Path(path)
        key = str(path)
        if key in self.seen:
            return
        self.seen.add(key)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.unreadable.append({"path": key, "kind": kind, "why": exc.strerror or str(exc)})
            return
        if not stat.S_ISREG(info.st_mode):
            self.unreadable.append({"path": key, "kind": kind, "why": "not a regular file (a link or a device)"})
            return
        if not os.access(path, os.R_OK):
            self.unreadable.append({"path": key, "kind": kind, "why": "no permission to read it"})
            return
        before = {name: self.budget[name] for name in ("read_bytes", "skipped_bytes", "lines", "long")}
        entry = {"path": key, "kind": kind, "size": info.st_size, "read": 0, "skipped": 0, "records": 0}
        self.used.append(entry)
        tail = min(self.tail, tail) if tail is not None else self.tail
        try:
            for record in module("logs").iter_records(path, self.budget, tail):
                entry["records"] += 1
                yield record
        finally:
            entry["read"] = self.budget["read_bytes"] - before["read_bytes"]
            entry["skipped"] = self.budget["skipped_bytes"] - before["skipped_bytes"]
            lines = self.budget["lines"] - before["lines"]
            if entry["skipped"]:
                entry["older_lines"] = max(1, entry["skipped"] * max(lines, 1) // max(1, entry["read"]))
            if self.budget["long"] - before["long"]:
                entry["long_lines"] = self.budget["long"] - before["long"]


# -- what the report covers --

def new_scope(what, title):
    return {"what": what, "title": title, "runs": {}, "dirs": {}, "templates": {}, "mcp_state": [], "notes": [],
            "live": {}}


def new_run(run, **fields):
    entry = {"run": run, "name": run, "role": "run", "part_of": None, "agent": None, "backend": None,
             "state": "ended", "started": None, "ended": None, "duration_s": None, "status": None, "workspaces": [],
             "log_dir": None, "mode": None, "ask": None, "vpn": None, "llm": None, "mcp_servers": [], "cut": None,
             "cut_since": None, "taint": None, "options": [], "settings": {}, "session": None, "events": {},
             "workspace_changes": []}
    entry.update({key: value for key, value in fields.items() if value is not None})
    return entry


def add_dir(scope, directory, runs=None, sessions=()):
    """Read DIRECTORY for RUNS (None: every run in it) and the runs of SESSIONS."""
    if not directory:
        return
    directory = Path(directory).expanduser()
    entry = scope["dirs"].setdefault(str(directory), {"runs": set() if runs is not None else None, "sessions": set()})
    if runs is None:
        entry["runs"] = None
    elif entry["runs"] is not None:
        entry["runs"].update(r for r in runs if r)
    entry["sessions"].update(sessions)


def running_records():
    try:
        return module("net").running()
    except Exception:  # noqa: BLE001 - no registry: nothing runs
        return {}


def start_entries():
    try:
        return module("starts").entries(True, prune=False)
    except Exception:  # noqa: BLE001 - the registry unreadable: runs are still found by their logs
        return []


def logs_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def launcher_argv(record):
    """The options the running launcher of RECORD (run.json) was started with: its command line in /proc, after
    agent_sandbox.py ([] where it cannot be read)."""
    try:
        procinfo = module("procinfo")
        words = procinfo.recorded_cmdline(record.get("pid"), record.get("pid_start"), Path("/proc"))
    except Exception:  # noqa: BLE001 - not readable here (another PID namespace, macOS)
        return []
    words = [os.fsdecode(w) if isinstance(w, bytes) else str(w) for w in words or []]
    for index, word in enumerate(words):
        if os.path.basename(word) == "agent_sandbox.py":
            return words[index + 1:]
    return []


def settings_of(argv, agent=None, session_data=None):
    """{option: value} that say why a destination was allowed and how the run was set up: presets, hosts, the
    security mode, VPN, limits..., from a start's options (and its project file, for a kept start)."""
    found = {}
    namespace = None
    with contextlib.suppress(Exception):
        if session_data is not None:
            namespace, _ = module("sessions").effective(session_data)
        if namespace is None and argv:
            launcher = module("logs").launcher()
            words = list(argv)
            if words and words[0] not in ("claude", "codex", "opencode") and agent:
                words = [agent, *words]
            namespace, _ = launcher.build_parser().parse_known_args(words)
    if namespace is None:
        return found
    for key in ("presets", "allow_host", "frontier", "llm", "no_inspect", "allow_tcp", "allow", "dns",
                "egress_all_allow", "mode", "preset", "vpn", "vpn_check", "ask", "telemetry", "memory", "cpus",
                "pids", "device", "gpu", "offline", "canaries", "no_canaries", "proxy", "no_proxy", "token_proxy",
                "allow_host_port", "backend"):
        value = getattr(namespace, key, None)
        if value not in (None, [], False, ""):
            found[key] = value if isinstance(value, (bool, int, float)) else \
                [str(v) for v in value] if isinstance(value, (list, tuple)) else str(value)
    return found


def shown_options(tokens):
    """A start's options for the report: the value of an option about secrets (--env, --api-key, --git-token-*)
    without what follows its first '='."""
    shown, secret_next = [], False
    for token in tokens or []:
        token = str(token)
        if secret_next and not token.startswith("-"):
            name = token.split("=", 1)[0]
            shown.append(f"{name}=[redacted]" if "=" in token else token)
            secret_next = False
            continue
        secret_next = False
        if token.startswith("-") and "=" in token and SECRET_OPTION.search(token.split("=", 1)[0]):
            option, value = token.split("=", 1)
            shown.append(f"{option}={value.split('=', 1)[0]}=[redacted]" if "=" in value else f"{option}=[redacted]")
            continue
        if token.startswith("-") and SECRET_OPTION.search(token):
            secret_next = True
        shown.append(token)
    return shown[:200]


def run_from_record(run, record, role="run", part_of=None):
    """A run of the report from a running sandbox's run.json."""
    mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
    directory = record.get("directory")
    paused = bool(directory) and (Path(str(directory)) / "paused").exists()
    entry = new_run(run, name=label_of(run, record), role=role, part_of=part_of, agent=record.get("agent"),
                    backend=record.get("backend"), state="paused" if paused else "running",
                    started=record.get("started"), log_dir=record.get("log_dir"), mode=mode.get("name"),
                    ask=record.get("ask"), llm=record.get("llm"),
                    workspaces=[{"host": record.get("workspace")}] if record.get("workspace") else None,
                    mcp_servers=[str(s) for s in record.get("mcp_servers") or [] if isinstance(s, str)] or None,
                    session=record.get("session") if isinstance(record.get("session"), str) else None)
    if record.get("vpn_profile"):
        entry["vpn"] = {"profile": record.get("vpn_profile"), "type": record.get("vpn_type")}
    if record.get("cut"):
        entry["cut"] = record["cut"]
    with contextlib.suppress(OSError, ValueError, AttributeError):
        data = json.loads((Path(str(directory)) / "cut.json").read_text())
        entry["cut_since"] = data.get("since") if entry["cut"] else None
    argv = launcher_argv(record)
    entry["options"] = shown_options(argv[1:] if argv and argv[0] in ("claude", "codex", "opencode") else argv)
    entry["settings"] = settings_of(argv, record.get("agent"))
    entry["run_dir"] = directory
    return entry


def label_of(run, record):
    try:
        return module("net").label(run, record)
    except Exception:  # noqa: BLE001
        return record.get("name") or run


def role_of(entry):
    if entry.get("kind") == "mcp":
        return "MCP server"
    if entry.get("helper_of"):
        return "helper"
    if entry.get("lead"):
        return "team member"
    return "start"


def record_role(record):
    if record.get("kind") == "mcp" or str(record.get("run", "")).startswith("mcp-"):
        return "MCP server"
    if record.get("helper_of"):
        return "helper"
    return "team member"


def add_running(scope, run, record, role="run", part_of=None):
    entry = run_from_record(run, record, role, part_of)
    scope["runs"][run] = entry
    add_dir(scope, record.get("log_dir"), {run})
    if entry["role"] == "MCP server":
        name = record.get("mcp_name") or (run[len("mcp-"):] if run.startswith("mcp-") else None)
        if name:
            scope["mcp_state"].append(name)
    return entry


def add_start(scope, entry, running, role="start", part_of=None, parent_dirs=()):
    """The runs of the start ENTRY (starts.entries): its running one, the ones its history names, and those its log
    directory's timeline names with its id."""
    name, key = entry.get("name"), entry.get("id")
    record = running.get(entry.get("run")) if isinstance(entry.get("run"), str) else None
    if entry.get("kind") == "mcp":
        server = name[len("mcp-"):] if isinstance(name, str) and name.startswith("mcp-") else name
        scope["mcp_state"].append(server)
        run = entry.get("run") or f"mcp-{server}"
        if record is not None:
            add_running(scope, run, record, "MCP server", part_of)
        else:
            scope["runs"].setdefault(run, new_run(run, name=name, role="MCP server", part_of=part_of,
                                                  backend=entry.get("backend"), state=entry.get("state") or "stopped",
                                                  started=entry.get("started")))
        for directory in parent_dirs:  # its own sandbox's connection log, where it logged beside its start
            add_dir(scope, directory, {run})
        return [run]
    data = None
    with contextlib.suppress(Exception):
        data = module("sessions").load(key)
    template = {"name": name, "role": role, "part_of": part_of, "agent": entry.get("agent"),
                "backend": entry.get("backend"), "session": key,
                "workspaces": [{"host": entry.get("workspace")}] if entry.get("workspace") else None,
                "options": shown_options((data or {}).get("options") or []),
                "settings": settings_of((data or {}).get("options") or [], entry.get("agent"), data),
                "llm": entry.get("llm"), "mcp_servers": entry.get("mcp") or None}
    scope["templates"][key] = template
    runs = set()
    if isinstance(entry.get("run"), str):
        runs.add(entry["run"])
    if data and isinstance(data.get("run"), str):
        runs.add(data["run"])
    with contextlib.suppress(Exception):
        for event in module("starts").history(name):
            if event.get("id") == key and isinstance(event.get("run"), str):
                runs.add(event["run"])
    log_dir = record.get("log_dir") if record else None
    if not log_dir:
        try:
            log_dir = str(module("activity").session_log_dir(key))
        except Exception as exc:  # noqa: BLE001 - said in the appendix
            scope["notes"].append(f"the log directory of {name} could not be found: {exc}")
    for run in runs:
        if record is not None and run == entry.get("run"):
            run_entry = add_running(scope, run, record, role, part_of)
            run_entry.update({k: v for k, v in template.items() if v and k in ("name", "options", "settings")
                              and not run_entry.get(k)})
            run_entry["session"] = key
        else:
            scope["runs"].setdefault(run, new_run(run, **template, state="ended"))
    add_dir(scope, log_dir, runs, {key})
    return [log_dir] if log_dir else []


def find_entry(entries, name):
    for key in ("name", "id", "run"):
        matches = [entry for entry in entries if entry.get(key) == name]
        if len(matches) == 1:
            return matches[0]
    return None


def merge(scopes):
    """One scope of several (a report of several starts): their runs, directories and notes together."""
    if len(scopes) == 1:
        return scopes[0]
    scope = new_scope("starts", ", ".join(s["title"] for s in scopes))
    for other in scopes:
        for run, entry in other["runs"].items():
            scope["runs"].setdefault(run, entry)
        for directory, filt in other["dirs"].items():
            add_dir(scope, directory, filt["runs"], filt["sessions"])
        scope["templates"].update(other["templates"])
        scope["mcp_state"] += other["mcp_state"]
        scope["notes"] += other["notes"]
    return scope


def resolve(name=None, session=None, every=False, project=None, log_dir=None, parts=True):
    """What the report covers (a scope): its runs, the log directories to read and for which runs. NAME: one name
    or a list of them."""
    if isinstance(name, (list, tuple)):
        if len(name) > 1:
            return merge([resolve(one, None, False, None, None, parts) for one in dict.fromkeys(name)])
        name = name[0] if name else None
    running = running_records()
    if every:
        scope = new_scope("all", "every run")
        try:
            directories = sorted(p for p in logs_root().iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            directories = []
        for directory in directories:
            add_dir(scope, directory)
        for run, record in running.items():
            if isinstance(run, str) and isinstance(record, dict):
                add_running(scope, run, record, record_role(record) if record.get("kind") == "mcp" else "run")
                add_dir(scope, record.get("log_dir"))
        return scope
    if log_dir or project:
        directory = Path(log_dir).expanduser() if log_dir else \
            module("activity").default_log_dir(Path(project).expanduser().resolve())
        scope = new_scope("directory", f"the runs of {directory}")
        add_dir(scope, directory)
        for run, record in running.items():
            if isinstance(record, dict) and record.get("log_dir") and Path(record["log_dir"]) == directory:
                add_running(scope, run, record)
        return scope
    wanted = session or name
    if not wanted:
        raise ValueError("name a run or a start (sandbox-ai list shows them), or use --all, --project or --log-dir")
    entries = start_entries()
    entry = find_entry(entries, wanted)
    if entry is None and session:
        with contextlib.suppress(Exception):
            entry = find_entry(entries, module("sessions").resolve(session))
    if entry is not None:
        scope = new_scope("start", f"{entry.get('name')}")
        dirs = add_start(scope, entry, running)
        if parts:
            for part in entries:
                if part is not entry and part.get("listed_under") == entry.get("name"):
                    dirs += add_start(scope, part, running, role_of(part), entry.get("name"), dirs)
        return scope
    if session:
        raise ValueError(f"no session named {printable(session, 80)} (see sandbox-ai list)")
    record = running.get(wanted)
    if record is None:
        with contextlib.suppress(Exception):
            record = module("net").choose(running, wanted)
    if isinstance(record, dict):
        run = record.get("run") or wanted
        scope = new_scope("run", label_of(run, record))
        add_running(scope, run, record, "run")
        if parts:
            with contextlib.suppress(Exception):
                for other, entry_ in module("net").related_runs(running, record):
                    add_running(scope, other, entry_, record_role(entry_), scope["runs"][run]["name"])
        return scope
    directory = None
    with contextlib.suppress(Exception):
        directory = module("activity").find_run(wanted)
    if directory is None:
        raise ValueError(f"no run or start named {printable(wanted, 80)}: sandbox-ai list shows the starts, "
                         "sandbox-ai activity --runs the runs of a log directory (a run with --log-dir elsewhere: "
                         "add --log-dir)")
    scope = new_scope("run", wanted)
    scope["runs"][wanted] = new_run(wanted)
    add_dir(scope, directory, {wanted})
    return scope


# -- gathering --

def new_destination(key):
    return {"target": key, "addresses": set(), "connections": set(), "requests": 0, "bytes_out": 0, "bytes_in": 0,
            "allowed": 0, "refused": 0, "failed": 0, "why_allowed": collections.Counter(),
            "why_refused": collections.Counter(), "processes": collections.Counter(), "runs": set(),
            "first": None, "last": None, "how": collections.Counter()}


def process_label(process):
    """"name (pid N)" of a record's process, or None."""
    if isinstance(process, str):
        return process[:120] or None
    if not isinstance(process, dict) or not process:
        return None
    if "unknown" in process and not process.get("comm"):
        return "not identified"
    name = process.get("comm") or process.get("name") or process.get("exe") or "?"
    number = process.get("ns_pid") or process.get("pid")
    return f"{name} (pid {number})" if number else str(name)


def host_of(record):
    host = record.get("host")
    if isinstance(host, str) and host:
        return host.lower()
    url = record.get("url")
    match = re.match(r"[a-z][a-z0-9+.-]*://([^/?#]+)", url) if isinstance(url, str) else None
    if match:
        return match.group(1).lower()
    return None


class Gatherer:
    """Reads the logs of a scope and keeps what the report shows: per run, per destination, the statistics and the
    findings."""

    def __init__(self, scope, since=None, until=None, sources=None, live=True):
        self.scope, self.since, self.until = scope, since, until
        self.sources = sources or Sources()
        self.live = live
        self.destinations, self.lookups = {}, {}
        self.approvals = {"asked": 0, "allowed": 0, "denied": 0, "unanswered": 0, "automatic": 0, "items": []}
        self.mcp, self.mcp_seen = {}, set()
        self.tools = {"checked": 0, "refused": 0, "tools": collections.Counter()}
        self.pushes = {"checked": 0, "refused": 0, "items": []}
        self.models, self.tokens = collections.Counter(), collections.Counter()
        self.credentials, self.added = collections.Counter(), collections.Counter()
        self.files, self.programs = collections.Counter(), collections.Counter()
        self.processes = 0
        self.agent = collections.Counter()
        self.devices = {"added": 0, "refused": 0, "items": []}
        self.vpn_checks = []
        self.recorded, self.derived = collections.defaultdict(list), collections.defaultdict(dict)
        self.derived_left, self.reasons = 0, {}
        self.extra = []  # findings no safeguard record holds: answers of no, VPN checks, cuts, canary events
        self.canary_runs = set()
        self.notes = list(scope.get("notes") or [])
        self.attributed_by_time = False
        self.redact = Scrubber(self.canary_tokens())
        try:
            self.safeguards = module("safeguards")
            self.sg_redact = self.safeguards.Redactor(self.redact.values)
        except Exception:  # noqa: BLE001 - without it the recorded safeguards are still read
            self.safeguards = None

    # -- helpers --

    def canary_tokens(self):
        """The canary tokens of the runs in scope (their state files): never shown, wherever a log has one."""
        tokens = []
        with contextlib.suppress(Exception):
            canary = module("canary")
            for run in list(self.scope["runs"]):
                with contextlib.suppress(ValueError):
                    record = canary.read_json(canary.run_state(run)) or {}
                    for entry in record.get("canaries") or []:
                        tokens += [t for t in (entry.get("tokens") or {}).values() if isinstance(t, str)]
        return tokens

    def in_period(self, record):
        if self.since is None and self.until is None:
            return True
        seconds = epoch(record.get("time"))
        if seconds is None:
            return False
        return (self.since is None or seconds >= self.since) and (self.until is None or seconds <= self.until)

    def window(self, run):
        entry = self.scope["runs"].get(run) or {}
        start = epoch(entry.get("started"))
        end = epoch(entry.get("ended"))
        if entry.get("state") in ("running", "paused"):
            end = None
        return (start if start is not None else float("-inf")), (end + 5 if end is not None else float("inf"))

    def owner(self, record, filt):
        """The run of RECORD among the runs FILT reads ("" for a record of no run named, where every run counts);
        None when it belongs to none of them."""
        run = record.get("run") if isinstance(record.get("run"), str) else None
        if run is None and isinstance(record.get("sandbox"), dict) and isinstance(record["sandbox"].get("run"), str):
            run = record["sandbox"]["run"]
        if run is not None:
            return run if filt["runs"] is None or run in filt["runs"] else None
        if filt["runs"] is None:
            return ""
        seconds = epoch(record.get("time"))
        for candidate in sorted(filt["runs"]):
            start, end = self.window(candidate)
            if seconds is None or start <= seconds <= end:
                self.attributed_by_time = True
                return candidate
        return None

    def run_entry(self, run, session=None):
        """The report's entry of RUN, made the first time it is seen (from its start's template)."""
        if run not in self.scope["runs"]:
            template = self.scope["templates"].get(session) or {}
            self.scope["runs"][run] = new_run(run, **template)
        return self.scope["runs"][run]

    def finding(self, kind, what, why, run, when, by=None, verdict=None, fix=None, severity=None, source=None,
                count=1):
        self.extra.append({"kind": kind, "what": what, "why": why, "run": run, "time": when, "by": by,
                           "verdict": verdict, "fix": fix, "severity": severity, "source": source, "count": count})

    def derive(self, convert, record, run, *extra):
        """Keep RECORD for a finding made by safeguards.py's CONVERT, for a run without safeguards.jsonl records (read
        first in its directory): equal records (but for their time, connection and the request's path) are made into
        one finding with their count, so a run of thousands of refusals is converted once per kind of refusal."""
        if self.safeguards is None or convert is None or self.recorded.get(run):
            return
        process = record.get("process")
        url = record.get("url")
        key = {k: v for k, v in record.items() if k not in VOLATILE}
        key["url"] = URL_PATH.sub(r"\1", url) if isinstance(url, str) else url
        key["process"] = process.get("comm") if isinstance(process, dict) else process
        key = (convert.__name__, extra, json.dumps(key, sort_keys=True, default=str)[:4000])
        groups = self.derived[run]
        group = groups.get(key)
        if group is None:
            if len(groups) >= DERIVED_LIMIT:
                self.derived_left += 1
                return
            group = groups[key] = {"convert": convert, "extra": extra, "record": record, "count": 0,
                                   "first": None, "last": None}
        group["count"] += 1
        when = record.get("time")
        if isinstance(when, str) and when:
            group["first"] = when if group["first"] is None or when < group["first"] else group["first"]
            group["last"] = when if group["last"] is None or when > group["last"] else group["last"]

    def converted(self, run):
        """The findings of the records derive() kept for RUN, as safeguards.jsonl records."""
        found = []
        for group in self.derived.get(run, {}).values():
            with contextlib.suppress(Exception):
                entry = group["convert"](group["record"], run or None, self.sg_redact, *group["extra"])
                if entry:
                    entry.update(count=group["count"], time=group["first"] or entry.get("time"), last=group["last"])
                    found.append(entry)
        return found

    # -- the logs --

    def read(self):
        if self.live:  # first: what was allowed during a run says why its connections were let through
            for run, entry in list(self.scope["runs"].items()):
                if entry.get("state") in ("running", "paused") and entry.get("run_dir"):
                    self.live_state(run, entry)
        for directory, filt in list(self.scope["dirs"].items()):
            self.read_dir(Path(directory), filt)
        for server in dict.fromkeys(self.scope["mcp_state"]):
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(server)):
                path = state_root() / "mcp" / server / "mcp-calls.jsonl"
                for record in self.sources.records(path, "MCP tool calls (the server's)"):
                    if self.in_period(record):
                        self.mcp_call({"server": server, **record}, f"mcp-{server}", derive=False)
        return self

    def read_dir(self, directory, filt):
        if not directory.is_dir():
            self.sources.unreadable.append({"path": str(directory), "kind": "log directory",
                                            "why": "no such directory (the run kept no logs, or they were removed)"})
            return
        # The timeline first: it names the runs of a session, whose connection logs are read below.
        for record in self.sources.records(directory / "activity.jsonl", "activity (timeline)"):
            self.activity(record, filt)
        readers = (("safeguards.jsonl", "safeguards", self.safeguard),
                   ("canaries.jsonl", "canary events", self.canary),
                   ("vpn.jsonl", "VPN and Tor checks", self.vpn),
                   ("egress-denied.jsonl", "firewall drops and refused lookups", self.egress),
                   ("tools.jsonl", "the agent's tool calls (hooks)", self.tool),
                   ("mcp-calls.jsonl", "MCP tool calls", self.mcp_call),
                   ("devices.jsonl", "devices", self.device),
                   ("workspaces.jsonl", "workspaces added and removed", self.workspace))
        for name, kind, handle in readers:
            for record in self.sources.records(directory / name, kind):
                if not self.in_period(record):
                    continue
                run = self.owner(record, filt)
                if run is not None:
                    handle(record, run)
        logs = []
        with contextlib.suppress(OSError):
            for path in directory.glob("connections-*.jsonl"):
                match = CONNECTION_LOG.fullmatch(path.name)
                if match and (filt["runs"] is None or match.group(2) in filt["runs"]):
                    logs.append((match.group(1), match.group(2), path))
        for _, run, path in sorted(logs, reverse=True):  # the newest first: the budget goes to them
            self.run_entry(run)
            for record in self.sources.records(path, "connection proxy"):
                if self.in_period(record):
                    self.connection(record, run)
        for record in self.sources.records(directory / "dns.jsonl", "name lookups forwarded"):
            if self.in_period(record):
                run = self.owner(record, filt)
                if run is not None:
                    self.dns(record, run)
        for record in self.sources.records(directory / "files.jsonl", "file changes", SMALL_TAIL):
            if self.in_period(record) and self.owner(record, filt) is not None and record.get("action"):
                self.files[str(record["action"])] += 1
        for record in self.sources.records(directory / "processes.jsonl", "processes started", SMALL_TAIL):
            if self.in_period(record) and self.owner(record, filt) is not None and record.get("command"):
                self.processes += 1
                self.programs[(str(record.get("command")).split() or ["?"])[0].rsplit("/", 1)[-1][:60]] += 1

    def activity(self, record, filt):
        run, session = record.get("run"), record.get("session")
        if not isinstance(run, str):
            return
        wanted = filt["runs"] is None or run in filt["runs"] or (session and session in filt["sessions"])
        if not wanted:
            return
        if filt["runs"] is not None:
            filt["runs"].add(run)
        entry = self.run_entry(run, session if session in filt["sessions"] else None)
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        kind = record.get("kind")
        if kind == "session":
            event = details.get("event")
            if event == "start":
                entry["started"] = entry["started"] or record.get("time")
                if epoch(record.get("time")) is not None and epoch(entry["started"]) is not None:
                    entry["started"] = min((entry["started"], record.get("time")), key=epoch)
                entry["agent"] = entry["agent"] or details.get("agent") or record.get("agent")
                entry["backend"] = entry["backend"] or details.get("backend")
                if isinstance(details.get("workspaces"), list) and details["workspaces"]:
                    entry["workspaces"] = [w for w in details["workspaces"] if isinstance(w, dict)][:20]
                entry["log_dir"] = entry["log_dir"] or details.get("log_dir")
                entry["session"] = entry["session"] or session
            elif event == "stop":
                entry["ended"] = record.get("time")
                if isinstance(details.get("status"), int):
                    entry["status"] = details["status"]
                if isinstance(details.get("duration_s"), (int, float)):
                    entry["duration_s"] = details["duration_s"]
            elif isinstance(event, str):
                entry["events"][event] = entry["events"].get(event, 0) + 1
        if not self.in_period(record):
            return
        if kind == "approval":
            self.approval(record, details, run)
        elif kind in ("prompt", "reply", "tool", "command", "mcp"):
            self.agent[kind] += 1
        elif kind == "file":
            count = details.get("count") if isinstance(details.get("count"), int) else 1
            self.agent["file changes"] += count

    def approval(self, record, details, run):
        answer = str(details.get("answer") or "?")
        automatic = bool(details.get("automatic"))
        self.approvals["asked"] += 1
        if answer in ("allow", "allowed"):
            self.approvals["allowed"] += 1
        elif answer in ("deny", "denied"):
            self.approvals["denied"] += 1
        elif answer == "unanswered":
            self.approvals["unanswered"] += 1
        if automatic:
            self.approvals["automatic"] += 1
        question = details.get("question") or record.get("summary") or details.get("kind") or "a question"
        if len(self.approvals["items"]) < APPROVALS_LIMIT:
            self.approvals["items"].append({"time": record.get("time"), "run": run, "question": question,
                                            "answer": answer, "scope": details.get("scope"),
                                            "automatic": automatic, "reason": details.get("reason")})
        if answer in ("deny", "denied") and not automatic:
            self.finding("approval-denied", f"asked: {question}", details.get("reason") or "you answered no",
                         run, record.get("time"), verdict=f"refused ({details.get('scope') or 'once'})",
                         fix="nothing to do: you refused it", source="approvals")

    def safeguard(self, record, run):
        if self.safeguards is not None and record.get("kind") not in self.safeguards.KINDS:
            return
        self.recorded[run].append(record)

    def canary(self, record, run):
        event = str(record.get("event") or "?")
        if record.get("canary"):
            self.canary_runs.add(run)
        severity = "critical" if event in ("send", "context") else "info" if event == "error" else "high"
        # What a token went to without its path and query: they may hold the token, and once the run's canary state
        # is gone (after 90 days) the report no longer knows it to redact it.
        what = URL_QUERY.sub(r"\1…", str(record.get("summary") or f"canary {record.get('canary') or ''} {event}"))
        if event == "error":
            what = f"canary watch: {record.get('error') or 'an error'}"
        where = URL_QUERY.sub(r"\1…", str(record.get("url"))) if record.get("url") else record.get("host")
        why = ("a decoy planted for the run was touched: nothing legitimate uses it" if event != "error" else
               "the canaries could not be watched for a while")
        if where:
            why += f" (to {where})"
        self.finding("canary", what, why, run, record.get("time"), by=process_label(record.get("process")),
                      verdict={"log": "logged", "report": "reported", "ask": "asked you", "pause": "the sandbox was "
                               "paused", "stop": "the sandbox was stopped", "block": "the request was refused"}.get(
                                   str(record.get("action")), str(record.get("action") or "recorded")),
                      fix=f"see sandbox-ai canaries log {run}" if run else "see sandbox-ai canaries log",
                      severity=severity, source="canaries")

    def vpn(self, record, run):
        entry = self.run_entry(run) if run else None
        profile = {"profile": record.get("profile"), "type": record.get("type"), "provider": record.get("provider")}
        if entry is not None and not entry.get("vpn"):
            entry["vpn"] = {k: v for k, v in profile.items() if v}
        failures = [str(f) for f in record.get("failures") or []]
        leaks = {str(f) for f in record.get("leaks") or []}
        warnings = [str(w) for w in record.get("warnings") or []]
        self.vpn_checks.append({"time": record.get("time"), "run": run, "profile": record.get("profile"),
                                "type": record.get("type"), "during_run": bool(record.get("recheck")),
                                "ok": not failures, "failures": len(failures), "warnings": len(warnings)})
        name = "Tor" if record.get("type") == "tor" else f"VPN {record.get('profile') or ''}".strip()
        when_text = "during the run" if record.get("recheck") else "at the start"
        for failure in failures:
            leak = failure in leaks
            self.finding("vpn", f"{name}: check {when_text} failed: {failure}",
                         "traffic left past the tunnel" if leak else "the tunnel did not prove what it must",
                         run, record.get("time"), verdict="the run was stopped (unless --vpn-check warn)" if leak
                         else "the start was refused (unless --vpn-check warn)", severity="critical" if leak else "high",
                         fix="sandbox-ai vpn check " + str(record.get("profile") or "PROFILE"), source="vpn")
        for warning in warnings:
            self.finding("vpn", f"{name}: check {when_text}: {warning}", "a check that could not decide",
                         run, record.get("time"), verdict="warned", severity="low", source="vpn")

    def egress(self, record, run):
        self.derive(getattr(self.safeguards, "from_egress", None), record, run)
        who = process_label(record.get("process"))
        if isinstance(record.get("host"), str):
            name = record["host"].lower()[:253]
            entry = self.lookups.setdefault(name, {"name": name, "forwarded": 0, "refused": 0, "failed": 0,
                                                   "why": collections.Counter(), "processes": collections.Counter(),
                                                   "first": None, "last": None, "runs": set()})
            entry["refused"] += 1
            entry["why"][str(record.get("reason") or "not in allowed hosts")] += 1
            self.touch(entry, record, run, who)
            return
        if record.get("address") is None:
            return
        port = record.get("port")
        key = f"{record.get('address')}:{port}" if port not in (None, "") else str(record.get("address"))
        destination = self.destinations.setdefault(key, new_destination(key))
        destination["refused"] += 1
        destination["how"][str(record.get("protocol") or "?")] += 1
        destination["why_refused"]["dropped by the firewall: not allowed by the run's network options"] += 1
        self.touch(destination, record, run, who)

    def touch(self, entry, record, run, who):
        when = record.get("time")
        if isinstance(when, str) and when:
            # The proxy, the firewall's and the DNS stub's logs all write YYYY-MM-DDTHH:MM:SSZ: compared as text.
            if entry["first"] is None or when < entry["first"]:
                entry["first"] = when
            if entry["last"] is None or when > entry["last"]:
                entry["last"] = when
        if run:
            entry["runs"].add(run)
        if who:
            entry["processes"][who[:80]] += 1

    def dns(self, record, run):
        name = str(record.get("host") or "?").lower()[:253]
        entry = self.lookups.setdefault(name, {"name": name, "forwarded": 0, "refused": 0, "failed": 0,
                                               "why": collections.Counter(), "processes": collections.Counter(),
                                               "first": None, "last": None, "runs": set()})
        if record.get("error"):
            entry["failed"] += 1
            entry["why"][str(record["error"])] += 1
        else:
            entry["forwarded"] += 1
            entry["why"]["forwarded: DNS was allowed (--dns, sandbox-ai net allow --dns)"] += 1
        self.touch(entry, record, run, process_label(record.get("process")))

    def tool(self, record, run):
        self.derive(getattr(self.safeguards, "from_tool_call", None), record, run, "tool")
        self.tools["checked"] += 1
        self.tools["tools"][str(record.get("tool") or "?")[:60]] += 1
        if record.get("decision") not in (None, "allow") or record.get("reason"):
            self.tools["refused"] += 1

    def mcp_call(self, record, run, derive=True):
        if record.get("event", "call") not in ("call", "refused"):
            return
        server = str(record.get("server") or "?")[:64]
        key = (server, str(record.get("time")), str(record.get("tool")), str(record.get("arguments"))[:200],
               record.get("event", "call"))
        if key in self.mcp_seen:
            return  # the relay and the server log the same call
        self.mcp_seen.add(key)
        if derive:
            self.derive(getattr(self.safeguards, "from_tool_call", None), record, run, "mcp")
        entry = self.mcp.setdefault(server, {"calls": 0, "refused": 0, "asked": 0, "tools": collections.Counter()})
        if record.get("event") == "refused":
            entry["refused"] += 1
            return
        entry["calls"] += 1
        entry["tools"][str(record.get("tool") or "?")[:80]] += 1
        approval = record.get("approval") if isinstance(record.get("approval"), dict) else None
        if approval:
            entry["asked"] += 1
        if record.get("reason") or record.get("decision") == "deny" or \
                (approval and approval.get("decision") != "allow"):
            entry["refused"] += 1

    def device(self, record, run):
        self.derive(getattr(self.safeguards, "from_device", None), record, run or record.get("run"))
        event = record.get("event")
        if event == "add":
            self.devices["added"] += 1
        elif event == "refuse":
            self.devices["refused"] += 1
        if event in ("add", "refuse", "remove") and len(self.devices["items"]) < 100:
            self.devices["items"].append({"time": record.get("time"), "run": run, "event": event,
                                          "device": record.get("value"), "reason": record.get("reason")})

    def workspace(self, record, run):
        if run:
            entry = self.run_entry(run)
            if len(entry["workspace_changes"]) < 50:
                entry["workspace_changes"].append({"time": record.get("time"), "event": record.get("event"),
                                                   "host": record.get("host"), "sandbox": record.get("sandbox"),
                                                   "mode": record.get("mode")})

    def connection(self, record, run):
        event = record.get("event")
        if not isinstance(event, str):
            return
        entry = self.run_entry(run)
        if event in ("taint", "untaint"):
            self.derive(getattr(self.safeguards, "from_connection", None), record, run)
            if event == "untaint":
                self.finding("untaint", f"taint taken back by {record.get('by') or 'the human'}",
                             "you said the content that entered can be trusted", run, record.get("time"),
                             verdict="the rules for tainted runs no longer apply", source="proxy")
            else:
                state = record.get("taint") if isinstance(record.get("taint"), dict) else {}
                source = state.get("source") if isinstance(state.get("source"), dict) else {}
                entry["taint"] = f"tainted since {state.get('since') or record.get('time')} by {source.get('what') or '?'}"
            return
        target = host_of(record) or str(record.get("destination") or "?")
        destination = self.destinations.setdefault(target, new_destination(target))
        if record.get("destination"):
            destination["addresses"].add(str(record["destination"])[:60])
        if isinstance(record.get("connection"), int):
            destination["connections"].add((run, record["connection"]))
        who = process_label(record.get("process"))
        self.touch(destination, record, run, who)
        for field in ("bytes_out", "bytes_in"):
            if isinstance(record.get(field), int):
                destination[field] += record[field]
        if event == "http":
            destination["requests"] += 1
        destination["how"][{"http": str(record.get("method") or "HTTP")[:10]}.get(event, event)] += 1
        model = record.get("model")
        if isinstance(model, dict) and isinstance(model.get("usage"), dict):
            self.models[str(model.get("model") or "?")[:80]] += 1
            for key, value in model["usage"].items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self.tokens[str(key)[:40]] += value
        for name in record.get("credentials") or []:
            self.credentials[f"{name} to {target}"[:120]] += 1
        if record.get("token_added"):
            self.added[f"a Git login to {target}"[:120]] += 1
        if record.get("llm"):
            self.added[f"the --llm key to {target}"[:120]] += 1
        for name in record.get("api_keys") or []:
            self.added[f"API key {name} to {target}"[:120]] += 1
        push = record.get("push")
        if isinstance(push, dict):
            self.pushes["checked" if push.get("ok") else "refused"] += 1
            if len(self.pushes["items"]) < 200:
                self.pushes["items"].append({"time": record.get("time"), "run": run, "url": record.get("url"),
                                             "refs": [str(r) for r in push.get("refs") or [] if isinstance(r, str)][:5],
                                             "ok": bool(push.get("ok")), "message": push.get("message"),
                                             "findings": len(push.get("findings") or [])})
        if event in ALLOWED_EVENTS and not (isinstance(push, dict) and not push.get("ok")):
            destination["allowed"] += 1
            approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
            key = (run, target, event, approval.get("decision"), approval.get("scope"), str(approval.get("reason"))[:40],
                   record.get("decision"), str(record.get("rule")), str(record.get("ruleset")))
            reason = self.reasons.get(key)
            if reason is None:
                reason = self.reasons[key] = self.allowed_reason(record, entry, target)
                if len(self.reasons) > 50000:
                    self.reasons.clear()
            destination["why_allowed"][reason] += 1
        elif event in ALLOWED_EVENTS:  # a push the privacy check refused: the request went nowhere
            destination["refused"] += 1
            destination["why_refused"][f"push refused: {push.get('message') or 'the privacy check'}"[:200]] += 1
        elif event in REFUSED_EVENTS and not record.get("abandoned"):
            destination["refused"] += 1
            reason = record.get("reason") or record.get("error") or ("TLS refused" if event == "tls-refused" else event)
            if event == "cut":
                reason = f"open connection closed: {reason}"
            destination["why_refused"][str(reason)[:200]] += 1
        elif event in FAILED_EVENTS or record.get("abandoned"):
            destination["failed"] += 1
            destination["why_refused"][f"failed: {record.get('error') or event}"[:200]] += 1
        if event == "cut":
            self.finding("killswitch", f"open connection to {target} closed", record.get("reason") or CUT_MARK, run,
                         record.get("time"), by=who, verdict="closed", fix=f"sandbox-ai net cut {run} off lifts it",
                         source="proxy")
        elif event == "tls-upstream-failed":
            self.finding("tls", f"TLS to {target}: the server's certificate", str(record.get("error") or
                         "the server's certificate was not accepted"), run, record.get("time"), by=who,
                         verdict="refused", fix="a server with a certificate the system does not trust is not "
                         "reached; it stays refused", source="proxy")
        elif event in ("denied", "tls-refused") or isinstance(push, dict):
            self.derive(getattr(self.safeguards, "from_connection", None), record, run)

    def allowed_reason(self, record, entry, target):
        """Why the connection of RECORD (of the run ENTRY) was let through, as far as the records say."""
        approval = record.get("approval") if isinstance(record.get("approval"), dict) else None
        if approval and approval.get("decision") == "allow":
            if str(approval.get("reason") or "").startswith("allowed automatically"):
                return "allowed automatically (--ask allow)"
            return f"you approved it ({approval.get('scope') or 'once'})"
        if record.get("decision") == "allow" and record.get("rule") not in (None, "", "(host)"):
            return f"allowed by rule {record.get('rule')}" + (f" of {record['ruleset']}" if record.get("ruleset") else "")
        if record.get("event") == "tls-passthrough":
            return f"--no-inspect {target}: passed on unopened"
        settings = entry.get("settings") or {}
        host = target.split(":")[0] if target.count(":") == 1 else target
        live = (self.scope["live"].get(entry.get("run")) or {})
        if host in live.get("hosts", ()):
            return "allowed during the run (sandbox-ai net allow or an approval)"
        if host in [h.lower() for h in settings.get("allow_host") or []]:
            return f"--allow-host {host}"
        with contextlib.suppress(Exception):
            launcher = module("logs").launcher()
            for preset in settings.get("presets") or []:
                if host in launcher.PRESETS.get(preset, []):
                    return f"--allow-{preset}"
            agent = entry.get("agent")
            if agent in launcher.PROVIDERS and host in launcher.PROVIDERS[agent]:
                return f"the model API of {agent} (always reachable)"
            if settings.get("frontier") and host in launcher.FRONTIER:
                return "--frontier"
        if settings.get("llm"):
            return f"the run's network options (--llm {settings['llm']}, presets, hosts)"
        if settings.get("egress_all_allow"):
            return "--egress-all-allow"
        if settings.get("preset"):
            return f"the run's network options (preset {settings['preset']})"
        return "the run's network options"

    def live_state(self, run, entry):
        """What a running sandbox's supervisor says was allowed during the run (sandbox-ai net show), its taint and
        its kill switch."""
        directory = entry.get("run_dir")
        net = module("net")
        found = {"https": [], "tcp": [], "nets": [], "dns": [], "host_ports": [], "hosts": [], "given": {}}
        with contextlib.suppress(Exception):
            names = net.remembered({"directory": directory})
            found["hosts"] = sorted(str(h).lower() for h in names)[:500]
        with contextlib.suppress(Exception):
            found["given"] = net.given({"directory": directory})
        try:
            result = net.ask(directory, {"op": "show"}, timeout=SHOW_SECONDS)
            for key in ("https", "nets", "dns", "host_ports"):
                found[key] = [str(v) for v in result.get(key) or []][:500]
            found["tcp"] = [f"{a}:{p}" for a, p in result.get("tcp") or []][:500]
            taint = result.get("taint")
            if isinstance(taint, dict):
                entry["taint"] = net.tainted_text(taint)
            if result.get("cut") in net.CUT_MODES:
                entry["cut"] = result["cut"]
        except Exception as exc:  # noqa: BLE001 - said in the report
            self.notes.append(f"{entry.get('name') or run}: its supervisor did not say what was allowed during the run "
                              f"({printable(exc, 200)})")
        self.scope["live"][run] = found
        if entry.get("cut"):
            self.finding("killswitch", f"the kill switch holds: {entry['cut']}", f"{CUT_MARK} "
                         f"({module('net').CUT_TEXT.get(entry['cut'], '')})", run, entry.get("cut_since"),
                         verdict="in force", fix=f"sandbox-ai net cut {run} off lifts it", source="supervisor",
                         severity="info")

    # -- the findings --

    def findings(self):
        """The findings, grouped (equal ones of a run counted), ordered by severity and time."""
        raw = []
        for run in set(self.recorded) | set(self.derived):
            # A run's safeguards.jsonl is the curated record (with the fix); the logs themselves where it has none.
            records = self.recorded.get(run) or self.converted(run)
            for record in records:
                kind = str(record.get("kind") or "?")
                if kind == "canary" and run in self.canary_runs:
                    continue  # canaries.jsonl says more of it
                raw.append(self.from_safeguard(record, run))
        raw += self.extra
        groups = {}
        for item in raw:
            kind = item["kind"]
            what, why = str(item.get("what") or ""), str(item.get("why") or "")
            if "canary" in why.lower() or kind == "canary":  # a request that carried a token: never its path or query
                what = URL_QUERY.sub(r"\1…", what)
            if kind == "network" and what.startswith("TLS to"):
                kind = "tls"
            fix = item.get("fix")
            if CUT_MARK in why or CUT_MARK in what:
                kind = "killswitch"
                fix = f"sandbox-ai net cut {item.get('run') or 'RUN'} off lifts it"
            severity = item.get("severity") or KINDS.get(kind, ("low",))[0]
            # Refused requests to one host are one finding, whatever their paths (a few of them shown).
            verb, _, rest = what.partition(" ")
            origin = URL_PATH.sub(r"\1", rest)
            short = f"{verb} {origin}" if kind in NETWORK_KINDS and URL_PATH.match(rest) else what
            key = (severity, kind, item.get("run") or "", self.redact(short, 300), self.redact(why, 300))
            group = groups.get(key)
            if group is None:
                group = groups[key] = {"severity": severity, "kind": kind, "title": KINDS.get(kind, ("", kind))[1],
                                       "run": item.get("run") or "", "what": short, "why": why,
                                       "verdict": item.get("verdict"), "fix": fix,
                                       "source": item.get("source"), "count": 0, "first": None, "last": None,
                                       "by": collections.Counter(), "examples": []}
            count = int(item.get("count") or 1)
            group["count"] += count
            if short != what and what not in group["examples"] and len(group["examples"]) < 3:
                group["examples"].append(what)
            for when in (item.get("time"), item.get("last")):
                seconds = epoch(when)
                if seconds is None:
                    continue
                if group["first"] is None or seconds < epoch(group["first"]):
                    group["first"] = when
                if group["last"] is None or seconds > epoch(group["last"]):
                    group["last"] = when
            if item.get("by"):
                group["by"][str(item["by"])[:160]] += count
        ordered = sorted(groups.values(), key=lambda g: (SEVERITIES.index(g["severity"]) if g["severity"] in SEVERITIES
                                                         else 9, epoch(g["first"]) or 0, g["kind"], g["what"]))
        for index, group in enumerate(ordered, 1):
            group["id"] = f"F{index}"
            group["by"] = [name for name, _ in group["by"].most_common(5)]
        return ordered

    @staticmethod
    def from_safeguard(record, run):
        kind = str(record.get("kind") or "?")
        if kind in ("memory", "oom"):
            verdict = "the process was ended"
        elif kind == "supervisor-memory":
            verdict = "caches dropped"
        elif kind == "taint":
            verdict = "the run is held to the rules for tainted runs"
        elif kind == "canary":
            verdict = "reported"
        else:
            verdict = "refused" + (" (it stays refused)" if record.get("stays") else "")
        return {"kind": kind, "what": record.get("what"), "why": record.get("why"), "run": record.get("run") or run,
                "time": record.get("time"), "by": record.get("process"), "verdict": verdict, "fix": record.get("fix"),
                "severity": None, "source": record.get("source"), "last": record.get("last"),
                "count": record.get("count") if isinstance(record.get("count"), int) else 1}

    # -- the result --

    def result(self):
        # The canary tokens of every run found by now (a log directory's runs are found as it is read).
        self.redact = Scrubber(self.canary_tokens())
        findings = self.findings()
        runs = []
        for run, entry in sorted(self.scope["runs"].items(), key=lambda item: (
                {"start": 0, "run": 1}.get(item[1].get("role"), 2), epoch(item[1].get("started")) or 0, item[0])):
            entry = dict(entry)
            started, ended = epoch(entry.get("started")), epoch(entry.get("ended"))
            if entry.get("duration_s") is None and started is not None:
                end = ended if ended is not None else (time.time() if entry.get("state") in ("running", "paused")
                                                       else None)
                entry["duration_s"] = round(end - started, 1) if end is not None else None
            entry.pop("run_dir", None)
            entry["live"] = self.scope["live"].get(run)
            runs.append(entry)
        destinations = []
        for entry in self.destinations.values():
            destinations.append({
                "target": entry["target"], "addresses": sorted(entry["addresses"])[:10],
                "connections": len(entry["connections"]), "requests": entry["requests"],
                "bytes_out": entry["bytes_out"], "bytes_in": entry["bytes_in"], "allowed": entry["allowed"],
                "refused": entry["refused"], "failed": entry["failed"],
                "verdict": "refused" if entry["refused"] and not entry["allowed"] else
                "partly refused" if entry["refused"] else "failed" if entry["failed"] and not entry["allowed"] else
                "allowed",
                "why_allowed": [f"{why} ({count})" for why, count in entry["why_allowed"].most_common(4)],
                "why_refused": [f"{why} ({count})" for why, count in entry["why_refused"].most_common(4)],
                "processes": [name for name, _ in entry["processes"].most_common(5)],
                "how": dict(entry["how"].most_common(6)), "runs": sorted(entry["runs"]),
                "first": entry["first"], "last": entry["last"]})
        destinations.sort(key=lambda d: (-(d["refused"] > 0), -(d["allowed"] + d["refused"] + d["failed"]),
                                         d["target"]))
        lookups = []
        for entry in self.lookups.values():
            lookups.append({"name": entry["name"], "forwarded": entry["forwarded"], "refused": entry["refused"],
                            "failed": entry["failed"], "why": [w for w, _ in entry["why"].most_common(3)],
                            "processes": [p for p, _ in entry["processes"].most_common(4)],
                            "first": entry["first"], "last": entry["last"], "runs": sorted(entry["runs"])})
        lookups.sort(key=lambda e: (-e["refused"], -(e["forwarded"] + e["failed"]), e["name"]))
        totals = {
            "connections": sum(d["connections"] for d in destinations),
            "requests": sum(d["requests"] for d in destinations),
            "bytes_out": sum(d["bytes_out"] for d in destinations),
            "bytes_in": sum(d["bytes_in"] for d in destinations),
            "allowed": sum(d["allowed"] for d in destinations),
            "refused": sum(d["refused"] for d in destinations),
            "failed": sum(d["failed"] for d in destinations),
            "destinations": len(destinations),
            "lookups_refused": sum(e["refused"] for e in lookups),
            "lookups_forwarded": sum(e["forwarded"] for e in lookups),
            "findings": {severity: sum(1 for f in findings if f["severity"] == severity) for severity in SEVERITIES},
        }
        if self.attributed_by_time:
            self.notes.append("records that name no run (the firewall's, the name lookups', files and processes) are "
                              "counted for the run that ran at their time: runs of the same log directory that ran at "
                              "the same time cannot be told apart in them")
        if self.derived_left:
            self.notes.append(f"{self.derived_left} refusals of more kinds than {DERIVED_LIMIT} per run were not made "
                              "into findings (they are counted in the statistics)")
        unread = sum(entry.get("older_lines", 0) for entry in self.sources.used)
        if unread:
            self.notes.append(f"about {unread} older log lines were not read (each log only from its end: "
                              "the logs are not rotated); the appendix names the files")
        data = {
            "title": f"sandbox-ai report: {self.scope['title']}", "scope": self.scope["what"],
            "subject": self.scope["title"], "generated": utc(time.time()), "version": version(),
            "period": {"since": utc(self.since) if self.since is not None else None,
                       "until": utc(self.until) if self.until is not None else None},
            "runs": runs, "totals": totals,
            "destinations": destinations[:DESTINATIONS_LIMIT], "destinations_left_out":
                max(0, len(destinations) - DESTINATIONS_LIMIT),
            "lookups": lookups[:LOOKUPS_LIMIT], "lookups_left_out": max(0, len(lookups) - LOOKUPS_LIMIT),
            "approvals": self.approvals,
            "mcp": {server: {**entry, "tools": dict(entry["tools"].most_common(10))} for server, entry in
                    sorted(self.mcp.items())},
            "tools": {**self.tools, "tools": dict(self.tools["tools"].most_common(10))},
            "pushes": self.pushes, "models": dict(self.models.most_common(10)), "tokens": dict(self.tokens),
            "credentials": dict(self.credentials.most_common(20)), "added": dict(self.added.most_common(20)),
            "agent": dict(self.agent), "files": dict(self.files), "processes": self.processes,
            "programs": dict(self.programs.most_common(15)), "devices": self.devices, "vpn_checks": self.vpn_checks[:200],
            "findings": findings[:FINDINGS_LIMIT], "findings_left_out": max(0, len(findings) - FINDINGS_LIMIT),
            "sources": self.sources.used, "unreadable": self.sources.unreadable, "notes": self.notes,
            "not_recorded": NOT_RECORDED,
        }
        return self.redact.deep(data)


NOT_RECORDED = [
    "when the kill switch was set or lifted: only the mode in force while a run runs (cut.json) and the connections "
    "it refused or closed are logged",
    "why a connection was allowed, when neither an approval nor a rule decided it: the proxy logs no reason for "
    "what the network options allow; the report names the option it matches (presets, --allow-host, the model API)",
    "the options of a run that is neither running nor a kept start (run.json goes when the run ends)",
    "what was allowed during a run (sandbox-ai net allow) once it has ended",
    "the run of a firewall drop, a name lookup, a file change and a process: they are counted by time",
    "reads of your files (only changes are logged) and the content of requests beyond their logged size",
]


def gather(scope, since=None, until=None, sources=None, live=True):
    """The report's data (scrubbed) of SCOPE (resolve())."""
    return Gatherer(scope, since, until, sources, live).read().result()


# -- the document: blocks every format renders (see pdfdoc.py) --

def t(text):
    return ("t", str(text))


def b(text):
    return ("b", str(text))


def c(text):
    return ("c", str(text))


def link(text, anchor):
    return ("l", str(text), anchor)


def slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "section"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    taken.add(name)
    return name


def cell(*segments):
    return [s if isinstance(s, tuple) else t(s) for s in segments]


def run_title(run):
    name = run.get("name") or run.get("run")
    return f"{name} (run {run['run']})" if name != run.get("run") else str(name)


def joined(values, limit=6):
    values = [str(v) for v in values if v not in (None, "")]
    return ", ".join(values[:limit]) + (f", … ({len(values) - limit} more)" if len(values) > limit else "") \
        if values else "-"


def build(data):
    """(blocks, outline) of the report DATA."""
    blocks, outline, taken = [], [], set()
    sections = [("summary", "Summary"), ("runs", "Runs"), ("statistics", "Statistics"), ("network", "Network"),
                ("security", "Security report"), ("appendix", "Appendix")]
    anchors = {key: slug(title, taken) for key, title in sections}
    run_anchors = {run["run"]: slug(f"run {run_title(run)}", taken) for run in data["runs"]}
    finding_anchors = {f["id"]: slug(f"finding {f['id']}", taken) for f in data["findings"]}
    severity_anchors = {s: slug(f"severity {s}", taken) for s in SEVERITIES}

    def heading(level, text, anchor):
        blocks.append(("heading", level, text, anchor))
        outline.append((level, text, anchor))

    totals = data["totals"]
    blocks.append(("heading", 1, data["title"], slug("top", taken)))
    blocks.append(("para", [t(f"Generated {data['generated']} by sandbox-ai {data['version']}. Times are UTC.")],
                   "muted"))
    toc = [(1, title, anchors[key]) for key, title in sections]
    toc_entries = []
    for level, title, anchor in toc:
        toc_entries.append((level, title, anchor))
        if anchor == anchors["runs"]:
            toc_entries += [(2, run_title(run), run_anchors[run["run"]]) for run in data["runs"][:60]]
        if anchor == anchors["security"]:
            toc_entries += [(2, SEVERITY_TITLES[s], severity_anchors[s]) for s in SEVERITIES
                            if totals["findings"].get(s)]
    blocks.append(("heading", 2, "Contents", slug("contents", taken)))
    blocks.append(("toc", toc_entries))

    # Summary
    heading(1, "Summary", anchors["summary"])
    period = data["period"]
    running = sum(1 for run in data["runs"] if run.get("state") in ("running", "paused"))
    rows = [[cell("Report of"), cell(data["subject"])], [cell("Generated"), cell(data["generated"])],
            [cell("sandbox-ai"), cell(data["version"])],
            [cell("Period"), cell(f"{period['since'] or 'from the oldest record read'} to "
                                  f"{period['until'] or 'now'}")],
            [cell("Runs"), cell(f"{len(data['runs'])} ({running} running, {len(data['runs']) - running} ended)")]]
    blocks.append(("table", ["", ""], rows))
    if data["runs"]:
        blocks.append(("table", ["Run", "Role", "Agent", "State", "Started", "Duration", "Mode", "VPN/Tor",
                                 "Kill switch"],
                       [[cell(link(run_title(run), run_anchors[run["run"]])),
                         cell(run.get("role") + (f" of {run['part_of']}" if run.get("part_of") else "")),
                         cell(run.get("agent") or "-"), cell(run.get("state") or "-"), cell(utc_text(run.get("started"))),
                         cell(duration(run.get("duration_s"))), cell(run.get("mode") or "-"),
                         cell(vpn_text(run.get("vpn"))), cell(run.get("cut") or "-")] for run in data["runs"][:200]]))
    workspaces = sorted({str(w.get("host")) for run in data["runs"] for w in run.get("workspaces") or []
                         if isinstance(w, dict) and w.get("host")})
    approvals = data["approvals"]
    numbers = [
        f"{totals['connections']} connections and {totals['requests']} requests to {totals['destinations']} "
        f"destinations: {totals['allowed']} allowed, {totals['refused']} refused, {totals['failed']} failed",
        f"{size_text(totals['bytes_out'])} sent, {size_text(totals['bytes_in'])} received",
        f"name lookups: {totals['lookups_refused']} refused, {totals['lookups_forwarded']} forwarded",
        f"questions: {approvals['asked']} asked, {approvals['allowed']} allowed, {approvals['denied']} denied, "
        f"{approvals['unanswered']} unanswered" + (f" ({approvals['automatic']} answered automatically by --ask allow)"
                                                   if approvals["automatic"] else ""),
        f"MCP tool calls: {sum(e['calls'] for e in data['mcp'].values())} "
        f"({sum(e['refused'] for e in data['mcp'].values())} not allowed)",
        f"pushes: {data['pushes']['checked']} checked, {data['pushes']['refused']} refused",
    ]
    if workspaces:
        numbers.insert(0, "workspaces: " + joined(workspaces, 8))
    blocks.append(("para", [b("Key numbers")]))
    blocks.append(("list", [[t(text)] for text in numbers]))
    counts = totals["findings"]
    blocks.append(("para", [b("Security findings: ")] + [
        segment for s in SEVERITIES for segment in ([link(f"{counts[s]} {s}", severity_anchors[s]) if counts[s]
                                                     else t(f"{counts[s]} {s}"), t("  ")])]))
    if data["findings"]:
        blocks.append(("list", [[link(f["id"], finding_anchors[f["id"]]),
                                 t(f"  {f['severity'].upper()}  {f['title']}: {printable(f['what'], 160)}"
                                   + (f" (×{f['count']})" if f["count"] > 1 else ""))]
                                for f in data["findings"][:300]]))
        if len(data["findings"]) > 300:
            blocks.append(("para", [t(f"… and {len(data['findings']) - 300} more in the "),
                                    link("security report", anchors["security"]), t(".")]))
    else:
        blocks.append(("para", [t("Nothing was refused, held or reported.")]))
    for note in data["notes"]:
        blocks.append(("para", [t("Note: " + note)], "muted"))

    # Runs
    heading(1, "Runs", anchors["runs"])
    if not data["runs"]:
        blocks.append(("para", [t("No run was found in the logs read.")]))
    for run in data["runs"]:
        heading(2, run_title(run), run_anchors[run["run"]])
        settings = run.get("settings") or {}
        rows = [("Run id", run["run"]), ("Name", run.get("name")),
                ("Role", run.get("role") + (f" of {run['part_of']}" if run.get("part_of") else "")),
                ("Agent", run.get("agent")), ("Backend", run.get("backend")), ("State", run.get("state")),
                ("Started", utc_text(run.get("started"))), ("Ended", utc_text(run.get("ended"))),
                ("Duration", duration(run.get("duration_s"))),
                ("Exit status", run.get("status") if run.get("status") is not None else "-"),
                ("Workspaces", joined([f"{w.get('host')}" + (f" -> {w['sandbox']}" if w.get("sandbox") else "")
                                       for w in run.get("workspaces") or [] if isinstance(w, dict)])),
                ("Log directory", run.get("log_dir")),
                ("Security mode", run.get("mode") or settings.get("mode")),
                ("Preset", settings.get("preset")), ("Questions (--ask)", run.get("ask") or settings.get("ask")),
                ("VPN/Tor", vpn_text(run.get("vpn")) if run.get("vpn") else settings.get("vpn")),
                ("LLM server", run.get("llm") or settings.get("llm")),
                ("MCP servers", joined(run.get("mcp_servers") or [])),
                ("Kill switch", (f"{run['cut']} since {run.get('cut_since') or '?'}" if run.get("cut") else
                                 "not cut" if run.get("state") in ("running", "paused") else "-")),
                ("Taint", run.get("taint") or ("not tainted" if run.get("state") in ("running", "paused") else "-")),
                ("Telemetry", settings.get("telemetry")),
                ("Limits", joined([f"{k} {settings[k]}" for k in ("memory", "cpus", "pids") if settings.get(k)])),
                ("Devices", joined(list(settings.get("device") or []) + list(settings.get("gpu") or []))),
                ("Presets of hosts", joined(settings.get("presets") or [])),
                ("Hosts allowed (--allow-host)", joined(settings.get("allow_host") or [], 12)),
                ("Session events", joined([f"{k} {v}" for k, v in sorted((run.get("events") or {}).items())]))]
        always = ("Run id", "Role", "Agent", "State", "Started", "Ended", "Duration")
        blocks.append(("table", ["", ""], [[cell(key), cell(str(value) if value not in (None, "") else "-")]
                                           for key, value in rows
                                           if key in always or value not in (None, "", "-")]))
        if run.get("options"):
            blocks.append(("para", [b("Start options: "), c(" ".join(run["options"])[:1500])]))
        if run.get("workspace_changes"):
            blocks.append(("table", ["Time", "Workspace", "Change"],
                           [[cell(utc_text(w.get("time"))), cell(f"{w.get('host')} -> {w.get('sandbox')}"),
                             cell(f"{w.get('event')} ({w.get('mode') or 'rw'})")] for w in run["workspace_changes"]]))

    # Statistics
    heading(1, "Statistics", anchors["statistics"])
    by_volume = sorted(data["destinations"], key=lambda d: -(d["connections"] + d["requests"] + d["refused"]))[:40]
    blocks.append(("para", [b("Connections and requests per destination")]))
    if by_volume:
        blocks.append(("table", ["Destination", "Connections", "Requests", "Sent", "Received", "Allowed", "Refused",
                                 "Failed"],
                       [[cell(d["target"]), cell(d["connections"]), cell(d["requests"]), cell(size_text(d["bytes_out"])),
                         cell(size_text(d["bytes_in"])), cell(d["allowed"]), cell(d["refused"]), cell(d["failed"])]
                        for d in by_volume]))
    else:
        blocks.append(("para", [t("No connection was logged.")], "muted"))
    blocks.append(("para", [b("Questions (approvals)")]))
    blocks.append(("para", [t(f"{approvals['asked']} asked: {approvals['allowed']} allowed, {approvals['denied']} "
                              f"denied, {approvals['unanswered']} unanswered, {approvals['automatic']} answered "
                              "automatically (--ask allow).")]))
    if approvals["items"]:
        blocks.append(("table", ["Time", "Run", "Question", "Answer", "Scope"],
                       [[cell(utc_text(a.get("time"))), cell(a.get("run") or "-"), cell(a.get("question")),
                         cell(a.get("answer") + (" (automatically)" if a.get("automatic") else "")),
                         cell(a.get("scope") or "-")] for a in approvals["items"]]))
    blocks.append(("para", [b("MCP tool calls")]))
    if data["mcp"]:
        blocks.append(("table", ["Server", "Calls", "Asked", "Not allowed", "Tools"],
                       [[cell(server), cell(e["calls"]), cell(e["asked"]), cell(e["refused"]),
                         cell(joined([f"{tool} {count}" for tool, count in e["tools"].items()], 8))]
                        for server, e in data["mcp"].items()]))
    else:
        blocks.append(("para", [t("No MCP tool call was logged.")], "muted"))
    tools = data["tools"]
    if tools["checked"]:
        blocks.append(("para", [b("The agent's tool calls checked by rules: "),
                                t(f"{tools['checked']} ({tools['refused']} refused): " +
                                  joined([f"{k} {v}" for k, v in tools["tools"].items()], 10))]))
    pushes = data["pushes"]
    blocks.append(("para", [b("Pushes (privacy check)")]))
    if pushes["items"]:
        blocks.append(("table", ["Time", "Run", "Where", "Refs", "Result"],
                       [[cell(utc_text(p.get("time"))), cell(p.get("run") or "-"), cell(p.get("url") or "-"),
                         cell(joined(p.get("refs") or [])),
                         cell("checked" if p.get("ok") else f"REFUSED: {p.get('message') or ''}")]
                        for p in pushes["items"]]))
    else:
        blocks.append(("para", [t("No push was made.")], "muted"))
    lines = []
    if data["models"]:
        lines.append([t("model calls: " + joined([f"{m} {n}" for m, n in data["models"].items()]) + "; tokens: " +
                        joined([f"{k} {v}" for k, v in sorted(data["tokens"].items())], 8))])
    if data["credentials"]:
        lines.append([t("credentials the agent sent (names only): " +
                        joined([f"{k} ({v})" for k, v in data["credentials"].items()], 8))])
    if data["added"]:
        lines.append([t("logins the proxy added on the wire: " +
                        joined([f"{k} ({v})" for k, v in data["added"].items()], 8))])
    if data["agent"]:
        lines.append([t("the agent's timeline: " + joined([f"{v} {k}" for k, v in sorted(data["agent"].items())], 8))])
    if data["files"]:
        lines.append([t("file events (files.jsonl): " + joined([f"{v} {k}" for k, v in sorted(data["files"].items())]))])
    if data["processes"]:
        lines.append([t(f"processes started: {data['processes']}: " +
                        joined([f"{k} {v}" for k, v in data["programs"].items()], 10))])
    devices = data["devices"]
    if devices["added"] or devices["refused"]:
        lines.append([t(f"devices: {devices['added']} added, {devices['refused']} refused during the run")])
    if data["vpn_checks"]:
        failed = sum(1 for check in data["vpn_checks"] if not check["ok"])
        lines.append([t(f"VPN and Tor checks: {len(data['vpn_checks'])} ({failed} failed)")])
    if lines:
        blocks.append(("para", [b("More")]))
        blocks.append(("list", lines))

    # Network
    heading(1, "Network", anchors["network"])
    blocks.append(("para", [t("Every destination the sandboxes tried, whether it was allowed and why (the option, "
                              "preset, approval or rule), or why it was refused, with the processes that tried where "
                              "the log names them.")]))
    if data["destinations"]:
        blocks.append(("table", ["Destination", "Verdict", "Why", "Count", "Processes", "First / last"],
                       [[cell(d["target"] + (f" ({joined(d['addresses'], 3)})" if d["addresses"] and
                                             d["addresses"] != [d["target"]] else "")),
                         cell(d["verdict"]),
                         cell("; ".join(d["why_refused"] + d["why_allowed"]) or "-"),
                         cell(f"{d['allowed']} allowed, {d['refused']} refused" +
                              (f", {d['failed']} failed" if d["failed"] else "")),
                         cell(joined(d["processes"], 3)),
                         cell(f"{utc_text(d['first'])} / {utc_text(d['last'])}")] for d in data["destinations"]]))
        if data["destinations_left_out"]:
            blocks.append(("para", [t(f"… and {data['destinations_left_out']} more destinations.")], "muted"))
    else:
        blocks.append(("para", [t("No connection was logged.")], "muted"))
    blocks.append(("para", [b("Name lookups")]))
    if data["lookups"]:
        blocks.append(("table", ["Name", "Refused", "Forwarded", "Why", "Processes", "First / last"],
                       [[cell(e["name"]), cell(e["refused"]), cell(e["forwarded"]), cell("; ".join(e["why"]) or "-"),
                         cell(joined(e["processes"], 3)), cell(f"{utc_text(e['first'])} / {utc_text(e['last'])}")]
                        for e in data["lookups"]]))
    else:
        blocks.append(("para", [t("No refused or forwarded lookup was logged (names the options pin are answered "
                                  "without a record).")], "muted"))
    for run in data["runs"]:
        live = run.get("live")
        if not live:
            continue
        blocks.append(("para", [b(f"Allowed during the run of {run_title(run)} (sandbox-ai net show)")]))
        rows = [[cell(kind), cell(joined(values, 20))] for kind, values in (
            ("HTTPS addresses", live.get("https")), ("TCP", live.get("tcp")), ("All ports", live.get("nets")),
            ("Nameservers", live.get("dns")), ("Host ports", live.get("host_ports")), ("Names", live.get("hosts")),
            ("Given with net allow", (live.get("given") or {}).get("hosts", []) +
             (live.get("given") or {}).get("tcp", []))) if values]
        blocks.append(("table", ["What", "Destinations"], rows) if rows else
                      ("para", [t("nothing beyond its start options")], "muted"))

    # Security report
    heading(1, "Security report", anchors["security"])
    blocks.append(("para", [t("Everything the sandboxes tried or did that was not allowed, held or reported, the most "
                              "severe first: refused connections and lookups, TLS refusals, canaries, pushes the "
                              "privacy check refused, rules, the security mode and the policy, questions answered "
                              "with no, the taint, VPN and Tor checks, and the kill switch. Equal events of a run "
                              "are counted once.")]))
    blocks.append(("table", ["Severity", "Findings"],
                   [[cell(link(SEVERITY_TITLES[s], severity_anchors[s]) if counts[s] else t(SEVERITY_TITLES[s])),
                     cell(counts[s])] for s in SEVERITIES]))
    if not data["findings"]:
        blocks.append(("para", [t("Nothing was refused, held or reported in the logs read.")]))
    for severity in SEVERITIES:
        group = [f for f in data["findings"] if f["severity"] == severity]
        if not group:
            continue
        heading(2, f"{SEVERITY_TITLES[severity]} ({len(group)})", severity_anchors[severity])
        for finding in group:
            title = f"{finding['id']} · {finding['title']}"
            heading(3, title, finding_anchors[finding["id"]])
            when = utc_text(finding["first"]) + (f" to {utc_text(finding['last'])}" if finding["count"] > 1 and
                                                  finding["last"] != finding["first"] else "")
            run = next((r for r in data["runs"] if r["run"] == finding["run"]), None)
            rows = [[cell("Severity"), cell(finding["severity"].upper())],
                    [cell("What"), cell(finding["what"])], [cell("Why"), cell(finding["why"] or "-")],
                    [cell("When"), cell(when + (f" ({finding['count']} times)" if finding["count"] > 1 else ""))],
                    [cell("Run"), cell(link(run_title(run), run_anchors[run["run"]]) if run else
                                       t(finding["run"] or "-"))],
                    [cell("By"), cell(joined(finding["by"], 5))],
                    *([[cell("Examples"), cell(joined(finding["examples"], 3))]] if finding.get("examples") else []),
                    [cell("Verdict"), cell(finding.get("verdict") or "-")],
                    [cell("What to do"), cell(finding.get("fix") or "-")],
                    [cell("Source"), cell(finding.get("source") or "-")]]
            blocks.append(("table", ["", ""], rows))
    if data["findings_left_out"]:
        blocks.append(("para", [t(f"… and {data['findings_left_out']} more findings (the least severe) not listed.")]))

    # Appendix
    heading(1, "Appendix", anchors["appendix"])
    blocks.append(("para", [b("Log files read")]))
    if data["sources"]:
        blocks.append(("table", ["File", "What", "Size", "Read", "Not read (older)", "Records"],
                       [[cell(c(s["path"])), cell(s["kind"]), cell(size_text(s["size"])), cell(size_text(s["read"])),
                         cell(f"{size_text(s['skipped'])} (about {s['older_lines']} lines)" if s.get("skipped") else "-"),
                         cell(s["records"])] for s in data["sources"]]))
    else:
        blocks.append(("para", [t("No log file was found.")], "muted"))
    blocks.append(("para", [b("What could not be read")]))
    if data["unreadable"]:
        blocks.append(("list", [[c(u["path"]), t(f" ({u['kind']}): {u['why']}")] for u in data["unreadable"]]))
    else:
        blocks.append(("para", [t("Every log found was read.")], "muted"))
    blocks.append(("para", [b("What sandbox-ai does not record (so this report cannot show it)")]))
    blocks.append(("list", [[t(text)] for text in data["not_recorded"]]))
    return blocks, outline


def vpn_text(vpn):
    if not isinstance(vpn, dict) or not vpn:
        return "-"
    if vpn.get("type") == "tor":
        return f"Tor ({vpn.get('profile')})" if vpn.get("profile") else "Tor"
    return " ".join(str(v) for v in (vpn.get("profile"), f"({vpn['type']})" if vpn.get("type") else None) if v)


# -- the formats --

MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|])")  # what could make markup, a link or a table cell of text


def md_text(text):
    return MD_SPECIAL.sub(r"\\\1", str(text)).replace("\n", " ")


def md_segments(segments):
    out = []
    for segment in segments:
        kind, text = segment[0], str(segment[1])
        if kind == "l":
            out.append(f"[{md_text(text)}](#{segment[2]})")
        elif kind == "b":
            stripped = text.rstrip()
            out.append(f"**{md_text(stripped)}**{text[len(stripped):]}" if stripped else md_text(text))
        elif kind == "c":
            out.append("`" + text.replace("`", "'") + "`" if text else "")
        else:
            out.append(md_text(text))
    return "".join(out)


def markdown(blocks):
    lines = []
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            lines += [f'<a id="{block[3]}"></a>', "", "#" * block[1] + " " + md_text(block[2]), ""]
        elif kind == "para":
            text = md_segments(block[1])
            lines += [f"*{text}*" if len(block) > 2 and block[2] == "muted" and text else text, ""]
        elif kind == "list":
            lines += [f"- {md_segments(item)}" for item in block[1]] + [""]
        elif kind == "toc":
            lines += [f"{'  ' * (level - 1)}- [{md_text(text)}](#{anchor})" for level, text, anchor in block[1]] + [""]
        elif kind == "table":
            headers = [md_text(h) or " " for h in block[1]]
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("|" + "|".join("---" for _ in headers) + "|")
            for row in block[2]:
                cells = [md_segments(cell_) or " " for cell_ in row] + [" "] * (len(headers) - len(row))
                lines.append("| " + " | ".join(cells[:len(headers)]) + " |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


HTML_STYLE = """
:root { color-scheme: light dark; --bg: #ffffff; --fg: #1d1f23; --muted: #5d636e; --line: #d8dbe0; --head: #f2f4f7;
  --link: #0b5cad; --critical: #b3261e; --high: #c05600; --medium: #8a6d00; --low: #2f6f9f; --info: #5d636e; }
@media (prefers-color-scheme: dark) { :root { --bg: #16181c; --fg: #e4e6ea; --muted: #9aa1ac; --line: #343842;
  --head: #23262d; --link: #7ab7ff; --critical: #ff6b61; --high: #ff9a4d; --medium: #e3c447; --low: #7ab7ff;
  --info: #9aa1ac; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5 system-ui, -apple-system, "Segoe UI",
  Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }
h1 { font-size: 1.6em; border-bottom: 2px solid var(--line); padding-bottom: 4px; margin-top: 1.6em; }
h2 { font-size: 1.25em; margin-top: 1.4em; }
h3 { font-size: 1.05em; margin: 1.2em 0 0.4em; }
a { color: var(--link); }
.muted { color: var(--muted); }
code { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 0.92em; word-break: break-all; }
nav.toc ul { list-style: none; padding-left: 1.2em; margin: 0.2em 0; }
nav.toc > ul { padding-left: 0; }
.table { overflow-x: auto; margin: 0.4em 0 1em; }
table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
th, td { border: 1px solid var(--line); padding: 4px 7px; text-align: left; vertical-align: top;
  overflow-wrap: anywhere; }
th { background: var(--head); }
table.facts td:first-child { width: 13em; color: var(--muted); }
.sev-critical { color: var(--critical); } .sev-high { color: var(--high); } .sev-medium { color: var(--medium); }
.sev-low { color: var(--low); } .sev-info { color: var(--info); }
@media print { body { background: #fff; color: #000; font-size: 10pt; } main { max-width: none; padding: 0; }
  a { color: #000; } h1 { break-before: page; } h1:first-of-type { break-before: avoid; }
  h2, h3 { break-after: avoid; } tr { break-inside: avoid; } nav.toc { break-after: page; } }
"""


def html_segments(segments):
    out = []
    for segment in segments:
        kind, text = segment[0], html.escape(str(segment[1]))
        if kind == "l":
            out.append(f'<a href="#{html.escape(segment[2])}">{text}</a>')
        elif kind == "b":
            out.append(f"<strong>{text}</strong>")
        elif kind == "c":
            out.append(f"<code>{text}</code>")
        else:
            out.append(text)
    return "".join(out)


def html_document(blocks, title):
    parts = [f"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
             f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
             f"<meta name=\"generator\" content=\"sandbox-ai report\">\n<title>{html.escape(title)}</title>\n"
             f"<style>{HTML_STYLE}</style>\n</head>\n<body>\n<main>"]
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            level, text, anchor = block[1], block[2], block[3]
            parts.append(f'<h{level} id="{html.escape(anchor)}">{html.escape(text)}</h{level}>')
        elif kind == "para":
            muted = ' class="muted"' if len(block) > 2 and block[2] == "muted" else ""
            parts.append(f"<p{muted}>{html_segments(block[1])}</p>")
        elif kind == "list":
            parts.append("<ul>" + "".join(f"<li>{html_segments(item)}</li>" for item in block[1]) + "</ul>")
        elif kind == "toc":
            def items(nodes):
                return "<ul>" + "".join(
                    f'<li><a href="#{html.escape(node["anchor"])}">{html.escape(node["title"])}</a>'
                    + (items(node["children"]) if node["children"] else "") + "</li>" for node in nodes) + "</ul>"
            parts.append('<nav class="toc">' + items(module("pdfdoc").outline_tree(block[1])) + "</nav>")
        elif kind == "table":
            headers = block[1]
            facts = all(not str(h) for h in headers)
            head = "" if facts else "<thead><tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in headers) + \
                "</tr></thead>"
            rows = []
            for row in block[2]:
                cells = []
                for index, cell_ in enumerate(row):
                    content = html_segments(cell_)
                    if index == 1 and facts and cell_ and str(cell_[0][1]).lower() in SEVERITIES:
                        content = f'<span class="sev-{cell_[0][1].lower()}">{content}</span>'
                    cells.append(f"<td>{content}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            css = ' class="facts"' if facts else ""
            parts.append(f'<div class="table"><table{css}>{head}<tbody>' + "".join(rows) + "</tbody></table></div>")
    parts.append("</main>\n</body>\n</html>\n")
    return "\n".join(parts)


def render(data, kind):
    """The report DATA as KIND (md, html: str; pdf: bytes)."""
    blocks, outline = build(data)
    if kind == "md":
        return markdown(blocks)
    if kind == "html":
        return html_document(blocks, data["title"])
    if kind == "pdf":
        return module("pdfdoc").render(blocks, title=data["title"], outline=outline)
    raise ValueError(f"no format {kind!r}; formats: {', '.join(FORMATS)}")


def write_file(path, content):
    """CONTENT into PATH (mode 0600: a report names what a run did), replaced whole."""
    path = Path(path).expanduser()
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    directory = path.parent if str(path.parent) else Path(".")
    fd, temporary = tempfile.mkstemp(prefix=".report-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


# -- the command --

def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai report", description=__doc__.split("\n\n")[0],
                                epilog="TIME: 10m, 2h, 3d, 14:30, 2026-11-04 or an ISO time. The report holds no "
                                "secret; it does name hosts, paths and commands of the runs: share it with care.")
    p.add_argument("name", nargs="*", metavar="NAME", help="A run id or the name of a start (sandbox-ai list); "
                   "several: one report of all of them")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-s", "--session", metavar="NAME", help="A session with all its runs and parts")
    where.add_argument("-a", "--all", action="store_true", help="Every run: the running ones and the default log "
                       "directories")
    where.add_argument("-p", "--project", type=Path, metavar="PATH", help="Every run of a project (its default log "
                       "directory)")
    where.add_argument("-l", "--log-dir", type=Path, metavar="DIR", help="Every run that logged into DIR")
    p.add_argument("-f", "--format", choices=FORMATS, help="md, html or pdf (default: the suffix of -o, else md)")
    p.add_argument("-o", "--output", type=Path, metavar="FILE", help="Write the report to FILE")
    p.add_argument("--since", metavar="TIME", help="Only what happened from TIME on")
    p.add_argument("--until", metavar="TIME", help="Only what happened up to TIME")
    p.add_argument("--no-parts", action="store_true", help="The start alone, without its MCP servers, helpers and team")
    p.add_argument("-j", "--json", action="store_true", help="The gathered data as JSON instead of a report")
    return p


def output_format(a):
    if a.format:
        return a.format
    if a.output is not None:
        return SUFFIXES.get(a.output.suffix.lower(), "md")
    return "md"


def cli(argv, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    a = parser().parse_args(argv)
    if a.name and (a.all or a.project or a.log_dir):
        raise ValueError("name a run or use --all, --project or --log-dir, not both")
    if a.name and a.session:
        raise ValueError("name a run or a session (--session), not both")
    activity = module("activity")
    since = activity.since_value(a.since) if a.since else None
    until = activity.since_value(a.until) if a.until else None
    if since is not None and until is not None and until < since:
        raise ValueError("--until is before --since")
    kind = output_format(a)
    if kind == "pdf" and a.output is None and not a.json and out.isatty():
        raise ValueError("a PDF is not shown in a terminal: write it to a file with -o FILE.pdf")
    scope = resolve(a.name, a.session, a.all, a.project, a.log_dir, parts=not a.no_parts)
    data = gather(scope, since, until)
    if a.json:
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        content = render(data, kind)
    if a.output is not None:
        path = write_file(a.output, content)
        print(f"sandbox-ai report: written to {printable(path, 400)} ({kind if not a.json else 'json'}, "
              f"{len(data['runs'])} run(s), {len(data['findings'])} finding(s))", file=err)
        return 0
    if isinstance(content, bytes):
        out.flush()
        out.buffer.write(content)
        out.buffer.flush()
    else:
        out.write(content)
    return 0


def main(argv=None):
    try:
        return cli(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            sys.stdout = open(os.devnull, "w")
        return 0
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai report: {printable(exc, 1000)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
