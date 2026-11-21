#!/usr/bin/env python3
"""sandbox-ai bridge across hosts: the half of a bridge on the other host, and the link between the two halves.

  sandbox-ai bridge add A HOST:B --allow ...     (bridge.py) B is the session B on HOST, a host of `sandbox-ai hosts`
  sandbox-ai bridge-stub ID                      the other host's half: started over SSH by this host's broker, it
                                                 speaks only the bridge's frames on its stdin and stdout (not for
                                                 typing)

A bridge across hosts has a half on each host. Each half is a broker (bridge.Broker) for its own side: it listens on
its side's socket, attaches the bridge-PEER server to its side's running sandbox (so its supervisor's relay logs,
holds and taints as for any MCP server), and keeps and checks everything of its side itself - the files (the same
walker: no link followed, the granted paths only, .git, private trees and canaries never), the questions to the human
(asked on the host where the files are: `sandbox-ai approvals` there, and the app's approvals page, which lists every
host's), the limits, the exposure of its run, and its logs (bridge.jsonl, the run's activity log). A call of one side
that acts on the other (send_message, ask, files, watch, hint, cut, pause) goes across as a "call" frame; the other
half checks it against its own copy of the grant (given at its start, never widened later) and does it there; the
calling half checks it before it leaves (granted, the path below a granted path, sizes) and frames what comes back as
the peer's content. Neither half takes the other's word for what was granted: a call names no side and no grant,
only a tool and its arguments.

The link: the local broker runs `sandbox-ai bridge-stub ID` on HOST through `hosts.argv` (the host's SSH settings:
BatchMode, the host key checked strictly, its key and port), on a connection of its own with every forwarding off and
no escape character; nothing listens on either host. Frames are lines of JSON (at most FRAME_LIMIT bytes):
{"t": "hello"} once from the stub (its protocol and version: an older sandbox-ai is refused with the reason),
{"t": "req", "id", "op", "body"} and {"t": "res", "id", "body"} (op "open" once, then "call"), {"t": "ev", "op",
"body"} (state, resume, alert, end, ping). A frame that is too large, not JSON or of no known kind, or a flood of frames,
ends the bridge on both sides; so does the end of the connection (a dropped SSH connection ends it: the other half has
closed its side's relay by then, so there is nothing to resume; one that drops without a word: nothing heard from
the other half, not even its ping, for LINK_IDLE seconds), a revoke on either host, and the end of either run.

Both sessions on other hosts: two of one host (HOST:A HOST:B) are bridged there (`bridge add A B` on HOST over SSH:
delegate_add; the bridge is that host's own, HOST:ID here); sessions of two other hosts (HOST1:A HOST2:B) each get
their half on their own host, started by this machine, which relays the frames between the two (Relay): the halves
never connect to each other, this machine has no side, and each half checks every call against its own grant there.
A half asked to describe its session first ("describe", before the open: the relay tells each half the other's run)
says so in its hello ("features"); an older one is refused for a relayed bridge.

Standard library only; bridge.py binds itself here as `b`.
"""
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

b = None  # bridge.py (bridge.remote() sets it)

PROTOCOL = 1                  # the frames this version speaks (hello); another number is refused
REMOTE_SINCE = (0, 1, 97)     # the first sandbox-ai with `bridge-stub`
REMOTE_SINCE_TEXT = "0.1.97"
# A bridge between sessions of two other hosts (this machine relays it) asks each half to describe its side before the
# open (the run, the agent, the workspace's mode the other half is told of): the first sandbox-ai that does.
RELAY_SINCE = (0, 1, 98)
RELAY_SINCE_TEXT = "0.1.98"
# What this version's half does besides the frames of protocol 1 (its hello says so): "describe" its session before
# the open; "rejoin" a relayed bridge whose link was lost (this machine slept) - see rejoin_half.
FEATURES = ("describe", "rejoin")
FRAME_LIMIT = 4 << 20         # bytes of one frame (a read_file result: 256 KiB, escaped)
RESULT_LIMIT = 1 << 20        # characters of one result's text taken from the other half
ERROR_TEXT = 400              # characters of a refusal taken from the other half (escaped)
# What a call that carries no content of the peer says when the other half did it (made here: see forward).
DONE = {"send_message": "sent; {peer} reads it with its inbox",
        "write_file": "written: {path} ({size} bytes, allowed by the human on the other host)",
        "hint": "hint put into the inbox of {peer}",
        "cut_network": "the network of {peer} is cut (only its model API stays reachable); the human was alarmed and "
                       "the bridge is suspended",
        "pause_peer": "{peer} is paused; only the human resumes it"}
LINK_IN_FLIGHT = 16           # calls from the other half served at once
FRAME_RATE = 200              # frames a second the other half may send (twice that at once); more ends the bridge
HELLO_WAIT = 45.0             # seconds for the stub's hello (SSH's own connect timeout is 10 s)
OPEN_WAIT = 90.0              # seconds for the other half to attach its side
REMOTE_START_WAIT = 150.0    # seconds `bridge add` waits for both
EXPOSURE_ENTRIES = 64
HOST_ID = re.compile(r"[0-9a-f]{16}")
HOST_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ORIGIN = re.compile(r"(?:[0-9a-f]{16}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
EVENTS = ("state", "resume", "alert", "end", "ping")
# Each half greets the other every PING_INTERVAL seconds; one that hears nothing from the other for LINK_IDLE seconds
# ends the bridge. A connection that drops without a word (a laptop that sleeps) is seen by this machine's ssh after
# its keepalives, but sshd on the other host keeps the stub's end open for hours without ClientAliveInterval.
PING_INTERVAL = 15.0
LINK_IDLE = 75.0
# A bridge relayed by this machine survives its sleep: the wall clock running ahead of the monotonic one (which stops
# while the machine sleeps, on Linux and macOS) by more than LINK_IDLE means each half has given its link up by now;
# a link that is lost (its end without an "end" frame, or silence) means the same. The relay then re-establishes the
# bridge - both halves started again and rejoined with the same grant, once both sessions still run the same runs -
# trying for REJOIN_WAIT seconds (REJOIN_STEPS apart); a half rejoins a bridge whose link it lost at most
# REJOIN_WINDOW seconds ago, never one ended for another reason (revoked, its session ended, its time ran out).
SLEEP_JUMP = 30.0
REJOIN_WAIT = 900.0
REJOIN_STEPS = (2.0, 5.0, 10.0, 20.0, 30.0)
REJOIN_WINDOW = 24 * 3600
REJOIN_SETTLE = 10.0  # seconds a half asked to rejoin waits for its old half to end (then: "try again")
# Changes of state (a suspension, a resume) the other half may announce a minute: each is a notification, events and
# log lines here, and a resume lifts this half's suspension. More is a flood: the bridge ends.
STATE_CHANGES = 10
# What one side may do that acts on the other: done by the other's half, on its host. The rest (info, inbox,
# answer, alert) a half answers for its own side; the other half never asks for them.
PEER_TOOLS = ("send_message", "ask", "list_files", "read_file", "write_file", "watch_activity", "watch_llm",
              "watch_changes", "watch_terminal", "hint", "cut_network", "pause_peer")
# Put before the host's own ssh options (ssh takes the first value of each): the channel carries the bridge's frames
# and nothing else, and is a connection of its own (a shared master is recycled after a minute of quiet).
SSH_OPTIONS = ("-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
               "-o", "PermitLocalCommand=no", "-o", "Tunnel=no", "-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "RequestTTY=no", "-e", "none")


def shown(value, limit=300):
    return b.shown(value, limit)


def version():
    """This sandbox-ai's version (agent_sandbox.py's __version__)."""
    with contextlib.suppress(OSError):
        for line in (b.HERE / "agent_sandbox.py").read_text().splitlines():
            if line.startswith("__version__ = "):
                return line.split('"')[1]
    return "unknown"


def host_id():
    """This host's bridge identity (16 hex digits, made once, kept beside the bridges): how its runs are named among
    the origins of another host's exposure (hops and loops across hosts)."""
    path = b.state_root().parent / "bridge-host-id"
    with contextlib.suppress(OSError, UnicodeDecodeError):
        value = path.read_text().strip()
        if HOST_ID.fullmatch(value):
            return value
    b.private_dir(path.parent)
    value = secrets.token_hex(8)
    # Written whole under a name of its own, then linked to its place (fails when one is there): whoever reads the
    # identity - another thread asking at the same moment, a relay checking both hellos at once - never finds it empty.
    draft = path.with_name(f".bridge-host-id.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value + "\n")
        os.link(draft, path)
    except FileExistsError:
        found = path.read_text().strip()
        if HOST_ID.fullmatch(found):
            return found
        raise ValueError(f"{path}: not a bridge host identity (remove it: a new one is made)") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(draft)
    return value


def origin_name():
    """What the other host calls this one (its sessions: ORIGIN:NAME there): this host's name, cleaned."""
    name = re.sub(r"[^a-z0-9_-]", "-", socket.gethostname().split(".")[0].lower()).strip("-_")[:32]
    return name if HOST_NAME.fullmatch(name or "") else "host"


# --- names: HOST:NAME -----------------------------------------------------------------------------------------------

def parse_name(text):
    """(host, name) of a session given to `bridge add`: (None, NAME) for one of this machine (NAME, local:NAME),
    (HOST, NAME) for HOST:NAME, HOST one of `sandbox-ai hosts` (only those: never an SSH address)."""
    text = str(text)
    if ":" not in text:
        return None, text
    host, _, name = text.partition(":")
    if not HOST_NAME.fullmatch(host) or not b.RUN_NAME.fullmatch(name):
        raise ValueError(f"{shown(text, 80)}: a session on another host is HOST:NAME, HOST a name of sandbox-ai hosts "
                         "list (never an SSH address)")
    hosts = b.module("hosts")
    if host == hosts.LOCAL:
        return None, name
    if host not in hosts.load():
        raise ValueError(f"no host {shown(host, 40)} in your list (sandbox-ai hosts list): a bridge reaches only hosts "
                         "you added with sandbox-ai hosts add")
    return host, name


def too_old(host, found):
    return (f"{shown(host, 40)} runs sandbox-ai {shown(found, 40)}, older than {REMOTE_SINCE_TEXT}: a bridge across "
            f"hosts needs {REMOTE_SINCE_TEXT} or later there (its half of the bridge, sandbox-ai bridge-stub); update "
            f"it: sandbox-ai hosts update {shown(host, 40)}")


def relay_too_old(host, found):
    return (f"{shown(host, 40)} runs sandbox-ai {shown(found, 40)}: a bridge between sessions of two other hosts "
            f"needs {RELAY_SINCE_TEXT} or later on both (their halves describe their sessions to each other through "
            f"this machine); update it: sandbox-ai hosts update {shown(host, 40)}")


def remote_side(host, name, relay=False):
    """The side record of the session NAME on HOST before its half has answered (bridge.add): a host whose
    sandbox-ai is known to be older than 0.1.97 (RELAY: 0.1.98, for a bridge this machine relays between two other
    hosts) is refused here (the stub's hello is the last word)."""
    found, key = b.module("hosts").ask_version(host)
    if key is not None and tuple(key) < REMOTE_SINCE:
        raise ValueError(too_old(host, found) + " (as sandbox-ai hosts list last found it; after an update: "
                         "sandbox-ai hosts list --refresh)")
    if relay and key is not None and tuple(key) < RELAY_SINCE:
        raise ValueError(relay_too_old(host, found) + " (as sandbox-ai hosts list last found it; after an update: "
                         "sandbox-ai hosts list --refresh)")
    return {"host": host, "start_name": name, "name": f"{host}:{name}", "run": None, "remote": True, "agent": None,
            "workspace_mode": None, "host_id": None, "ready": False}


def network_of(side):
    """The network view (bridge.network_view) the other half reported for its side, or None."""
    view = side.get("network")
    if not isinstance(view, dict):
        return None
    return {"reach": set(view.get("reach") or []), "vpn": view.get("vpn"), "vpn_type": view.get("vpn_type")}


def network_report(side):
    view = b.network_view(side)
    if view is None:
        return None
    return {"reach": sorted(view["reach"])[:2000], "vpn": view.get("vpn"), "vpn_type": view.get("vpn_type")}


# --- what crosses: grants, paths, exposure --------------------------------------------------------------------------

def check_grant(value):
    """A grant as the other half sent it, taken apart and built anew (bridge.parse_caps's rules); ValueError."""
    if not isinstance(value, dict) or set(value) - {"message", "ask", "watch", "files", "actions"}:
        raise ValueError("a grant is {message, ask, watch, files, actions}")
    grant = b.empty_grant()
    for key in ("message", "ask", "watch"):
        if not isinstance(value.get(key, False), bool):
            raise ValueError(f"grant: {key} is true or false")
        grant[key] = value.get(key, False)
    files = value.get("files", [])
    if not isinstance(files, list) or len(files) > 32:
        raise ValueError("grant: files is a list of at most 32 paths")
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode"} or entry["mode"] not in ("ro", "rw"):
            raise ValueError("grant: a file entry is {path, mode ro|rw}")
        path = "/".join(b.path_parts(entry["path"])) or "."
        grant["files"].append({"path": path, "mode": entry["mode"]})
    actions = value.get("actions", [])
    if not isinstance(actions, list) or any(action not in b.OBSERVER_ACTIONS for action in actions):
        raise ValueError(f"grant: actions are some of {', '.join(b.OBSERVER_ACTIONS)}")
    if actions and not grant["watch"]:
        raise ValueError("grant: observer actions go with watch")
    grant["actions"] = [action for action in b.OBSERVER_ACTIONS if action in actions]
    return grant


def check_limits(value):
    if not isinstance(value, dict) or set(value) != set(b.DEFAULT_LIMITS):
        raise ValueError(f"limits are {', '.join(b.DEFAULT_LIMITS)}")
    for key, number in value.items():
        low, high = b.LIMIT_RANGES[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f"limit {key}: from {low} to {high}")
    return dict(value)


def granted_path(grant, value, write=False):
    """PATH's parts if GRANT covers it (bridge.Workspace.parts's rule, without the files: checked here before a call
    leaves, and again by the other half on its files); Refused."""
    try:
        parts = b.path_parts(value)
    except ValueError as exc:
        raise b.Refused(str(exc)) from None
    granted = [(entry["path"].split("/") if entry["path"] != "." else [], entry["mode"]) for entry in grant["files"]]
    if not any(parts[:len(prefix)] == prefix and (mode == "rw" or not write) for prefix, mode in granted):
        allowed = ", ".join(f"{'/'.join(p) or '.'} ({m})" for p, m in granted) or "nothing"
        raise b.Refused(f"{'/'.join(parts) or '.'} is not granted {'for writing ' if write else ''}on this bridge "
                        f"(granted: {allowed})")
    return parts


def export_exposure(side):
    """The exposure of SIDE's run (a run of this host) as it crosses: this host's runs named HOST-ID:RUN."""
    if b.is_remote(side):
        return {}
    with b.exposure_lock() as directory:
        origins = b.read_exposure(directory, side)
    mine = host_id()
    found = {}
    for run, entry in sorted(origins.items(), key=lambda item: -int((item[1] or {}).get("hops") or 0)):
        if len(found) >= EXPOSURE_ENTRIES or not isinstance(entry, dict):
            break
        found[run if ":" in run else f"{mine}:{run}"] = {"hops": int(entry.get("hops") or 0),
                                                          "bridges": list(entry.get("bridges") or [])[:16]}
    return found


def import_exposure(value):
    """An exposure the other half sent (export_exposure there), checked: this host's runs by their own names again.
    ValueError for one that is not well formed (the call is refused: hops cannot be told)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > EXPOSURE_ENTRIES:
        raise ValueError("the exposure it sent is not well formed")
    mine, found = host_id(), {}
    for key, entry in value.items():
        if not isinstance(key, str) or not ORIGIN.fullmatch(key) or not isinstance(entry, dict):
            raise ValueError("the exposure it sent is not well formed")
        hops, bridges = entry.get("hops"), entry.get("bridges", [])
        if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= b.MAX_HOPS + 2 or \
                not isinstance(bridges, list) or len(bridges) > 16 or \
                any(not isinstance(item, str) or not b.ID.fullmatch(item) for item in bridges):
            raise ValueError("the exposure it sent is not well formed")
        found[key[len(mine) + 1:] if key.startswith(mine + ":") else key] = {"hops": hops,
                                                                            "bridges": sorted(set(bridges))}
    return found


# --- the link -------------------------------------------------------------------------------------------------------

class ProtocolError(Exception):
    """The other half sent what the bridge's protocol does not have: the bridge ends."""


class Link:
    """Framed messages between the two halves over a pair of byte streams (SSH's stdio)."""

    def __init__(self, reader, writer, label):
        self.reader, self.writer, self.label = reader, writer, label
        self.write_lock, self.lock = threading.Lock(), threading.Lock()
        self.pending, self.counter = {}, 0
        self.closed, self.reason = threading.Event(), None
        self.lost = False  # closed because the connection was lost (its end, an error, silence): not by a word
        self.ending = None  # why this side ends it (end()): its "end" frame's failed write is no lost link
        self.slots = threading.BoundedSemaphore(LINK_IN_FLIGHT)
        self.bucket = [2.0 * FRAME_RATE, time.monotonic()]
        self.heard = time.monotonic()  # the last frame from the other half
        self.changes = []  # when the other half announced a change of state (monotonic), the last minute's
        # What a request and an event of the other half are handed to (default: a half's serve and on_event; this
        # machine's relay between two other hosts passes them on to the other link instead: Relay)
        self.serving, self.events = serve, on_event
        # Changes of state the other half announced (suspended, resumed), done one after the other in the order they
        # came, in a thread of their own (a resume followed at once by "still suspended" must not be overtaken)
        self.changes_due, self.changing = [], False

    def send(self, frame):
        """FRAME across; ValueError when it is too large, ConnectionError when the link is closed."""
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "surrogatepass")
        if len(data) > FRAME_LIMIT:
            raise ValueError(f"more than {FRAME_LIMIT} bytes")
        with self.write_lock:
            if self.closed.is_set():
                raise ConnectionError(self.reason)
            try:
                self.writer.write(data)
                self.writer.flush()
            except (OSError, ValueError) as exc:
                if self.ending is not None:
                    self.close(self.ending)  # (ended on purpose: never a lost link, never rejoined)
                else:
                    self.close(f"the connection to {self.label} was lost ({shown(exc, 100)})", lost=True)
                raise ConnectionError(self.reason) from None

    def read_frame(self):
        """The next frame (a dict), None at the end; ProtocolError for what is not one."""
        line = self.reader.readline(FRAME_LIMIT + 1)
        if not line:
            return None
        if len(line) > FRAME_LIMIT:
            raise ProtocolError(f"a frame of more than {FRAME_LIMIT} bytes")
        if not line.endswith(b"\n"):
            raise ProtocolError("a frame cut off")
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            raise ProtocolError("a frame that is not JSON") from None
        if not isinstance(frame, dict):
            raise ProtocolError("a frame that is not an object")
        return frame

    def first(self, timeout):
        """The first frame, read within TIMEOUT seconds (None: none came, or the connection ended)."""
        box = {}

        def read():
            try:
                box["frame"] = self.read_frame()
            except ProtocolError as exc:
                box["error"] = exc
            except (OSError, ValueError):
                box["frame"] = None
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if "error" in box:
            raise box["error"]
        return box.get("frame")

    def start(self, broker):
        self.heard = time.monotonic()
        threading.Thread(target=self.read_loop, args=(broker,), daemon=True).start()
        threading.Thread(target=self.greet, daemon=True).start()

    def greet(self):
        """A ping now and then (in a thread of its own: a write into a connection that is gone may block)."""
        while not self.closed.wait(PING_INTERVAL):
            self.event("ping", {})

    def silent(self):
        """Why the bridge ends when nothing came from the other half for LINK_IDLE seconds, else None."""
        quiet = time.monotonic() - self.heard
        if quiet < LINK_IDLE:
            return None
        return f"nothing was heard from {self.label} for {int(quiet)} s (the connection is gone)"

    def count_change(self):
        """One change of state the other half announced; ProtocolError beyond STATE_CHANGES a minute."""
        now = time.monotonic()
        self.changes = [moment for moment in self.changes if now - moment < 60] + [now]
        if len(self.changes) > STATE_CHANGES:
            raise ProtocolError(f"more than {STATE_CHANGES} changes of state a minute")

    def in_order(self, work):
        """WORK() after the changes announced before it, in a thread (the read of frames goes on meanwhile)."""
        with self.lock:
            self.changes_due.append(work)
            if self.changing:
                return
            self.changing = True

        def drain():
            while True:
                with self.lock:
                    if not self.changes_due:
                        self.changing = False
                        return
                    due = self.changes_due.pop(0)
                with contextlib.suppress(Exception):
                    due()
        threading.Thread(target=drain, daemon=True).start()

    def throttle(self):
        tokens, last = self.bucket
        now = time.monotonic()
        tokens = min(2.0 * FRAME_RATE, tokens + (now - last) * FRAME_RATE)
        if tokens < 1.0:
            raise ProtocolError(f"more than {FRAME_RATE} frames a second")
        self.bucket = [tokens - 1.0, now]

    def read_loop(self, broker):
        try:
            while not self.closed.is_set():
                frame = self.read_frame()
                if frame is None:
                    self.close(f"the connection to {self.label} was lost", lost=True)
                    return
                self.heard = time.monotonic()
                self.throttle()
                self.dispatch(broker, frame)
        except ProtocolError as exc:
            self.end(f"{self.label} broke the bridge's protocol ({exc})")
        except (OSError, ValueError, RuntimeError) as exc:
            self.close(f"the connection to {self.label} was lost ({shown(exc, 100)})", lost=True)

    def dispatch(self, broker, frame):
        kind, ident, op, body = frame.get("t"), frame.get("id"), frame.get("op"), frame.get("body")
        number = isinstance(ident, int) and not isinstance(ident, bool) and 0 < ident < 1 << 53
        if kind == "res":
            if not number or not isinstance(body, dict):
                raise ProtocolError("a malformed answer")
            with self.lock:
                waiter = self.pending.pop(ident, None)
            if waiter is not None:
                waiter[1] = body
                waiter[0].set()
            return  # (an answer to nothing asked, or to a call given up: dropped)
        if not isinstance(op, str) or not isinstance(body, dict):
            raise ProtocolError("a frame without its op and body")
        if kind == "req":
            if not number:
                raise ProtocolError("a call without its number")
            if not self.slots.acquire(blocking=False):
                self.reply(ident, {"ok": False, "text": f"more than {LINK_IN_FLIGHT} calls open across the bridge; "
                                                        "wait a little"})
                return
            try:
                threading.Thread(target=self.serve_one, args=(broker, ident, op, body), daemon=True).start()
            except RuntimeError:  # no thread to be had (the process limit): this call only, the reading goes on
                self.slots.release()
                self.reply(ident, {"ok": False, "text": "no thread to serve the call here (the process limit); try "
                                                        "again"})
        elif kind == "ev":
            if op not in EVENTS:
                raise ProtocolError(f"no event {shown(op, 20)}")
            if op in ("state", "resume"):
                self.count_change()
            self.events(broker, self, op, body)
        else:
            raise ProtocolError("a frame of no known kind")

    def serve_one(self, broker, ident, op, body):
        try:
            answer = self.serving(broker, op, body)
        except Exception as exc:  # noqa: BLE001 - one call's failure never ends the half
            answer = {"ok": False, "text": f"the call failed ({shown(exc, 200)})"}
        finally:
            self.slots.release()
        self.reply(ident, answer)

    def reply(self, ident, body):
        try:
            self.send({"t": "res", "id": ident, "body": body})
        except ValueError:
            with contextlib.suppress(ValueError, ConnectionError):
                self.send({"t": "res", "id": ident, "body": {"ok": False, "text": "the result is too large to cross "
                                                                                  "the bridge"}})
        except ConnectionError:
            pass

    def request(self, op, body, timeout):
        """The other half's answer (a dict) to OP; Refused when it does not come."""
        with self.lock:
            self.counter += 1
            ident = self.counter
            waiter = [threading.Event(), None]
            self.pending[ident] = waiter
        try:
            self.send({"t": "req", "id": ident, "op": op, "body": body})
            deadline = time.monotonic() + timeout
            while not waiter[0].wait(0.2):
                if self.closed.is_set():
                    break
                if time.monotonic() > deadline:
                    raise b.Refused(f"no answer from {self.label} within {int(timeout)} s")
        except ValueError:
            raise b.Refused("the call is too large to cross the bridge") from None
        except ConnectionError:
            pass
        finally:
            with self.lock:
                self.pending.pop(ident, None)
        if waiter[1] is None:
            raise b.Refused(f"{self.reason or 'the connection is closed'}; the bridge ends")
        return waiter[1]

    def event(self, op, body):
        with contextlib.suppress(ValueError, ConnectionError):
            self.send({"t": "ev", "op": op, "body": body})

    def end(self, reason, told=None):
        """This side ends the link for REASON: the other half is told (TOLD, default REASON), then it closes - never
        a lost link (only those are rejoined), whatever the write of the "end" frame does."""
        self.ending = reason
        self.event("end", {"reason": told if told is not None else reason})
        self.close(reason)

    def close(self, reason, lost=False):
        """The link ended for REASON; LOST: the connection went (its end, an error, silence), nobody ended it."""
        with self.lock:
            if self.closed.is_set():
                return
            self.reason, self.lost = reason, lost
            self.closed.set()
            waiters = list(self.pending.values())
        for waiter in waiters:
            waiter[0].set()

        def shut():  # (in a thread: a writer stuck on a full pipe must not hold up the end)
            with contextlib.suppress(OSError, ValueError):
                self.writer.close()
        threading.Thread(target=shut, daemon=True).start()


# --- what a half does with the other's frames -----------------------------------------------------------------------

def serve(broker, op, body):
    """A call of the other half: the tool its side called, done here for this host's side (bridge.Broker.call: the
    grant as this half has it, the limits, the human's questions, the files' checks). The frame names no side: a
    call from there is always the other side's."""
    if op != "call":
        return {"ok": False, "text": f"no operation {shown(op, 20)} now"}
    tool, arguments = body.get("tool"), body.get("arguments")
    if tool not in PEER_TOOLS:
        return {"ok": False, "text": f"{shown(tool, 40)} is not served across hosts (each half answers its own side's "
                                     "info, inbox and answer)"}
    if not isinstance(arguments, dict):
        return {"ok": False, "text": "arguments must be an object"}
    try:
        carried = import_exposure(body.get("exposure"))
    except ValueError as exc:
        return {"ok": False, "text": f"sandbox-ai bridge: {exc}"}
    # The side's calls in flight (bridge.Broker.in_flight): the other side's over the link count as over its sockets.
    side_slots = getattr(broker, "in_flight", {}).get(broker.remote)
    if side_slots is not None and not side_slots.acquire(blocking=False):
        return {"ok": False, "text": "too many calls open on this side of the bridge"}
    b.INCOMING.exposure = carried
    try:
        text, error = broker.call(broker.remote, tool, arguments)
    finally:
        b.INCOMING.exposure = None
        if side_slots is not None:
            side_slots.release()
    return {"ok": not error, "text": text, "exposure": export_exposure(broker.data[b.other(broker.remote)])}


def on_event(broker, link, op, body):
    far = broker.remote
    if op == "ping":
        return  # (heard: Link.heard)
    if op == "end":
        link.close(f"its half on {link.label} ended it: {shown(body.get('reason'), 300)}")
    elif op == "state" and body.get("state") == "suspended":
        reason = f"{shown(body.get('reason'), 300)} (on {link.label})"
        link.in_order(lambda: broker.suspend(reason, from_peer=True))
    elif op == "resume":
        link.in_order(lambda: peer_resumed(broker))
    elif op == "alert" and far is not None:
        grant = broker.grant(far)
        if not grant.get("watch") or "alert" not in (grant.get("actions") or []):
            return  # (the grant this half was given: the far side is no observer that may alert; not logged either,
            # or a flood of these would grow the bridge's log)
        severity = body.get("severity") if body.get("severity") in ("info", "warning", "critical") else "warning"
        text = "".join(c if c.isprintable() else " " for c in str(body.get("text") or ""))[:b.ALERT_TEXT]
        now = time.monotonic()
        with broker.lock:
            times = broker.alert_times[far]
            while times and now - times[0] > 3600:
                times.popleft()
            if len(times) >= b.ALERTS_PER_HOUR:
                return
            times.append(now)
        broker.raise_alert(far, severity, text, from_peer=True)


def peer_resumed(broker):
    """The human lifted the suspension on the other host: lifted here as well, unless this side's own cause holds
    (its kill switch); then the other half is told it stays suspended."""
    local = b.other(broker.remote)
    side = broker.data[local]
    with broker.lock:
        if broker.status != "suspended" or broker.stopping.is_set():
            return
    cut = b.module("net").cut_state(side["directory"])
    if cut:
        reason = f"the kill switch of {side['name']} still holds ({cut})"
        with broker.lock:
            broker.reason = reason
        if broker.link is not None:
            broker.link.event("state", {"state": "suspended", "reason": reason})
        return

    def apply(current):
        if current.get("state") == "suspended":
            current.update(state="active", reason=None, resumed=b.now_text(),
                           resumes=int(current.get("resumes") or 0) + 1,
                           canary_baseline={**(current.get("canary_baseline") or {}), local: b.canary_count(side)})
    with contextlib.suppress(Exception):
        current = b.change(broker.ident, apply)
        with broker.lock:
            broker.resumes = int(current.get("resumes") or 0)
            broker.data.update({key: current[key] for key in ("state", "reason", "canary_baseline") if key in current})
            broker.status, broker.reason = "active", None
            broker.changed.notify_all()
        b.both_events(current, f"bridge {broker.ident} resumed by the human (on the other host)", {"event": "resumed"})
        b.append_log(broker.ident, {"event": "resumed", "by": "the other host"})


def call_timeout(limits, tool):
    """How long a call of TOOL waits for the other half: its own waits there (the human's question, an answer) and
    more."""
    if tool == "ask":
        return int(limits["ask-timeout"]) + b.QUESTION_SECONDS + 60
    if tool == "write_file":
        return b.WRITE_QUESTION_SECONDS + 60
    return b.QUESTION_SECONDS + 120


def forward_timeout(broker, tool):
    return call_timeout(broker.data["limits"], tool)


def forward(broker, side, tool, arguments):
    """(text, bytes) of SIDE's call of TOOL that the other host's half does: checked here first (the grant as the
    human gave it here, the path, the sizes), the result checked when it comes back, framed as the peer's content and
    counted against this run's exposure (hops and loops)."""
    far = b.other(side)
    host = broker.data[far].get("host")
    link = broker.link
    if link is None or link.closed.is_set():
        raise b.Refused(f"{host} is not connected ({link.reason if link is not None else 'not yet'})")
    grant = broker.grant(side)
    if tool in ("list_files", "read_file", "write_file"):
        granted_path(grant, arguments.get("path", "."), write=tool == "write_file")
    if tool == "write_file" and len(arguments["content"].encode("utf-8", "surrogatepass")) > b.WRITE_LIMIT:
        raise b.Refused(f"the content is more than {b.WRITE_LIMIT} bytes; a write takes at most {b.WRITE_LIMIT}")
    for key, what in (("text", "the text"), ("question", "the question")):
        if isinstance(arguments.get(key), str) and tool != "write_file":
            broker.text_limit(arguments[key], what)
    sent = len(json.dumps(arguments, ensure_ascii=False))
    broker.count(sent)
    reply = link.request("call", {"tool": tool, "arguments": arguments, "exposure": export_exposure(broker.data[side])},
                         forward_timeout(broker, tool))
    ok, text = reply.get("ok"), reply.get("text")
    if not isinstance(ok, bool) or not isinstance(text, str) or len(text) > RESULT_LIMIT:
        link.end(f"{host} broke the bridge's protocol (an answer that is not well formed)",
                 "an answer that is not well formed")
        raise b.Refused(f"{host} sent an answer that is not well formed; the bridge ends")
    if not ok:
        raise b.Refused(f"refused on {host}: " + shown(text.removeprefix("sandbox-ai bridge: "), ERROR_TEXT))
    if tool in b.CONTENT_TOOLS:
        try:
            carried = import_exposure(reply.get("exposure"))
        except ValueError as exc:
            raise b.Refused(f"{host}: {exc}") from None
        b.transfer(broker.ident, broker.data[far], broker.data[side], carried=carried)
        if tool == "watch_terminal":
            text = b.terminal_text(text)  # the snapshot's cleaning made here again: never taken on the other's word
        broker.count(len(text))
        text = broker.framed(side, text)
    else:
        # A tool whose result carries no content of the peer: its words are made here, never taken from there (they
        # would reach the agent as sandbox-ai's own, untainted).
        text = DONE[tool].format(peer=shown(broker.data[far]["name"]), path=shown(arguments.get("path"), 300),
                                 size=len(str(arguments.get("content", "")).encode("utf-8", "surrogatepass")))
    broker.event(side, f"bridge {broker.ident}: {broker.data[side]['name']} called {tool} of "
                       f"{broker.data[far]['name']} (done on {host})", {"event": "remote-call", "tool": tool},
                 both=False)
    return text, sent + len(text)


# --- this host's broker: the link to the other host's half ----------------------------------------------------------

def ssh_command(host, ident):
    """The command that starts the half of bridge IDENT on HOST (a host of sandbox-ai hosts only)."""
    hosts = b.module("hosts")
    if host == hosts.LOCAL or host not in hosts.load():
        raise ValueError(f"no host {shown(host, 40)} in your list (sandbox-ai hosts list)")
    command = hosts.argv(host, ["bridge-stub", ident])
    return [command[0], *SSH_OPTIONS, *command[1:]]


def connect(host, ident):
    """The SSH process of the other half (its stdin and stdout carry the frames; its stderr: broker.log)."""
    log = os.open(b.bridge_dir(ident) / "broker.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW |
                  os.O_CLOEXEC, 0o600)
    try:
        return subprocess.Popen(ssh_command(host, ident), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                close_fds=True)
    finally:
        os.close(log)


def check_hello(hello, host):
    if not isinstance(hello, dict) or hello.get("t") != "hello":
        raise ValueError(f"{shown(host, 40)} did not answer as sandbox-ai {REMOTE_SINCE_TEXT} or later does (its "
                         "sandbox-ai is older, not found there, or SSH failed; see broker.log): a bridge across hosts "
                         f"needs sandbox-ai bridge-stub there; update it: sandbox-ai hosts update {shown(host, 40)}")
    found = hello.get("version") if isinstance(hello.get("version"), str) else "unknown"
    protocol = hello.get("protocol")
    if protocol != PROTOCOL:
        if isinstance(protocol, int) and not isinstance(protocol, bool) and protocol > PROTOCOL:
            raise ValueError(f"{shown(host, 40)} runs sandbox-ai {shown(found, 40)}, which speaks a newer bridge "
                             "protocol: update this machine's sandbox-ai (sandbox-ai update)")
        raise ValueError(too_old(host, found))
    if not isinstance(hello.get("host_id"), str) or not HOST_ID.fullmatch(hello["host_id"]):
        raise ValueError(f"{shown(host, 40)} sent a hello without its identity")
    if hello["host_id"] == host_id():
        raise ValueError(f"{shown(host, 40)} is this machine (the same bridge identity): bridge its sessions here")
    return hello


def check_ready(reply, host):
    """What the other half says of its side, checked (it is shown and used for warnings only: what is granted is
    this host's own record)."""
    side = reply.get("side")
    network = reply.get("network")
    notes = reply.get("notes", [])
    if not isinstance(side, dict) or not isinstance(side.get("name"), str) or not b.RUN_NAME.fullmatch(side["name"]) \
            or not isinstance(side.get("run"), str) or not b.RUN_NAME.fullmatch(side["run"]) \
            or not isinstance(side.get("server"), str) \
            or not re.fullmatch(r"bridge-[A-Za-z0-9_-]{1,25}", side["server"]) \
            or side.get("workspace_mode") not in ("ro", "rw", None) \
            or not isinstance(side.get("agent"), str) or not re.fullmatch(r"[a-z0-9_-]{1,20}", side["agent"]):
        raise ValueError(f"{shown(host, 40)} described its side in a way that is not well formed")
    if network is not None:
        reach = network.get("reach") if isinstance(network, dict) else None
        if not isinstance(reach, list) or len(reach) > 2000 or \
                any(not isinstance(item, str) or len(item) > 300 for item in reach) or \
                not isinstance(network.get("vpn"), (str, type(None))) or \
                not isinstance(network.get("vpn_type"), (str, type(None))):
            raise ValueError(f"{shown(host, 40)} described its network in a way that is not well formed")
        network = {"reach": [shown(item, 300) for item in reach], "vpn": shown(network["vpn"], 64)
                   if network.get("vpn") else None, "vpn_type": shown(network["vpn_type"], 20)
                   if network.get("vpn_type") else None}
    if not isinstance(notes, list):
        notes = []
    return {"run": side["run"], "agent": side["agent"], "workspace_mode": side.get("workspace_mode"),
            "server": side["server"], "network": network,
            "remote_notes": [shown(note, 300) for note in notes[:10] if isinstance(note, str)]}


def open_link(broker):
    """Start the other host's half (bridge add's broker, in a thread of its own): its hello, the open with the grant,
    what it says of its side into the record (ready); anything that fails: the bridge failed, with the reason."""
    far = broker.remote
    local = b.other(far)
    peer = broker.data[far]
    host = peer["host"]
    link = None
    try:
        process = (broker.connect or connect)(host, broker.ident)
        broker.process = process
        link = Link(process.stdout, process.stdin, host)
        try:
            hello = link.first(HELLO_WAIT)
        except ProtocolError:
            hello = None  # (an older sandbox-ai's usage text, a shell's greeting: no hello)
        hello = check_hello(hello, host)
        broker.link = link
        link.start(broker)
        mine = broker.data[local]
        reply = link.request("open", {
            "id": broker.ident, "side": far, "name": peer["start_name"], "origin": origin_name(),
            "peer": {"name": mine["name"], "run": mine["run"], "agent": mine.get("agent") or "unknown",
                     "host_id": host_id()},
            "grants": broker.data["grants"], "ask": broker.data["ask"], "limits": broker.data["limits"],
            "until": broker.data.get("until")}, OPEN_WAIT)
        if reply.get("ok") is not True:
            raise ValueError(f"{host}: {shown(reply.get('text'), 600)}")
        info = check_ready(reply, host)

        def apply(current):
            current[far].update(info, host_id=hello["host_id"], version=shown(hello.get("version"), 40), ready=True)
        broker.data = b.change(broker.ident, apply)
        b.append_log(broker.ident, {"event": "connected", "host": host, "version": shown(hello.get("version"), 40)})
    except Exception as exc:  # noqa: BLE001 - whatever it was, the bridge did not come up: said, and ended
        reason = shown(exc, 600) if isinstance(exc, (ValueError, b.Refused)) else \
            f"{host}: the other half could not be started ({shown(exc, 300)})"

        def failed(current):
            if current.get("state") in ("starting", "active", "suspended"):
                current.update(state="failed", reason=reason)
        with contextlib.suppress(Exception):
            b.change(broker.ident, failed)
        if link is not None:
            link.end(reason)
        broker.stopping.set()


def close_link(broker, reason):
    """The end of this half: the other one is told (it ends its side there) and the connection closed."""
    link = broker.link
    if link is not None and not link.closed.is_set():
        link.end(reason, shown(reason, 300))
    end_process(broker.process)


def end_process(process):
    """The SSH process of a link ended (its input closed first, so that ssh ends by itself); never raises."""
    if process is not None:
        def close_input():
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
        # (a frame stuck in a write to a half that reads nothing holds the stream: its close would wait as long, and
        # this half's sides would never be detached; ssh ended, the write fails and the close goes through)
        closer = threading.Thread(target=close_input, daemon=True)
        closer.start()
        closer.join(5)
        if closer.is_alive():
            with contextlib.suppress(OSError):
                process.kill()
            closer.join(5)
        try:
            process.wait(5)
        except (subprocess.TimeoutExpired, OSError):
            with contextlib.suppress(OSError):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                process.wait(5)


# --- this machine between two other hosts: the relay ---------------------------------------------------------------
#
# `bridge add HOST1:A HOST2:B` (two different hosts): each host runs its half (the stub, the same as for a bridge with
# one far side), each over an SSH connection of its own from this machine; this machine runs no half - no socket, no
# relay in a sandbox, no file, no question - only the Relay, which passes the frames of the one half to the other.
# The halves never connect to each other (neither host gets a way into the other), nothing listens anywhere.

RELAY_SLACK = 30  # seconds the relay waits for an answer beyond what the calling half waits (it gives up first)
# The calls of a side the relay passes on (or refuses and logs) a minute: twice the bridge's rate - each half keeps the
# rate itself; this only bounds what a half that ignores it costs this machine (its log grows no faster).
RELAY_RATE = 2


def check_described(reply, host):
    """What the half on HOST said of its session (describe), checked; ValueError."""
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        text = reply.get("text") if isinstance(reply, dict) else None
        raise ValueError(f"{shown(host, 40)}: {shown(text or 'its session could not be described', 600)}")
    side = reply.get("side")
    if not isinstance(side, dict) or not isinstance(side.get("name"), str) or not b.RUN_NAME.fullmatch(side["name"]) \
            or not isinstance(side.get("run"), str) or not b.RUN_NAME.fullmatch(side["run"]) \
            or not isinstance(side.get("agent"), str) or not re.fullmatch(r"[a-z0-9_-]{1,20}", side["agent"]) \
            or side.get("workspace_mode") not in ("ro", "rw", None):
        raise ValueError(f"{shown(host, 40)} described its session in a way that is not well formed")
    return {key: side.get(key) for key in ("name", "run", "agent", "workspace_mode")}


def relayed_answer(reply):
    """A half's answer to a call, as the relay passes it on: {ok, text, exposure} and nothing else; None when it is
    not well formed (the half broke the protocol)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool) or not isinstance(reply.get("text"), str) \
            or len(reply["text"]) > RESULT_LIMIT:
        return None
    exposure = reply.get("exposure")
    if exposure is not None and (not isinstance(exposure, dict) or len(exposure) > EXPOSURE_ENTRIES):
        return None
    return {"ok": reply["ok"], "text": reply["text"], **({"exposure": exposure} if exposure is not None else {})}


class Permanent(ValueError):
    """A reason a relayed bridge cannot be re-established however often it is tried (a session that ended or runs
    another run, a half that cannot or may not rejoin)."""


class Relay:
    """This machine's part of a bridge between sessions of two other hosts. It starts both halves (HOST1's for side
    a, HOST2's for side b), asks each to describe its session, opens each with the human's grant (the same for both;
    each half keeps its own copy and never takes a widening from a frame) and the other's session, then passes on:
    a call of side X to the other half only when X was granted that tool (and the path lies below a granted one)
    and the bridge is active - the halves check it again, each on its own host -; the answer back as it came, cut to
    {ok, text, exposure}; the events (suspended, resumed, alerts, the end) to the other half. It never makes a call
    of its own. Its record here is the bridge's (bridge list, show, revoke, resume); a revoke here, the end of either
    half or --until end it on both hosts. A link that is lost (it ends without a word, or goes silent) or this
    machine's sleep (SLEEP_JUMP) does not end it: both halves are started again and rejoined with the same grant
    while both sessions still run the same runs (reconnect; the record says "reconnecting" meanwhile), else it ends
    with the reason. Duck-typed as a bridge.Broker (start, record_broker, run, stopping) for bridge add and the tests.
    CLOCK: the wall clock, MONOTONIC: one that stops while the machine sleeps (the tests pass their own)."""

    def __init__(self, ident, clock=time.time, connect=None, monotonic=time.monotonic):
        self.ident, self.clock, self.connect, self.monotonic = ident, clock, connect, monotonic
        self.tick = None  # (wall, monotonic) at the last check: a jump between them is a sleep of this machine
        self.slept = None  # how long this machine slept (seconds), seen at the last check
        self.rejoin_due = None  # why the bridge is to be re-established (the run loop does it)
        self.dir = b.bridge_dir(ident)
        self.data = b.load(ident)
        self.links, self.processes = {}, {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        # Both halves opened (or the start given up: ready, not opened). A half may call or tell something as soon as
        # its own open is answered, before the other's is: it waits for this.
        self.opened, self.ready = False, threading.Event()
        self.status, self.reason = "active", None
        self.resumes = int(self.data.get("resumes") or 0)
        self.state_mtime = None
        self.closed = False
        self.calls = int(self.data.get("calls") or 0)
        self.volume = dict(self.data.get("volume") or {"day": None, "bytes": 0})
        self.dirty = False
        self.alert_times = {side: [] for side in b.SIDES}
        rate = int(self.data["limits"]["rate"]) * RELAY_RATE
        self.buckets = {side: [float(max(10, rate // 3)), time.monotonic()] for side in b.SIDES}
        self.over_rate = {side: [0, -60.0] for side in b.SIDES}  # calls refused for the rate, when last logged

    def host(self, side):
        return self.data[side]["host"]

    def record_broker(self):
        pid = os.getpid()
        start = b.module("net").process_start(pid)
        self.data = b.change(self.ident, lambda current: current.update(broker_pid=pid, broker_start=start))

    def start(self):
        threading.Thread(target=self.open, daemon=True).start()

    # -- the start: both halves up, described, opened --

    def each(self, work):
        """WORK(side) for both sides at once; {side: result}; the first failure raised (both done by then)."""
        results, errors = {}, {}

        def run(side):
            try:
                results[side] = work(side)
            except Exception as exc:  # noqa: BLE001 - said with its host
                errors[side] = exc
        threads = [threading.Thread(target=run, args=(side,), daemon=True) for side in b.SIDES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for side in b.SIDES:
            if side in errors:
                raise errors[side]
        return results

    def connect_half(self, side):
        host = self.host(side)
        process = (self.connect or connect)(host, self.ident)
        self.processes[side] = process
        link = Link(process.stdout, process.stdin, host)
        link.serving = lambda relay, op, body, s=side: relay.relay_call(s, op, body)
        link.events = lambda relay, _link, op, body, s=side: relay.relay_event(s, op, body)
        self.links[side] = link
        try:
            hello = link.first(HELLO_WAIT)
        except ProtocolError:
            hello = None
        hello = check_hello(hello, host)
        if "describe" not in (hello.get("features") if isinstance(hello.get("features"), list) else []):
            raise ValueError(relay_too_old(host, hello.get("version") if isinstance(hello.get("version"), str)
                                           else "unknown"))
        return hello

    def open(self):
        """Both halves started, described and opened (in a thread of its own); what fails: the bridge failed, with
        the reason, and both halves told."""
        try:
            self.establish()
        except Exception as exc:  # noqa: BLE001 - whatever it was, the bridge did not come up: said, and ended
            reason = shown(exc, 600) if isinstance(exc, (ValueError, b.Refused)) else \
                f"the halves could not be started ({shown(exc, 300)})"

            def failed(current):
                if current.get("state") in ("starting", "active", "suspended"):
                    current.update(state="failed", reason=reason)
            with contextlib.suppress(Exception):
                b.change(self.ident, failed)
            self.stopping.set()
            self.ready.set()

    def establish(self, rejoin=False):
        """Both halves started, described and opened - REJOIN: again, after the links were lost; each half must
        rejoin (its hello says it can) and each session must run the run it ran. Raises what failed."""
        hellos = self.each(self.connect_half)
        if rejoin:
            for side in b.SIDES:
                if "rejoin" not in (hellos[side].get("features") or []):
                    raise Permanent(f"{shown(self.host(side), 40)} runs sandbox-ai "
                                    f"{shown(hellos[side].get('version'), 40)}, which cannot rejoin a bridge; "
                                    f"update it: sandbox-ai hosts update {shown(self.host(side), 40)}")
        if hellos["a"]["host_id"] == hellos["b"]["host_id"]:
            raise ValueError(f"{shown(self.host('a'), 40)} and {shown(self.host('b'), 40)} are the same machine "
                             f"(one bridge identity): bridge its sessions there (sandbox-ai bridge add "
                             f"{shown(self.host('a'), 40)}:A {shown(self.host('a'), 40)}:B makes the bridge there)")
        for link in self.links.values():
            link.start(self)
        try:
            described = self.each(lambda side: check_described(self.links[side].request(
                "describe", {"name": self.data[side]["start_name"]}, OPEN_WAIT), self.host(side)))
        except ValueError as exc:
            raise (Permanent(str(exc)) if rejoin else exc) from None
        if rejoin:
            for side in b.SIDES:
                if described[side]["run"] != self.data[side].get("run"):
                    raise Permanent(f"{shown(self.data[side]['name'])} runs another run now (it was started "
                                    "again): the bridge is not re-established; add a new one")
        grants = self.data["grants"]
        for actor in b.SIDES:
            peer = b.other(actor)
            if any(entry["mode"] == "rw" for entry in grants[actor]["files"]) and \
                    described[peer]["workspace_mode"] != "rw":
                mode = "read-only for it" if described[peer]["workspace_mode"] == "ro" else "of unknown mode"
                raise ValueError(f"files:rw: the workspace of {shown(self.data[peer]['name'])} is {mode}; a bridge "
                                 "never writes where the session itself may not")

        def open_one(side):
            peer = b.other(side)
            reply = self.links[side].request("open", {
                "id": self.ident, "side": side, "name": self.data[side]["start_name"],
                "origin": self.host(peer), "relay": origin_name(),
                "peer": {"name": described[peer]["name"], "run": described[peer]["run"],
                         "agent": described[peer]["agent"], "host_id": hellos[peer]["host_id"]},
                "grants": grants, "ask": self.data["ask"], "limits": self.data["limits"],
                "until": self.data.get("until"), **({"rejoin": True} if rejoin else {})}, OPEN_WAIT)
            if reply.get("ok") is not True:
                text = f"{self.host(side)}: {shown(reply.get('text'), 600)}"
                raise ValueError(text) if not rejoin or "try again" in text else Permanent(text)
            return check_ready(reply, self.host(side))
        ready = self.each(open_one)

        def apply(current):
            for side in b.SIDES:
                current[side].update(ready[side], host_id=hellos[side]["host_id"],
                                     version=shown(hellos[side].get("version"), 40), ready=True)
            if rejoin:
                current.update(link=None, reconnects=int(current.get("reconnects") or 0) + 1,
                               reconnected=b.now_text())
        self.data = b.change(self.ident, apply)
        self.opened = True
        self.ready.set()
        b.append_log(self.ident, {"event": "reconnected" if rejoin else "connected", "relay": True,
                                  "hosts": [self.host(side) for side in b.SIDES]})

    # -- after a sleep of this machine (or a lost link): both halves again --

    def drop_links(self, reason):
        """Both links closed without a word (each half sees its connection end: a lost link, which it may rejoin)."""
        for link in list(self.links.values()):
            link.close(reason, lost=True)
        for process in list(self.processes.values()):
            end_process(process)
        self.links, self.processes = {}, {}

    def reconnect(self, reason):
        """The bridge re-established after its links were lost for REASON: both halves started and rejoined, tried
        for REJOIN_WAIT seconds; the record says "reconnecting" meanwhile. None once it runs again, else why it
        ends (a session that ended or was started again, a half that cannot or may not rejoin, the time, a revoke)."""
        with self.lock:
            self.opened = False
            self.ready = threading.Event()
        self.drop_links(reason)
        since = b.now_text()
        with contextlib.suppress(Exception):
            self.data = b.change(self.ident, lambda current: current.update(
                link={"state": "reconnecting", "since": since, "reason": shown(reason, 300)}))
        b.append_log(self.ident, {"event": "reconnecting", "reason": shown(reason, 300)})
        deadline = self.monotonic() + REJOIN_WAIT
        attempt, last = 0, reason
        while not self.stopping.is_set():
            try:
                current = b.load(self.ident)
            except ValueError:
                return "its record cannot be read"
            if current.get("state") not in b.LIVE_STATES:
                return current.get("reason") or current.get("state")
            until = b.epoch(self.data.get("until")) if self.data.get("until") else None
            if until is not None and self.clock() >= until:
                return f"its time ran out ({self.data['until']})"
            try:
                self.establish(rejoin=True)
                b.notify(f"sandbox-ai bridge {self.ident} re-established",
                         f"its link was lost ({shown(reason, 200)}); both halves rejoined with the same grant")
                return None
            except Permanent as exc:
                self.drop_links(str(exc))
                return f"it could not be re-established after its link was lost: {shown(exc, 400)}"
            except Exception as exc:  # noqa: BLE001 - a host not reachable yet (the network after a wake): again
                last = shown(exc, 300)
                self.drop_links(last)
            if self.monotonic() >= deadline:
                return f"it could not be re-established within {int(REJOIN_WAIT)} s after its link was lost: {last}"
            self.stopping.wait(REJOIN_STEPS[min(attempt, len(REJOIN_STEPS) - 1)])
            attempt += 1
        return "the relay was stopped"

    # -- passing on --

    def relay_call(self, side, op, body):
        """A call of SIDE's half (its session called TOOL of the other's): passed on to the other half when SIDE was
        granted it; else refused here (and logged), never sent on."""
        tool, arguments = body.get("tool"), body.get("arguments")
        if not self.take(side):
            return {"ok": False, "text": "sandbox-ai bridge (relay): too many calls a minute on this bridge; wait a "
                                         "little"}
        try:
            if op != "call":
                raise b.Refused(f"no operation {shown(op, 20)} now")
            if not self.ready.wait(OPEN_WAIT) or not self.opened:
                raise b.Refused("the bridge did not start")
            if tool not in PEER_TOOLS:
                raise b.Refused(f"{shown(tool, 40)} is not served across hosts")
            if not isinstance(arguments, dict):
                raise b.Refused("arguments must be an object")
            grant = self.data["grants"][side]
            if tool not in b.tools_of(self.data["grants"], side):
                raise b.Refused(f"{tool} is not granted on this bridge (only the human widens a bridge)")
            if tool in ("list_files", "read_file", "write_file"):
                granted_path(grant, arguments.get("path", "."), write=tool == "write_file")
            with self.lock:
                if self.status != "active":
                    raise b.Refused(f"the bridge is suspended ({shown(self.reason, 300)}): only the human lifts it "
                                    f"(sandbox-ai bridge resume {self.ident})")
            target = self.links[b.other(side)]
            started = time.monotonic()
            reply = target.request("call", {"tool": tool, "arguments": arguments,
                                            "exposure": body.get("exposure") if isinstance(body.get("exposure"), dict)
                                            else {}},
                                   call_timeout(self.data["limits"], tool) + RELAY_SLACK)
            answer = relayed_answer(reply)
            if answer is None:
                reason = f"{self.host(b.other(side))} broke the bridge's protocol (an answer that is not well formed)"
                target.end(reason, "an answer that is not well formed")
                raise b.Refused(f"{self.host(b.other(side))} sent an answer that is not well formed; the bridge ends")
        except b.Refused as exc:
            self.log(side, tool, False, 0, str(exc))
            return {"ok": False, "text": f"sandbox-ai bridge (relay): {shown(exc, ERROR_TEXT)}"}
        moved = len(json.dumps(arguments, ensure_ascii=False)) + len(answer["text"])
        self.log(side, tool, answer["ok"], moved, None, int((time.monotonic() - started) * 1000))
        return answer

    def take(self, side):
        """One call of SIDE against the relay's rate (RELAY_RATE times the bridge's); False over it, logged once a
        minute at most (with how many there were)."""
        rate = int(self.data["limits"]["rate"]) * RELAY_RATE
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets[side]
            tokens = min(float(max(10, rate // 3)), tokens + (now - last) * rate / 60.0)
            if tokens >= 1.0:
                self.buckets[side] = [tokens - 1.0, now]
                return True
            self.buckets[side] = [tokens, now]
            count, logged = self.over_rate[side]
            self.over_rate[side] = [count + 1, logged]
            if now - logged < 60:
                return False
            self.over_rate[side] = [0, now]
        b.append_log(self.ident, {"side": side, "event": "rate", "refused": count + 1})
        return False

    def log(self, side, tool, ok, moved, refused=None, ms=0):
        """One call passed on (or refused here): its side, tool, outcome and size, never its content."""
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        with self.lock:
            self.calls += 1
            if self.volume.get("day") != day:
                self.volume = {"day": day, "bytes": 0}
            self.volume["bytes"] = int(self.volume.get("bytes") or 0) + moved
            self.dirty = True
        b.append_log(self.ident, {"side": side, "tool": shown(tool, 60), "relayed": refused is None,
                                  "error": not ok, "bytes": moved, "ms": ms,
                                  **({"result": shown(refused, 200)} if refused else {})})

    def relay_event(self, side, op, body):
        """An event of SIDE's half: the other half told of it (and this machine's record kept)."""
        host, other_link = self.host(side), self.links.get(b.other(side))
        if op == "ping":
            return
        if not self.opened and op != "end":
            # (told as soon as its own open was answered: passed on once the other's is too - in a thread, so that
            # the read of this half's frames goes on meanwhile; dropped when the start is given up)
            def later():
                if self.ready.wait(OPEN_WAIT) and self.opened:
                    self.relay_event(side, op, body)
            threading.Thread(target=later, daemon=True).start()
            return
        if op == "end":
            reason = f"its half on {host} ended it: {shown(body.get('reason'), 300)}"
            self.links[side].close(reason)
            return
        if op == "state" and body.get("state") == "suspended":
            reason = f"{host}: {shown(body.get('reason'), 300)}"
            if other_link is not None:
                other_link.event("state", {"state": "suspended", "reason": reason})
            self.set_state("suspended", reason)
        elif op == "resume":
            if other_link is not None:
                other_link.event("resume", {})
            self.set_state("active", None, by=host)
        elif op == "alert":
            grant = self.data["grants"][side]
            if not grant.get("watch") or "alert" not in (grant.get("actions") or []):
                return  # (no observer that may alert: dropped, as the other half would drop it)
            now = time.monotonic()
            with self.lock:
                times = self.alert_times[side] = [t for t in self.alert_times[side] if now - t < 3600]
                if len(times) >= b.ALERTS_PER_HOUR:
                    return
                times.append(now)
            severity = body.get("severity") if body.get("severity") in ("info", "warning", "critical") else "warning"
            text = "".join(c if c.isprintable() else " " for c in str(body.get("text") or ""))[:b.ALERT_TEXT]
            if other_link is not None:
                other_link.event("alert", {"severity": severity, "text": text})
            observer, peer = self.data[side]["name"], self.data[b.other(side)]["name"]
            entry = {"time": b.now_text(), "from": observer, "about": peer, "severity": severity, "text": text}

            def apply(current):
                current["alerts"] = (current.get("alerts") or [])[-49:] + [entry]
            with contextlib.suppress(Exception):
                self.data = b.change(self.ident, apply)
            b.notify(f"sandbox-ai bridge {self.ident}: alert about {shown(peer, 60)}",
                     f"observer {shown(observer, 60)} ({severity}): {shown(text, b.ALERT_TEXT)}",
                     alarm=severity == "critical")
            b.append_log(self.ident, {"event": "alert", "side": side, "severity": severity})

    def set_state(self, state, reason, by=None):
        """The bridge suspended (REASON) or active again (lifted on host BY), here as the halves have it."""
        with self.lock:
            if self.stopping.is_set() or self.status == state:
                return
            self.status, self.reason = state, reason

        def apply(current):
            if state == "suspended" and current.get("state") == "active":
                current.update(state="suspended", reason=reason, suspended=b.now_text())
            elif state == "active" and current.get("state") == "suspended":
                current.update(state="active", reason=None, resumed=b.now_text())
        with contextlib.suppress(Exception):
            self.data = b.change(self.ident, apply)
            self.state_mtime = None
        b.append_log(self.ident, {"event": state if state == "suspended" else "resumed",
                                  **({"reason": reason} if reason else {}), **({"by": by} if by else {})})
        if state == "suspended":
            b.notify(f"sandbox-ai bridge {self.ident} suspended", f"{shown(reason, 300)}\nsandbox-ai bridge resume "
                                                                  f"{self.ident} (or revoke {self.ident})")

    # -- its life --

    def check(self):
        """One look at the record and both links; the reason to end, or None (REJOIN_DUE set: its links were lost,
        the bridge is to be re-established)."""
        wall, mono = self.clock(), self.monotonic()
        if self.tick is not None:
            jump = (wall - self.tick[0]) - (mono - self.tick[1])
            if jump > SLEEP_JUMP:
                self.slept = jump
                b.append_log(self.ident, {"event": "slept", "seconds": int(jump)})
        self.tick = (wall, mono)
        try:
            mtime = (self.dir / "bridge.json").stat().st_mtime_ns
        except OSError:
            return "its record is gone"
        if mtime != self.state_mtime:
            self.state_mtime = mtime
            try:
                current = b.load(self.ident)
            except ValueError:
                return "its record cannot be read"
            if current.get("state") in ("revoked", "ended", "failed"):
                return current.get("reason") or current.get("state")
            if current.get("state") == "starting" and (b.epoch(current.get("created")) or 0) + 120 < time.time():
                return "it never finished starting"
            resumed = False
            with self.lock:
                resumes = int(current.get("resumes") or 0)
                if resumes > self.resumes:
                    self.resumes = resumes
                    if current.get("state") == "active" and self.status == "suspended":
                        self.status, self.reason = "active", None  # the human lifted it here (bridge resume)
                        resumed = True
            if resumed:
                for link in self.links.values():
                    link.event("resume", {})  # (each half lifts it, unless its own cause still holds: it says so)
        slept, self.slept = self.slept, None
        if self.opened and slept is not None and slept > LINK_IDLE:
            # (each half has given its link up by now: re-established at once, without waiting for silence)
            self.rejoin_due = f"this machine slept for {int(slept)} s"
            return None
        for side, link in list(self.links.items()):
            if self.opened and not link.closed.is_set():
                quiet = link.silent()
                if quiet:
                    link.close(quiet, lost=True)
            if link.closed.is_set():
                if self.opened and link.lost:
                    self.rejoin_due = link.reason
                    return None
                return link.reason
        until = b.epoch(self.data.get("until")) if self.data.get("until") else None
        if until is not None and self.clock() >= until:
            return f"its time ran out ({self.data['until']})"
        if self.dirty:
            with self.lock:
                volume, calls, self.dirty = dict(self.volume), self.calls, False
            with contextlib.suppress(Exception):
                self.data = b.change(self.ident, lambda record: record.update(volume=volume, calls=calls))
        return None

    def run(self, interval=1.0):
        reason = None
        try:
            while not self.stopping.is_set():
                reason = self.check()
                if reason:
                    break
                if self.rejoin_due:
                    due, self.rejoin_due = self.rejoin_due, None
                    reason = self.reconnect(due)
                    if reason:
                        break
                    self.tick = None
                    continue
                self.stopping.wait(interval)
        finally:
            self.close(reason or "the relay was stopped")

    def close(self, reason):
        """Both halves told why (they end their sides there) and both connections closed; the record ended."""
        with self.lock:
            already, self.closed = self.closed, True
            self.stopping.set()
            self.ready.set()
        if already:
            return
        try:
            current = b.load(self.ident)
        except ValueError:
            current = self.data
        told = current.get("state") in ("revoked", "failed") and current.get("reason") or reason
        for side, link in list(self.links.items()):
            if not link.closed.is_set():
                link.end(told, shown(told, 300))
        for process in list(self.processes.values()):
            end_process(process)
        final = {"volume": dict(self.volume), "calls": self.calls}

        def apply(record):
            record.update(final)
            if record.get("state") in b.LIVE_STATES:
                record.update(state="ended", reason=reason, ended=b.now_text())
        with contextlib.suppress(Exception):
            current = b.change(self.ident, apply)
        if current.get("state") == "ended":
            b.append_log(self.ident, {"event": "ended", "reason": reason})


# --- bridges of other hosts: made, listed, shown, revoked and resumed there ------------------------------------------

DELEGATE_WAIT = 180   # seconds `bridge add` on another host may take (its broker, both relays attached)
HOSTS_WAIT = 60       # seconds one host may take to list its bridges (bridge list --hosts)
QUALIFIED_ID = re.compile(r"(?:(?P<host>[a-z0-9][a-z0-9_-]{0,31}):)?(?P<id>[0-9a-f]{8})")


def delegate_add(host, a_name, b_name, allow, both=False, until=None, ask="first", observer_actions=None, limits=(),
                 out=print, dry_run=False):
    """`bridge add HOST:A HOST:B ...`: both sessions run on HOST, so the bridge is made there (`sandbox-ai bridge add
    A B ...` on HOST over SSH, as `sandbox-ai hosts run HOST -- bridge add` would): one broker on HOST, nothing of it
    on this machine - no hop through it, and it lasts while HOST runs, whatever this machine does. What is given is
    checked here first (as `bridge add` checks it there again); --until goes as the time it names here (an HH:MM of
    this machine's clock, not HOST's). Its record (as bridge add --json says it there) with "host": HOST and the
    sessions named HOST:NAME."""
    hosts = b.module("hosts")
    found, key = hosts.ask_version(host)
    if key is not None and tuple(key) < REMOTE_SINCE:
        raise ValueError(f"{shown(host, 40)} runs sandbox-ai {shown(found, 40)}, which has no bridges: they came with "
                         f"{REMOTE_SINCE_TEXT}; update it: sandbox-ai hosts update {shown(host, 40)}")
    if a_name == b_name:
        raise ValueError("a bridge joins two different sessions")
    if ask not in b.ASK_POLICIES:
        raise ValueError(f"--ask: {', '.join(b.ASK_POLICIES)}")
    grant = b.parse_caps(allow)  # (paths relative, or below /work: its workspace is known there only)
    actions = b.parse_actions(observer_actions, grant["watch"])
    b.parse_limits(limits)
    args = ["bridge", "add", a_name, b_name, "--allow=" + ",".join(
        [name for name in ("message", "ask", "watch") if grant[name]] +
        [f"files:{entry['mode']}:{entry['path']}" for entry in grant["files"]])]
    if both:
        args.append("--both")
    if until:
        args.append("--until=" + b.now_text(b.parse_until(until)))
    args.append(f"--ask={ask}")
    if observer_actions is not None:
        args.append("--observer-actions=" + ",".join(actions))
    args += [f"--limit={value}" for value in limits or ()]
    args += ["--dry-run"] if dry_run else []
    args.append("--json")
    status, said, err = hosts.run(host, args, timeout=DELEGATE_WAIT)
    try:
        data = json.loads(said) if status == 0 else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict) or not (data.get("id") is None if dry_run else
                                          isinstance(data.get("id"), str) and b.ID.fullmatch(data["id"])):
        text = (err or said or "").strip().removeprefix("sandbox-ai bridge: ")
        raise ValueError(f"{shown(host, 40)}: " + (shown(text[-600:], 600) if text else f"exit status {status}"))
    data = {**data, "host": host, "a": {"name": f"{host}:{a_name}", "host": host},
            "b": {"name": f"{host}:{b_name}", "host": host},
            "warnings": [shown(item, 600) for item in data.get("warnings") or [] if isinstance(item, str)][:20],
            "notes": [shown(item, 600) for item in data.get("notes") or [] if isinstance(item, str)][:20]}
    label = f"{data['id']} on {host}" if data.get("id") else f"(dry run, nothing made) on {host}"
    out(f"bridge {label}: {shown(data['a']['name'])} -> {shown(data['b']['name'])} ({b.grant_text(grant)})"
        + (f", back: {b.grant_text(grant)}" if both else ""))
    if data.get("id"):
        out(f"  made on {host} (both sessions run there; no hop through this machine): sandbox-ai bridge show|revoke|"
            f"resume {host}:{data['id']}, sandbox-ai bridge list --hosts")
    for warning in data["warnings"]:
        out(f"  warning: {warning}")
    for note in data["notes"]:
        out(f"  note: {note}")
    return data


def qualified_id(text):
    """(HOST or None, ID) of a bridge named ID (this machine's) or HOST:ID (one of a host of `sandbox-ai hosts`);
    ValueError."""
    match = QUALIFIED_ID.fullmatch(str(text or ""))
    if not match:
        raise ValueError(f"{shown(text, 60)}: a bridge is ID (eight hex digits), or HOST:ID for one on a host of "
                         "sandbox-ai hosts")
    host = match.group("host")
    if host in (None, b.module("hosts").LOCAL):
        return None, match.group("id")
    if host not in b.module("hosts").load():
        raise ValueError(f"no host {shown(host, 40)} in your list (sandbox-ai hosts list)")
    return host, match.group("id")


def on_host(host, args, out=print, timeout=90):
    """`sandbox-ai bridge ARGS` on HOST (show, revoke, resume of a bridge there): what it says, line by line, each
    cleaned of control characters; ValueError with what it said when it refuses."""
    status, said, err = b.module("hosts").run(host, ["bridge", *args], timeout=timeout)
    if status != 0:
        text = (err or said or "").strip().removeprefix("sandbox-ai bridge: ")
        raise ValueError(f"{shown(host, 40)}: " + (shown(text[-600:], 600) if text else f"exit status {status}"))
    for line in said.splitlines():
        out(shown(line, 2000))
    return said


def show_on_host(host, ident, as_json=False, out=print):
    """`bridge show HOST:ID`: shown there; --json: its document with "host"."""
    if not as_json:
        return on_host(host, ["show", ident], out)
    document = b.module("hosts").run_json(host, ["bridge", "show", ident, "--json"], timeout=90)
    if not isinstance(document, dict) or document.get("id") != ident:
        raise ValueError(f"{shown(host, 40)}: no bridge {ident} there")
    document = {**document, "host": host}
    out(json.dumps(document, indent=2))
    return document


def host_rows(host, rows):
    """The rows HOST's `bridge list --json` gave, as this machine lists them: "host": HOST; a session of HOST itself
    named HOST:NAME (a_host, b_host: HOST); one of a third host as HOST named it. Rows that are not one: left out."""
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not b.ID.fullmatch(row["id"]):
            continue
        row = {key: value for key, value in row.items() if isinstance(key, str)}
        for side in b.SIDES:
            name = row.get(side)
            if not isinstance(name, str):
                row[side] = "?"
            elif not row.get(f"{side}_host"):
                row[side], row[f"{side}_host"] = f"{host}:{name}", host
        found.append({**row, "host": host})
    return found


def merged_rows(local, remote_rows):
    """This machine's bridges (LOCAL) and those the hosts listed (REMOTE_ROWS: host_rows'), each bridge once: a row
    of a half that serves another machine's bridge (remote_half) is left out when the bridge's own row is listed
    (here, or by the host that made it)."""
    rows = [{**row, "host": None} for row in local]
    seen = {row["id"] for row in rows}
    for row in [row for row in remote_rows if not row.get("remote_half")] + \
            [row for row in remote_rows if row.get("remote_half")]:
        if row.get("remote_half") and row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def listing_of_hosts(include_all=False, problems=None, names=None):
    """`bridge list --hosts`: this machine's bridges and those of every host of `sandbox-ai hosts` (asked at once,
    HOSTS_WAIT seconds each); a host that does not answer is said in PROBLEMS ([(host, why)])."""
    hosts = b.module("hosts")
    names = [name for name in (names if names is not None else hosts.load()) if name != hosts.LOCAL]
    found, lock = [], threading.Lock()

    def ask(host):
        try:
            rows = hosts.run_json(host, ["bridge", "list", *(["--all"] if include_all else []), "--json"],
                                  timeout=HOSTS_WAIT)
            if not isinstance(rows, list):
                raise ValueError("not a list of bridges")
            with lock:
                found.extend(host_rows(host, rows))
        except Exception as exc:  # noqa: BLE001 - one host that does not answer holds up none of the others
            if problems is not None:
                with lock:
                    problems.append((host, shown(exc, 300)))
    threads = [threading.Thread(target=ask, args=(name,), daemon=True) for name in names]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + HOSTS_WAIT + 15  # (all of them asked at once: one wait for all)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    with lock:  # (a host still answering adds nothing from here on, and is said)
        rows = list(found)
        if problems is not None:
            answered = {row["host"] for row in rows} | {host for host, _ in problems}
            problems += [(name, f"no answer within {HOSTS_WAIT + 15} s") for name, thread in zip(names, threads)
                         if thread.is_alive() and name not in answered]
    rows.sort(key=lambda row: (row["host"], row.get("created") or "", row["id"]))
    return merged_rows(b.listing(include_all), rows)


# --- the other host's half: sandbox-ai bridge-stub ID ---------------------------------------------------------------

def open_half(ident, body, link, refuse=lambda text: None):
    """This host's half of the bridge IDENT as the other host's broker asks for it (BODY of its "open"), checked
    here: the session runs here, the grant is well formed (and is this half's own from now on: nothing widens it),
    files:rw only where the session may write itself; its record, its broker, its side attached. The Broker."""
    directory = b.bridge_dir(ident)
    if directory.exists() and body.get("rejoin") is True:
        return rejoin_half(ident, body, link, refuse)
    if directory.exists():
        raise ValueError(f"bridge {ident} was here before: a bridge is never opened again (add a new one)")
    side = body.get("side")
    if side not in b.SIDES:
        raise ValueError("no side")
    far = b.other(side)
    name, origin, peer = body.get("name"), body.get("origin"), body.get("peer")
    if not isinstance(name, str) or not b.RUN_NAME.fullmatch(name):
        raise ValueError("not a session's name")
    if not isinstance(origin, str) or not HOST_NAME.fullmatch(origin):
        origin = "host"
    # A bridge between this host's session and one of a third host, relayed by the machine that made it (its
    # name; ORIGIN is then that machine's name for the other session's host): said in the record, nothing more.
    relay = body.get("relay")
    relay = relay if isinstance(relay, str) and HOST_NAME.fullmatch(relay) else None
    if not isinstance(peer, dict) or not isinstance(peer.get("name"), str) or \
            not b.RUN_NAME.fullmatch(peer["name"]) or not isinstance(peer.get("run"), str) or \
            not b.RUN_NAME.fullmatch(peer["run"]) or not isinstance(peer.get("host_id"), str) or \
            not HOST_ID.fullmatch(peer["host_id"]):
        raise ValueError("the other side is not described well")
    if peer["host_id"] == host_id():
        raise ValueError("the other side runs on this machine: a bridge across hosts joins two hosts")
    grants = body.get("grants")
    if not isinstance(grants, dict) or set(grants) != set(b.SIDES):
        raise ValueError("no grant for each side")
    grants = {key: check_grant(grants[key]) for key in b.SIDES}
    if body.get("ask") not in b.ASK_POLICIES:
        raise ValueError(f"ask is {', '.join(b.ASK_POLICIES)}")
    limits = check_limits(body.get("limits"))
    until = body.get("until")
    if until is not None and (not isinstance(until, str) or b.epoch(until) is None):
        raise ValueError("until is a time")
    mine = b.find_side(name)
    if any(entry["mode"] == "rw" for entry in grants[far]["files"]) and mine["workspace_mode"] != "rw":
        raise ValueError(f"files:rw: the workspace of {shown(name)} is "
                         f"{'read-only for it' if mine['workspace_mode'] == 'ro' else 'of unknown mode'}; a bridge "
                         "never writes where the session itself may not")
    other_side = {"host": origin, "host_id": peer["host_id"], "name": f"{origin}:{peer['name']}",
                  "start_name": peer["name"], "run": peer["run"], "remote": True, "ready": True,
                  "agent": shown(peer.get("agent"), 20)}
    # A half of a bridge of the same two sessions killed here before (it closed nothing): what it left is closed
    # first, as bridge add does on its host (its relay would hold the peer's server name).
    for other_ident in b.all_ids():
        with contextlib.suppress(Exception):
            old = b.load(other_ident)
            if b.orphaned(old) and {b.side_key(old["a"]), b.side_key(old["b"])} == \
                    {b.side_key(mine), b.side_key(other_side)}:
                b.reap(other_ident)
    mine["server"] = b.server_name(other_side, ident)
    other_side["server"] = b.server_name(mine, ident)
    data = {"id": ident, "created": b.now_text(), "state": "starting", "reason": None, "remote_half": True,
            "origin": origin, **({"relayed_by": relay} if relay else {}), side: mine, far: other_side,
            "grants": grants, "ask": body["ask"], "limits": limits,
            "until": until, "warnings": [], "notes": [], "alerts": [], "calls": 0,
            "volume": {"day": None, "bytes": 0}, "canary_baseline": {side: b.canary_count(mine), far: 0}}
    b.private_dir(b.state_root())
    try:
        os.mkdir(directory, 0o700)  # (made by exactly one: a second open of the same id fails here)
    except FileExistsError:
        raise ValueError(f"bridge {ident} was here before: a bridge is never opened again (add a new one)") from None
    for part in (side, "approvals"):
        b.private_dir(directory / part)
    b.save(data)
    broker = b.Broker(ident)
    broker.link = link
    try:
        broker.record_broker()
        broker.start()
        link.start(broker)
        notes = []
        b.attach_side(data, side, notes)

        def activate(current):
            current[side].update(data[side])
            current["notes"] = notes
            if current.get("state") == "starting":
                current["state"] = "active"
        broker.data = b.change(ident, activate)
        if broker.data["state"] != "active":
            raise ValueError(f"it ended while it started ({shown(broker.data.get('reason'), 200)})")
    except BaseException as exc:
        b.detach_side(data, side)
        reason = f"it could not be set up here: {shown(exc, 300)}"
        with contextlib.suppress(Exception):
            b.change(ident, lambda current: current.update(state="failed", reason=reason))
        refuse(reason)  # (said first: the end of this half closes the link)
        broker.stopping.set()
        broker.close(reason)
        raise
    broker.notes = notes
    b.both_events(broker.data, f"bridge {ident} {broker.data['a']['name']} -> {broker.data['b']['name']} (across "
                               f"hosts): {b.grant_text(grants['a'])}" +
                  (f"; back: {b.grant_text(grants['b'])}" if b.grant_text(grants['b']) != "-" else ""),
                  {"event": "created"})
    b.append_log(ident, {"event": "created", "from": origin, "a": broker.data["a"]["run"], "b": broker.data["b"]["run"],
                         "grants": {key: b.grant_text(grants[key]) for key in b.SIDES}})
    return broker


def rejoinable(old, body, now=None):
    """Why the relay may not rejoin the half of OLD (this host's record of it) as BODY (its "open" with rejoin) asks,
    or None. Only a half of a bridge relayed by another machine, ended because its link was lost at most
    REJOIN_WINDOW seconds ago - never one revoked, failed, ended for its session, its time or a word of the other
    side -, for the same side, session and run, the same peer (name, run, identity) and exactly the grant, questions,
    limits and time it had: a rejoin never widens anything and never joins another session."""
    now = time.time() if now is None else now
    if not old.get("remote_half") or not old.get("relayed_by"):
        return "it was not relayed by another machine (only a relayed bridge is rejoined)"
    if old.get("state") != "ended" or not old.get("link_lost"):
        return f"it ended for good here ({shown(old.get('reason') or old.get('state'), 200)}): add a new bridge"
    ended = b.epoch(old.get("ended")) if old.get("ended") else None
    if ended is None or now - ended > REJOIN_WINDOW:
        return f"its link was lost more than {REJOIN_WINDOW // 3600} h ago: add a new bridge"
    side = body.get("side")
    if side not in b.SIDES or b.is_remote(old.get(side) or {}):
        return "not the side this host had"
    far = b.other(side)
    peer = body.get("peer") if isinstance(body.get("peer"), dict) else {}
    if body.get("name") != old[side].get("name") and body.get("name") != old[side].get("run"):
        return "not the session this host had"
    if (peer.get("name"), peer.get("run"), peer.get("host_id")) != \
            (old[far].get("start_name"), old[far].get("run"), old[far].get("host_id")):
        return "not the other session it had (another run: it was started again)"
    grants = body.get("grants")
    if not isinstance(grants, dict) or set(grants) != set(b.SIDES) or \
            {key: check_grant(grants[key]) for key in b.SIDES} != old.get("grants"):
        return "not the grant it had (a rejoin never changes what is granted)"
    if body.get("ask") != old.get("ask") or check_limits(body.get("limits")) != old.get("limits") or \
            body.get("until") != old.get("until"):
        return "not the questions, limits or time it had"
    until = b.epoch(old["until"]) if old.get("until") else None
    if until is not None and now >= until:
        return f"its time ran out ({old['until']})"
    return None


def rejoin_half(ident, body, link, refuse=lambda text: None):
    """This host's half of the relayed bridge IDENT again, after its link was lost (the relaying machine slept):
    checked by rejoinable() against this host's own record, the session found again (the same run), its side
    attached again; the record says it was rejoined. The Broker; ValueError why not."""
    old = b.load(ident)
    deadline = time.monotonic() + REJOIN_SETTLE
    while old.get("state") in b.LIVE_STATES and time.monotonic() < deadline:
        time.sleep(0.2)  # (the old half sees its connection end and ends its side: a moment)
        old = b.load(ident)
    if old.get("state") in b.LIVE_STATES:
        raise ValueError(f"bridge {ident} still ends here (its old link has not ended yet): try again in a moment")
    why = rejoinable(old, body)
    if why:
        raise ValueError(f"bridge {ident} is not rejoined: {why}")
    # (its record says "ended" only once its broker has detached its side: nothing of the old half is left to wait for)
    side = body["side"]
    far = b.other(side)
    mine = b.find_side(old[side]["run"])
    if mine["run"] != old[side]["run"]:
        raise ValueError(f"bridge {ident} is not rejoined: {shown(old[side]['name'])} runs another run now")
    mine["server"] = old[side]["server"]

    def reopen(current):
        if current.get("state") != "ended" or not current.get("link_lost"):
            raise ValueError(f"bridge {ident} changed meanwhile ({shown(current.get('state'), 20)})")
        current.update(state="starting", reason=None, ended=None, link_lost=None, rejoined=b.now_text(),
                       rejoins=int(current.get("rejoins") or 0) + 1,
                       canary_baseline={side: b.canary_count(mine), far: 0})
        current[side] = {**current[side], **mine}
    data = b.change(ident, reopen)
    broker = b.Broker(ident)
    broker.link = link
    try:
        broker.record_broker()
        broker.start()
        link.start(broker)
        notes = []
        b.attach_side(data, side, notes)

        def activate(current):
            current[side].update(data[side])
            current["notes"] = notes
            if current.get("state") == "starting":
                current["state"] = "active"
        broker.data = b.change(ident, activate)
        if broker.data["state"] != "active":
            raise ValueError(f"it ended while it started ({shown(broker.data.get('reason'), 200)})")
    except BaseException as exc:
        b.detach_side(data, side)
        reason = f"it could not be set up here again: {shown(exc, 300)}"
        with contextlib.suppress(Exception):
            b.change(ident, lambda current: current.update(state="failed", reason=reason))
        refuse(reason)
        broker.stopping.set()
        broker.close(reason)
        raise
    broker.notes = notes
    b.both_events(broker.data, f"bridge {ident} {broker.data['a']['name']} -> {broker.data['b']['name']} rejoined "
                               "after its link was lost (the relaying machine slept)", {"event": "rejoined"})
    b.append_log(ident, {"event": "rejoined", "by": old.get("relayed_by")})
    return broker


def next_request(link, deadline):
    """The next request of the other half before the open (its pings passed over), None when none came in time."""
    while True:
        frame = link.first(max(0.1, deadline - time.monotonic()))
        if not (isinstance(frame, dict) and frame.get("t") == "ev" and frame.get("op") == "ping") or \
                time.monotonic() > deadline:
            return frame


def describe(body):
    """What a relay is told of the session BODY names before the open: it runs here (bridge.find_side's checks), its
    run, agent and workspace's mode (the other half's record of its peer, and the check of files:rw). An answer."""
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not b.RUN_NAME.fullmatch(name):
        return {"ok": False, "text": "not a session's name"}
    try:
        side = b.find_side(name)
    except ValueError as exc:
        return {"ok": False, "text": shown(exc, 600)}
    return {"ok": True, "side": {"name": side["name"], "run": side["run"], "agent": side.get("agent") or "unknown",
                                 "workspace_mode": side.get("workspace_mode")}}


def stub_main(argv, reader=None, writer=None, signals=True):
    """`sandbox-ai bridge-stub ID`: this host's half of a bridge across hosts, on stdin and stdout (READER and
    WRITER: byte streams in its place). The exit status."""
    usage = "usage: sandbox-ai bridge-stub ID (started by sandbox-ai bridge add on the other host, over SSH)"
    if list(argv) in (["--help"], ["-h"]):  # asked for: the usage, as every command says its own (to stdout)
        print("Usage:  sandbox-ai bridge-stub ID\n\nThe other host's half of a bridge across hosts (started by "
              "sandbox-ai bridge add on the other host, over SSH): it speaks only the bridge's frames on its stdin "
              "and stdout and is not for typing.\n\nRun 'sandbox-ai bridge --help' for the bridges.")
        return 0
    if len(argv) != 1 or not isinstance(argv[0], str) or not b.ID.fullmatch(argv[0]):
        print(usage, file=sys.stderr)
        return 2
    ident = argv[0]
    try:
        b.refuse_inside("stub")
    except ValueError as exc:
        print(f"sandbox-ai bridge-stub: {exc}", file=sys.stderr)
        return 1
    if reader is None:
        frames = os.dup(1)
        os.dup2(2, 1)  # whatever else writes to stdout goes to stderr: never into the frames
        reader, writer = sys.stdin.buffer, os.fdopen(frames, "wb")
    link = Link(reader, writer, "the other host")
    try:
        link.send({"t": "hello", "protocol": PROTOCOL, "version": version(), "host_id": host_id(),
                   "features": list(FEATURES)})
        deadline = time.monotonic() + OPEN_WAIT
        request = next_request(link, deadline)
        if isinstance(request, dict) and request.get("op") == "describe":
            # (a relay between two other hosts asks first which run the session is, to tell the other half)
            link.reply(request.get("id"), describe(request.get("body")))
            request = next_request(link, deadline)
    except (ProtocolError, ConnectionError, ValueError, OSError) as exc:
        print(f"sandbox-ai bridge-stub: {exc}", file=sys.stderr)
        link.close(str(exc))
        return 1
    if not isinstance(request, dict) or request.get("t") != "req" or request.get("op") != "open" or \
            not isinstance(request.get("id"), int) or not isinstance(request.get("body"), dict):
        link.close("no open")
        return 1
    said = []

    def refuse(text):
        said.append(text)
        link.reply(request["id"], {"ok": False, "text": shown(text, 600)})
    try:
        broker = open_half(ident, request["body"], link, refuse)
    except Exception as exc:  # noqa: BLE001 - refused, with the reason, whatever it was
        if not said:
            refuse(str(exc))
        link.close(f"refused: {exc}")
        print(f"sandbox-ai bridge-stub: {exc}", file=sys.stderr)
        return 1
    if signals:
        signal.signal(signal.SIGTERM, lambda *_: broker.stopping.set())
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # (a dropped SSH connection ends it by its end of stdin)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    local = broker.data[[key for key in b.SIDES if key != broker.remote][0]]
    link.reply(request["id"], {"ok": True, "side": {"name": local["name"], "run": local["run"],
                                                    "agent": local.get("agent") or "unknown",
                                                    "workspace_mode": local.get("workspace_mode"),
                                                    "server": local["server"]},
                               "network": network_report(local), "notes": getattr(broker, "notes", [])})
    broker.run()
    return 0
