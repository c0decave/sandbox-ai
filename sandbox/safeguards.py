#!/usr/bin/env python3
"""sandbox-ai safeguards: what a sandbox's safeguards refused or ended, why, and what would allow it.

  sandbox-ai safeguards [RUN] [--follow] [--json]          one run (a run id or the name of a start)
  sandbox-ai safeguards --all [--tail N] [--json]           every run with a log directory, and every running one
  sandbox-ai safeguards --session NAME | --project PATH | --log-dir DIR
  sandbox-ai safeguards kinds [--json]                      the kinds, their level, which are muted

While a sandbox runs, its launcher collects every safeguard that fires into one record each (safeguards.jsonl in
the run's log directory; in its control directory when it has none): the firewall dropping a connection, a name
lookup refused, the proxy refusing a connection or a request, a rule's deny, a question nobody answered in time,
the privacy check refusing a push, a publishing request refused, the security mode or the administrator's
policy refusing a run-time change, the run's taint holding an action, a device refused, the memory watch ending
a process, the supervisor dropping its caches, the kernel's out-of-memory killer, a failed safety check and a
canary that went off; and what ended or failed an MCP server's process (its container out of memory, at its
process limit, its /tmp full, an exit nobody asked for: the server's state directory's safeguards.jsonl). A record
says when, which run, what was refused or ended, why (the rule, mode, policy or
check), the process when it is known, and the fix: the exact command or option that would allow it, or that it
stays refused.

The launcher also tells you on the desktop: warnings (errors for a canary, an out-of-memory kill or a failed
check), grouped and rate-limited: "12 connections to x.example dropped by the firewall in the last minute"
instead of 12 notifications. The desktop app and the web interface show the same as popups and as a list.
"safeguard_mute" in ~/.config/sandbox-ai/defaults.json (a list of kinds; `sandbox-ai defaults set
safeguard_mute '["firewall","dns"]'`) silences kinds: they are still recorded, never announced.

Records hold no secret values: they are redacted as the activity log is (sandbox-ai activity), and what the
agent chose (names, URLs, arguments) is shortened and kept as plain text.

Standard library only.
"""
import argparse
import collections
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
FILE = "safeguards.jsonl"               # the run's records (log directory, else control directory)
SUPERVISOR_FILE = "safeguard-events.jsonl"  # what the supervisor reports (control directory, hidden from the agent)
SUPERVISOR_MEMORY_FILE = "supervisor-memory.jsonl"
SCHEMA = 1
FILE_LIMIT = 16 << 20       # bytes of safeguards.jsonl per directory; then no more records
TEXT_LIMIT = 300            # characters of "what" and "why"
FIX_LIMIT = 400
WINDOW = 60.0               # seconds a group of equal events is counted before it is told again
RATE = 4                    # warnings told per WINDOW at most (errors always are)
INTERVAL = 2.0              # seconds between two looks at the run's logs
MUTE_KEY = "safeguard_mute"
LEVELS = ("warning", "error")
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z][a-z0-9-]{0,62}")

# kind: (level, title, the grouped text with {count} and {target})
KINDS = {
    "firewall": ("warning", "connection dropped by the firewall",
                 "{count} connections to {target} dropped by the firewall in the last minute"),
    "dns": ("warning", "name lookup refused", "{count} lookups of {target} refused in the last minute"),
    "network": ("warning", "connection refused by the proxy", "{count} connections to {target} refused in the last minute"),
    "rule": ("warning", "refused by a rule", "{count} actions refused by {target} in the last minute"),
    "approval-timeout": ("warning", "refused without an answer",
                         "{count} questions about {target} refused without an answer in the last minute"),
    "push": ("warning", "push refused by the privacy check", "{count} pushes to {target} refused in the last minute"),
    "publish": ("warning", "publishing refused", "{count} publishing requests to {target} refused in the last minute"),
    "mode": ("warning", "refused by the security mode", "{count} changes refused by the security mode in the last minute"),
    "policy": ("warning", "refused by the administrator's policy",
               "{count} actions refused by the administrator's policy in the last minute"),
    "taint": ("warning", "held: the run is tainted", "{count} actions held because the run is tainted in the last minute"),
    "device": ("warning", "device refused", "{count} devices refused in the last minute"),
    "memory": ("warning", "process ended: memory nearly full", "{count} processes ended to free memory in the last minute"),
    "supervisor-memory": ("warning", "the supervisor dropped its caches",
                          "the supervisor dropped its caches {count} times in the last minute"),
    "guard": ("error", "a safety check failed", "{count} safety checks failed in the last minute"),
    "canary": ("error", "a canary went off", "{count} canary events in the last minute"),
    "oom": ("error", "out of memory: the kernel ended a process",
            "the kernel ended {count} processes for lack of memory in the last minute"),
    # An MCP server's container (mcp.py's watch): out of memory, its process limit, a full /tmp, an exit nobody asked for.
    "mcp-server": ("error", "an MCP server's process ended or failed",
                   "{count} times an MCP server's process ended or failed in the last minute ({target})"),
    "loop": ("warning", "loop guard intervened", "{count} loop guard interventions at {target} in the last minute"),
}
# What the supervisor may report itself (it runs beside the agent: nothing else is taken from its file).
SUPERVISOR_KINDS = {"mode", "policy", "guard"}
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_safeguards", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def default_log_dir(project):
    """launcher().default_log_dir, without loading the launcher for it (a test keeps both the same): the log
    directory the wizard uses for PROJECT, per project, also for equal directory names."""
    project = Path(project)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state / "sandbox-ai" / "logs" / f"{project.name or 'root'}-{digest}"


def stamp(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if seconds is None else seconds))


def printable(text, limit=TEXT_LIMIT):
    """TEXT on one line, control characters replaced, at most LIMIT characters."""
    text = "".join(c if c.isprintable() else " " for c in str(text if text is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Redactor:
    """The activity log's redaction (activity.Redactor: the run's secret values, token formats, credential
    patterns); a plain cut where that module cannot be loaded."""

    def __init__(self, values=()):
        try:
            self.inner = module("activity").Redactor(values)
        except Exception:  # noqa: BLE001 - never without a record: the values alone are still removed
            self.inner = None
        self.values = sorted({v for v in values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text, limit=TEXT_LIMIT):
        text = str(text if text is not None else "")
        if self.inner is not None:
            text = self.inner(text, limit + 200)
        else:
            for value in self.values:
                text = text.replace(value, "[redacted]")
        return printable(text, limit)


def process_text(process):
    """One short line about the process of a record (what the supervisor's attribution found), or None."""
    if isinstance(process, str):
        return printable(process, 160) or None
    if not isinstance(process, dict) or not process:
        return None
    if process.get("unknown") is not None and not process.get("comm"):
        return None
    name = process.get("comm") or process.get("name") or process.get("exe") or "?"
    text = f"{name} (pid {process.get('ns_pid') or process.get('pid') or '?'})"
    argv = process.get("argv") if isinstance(process.get("argv"), list) else None
    command = " ".join(str(part) for part in argv[:8]) if argv else process.get("command") or process.get("cmdline")
    if command:
        text += f": {command}"
    return printable(text, 200)


def record(kind, what, why, fix, run=None, process=None, target=None, rule=None, source=None, stays=False,
           count=1, when=None, announced=False, redact=None, details=None):
    """One safeguards.jsonl record. Everything that came from the sandbox goes through REDACT (a Redactor)."""
    redact = redact or Redactor()
    level = KINDS.get(kind, ("warning",))[0]
    entry = {"v": SCHEMA, "time": when if isinstance(when, str) and when else stamp(), "run": run, "kind": kind,
             "level": level, "what": redact(what), "why": redact(why), "fix": redact(fix, FIX_LIMIT),
             "stays": bool(stays), "count": count if isinstance(count, int) and count > 0 else 1,
             "source": source or "sandbox-ai"}
    shown = process_text(process)
    if shown:
        entry["process"] = redact(shown, 200)
    if target:
        entry["target"] = redact(target, 200)
    if rule:
        entry["rule"] = redact(rule, 120)
    if announced:
        entry["announced"] = True  # told already where it happened (a canary's own notification)
    if isinstance(details, dict) and details:
        entry["details"] = {printable(k, 40): redact(v, 200) if isinstance(v, str) else v
                            for k, v in list(details.items())[:12]
                            if isinstance(v, (str, int, float, bool)) and not isinstance(k, bool)}
    # Unique: equal refusals in the same second are separate records (the readers drop a record read twice by it).
    entry["id"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode() + os.urandom(16)).hexdigest()[:16]
    return entry


def append(path, records, limit=FILE_LIMIT):
    """Append RECORDS to PATH (mode 0600, never through a link); False once it holds LIMIT bytes."""
    if not records:
        return True
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size >= limit:
            return False
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8", "replace")
        if info.st_size + len(data) > limit:
            return False
        os.write(fd, data)
        return True
    finally:
        os.close(fd)


def read(path, keep=None, tail=0):
    """The records of PATH that KEEP(record) wants; damaged lines are skipped; with TAIL only the last TAIL."""
    found = collections.deque(maxlen=tail) if tail else []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        if tail:  # the last TAIL: from the end, only as far back as needed (the file holds every run)
            tails = module("jsonltail")
            return tails.newest(fd, tails.complete_end(fd, os.fstat(fd).st_size), safeguard_entry, keep, tail)
        with os.fdopen(os.dup(fd), "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("kind") in KINDS and (keep is None or keep(entry)):
                    found.append(entry)
    finally:
        os.close(fd)
    return list(found)


# -- the fix for each refusal ---------------------------------------------------------------------------

def run_word(run):
    return shlex.quote(str(run)) if run else "RUN"


def address_option(address, port, protocol):
    """--allow-tcp ADDRESS:PORT for TCP, --allow ADDRESS otherwise; None for what is no address."""
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    if str(protocol).lower() == "tcp" and isinstance(port, int) and 0 < port < 65536:
        return f"--allow-tcp {f'[{ip}]' if ip.version == 6 else ip}:{port}"
    return f"--allow {ip}"


def host_fix(run, host):
    if isinstance(host, str) and HOST.fullmatch(host.lower()):
        return f"sandbox-ai net allow {run_word(run)} --allow-host {host.lower()}"
    return None


TAINT_MARK = "entered this run"


def classify_refusal(reason, approval, rule, ruleset):
    """The kind of a refusal a log names (rule deny, a question not answered, the taint, the policy, publish),
    or None for the human's own "deny" (that is an answer, not a safeguard)."""
    reason = str(reason or "")
    answer = approval if isinstance(approval, dict) else {}
    answered = str(answer.get("reason") or "")
    if answer and answered == "answered":
        return None
    if TAINT_MARK in reason:
        return "taint"
    if answered.startswith("no answer"):
        return "approval-timeout"
    if ruleset == "publish":
        return "publish"
    if ruleset == "policy":
        return "policy"
    if rule is not None or answer:
        return "rule"
    return None


def rule_label(rule, ruleset):
    if rule in (None, "") and not ruleset:
        return None
    return f"rule {rule} of {ruleset}" if ruleset else f"rule {rule}"


def rule_fix(kind, run, rule, ruleset, reason, approval=None):
    """(fix, stays) for a refusal of KIND by a rule."""
    if kind == "taint":
        return (f"sandbox-ai net untaint {run_word(run)} takes the taint back (only if you trust what entered); "
                "otherwise it stays held", False)
    if kind == "approval-timeout":
        question = approval.get("question") if isinstance(approval, dict) else None
        question = question if isinstance(question, str) and re.fullmatch(r"[0-9-]{1,40}", question) else None
        return ("nobody answered in time: it is refused for 2 minutes without asking, then asked again; allow it for "
                "the run on the app's Approvals page (Blocked)" + (f" or with sandbox-ai approvals allow {question} "
                                                                    "--scope run" if question else "")
                + ", or answer the next question in time", False)
    if kind == "publish":
        return ("publishing is asked first: start with --ask on (the default) and answer the question; with --ask "
                "off it stays refused", False)
    if kind == "policy" or ruleset == "policy":
        return "the administrator's policy refuses it: it stays refused (ask your administrator)", True
    if ruleset in ("project", ".sandbox-ai.json"):
        return f"rule {rule} of the project file refuses it: change that rule (sandbox-ai wizard edit) or keep it", False
    where = {"rules": "~/.config/sandbox-ai/rules.json", "--rules": "the --rules file",
             "--allow-http": "the --allow-http options"}.get(str(ruleset), f"the ruleset {ruleset}")
    if rule == "(unlisted)":
        return (f"only the requests {where} allows are passed: add an allow rule for it there, or keep it "
                "refused", False)
    return f"rule {rule} in {where} refuses it: change that rule for the next start, or keep it refused", False


# -- the run's logs, as records -------------------------------------------------------------------------

def from_egress(entry, run, redact):
    """egress-denied.jsonl: the firewall dropped a destination, or a name lookup was refused."""
    if isinstance(entry.get("host"), str):
        host = entry["host"]
        fix = host_fix(run, host)
        why = f"{host} is {entry.get('reason') or 'not in allowed hosts'} (--allow-host): the sandbox gets no address for it"
        return record("dns", f"lookup of {host}", why, fix or "not a host name: it stays refused", run,
                      entry.get("process"), host, source="dns", stays=fix is None, when=entry.get("time"),
                      redact=redact)
    if entry.get("address") is None:
        return None
    address, port, protocol = entry.get("address"), entry.get("port"), entry.get("protocol") or "?"
    option = address_option(address, port, protocol)
    target = f"{address}:{port}" if port not in (None, "") else str(address)
    return record("firewall", f"{protocol} to {target}", "not allowed by the run's network options: the firewall "
                  "drops it", f"sandbox-ai net allow {run_word(run)} {option}" if option else "it stays refused",
                  run, entry.get("process"), str(address), source="firewall", stays=option is None,
                  when=entry.get("time"), redact=redact)


def connection_target(entry):
    host = entry.get("host")
    if isinstance(host, str) and host:
        return host
    url = entry.get("url")
    match = re.match(r"[a-z][a-z0-9+.-]*://([^/?#]+)", url) if isinstance(url, str) else None
    if match:
        return match.group(1)
    return str(entry.get("destination") or "?")


LOOP_WHAT = {"note": "a note to the model", "paused tools": "tool use paused for one answer",
             "answer stopped": "a repeating answer ended"}


def from_loop(entry, run, redact):
    """The loop guard on an --llm server's request (the proxy's llm_loop): what it found and did."""
    loop = entry["llm_loop"]
    action, kind = str(loop.get("action") or "?"), str(loop.get("kind") or "?")
    endpoint = str(loop.get("endpoint") or "the LLM server")
    if kind == "text":
        why = str(loop.get("detail") or "the answer repeated itself")
    elif kind == "watchdog":
        why = f"the watchdog: {loop.get('watchdog') or 'the turn went on too long'}"
    else:
        why = f"the model called {loop.get('tool') or 'a tool'} {loop.get('count') or 'several'} times with the same " \
              "arguments and the same result"
    fix = (f"nothing to allow: the model goes on with the note; to change it: sandbox-ai llm set {endpoint} "
           "--loop-guard LAYER=VALUE (warn, block, text, watchdog_requests ...) or --loop-guard off")
    return record("loop", f"{LOOP_WHAT.get(action, action)} ({endpoint})", why, fix, run, entry.get("process"),
                  endpoint, source="proxy", when=entry.get("time"), redact=redact)


def from_connection(entry, run, redact):
    """A line of the proxy's connection log: refusals, refused pushes, the run becoming tainted, the loop guard."""
    event = entry.get("event")
    when = entry.get("time")
    if event == "http" and isinstance(entry.get("llm_loop"), dict):
        return from_loop(entry, run, redact)
    if event == "taint":
        state = entry.get("taint") if isinstance(entry.get("taint"), dict) else {}
        source = state.get("source") if isinstance(state.get("source"), dict) else {}
        what = source.get("what") or "a source marked untrusted"
        rule = rule_label(source.get("rule"), source.get("ruleset"))
        return record("taint", f"untrusted content from {what} entered the run",
                      f"{rule or 'a rule'} marks it untrusted: the rules for tainted runs apply from now on",
                      f"sandbox-ai net untaint {run_word(run)} (only if you trust what entered)", run, None, str(what),
                      rule, source="proxy", when=when, redact=redact)
    push = entry.get("push")
    target = connection_target(entry)
    if isinstance(push, dict) and not push.get("ok"):
        url = entry.get("url") or target
        if push.get("canary"):
            return record("canary", f"push to {url} carried canary tokens", push.get("message") or "canary tokens",
                          "see sandbox-ai canaries log; the push stays refused", run, entry.get("process"), target,
                          source="proxy", stays=True, when=when, announced=True, redact=redact)
        return record("push", f"push to {url}", push.get("message") or "the privacy check refused it",
                      "fix what the check found (sandbox-ai privacy-check in the workspace lists it, the same rules) "
                      "and push again; findings that only warn are asked instead", run, entry.get("process"), target,
                      source="proxy", when=when, redact=redact)
    if event not in ("denied", "tls-refused"):
        return None
    reason = str(entry.get("reason") or entry.get("error") or "")
    approval = entry.get("approval")
    rule, ruleset = entry.get("rule"), entry.get("ruleset")
    what = f"{entry.get('method')} {entry.get('url')}" if entry.get("method") and entry.get("url") else \
        f"connection to {entry.get('destination') or target}"
    if event == "tls-refused" and entry.get("abandoned"):
        return None  # the program left mid-handshake (it ended): nothing was refused
    if event == "tls-refused":
        # The handshake between the program and the proxy failed: the proxy opens TLS (it shows the program a
        # certificate of the sandbox's session CA), and the program refused it or broke off - not the server's
        # certificate (that is tls-upstream-failed). Mostly a program with its own list of CAs or pinned keys.
        name = str(entry.get("host") or entry.get("sni") or target or "")
        host = name.rpartition(":")[0] if name.count(":") == 1 else name
        return record("network", f"TLS to {target}",
                      (reason or "the handshake failed") + " - the program broke off the TLS handshake with the "
                      "proxy, which opens TLS to look into it: the program did not accept the sandbox's session CA",
                      f"a program with its own CA list or pinned keys needs the session CA "
                      f"(NODE_EXTRA_CA_CERTS, SSL_CERT_FILE, REQUESTS_CA_BUNDLE point to it in the sandbox) or "
                      f"--no-inspect {host or 'HOST'} (the connection passes unopened; only its name is logged)",
                      run, entry.get("process"), target, source="proxy", when=when, redact=redact)
    kind = classify_refusal(reason, approval, rule, ruleset)
    if kind is None and isinstance(approval, dict) and approval.get("reason") == "answered":
        return None  # the human said no
    if kind is None:
        destination = str(entry.get("destination") or "")
        address, _, port = destination.rpartition(":")
        option = address_option(address.strip("[]"), int(port) if port.isdigit() else None, "tcp")
        name = host_fix(run, entry.get("host"))
        if "blocked always" in reason:
            fix, stays = "you blocked it for this project: sandbox-ai approvals remembered, then forget it", False
        elif "not allowed by the network options" in reason or "not approved" in reason:
            fix = name or (f"sandbox-ai net allow {run_word(run)} {option}" if option else "it stays refused")
            stays = not (name or option)
        else:
            fix, stays = "the proxy refuses what it cannot check: it stays refused", True
        return record("network", what, reason or "refused by the proxy", fix, run, entry.get("process"), target,
                      source="proxy", stays=stays, when=when, redact=redact)
    fix, stays = rule_fix(kind, run, rule, ruleset, reason, approval)
    group = rule_label(rule, ruleset) if kind == "rule" else target
    return record(kind, what, reason, fix, run, entry.get("process"), group or target, rule_label(rule, ruleset),
                  source="proxy", stays=stays, when=when, redact=redact)


def from_tool_call(entry, run, redact, source):
    """tools.jsonl (the agent's own tools) or mcp-calls.jsonl (MCP tool calls): calls a rule refused."""
    reason = entry.get("reason")
    if not reason or entry.get("decision") == "allow" and not entry.get("approval"):
        return None
    if source == "mcp" and entry.get("event") == "refused":
        return record("rule", f"MCP message to {entry.get('server') or 'a server'}", reason,
                      "the relay passes only well-formed MCP: it stays refused", run, entry.get("process"),
                      entry.get("server") or "mcp", source="mcp", stays=True, when=entry.get("time"), redact=redact)
    if source == "mcp" and entry.get("event") != "call":
        return None
    approval = entry.get("approval")
    rule, ruleset = entry.get("rule"), entry.get("ruleset")
    kind = classify_refusal(reason, approval, rule, ruleset)
    if kind is None:
        if rule is None and not approval and "not a hook of a known agent" in str(reason):
            kind = "guard"
        else:
            return None
    tool = entry.get("tool") or "?"
    what = f"MCP tool {entry.get('server')}/{tool}" if source == "mcp" else f"{entry.get('agent') or 'agent'} tool {tool}"
    if kind == "guard":
        return record("guard", what, reason, "the tool hook refuses what it cannot check: it stays refused", run,
                      entry.get("process"), tool, source=source, stays=True, when=entry.get("time"), redact=redact)
    fix, stays = rule_fix(kind, run, rule, ruleset, reason, approval)
    group = rule_label(rule, ruleset) if kind == "rule" else tool
    return record(kind, what, reason, fix, run, entry.get("process"), group, rule_label(rule, ruleset), source=source,
                  stays=stays, when=entry.get("time"), redact=redact)


def from_device(entry, run, redact):
    """devices.jsonl: a device refused at run time."""
    if entry.get("run") not in (None, run) or entry.get("event") != "refuse":
        return None
    value = entry.get("value") or "a device"
    reason = entry.get("reason") or "refused"
    ruled = entry.get("decision") == "deny" and entry.get("rule") is not None
    if ruled:
        fix, stays = rule_fix("policy" if entry.get("ruleset") == "policy" else "rule", run, entry.get("rule"),
                              entry.get("ruleset"), reason)
    else:
        fix, stays = (f"fix what the reason names, then sandbox-ai devices add {run_word(run)} "
                      f"{shlex.quote(str(value))}"), False
    return record("device", f"device {value}", reason, fix, run, None, str(value),
                  rule_label(entry.get("rule"), entry.get("ruleset")), source="devices", stays=stays,
                  when=entry.get("time"), redact=redact)


def from_question(question, done, run, redact):
    """A question of the run's approvals directory: a canary that went off, or a question nobody answered
    (for the kinds whose refusal no other log names with its answer)."""
    kind = question.get("kind")
    if kind == "canary":
        return record("canary", question.get("summary") or f"canary {question.get('canary') or ''}",
                      f"a decoy was touched ({question.get('event') or 'read'}): nothing legitimate uses it",
                      f"see sandbox-ai canaries log {run_word(run)}; nothing to allow", run, None,
                      question.get("canary") or question.get("path"), source="canary", stays=True,
                      when=question.get("time"), announced=True, redact=redact)
    if not isinstance(done, dict) or not str(done.get("reason") or "").startswith("no answer"):
        return None
    if kind not in ("dns", "push", "device", "limits"):
        return None  # connections, requests and tool calls: their logs name the refusal with this answer
    about = question.get("name") or question.get("url") or question.get("value") or question.get("path") or kind
    return record("approval-timeout", f"question about {kind} {about}", done.get("reason"),
                  rule_fix("approval-timeout", run, None, None, "", {"question": question.get("id")})[0], run,
                  question.get("process"), str(about),
                  source="approvals", when=question.get("time"), redact=redact)


def from_supervisor(entry, run, redact):
    """CONTROL_DIR/safeguard-events.jsonl (the supervisor runs beside the agent): only its own kinds, plain
    text; the fix is said here, not taken from the file."""
    kind = entry.get("kind")
    if kind not in SUPERVISOR_KINDS:
        return None
    what, why = entry.get("what") or kind, entry.get("why") or ""
    option = entry.get("option") if isinstance(entry.get("option"), str) else None
    if entry.get("target") == "approval scope":  # an answer wider than allowed: it counts with the narrower scope
        fix = ("the answer counts with the narrower scope: " + ("start again with a laxer --mode for wider answers"
               if kind == "mode" else "the administrator's policy sets it (max_approval_scope)"))
        return record(kind, what, why, fix, run, None, "approval scope", source="supervisor", stays=kind == "policy",
                      when=entry.get("time") if isinstance(entry.get("time"), str) else None, redact=redact)
    if kind == "mode":
        mode = entry.get("mode") if isinstance(entry.get("mode"), str) else "strict"
        fix = (f"the security mode {mode} refuses it at run time: start again with a laxer --mode "
               f"(e.g. --mode medium{', and ' + option if option else ''}), or answer the run's questions instead")
        stays = False
    elif kind == "policy":
        fix, stays = "the administrator's policy refuses it: it stays refused (ask your administrator)", True
    else:
        fix, stays = "the run is refused rather than started without its checks: see the message; it stays refused", True
    return record(kind, what, why, fix, run, None, entry.get("target") or what, source="supervisor", stays=stays,
                  when=entry.get("time") if isinstance(entry.get("time"), str) else None, redact=redact)


def from_supervisor_memory(entry, run, redact):
    shed = entry.get("shed")
    if not isinstance(shed, list) or not shed:
        return None
    names = ", ".join(str(n) for n in shed[:8])
    rss = entry.get("rss")
    held = f" (it held {rss / (1 << 30):.1f} GiB)" if isinstance(rss, int) else ""
    return record("supervisor-memory", f"dropped: {names}", f"sandbox-ai's supervisor grew too large{held}",
                  "nothing to allow; if it happens again, please report memory.jsonl of the log directory", run,
                  target="supervisor", source="supervisor", when=entry.get("time"), redact=redact)


# -- muting, grouping, telling --------------------------------------------------------------------------

def defaults_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"


def check_mute(value, where=MUTE_KEY):
    """The kinds a safeguard_mute VALUE names (a list of kind names); a ValueError for anything else."""
    if not isinstance(value, list) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{where} is a list of kinds ({', '.join(KINDS)})")
    unknown = sorted(set(value) - set(KINDS))
    if unknown:
        raise ValueError(f"{where}: no kind {', '.join(unknown)}; the kinds are {', '.join(KINDS)}")
    return list(dict.fromkeys(value))


def muted(path=None):
    """The kinds muted in the defaults file (none without it, or with a damaged one)."""
    try:
        document = json.loads(Path(path or defaults_file()).read_text())
        return set(check_mute(document.get(MUTE_KEY, []))) if isinstance(document, dict) else set()
    except (OSError, ValueError):
        return set()


def describe(entry):
    """The body of a notification: what, why and what to do."""
    lines = [entry.get("what") or ""]
    if entry.get("process"):
        lines.append(f"Process: {entry['process']}")
    if entry.get("why"):
        lines.append(f"Why: {entry['why']}")
    if entry.get("fix"):
        lines.append(f"{'Stays refused' if entry.get('stays') else 'To allow it'}: {entry['fix']}")
    return "\n".join(line for line in lines if line)


def run_title(run, text):
    return f"sandbox-ai {run}: {text}" if run else f"sandbox-ai: {text}"


class Notifier:
    """Turns records into notifications ({"level", "title", "text", "kind", "run", "count", "entry": the record,
    or for the summary "kinds"}) for SEND:
    the first of a group (run, kind, target) at once, the ones that follow within WINDOW seconds counted and
    told as one when the window ends ("12 connections to x.example ... in the last minute"); at most RATE
    warnings per WINDOW (errors always), the rest summed up once the rate allows. MUTED: kinds never told
    (a set, or a callable giving one); records marked "announced" were told where they happened."""

    def __init__(self, send, muted=(), window=WINDOW, rate=RATE, clock=time.monotonic, where=None):
        self.send, self.window, self.rate, self.clock = send, window, rate, clock
        self.muted = muted if callable(muted) else (lambda kinds=frozenset(muted): kinds)
        self.where = where  # how to see them all, e.g. "sandbox-ai safeguards RUN"
        self.groups, self.sent, self.held, self.lock = {}, collections.deque(), {}, threading.RLock()

    def add(self, entry):
        kind = entry.get("kind")
        if kind not in KINDS or entry.get("announced"):
            return
        with self.lock:
            if kind in self.muted():
                return
            now = self.clock()
            key = (entry.get("run"), kind, entry.get("target") or entry.get("what"))
            group = self.groups.get(key)
            number = entry.get("count") if isinstance(entry.get("count"), int) and entry["count"] > 0 else 1
            if group is None:
                # NEW: told in the group's notification when the window ends; TOTAL: in the window, the first too.
                self.groups[key] = {"since": now, "new": 0, "total": number, "entry": entry}
                self.emit({"level": entry.get("level") or KINDS[kind][0],
                           "title": run_title(entry.get("run"), KINDS[kind][1]), "text": describe(entry),
                           "kind": kind, "run": entry.get("run"), "count": 1, "entry": entry}, now)
            else:
                group["new"] += number
                group["total"] += number
                group["entry"] = entry

    def flush(self, final=False):
        """Tell the groups whose window ended (all of them when FINAL) and what the rate held back."""
        with self.lock:
            now = self.clock()
            for key, group in list(self.groups.items()):
                if not final and now - group["since"] < self.window:
                    continue
                if group["new"]:
                    entry, kind = group["entry"], key[1]
                    text = KINDS[kind][2].format(count=group["total"], target=entry.get("target") or "one place")
                    body = "\n".join(line for line in (
                        f"Why: {entry['why']}" if entry.get("why") else "",
                        f"{'Stays refused' if entry.get('stays') else 'To allow it'}: {entry['fix']}"
                        if entry.get("fix") else "", f"All of them: {self.where}" if self.where else "") if line)
                    self.emit({"level": entry.get("level") or KINDS[kind][0], "title": run_title(key[0], text),
                               "text": body, "kind": kind, "run": key[0], "count": group["total"], "entry": entry},
                              now, final)
                    group.update(since=now, new=0, total=0)
                else:
                    del self.groups[key]
            if self.held and (final or self.room(now)):
                count, runs = sum(self.held.values()), {run for run, _ in self.held}
                kinds = sorted({kind for _, kind in self.held})
                run = next(iter(runs)) if len(runs) == 1 else None
                self.held = {}
                self.emit({"level": "warning", "title": run_title(run, f"{count} more safeguard events"),
                           "text": f"Kinds: {', '.join(kinds)}" + (f"\nAll of them: {self.where}" if self.where else
                                                                  "\nAll of them: sandbox-ai safeguards --all"),
                           "kind": "summary", "run": run, "count": count, "kinds": kinds}, now, True)

    def room(self, now):
        while self.sent and now - self.sent[0] >= self.window:
            self.sent.popleft()
        return len(self.sent) < self.rate

    def emit(self, notification, now, force=False):
        if notification["level"] != "error" and not force and not self.room(now):
            key = (notification["run"], notification["kind"])
            self.held[key] = self.held.get(key, 0) + notification["count"]
            return
        if notification["level"] != "error":
            self.room(now)
            self.sent.append(now)
        with contextlib.suppress(Exception):  # a notification that fails never stops the records
            self.send(notification)


# -- the launcher's watch -------------------------------------------------------------------------------

class Source:
    """One JSONL file followed through activity.LogFile (no links, bounded reads, a replaced file read anew)."""

    def __init__(self, directory, name, convert, from_start):
        self.log = module("activity").LogFile(directory, name, from_start)
        self.convert = convert

    def poll(self, budget, final=False):
        return [entry for _, entry in self.log.records(budget, final)]


class Questions:
    """The run's approvals directory: each question once, when it has its .done (or at the end)."""

    def __init__(self, directory):
        self.directory, self.seen = Path(directory), set()

    def poll(self, final=False):
        found = []
        try:
            names = sorted(p.name for p in self.directory.iterdir() if p.suffix == ".json" and not p.is_symlink())
        except OSError:
            return found
        for name in names[:2000]:
            ident = name[:-5]
            if ident in self.seen or not re.fullmatch(r"[0-9-]{1,40}", ident):
                continue
            done_path = self.directory / f"{ident}.done"
            try:
                question = json.loads((self.directory / name).read_text()[:1 << 16])
            except (OSError, ValueError):
                continue
            if not isinstance(question, dict):
                continue
            if question.get("kind") == "canary":
                done = None
            elif done_path.exists():
                try:
                    done = json.loads(done_path.read_text()[:1 << 16])
                except (OSError, ValueError):
                    done = None
            elif final:
                done = None
            else:
                continue
            self.seen.add(ident)
            found.append((question, done))
        return found


class Watch:
    """Collects the safeguards of one run while it runs (see the module's description): follows the run's logs
    in LOG_DIR and CONTROL_DIR, takes REPORT()s from the launcher itself (memory, out of memory), writes each
    record to safeguards.jsonl and hands it to a Notifier that calls NOTIFY(notification)."""

    def __init__(self, run, log_dir=None, control_dir=None, connection_log=None, notify=None, secrets=(),
                 muted_kinds=None, interval=INTERVAL, clock=time.monotonic, where=None):
        self.run, self.interval = run, interval
        self.redact = Redactor(secrets)
        log_dir = Path(log_dir).expanduser() if log_dir else None
        control_dir = Path(control_dir) if control_dir else None
        self.path = log_dir / FILE if log_dir else control_dir / FILE if control_dir else None
        self.sources = []
        if log_dir:
            self.add_source(log_dir, "egress-denied.jsonl", from_egress, False)
            self.add_source(log_dir, "tools.jsonl", lambda e, r, x: from_tool_call(e, r, x, "tool"), False)
            self.add_source(log_dir, "mcp-calls.jsonl", lambda e, r, x: from_tool_call(e, r, x, "mcp"), False)
            self.add_source(log_dir, "devices.jsonl", from_device, False)
            if connection_log:
                self.add_source(log_dir, connection_log, from_connection, True)
        if control_dir:
            self.add_source(control_dir, SUPERVISOR_FILE, from_supervisor, True)
            self.add_source(control_dir, SUPERVISOR_MEMORY_FILE, from_supervisor_memory, True)
        self.questions = Questions(control_dir / "approvals") if control_dir else None
        mute = muted_kinds if muted_kinds is not None else (lambda: muted())
        self.notifier = Notifier(notify or (lambda notification: None), mute, clock=clock,
                                 where=where or f"sandbox-ai safeguards {run_word(run)}")
        self.counts, self.lock, self.full = collections.Counter(), threading.Lock(), False
        self.stopping, self.thread, self.errors = threading.Event(), None, 0

    def add_source(self, directory, name, convert, from_start):
        with contextlib.suppress(Exception):
            self.sources.append(Source(directory, name, convert, from_start))

    def emit(self, records):
        records = [r for r in records if r]
        if not records:
            return
        with self.lock:
            for entry in records:
                self.counts[entry["kind"]] += entry.get("count", 1)
            if self.path is not None and not self.full:
                try:
                    self.full = not append(self.path, records)
                except OSError:
                    self.errors += 1
        for entry in records:
            self.notifier.add(entry)

    def report(self, kind, what, why, fix, process=None, target=None, stays=False, details=None):
        """A safeguard the launcher itself saw (the memory watch, the out-of-memory killer)."""
        with contextlib.suppress(Exception):
            self.emit([record(kind, what, why, fix, self.run, process, target, source="launcher", stays=stays,
                              redact=self.redact, details=details)])

    def poll(self, final=False):
        budget, found = [8 << 20], []
        for source in self.sources:
            try:
                for entry in source.poll(budget, final):
                    with contextlib.suppress(Exception):
                        found.append(source.convert(entry, self.run, self.redact))
            except Exception:  # noqa: BLE001 - one damaged log does not stop the others
                self.errors += 1
        if self.questions is not None:
            for question, done in self.questions.poll(final):
                with contextlib.suppress(Exception):
                    found.append(from_question(question, done, self.run, self.redact))
        found = [entry for entry in found if entry]
        found.sort(key=lambda entry: entry["time"])
        self.emit(found)
        self.notifier.flush(final)
        return len(found)

    def watch(self):
        while not self.stopping.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - the watch never takes the run down
                self.errors += 1
                if self.errors > 50:
                    return

    def start(self):
        self.thread = threading.Thread(target=self.watch, name="sandbox-ai-safeguards", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """The last look; the lines that tell the human what happened (none when nothing did)."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(self.interval + 5)
        with contextlib.suppress(Exception):
            self.poll(final=True)
        if not self.counts:
            return []
        parts = ", ".join(f"{count} {kind}" for kind, count in self.counts.most_common())
        return [f"safeguards: {sum(self.counts.values())} refused or ended ({parts}): "
                f"sandbox-ai safeguards {run_word(self.run)}"]


def desktop_sender(approvals=None):
    """SEND for a Notifier: notify-send through approvals.desktop_notification, urgency by level."""
    def send(notification):
        module_ = approvals or module("approvals")
        urgency = "critical" if notification["level"] == "error" else "normal"
        try:
            module_.desktop_notification(notification["title"], notification["text"], urgency=urgency)
        except TypeError:  # an approvals module without urgency
            module_.desktop_notification(notification["title"], notification["text"])
    return send


# -- reading them back (the CLI, the app, the web interface) --------------------------------------------

def logs_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"


def running():
    try:
        return module("net").running()
    except Exception:  # noqa: BLE001 - no registry: nothing runs
        return {}


def run_files(record):
    """The safeguards.jsonl files of a running sandbox's RECORD (its log directory's, else its control directory's)."""
    if isinstance(record.get("log_dir"), str) and record["log_dir"]:
        return [Path(record["log_dir"]) / FILE]
    if isinstance(record.get("directory"), str):
        return [Path(record["directory"]) / FILE]
    return []


def all_files(runs=None):
    """Every safeguards.jsonl: the running sandboxes' and the default log directories' (newest first)."""
    files = []
    for record in (running() if runs is None else runs).values():
        files += run_files(record)
    try:
        directories = sorted((p for p in logs_root().iterdir() if p.is_dir() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        directories = []
    files += [d / FILE for d in directories]
    files += mcp_files()
    unique = []
    for path in files:
        if path not in unique and path.is_file():
            unique.append(path)
    return unique


def mcp_files():
    """The safeguards.jsonl of every MCP server instance here (mcp.py's watch writes them into its state directory)."""
    root = logs_root().parent / "mcp"
    try:
        return sorted(p / FILE for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / FILE).is_file())
    except OSError:
        return []


def find_run(run):
    """The safeguards.jsonl of RUN: the running sandbox's, else a default log directory's that names it."""
    record = running().get(run)
    if record:
        return run_files(record)[0] if run_files(record) else None
    needle = json.dumps(run).encode()
    for path in all_files(runs={}):
        try:
            if needle in path.read_bytes():
                return path
        except OSError:
            continue
    return None


def run_id(name):
    try:
        return module("activity").run_id(name)
    except Exception:  # noqa: BLE001 - the name as it is
        return name


def matcher(run=None, kinds=None, since=None):
    kinds = set(kinds or ())

    def keep(entry):
        if run and entry.get("run") != run:
            return False
        if kinds and entry.get("kind") not in kinds:
            return False
        if since is not None:
            seconds = module("activity").epoch(entry.get("time"))
            if seconds is None or seconds < since:
                return False
        return True
    return keep


def safeguard_entry(line):
    try:
        entry = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return entry if isinstance(entry, dict) and entry.get("kind") in KINDS else None


def collect(files, keep=None, tail=0):
    found = []
    for path in files:
        found += read(path, keep, tail)  # (each file's last TAIL: the last TAIL of all are among them)
    unique = {}
    for entry in found:
        unique.setdefault(entry.get("id") or json.dumps(entry, sort_keys=True), entry)
    found = sorted(unique.values(), key=lambda e: str(e.get("time")))
    return found[-tail:] if tail else found


def show(entry, out=print, colour=None):
    t = module("textout")
    when = str(entry.get("time") or "")[:19].replace("T", " ")
    level = "fail" if entry.get("level") == "error" else "warn"
    count = f" ×{entry['count']}" if isinstance(entry.get("count"), int) and entry["count"] > 1 else ""
    out(f"{when}  {t.style(str(entry.get('kind')).ljust(16), level, colour)}  {printable(entry.get('run') or '-', 40)}"
        f"  {printable(entry.get('what'), 400)}{count}")
    if entry.get("process"):
        out(f"{' ' * 21}process: {printable(entry['process'], 300)}")
    if entry.get("why"):
        out(f"{' ' * 21}why:     {printable(entry['why'], 400)}")
    if entry.get("fix"):
        out(f"{' ' * 21}{'stays:  ' if entry.get('stays') else 'allow:  '} {printable(entry['fix'], 500)}")


def kinds_table(out, as_json):
    silenced = muted()
    rows = [{"kind": k, "level": v[0], "title": v[1], "muted": k in silenced} for k, v in KINDS.items()]
    if as_json:
        out(json.dumps(rows, indent=2))
        return 0
    t = module("textout")
    t.table(["KIND", "LEVEL", "WHAT", "MUTED"], [[r["kind"], r["level"], r["title"], "yes" if r["muted"] else ""]
                                                  for r in rows], out, indent=0)
    out(f"Mute kinds: sandbox-ai defaults set {MUTE_KEY} '[\"firewall\", \"dns\"]' (still recorded, never announced)")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai safeguards", description=__doc__.split("\n\n")[0],
                                epilog=f"Kinds: {', '.join(KINDS)}. `sandbox-ai safeguards kinds` lists them.")
    p.add_argument("run", nargs="?", help="A run id or the name of a start (default: the current project's runs)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-a", "--all", action="store_true", help="Every run: running ones and the default log directories")
    where.add_argument("-s", "--session", metavar="NAME", help="The runs of a named session")
    where.add_argument("-p", "--project", type=Path, metavar="PATH", help="Every run of a project (its default log directory)")
    where.add_argument("-l", "--log-dir", type=Path, metavar="DIR", help="Every run that logged into DIR")
    p.add_argument("-f", "--follow", action="store_true", help="Go on showing new records (Ctrl-C ends)")
    p.add_argument("-k", "--kind", action="append", metavar="KIND[,KIND]", help="Only these kinds; repeatable")
    p.add_argument("--since", metavar="TIME", help="Only records from TIME on (10m, 2h, 14:30, an ISO time)")
    p.add_argument("-n", "--tail", type=int, default=0, metavar="N", help="Only the last N records")
    p.add_argument("-j", "--json", action="store_true", help="JSON lines, one record each")
    return p


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


def locate(a):
    """([files], run filter) for the arguments A."""
    run = run_id(a.run) if a.run else None
    if a.all:
        return all_files(), run
    if a.log_dir:
        return [a.log_dir.expanduser() / FILE], run
    if a.session:
        activity = module("activity")
        return [activity.session_log_dir(a.session) / FILE], run
    if a.project:
        return [default_log_dir(a.project.expanduser().resolve()) / FILE], run
    if run:
        path = find_run(run)
        if path is None:
            raise ValueError(f"no safeguard records of a run {run!r} found (a run with --log-dir elsewhere: add --log-dir)")
        return [path], run
    return [default_log_dir(Path(os.getcwd()).resolve()) / FILE], None


def cli(argv, out=None):
    out = out or (lambda text: print(text, flush=True))
    if argv[:1] == ["kinds"]:
        return kinds_table(out, "--json" in argv[1:] or "-j" in argv[1:])
    a = parser().parse_args(argv)
    if a.tail < 0:
        raise ValueError("--tail must not be negative")
    since = module("activity").since_value(a.since) if a.since else None
    files, run = locate(a)
    keep = matcher(run, kinds_value(a.kind), since)
    entries = collect(files, keep, a.tail)
    for entry in entries:
        out(json.dumps(entry, ensure_ascii=False)) if a.json else show(entry, out)
    if not entries and not a.json and not a.follow:
        out(module("textout").style("no safeguard refused or ended anything" + (" that matches" if a.kind or since
                                                                                else ""), "muted"))
    if a.follow:
        follow(lambda: all_files() if a.all else files, keep,
               lambda entry: out(json.dumps(entry, ensure_ascii=False)) if a.json else show(entry, out))
    return 0


class Tail:
    """The records appended to files since the last look. A file seen at the first look is read from where it ends
    then (what happened before is not new); one that appears later, from its start (a run that started since)."""

    def __init__(self, limit=4 << 20):
        self.offsets, self.primed, self.limit = {}, False, limit

    def read(self, files):
        found = []
        for path in files:
            path = Path(path)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if path not in self.offsets:
                    self.offsets[path] = (info.st_ino, info.st_size if not self.primed else 0)
                inode, start = self.offsets[path]
                if inode != info.st_ino or info.st_size < start:
                    start = 0  # replaced or cut: from its start
                if info.st_size <= start:
                    self.offsets[path] = (info.st_ino, start)
                    continue
                os.lseek(fd, start, os.SEEK_SET)
                data = os.read(fd, min(info.st_size - start, self.limit))
            finally:
                os.close(fd)
            complete = data[:data.rfind(b"\n") + 1]
            self.offsets[path] = (info.st_ino, start + len(complete))
            for line in complete.splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("kind") in KINDS:
                    found.append(entry)
        self.primed = True
        return found

    def running(self, runs):
        """The new records of the running sandboxes RUNS ({run: record} of net.running())."""
        return self.read([path for record in runs.values() for path in run_files(record)])


def follow(files, keep, shown, stop=None, interval=1.0):
    """SHOWN(record) for every record appended to FILES() from now on (a file that appears later: from its
    start), until STOP is set or Ctrl-C."""
    tail = Tail()
    tail.read(files())
    stop = stop or threading.Event()
    while not stop.wait(interval):
        for entry in tail.read(files()):
            if keep(entry):
                shown(entry)


def main(argv=None):
    try:
        return cli(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            sys.stdout = open(os.devnull, "w")
        return 0
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai safeguards: {printable(exc, 600)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
