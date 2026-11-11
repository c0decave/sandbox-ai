#!/usr/bin/env python3
"""sandbox-ai approvals: answer what running sandboxes ask.

  sandbox-ai approvals list [--json]             open questions of every running sandbox
  sandbox-ai approvals allow ID [--scope once|run|always] [--no-open]
  sandbox-ai approvals deny ID [--scope run|once]
  sandbox-ai approvals history [RUN]             questions and their answers
  sandbox-ai approvals remembered [--json]        what was allowed "always", per project
  sandbox-ai approvals forget PROJECT ENTRY       forget one of them (a host name or IP:PORT)

A sandbox asks (with --ask, the default) when its agent connects to a destination the options do not
allow, looks up a name they do not pin, pushes commits the privacy check has warnings about, or sends a
request an "ask" rule holds; an MCP server asks about tool calls its rules hold; a device rule asks before a
device is attached (`sandbox-ai devices add`, --device, a replug). Without an answer within
60 seconds the question is refused (a canary's within 5 minutes; a request to open a URL in your browser
waits until it is answered or the run ends). Scopes: once (this connection), run (until the
sandbox ends), always (also every later start of this project; kept in
~/.config/sandbox-ai/approved.json). A name allowed after a lookup is reached by the agent's next try
(for lookups once means run: the name stays pinned); only its public addresses are opened. A deny counts
for the rest of the run (--scope once: the agent may ask again). The name in a connection question is the
one the agent sends: what is allowed is the address, and "always" keeps the name only when the sandbox
proved it (its own pin, or the real server's certificate on the approved connection); the question says
which it will be.

One question, one prompt: while the desktop app or the web interface (`sandbox-ai web`, with a page open) watches
the questions, it says so with a heartbeat in ~/.local/state/sandbox-ai/approvals-watchers/PID.json, and the
launcher shows no notification of its own for a question or a browser request (no buttons, no plain notification
either): the watcher's prompt is the only one. A canary's alarm is always shown. A heartbeat older than 15 seconds,
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
            elif net().cut_state(record["directory"]) in ("remote", "api", "all"):
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
    if actions in (QUESTION_ACTIONS, BROWSER_ACTIONS) and watched():
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
    try:
        config = json.loads((Path(record["directory"]) / CANARY_CONFIG).read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return True
    forms = config.get("names") if isinstance(config, dict) else None
    if not isinstance(forms, list):
        return True
    name = str(name).lower().rstrip(".")
    needles = {entry[0].lower() for entry in forms
               if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str) and len(entry[0]) >= 8}
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


def desktop_notification(title, text, urgency="critical"):
    """notify-send; TEXT names what the agent asked for, so markup in it is escaped (some notification
    daemons render <b>, <a> or <img> in the body) and control characters are dropped. URGENCY: critical (a
    question, an error) or normal (a warning, e.g. a safeguard that refused something)."""
    import html
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
    args = parser.parse_args(argv)
    try:
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
