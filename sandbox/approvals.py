#!/usr/bin/env python3
"""sandbox-ai approvals: answer what running sandboxes ask.

  sandbox-ai approvals list [--json]             open questions of every running sandbox
  sandbox-ai approvals allow ID [--scope once|run|always] [--no-open]
  sandbox-ai approvals deny ID [--scope run|once]
  sandbox-ai approvals history [RUN]             questions and their answers
  sandbox-ai approvals remembered [--json]        what was allowed "always", per project
  sandbox-ai approvals forget PROJECT ENTRY       forget one of them (a host name or IP:PORT)
  sandbox-ai approvals blocked [RUN] [--json]     what a running sandbox refused, newest first: why, and what to do
  sandbox-ai approvals decided [RUN] [--json]     the decisions a running sandbox remembers for the run
  sandbox-ai approvals ask-again ID               forget what the run decided about question ID: asked again

A sandbox asks (with --ask, the default) when its agent connects to a destination the options do not
allow, looks up a name they do not pin, pushes commits the privacy check has warnings about, or sends a
request an "ask" rule holds; an MCP server asks about tool calls its rules hold; a device rule asks before a
device is attached (`sandbox-ai devices add`, --device, a replug). Without an answer within
60 seconds the question is refused (a canary's within 5 minutes; a request to open a URL in your browser
waits until it is answered or the run ends). Scopes: once (this connection), run (until the
sandbox ends), always (also every later start of this project; kept in
~/.config/sandbox-ai/approved.json). A name allowed after a lookup is reached by the agent's next try
(for lookups once means run: the name stays pinned); only its public addresses are opened. A deny counts
for the rest of the run (--scope once: the agent may ask again). A question nobody answers is no decision: it is
refused, the same request is refused at once for the next 2 minutes (the agent does not wait out the timeout again
and again), then asked again. On a question answered (or left unanswered) already, allow ID --scope run and deny ID
change what the run decided, for the rest of it, and ask-again ID forgets it; allow ID --scope always on a request or
MCP tool call writes an exception rule into the ruleset that held it (yours in place; a preset's or someone else's
as a copy of your own), on a connection or lookup it keeps the destination in the project (net allow --keep). The name in a connection question is the
one the agent sends: what is allowed is the address, and "always" keeps the name only when the sandbox
proved it (its own pin, or the real server's certificate on the approved connection); the question says
which it will be.

One question, one prompt: while the desktop app or the web interface (`sandbox-ai web`, with a page open) watches
the questions, it says so with a heartbeat in ~/.local/state/sandbox-ai/approvals-watchers/PID.json, and the
launcher shows no notification of its own - for a question, a browser request, a safeguard that refused or ended
something, a limit reached (no buttons, no plain notification either): the watcher shows it, or keeps it on its
pages. A canary's alarm is always shown. A heartbeat older than 15 seconds,
or of a process that is gone, counts no more.

A sandbox started with --ask allow asks nobody: every question is answered "allow" at once, for the run (or the
narrower scope the administrator's policy and the run's security mode allow; never "always"), by the launcher on
the host (auto_answer), and recorded like your answer ("allowed automatically (--ask allow)" in approvals history,
the connection log and sandbox-ai logs). Not questions, so not answered: what rules deny, the privacy check's
blocking findings, what you blocked always. A canary's alarm (a decoy file read: the sandbox is paused) and a
browser request whose URL carries a canary token of the run still go to you; a lookup no answer may allow (a name
with a canary token, one outside the policy, one with no public address, a run that approves only once) is
refused at once.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent


_textout = None


def textout():
    """sandbox/textout.py (aligned tables, colour for terminals), loaded by path like the other modules."""
    global _textout
    if _textout is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_approvals", Path(__file__).resolve().parent / "textout.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _textout is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _textout = loaded
    return _textout
ID = re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}")
REQUEST_LIMIT = 8192  # one browser request from the sandbox: {"url": ...}
# Printable ASCII only (no spaces, controls, look-alike letters), and no user part: https://github.com@evil.example/
# reads as github.com but opens evil.example.
URL = re.compile(r"https?://(?![^/?#]*@)[!-~]{1,4000}")
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{1,63}")
TCP = re.compile(r"(?:[0-9.]{7,15}|\[[0-9a-f:.]{2,45}\]):[0-9]{1,5}")


def shown(value, limit=300):
    """VALUE as it may be printed: what the agent chose (names, URLs, refs) with every character outside
    printable ASCII escaped, so it cannot add lines, colours or look-alike text to what you read."""
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


def net():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_net_for_approvals", HERE / "net.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_runtime = None


def runtime():
    """runtime.py (loaded once, when needed): how rules read a path."""
    global _runtime
    if _runtime is None:
        spec = importlib.util.spec_from_file_location("runtime_for_approvals", HERE / "runtime.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _runtime = module
    return _runtime


SCOPES = ("once", "run", "always")


POLICY_FILE = Path("/etc/sandbox-ai/policy.json")  # agent_sandbox.POLICY_FILE (a test keeps them the same)


def policy_scope():
    """The widest scope the administrator's policy allows (always without a policy). Without a policy file (most
    machines) the launcher is not loaded for it: that took a third of every `sandbox-ai approvals` command (each
    answer, each listing of the app and the web interface); with one, the launcher reads and checks it."""
    if not os.path.lexists(POLICY_FILE):
        return "always"
    spec = importlib.util.spec_from_file_location("agent_sandbox_for_approvals", HERE / "agent_sandbox.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher.load_policy(POLICY_FILE).get("max_approval_scope", "always")


WATCHER_STALE = 15  # seconds: a watcher's heartbeat older than this counts no more
WATCHER_REFRESH = 5  # seconds between the refreshes of a watcher's heartbeat (the app, the web interface)
WATCHER_KINDS = ("app", "web")


def watchers_dir():
    """Where the watchers of the questions (the desktop app, the web interface) keep their heartbeats."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "approvals-watchers"


def heartbeat(kind, pid=None):
    """Write (or refresh) the heartbeat of the watcher KIND ("app", "web") of process PID (this one): while it is
    fresh, the launcher leaves the questions to that watcher's prompt (watched). Refresh it every WATCHER_REFRESH
    seconds, remove it with end_heartbeat when the watcher quits."""
    if kind not in WATCHER_KINDS:
        raise ValueError(f"a watcher is one of {', '.join(WATCHER_KINDS)}")
    pid = int(pid or os.getpid())
    directory = watchers_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / f".{pid}.{threading.get_ident()}.json"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as file:
        json.dump({"pid": pid, "kind": kind, "time": time.time()}, file)
    path = directory / f"{pid}.json"
    os.replace(temporary, path)
    return path


def end_heartbeat(pid=None):
    """Remove the heartbeat of process PID (this one): the watcher quits."""
    with contextlib.suppress(OSError):
        (watchers_dir() / f"{int(pid or os.getpid())}.json").unlink()


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:  # another user's process: there
        return True
    except (OSError, OverflowError):
        return False
    return True


def watchers(now=None):
    """[{"pid", "kind", "time"}] of the watchers whose heartbeat is fresh (at most WATCHER_STALE seconds old) and
    whose process is still there."""
    now = time.time() if now is None else now
    try:
        files = sorted(watchers_dir().glob("*.json"))
    except OSError:
        return []
    found = []
    for path in files:
        if not path.stem.isascii() or not path.stem.isdigit() or len(path.stem) > 10:
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("pid") != int(path.stem) or \
                record.get("kind") not in WATCHER_KINDS or not isinstance(record.get("time"), (int, float)):
            continue
        if abs(now - record["time"]) <= WATCHER_STALE and process_alive(record["pid"]):
            found.append(record)
    return found


def watched(now=None):
    """The desktop app or the web interface watches the questions now: they ask, the launcher does not."""
    return bool(watchers(now))


# The answers a prompt offers (choices): (decision, scope, label). Browser requests and canaries have their own.
CHOICES = (("allow", "once", "Allow once"), ("allow", "run", "Allow for this run"), ("allow", "always", "Allow always"),
           ("deny", "run", "Block"), ("deny", "always", "Block always"))
OWN_CHOICES = {"browser": (("allow", "once", "Open"), ("deny", "once", "Ignore")),
               "canary": (("allow", "once", "Continue"), ("deny", "run", "Stop the sandbox"))}
SCOPE_WORDS = {"once": "once", "run": "for the run", "always": "always"}


def choices(question, record=None, widest=None):
    """The answers to QUESTION, as the buttons of a prompt: [{"decision", "scope", "label", "enabled", "why",
    "reason"}], every one of CHOICES, with the caps answer() applies: the administrator's policy (WIDEST, default
    policy_scope()), the run's security mode (RECORD, its run.json: "mode" -> "approval_scope") and the kind of
    question (pushes, requests, tool calls and devices at most for the run; only names and addresses are blocked
    always; a name is pinned for the run, so a run that approves only once cannot allow one). A choice that is not
    possible is not left out: "enabled" is false and "why" says why. An enabled one may carry a note in "why" too
    (what "always" remembers; a looked-up name stays pinned for the run). "reason" names the why: policy, mode,
    push, request, device, dns-once, block-kind, dns-pinned, remembers."""
    question = question if isinstance(question, dict) else {}
    kind = question.get("kind")
    if kind in OWN_CHOICES:
        return [{"decision": decision, "scope": scope, "label": label, "enabled": True, "why": "", "reason": ""}
                for decision, scope, label in OWN_CHOICES[kind]]
    if widest not in SCOPES:
        try:
            widest = policy_scope()
        except (OSError, ValueError):
            widest = "once"  # an unreadable policy: the narrowest (answer() checks it itself)
        widest = widest if widest in SCOPES else "once"
    mode = record.get("mode") if isinstance(record, dict) and isinstance(record.get("mode"), dict) else {}
    cap = mode.get("approval_scope") if mode.get("approval_scope") in SCOPES else "always"
    by_policy = ("policy", f"capped by the administrator's policy (at most {SCOPE_WORDS[widest]})")
    by_mode = ("mode", f"capped by the security mode {shown(mode.get('name') or '?', 20)}")
    no_name = kind == "dns" and widest_answer(question, record if isinstance(record, dict) else {}, widest) is None
    found = []
    for decision, scope, label in CHOICES:
        reason, why = "", ""
        if decision == "allow":
            if no_name:
                reason, why = "dns-once", DNS_ONCE
            elif scope == "always" and kind == "push":
                reason, why = "push", "pushes are approved at most for the run"
            elif scope == "always" and kind in ("http", "mcp", "tool"):
                reason, why = "request", "requests and tool calls are approved at most for the run; to allow them " \
                                         "always, write a rule"
            elif scope == "always" and kind == "device":
                reason, why = "device", "a device is approved at most for the run; to allow it always, write a " \
                                        "device rule"
            elif SCOPES.index(scope) > SCOPES.index(widest):
                reason, why = by_policy
            elif SCOPES.index(scope) > SCOPES.index(cap):
                reason, why = by_mode
        elif scope == "always":
            if kind not in ("connect", "dns"):
                reason, why = "block-kind", "only names and addresses are blocked always"
            elif widest != "always":
                reason, why = by_policy
            elif cap != "always":
                reason, why = by_mode
        enabled = not reason
        if enabled and decision == "allow" and scope == "once" and kind == "dns":
            reason, why = "dns-pinned", "a name stays pinned for the run"
        elif enabled and decision == "allow" and scope == "always" and kind == "connect":
            text = always(question).removeprefix("; Allow always: ")
            if not text:  # a question of an older sandbox: the address is what is remembered
                address, port = str(question.get("address", "?")), question.get("port", "?")
                text = f"{shown(f'[{address}]' if ':' in address else address, 60)}:{shown(port, 6)} (address only)"
            reason, why = "remembers", f"remembers {text}"
        elif enabled and decision == "allow" and scope == "always" and kind == "dns":
            reason, why = "remembers", f"remembers the name {shown(question.get('name'), 253)}"
        found.append({"decision": decision, "scope": scope, "label": label, "enabled": enabled, "why": why,
                      "reason": reason})
    return found


def approved_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "approved.json"


def approved_for(workspace, blocked=False):
    """{"hosts": [...], "tcp": ["IP:PORT", ...]} always allowed for the project WORKSPACE (BLOCKED: always
    blocked: refused at once, without a question)."""
    try:
        data = json.loads(approved_file().read_text())
    except (OSError, ValueError):
        return {"hosts": [], "tcp": []}
    entry = data.get(str(Path(workspace).resolve())) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return {"hosts": [], "tcp": []}
    prefix = "blocked_" if blocked else ""
    hosts = [h for h in entry.get(prefix + "hosts", []) if isinstance(h, str) and HOST.fullmatch(h)]
    tcp = [t for t in entry.get(prefix + "tcp", []) if isinstance(t, str) and valid_tcp(t)]
    return {"hosts": hosts, "tcp": tcp}


def blocked_for(workspace):
    return approved_for(workspace, blocked=True)


def valid_tcp(value):
    if not TCP.fullmatch(value):
        return False
    address, port = value.rsplit(":", 1)
    try:
        ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return 1 <= int(port) <= 65535


@contextlib.contextmanager
def approved_store():
    """The approvals store, read and written under a lock: remember and forget at the same time (an answer in
    one terminal, Forget in the app) must not lose or resurrect an entry."""
    path = approved_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.with_name(".approved.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError, RecursionError):
            data = {}
        yield data
        temporary = path.with_name(f".approved.{os.getpid()}.json")
        temporary.write_text(json.dumps(data, indent=2))
        temporary.chmod(0o600)
        os.replace(temporary, path)


def remember(workspace, host=None, tcp=None, blocked=False, details=None):
    with approved_store() as data:
        remember_in(data, workspace, host, tcp, blocked, details)


def detail_key(field, value):
    """The key of an entry's details: "hosts:NAME", "blocked_tcp:IP:PORT", ..."""
    return f"{field}:{value}"


def remember_in(data, workspace, host=None, tcp=None, blocked=False, details=None):
    """Remember HOST or TCP as allowed (or, BLOCKED, as blocked) always for WORKSPACE; one excludes the other.
    DETAILS ({"added", "question", "kind", "verified", "destination", "why"}) say where it came from."""
    if host is not None and not HOST.fullmatch(host):
        raise ValueError(f"not a host name to remember: {shown(host)}")
    if tcp is not None and not valid_tcp(tcp):
        raise ValueError(f"not an address to remember: {shown(tcp)}")
    entry = data.setdefault(str(Path(workspace).resolve()), {"hosts": [], "tcp": []})
    keep, drop = ("blocked_", "") if blocked else ("", "blocked_")
    notes = entry.get("details")
    if not isinstance(notes, dict):
        notes = entry["details"] = {}
    for field, value in (("hosts", host), ("tcp", tcp)):
        if value:
            values = entry.setdefault(keep + field, [])
            if value not in values:
                values.append(value)
            if value in entry.get(drop + field, []):
                entry[drop + field] = [v for v in entry[drop + field] if v != value]
                notes.pop(detail_key(drop + field, value), None)
            notes[detail_key(keep + field, value)] = {"added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                      **(details or {})}


def remembered():
    """{project: {"hosts": [...], "tcp": [...], "blocked_hosts", "blocked_tcp", "details"}} of everything approved
    or blocked "always" (valid entries only); details: {"hosts:NAME": {"added", "question", ...}} where known."""
    try:
        data = json.loads(approved_file().read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    result = {}
    for project in sorted(data) if isinstance(data, dict) else []:
        entry, blocked = approved_for(project), blocked_for(project)
        if entry["hosts"] or entry["tcp"] or blocked["hosts"] or blocked["tcp"]:
            known = data[project].get("details") if isinstance(data[project], dict) else None
            listed = {detail_key(field, value) for field, values in (
                ("hosts", entry["hosts"]), ("tcp", entry["tcp"]), ("blocked_hosts", blocked["hosts"]),
                ("blocked_tcp", blocked["tcp"])) for value in values}
            details = {key: value for key, value in (known.items() if isinstance(known, dict) else [])
                       if key in listed and isinstance(value, dict)}
            result[project] = {**entry, "blocked_hosts": blocked["hosts"], "blocked_tcp": blocked["tcp"],
                               "details": details}
    return result


def origin_of(details, field="tcp"):
    """What the entry of FIELD (hosts, tcp, blocked_...) is and where it came from, for `remembered`: "name,
    verified by its certificate; added TIME from question ID (connect to IP:PORT)"; what came from a question
    is escaped (see shown)."""
    kind = "name" if field.endswith("hosts") else "address"
    if not isinstance(details, dict) or not details:
        return f"{kind}; added before sandbox-ai kept details"
    what = {"certificate": "name, verified by its certificate", "pinned": "name, pinned by sandbox-ai",
            "lookup": "name, from a lookup question"}.get(details.get("verified"), kind)
    if details.get("why"):
        what += f" ({shown(details['why'], 200)})"
    text = f"{what}; added {shown(details.get('added', '?'), 40)}"
    if details.get("question"):
        text += f" from question {shown(details['question'], 40)}"
        if details.get("asked"):
            text += f" ({shown(details['asked'], 300)})"
    return text


def forget(project, value):
    """Remove VALUE (a host name or IP:PORT) from what PROJECT has approved "always"; True if it was there.
    Running sandboxes keep what they loaded at their start until they end (`sandbox-ai net revoke`)."""
    key = str(Path(project).expanduser().resolve())
    with approved_store() as data:
        entry = data.get(key)
        if not isinstance(entry, dict):
            return False
        found = False
        fields = ("hosts", "tcp", "blocked_hosts", "blocked_tcp")
        for field in fields:
            values = entry.get(field) if isinstance(entry.get(field), list) else []
            if value in values:
                entry[field] = [v for v in values if v != value]
                if isinstance(entry.get("details"), dict):
                    entry["details"].pop(detail_key(field, value), None)
                found = True
        if not any(entry.get(field) for field in fields):
            del data[key]
        return found


def mcp_runs(state_root=None):
    """{"mcp-NAME": record} of the MCP servers that can ask (an approvals directory in their state directory);
    a record names the directory as a run's does, so listing and answering work alike."""
    root = Path(state_root or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "mcp"
    try:
        servers = sorted(path for path in root.iterdir() if (path / "approvals").is_dir())
    except OSError:
        return {}
    return {f"mcp-{path.name}": {"directory": str(path), "run": f"mcp-{path.name}", "mcp": path.name}
            for path in servers}


def questions(runs=None, answered=False):
    """[(run, record, question)] of the running sandboxes and MCP servers; open ones only unless ANSWERED."""
    found = []
    for name, record in (runs if runs is not None else {**net().running(), **mcp_runs()}).items():
        directory = Path(record["directory"]) / "approvals"
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            if not ID.fullmatch(path.stem):
                continue
            try:
                question = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            done = path.with_suffix(".done")
            if done.exists():
                if not answered:
                    continue
                try:
                    question["result"] = json.loads(done.read_text())
                except (OSError, ValueError):
                    pass
            elif path.with_suffix(".answer").exists() and not answered:
                continue
            found.append((name, record, question))
    return found


def describe(question):
    """One line about QUESTION and who asks; everything the agent chose is escaped (see shown)."""
    return what(question) + because(question) + asked_by(question)


def always(question):
    """"; Allow always: ..." for a connection question: what an "always" answer would remember (the
    supervisor's verdict, see ApprovalBroker.remembers); "" for questions of older versions."""
    remembers = question.get("remembers")
    if not isinstance(remembers, dict) or not remembers.get("tcp"):
        return ""
    address = shown(remembers["tcp"], 60)
    name = shown(remembers.get("host"), 253)
    how = remembers.get("verified")
    if how == "certificate":
        return f"; Allow always: {name} (verified by its certificate)"
    if how == "pinned":
        return f"; Allow always: {name} (verified: sandbox-ai pinned it to this address)"
    if how == "on connection":
        return f"; Allow always: {name} if its certificate proves it on this connection, else {address} (address only)"
    why = remembers.get("why") or "the name is the agent's claim and could not be verified"
    return f"; Allow always: {address} (address only: {shown(why, 200)})"


def because(question):
    """"; asked because untrusted content from X entered this run at T" for a question a rule for tainted runs
    holds, else ""."""
    taint = question.get("taint")
    if not isinstance(taint, dict) or not taint.get("tainted"):
        return ""
    source = taint.get("source") if isinstance(taint.get("source"), dict) else {}
    return f"; asked because untrusted content from {shown(source.get('what', '?'), 200)} entered this run at " \
           f"{shown(taint.get('since', '?'), 40)}"


def asked_by(question):
    """", asked by NAME (pid N) in RUN" for a question that names the process behind it, else "". The PID is
    the one the agent's own tools show (inside Firejail); name and command line are the process's own
    choice, shown escaped."""
    process = question.get("process")
    if not isinstance(process, dict):
        return ""
    sandbox = question.get("sandbox") if isinstance(question.get("sandbox"), dict) else {}
    where = sandbox.get("run") or sandbox.get("pod")
    where = f" in {shown(where, 80)}" if where else ""
    if "unknown" in process:
        return f"; asked by a process not identified ({shown(process['unknown'], 120)}){where}"
    pid = process.get("ns_pid") or process.get("pid")
    exe = process.get("exe")
    return f"; asked by {shown(process.get('comm', '?'), 64)} (pid {shown(pid, 12)}" + \
        (f", {shown(exe, 200)}" if exe else "") + f"){where}"


def what(question):
    """What QUESTION asks for."""
    kind = question.get("kind")
    if kind == "connect":
        host, protocol = question.get("host"), question.get("protocol")
        text = f"connect to {shown(question.get('destination'))}"
        if protocol and protocol != "tcp":
            text += f" ({shown(protocol, 10)})"
        return text + (f", named {shown(host)} by the agent (not verified)" if host else "") + always(question)
    if kind == "dns":
        return f"look up {shown(question.get('name'))}"
    if kind == "push":
        refs = question.get("refs") if isinstance(question.get("refs"), list) else []
        return f"push {', '.join(shown(r, 200) for r in refs[:10])} to {shown(question.get('url'), 500)} " \
               "despite privacy warnings"
    if kind == "http":
        claim = question.get("host_header")
        return f"{shown(question.get('method'), 20)} {shown(question.get('url'), 500)} (rule " \
               f"{shown(question.get('rule'), 80)} of {shown(question.get('ruleset'), 120)})" + \
               (f"; its Host header names {shown(claim, 300)} (not verified): a server that routes by it may "
                f"pass the request on there" if claim else "")
    if kind == "mcp":
        return f"call the MCP tool {shown(question.get('tool'), 128)} of {shown(question.get('server'), 40)} with " \
               f"{shown(question.get('arguments'), 300)} (rule {shown(question.get('rule'), 80)} of " \
               f"{shown(question.get('ruleset'), 120)})"
    if kind == "device":
        nodes = question.get("nodes") if isinstance(question.get("nodes"), list) else []
        device = " ".join(shown(part, 120) for part in (
            question.get("usb") and f"usb {question['usb']}", question.get("product") and f"\"{question['product']}\"",
            question.get("serial") and f"serial {question['serial']}", question.get("port") and
            f"port {question['port']}") if part)
        return (f"{'re-attach' if question.get('replug') else 'attach'} the device "
                f"{', '.join(shown(n, 200) for n in nodes[:10]) or shown(question.get('value'))}"
                + (f" ({device})" if device else "") + f", given as {shown(question.get('value'), 300)} (rule "
                f"{shown(question.get('rule'), 80)} of {shown(question.get('ruleset'), 120)}; what a device reports "
                "about itself can be claimed by another)")
    if kind == "canary":  # canary.py: a decoy file was read, or a token of one left the sandbox
        return f"canary {shown(question.get('canary'), 40)}: {shown(question.get('summary'), 500)}"
    if kind == "tool":
        return f"let {shown(question.get('agent'), 20)} use its tool {shown(question.get('tool'), 128)} with " \
               f"{shown(question.get('arguments'), 300)} (rule {shown(question.get('rule'), 80)} of " \
               f"{shown(question.get('ruleset'), 120)})"
    if kind == "browser":
        url = question.get("url")
        host = url_host(url)
        return f"open {shown(url, 1000)} in your browser" + (f" (host {shown(host, 253)})" if host else "")
    return shown(kind, 40)


def url_host(url):
    """The host a browser opens for URL (http or https), or None. The authority ends at the first /, ?, # or
    backslash (a browser reads a backslash as a slash in these URLs: https://a.example\\.b.example/ opens
    a.example), after a user part, without a port; an IPv6 address in its brackets."""
    if not isinstance(url, str):
        return None
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        return None
    rest = rest.lstrip("/\\")  # as a browser does: https:///a.example and https://\\a.example open a.example
    authority = re.split(r"[/?#\\]", rest, maxsplit=1)[0]
    authority = authority.rpartition("@")[2]  # after a user part (URL refuses those: this is where it would go)
    if authority.startswith("["):
        host = authority[:authority.find("]") + 1] if "]" in authority else ""
    else:
        host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
    # As a browser reads it: percent-encoding decoded (https://github.com%2eevil.example/ opens
    # github.com.evil.example) and the ideographic and fullwidth full stops taken as dots.
    host = urllib.parse.unquote(host, errors="replace")
    host = re.sub("[\u3002\uff0e\uff61]", ".", host)
    return host.lower() or None


def question_times(question, now=None):
    """{"timeout", "seconds_left"} of an open QUESTION: computed here, on its host, so another machine's clock
    does not matter (the web and the app count down from seconds_left). Every question that is refused without
    an answer carries its own "timeout" (the sandbox's, a device's, a canary's); one without (a browser
    question: nothing in the sandbox waits for it, it stays open until answered or the run ends) has no
    deadline: both are None."""
    import calendar
    timeout = question.get("timeout")
    if question.get("kind") == "browser" or isinstance(timeout, bool) or \
            not (isinstance(timeout, (int, float)) and 0 < timeout <= 86400):
        return {"timeout": None, "seconds_left": None}
    try:
        asked = calendar.timegm(time.strptime(str(question.get("time")), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return {"timeout": timeout, "seconds_left": None}
    left = asked + timeout - (time.time() if now is None else now)
    return {"timeout": timeout, "seconds_left": max(0, int(left))}


def answer_cap(record, widest="always"):
    """The widest scope the policy (WIDEST) and the run's security mode (its run.json RECORD) allow."""
    caps = [widest if widest in SCOPES else "always"]
    mode = record.get("mode") if isinstance(record, dict) and isinstance(record.get("mode"), dict) else {}
    if mode.get("approval_scope") in SCOPES:
        caps.append(mode["approval_scope"])
    return min(caps, key=SCOPES.index)


# --ask allow: the "by" of an answer the launcher gave by itself (in the .answer file); the sandbox's readers of the
# answer (runtime.ApprovalBroker, devices.read_answer) record AUTO_REASON (AUTO_REFUSED for a refusal) as its reason.
AUTO_BY = "ask-allow"
AUTO_REASON = "allowed automatically (--ask allow)"
AUTO_REFUSED = "refused automatically: --ask allow could not allow it"
AUTO_SKIPPED = ("canary",)  # an alarm (the sandbox is paused until you answer), not a question: always yours


DNS_ONCE = ("an allowed name stays pinned for the run (the agent's next try reaches it), and this run approves at "
            "most once (its security mode or the administrator's policy): deny it, or allow the name at the next "
            "start (--allow-host) if you mean it")


def widest_answer(question, record, widest="always"):
    """The widest scope an "allow" of QUESTION keeps, as answer() then applies it: the policy's, the run's security
    mode's, and the kind's own (pushes, requests, tool calls and devices at most for the run). A name is pinned
    for the run at least: where only "once" is allowed it cannot be allowed at all (None)."""
    cap = answer_cap(record, widest)
    if question.get("kind") in ("push", "http", "mcp", "tool", "device"):
        cap = min((cap, "run"), key=SCOPES.index)
    if question.get("kind") == "dns" and cap == "once":
        return None
    return cap


def origin(record):
    """Which sandbox asks: run name, workspace (~ for HOME) and session, from its run.json RECORD."""
    if not isinstance(record, dict):
        return ""
    workspace = str(record.get("workspace") or "")
    home = str(Path.home())
    if workspace == home or workspace.startswith(home + "/"):
        workspace = "~" + workspace[len(home):]
    parts = [shown(workspace, 200)] if workspace else []
    if record.get("name"):
        parts.append(f"name {shown(record.get('name'), 80)}")
    elif record.get("session"):
        parts.append(f"session {shown(record.get('session'), 80)}")
    name = shown(record.get("run"), 80) if record.get("run") else ""
    return f"{name} ({', '.join(parts)})" if name and parts else name or ", ".join(parts)


def run_record(approvals_dir):
    """run.json next to APPROVALS_DIR (the run's control directory), or {}."""
    try:
        record = json.loads((Path(approvals_dir).parent / "run.json").read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def resolve(name):
    infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def public_addresses(name, addresses):
    """The public ones of ADDRESSES; refuses when there are none (a name the agent controls may resolve to
    this host, the LAN or a VPN: those are allowed only explicitly, with sandbox-ai net allow)."""
    # Global and no NAT64 (a local translator turns those into any IPv4 address: the LAN, this host).
    nat64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
    public = [a for a in addresses if (lambda ip: ip.is_global and not (ip.version == 6 and any(ip in n for n in nat64))
                                       )(ipaddress.ip_address(a.split("%")[0]))]
    if not public:
        raise ValueError(f"{shown(name)} resolves only to non-public addresses ({', '.join(addresses) or 'none'}); "
                         "allow them explicitly with sandbox-ai net allow --allow-tcp IP:PORT if you mean it")
    return public


def wait_done(path, seconds):
    """The supervisor's result for an answer (its .done file), or {} after SECONDS."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            result = json.loads(path.read_text())
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                return {}
            time.sleep(0.1)


def answer(ident, decision, scope="once", runs=None, resolver=None, opener=None, out=print, wait=10.0,
           no_open=False, auto=None):
    """Write the answer to question IDENT; side effects the supervisor cannot do itself happen here:
    a name is resolved and pinned, a browser is opened, an "always" is remembered for the project. NO_OPEN (a browser
    question only): allowed without opening the page on this host (the human opened it where they are: the web
    interface or the app on another machine). AUTO: given by --ask allow, not by a human (auto_answer): True, or
    the reason it could not allow (with a deny); the answer says so (AUTO_BY) and is recorded with AUTO_REASON."""
    if decision not in ("allow", "deny") or scope not in ("once", "run", "always"):
        raise ValueError("decision allow|deny, scope once|run|always")
    if no_open and decision != "allow":
        raise ValueError("--no-open goes with allow (of a browser question)")
    widest = policy_scope()
    if decision == "allow" and SCOPES.index(scope) > SCOPES.index(widest):
        raise ValueError(f"the administrator's policy allows answers up to --scope {widest}")
    matches = [entry for entry in questions(runs) if entry[2].get("id") == ident]
    if len(matches) > 1:
        raise ValueError(f"{shown(ident)} names questions of several sandboxes "
                         f"({', '.join(shown(m[0]) for m in matches)}); answer it in the app or wait")
    for name, record, question in matches:
        kind = question.get("kind")
        notes = []
        if no_open and kind != "browser":
            raise ValueError(f"--no-open is for browser questions only; {shown(ident)} is a {shown(kind, 20)} question")
        # The run's security mode (run.json) caps answers too; its supervisor holds the same cap.
        mode = record.get("mode") if isinstance(record, dict) and isinstance(record.get("mode"), dict) else {}
        cap = mode.get("approval_scope", "always")
        if decision == "allow" and kind == "dns" and widest_answer(question, record, widest) is None:
            raise ValueError(f"{shown(question.get('name'))}: {DNS_ONCE}")
        if decision == "allow" and cap in SCOPES and SCOPES.index(scope) > SCOPES.index(cap):
            scope, notes = cap, [f"the security mode {shown(mode.get('name'), 20)} of this run approves at most "
                                 f"for {'the run' if cap == 'run' else 'once'}"]
        if decision == "allow" and kind == "push" and scope == "always":
            scope, notes = "run", ["pushes are approved at most for the run"]
        if decision == "allow" and kind in ("http", "mcp", "tool") and scope == "always":
            scope, notes = "run", ["requests and tool calls are approved at most for the run (an MCP server's run "
                                   "lasts until it stops); to allow them always, write a rule"]
        if decision == "allow" and kind == "device" and scope == "always":
            scope, notes = "run", ["a device is approved at most for the run (with run, a replug is not asked "
                                   "about again); to allow it always, write a device rule"]
        if decision == "allow" and kind == "dns" and scope == "once":
            scope, notes = "run", ["a name stays pinned for the run"]
        later = None  # remembered for the project once the sandbox has applied the answer
        if decision == "deny" and scope == "always":
            # Blocked for this project: refused at once at every later start; in this run it counts for the run.
            scope = "run"
            if kind == "dns" and isinstance(question.get("name"), str) and HOST.fullmatch(question["name"]):
                later = {"host": question["name"], "blocked": True}
            elif kind == "connect":
                address, port = str(question.get("address", "")), int(question.get("port", 0))
                later = {"tcp": f"[{address}]:{port}" if ":" in address else f"{address}:{port}", "blocked": True}
            else:
                notes.append("only names and addresses are blocked always; this one is denied for the run")
        reply = {"decision": decision, "scope": scope}
        if auto:
            reply["by"] = AUTO_BY
            if isinstance(auto, str):
                reply["why"] = shown(auto, 300)
        if decision == "allow" and kind == "dns":
            host = question.get("name")
            if not isinstance(host, str) or not HOST.fullmatch(host):
                raise ValueError(f"not a host name: {shown(host)}")
            if canary_in_name(record, host):  # looked up here, the token would leave through this host's DNS
                raise ValueError(f"{shown(host)} carries a canary token of the run: it is never looked up")
            # A run with --vpn: through the VPN's resolvers (its container), never past it.
            resolved = (resolver or net().vpn_resolver(record) or resolve)(host)
            addresses = public_addresses(host, resolved)
            if len(addresses) < len(resolved):
                notes.append("not opened (not public): " + ", ".join(a for a in resolved if a not in addresses))
            reply["addresses"] = addresses
            net().update_hosts(record, {host: addresses})
            notes.append(f"{host} -> {', '.join(addresses)}")
            if scope == "always":
                later = {"host": host}
        elif decision == "allow" and kind == "connect" and scope == "always":
            # The address, unless the sandbox proves the name the agent sent (its .done says, see below).
            address, port = str(question.get("address", "")), int(question.get("port", 0))
            later = {"tcp": f"[{address}]:{port}" if ":" in address else f"{address}:{port}"}
            wait = max(wait, VERIFY_WAIT)
        elif decision == "allow" and kind == "browser":
            url = question.get("url", "")
            if not isinstance(url, str) or not URL.fullmatch(url):
                raise ValueError("only http(s) URLs are opened")
            if no_open:
                notes.append("not opened on this host (--no-open): open it where you are")
            elif net().tor_record(record):
                # Your browser would reach the agent's URL from your own address, past the run's Tor.
                notes.append("not opened: this run goes through Tor, and your browser would open the URL from your "
                             "own address; open it in Tor Browser if you mean to")
            elif internet_cut(net().cut_state(record["directory"])):
                # The run's kill switch cuts the internet: your browser would reach it for the agent.
                notes.append(f"not opened: the run's network is cut (sandbox-ai net cut: "
                             f"{net().cut_state(record['directory'])}); lift the cut first if you mean to open it")
            else:
                (opener or open_browser)(url)
        path = Path(record["directory"]) / "approvals" / f"{ident}.answer"
        temporary = path.with_name(f".{ident}.answer")
        temporary.write_text(json.dumps(reply))
        os.replace(temporary, path)
        if kind == "browser":  # nobody waits for these in the sandbox
            path.with_name(f"{ident}.done").write_text(json.dumps({**reply, "reason": answered_reason(reply)}))
        if later:
            # Only what the sandbox accepted is remembered: a question that timed out meanwhile, or an answer the
            # supervisor could not apply, must not open the next start.
            result = wait_done(path.with_suffix(".done"), wait)
            details = {"question": ident, "kind": kind, "asked": what({**question, "remembers": None})[:300]}
            if kind == "dns":
                details["verified"] = "lookup"
            if kind == "connect" and not later.get("blocked"):
                later, details, note = proven(question, result, later, details)
                notes += [note] if note else []
            if result.get("decision") == ("deny" if later.get("blocked") else "allow"):
                remember(record["workspace"], **later, details=details)
            else:
                notes.append("not remembered for the project: the sandbox did not apply it ("
                             f"{shown(result.get('reason', 'no reply'), 200)})")
        out(f"{shown(name)}: {decision} ({scope}{', automatically: --ask allow' if auto else ''}): {describe(question)}"
            + "".join(f"\n  {shown(note, 1000)}" for note in notes))
        return reply
    raise ValueError(f"no open question {ident}; see sandbox-ai approvals list")


def answered_reason(reply):
    """The reason an answer REPLY (an .answer file's content) is recorded with: "answered" for a human's, AUTO_REASON
    (AUTO_REFUSED and why) for one --ask allow gave."""
    if not isinstance(reply, dict) or reply.get("by") != AUTO_BY:
        return "answered"
    if reply.get("decision") == "allow":
        return AUTO_REASON
    return AUTO_REFUSED + (f" ({shown(reply['why'], 300)})" if reply.get("why") else "")


def auto_scope(question, record):
    """The scope an automatic "allow" of QUESTION has: the run, or narrower where the administrator's policy or the
    run's security mode caps answers (widest_answer); None where no answer may allow it (a lookup in a run that
    approves only once)."""
    widest = widest_answer(question, record, policy_scope())
    return None if widest is None else min(("run", widest), key=SCOPES.index)


def auto_answer(question, record, answer_with=None, out=None):
    """--ask allow: answer QUESTION of the run RECORD (its run.json with "directory") "allow" at once, for the run
    (auto_scope; never "always": nothing is remembered for later starts), applied and recorded as a human's answer
    is (answer(): a name is resolved and pinned, a URL opened), with AUTO_REASON. Not answered (None): a canary's
    alarm (AUTO_SKIPPED) and a browser request whose URL carries a canary token of the run: they stay with the
    human. Refused for the run at once, with why (AUTO_REFUSED): what no answer may allow (a lookup in a run that
    approves only once, a name with a canary token, outside the administrator's policy or without a public
    address)."""
    answer_with = answer_with or answer
    kind, ident = question.get("kind"), question.get("id")
    runs = {record.get("run", "run"): record}
    if kind in AUTO_SKIPPED or (kind == "browser" and canary_in_name(record, question.get("url", ""))):
        return None
    try:
        scope = auto_scope(question, record)
        if scope is None:
            raise ValueError(DNS_ONCE)
        if kind == "dns" and isinstance(question.get("name"), str) and not policy_allows_host(question["name"]):
            raise ValueError(f"{shown(question['name'])} lies outside the administrator's policy (allow_hosts)")
        return answer_with(ident, "allow", scope, runs=runs, out=out or (lambda line: None), auto=True)
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError, ValueError):
            answer_with(ident, "deny", "run", runs=runs, out=lambda line: None, auto=str(exc) or "refused")
        return {"decision": "deny", "reason": f"{AUTO_REFUSED} ({shown(str(exc), 300)})"}


VERIFY_WAIT = 30  # seconds: an "always" for a name waits for the sandbox's certificate check (15 s at most)


def proven(question, result, later, details):
    """(what to remember, its details, a note) for "Allow always" on connection QUESTION: the name when the
    sandbox's answer (RESULT, its .done) says it proved the name the question offered, by a certificate or its
    own pin, and the administrator's policy allows the name; else the address LATER, with why."""
    offered = question.get("remembers") if isinstance(question.get("remembers"), dict) else {}
    remember = result.get("remember") if isinstance(result.get("remember"), dict) else {}
    name = remember.get("host")
    proved = remember.get("verified") in ("certificate", "pinned")
    details = {**details, "destination": later["tcp"]}
    if proved and isinstance(name, str) and HOST.fullmatch(name) and name == offered.get("host"):
        if not policy_allows_host(name):
            return later, {**details, "why": "the administrator's policy does not list the name"}, \
                f"remembered as the address: {name} is not among the hosts the administrator's policy allows"
        return {"host": name}, {**details, "verified": remember["verified"]}, \
            f"remembered as the name {name} ({'verified by its certificate' if remember['verified'] == 'certificate' else 'pinned by sandbox-ai'}): later starts resolve it"
    why = remember.get("why") or offered.get("why") or "the name could not be verified"
    return later, {**details, "why": why}, f"remembered as the address {later['tcp']}: {why}"


def launcher_module():
    spec = importlib.util.spec_from_file_location("agent_sandbox_for_approvals", HERE / "agent_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy_allows_host(name):
    """NAME lies inside the administrator's ceiling (allow_hosts, when allow_within is set), or there is none."""
    launcher = launcher_module()
    policy = launcher.load_policy()
    return launcher.within_ceiling(argparse.Namespace(policy={key: policy[key] for key in ("allow_within", "allow_hosts")
                                                              if key in policy}), host=name)


def open_browser(url):
    """Open URL in the host's browser, never through a shell."""
    # macOS: open(1) (on Debian "open" is openvt: only on a Mac, and only its own).
    programs = ("/usr/bin/open",) if sys.platform == "darwin" else ("xdg-open", "sensible-browser")
    for program in programs:
        if shutil.which(program):
            subprocess.Popen([program, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    raise ValueError("no program to open a browser (xdg-open)")


QUESTION_ACTIONS = (("once", "allow", "once", "Allow"), ("always", "allow", "always", "Allow always"),
                    ("deny", "deny", "run", "Block"), ("never", "deny", "always", "Block always"))
BROWSER_ACTIONS = (("open", "allow", "once", "Open"), ("ignore", "deny", "once", "Ignore"))
_actions_supported = None


def notify_actions():
    """notify-send can show buttons and report the one clicked (libnotify 0.7.9 and later)."""
    global _actions_supported
    if _actions_supported is None:
        try:
            text = subprocess.run(["notify-send", "--help"], capture_output=True, text=True, timeout=5,
                                  stdin=subprocess.DEVNULL).stdout if shutil.which("notify-send") else ""
        except (OSError, subprocess.SubprocessError):
            text = ""
        _actions_supported = "--action" in text and "--wait" in text
    return _actions_supported


QUESTION_NAMES = {("allow", "once"): "once", ("allow", "run"): "run", ("allow", "always"): "always",
                  ("deny", "run"): "deny", ("deny", "always"): "never"}


def desktop_question(title, text, ident, actions=QUESTION_ACTIONS, answer_with=None, seconds=60, runner=None,
                     question=None, record=None):
    """A notification with buttons; the button clicked answers question IDENT as `sandbox-ai approvals` would.
    With QUESTION (and its run's RECORD) the buttons are its choices: Allow once, Allow for this run, Allow always,
    Block, Block always, those not possible named with why in the text; without, ACTIONS, "Allow always" left out
    when the administrator's policy caps the scope. A question or a browser request is not shown while the desktop
    app or the web interface watches the questions (watched): they ask, one prompt per question (a canary's alarm
    is shown all the same). Runs until clicked or expired (one thread per question)."""
    import html
    if not shows(alarm=actions not in (QUESTION_ACTIONS, BROWSER_ACTIONS)):  # a canary's buttons: an alarm
        return None
    notes = []
    if question is not None and actions is QUESTION_ACTIONS:
        offered = []
        for choice in choices(question, record):
            if choice["enabled"]:
                offered.append((QUESTION_NAMES[choice["decision"], choice["scope"]], choice["decision"],
                                choice["scope"], choice["label"]))
            elif choice["why"]:
                notes.append(f"{choice['label']}: {choice['why']}")
    elif not any(scope == "always" for _, decision, scope, _ in actions if decision == "allow") or \
            policy_scope() == "always":
        offered = actions
    else:
        offered = [action for action in actions if not (action[1] == "allow" and action[2] == "always")]
    text = text[:1000] + "".join(f"\n{note[:300]}" for note in notes)
    text = html.escape("".join(c if c == "\n" or c.isprintable() else "?" for c in text), quote=False)
    command = ["notify-send", "--app-name=sandbox-ai", "--urgency=critical", "--wait",
               f"--expire-time={seconds * 1000}", *[f"--action={name}={label}" for name, _, _, label in offered],
               title, text]
    try:
        chosen = (runner or subprocess.run)(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                            timeout=seconds + 5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    for name, decision, scope, _ in offered:
        if chosen == name:
            with contextlib.suppress(ValueError, OSError):
                (answer_with or answer)(ident, decision, scope, out=lambda line: None)
            return name
    return None


# Names answered by themselves below one wildcard in one run: more go to the human as any lookup does (the agent
# chooses the names: without a cap it could grow the pins, the firewall sets and the host's lookups without end).
WILDCARD_NAMES = 256
CANARY_CONFIG = "canary.json"  # in the run's control directory: the canaries' name forms (canary.py, runtime.py)


def canary_in_name(record, name):
    """NAME carries a canary token of the run (its control directory's canary.json, as the DNS stub matches: in
    the name or across its labels), or the list cannot be read (then every name counts as carrying one)."""
    return carries_canary(canary_names(record), name)


def canary_names(record):
    """The forms canary_in_name looks for in a name, from the run RECORD's canary.json (read once for many names: the
    Blocked list's); None when the list cannot be read."""
    try:
        config = json.loads((Path(record["directory"]) / CANARY_CONFIG).read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    forms = config.get("names") if isinstance(config, dict) else None
    if not isinstance(forms, list):
        return None
    return {entry[0].lower() for entry in forms
            if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str) and len(entry[0]) >= 8}


def carries_canary(needles, name):
    """NAME holds one of NEEDLES (canary_names; None: every name counts as carrying one)."""
    if needles is None:
        return True
    name = str(name).lower().rstrip(".")
    return any(needle in name or needle in name.replace(".", "") for needle in needles)


@contextlib.contextmanager
def wildcard_lock(record):
    """The run's lock for names opened below its wildcards: counting them and opening one more is one step (two
    answers at once must not both pass the cap). Held around answer(), which takes the names lock (net.hosts_lock)
    inside it, never the other way round."""
    fd = os.open(Path(record["directory"]) / "wildcard-names.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def under_wildcard(question, record):
    """The *.DOMAIN of the run (--allow-host at the start or `sandbox-ai net allow`) that QUESTION's name lies
    below, or None: only lookups (kind dns) of a proper host name without a canary token of the run, and only while
    fewer than WILDCARD_NAMES names below it were opened in this run."""
    name = question.get("name") if question.get("kind") == "dns" else None
    if not isinstance(name, str) or not HOST.fullmatch(name):
        return None
    if question.get("canary") or canary_in_name(record, name):
        return None  # never looked up on the host by itself: the token would leave through its DNS
    for wildcard in net().run_wildcards(record):
        if name.endswith(wildcard[1:]):
            try:
                opened = [host for host in net().remembered(record) if str(host).endswith(wildcard[1:])]
            except (OSError, ValueError, KeyError):
                return None  # not countable: asked of the human, never answered without a limit
            return wildcard if len(opened) < WILDCARD_NAMES else None
    return None


def answer_wildcard(question, record, wildcard, answer_with=None, out=None, ask_human=None):
    """Answer the lookup QUESTION below WILDCARD on the host, at once: allowed for the run when the name lies inside
    the administrator's ceiling and resolves to public addresses (answer() resolves, pins and checks them); denied
    for the run otherwise, so the held lookup does not wait for nobody."""
    answer_with = answer_with or answer
    runs = {record.get("run", "run"): record}
    ident, name = question.get("id"), question.get("name")
    try:
        with wildcard_lock(record):
            # Counted again under the lock, and the name opened before it is let go: the cap holds for answers
            # that arrive together. Past it (or a canary token found now), the human is asked, as for any lookup.
            if under_wildcard(question, record) != wildcard:
                (ask_human or desktop_notification)(
                    "sandbox-ai asks", f"{describe(question)}\nsandbox-ai approvals allow {shown(ident)}")
                return None
            if not policy_allows_host(name):
                raise ValueError(f"{shown(name)} lies outside the administrator's policy (allow_hosts)")
            return answer_with(ident, "allow", "run", runs=runs, out=out or (lambda line: None))
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError, ValueError):
            answer_with(ident, "deny", "run", runs=runs, out=lambda line: None)
        return {"decision": "deny", "reason": f"{wildcard}: {exc}"}


def watch(directory, stop, notify=None, interval=1.0, burst=3, buttons=None, wildcards=answer_wildcard, auto=False,
          autos=None):
    """Notify (notify-send) about every new question in DIRECTORY until STOP is set (the launcher runs this
    while its sandbox runs: the agent's terminal is busy with the agent). More than BURST new questions at
    once give one notification; browser requests are announced by serve_browser. AUTO (--ask allow, from the
    launcher's own options: nothing in the sandbox can set it): every question is answered by AUTOS (auto_answer)
    instead, and nobody is notified; a canary's alarm is left to the human (it notifies itself). Questions seen
    while the desktop app or the web interface watches (watched) get no notification here: their prompt is the
    one."""
    autos = autos or auto_answer
    if buttons is None and notify is None and notify_actions():
        buttons = desktop_question  # buttons in the notification answer it
    notify = notify or desktop_notification
    seen = set()
    asker = origin(run_record(directory))
    title = f"sandbox-ai asks: {asker}" if asker else "sandbox-ai asks"
    while not stop.wait(interval):
        try:
            files = sorted(Path(directory).glob("*.json"))
        except OSError:
            continue
        fresh = []
        for path in files:
            if path.name in seen or not ID.fullmatch(path.stem) or path.with_suffix(".done").exists():
                continue
            seen.add(path.name)
            try:
                question = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(question, dict):
                continue
            record = {**run_record(directory), "directory": str(Path(directory).parent)}
            wildcard = under_wildcard(question, record)
            if wildcard is not None:  # --allow-host *.DOMAIN: answered here, asked of nobody
                # Past its cap the lookup goes to the human, which --ask allow answers as well.
                extra = {"ask_human": lambda title, text, q=question, r=record: autos(q, r)} if auto else {}
                threading.Thread(target=wildcards, args=(question, record, wildcard), kwargs=extra,
                                 daemon=True).start()
                continue
            if auto and question.get("kind") not in ("browser", *AUTO_SKIPPED):  # browser ones: serve_browser
                threading.Thread(target=autos, args=(question, record), daemon=True).start()
                continue
            if question.get("kind") not in ("browser", "canary"):  # they notify themselves
                fresh.append(question)
        seen &= {path.name for path in files}
        if fresh and watched():
            continue  # the app or the web interface asks: one prompt per question
        if len(fresh) > burst:
            notify(title, f"{len(fresh)} new questions\nsandbox-ai approvals list")
            continue
        for item in fresh:
            if buttons is not None:
                threading.Thread(target=buttons, args=(title, describe(item), item.get("id")), daemon=True,
                                 kwargs={"question": item, "record": run_record(directory)}).start()
            else:
                notify(title, f"{describe(item)}\nsandbox-ai approvals allow {shown(item.get('id'))}")


DESKTOP_LEVELS = ("all", "alarms", "none")  # "desktop_notifications" in ~/.config/sandbox-ai/defaults.json


def desktop_level(path=None):
    """What the launchers show on the desktop: "all" (the default, also for a file that cannot be read), "alarms"
    (only a canary's alarm) or "none" ("desktop_notifications" in the defaults file, sandbox-ai defaults set)."""
    path = Path(path) if path else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / \
        "sandbox-ai" / "defaults.json"
    try:
        value = json.loads(path.read_text()).get("desktop_notifications", "all")
    except (OSError, ValueError, AttributeError, RecursionError):
        return "all"
    return value if value in DESKTOP_LEVELS else "all"


def shows(alarm=False):
    """A desktop notification of the launcher is shown now: its level allows it (desktop_level) and, but for an
    ALARM, neither the app nor the web interface watches (they show it, or keep it on their pages)."""
    level = desktop_level()
    if level == "none" or level == "alarms" and not alarm:
        return False
    return alarm or not watched()


def desktop_notification(title, text, urgency="critical", alarm=False):
    """notify-send; TEXT names what the agent asked for, so markup in it is escaped (some notification
    daemons render <b>, <a> or <img> in the body) and control characters are dropped. URGENCY: critical (a
    question, an error) or normal (a warning, e.g. a safeguard that refused something).
    While the desktop app or the web interface watches (watched), nothing is shown: they show it themselves (a
    safeguard, a limit reached, a question) or keep it on their pages - one place, not two. ALARM (a canary that
    went off) is shown all the same."""
    import html
    if not shows(alarm):
        return None
    if sys.platform == "darwin":
        return mac_notification(title, text)
    text = html.escape("".join(c if c == "\n" or c.isprintable() else "?" for c in text[:1000]), quote=False)
    urgency = urgency if urgency in ("low", "normal", "critical") else "critical"
    if shutil.which("notify-send"):
        # Never an error: a notify-send that hangs (a session bus without a notification daemon) would otherwise
        # end the thread that called it, the watch of the questions or the browser listener, for the whole run.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["notify-send", "--app-name=sandbox-ai", f"--urgency={urgency}", title, text],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           check=False)


# macOS: a notification through AppleScript. The title and text are the script's arguments (on run argv), never put
# into its source: what an agent asked for cannot end the string and run AppleScript of its own.
OSASCRIPT_NOTIFICATION = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


def mac_notification(title, text, runner=None):
    """macOS: TITLE and TEXT as a notification of the Notification Center (osascript), control characters dropped."""
    def clean(value, limit):
        return "".join(c if c == "\n" or c.isprintable() else "?" for c in str(value)[:limit])
    command = ["/usr/bin/osascript", *(part for line in OSASCRIPT_NOTIFICATION for part in ("-e", line)),
               "--", clean(title, 200), clean(text, 1000)]
    with contextlib.suppress(OSError, subprocess.SubprocessError):  # never an error, as desktop_notification
        (runner or subprocess.run)(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)


def serve_browser(socket_path, approvals_dir, stop, notify=None, limit=20, listen=None, auto=False, autos=None):
    """Take the sandbox's requests to open a URL in your browser (its xdg-open) on SOCKET_PATH and turn each
    into a question in APPROVALS_DIR; at most LIMIT open at a time. Runs on the host while the sandbox runs. AUTO
    (--ask allow): each is answered by AUTOS (auto_answer) at once, the URL opened without a question (one that
    carries a canary token of the run is still asked about)."""
    import importlib.util
    import itertools
    if listen is None:
        spec = importlib.util.spec_from_file_location("runtime_for_browser", HERE / "runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        listen = runtime.listen_unix
    notify = notify or desktop_notification
    server = listen(socket_path)
    server.settimeout(0.5)
    counter = itertools.count(1)
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            try:
                line = read_line(connection, REQUEST_LIMIT, deadline=3.0)
            except (OSError, ValueError) as exc:
                reply = {"ok": False, "message": f"sandbox-ai: {str(exc)[:200]}"}
            else:
                reply = browser_reply(line, approvals_dir, counter, notify, limit, auto, autos)
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(reply) + "\n").encode())
    server.close()


def serve_browser_files(directory, approvals_dir, stop, notify=None, limit=20, auto=False, autos=None, interval=0.3):
    """macOS: the sandbox's requests to open a URL as files (DIRECTORY/ID.request, written by its supervisor in podman
    machine's VM: a Unix socket does not cross the VM's shared folders), each answered as serve_browser answers one
    (DIRECTORY/ID.reply). DIRECTORY lies in the run's control directory, which the agent cannot see."""
    import itertools
    notify = notify or desktop_notification
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    counter = itertools.count(1)
    while not stop.is_set():
        for path in sorted(directory.glob("*.request"))[:limit]:
            ident = path.name[:-len(".request")]
            try:
                with open(path, "rb") as stream:
                    data = stream.read(REQUEST_LIMIT + 1)
                path.unlink()
            except OSError:
                continue
            if len(data) > REQUEST_LIMIT:
                reply = {"ok": False, "message": "sandbox-ai: request too long"}
            else:
                reply = browser_reply(data.split(b"\n", 1)[0].decode("utf-8", "replace"), approvals_dir, counter,
                                      notify, limit, auto, autos)
            target = directory / f"{ident}.reply"
            with contextlib.suppress(OSError):
                temporary = directory / f".{ident}.reply"
                temporary.write_text(json.dumps(reply) + "\n")
                os.replace(temporary, target)
        stop.wait(interval)


def browser_reply(line, approvals_dir, counter, notify, limit=20, auto=False, autos=None):
    """The answer to one request LINE ({"url": ...}) of the sandbox's xdg-open: the question it became in
    APPROVALS_DIR (announced with NOTIFY), or why not."""
    try:
        request = json.loads(line or "{}")
        url = request.get("url", "") if isinstance(request, dict) else ""
        if not isinstance(url, str) or not URL.fullmatch(url):
            reply = {"ok": False, "message": "sandbox-ai: only http(s) URLs can be opened"}
        elif len([p for p in Path(approvals_dir).glob("*.json") if not p.with_suffix(".done").exists()]) >= limit:
            reply = {"ok": False, "message": "sandbox-ai: too many open questions; open the URL yourself"}
        else:
            ident = f"{int(time.time())}-{os.getpid()}-{next(counter)}"
            question = {"id": ident, "kind": "browser", "url": url,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            path = Path(approvals_dir) / f"{ident}.json"
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.with_name(f".{ident}.json").write_text(json.dumps(question))
            os.replace(path.with_name(f".{ident}.json"), path)
            asker = origin(run_record(approvals_dir))
            done = None
            if auto:
                record = {**run_record(approvals_dir), "directory": str(Path(approvals_dir).parent)}
                done = (autos or auto_answer)(question, record)
            if done is not None:
                reply = {"ok": done.get("decision") == "allow",
                         "message": f"sandbox-ai: opened in the human's browser without a question (--ask "
                                    f"allow; question {ident})" if done.get("decision") == "allow" else
                                    f"sandbox-ai: not opened ({shown(done.get('reason', 'refused'), 300)})"}
            elif notify is desktop_notification and notify_actions():
                threading.Thread(target=desktop_question, daemon=True, args=(
                    f"sandbox-ai: open a URL? ({asker})" if asker else "sandbox-ai: open a URL?",
                    url[:300], ident, BROWSER_ACTIONS)).start()
            else:
                notify(f"sandbox-ai: open a URL? ({asker})" if asker else "sandbox-ai: open a URL?",
                       f"{url[:300]}\nsandbox-ai approvals allow {ident}")
            if done is None:
                reply = {"ok": True, "message": "sandbox-ai: asked the human to open it in their browser "
                                                f"(question {ident}); nothing opens without their answer"}
    except (OSError, ValueError, RecursionError) as exc:
        reply = {"ok": False, "message": f"sandbox-ai: {str(exc)[:200]}"}
    return reply


def browser_fields(url):
    """What list --json says about a browser question's URL, so the page may be opened where the human is (the
    web, the app on another host): "url" (whole, escaped) only when it is one an "allow" opens (URL: http(s),
    printable ASCII, no user part), else "url_refused"; "url_host", the host it opens (shown as the real
    destination); "url_truncated" when it had to be cut."""
    if not isinstance(url, str):
        return {"url_refused": "no URL"}
    if not URL.fullmatch(url):
        return {"url_refused": "not an http(s) URL of printable ASCII without a user part (it is never opened)",
                **({"url_host": shown(url_host(url), 253)} if url_host(url) else {})}
    host = url_host(url)
    return {"url": shown(url, REQUEST_LIMIT), **({"url_truncated": True} if len(url) > REQUEST_LIMIT else {}),
            "url_host": shown(host, 253) if host else None}


def internet_cut(cut):
    """The kill switch CUT (net.cut_state: a mode, or the modes that hold together, "api+remote") cuts the internet:
    any of them but local does - also where a local cut holds beside it."""
    return bool(cut) and any(mode in ("remote", "api", "all") for mode in str(cut).split("+"))


def read_line(connection, limit, deadline):
    """One line of at most LIMIT bytes, read within DEADLINE seconds in all (a slow sender cannot hold the
    listener)."""
    end, data = time.monotonic() + deadline, b""
    while b"\n" not in data:
        left = end - time.monotonic()
        if left <= 0:
            raise ValueError("request too slow")
        connection.settimeout(left)
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("request too long")
    return data.split(b"\n", 1)[0].decode("utf-8", "replace")


# --- what a running sandbox refused: the Blocked list ------------------------------------------------------------
#
# `sandbox-ai approvals blocked [RUN]` and the app's Approvals page (Blocked): every connection, lookup, request and
# tool call a running sandbox (and its MCP servers and helpers, each a sandbox of its own) had refused, newest first,
# with why - not allowed by the network options, a ruleset's rule, a question nobody answered, your own deny, the
# security mode, the administrator's policy, the kill switch, a block "always" - and the process that tried. Read
# from the logs the run writes anyway, bounded: the newest BLOCKED_TAIL bytes of each log, the newest
# BLOCKED_QUESTIONS questions; never a whole file. What may be done about one entry ("actions") is said with it:
#   allow-run     for the run: sandbox-ai net allow (a destination) or, for a question, approvals allow ID --scope run
#   ask-again     forget the decision the run remembered (approvals ask-again ID): its next request is asked again
#   deny-run      approvals deny ID: refused for the rest of the run without a question
#   allow-always  for the project: net allow ... --keep (a destination) or an exception rule (rule_exception)

BLOCKED_TAIL = 4 << 20       # bytes read from the end of each log
BLOCKED_LIMIT = 300          # entries at most (equal ones counted as one)
BLOCKED_QUESTIONS = 400      # the newest questions of a run read
BLOCKED_KINDS = ("network", "rule", "unanswered", "denied", "mode", "policy", "cut", "blocked", "taint", "other")
REDECIDED = ("http", "mcp", "tool", "push", "connect")  # questions the supervisor's control op decide takes
CUT_MARK = "cut by the kill switch"


_jsonltail = None


def jsonltail():
    """sandbox/jsonltail.py (the newest lines of a log, read from its end), loaded once."""
    global _jsonltail
    if _jsonltail is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_jsonltail_for_approvals", HERE / "jsonltail.py")
        with LOADING:
            if _jsonltail is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _jsonltail = loaded
    return _jsonltail


def tail_records(path, keep=None, tail=BLOCKED_TAIL, stop=None, limit=BLOCKED_LIMIT * 4):
    """The JSON objects of the log PATH that KEEP wants, newest first, from its newest TAIL bytes only (a log of the
    project holds every run: never read whole); reading ends early once STOP(record) says the rest is older. A line
    being written, a damaged line and a link are skipped."""
    tails = jsonltail()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    found = []
    try:
        info = os.fstat(fd)
        if not stat_regular(info):
            return []
        end = tails.complete_end(fd, info.st_size)
        read = 0
        for line in tails.lines_from_end(fd, end, chunk=min(tails.CHUNK, max(4096, tail)), line_limit=1 << 20):
            read += len(line) + 1
            if read > tail:
                break
            try:
                record = tails.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            if stop is not None and stop(record):
                break
            if keep is None or keep(record):
                found.append(record)
                if len(found) >= limit:
                    break
    finally:
        os.close(fd)
    return found


def stat_regular(info):
    import stat as stat_module
    return stat_module.S_ISREG(info.st_mode)


def process_text(process):
    """NAME (pid N, PROGRAM) of a log's "process", escaped; "" when the log names none."""
    if not isinstance(process, dict):
        return ""
    if "unknown" in process:
        return f"not identified ({shown(process['unknown'], 120)})"
    pid = process.get("ns_pid") or process.get("pid")
    exe = process.get("exe")
    return f"{shown(process.get('comm', '?'), 64)} (pid {shown(pid, 12)}" + (f", {shown(exe, 200)}" if exe else "") + ")"


def refusal_kind(reason, approval=None, rule=None, ruleset=None):
    """Which of BLOCKED_KINDS a refusal is, by its REASON, its APPROVAL (the log's) and its RULE and RULESET."""
    reason = str(reason or "")
    answer = approval if isinstance(approval, dict) else {}
    answered = str(answer.get("reason") or "")
    if CUT_MARK in reason or CUT_MARK in answered:
        return "cut"
    if answered.startswith("no answer") or reason.startswith("no answer"):
        return "unanswered"
    if answered == "answered" or reason == "answered":
        return "denied"
    if "blocked always" in reason or "blocked always" in answered:
        return "blocked"
    if "entered this run" in reason:
        return "taint"
    if ruleset == "policy" or "administrator's policy" in reason:
        return "policy"
    if str(ruleset or "").startswith("mode ") or "security mode" in reason:
        return "mode"
    if reason in ("not allowed by the network options", "not in allowed hosts"):
        return "network"
    if rule not in (None, ""):
        return "rule"
    return "other"


def blocked_why(entry):
    """Why, in plain words, and what to do: for `approvals blocked` (the app says it in its own language)."""
    kind, rule, ruleset = entry["kind"], entry.get("rule"), entry.get("ruleset")
    question = entry.get("question")
    by_rule = f"the ruleset {ruleset} (rule {rule})" if ruleset else "a rule"
    if kind == "network":
        return "not allowed by the run's network options" + (
            ": allow it for the run (sandbox-ai net allow) or always for the project (--keep)" if entry.get("target")
            else "")
    if kind == "rule":
        return f"{by_rule} denies it; the network settings do not change what a ruleset decides"
    if kind == "unanswered":
        asks = f"{by_rule} asks before it" if ruleset else "it was asked about"
        return f"{asks}, and nobody answered in time: refused for a while, then asked again" + (
            f" (allow it: sandbox-ai approvals allow {question} --scope run)" if question else "")
    if kind == "denied":
        return "you denied it for this run" + (f" (take it back: sandbox-ai approvals ask-again {question})"
                                               if question else "")
    if kind == "mode":
        return "the run's security mode refuses it: start again with a laxer --mode"
    if kind == "policy":
        return "the administrator's policy refuses it (ask your administrator)"
    if kind == "cut":
        return "the kill switch cuts it: sandbox-ai net cut RUN off lifts the cut"
    if kind == "blocked":
        return "you blocked it always for this project (sandbox-ai approvals remembered, forget lifts it)"
    if kind == "taint":
        return "the run is tainted: sandbox-ai net untaint RUN takes the taint back (only if you trust what entered)"
    return shown(entry.get("reason") or "refused", 300)


def blocked_actions(entry, record, widest=None):
    """What may be done about ENTRY of the run RECORD (see BLOCKED_TAIL): a subset of allow-run, ask-again,
    deny-run and allow-always. The security mode's and the policy's caps (WIDEST: the policy's, read once by the
    caller): an allow for the run where they approve only once is not offered (the supervisor refuses it anyway);
    "always" only where they keep answers."""
    cap = answer_cap(record, policy_scope() if widest is None else widest)
    kind, question, qkind = entry["kind"], entry.get("question"), entry.get("question_kind")
    actions = []
    target = entry.get("target") or {}
    if kind in ("unanswered", "denied") and question and qkind in REDECIDED:
        if cap != "once":
            actions.append("allow-run")
        actions.append("ask-again")
        if kind == "unanswered":
            actions.append("deny-run")
        if cap == "always" and (qkind == "connect" and target or qkind in ("http", "mcp") and entry.get("ruleset")):
            actions.append("allow-always")
    elif kind in ("unanswered", "denied") and question and qkind == "dns":
        if target.get("host") and cap != "once":
            actions.append("allow-run")
        actions.append("ask-again")
        if target.get("host") and cap == "always":
            actions.append("allow-always")
    elif kind == "network" and (target.get("host") or target.get("tcp")):
        # Not for a packet of another protocol ({"address"}): `net allow --allow ADDRESS` would open every port and
        # protocol of it, far more than the one refused.
        actions.append("allow-run")
        if cap == "always":
            actions.append("allow-always")
    elif kind == "rule" and entry.get("question_kind") in ("http", "mcp") and entry.get("ruleset") and cap == "always":
        actions.append("allow-always")
    return actions


def blocked_entry(run, kind, what, time_, reason="", process=None, rule=None, ruleset=None, question=None,
                  question_kind=None, target=None, source=""):
    return {"time": str(time_ or ""), "run": run, "kind": kind if kind in BLOCKED_KINDS else "other",
            "what": shown(what, 600), "reason": shown(reason or "", 1000), "process": process_text(process),
            "rule": shown(rule, 120) if rule not in (None, "") else None,
            "ruleset": shown(ruleset, 200) if ruleset not in (None, "") else None,
            "question": question if isinstance(question, str) and ID.fullmatch(question) else None,
            "question_kind": question_kind, "target": target or None, "source": source, "count": 1}


def tcp_target(destination):
    """{"tcp": "ADDRESS:PORT"} of a connection's destination as the logs write it, or None."""
    match = re.fullmatch(r"\[?([0-9a-fA-F:.]+)\]?:([0-9]{1,5})", str(destination or ""))
    if not match:
        return None
    try:
        ip = ipaddress.ip_address(match.group(1))
    except ValueError:
        return None
    port = int(match.group(2))
    if not 0 < port < 65536:
        return None
    return {"tcp": f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"}


def question_target(question):
    if question.get("kind") == "dns" and isinstance(question.get("name"), str) and HOST.fullmatch(question["name"]):
        return {"host": question["name"]}
    if question.get("kind") == "connect":
        return tcp_target(question.get("destination"))
    return None


def blocked_of_run(run, record, tail=BLOCKED_TAIL):
    """The refusals of one running sandbox RUN (its run.json RECORD with "directory"), unsorted."""
    found = []
    started = str(record.get("started") or "")
    older = (lambda r: bool(started) and isinstance(r.get("time"), str) and r["time"] < started)
    directory = Path(record.get("directory") or "")
    # Its questions: those refused (unanswered, your deny, the kill switch); the open ones are the page's above.
    questions_dir = directory / "approvals"
    try:
        names = sorted((p.name for p in questions_dir.iterdir() if p.suffix == ".json" and ID.fullmatch(p.stem)),
                       reverse=True)[:BLOCKED_QUESTIONS]
    except OSError:
        names = []
    for name in names:
        # Its answer first: the question itself is read only for a refusal (most were allowed, or are still open).
        try:
            done = json.loads((questions_dir / name).with_suffix(".done").read_text()[:1 << 16])
            if not isinstance(done, dict) or done.get("decision") != "deny":
                continue
            question = json.loads((questions_dir / name).read_text()[:1 << 16])
        except (OSError, ValueError):
            continue
        if not isinstance(question, dict):
            continue
        if question.get("kind") in ("browser", "canary", "device"):
            continue  # not refusals of the network or of a rule: the history lists them
        reason = str(done.get("reason") or "")
        found.append(blocked_entry(run, refusal_kind(reason), what(question), question.get("time"), reason,
                                   question.get("process"), question.get("rule"), question.get("ruleset"),
                                   question.get("id"), question.get("kind"), question_target(question),
                                   "approvals"))
    log_dir = record.get("log_dir")
    if isinstance(log_dir, str) and log_dir:
        logs = Path(log_dir)
        # The proxy's log of this run: connections-TIME-RUN.jsonl.
        try:
            connection_logs = sorted((p for p in logs.glob(f"connections-*-{run}.jsonl")), reverse=True)[:2]
        except OSError:
            connection_logs = []
        for path in connection_logs:
            for entry in tail_records(path, tail=tail, keep=lambda r: r.get("event") == "denied" or (
                    r.get("event") == "tls-refused" and not r.get("abandoned"))):
                approval = entry.get("approval") if isinstance(entry.get("approval"), dict) else {}
                reason = entry.get("reason") or entry.get("error") or ""
                if entry.get("method") and entry.get("url"):
                    shown_what = f"{entry['method']} {entry['url']}"
                    qkind = "http"
                else:
                    shown_what = f"connection to {entry.get('destination') or entry.get('host') or '?'}" + (
                        f" (named {entry['host']} by the agent)" if entry.get("host") else "")
                    qkind = "connect"
                kind = refusal_kind(reason, approval, entry.get("rule"), entry.get("ruleset"))
                target = tcp_target(entry.get("destination")) if qkind == "connect" else None
                found.append(blocked_entry(run, kind, shown_what, entry.get("time"), reason, entry.get("process"),
                                           entry.get("rule"), entry.get("ruleset"), approval.get("question"),
                                           qkind, target, "proxy"))
        # The firewall's and the DNS stub's refusals (one file for every run of the project: this run's only).
        for entry in tail_records(logs / "egress-denied.jsonl", tail=tail, stop=older,
                                  keep=lambda r: (r.get("sandbox") or {}).get("run") in (None, run)
                                  if isinstance(r.get("sandbox") or {}, dict) else False):
            if isinstance(entry.get("host"), str):
                host = entry["host"].lower()
                found.append(blocked_entry(run, "network", f"lookup of {host}", entry.get("time"),
                                           entry.get("reason") or "not in allowed hosts", entry.get("process"),
                                           target={"host": host} if HOST.fullmatch(host) else None, source="dns"))
            elif entry.get("address") is not None:
                protocol, port = str(entry.get("protocol") or "?"), entry.get("port")
                try:
                    ip = ipaddress.ip_address(str(entry["address"]))
                except ValueError:
                    continue
                if protocol == "tcp" and isinstance(port, int) and 0 < port < 65536:
                    target = {"tcp": f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"}
                else:
                    target = {"address": str(ip)}
                found.append(blocked_entry(run, "network", f"{protocol} to {ip}" + (f":{port}" if port else ""),
                                           entry.get("time"), "not allowed by the network options",
                                           entry.get("process"), target=target, source="firewall"))
        # Tool calls a rule refused (the agent's own tools, the MCP servers' tools).
        for name, source in (("mcp-calls.jsonl", "mcp"), ("tools.jsonl", "tool")):
            for entry in tail_records(logs / name, tail=tail, stop=older, keep=lambda r: bool(r.get("reason")) and (
                    r.get("decision") != "allow" or r.get("approval"))):
                approval = entry.get("approval") if isinstance(entry.get("approval"), dict) else {}
                tool = entry.get("tool") or "?"
                shown_what = (f"MCP tool {entry.get('server') or '?'}/{tool}" if source == "mcp" else
                              f"{entry.get('agent') or 'agent'} tool {tool}") + (
                    f" with {entry['arguments']}" if isinstance(entry.get("arguments"), str) else "")
                kind = refusal_kind(entry.get("reason"), approval, entry.get("rule"), entry.get("ruleset"))
                found.append(blocked_entry(run, kind, shown_what, entry.get("time"), entry.get("reason"),
                                           entry.get("process"), entry.get("rule"), entry.get("ruleset"),
                                           approval.get("question") or entry.get("question"), source, None, source))
    # What the supervisor refused at run time: the security mode's and the policy's refusals.
    for entry in tail_records(directory / "safeguard-events.jsonl", tail=1 << 20):
        if entry.get("kind") in ("mode", "policy"):
            found.append(blocked_entry(run, entry["kind"], entry.get("what") or entry["kind"], entry.get("time"),
                                       entry.get("why"), source="supervisor"))
    # A name that carries a canary token of the run is never offered to allow: `net allow --allow-host` would look it
    # up through this host's DNS, and the token would leave (an answer to its question refuses it as well).
    named = [entry for entry in found if (entry.get("target") or {}).get("host")]
    needles = canary_names(record) if named else None  # read once for all of them
    for entry in named:
        if carries_canary(needles, entry["target"]["host"]):
            entry["target"] = None
    return found


def blocked(record, runs=None, limit=BLOCKED_LIMIT, tail=BLOCKED_TAIL):
    """What the running sandbox RECORD (run.json with "directory") and its MCP servers, helpers and team members
    (net.related_runs of RUNS, the running ones) refused, newest first: equal refusals (the same question, or the same
    kind and target) as one entry with their count and the newest time. Each entry: time, run, kind (BLOCKED_KINDS),
    what, reason, process, rule, ruleset, question (its ID, when one was asked), question_kind, target (a destination
    `net allow` takes: {"host"}, {"tcp"} or {"address"}), why (plain words, and what to do) and actions
    (blocked_actions)."""
    runs = net().running() if runs is None else runs
    members = [(record.get("run"), record)] + [(name, other) for name, other in net().related_runs(runs, record)
                                               if isinstance(other, dict) and other.get("directory")]
    entries = []
    for name, member in members:
        try:
            entries += blocked_of_run(str(name), member, tail)
        except OSError:
            continue
    grouped = {}
    for entry in sorted(entries, key=lambda e: e["time"], reverse=True):
        key = (entry["run"], "q", entry["question"]) if entry["question"] else \
            (entry["run"], entry["kind"], entry["what"])
        known = grouped.get(key)
        if known is None:
            grouped[key] = entry
            continue
        known["count"] += 1
        for field in ("process", "rule", "ruleset", "target", "question_kind"):  # the question's file has more
            if not known.get(field) and entry.get(field):
                known[field] = entry[field]
        if entry["source"] == "approvals":  # the question's own refusal says what it was asked about
            known["kind"], known["question_kind"] = entry["kind"], entry["question_kind"]
            known["what"] = entry["what"]
    result = sorted(grouped.values(), key=lambda e: e["time"], reverse=True)[:limit]
    by_run = {name: member for name, member in members}
    widest = policy_scope()  # (with a policy file, read once: it loads the launcher)
    for entry in result:
        entry["why"] = blocked_why(entry)
        entry["actions"] = blocked_actions(entry, by_run.get(entry["run"], record), widest)
    return result


def decided(record):
    """The decisions the running sandbox RECORD remembers for the run, and the requests refused for now because
    nobody answered (its supervisor's control op decide)."""
    return net().ask(record["directory"], {"op": "decide", "action": "list"})


def answered_question(ident, runs=None):
    """(run, record, question with its "result") of the answered question IDENT of a running sandbox, or None."""
    matches = [entry for entry in questions(runs, answered=True) if entry[2].get("id") == ident
               and isinstance(entry[2].get("result"), dict)]
    if len(matches) > 1:
        raise ValueError(f"{shown(ident)} names questions of several sandboxes "
                         f"({', '.join(shown(m[0]) for m in matches)})")
    return matches[0] if matches else None


def redecide(ident, action, runs=None, out=print):
    """Change what a running sandbox decided about its answered (or unanswered) question IDENT for the rest of the
    run: ACTION allow or deny (for the run), or forget (its next request is asked again). Through the supervisor's
    control op decide, which holds the security mode's and the policy's caps; only the human reaches it."""
    if action not in ("allow", "deny", "forget"):
        raise ValueError("allow, deny or forget")
    found = answered_question(ident, runs)
    if found is None:
        raise ValueError(f"no answered question {shown(ident)} of a running sandbox; see sandbox-ai approvals history")
    name, record, question = found
    if record.get("mcp") and not record.get("pid"):
        raise ValueError(f"{shown(name)} is an MCP server of its own: its decisions last until it stops "
                         "(sandbox-ai mcp stop) and cannot be changed during its run")
    if action == "allow" and widest_answer(question, record, policy_scope()) == "once":
        raise ValueError(f"{shown(name)} approves at most once (its security mode or the administrator's policy): "
                         f"ask again (sandbox-ai approvals ask-again {shown(ident)}) and answer the next question")
    answer = net().ask(record["directory"], {"op": "decide", "action": action, "question": ident})
    said = {"allow": "allowed for the rest of the run", "deny": "denied for the rest of the run",
            "forget": "forgotten: its next request is asked again" if answer.get("forgot") else
            "nothing was remembered for it: its next request is asked"}[action]
    out(f"{shown(name)}: {said}: {describe(question)}")
    for note in answer.get("notes") or []:
        out(f"  {shown(note, 1000)}")
    return answer


# --- "Allow always" for a request a ruleset holds: an exception rule in a ruleset of your own -------------------
#
# Each ruleset decides on its own and the strictest decision wins: a rule in another ruleset cannot widen what one
# asks or denies. So the exception goes into the ruleset that held the request, never into a preset's or anyone
# else's file: your own rules.json is changed in place (the running start reads it again: sandbox-ai rules reload),
# the project's own ruleset (the project file's "rules", which a board start passes as a copy) in the trusted project
# file (net.keep_in_project's checks: the next start has it), and any other file (a preset's, someone else's) is
# copied to a file of your own with the exception - which a start uses only in its place.

OWN_RULES_DIR = "rules"  # ~/.config/sandbox-ai/rules/: the copies with your exceptions


def reload_rules(record):
    """The running start RECORD reads its HTTP rules anew (sandbox-ai rules reload): None when done, else why not."""
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_rules_for_approvals", HERE / "rules_cmd.py")
        rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rules)
        rules.reload(record.get("run"), lambda line: None, False)
        return None
    except (ValueError, OSError) as exc:
        return str(exc) or type(exc).__name__


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def exception_rule(entry):
    """The allow rule for the request or tool call ENTRY (a Blocked entry or a question): exactly its method, host and
    path, or its MCP server and tool."""
    kind = entry.get("question_kind") or entry.get("kind")
    stamp = time.strftime("%Y-%m-%d")
    if kind == "http":
        method, url = entry.get("method"), entry.get("url")
        if not (method and url):
            method, _, url = str(entry.get("what") or "").partition(" ")
        parts = urllib.parse.urlsplit(str(url))
        host, raw = (parts.hostname or "").lower(), parts.path or "/"
        if not HOST.fullmatch(host) or not re.fullmatch(r"[A-Z]{1,20}", str(method or "")):
            raise ValueError("not a request sandbox-ai can name exactly (method, host, path)")
        # The path as a rule reads it (decoded, dot segments and ;parameters resolved: runtime.normal_path): the raw
        # one would be decoded on loading, so /v1/%2A%2A became the pattern /v1/** and allowed every path below.
        rt = runtime()
        path = rt.normal_path(raw) or "/"
        if "*" in path or rt.ambiguous_path(raw) or (rt.normal_path(path) or "/") != path:
            raise ValueError("its path holds a * (encoded or not), a dot segment or more than one encoding, which a "
                             "rule would read otherwise: write that rule yourself")
        ident = "allowed-" + hashlib.sha256(f"{method} {host}{path}".encode()).hexdigest()[:8]
        return {"id": ident, "host": host, "methods": method, "path": path, "decision": "allow",
                "reason": f"allowed always for this project ({stamp}, sandbox-ai approvals)"}, "http"
    if kind == "mcp":
        server, tool = entry.get("server"), entry.get("tool")
        if not (server and tool):
            match = re.match(r"MCP tool ([A-Za-z0-9_-]{1,32})/([^ ]{1,128})", str(entry.get("what") or ""))
            server, tool = match.groups() if match else (None, None)
        if not (server and tool) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", str(tool)):
            raise ValueError("not a tool call sandbox-ai can name exactly (server, tool)")
        ident = "allowed-" + hashlib.sha256(f"{server} {tool}".encode()).hexdigest()[:8]
        return {"id": ident, "server": server, "tool": tool, "decision": "allow",
                "reason": f"allowed always for this project ({stamp}, sandbox-ai approvals); every call of this tool"
                }, "mcp"
    raise ValueError("only HTTP requests and MCP tool calls get an exception rule")


def ruleset_file(record, ruleset):
    """(path, how) of the ruleset named RULESET in the run RECORD: how is "own" (your rules.json), "project" (the
    project's own ruleset: its project file, or the board's copy of it) or "other" (a preset's, someone else's)."""
    name = str(ruleset or "")
    if name in ("project", ".sandbox-ai.json"):
        return None, "project"
    if name == "rules.json":
        return config_dir() / "rules.json", "own"
    sources = record.get("rules_sources") if isinstance(record.get("rules_sources"), dict) else {}
    files = [Path(path) for path in sources.get("files") or [] if isinstance(path, str)]
    base = re.sub(r" \([0-9]+\)$", "", name.removeprefix("--rules "))
    matching = [path for path in files if path.name == base]
    if not name.startswith("--rules ") or len(matching) != 1:
        raise ValueError(f"the ruleset {shown(name)} is no file of this run sandbox-ai can name (the policy's, the "
                         "security mode's and --allow-http hold as they are)")
    path = matching[0]
    board_copies = config_dir() / "app" / "rules"
    if path.parent == board_copies and re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
        return path, "project"  # a board start passes the project's ruleset as this copy
    own = config_dir() / OWN_RULES_DIR
    if path.parent == own or path.parent.parent == own and re.fullmatch(r"[0-9a-f]{16}", path.parent.name):
        return path, "own"
    return path, "other"


def copy_folder(path):
    """The folder below OWN_RULES_DIR of the copies of the ruleset PATH (another's): a hash of its full path."""
    return hashlib.sha256(os.path.abspath(str(path)).encode()).hexdigest()[:16]


def with_exception(document, section, rule):
    """DOCUMENT (a ruleset) with RULE first in SECTION (the first rule that matches decides within a ruleset);
    the same rule there already: unchanged (None)."""
    document = dict(document) if isinstance(document, dict) else {"version": 1}
    rules = list(document.get(section) or [])
    if any(isinstance(r, dict) and r.get("id") == rule["id"] for r in rules):
        return None
    document.setdefault("version", 1)
    document[section] = [rule] + rules
    return document


def write_ruleset(path, document):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to write through it")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(document, indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def rule_exception(record, entry, write=False):
    """"Allow always" for the request or tool call ENTRY (a Blocked entry, or a question) of the running sandbox
    RECORD: what changes ({"rule", "section", "ruleset", "where", "path", "changes", "applies"}) and, with WRITE, the
    change made. Refused where the administrator's policy or the run's security mode approves less than always."""
    if policy_scope() != "always":
        raise ValueError(f"the administrator's policy approves at most for {policy_scope()}: nothing is allowed always")
    if answer_cap(record) != "always":
        mode = record["mode"].get("name") if isinstance(record.get("mode"), dict) else None
        raise ValueError(f"the security mode {shown(str(mode or ''))} approves at most for {answer_cap(record)}: "
                         "nothing is allowed always")
    ruleset = entry.get("ruleset")
    if not ruleset:
        raise ValueError("no ruleset held it")
    rule, section = exception_rule(entry)
    path, how = ruleset_file(record, ruleset)
    plan = {"rule": rule, "section": section, "ruleset": ruleset, "where": how}
    if how == "own":
        try:
            document = json.loads(Path(path).read_text()) if Path(path).exists() else {"version": 1}
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path}: {exc}") from None
        plan.update(path=str(path), changes=f"adds the rule {rule['id']} first in {path} (your own ruleset)",
                    applies="now: the running start reads its rulesets again (sandbox-ai rules reload), and every "
                            "later start")
        if write:
            changed = with_exception(document, section, rule)
            if changed is not None:
                write_ruleset(path, changed)
            plan["written"] = changed is not None
            plan["reloaded"] = reload_rules(record)
    elif how == "project":
        project = record.get("workspace")
        plan.update(path=str(Path(str(project)) / ".sandbox-ai.json"),
                    changes=f"adds the rule {rule['id']} first to the project's own ruleset (\"rules\" of its "
                            ".sandbox-ai.json, trusted at once; the Board shows it in Approvals and rules)",
                    applies="from the project's next start; this run keeps its rules (allow it for the run as well)")
        if write:
            def change(document):
                updated = with_exception(document.get("rules") if isinstance(document.get("rules"), dict) else
                                         {"version": 1}, section, rule)
                if updated is None:
                    return []
                document["rules"] = updated
                return ["rules", rule["id"]]
            path_written, added = net().keep_in_project(record, [], change=change)
            plan["written"] = bool(added)
    else:
        # A folder of its own per original (its path's hash), the name kept: a copy of another x.json (another
        # preset's, someone else's) is never taken as the base of this one's, nor used in its place.
        copy = config_dir() / OWN_RULES_DIR / copy_folder(path) / Path(path).name
        try:
            original = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path}: {exc}") from None
        plan.update(path=str(copy), original=str(path),
                    changes=f"{path} is not yours (a preset's or someone else's) and stays as it is: a copy of it "
                            f"with the rule {rule['id']} first is written to {copy}",
                    applies=f"only where a start uses the copy in place of {Path(path).name} (--rules, or Approvals and "
                            "rules on the Board): a ruleset of yours beside it cannot widen what it decides")
        if write:
            existing = None
            with contextlib.suppress(OSError, ValueError):
                existing = json.loads(copy.read_text()) if copy.exists() else None
            changed = with_exception(existing if isinstance(existing, dict) else original, section, rule)
            if changed is not None:
                write_ruleset(copy, changed)
            plan["written"] = changed is not None
    return plan


def decide_again(found, decision, scope, out=print):
    """`approvals allow|deny ID` on a question FOUND (run, record, question) answered or refused already: what the run
    decided is changed for the rest of it (redecide); "allow --scope always" keeps it for the project too."""
    name, record, question = found
    kind, ident = question.get("kind"), question.get("id")
    if decision == "deny":
        if scope != "run":
            raise ValueError(f"question {shown(ident)} was answered or refused already: deny it for the rest of the run "
                             "(--scope run); a name or address is blocked always while its question is open")
        redecide(ident, "deny", out=out)
        return 0
    if scope == "once":
        raise ValueError(f"question {shown(ident)} was answered or refused already: allow it for the rest of the run "
                         f"(--scope run), or let it be asked again: sandbox-ai approvals ask-again {shown(ident)}")
    target = question_target(question)
    if kind == "dns" and target and canary_in_name(record, target["host"]):
        # `net allow --allow-host` looks the name up through this host's DNS: the token would leave.
        raise ValueError(f"{shown(target['host'])} carries a canary token of the run: it is never looked up")
    if scope == "always" and answer_cap(record, policy_scope()) != "always":
        # Checked before anything is written: the exception rule or the project file would keep it.
        raise ValueError(f"{shown(name)}: its security mode or the administrator's policy keeps no answer always; "
                         f"allow it for the run (--scope run)" + (" if they allow that" if answer_cap(
                             record, policy_scope()) == "once" else ""))
    if scope == "run":
        if kind == "dns":
            if not target:
                raise ValueError(f"{shown(question.get('name'))} is no host name")
            status = net().main(["allow", str(record.get("run")), "--allow-host", target["host"]], out=out)
            with contextlib.suppress(ValueError, OSError):
                redecide(ident, "forget", out=lambda line: None)
            return status
        redecide(ident, "allow", out=out)
        return 0
    if kind in ("http", "mcp"):
        plan = rule_exception(record, {**question, "question_kind": kind}, write=True)
        out(f"{shown(name)}: {plan['changes']}; applies {plan['applies']}")
        if plan.get("reloaded"):
            out(f"  not reloaded: {shown(plan['reloaded'], 500)}")
        redecide(ident, "allow", out=out)
        return 0
    if kind in ("connect", "dns") and target:
        option = ["--allow-host", target["host"]] if "host" in target else ["--allow-tcp", target["tcp"]]
        status = net().main(["allow", str(record.get("run")), *option, "--keep"], out=out)
        with contextlib.suppress(ValueError, OSError):
            redecide(ident, "forget", out=lambda line: None)
        return status
    raise ValueError(f"a {shown(kind, 20)} question is allowed at most for the run (--scope run)")


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai approvals", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = parser.add_subparsers(dest="command", required=True)
    open_ones = commands.add_parser("list", help="open questions")
    open_ones.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts and the web interface")
    history = commands.add_parser("history", help="questions with their answers")
    history.add_argument("run", nargs="?")
    for name in ("allow", "deny"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "allow":
            command.add_argument("-s", "--scope", choices=["once", "run", "always"], default="once")
            command.add_argument("--no-open", action="store_true",
                                 help="a browser question: allow it without opening the page on this host (you "
                                      "opened it where you are: the web interface, the app on another machine)")
        else:
            command.add_argument("-s", "--scope", choices=["run", "once", "always"], default="run",
                                 help="run (default): not asked again in this run; once: may be asked again; "
                                      "always: also refused without a question at every later start of the project")
    listing = commands.add_parser("remembered", help="what was allowed always, per project")
    listing.add_argument("-j", "--json", action="store_true")
    forgetting = commands.add_parser("forget", help="forget an entry allowed always")
    forgetting.add_argument("project")
    forgetting.add_argument("entry", help="a host name or IP:PORT, as `remembered` shows it")
    refused = commands.add_parser("blocked", help="what a running sandbox (and its MCP servers and helpers) refused, "
                                                  "newest first: why, and what to do")
    refused.add_argument("run", nargs="?")
    refused.add_argument("-j", "--json", action="store_true", help="as JSON, for scripts and the app")
    refused.add_argument("-n", "--limit", type=int, default=BLOCKED_LIMIT, metavar="N", help="at most N entries")
    remembered_now = commands.add_parser("decided", help="the decisions a running sandbox remembers for the run, and "
                                                         "the requests refused for now because nobody answered")
    remembered_now.add_argument("run", nargs="?")
    remembered_now.add_argument("-j", "--json", action="store_true")
    again = commands.add_parser("ask-again", help="forget what the run decided about question ID (an answer, or no "
                                                  "answer): its next request is asked again")
    again.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command in ("blocked", "decided"):
            loaded = net()
            runs = loaded.running()
            record = loaded.choose(runs, args.run, command=f"approvals {args.command}")
            if args.command == "decided":
                entries = decided(record).get("decided") or []
                if args.json:
                    out(json.dumps(entries, indent=2))
                    return 0
                textout().table(["QUESTION", "DECISION", "SCOPE", "KIND", "WHAT"],
                                [[shown(e.get("question")), shown(e.get("decision")), shown(e.get("scope")),
                                  shown(e.get("kind")), shown(" ".join(str(v) for v in (e.get("details") or {}).values()), 300)]
                                 for e in entries], out, indent=0, empty="nothing is remembered for this run")
                return 0
            if args.limit < 1:
                raise ValueError("--limit: at least 1")
            entries = blocked(record, runs, limit=args.limit)
            if args.json:
                out(json.dumps(entries, indent=2))
                return 0
            t = textout()
            t.table(["TIME", "RUN", "KIND", "WHAT", "WHY"],
                    [[shown(e["time"]), shown(e["run"], 40), e["kind"],
                      shown(e["what"], 300) + (f" (x{e['count']})" if e["count"] > 1 else "") +
                      (f"; by {e['process']}" if e["process"] else ""), e["why"]] for e in entries], out, indent=0,
                    empty="nothing was refused")
            return 0
        if args.command == "ask-again":
            redecide(args.id, "forget", out=out)
            return 0
        if args.command in ("allow", "deny") and ID.fullmatch(args.id) and \
                not any(q.get("id") == args.id for _, _, q in questions()):
            found = answered_question(args.id)
            if found is not None:
                return decide_again(found, args.command, args.scope, out)
        if args.command == "remembered":
            known = remembered()
            if args.json:
                out(json.dumps(known, indent=2))
            elif not known:
                out("nothing is allowed or blocked always")
            for project, entry in ({} if args.json else known).items():
                out(shown(project, 1000))
                details = entry.get("details", {})
                for verb, fields in (("allow", ("hosts", "tcp")), ("block", ("blocked_hosts", "blocked_tcp"))):
                    for field in fields:
                        for value in entry[field]:
                            out(f"  {verb} {shown(value)}  {origin_of(details.get(detail_key(field, value)), field)}")
            return 0
        if args.command == "forget":
            if not forget(args.project, args.entry):
                raise ValueError(f"{shown(args.entry)} is not remembered for {shown(args.project, 1000)}")
            out(f"forgot {shown(args.entry)} for {shown(args.project, 1000)}; it applies from the next start")
            return 0
        if args.command == "list" and args.json:
            # Everything the agent chose comes escaped (describe, origin), as in the table.
            widest = policy_scope()
            out(json.dumps([{"id": shown(q.get("id")), "run": shown(name), "sandbox": origin(record),
                             "kind": shown(q.get("kind")), "time": shown(q.get("time")), "question": describe(q),
                             "always": always(q).removeprefix("; "), **question_times(q),
                             "max_scope": widest_answer(q, record, widest),
                             **({"allow_refused": DNS_ONCE} if q.get("kind") == "dns"
                                and widest_answer(q, record, widest) is None else {}),
                             **(browser_fields(q.get("url")) if q.get("kind") == "browser" else {})}
                            for name, record, q in questions()], indent=2))
            return 0
        if args.command == "list":
            pending = questions()
            textout().table(["ID", "SANDBOX", "QUESTION"],
                            [[shown(q.get("id")), origin(record), describe(q)] for _, record, q in pending],
                            out, indent=0, empty="no open questions")
            if pending:
                out(textout().style("answer: sandbox-ai approvals allow|deny ID [--scope once|run|always]", "muted"))
            return 0
        if args.command == "history":
            t, rows = textout(), []
            wanted = None
            if args.run:  # a run id, or the name `sandbox-ai list` shows (net.choose says what a stopped one is)
                loaded = net()
                runs = {**loaded.running(), **mcp_runs()}
                chosen = loaded.choose(runs, args.run, command="approvals history")
                wanted = next((key for key, record in runs.items() if record is chosen), args.run)
            for name, _, question in questions(answered=True):
                if wanted and name != wanted:
                    continue
                result = question.get("result", {})
                decision = shown(result.get("decision", "open"))
                kind = {"allow": "ok", "deny": "bad"}.get(result.get("decision"), "warn")
                rows.append([shown(question.get("time")), shown(name), t.style(decision, kind),
                             shown(result.get("scope", "")), describe(question), shown(result.get("reason", ""))])
            # The question last: it is the longest and wraps at the terminal's width.
            t.table(["TIME", "SANDBOX", "ANSWER", "SCOPE", "REASON", "QUESTION"],
                    [row[:4] + [row[5] or "-", row[4]] for row in rows] if rows else [], out, indent=0,
                    empty="no answered questions")
            return 0
        answer(args.id, args.command, args.scope, out=out, **({"no_open": True} if getattr(args, "no_open", False)
                                                              else {}))
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai approvals: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
