"""Session bridges (sandbox-ai bridge) as the app shows and makes them. No Qt.

A bridge lets one running agent session reach another through a broker on the host: message, ask, files (read-only or
writable, every write asked with its diff on the Approvals page) and watch (an observer, which may alert, and when
switched on hint, cut the peer's network or pause it). Only the human makes one: the app builds the words of
`sandbox-ai bridge add`, shows what `--dry-run` warns about (the egress direction above all) and asks before the add.

Here: the values of the New bridge… dialog and of the Board's Bridge part turned into those words, the paths checked as
the launcher checks them, which bridge touches which start, and the bridge events of the activity log in the
language of the app (the launcher writes them in English).
"""
from __future__ import annotations

import os
import re

from .files import printable
from .i18n import N_, language, tr

CAPABILITIES = ("message", "ask", "watch")  # and files:ro:PATH, files:rw:PATH
OBSERVER_ACTIONS = ("alert", "hint", "net-cut", "pause")
DEFAULT_ACTIONS = ("alert",)
ASK_MODES = (("first", N_("the first call of each tool")), ("each", N_("every call")),
             ("never", N_("never (writes are asked all the same)")))
# --limit KEY=VALUE: (key, title, the launcher's default, help)
LIMITS = (("rate", N_("Calls per minute"), "60", N_("calls per minute and side (1 to 600)")),
          ("size", N_("Largest message"), "16k", N_("one message, question or answer: bytes, or with k or m "
                                                    "(256 to 256k)")),
          ("ask-timeout", N_("Answer within (seconds)"), "300", N_("how long ask waits for the peer's answer "
                                                                   "(5 to 1800)")),
          ("daily", N_("Volume per day"), "64m", N_("bytes across it per day, or with k, m, g (64k to 16g)")))
LIVE_STATES = ("starting", "active", "suspended")
STATE_TITLES = {"starting": N_("starting"), "active": N_("active"), "suspended": N_("suspended"),
                "revoked": N_("revoked"), "ended": N_("ended"), "failed": N_("failed"), "planned": N_("planned")}
# Agents that read their MCP servers only when they start (bridge.py warns: only Claude Code takes one while it runs).
LIVE_MCP_AGENTS = ("claude",)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HOST = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # a host of `sandbox-ai hosts` (hosts.NAME)
# A session as `bridge add` takes it: NAME of this machine, or HOST:NAME of a host of `sandbox-ai hosts`.
PEER = re.compile(rf"(?:{HOST.pattern}:)?{NAME.pattern}")
UNTIL = re.compile(r"[0-9]{1,6}[smhd]|([01]?[0-9]|2[0-3]):[0-5][0-9]|[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+Z-]{4,25}")
SIZE = re.compile(r"[0-9]{1,12}[kKmMgG]?")


def relative_path(path, workspace=None):
    """PATH as `files:MODE:PATH` takes it: relative to the peer's workspace ("." for all of it). An absolute PATH
    inside WORKSPACE (a folder chosen with the picker) becomes relative. ValueError, in words, for what the launcher
    refuses: outside the workspace, '..', .git, a comma (it would split the capabilities), control characters."""
    text = str(path or "").strip() or "."
    if any(c < " " or c == "\x7f" for c in text) or len(text) > 4096:
        raise ValueError(tr("{path}: control characters and paths longer than 4096 characters are not taken",
                            path=printable(text)[:200]))
    if "," in text:
        raise ValueError(tr("{path}: a comma cannot be in a bridge's path", path=printable(text)[:200]))
    if text.startswith("/"):
        root = os.path.normpath(str(workspace)) if workspace else None
        if not root or not (text.rstrip("/") == root or text.startswith(root.rstrip("/") + "/")):
            raise ValueError(tr("{path} is not in the workspace of the other session ({workspace}): a path is "
                                "relative to it", path=printable(text)[:200], workspace=printable(workspace or "?")))
        text = text[len(root):].lstrip("/") or "."
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError(tr("{path}: '..' is never followed", path=printable(text)[:200]))
    if ".git" in parts:
        raise ValueError(tr("{path}: .git is never reached through a bridge", path=printable(text)[:200]))
    return "/".join(parts) or "."


def capabilities(message=False, ask=False, read=(), write=(), watch=False, workspace=None):
    """The --allow list of the choices: message, ask, files:ro:PATH (READ), files:rw:PATH (WRITE), watch; the paths
    made relative to WORKSPACE (the peer's). ValueError for a path the launcher would refuse, or no capability."""
    caps = [name for name, on in (("message", message), ("ask", ask)) if on]
    seen = set()
    for mode, paths in (("rw", write), ("ro", read)):
        for path in paths:
            if not str(path).strip():
                continue
            relative = relative_path(path, workspace)
            if relative not in seen:
                seen.add(relative)
                caps.append(f"files:{mode}:{relative}")
    if watch:
        caps.append("watch")
    if not caps:
        raise ValueError(tr("Choose at least one capability."))
    return caps


def until_problem(text):
    """Why --until TEXT is not taken (None: empty, or a form the launcher reads: 30m, 2h, 1d, HH:MM, an ISO time)."""
    text = str(text or "").strip()
    if not text or UNTIL.fullmatch(text):
        return None
    return tr("Until: 30m, 2h, 1d, HH:MM or an ISO time (empty: until either session ends)")


def limit_problem(key, value):
    """Why --limit KEY=VALUE is not taken (None: empty or a value of the right form; the launcher checks the range)."""
    value = str(value or "").strip()
    if not value:
        return None
    if key in ("rate", "ask-timeout"):
        return None if value.isdigit() and len(value) <= 6 else tr("{what}: a whole number", what=key)
    return None if SIZE.fullmatch(value) else tr("{what}: bytes, or with k, m or g", what=key)


def peer_host(name):
    """The host of the session NAME as `bridge add` takes it (HOST:NAME), or None for one of this machine."""
    host, sep, _ = str(name or "").partition(":")
    return host if sep and host != "local" else None


def plain(name):
    """NAME without "local:" (this machine's session as `bridge add` also takes it)."""
    text = str(name or "")
    return text[len("local:"):] if text.startswith("local:") else text


def on_host(name, host):
    """The session NAME as `bridge add` on this machine takes it when NAME is written as a board on HOST sees it (the
    board's host context): a bare NAME is a session of HOST (HOST:NAME; this machine's when HOST is this machine),
    local:NAME this machine's, OTHER:NAME a session of another host. HOST None or "local": this machine."""
    text = str(name or "").strip()
    if not text or ":" in text:
        return plain(text) if host in (None, "local") else text
    return text if host in (None, "local") else f"{host}:{text}"


def peer_groups(local, hosts, board_host=None):
    """[(host or None, [(value, entry)])] of the sessions a board's Bridge part may join, grouped by host: the board's
    host first, then this machine, then the other hosts by name. LOCAL: this machine's sessions (bridges_view
    .candidates); HOSTS: {host: that host's sessions as candidates(remote=True) names them (HOST:NAME)}. VALUE is the
    peer as the part keeps it in the board's host context (on_host): a bare NAME on the board's host, local:NAME for
    this machine's seen from a board on another host, HOST:NAME for another host's."""
    board_host = None if board_host in (None, "local") else board_host

    def value(host, name):
        name = name.split(":", 1)[1] if host and name.startswith(f"{host}:") else name
        if host == board_host:
            return name
        return f"local:{name}" if host is None else f"{host}:{name}"
    found = {None: [(value(None, entry["name"]), entry) for entry in local or ()]}
    for host, entries in (hosts or {}).items():
        if HOST.fullmatch(str(host)) and host != "local":
            found[host] = [(value(host, entry["name"]), entry) for entry in entries or ()]
    order = [board_host, None, *sorted(host for host in found if host not in (None, board_host))]
    return [(host, sorted(found[host], key=lambda item: item[0])) for host in dict.fromkeys(order) if found.get(host)]


def placement(a, b):
    """How the launcher joins A and B (bridge add's names): "here" (a session of this machine on either side),
    "there" (both on one other host: the bridge is made there) or "relayed" (two other hosts: this machine passes
    the frames between their halves)."""
    hosts = (peer_host(a), peer_host(b))
    if not all(hosts):
        return "here"
    return "there" if hosts[0] == hosts[1] else "relayed"


def placement_note(a, b):
    """What the dialog says of a bridge between two sessions of other hosts (None for one with a side here)."""
    kind = placement(a, b)
    if kind == "there":
        return tr("Both sessions run on {host}: the bridge is made there (no hop through this machine) and listed "
                  "here as {host}:ID.", host=printable(peer_host(a)))
    if kind == "relayed":
        return tr("The sessions run on {a} and {b}: each host runs its half of the bridge, and this machine passes "
                  "their messages on over two SSH connections (the hosts never connect to each other). When this "
                  "machine sleeps or loses a host, it re-establishes the bridge with the same grant once both "
                  "sessions still run (sandbox-ai 0.1.99 or later on both; else it ends); both need sandbox-ai 0.1.98 "
                  "or later.",
                  a=printable(peer_host(a)), b=printable(peer_host(b)))
    return None


def add_values(a, b, caps, both=False, until="", ask="first", watch=False, actions=DEFAULT_ACTIONS, limits=None):
    """{a, b, allow, both, until, ask, observer_actions, limits} for SandboxAI.bridge_add_args: the observer actions only
    with watch, the limits that were given (KEY: VALUE). A or B or both may be HOST:NAME, a session of another host
    (both of one host: made there; of two hosts: relayed by this machine). ValueError with every problem in words."""
    problems = []
    if not (isinstance(a, str) and PEER.fullmatch(a)) or not (isinstance(b, str) and PEER.fullmatch(b)):
        problems.append(tr("Choose both sessions."))
    elif plain(a) == plain(b):
        problems.append(tr("A bridge joins two different sessions."))
    if not caps:
        problems.append(tr("Choose at least one capability."))
    until_why = until_problem(until)
    if until_why:
        problems.append(until_why)
    given = []
    for key, value in (limits or {}).items():
        why = limit_problem(key, value)
        if why:
            problems.append(why)
        elif str(value or "").strip():
            given.append((key, str(value).strip()))
    if problems:
        raise ValueError("\n".join(problems))
    chosen = [action for action in OBSERVER_ACTIONS if action in (actions or ())]
    return {"a": a, "b": b, "allow": list(caps), "both": bool(both), "until": str(until or "").strip() or None,
            "ask": ask if ask in dict(ASK_MODES) else "first",
            "observer_actions": chosen if watch else None, "limits": given}


def live(rows):
    """The bridges of ROWS (bridge list --json) that still join their sessions: starting, active or suspended."""
    return [row for row in rows or () if isinstance(row, dict) and row.get("state") in LIVE_STATES]


ID = re.compile(r"[0-9a-f]{8}")


def from_host(rows, host):
    """The rows HOST's `bridge list --json` gave, as this machine shows them (bridge_remote.host_rows): "host": HOST;
    a session of HOST itself named HOST:NAME (a_host/b_host HOST); one of a third host as HOST named it."""
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not ID.fullmatch(row["id"]):
            continue
        row = dict(row)
        for side in ("a", "b"):
            if not isinstance(row.get(side), str):
                row[side] = "?"
            elif not row.get(f"{side}_host"):
                row[side], row[f"{side}_host"] = f"{host}:{row[side]}", host
        found.append({**row, "host": host})
    return found


def merged(local, remote):
    """This machine's bridges (LOCAL) and the hosts' (REMOTE: from_host's), each bridge once: a host's half of a
    bridge made elsewhere (remote_half) is left out when the bridge's own row is there (bridge_remote.merged_rows)."""
    rows = [dict(row) for row in local or () if isinstance(row, dict)]  # (this machine's: no "host")
    seen = {row.get("id") for row in rows}
    ordered = [row for row in remote if not row.get("remote_half")] + [row for row in remote if row.get("remote_half")]
    for row in ordered:
        if row.get("remote_half") and row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        rows.append(row)
    return rows


def qualified(row):
    """The bridge ROW as `sandbox-ai bridge show|revoke|resume` takes it: ID, or HOST:ID for one of another host."""
    return f"{row['host']}:{row['id']}" if row.get("host") else str(row.get("id"))


def local_run(row, side):
    """The run of bridge ROW's SIDE ("a" or "b") when it is a session of this machine; None for one on another host
    (a_host/b_host: a bridge across hosts), whose run id may well be a local run's too."""
    return None if row.get(f"{side}_host") else row.get(f"{side}_run")


def of_run(rows, run):
    """The live bridges of ROWS one of whose sides is the run RUN (a start's name in the run registry)."""
    if not run:
        return []
    return [row for row in live(rows) if run in (local_run(row, "a"), local_run(row, "b"))]


def peer_of(row, run):
    """The name of the other side of bridge ROW, seen from the run RUN (HOST:NAME for one on another host)."""
    return row.get("b") if local_run(row, "a") == run else row.get("a")


def direction_text(row, run):
    """What bridge ROW lets the run RUN do and what it lets its peer do, in words for a tooltip."""
    mine, theirs = (row.get("a_to_b"), row.get("b_to_a")) if local_run(row, "a") == run else \
        (row.get("b_to_a"), row.get("a_to_b"))
    peer = printable(peer_of(row, run) or "?")
    parts = []
    if mine and mine != "-":
        parts.append(tr("it may reach {peer}: {caps}", peer=peer, caps=printable(mine)))
    if theirs and theirs != "-":
        parts.append(tr("{peer} may reach it: {caps}", peer=peer, caps=printable(theirs)))
    return "; ".join(parts)


def marker(rows, run):
    """The text the State column adds for the bridges of the run RUN ("bridged with docs", "bridged (2)"), or ""."""
    found = of_run(rows, run)
    if not found:
        return ""
    if len(found) == 1:
        text = tr("bridged with {peer}", peer=printable(peer_of(found[0], run) or "?"))
        return text + (" " + tr("(suspended)") if found[0].get("state") == "suspended" else "")
    return tr("bridged ({count})", count=len(found))


def tooltip(rows, run):
    """One line per live bridge of the run RUN: its id, state, peer and what it allows each way."""
    lines = []
    for row in of_run(rows, run):
        lines.append(tr("Bridge {id} ({state}): {what}", id=printable(row.get("id")),
                        state=tr(STATE_TITLES.get(row.get("state"), "?")), what=direction_text(row, run)))
    return "\n".join(lines)


def peer_sees_tools(message=False, ask=False, hint=False, both=False):
    """Whether session B of a bridge (A reaches B) gets a tool of it besides info (bridge.py's sees_tools): an inbox
    for A's messages, questions or hints, or what --both lets it do. A watched B alone gets none: it need not see the
    bridge's MCP server."""
    return bool(message or ask or hint or both)


def mcp_at_start(agent):
    """True when AGENT reads its MCP servers only when it starts: its relay opens, but its agent does not see the
    bridge's MCP server (the launcher warns so; a restart does not keep a bridge)."""
    return bool(agent) and agent not in LIVE_MCP_AGENTS


# -- the activity log's bridge events, in the app's language --

# (event, pattern of bridge.py's English summary, the text with the same names, N_ for the translation)
# A session's name: NAME, or HOST:NAME for one on another host (a bridge across hosts).
_ID, _NAME = r"(?P<id>[0-9a-f]{8})", r"(?P<{}>(?:[a-z0-9][a-z0-9_-]{{0,31}}:)?[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}})"
EVENTS = (
    # (the other host's half says "(across hosts)" after the names)
    ("created", rf"bridge {_ID} {_NAME.format('a')} -> {_NAME.format('b')}(?: \(across hosts\))?: (?P<caps>\S+)"
                r"(?:; back: (?P<back>\S+))?", None),
    ("revoked", rf"bridge {_ID} revoked", N_("bridge {id} revoked")),
    ("resumed", rf"bridge {_ID} resumed by the human", N_("bridge {id} resumed by you")),
    ("suspended", rf"bridge {_ID} suspended: (?P<reason>.*)", N_("bridge {id} suspended: {reason}")),
    ("ended", rf"bridge {_ID} ended: (?P<reason>.*)", N_("bridge {id} ended: {reason}")),
    ("message", rf"bridge {_ID}: message from {_NAME.format('a')} to {_NAME.format('b')} \((?P<n>[0-9]+) bytes\)",
     N_("bridge {id}: message from {a} to {b} ({n} bytes)")),
    ("ask", rf"bridge {_ID}: question from {_NAME.format('a')} to {_NAME.format('b')}",
     N_("bridge {id}: question from {a} to {b}")),
    ("answer", rf"bridge {_ID}: {_NAME.format('a')} answered a question of {_NAME.format('b')}",
     N_("bridge {id}: {a} answered a question of {b}")),
    ("read", rf"bridge {_ID}: {_NAME.format('a')} read (?P<path>.+) of {_NAME.format('b')} \((?P<n>[0-9]+) bytes\)",
     N_("bridge {id}: {a} read {path} of {b} ({n} bytes)")),
    ("write-asked", rf"bridge {_ID}: {_NAME.format('a')} asks to write (?P<path>.+) of {_NAME.format('b')}",
     N_("bridge {id}: {a} asks to write {path} of {b}")),
    ("write-denied", rf"bridge {_ID}: write of (?P<path>.+) refused", N_("bridge {id}: write of {path} refused")),
    ("write", rf"bridge {_ID}: {_NAME.format('a')} wrote (?P<path>.+) of {_NAME.format('b')} \((?P<n>[0-9]+) bytes, "
              r"allowed by the human\)", N_("bridge {id}: {a} wrote {path} of {b} ({n} bytes, allowed by you)")),
    ("alert", rf"bridge {_ID}: alert \((?P<severity>[a-z]+)\) by {_NAME.format('a')} about {_NAME.format('b')}: "
              r"(?P<text>.*)", N_("bridge {id}: alert ({severity}) by {a} about {b}: {text}")),
    ("hint", rf"bridge {_ID}: hint from observer {_NAME.format('a')} to {_NAME.format('b')}",
     N_("bridge {id}: hint from observer {a} to {b}")),
    ("net-cut", rf"bridge {_ID}: observer {_NAME.format('a')} cut the network of {_NAME.format('b')}",
     N_("bridge {id}: observer {a} cut the network of {b}")),
    ("pause", rf"bridge {_ID}: observer {_NAME.format('a')} paused {_NAME.format('b')}",
     N_("bridge {id}: observer {a} paused {b}")),
)
_EVENTS = {event: (re.compile(pattern, re.DOTALL), text) for event, pattern, text in EVENTS}
# The reasons a bridge is suspended or ends (bridge.py), in the app's language too.
REASONS = (
    (rf"the kill switch of {_NAME.format('a')} holds \((?P<mode>[a-z+]+)\)",
     N_("the kill switch of {a} holds ({mode})")),
    (rf"a canary of {_NAME.format('a')} went off", N_("a canary of {a} went off")),
    (rf"observer {_NAME.format('a')} cut the network of {_NAME.format('b')}",
     N_("observer {a} cut the network of {b}")),
    (rf"{_NAME.format('a')} has ended", N_("{a} has ended")),
    (r"its time ran out \((?P<time>[0-9TZ:-]+)\)", N_("its time ran out ({time})")),
    (r"revoked by the human \(sandbox-ai bridge revoke\)", N_("revoked by you (sandbox-ai bridge revoke)")),
    (r"the broker was stopped", N_("the broker was stopped")),
    (r"it never finished starting", N_("it never finished starting")),
    (r"its broker had ended without closing it \(killed, or the machine restarted\)",
     N_("its broker had ended without closing it (killed, or the machine restarted)")),
    (rf"its broker is gone without closing it: sandbox-ai bridge revoke {_ID} closes what it left",
     N_("its broker is gone without closing it: sandbox-ai bridge revoke {id} closes what it left")),
    (r"nothing was heard from (?P<host>.{1,80}) for (?P<n>[0-9]+) s \(the connection is gone\)",
     N_("nothing was heard from {host} for {n} s (the connection is gone)")),
)
_REASONS = [(re.compile(pattern), text) for pattern, text in REASONS]
SEVERITIES = {"info": N_("info"), "warning": N_("warning"), "critical": N_("critical")}


def reason_text(reason):
    """A suspension's or an end's REASON in the app's language when it is one bridge.py writes; else as it is."""
    text = str(reason or "")
    if language() == "en":  # the launcher's own words
        return text
    for pattern, words in _REASONS:
        match = pattern.fullmatch(text)
        if match:
            return tr(words, **match.groupdict())
    return text


def event_summary(record):
    """The summary of an activity event of kind "bridge" in the app's language (bridge.py writes it in English):
    matched by its details' event and the launcher's own words; a summary it does not know stays as it is."""
    summary = str(record.get("summary") or "")
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    found = _EVENTS.get(details.get("event"))
    if record.get("kind") != "bridge" or found is None or language() == "en":  # English: the launcher's words
        return summary
    match = found[0].fullmatch(summary)
    if match is None:
        return summary
    values = match.groupdict()
    if details.get("event") == "created":
        text = tr("bridge {id} {a} -> {b}: {caps}", id=values["id"], a=values["a"], b=values["b"], caps=values["caps"])
        return text + ("; " + tr("back: {caps}", caps=values["back"]) if values.get("back") else "")
    if "reason" in values:
        values["reason"] = reason_text(values["reason"])
    if "severity" in values:
        values["severity"] = tr(SEVERITIES.get(values["severity"], values["severity"]))
    return tr(found[1], **values)
