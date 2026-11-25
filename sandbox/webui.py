#!/usr/bin/env python3
"""sandbox-ai web: the web interface, beside the desktop app. One service manages this machine and every host of
`sandbox-ai hosts` over SSH: starts and sessions with their terminals, approvals, the doctor, logs, LLM servers and
the defaults of each host.

  sandbox-ai web user add NAME          a login: a password (12 characters or more) and a TOTP app
  sandbox-ai web user list | remove NAME | reset NAME
  sandbox-ai web serve [--listen ADDRESS:PORT] [--hostname NAME]... [--cert FILE --key FILE]
  sandbox-ai web cert [--renew]         the service's certificate and its SHA-256 fingerprint

Only HTTPS: at the first start the service makes a certificate of its own (openssl), whose fingerprint it prints;
your browser asks once whether to trust it. It listens on 127.0.0.1:8443 unless --listen says otherwise (to reach
it from elsewhere: --listen 0.0.0.0:8443, or an SSH tunnel, or a reverse proxy of yours).

Every login needs the password and a current TOTP code (a code is accepted once). Failed logins slow down per
address, per address and user, and per user (that one never keeps the right password and code out). A session lives
30 minutes without use (the page's background polls are none; typing in a terminal is use) and 12 hours at most, in
memory only; its cookie is HttpOnly, Secure, SameSite=Strict. Every change needs the session's CSRF token and a same-origin request; the Host
header must be one of the service's names. Every action is written to the audit log
(~/.local/state/sandbox-ai/web/audit.jsonl). The browser gets JSON from a fixed set of actions, each run as
`sandbox-ai ARGS` with its arguments quoted one by one (locally, or over SSH: hosts.py); nothing it sends becomes
a shell command. No sandbox can reach the service: the launcher refuses --allow-host-port for its port.

What ends, removes or weakens something needs {"confirm": true} in the request, by one table (NEEDS_CONFIRM, served
as GET /api/policy: the page asks for exactly these); a default or a limit counts as weakened by any step down from
what its host has now (defaults show, limits --show). Each answer says how long the session lives without use
(X-Session-Idle-Left): the page warns two minutes before. The questions and the starts of every host are asked at
once with at most one request in flight per host (GET /api/approvals, GET /api/starts). A secret's value goes once,
on standard input, to `secrets set` there: never on a command line, in a response or in the audit log.

Standard library only (openssl for the certificate).
"""
import argparse
import base64
import concurrent.futures
import contextlib
import errno
import functools
import fcntl
import getpass
import hashlib
import hmac
import http.server
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shlex
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse

HERE = Path(__file__).resolve().parent


def ready_fds(read, write=(), timeout=None):
    """(readable, writable) of the descriptors READ and WRITE within TIMEOUT seconds, as select.select's first two -
    for descriptors of any number: select() refuses those of 1024 and above (ValueError), which a web interface
    that has served many terminals and downloads reaches. A hang-up or an error counts as readable (the next read
    says which), as with select()."""
    poller, wanted = select.poll(), {}
    for items, mask in ((read, select.POLLIN | select.POLLPRI), (write, select.POLLOUT)):
        for item in items:
            fd = item if isinstance(item, int) else item.fileno()
            wanted.setdefault(fd, []).append((item, mask))
    for fd, entries in wanted.items():
        poller.register(fd, functools.reduce(lambda total, entry: total | entry[1], entries, 0))
    readable, writable = [], []
    for fd, events in poller.poll(None if timeout is None else max(0, int(timeout * 1000))):
        broken = events & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
        for item, mask in wanted.get(fd, ()):
            if mask & select.POLLOUT:
                if events & select.POLLOUT or broken:
                    writable.append(item)
            elif events & (select.POLLIN | select.POLLPRI) or broken:
                readable.append(item)
    return readable, writable

def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
# The client address of the request this thread answers (Service.respond sets it): an answer to a browser question
# relays its login's callback only for a browser on this machine (API.answer, oauth_relay.py).
REQUEST = threading.local()
STATIC = HERE / "web"
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "i18n.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8", "themes.json": "application/json",
                "help.json": "application/json", "icon.svg": "image/svg+xml"}
# The fonts of the help and the reading fonts (web/fonts, the same families as the desktop app's, fonts.json says
# where from); served from here only, never from another host.
FONT_FILES = {name: ("font/woff2" if name.endswith(".woff2") else "text/plain; charset=utf-8")
              for name in ("AtkinsonHyperlegibleNext-Regular.woff2", "AtkinsonHyperlegibleNext-Bold.woff2",
                           "AtkinsonHyperlegibleMono-Regular.woff2", "OpenDyslexic-Regular.woff2",
                           "OpenDyslexic-Bold.woff2", "Lexend-Regular.woff2", "Lexend-Bold.woff2",
                           "OFL-AtkinsonHyperlegibleNext.txt", "OFL-AtkinsonHyperlegibleMono.txt",
                           "OFL-OpenDyslexic.txt", "OFL-Lexend.txt")}
VENDOR_FILES = {"xterm.js": "text/javascript; charset=utf-8", "xterm.css": "text/css; charset=utf-8",
                "addon-fit.js": "text/javascript; charset=utf-8"}
DEFAULT_LISTEN = "127.0.0.1:8443"
USER_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")  # a start's name or id (START_ID)
NEW_START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")  # a name given to a start (SESSION_NAME)
MCP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
APPROVAL_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}")  # never a leading "-": read as an option there
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
# The agents of the commands that take fewer: sandbox-ai login (login.AGENTS) and plugins (plugins.py); a terminal
# for another one is refused here, not opened to fail there. A team's members are team.AGENTS (team_agents()).
LOGIN_AGENTS = ("claude", "codex", "opencode", "hermes", "qwen", "pi")
PLUGIN_AGENTS = ("claude", "codex", "opencode", "opencode2", "all")


def agent_words(agents=AGENTS):
    """"agent is claude, codex or opencode": the refusal naming AGENTS."""
    return f"agent is {', '.join(agents[:-1])} or {agents[-1]}"
MIN_PASSWORD = 12
SCRYPT = {"n": 1 << 15, "r": 8, "p": 1}
TOTP_PERIOD, TOTP_DIGITS = 30, 6
IDLE, LIFETIME = 30 * 60, 12 * 3600
BODY_LIMIT = 1 << 20
FRAME_LIMIT = 1 << 20
TERMINALS_PER_SESSION = 8
# A terminal's WebSocket: pinged every WS_PING seconds; a browser that sends nothing (not even the pong) for
# WS_DEADLINE seconds is gone, and one that takes nothing of the output for that long is dropped.
WS_PING, WS_DEADLINE = 30, 90
# Connections served at once, each a thread of its own (open terminals among them): one more is closed at once.
# One address holds at most MAX_PER_ADDRESS of them, so that a few slow clients cannot take every place.
MAX_CONNECTIONS = 64
MAX_PER_ADDRESS = 16
# Seconds a request may take from its first byte to the end of its headers (the TLS handshake too), and its body to
# arrive: a client that sends one byte every few seconds (under the per-read timeout) is cut off, not served for
# minutes. Each is a deadline for the whole of it (Reaper), not for one read.
HEAD_DEADLINE, BODY_DEADLINE = 10, 30
# Seconds an idle HTTP connection is kept: KEEPALIVE once it carried a session, GUEST_KEEPALIVE before (one that
# never logs in holds its thread a few seconds, not two minutes).
KEEPALIVE, GUEST_KEEPALIVE = 120, 10
SESSION_ENDED = "the session ended (logged out, idle too long, or its user changed): log in again"
COOKIE = "__Host-sandbox-ai"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "web"


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "web"


def users_file():
    return config_dir() / "users.json"


def listen_file(pid=None):
    """Where the running service PID (default: this process) says which port it has - a record of its own, as two
    may run at once (loopback and the LAN): the launcher refuses --allow-host-port for each such port
    (agent_sandbox.web_service_ports). Before 0.1.98 every service wrote listen.json, and the second wrote over
    the first's port."""
    return config_dir() / f"listen-{pid or os.getpid()}.json"


_modules, _modules_lock = {}, threading.RLock()


def module(name):
    """sandbox-ai's module NAME, loaded once, under a lock: the handlers' threads ask for it at the same time, and
    each loaded a copy of its own (with its own locks and caches; the last one won)."""
    if name not in _modules:
        with _modules_lock:
            if name not in _modules:
                spec = importlib.util.spec_from_file_location(f"{name}_for_web", HERE / f"{name}.py")
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _modules[name] = loaded
    return _modules[name]


def printable(text):
    return module("hosts").printable(text)


def team_agents():
    """The agents a team may have as members: team.py's own list (team.AGENTS), never a copy of it."""
    return module("team").AGENTS


@functools.lru_cache(maxsize=1)
def team_line():
    """A line of team list: NAME  MEMBERS  WORKSPACE (the members of team_agents())."""
    agent = "(?:" + "|".join(map(re.escape, team_agents())) + ")"
    return re.compile(rf"(?P<name>[a-z0-9][a-z0-9_-]{{0,19}})\s+(?P<members>{agent}(?:, {agent})*)"
                      r"\s+(?P<workspace>/.*)")


def write_private(path, data):
    """DATA (bytes) to PATH, mode 0600 from the start, atomically; the directory 0700."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --- passwords and TOTP -------------------------------------------------------------------------------------

def hash_password(password, salt=None, n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"]):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 << 20, dklen=32)
    return {"kdf": "scrypt", "n": n, "r": r, "p": p, "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(digest).decode()}


DUMMY = None  # a hash to check unknown users against, so that they take as long as known ones


def check_password(password, record):
    global DUMMY
    if record is None:
        DUMMY = DUMMY or hash_password("x" * MIN_PASSWORD)
        record, known = DUMMY, False
    else:
        known = True
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
        computed = hash_password(password, salt, record["n"], record["r"], record["p"])["hash"]
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(base64.b64decode(computed), expected) and known


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret, counter, digits=TOTP_DIGITS):
    """RFC 6238 (HMAC-SHA1) for COUNTER (time // period)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def check_totp(secret, code, last_counter, now=None):
    """The counter CODE belongs to (the current period or one next to it), if it is newer than LAST_COUNTER (a
    code is accepted once), else None."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return None
    current = int((time.time() if now is None else now) // TOTP_PERIOD)
    found = None
    for counter in (current - 1, current, current + 1):
        if hmac.compare_digest(totp(secret, counter), code) and counter > (last_counter or -1):
            found = counter
    return found


def otpauth_uri(user, secret):
    label = urllib.parse.quote(f"sandbox-ai:{user}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer=sandbox-ai&algorithm=SHA1&digits={TOTP_DIGITS}"
            f"&period={TOTP_PERIOD}")


# --- users ------------------------------------------------------------------------------------------------

_users_lock = threading.Lock()


def load_users():
    path = users_file()
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except RecursionError:  # (nested deeper than Python reads: damaged)
        raise ValueError(f"{path}: not valid JSON (nested too deeply)") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("users"), dict):
        raise ValueError(f"{path}: expected {{\"version\": 1, \"users\": {{...}}}}")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{path}: readable by others; chmod 600 it")
    return document["users"]


def save_users(users):
    write_private(users_file(), (json.dumps({"version": 1, "users": users}, indent=2) + "\n").encode())


@contextlib.contextmanager
def users_locked():
    """The users, read and written under a lock (this process's and the file's)."""
    lock = config_dir() / ".users.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _users_lock, open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        users = load_users()
        before = json.dumps(users, sort_keys=True)
        yield users
        if json.dumps(users, sort_keys=True) != before:
            save_users(users)


def users_unreadable(exc):
    """The answer when load_users() fails (users.json readable by others, or broken): a clear error for the browser,
    the details in the audit log only (the browser of anyone gets this answer, the file's path is none of its
    business)."""
    with contextlib.suppress(OSError):
        audit("users-unreadable", why=printable(exc)[:500])
    return Refused(503, "the service cannot read its logins (users.json is readable by others or broken): "
                        "see sandbox-ai web user list on the server")


# --- the audit log ----------------------------------------------------------------------------------------

AUDIT_PART = 16 << 20       # audit.jsonl is rotated at this size (statedirs.rotate: renamed and compressed beside it)
AUDIT_ARCHIVE = 256 << 20   # its compressed older parts kept; beyond, the oldest go


def rotation_time():
    """The time a rotated log's older part is named by (tests: their own)."""
    return time.time()


def audit(event, **fields):
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    path = state_dir() / "audit.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True) + "\n"
    for attempt in range(4):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            # Another process rotated the log while this one waited for the lock: the record goes into the new one.
            with contextlib.suppress(OSError):
                if os.stat(path, follow_symlinks=False).st_ino != os.fstat(fd).st_ino and attempt < 3:
                    continue
            if os.fstat(fd).st_size >= AUDIT_PART and attempt < 3:
                with contextlib.suppress(Exception):  # a log that cannot be rotated is written on
                    if module("statedirs").rotate(path, AUDIT_ARCHIVE, rotation_time()):
                        continue
            stream.write(line)
            return


# --- logins: rate limits and sessions ---------------------------------------------------------------------

class Limiter:
    """Failed logins per key (an address, an address with a user, a user): after FREE failures each further try
    waits, doubling up to MAXIMUM seconds; a success clears the key."""

    def __init__(self, free=5, first=30, maximum=900, clock=time.monotonic):
        self.free, self.first, self.maximum, self.clock = free, first, maximum, clock
        self.failures, self.lock = {}, threading.Lock()

    def wait(self, *keys):
        """Seconds until KEYS may try again (0: now)."""
        now = self.clock()
        with self.lock:
            return max([0] + [max(0, until - now) for key in keys for _, until in [self.failures.get(key, (0, 0))]])

    def failed(self, *keys, at_least=0):
        """One more failure for each of KEYS; AT_LEAST: the count it has at least now (FREE: it waits at once)."""
        now = self.clock()
        with self.lock:
            for key in keys:
                count, _ = self.failures.get(key, (0, 0))
                count = max(count + 1, at_least)
                delay = 0 if count < self.free else min(self.maximum, self.first * 2 ** (count - self.free))
                self.failures[key] = (count, now + delay)
            if len(self.failures) > 10000:  # an address flood must not grow it without bound
                for key in sorted(self.failures, key=lambda k: self.failures[k][1])[:5000]:
                    del self.failures[key]

    def succeeded(self, *keys):
        with self.lock:
            for key in keys:
                self.failures.pop(key, None)


def user_fingerprint(record):
    """What a session remembers of its user's record (the password hash and the TOTP secret, hashed): a removed or
    reset user (sandbox-ai web user remove|reset) ends the sessions made with the old record at their next request."""
    if not isinstance(record, dict):
        return None
    return hashlib.sha256(json.dumps([record.get("password"), record.get("totp")], sort_keys=True).encode()).hexdigest()


class Sessions:
    def __init__(self, idle=IDLE, lifetime=LIFETIME, clock=time.monotonic):
        self.idle, self.lifetime, self.clock = idle, lifetime, clock
        self.items, self.lock = {}, threading.Lock()

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).hexdigest()  # what an attacker with this memory could not reuse

    def new(self, user, address, fingerprint=None):
        token, now = secrets.token_urlsafe(32), self.clock()
        with self.lock:
            # Sessions past their limits are dropped here too: one that is never used again would stay for good.
            for key in [k for k, s in self.items.items()
                        if now - s["seen"] > self.idle or now - s["created"] > self.lifetime]:
                del self.items[key]
            self.items[self.key(token)] = {"user": user, "csrf": secrets.token_urlsafe(32), "created": now,
                                           "seen": now, "address": address, "terminals": 0, "user_record": fingerprint}
        return token

    def get(self, token, touch=True):
        """The session of TOKEN, or None (and gone) past its idle time or lifetime. TOUCH: this counts as the
        user's activity (a request); a terminal's check that its session still lives does not."""
        if not token:
            return None
        now = self.clock()
        with self.lock:
            session = self.items.get(self.key(token))
            if session is None:
                return None
            if now - session["seen"] > self.idle or now - session["created"] > self.lifetime:
                del self.items[self.key(token)]
                return None
            if touch:
                session["seen"] = now
            return session

    def drop(self, token):
        with self.lock:
            self.items.pop(self.key(token or ""), None)

    def left(self, session):
        """(seconds until SESSION ends without use, seconds until its lifetime ends)."""
        now = self.clock()
        return (max(0, int(self.idle - (now - session["seen"]))),
                max(0, int(self.lifetime - (now - session["created"]))))

    def current(self, token, users, touch=True):
        """The session of TOKEN while its user is still the one it was made for (USERS: load_users()), else None
        (and gone). TOUCH: as for get()."""
        session = self.get(token, touch)
        if session is not None and user_fingerprint(users.get(session["user"])) != session.get("user_record"):
            self.drop(token)
            return None
        return session


# --- what the browser may ask for ---------------------------------------------------------------------------

# The codes of the common refusals (Refused's CODE, the answer's "code" with its "params"): the page shows its own
# text for a code it knows (German too), with the service's English message below it; any other refusal is shown as
# its message. A code is never a reason to pass anything: it only says which of these it is.
ERROR_CODES = ("no-host", "confirm-needed", "bad-path", "bad-name", "host-unreachable", "timeout", "command-failed",
               "unknown-preset-value", "no-preset", "login-needed", "page-outdated", "too-many-logins", "busy",
               "changed-meanwhile", "nothing-checked", "host-too-old", "mcp-unsigned-catalog",
               "shared-wildcard", "mcp-unpublished", "mcp-build-asked", "mcp-unpublished-start")

# What the page sends that only newer sandbox-ai versions know: (since, what, test of the argv). A host whose last
# status (hosts list, kept; never asked for this) names an older version is told so before anything runs there
# (newer_needed); an older one whose version is not known yet says so itself (argparse's "unrecognized arguments",
# "invalid choice"), which is told the same way (too_old_said).
def asks_allow(args):
    """ARGS start a sandbox with --ask allow, however written: an agent's start (its options up to "--") or a team's
    (`team start ... -- OPTIONS`)."""
    words = [str(word) for word in args]
    if words[:2] == ["team", "start"]:
        words = words[words.index("--") + 1:] if "--" in words else []
    elif words[:1] and words[0] in AGENTS:
        words = words[1:words.index("--")] if "--" in words else words[1:]
    else:
        return False
    return any(word in ("--ask=allow", "-Q=allow") or (word in ("--ask", "-Q") and following == "allow")
               for word, following in zip(words, words[1:] + [""]))


def team_start_words(args):
    """The words of a `team start` ARGS before its "--" (its own options and NAME), else []."""
    return args[2:args.index("--") if "--" in args else len(args)] if args[:2] == ["team", "start"] else []


def with_beyond_the_trio(args):
    """A start (or a team's options) with --with another agent than Claude Code, Codex and OpenCode, or with --with
    behind one of the others: the team image's layers (0.1.98)."""
    trio = {"claude", "codex", "opencode"}
    values = [name.strip() for value in [args[i + 1] for i, x in enumerate(args[:-1]) if x in ("--with", "-wa")] +
              [x[len("--with="):] for x in args if x.startswith("--with=")] for name in value.split(",")
              if name.strip()]
    return bool(values) and (bool(set(values) - trio) or bool(args[:1]) and args[0] in AGENTS and args[0] not in trio)


NEWER = (
    # (0.2.2: the release the agent intro came with)
    ((0, 2, 2), "the default agent_intro (whether a start tells its agent what its sandbox has)",
     lambda a: a[:1] == ["defaults"] and a[1:3] in (["set", "agent_intro"], ["unset", "agent_intro"])),
    ((0, 1, 213), "llm share, unshare and sync (an LLM server for all hosts)",
     lambda a: a[:2] in (["llm", "share"], ["llm", "unshare"], ["llm", "sync"])),
    ((0, 1, 196), "mcp add --build (an MCP server its catalog has no signed image of, built from its source)",
     lambda a: a[:2] == ["mcp", "add"] and any(x in ("--build", "--build-plan") for x in a)),
    ((0, 1, 168), "net allow --allow-shared-wildcard (a wildcard below a shared service, after its confirmation)",
     lambda a: a[:2] == ["net", "allow"] and any(x.startswith("--allow-shared-wildcard=") for x in a)),
    ((0, 1, 98), "--with an agent beyond Claude Code, Codex and OpenCode", with_beyond_the_trio),
    ((0, 1, 98), "team members openhands, nanobot, letta, opencode2 and pi", lambda a: any(
        x.startswith("--agents=") and set(x[len("--agents="):].split(",")) & {"openhands", "nanobot", "letta",
                                                                              "opencode2", "pi"}
        for x in team_start_words(a))),
    ((0, 1, 98), "team start --role, --member-model, --member-llm, --member-mode, --dry-run and --no-config for every "
                 "member", lambda a: any(
        x.startswith(("--role=", "--member-model=", "--member-llm=", "--member-mode=")) or x == "--dry-run"
        for x in team_start_words(a)) or (a[:2] == ["team", "start"] and "--no-config" in a[a.index("--"):]
                                          if "--" in a else False)),
    ((0, 1, 217), "the fix plan of a start (--fix-plan, --fix-only: what it lacks, asked about in the page)",
     lambda a: any(x in ("--fix-plan", "--fix-only") for x in a[:a.index("--") if "--" in a else len(a)])),
    ((0, 1, 99), "sandbox-ai board export and import", lambda a: a[:1] == ["board"]),
    ((0, 1, 99), "sandbox-ai checkpoint", lambda a: a[:1] == ["checkpoint"]),
    ((0, 1, 99), "sandbox-ai session memory, files and export", lambda a: a[:1] == ["session"] and
     a[1:2] in (["memory"], ["files"], ["export"])),
    ((0, 1, 193), "a session's state (session import, duplicate, export --with-workspace)",
     lambda a: a[:2] in (["session", "import"], ["session", "duplicate"]) or (
         a[:2] == ["session", "export"] and "--with-workspace" in a)),
    ((0, 1, 97), "sandbox-ai shell", lambda a: a[:1] == ["shell"]),
    ((0, 1, 96), "team start --vpn, --member-vpn and --allow-shared-openvpn", lambda a: any(
        x.startswith(("--vpn=", "--member-vpn=")) or x == "--allow-shared-openvpn" for x in team_start_words(a))),
    ((0, 1, 96), "team members hermes, dsh, qwen and goose", lambda a: any(
        x.startswith("--agents=") and set(x[len("--agents="):].split(",")) - {"claude", "codex", "opencode"}
        for x in team_start_words(a))),
    ((0, 1, 95), "mcp catalog --json", lambda a: a[:2] == ["mcp", "catalog"] and "--json" in a),
    ((0, 1, 95), "mcp add --env NAME=VALUE and NAME@SECRET", lambda a: a[:2] == ["mcp", "add"] and any(
        x.startswith("--env=") and re.search(r"[=@]", x[len("--env="):]) for x in a)),
    ((0, 1, 80), "--ask allow", asks_allow),
    ((0, 1, 78), "approvals allow --no-open", lambda a: a[:2] == ["approvals", "allow"] and "--no-open" in a),
    ((0, 1, 98), "approvals list --state", lambda a: a[:2] == ["approvals", "list"] and "--state" in a),
    ((0, 1, 78), "wizard trust --list, --show and --digest", lambda a: a[:2] == ["wizard", "trust"] and any(
        x in ("--list", "--show") or x.startswith("--digest") for x in a)),
    ((0, 1, 78), "wizard models --url", lambda a: a[:2] == ["wizard", "models"] and any(x.startswith("--url") for x in a)),
    ((0, 1, 79), "llm status", lambda a: a[:2] == ["llm", "status"]),
    ((0, 1, 79), "wizard models --status", lambda a: a[:2] == ["wizard", "models"] and "--status" in a),
    ((0, 1, 78), "workspace add --name", lambda a: a[:2] == ["workspace", "add"] and any(x.startswith("--name") for x in a)),
    ((0, 1, 78), "limits --show", lambda a: a[:1] == ["limits"] and "--show" in a),
    ((0, 1, 78), "the default name_style", lambda a: a[:1] == ["defaults"] and a[1:3] in (["set", "name_style"],
                                                                                         ["unset", "name_style"])),
    ((0, 1, 78), "net show --keep-all", lambda a: a[:2] == ["net", "show"] and "--keep-all" in a),
    ((0, 1, 78), "mcp attach --live and --helper", lambda a: a[:2] == ["mcp", "attach"] and (
        "--live" in a or any(x.startswith("--helper") for x in a))),
    ((0, 1, 78), "mcp detach", lambda a: a[:2] == ["mcp", "detach"]),
    ((0, 1, 98), "the primary password of the secrets (secrets status, unlock, lock, password)",
     lambda a: a[:1] == ["secrets"] and a[1:2] and a[1] in ("status", "unlock", "lock", "password")),
    ((0, 1, 98), "kits host-list, host-show and host-import (what the agents there have of their own)",
     lambda a: a[:1] == ["kits"] and a[1:2] and a[1].startswith("host-")),
    ((0, 1, 78), "plugins marketplaces", lambda a: a[:2] == ["plugins", "marketplaces"]),
    ((0, 1, 117), "the update mode (sandbox-ai update mode)", lambda a: a[:2] == ["update", "mode"]),
    ((0, 1, 159), "a search in the session files (session files --search, --text)",
     lambda a: a[:2] == ["session", "files"] and any(x.startswith(("--search", "--text")) for x in a)),
    ((0, 1, 146), "a file marked as a login or key (session files|memory --show-secret)",
     lambda a: a[:1] == ["session"] and "--show-secret" in a),
    ((0, 1, 146), "an export with its logins and keys (session export --include-secrets)",
     lambda a: a[:2] == ["session", "export"] and "--include-secrets" in a),
    ((0, 1, 151), "the storage check (gc --check, --only, --compact-transcripts)", lambda a: a[:1] == ["gc"] and any(
        x in ("--check", "--only", "--compact-transcripts") for x in a)),
    ((0, 1, 151), "the storage check's defaults (storage_...)", lambda a: a[:1] == ["defaults"] and a[1:2] in (
        ["set"], ["unset"]) and a[2:3] and a[2].startswith("storage_")),
    ((0, 1, 146), "the default hide_secrets", lambda a: a[:1] == ["defaults"] and a[1:3] in (["set", "hide_secrets"],
                                                                                            ["unset", "hide_secrets"])),
    ((0, 1, 78), "plugins --marketplaces-only", lambda a: a[:1] == ["plugins"] and "--marketplaces-only" in a),
    ((0, 1, 78), "kits enable-plugin, disable-plugin, remove-marketplace and import-marketplace",
     lambda a: a[:1] == ["kits"] and a[1:2] and a[1] in KIT_NEW_ACTIONS),
    ((0, 1, 78), "plugins and marketplaces of the default kit changed by sandbox-ai kits",
     lambda a: a[:1] == ["kits"] and a[1:2] and a[1] in KIT_DEFAULT_ACTIONS and "--" in a
     and a[a.index("--") + 1:a.index("--") + 2] == ["default"]),
)


UPDATE_MODES = ("off", "notify", "download", "auto")  # updates.py MODES
UPDATE_WINDOW = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9](-([01][0-9]|2[0-3]):[0-5][0-9])?")


def version_text(key):
    return ".".join(str(part) for part in key)


def too_old(host, version, what, since=None):
    """The refusal of a command HOST's sandbox-ai (VERSION, or None: not known) does not know."""
    needed = f" (it came with {version_text(since)})" if since else ""
    return Refused(409, f"sandbox-ai {version or ''}{' ' if version else ''}on {host} does not know {what}{needed}: "
                        "update it on the page Hosts (Update, or Update from this machine's copy)", code="host-too-old",
                   params={"host": host, "version": version or "", "what": what})


def newer_needed(host, args):
    """A refusal when HOST's sandbox-ai is known to be older than what ARGS need (NEWER), else None."""
    hosts = module("hosts")
    if host == hosts.LOCAL:
        return None
    for since, what, test in NEWER:
        if test(list(args)):
            version = hosts.older_than(host, since)
            if version:
                return too_old(host, version, what, since)
    return None


def too_old_said(host, args, status, err):
    """A refusal when HOST's sandbox-ai refused ARGS, which hold what only newer versions know (NEWER), as argparse
    refuses what it does not know (status 2: "unrecognized arguments", "invalid choice", or "the following arguments
    are required" where an older parser reads a newer form's words otherwise, as 0.1.77's `wizard trust --list` and
    `mcp attach --helper`): the page then says to update it, not just the usage line."""
    if host == module("hosts").LOCAL or status != 2 or not re.search(
            r"unrecognized arguments|invalid choice|the following arguments are required", err or ""):
        return None
    what = next((what for _, what, test in NEWER if test(list(args))), None)  # other callers read such a refusal
    return too_old(host, module("hosts").known_version(host)[0], what) if what else None  # themselves (doctor)


class Refused(Exception):
    """STATUS and MESSAGE for the browser; CONFIRM: the NEEDS_CONFIRM action the request lacks the confirmation of
    (the answer names it, so a page whose view was out of date asks for exactly that); CODE (one of ERROR_CODES) and
    PARAMS ({name: text}): which common refusal it is, for the page's own words."""

    def __init__(self, status, message, confirm=None, code=None, params=None):
        super().__init__(message)
        if code is not None and code not in ERROR_CODES:
            raise KeyError(code)  # a refusal naming a code the page cannot know: a bug, never a pass
        self.status, self.confirm, self.code = status, confirm, code
        self.params = {key: printable(str(value))[:300] for key, value in (params or {}).items()}


# Session bridges: a session as `bridge add` takes it (NAME, or HOST:NAME of a host of that machine's), a bridge's id
# (ID or HOST:ID), a capability, --until and a --limit value.
BRIDGE_SESSION = re.compile(r"(?:[a-z0-9][a-z0-9_-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
BRIDGE_ID = re.compile(r"(?:[a-z0-9][a-z0-9_-]{0,31}:)?[0-9a-f]{8}")
BRIDGE_CAP = re.compile(r"message|ask|watch|files:(?:ro|rw):[^,\x00-\x1f\x7f]{1,4096}")
BRIDGE_UNTIL = re.compile(r"[0-9]{1,6}[smhd]|(?:[01]?[0-9]|2[0-3]):[0-5][0-9]|"
                          r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+Z-]{4,25}")
BRIDGE_LIMIT = re.compile(r"[0-9]{1,12}[kKmMgG]?")


def bridge_session(value, what):
    if not (isinstance(value, str) and BRIDGE_SESSION.fullmatch(value)):
        raise Refused(400, f"{what}: a session's name, or HOST:NAME of one on a host of yours")
    return value


def bridge_id(value):
    if not (isinstance(value, str) and BRIDGE_ID.fullmatch(value)):
        raise Refused(400, "a bridge is its id (eight hex digits), or HOST:ID")
    return value


def host_name(value):
    hosts = module("hosts")
    if value != hosts.LOCAL and not (isinstance(value, str) and hosts.NAME.fullmatch(value)):
        raise Refused(400, "no such host name", code="no-host",
                      params={"host": value if isinstance(value, str) else ""})
    return value


# A host name: labels of letters, digits and "-", and "_" as DNS names have it in practice (_sip._tcp.example.org,
# SRV-like and service names); never a leading "-" (read as an option).
NET_HOST = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62})(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62}))*")
# A host wildcard as `sandbox-ai net allow` takes it: *.DOMAIN.TLD, at least two labels below the star (*.org would
# open a whole top-level domain). The command refuses public suffixes itself (runtime.wildcard_suffix); one below a
# shared service is asked about first (NEEDS_CONFIRM "shared-wildcard"), then passed as --allow-shared-wildcard.
NET_WILDCARD = re.compile(r"\*\.(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}")
NET_TCP = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:.]+\]):[0-9]{1,5}")
NET_GIT_URL = re.compile(r"https?://(?:[A-Za-z0-9._-]{1,64}@)?(?:[A-Za-z0-9.-]{1,253}|\[::1\])(?::[0-9]{1,5})?/?")
NET_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


# What a running sandbox is given while it runs (sandbox-ai workspace, devices, limits): never a leading "-".
DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9_+:.@-][A-Za-z0-9_+:.@/-]{0,250}")
USB_SELECTOR = re.compile(r"usb:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}(?::[!-~]{1,126})?")  # usb:VENDOR:PRODUCT[:SERIAL]
DEVICE_ID = re.compile(r"[0-9]{1,9}")  # a grant's id, as devices list shows it
# sandbox-ai install's items (install.ITEM): KIND:NAME, never an option.
INSTALL_ITEM = re.compile(r"(image|mcp|toolchain|preset):[A-Za-z0-9][A-Za-z0-9_.@+:-]{0,127}")
INSTALL_ITEMS_LIMIT = 200
TOOLCHAIN = re.compile(r"(?!bin(?:@|$))[a-z][a-z0-9-]{0,31}(?:@[0-9A-Za-z][0-9A-Za-z.+_-]{0,63})?")  # toolchains.spec
WORKSPACE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+@ -]{0,254}")  # the NAME of /workspaces/NAME
WORKSPACE_AS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # workspace add --name NAME (workspaces.NAME)
# devices add waits up to devices.APPROVAL_SECONDS (60) for the answer to an "ask" rule; over SSH, with room to spare
DEVICE_ADD_TIMEOUT = 180
LIMIT_VALUE = re.compile(r"none|[0-9]{1,12}(?:\.[0-9]{1,6})?[kmgtKMGT]?")
SIZE_UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}  # as limits takes them (devices.bytes_of)


# `sandbox-ai hosts install` (on the machine of the service: the local sandbox-ai copies itself, or install.sh with the
# release keys, to the host over SSH). Each of its steps may take up to hosts.INSTALL_TIMEOUT (900 s): the copy and
# install.sh; the route waits for all of them, with room to spare. The page stays usable meanwhile.
INSTALL_PREFIX = re.compile(r"/[A-Za-z0-9._/-]{0,200}")
INSTALL_KEYS = ("mode", "prefix", "sudo", "source", "version")


def install_route_timeout():
    return 2 * getattr(module("hosts"), "INSTALL_TIMEOUT", 900) + 120


def uninstall_argv(name, which, flags, dry_run=False):
    """`sandbox-ai hosts uninstall NAME --json ...` of a checked choice (Api.uninstall_choice)."""
    return ["hosts", "uninstall", name, "--json", *(["--installation", which] if which else []),
            *(["--purge"] if flags.get("purge") else []), *(["--secrets"] if flags.get("secrets") else []),
            *(["--mcp-data"] if flags.get("mcp_data") else []),
            *(["--stop-running"] if flags.get("stop_running") and not dry_run else []),
            *(["--remove-from-list"] if flags.get("remove") and not dry_run else []),
            "--dry-run" if dry_run else "--yes"]


def install_choice(body):
    """{"mode", "prefix", "sudo", "source", "version"} of BODY checked as `sandbox-ai hosts install` takes them (mode
    user or system, or null: the host decides (system when the SSH user is root); prefix only for system, an
    absolute path of letters, digits, . _ - and / without . or .. parts; sudo true, false or null (with sudo when a
    system installation as no root needs it; null for mode user, which never uses it); source here (this machine's copy) or download (the signed release),
    version X.Y.Z only with download)."""
    hosts = module("hosts")
    if set(body) - {*INSTALL_KEYS, "confirm"}:
        raise Refused(400, "an install has mode, prefix, sudo, source and version")
    mode, prefix, sudo = body.get("mode"), body.get("prefix") or None, body.get("sudo")
    source, version = body.get("source") or "here", body.get("version") or None
    if mode not in (None, "user", "system"):
        raise Refused(400, "mode is user, system or null (the host decides)")
    if prefix is not None:
        if mode != "system":
            raise Refused(400, "a prefix is for an installation for every user (mode system)")
        if not (isinstance(prefix, str) and INSTALL_PREFIX.fullmatch(prefix)):
            raise Refused(400, "the prefix is an absolute path of letters, digits, . _ - and /")
        no_dots(prefix, "the prefix")
        if "//" in prefix or (len(prefix) > 1 and prefix.endswith("/")):
            raise Refused(400, "the prefix is an absolute path without empty parts")
    if sudo not in (None, True, False):
        raise Refused(400, "sudo is true, false or null (when needed)")
    if sudo is True and mode == "user":  # a user's own installation never uses sudo (hosts.install): a retry that
        sudo = None                      # names it anyway (from here, with the host's update setting) is not refused
    if source not in ("here", "download"):
        raise Refused(400, "source is here (this machine's sandbox-ai) or download (the signed release)")
    if version is not None:
        if source != "download":
            raise Refused(400, "a version is for download (this machine's copy is its own version)")
        if not (isinstance(version, str) and hosts.VERSION.fullmatch(version)):
            raise Refused(400, "version is X.Y.Z")
    return {"mode": mode, "prefix": prefix, "sudo": sudo, "source": source, "version": version}


def install_argv(name, choice, as_json=True):
    """The arguments of the local sandbox-ai that install it on host NAME as CHOICE (install_choice) says."""
    args = ["hosts", "install", name]
    args += {"user": ["--user"], "system": ["--system"], None: []}[choice["mode"]]
    args += [f"--prefix={choice['prefix']}"] if choice["prefix"] else []
    args += {True: ["--sudo"], False: ["--no-sudo"], None: []}[choice["sudo"]]
    args += ["--download"] if choice["source"] == "download" else ["--from-here"]
    args += [f"--version={choice['version']}"] if choice["version"] else []
    return args + (["--json"] if as_json else [])


def install_words(choice, entry):
    """How CHOICE installs, in words for its confirmation (for whom, where, with sudo or not, from where). Whether
    the SSH user is root is the host's to say (an ssh_config "User root" has no root@ in the entry): never guessed
    here, so a question never says "as root" for what may need sudo, or the other way round."""
    mode = choice["mode"]
    who = f"for every user in {choice['prefix'] or '/usr/local'}" if mode == "system" else "for the SSH user" \
        if mode == "user" else "for every user when the SSH user is root, else for the SSH user (the host decides)"
    how = "without sudo" if mode == "user" or choice["sudo"] is False else "with sudo" if choice["sudo"] is True \
        else "with sudo unless the SSH user is root"
    where = (f"the signed release {choice['version']}" if choice["version"] else "the signed release") \
        if choice["source"] == "download" else "this machine's copy"
    return f"{who}, {how}, from {where}"


def limit_number(key, value):
    """A limit as limits --show gives it (bytes, CPUs, processes) from VALUE as limits takes it; none: infinite."""
    if value == "none":
        return float("inf")
    if key == "memory" and value[-1:].lower() in SIZE_UNITS:
        return float(value[:-1]) * SIZE_UNITS[value[-1].lower()]
    return float(value)
# The open questions of every host, asked at once: each host gets APPROVALS_HOST_SECONDS (an unreachable one is
# named in "errors", the others are shown all the same).
APPROVALS_HOST_SECONDS = 15
STARTS_HOST_SECONDS = 20  # the same for the starts of every host (GET /api/starts)


CANARY_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
REVIEW_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # workspace_review.RUN_ID
SAFEGUARDS_TAIL = 500  # safeguard records a page asks a host for at most (the newest)
# What `approvals remembered` lists for a project: a host name (or *.wildcard), IP:PORT, [IPv6]:PORT; never "-...".
REMEMBERED_ENTRY = re.compile(r"(?:\*\.)?[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\]:[0-9]{1,5}")
VPN_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
VPN_LOCATION = re.compile(r"[a-z]{2}(?:-[a-z0-9-]{1,20})?")
VPN_LOGIN_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?")


def vpn_name(value):
    if not (isinstance(value, str) and VPN_NAME.fullmatch(value)):
        raise Refused(400, "a profile name is 1-32 lower-case letters, digits, '_' or '-'")
    return value


LLM_KINDS = ("lmstudio", "ollama", "llamacpp", "vllm", "openai-compatible")  # llm.KINDS
# http(s)://HOST[:PORT][/PREFIX] of a server not registered yet, as `wizard models --url` takes it (llm.URL): never
# a leading "-", no user, no query; the command checks it again.
LLM_URL = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})(?::[0-9]{1,5})?"
                     r"(?:/[A-Za-z0-9._~/-]{0,200})?")


def llm_kind(value):
    if value not in LLM_KINDS:
        raise Refused(400, f"kind is one of {', '.join(LLM_KINDS)}")
    return value


def secret_name(value):
    if not (isinstance(value, str) and NET_REF.fullmatch(value)):
        raise Refused(400, "a secret's name is letters, digits and _ (up to 64), not starting with a digit")
    return value


LAUNCHER_WARNING = re.compile(r"^(?:agent-sandbox|sandbox-ai(?: [a-z-]+)?): warning: (.+)$")


def launcher_warnings(text):
    """[warning] of the launcher's own in TEXT (a command's standard error and output), each once, printable and at
    most 20 of 2000 characters: you decide what a sandbox gets - a host path with a risk (HOME, sandbox-ai's own
    folders, a kernel tree...) is given, and such a line says what it means."""
    found = []
    for line in str(text or "").splitlines():
        match = LAUNCHER_WARNING.match(line.strip())
        if match and printable(match.group(1))[:2000] not in found:
            found.append(printable(match.group(1))[:2000])
    return found[:20]


CHECKPOINT_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")  # checkpoints.ID


def checkpoint_id(value):
    if not (isinstance(value, str) and CHECKPOINT_ID.fullmatch(value)):
        raise Refused(400, "a checkpoint's id is DATE-TIME-HEX (sandbox-ai checkpoint list)")
    return value


SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")  # the launcher's SESSION_NAME (--session, --as)


def start_name(value):
    if not (isinstance(value, str) and START_NAME.fullmatch(value)):
        raise Refused(400, "a start's name is 1-48 letters, digits, '_', '.' or '-'", code="bad-name",
                      params={"what": "start"})
    return value


# The commands of the storage check's plan (starts.start_plan, global_plan) the page may run: gc's own forms only.
STORAGE_FLAGS = {"--only", "--caches", "--build-output", "--toolchains", "--images", "--yes"}
STORAGE_DAYS = {"--compact-transcripts", "--transcripts", "--logs-older-than"}


def storage_argv(argv):
    """ARGV when it is one of the gc commands the storage check names (gc; gc --only with --caches, --build-output,
    --toolchains, --images, --compact-transcripts DAYS, --transcripts DAYS, --logs-older-than DAYS, --home NAME,
    --yes; gc --pack --home NAME --yes; gc --unpack NAME), else Refused (400)."""
    refused = Refused(400, "argv is a command of the storage check's plan (sandbox-ai gc --check)")
    if not isinstance(argv, list) or not argv or argv[0] != "gc" or len(argv) > 12 or \
            not all(isinstance(part, str) for part in argv):
        raise refused
    rest = argv[1:]
    if rest[:1] == ["--unpack"]:
        if len(rest) == 2 and START_NAME.fullmatch(rest[1]):
            return list(argv)
        raise refused
    if rest[:1] == ["--pack"]:
        if len(rest) == 4 and rest[1] == "--home" and START_NAME.fullmatch(rest[2]) and rest[3] == "--yes":
            return list(argv)
        raise refused
    if rest and rest[0] != "--only":
        raise refused
    index = 0
    while index < len(rest):
        part = rest[index]
        if part in STORAGE_FLAGS:
            index += 1
        elif part in STORAGE_DAYS and index + 1 < len(rest) and re.fullmatch(r"[1-9][0-9]{0,4}", rest[index + 1]):
            index += 2
        elif part == "--home" and index + 1 < len(rest) and START_NAME.fullmatch(rest[index + 1]):
            index += 2
        else:
            raise refused
    return list(argv)


def listed_file(value):
    """A file's name as `sandbox-ai session memory|files` lists it (a path inside the start's HOME): one printable
    line; the command takes only a name of its own listing."""
    if not (isinstance(value, str) and value and len(value) <= 4096 and value.isprintable()
            and not value.startswith(("/", "-"))):
        raise Refused(400, "a file is named as the listing names it")
    return value


def new_start_name(value):
    """The name a start is given (--name, rename NEW): 1-40 characters as the launcher's SESSION_NAME takes them (a
    start is named by its id too, up to 48: start_name)."""
    if not (isinstance(value, str) and NEW_START_NAME.fullmatch(value)):
        raise Refused(400, "a start's new name is 1-40 letters, digits, '_', '.' or '-', starting with a letter or "
                           "digit", code="bad-name", params={"what": "start"})
    return value


# Kits (sandbox-ai kits): the names as kits.py has them; every value is checked here and again by the command.
KIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PLUGIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}@[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # kits.PLUGIN_NAME twice
LIVE_ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}")  # kits_live.ENTRY_NAME, or a kit's name
KIT_SKILL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # a new skill's name (Claude's rule)
KIT_PLUGIN = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._^~<>=*+-]{0,200}")  # NAME@MARKETPLACE or an npm package
KIT_ACTIONS = ("add-skill", "new-skill", "add-plugin", "add-marketplace", "remove-skill", "remove-plugin",
               "add-hook", "remove-hook", "enable-plugin", "disable-plugin", "remove-marketplace", "import-marketplace")
KIT_NEW_ACTIONS = ("enable-plugin", "disable-plugin", "remove-marketplace", "import-marketplace")  # since 0.1.78
# What sandbox-ai kits changes of the default kit (part of every sandbox): its plugins and marketplaces, nothing else
KIT_DEFAULT_ACTIONS = ("add-plugin", "remove-plugin", "enable-plugin", "disable-plugin", "add-marketplace",
                       "remove-marketplace", "import-marketplace")
# What a kit change does to every later start with that kit: widens it (asked first: kit-add) or narrows it
# (kit-remove)
KIT_WIDENS = ("add-skill", "add-plugin", "add-marketplace", "add-hook", "enable-plugin", "import-marketplace")
KIT_NARROWS = ("remove-skill", "remove-plugin", "remove-hook", "disable-plugin", "remove-marketplace")


AGENT_PART = re.compile(r"[a-z0-9]{2,12}:[a-z-]{3,16}:[^\x00-\x1f\x7f]{1,500}")


def agent_part_id(value):
    """A part's id of sandbox-ai kits host-list (AGENT:KIND:NAME); the command checks it again."""
    if not (isinstance(value, str) and AGENT_PART.fullmatch(value) and value.isprintable()):
        raise Refused(400, "a part is AGENT:KIND:NAME as sandbox-ai kits host-list shows it")
    return value


def kit_name(value, what="a kit's name"):
    if not (isinstance(value, str) and KIT_NAME.fullmatch(value)) or value in ("default", "built-in"):
        raise Refused(400, f"{what} is 1-64 letters, digits, '_', '.' or '-' (not default or built-in)")
    return value


def kit_agents(body, many=True):
    """--agent=AGENT options of BODY's "agents" (a list) or "agent" (one)."""
    values = strings(body.get("agents", []), "agents", 3) if many else (
        [body["agent"]] if body.get("agent") is not None else [])
    if not all(value in AGENTS for value in values):
        raise Refused(400, "an " + agent_words())
    return [f"--agent={value}" for value in dict.fromkeys(values)]


def host_path(value, what):
    if not (isinstance(value, str) and value.startswith("/") and "\0" not in value and len(value) < 4096):
        raise Refused(400, f"{what} is an absolute path on that host", code="bad-path", params={"what": what})
    return value


def no_dots(value, what):
    """VALUE (an absolute path) without a "." or ".." part, printable."""
    if not value.isprintable() or any(part in (".", "..") for part in value.split("/")):
        raise Refused(400, f"{what}: a printable absolute path without . or .. parts", code="bad-path",
                      params={"what": what})
    return value


def toolchain_value(value):
    """A toolchain as `sandbox-ai toolchain attach` takes it: NAME or NAME@VERSION."""
    if not (isinstance(value, str) and TOOLCHAIN.fullmatch(value)):
        raise Refused(400, "a toolchain is NAME or NAME@VERSION (lower-case letters, digits and '-')")
    return value


def device_value(value):
    """A device as `sandbox-ai devices add` takes it: a path under /dev or usb:VENDOR:PRODUCT[:SERIAL]."""
    if isinstance(value, str) and USB_SELECTOR.fullmatch(value):
        return value
    if not (isinstance(value, str) and DEVICE_PATH.fullmatch(value)):
        raise Refused(400, "a device is a path under /dev or usb:VENDOR:PRODUCT[:SERIAL] (four hex digits each)")
    return no_dots(value, "a device")


# The parity actions (Projects, MCP servers, logins and identities, images, activity, ...): every value is checked here
# as the command takes it, and passed as --flag=VALUE or after its options, never starting with "-".
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTITY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # identities.NAME
LOGIN_USER = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")  # git_logins.USER, never a leading "-"
EMAIL = re.compile(r"[^\s<>@,;\"'-][^\s<>@,;\"']{0,63}@[A-Za-z0-9.-]{1,253}")  # identities.EMAIL
PUBLISH_KINDS = ("crates", "github", "gitlab", "npm", "pypi", "testpypi")
PUBLISH_URL = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})/?")  # publish_logins: https://HOST, no path
MCP_ENTRY = re.compile(r"(?:[a-z0-9][a-z0-9-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # duckduckgo, hub:binwalk
# An MCP server's network in the agents' words (mcp.NETWORK_WORDS), and back to what every sandbox-ai takes.
MCP_NETWORK_WORDS = {"none": "offline", "full": "egress-all"}
MCP_NETWORK_ALIASES = {word: stored for stored, word in MCP_NETWORK_WORDS.items()}


def mcp_network_words(text):
    """A catalog line's network ("full duckduckgo.com"), its first word in the agents' words (egress-all ...)."""
    word, space, rest = str(text).partition(" ")
    return MCP_NETWORK_WORDS.get(word, word) + space + rest


MCP_CATALOG_LINE = re.compile(r"(?P<prefix>[^:\s]+):(?P<name>\S+)  \[(?P<network>[^\]]*)\]  (?P<title>.*?)"
                              r"(?P<unpublished>  \(not published yet\))?  (?P<signature>signed|SIGNATURE BAD|unsigned)")
# What `sandbox-ai mcp add` says when it refuses an entry only because its catalog no trusted key signed
# (mcp.check_signature; the app's backend.MCP_UNSIGNED_HINT the same): `mcp add` has no --json, its words are the
# signal. The page then offers to add it anyway (--unsigned, asked: mcp-unsigned); a signature that does not verify
# is a different refusal, never offered.
MCP_UNSIGNED_HINT = "--unsigned adds it anyway"
MCP_UNSIGNED_SOURCE = re.compile(r"\((?P<catalog>(?:https://|/)\S+?)\.sig cannot be read\)")
SIZE_VALUE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,6})?[kmgtKMGT]?")
NET_PRESETS = ("pypi", "github", "gitlab", "kernel-org", "duckduckgo", "npm")  # agent_sandbox.PRESETS (a test agrees)
NETWORK = re.compile(r"[0-9A-Fa-f:.]{2,45}(?:/[0-9]{1,3})?")
BUILD_TARGET = re.compile(r"[a-z0-9][a-z0-9_.-]{0,29}(?:@[a-z]{1,20})?")  # (NAME@AGENT: that agent's variant NAME)  # codex, claude, opencode, opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi, all, team, cairn, artex, vpn, tor, browser or a variant
HOOK_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HOOK_EVENT = re.compile(r"[A-Z][A-Za-z]{1,40}")
HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
PRESET_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
ACTIVITY_KINDS = ("prompt", "reply", "tool", "command", "file", "network", "approval", "mcp", "session", "tokens")
ACTIVITY_SINCE = re.compile(r"[0-9]{1,5}[smhd]|[0-9]{1,2}:[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9:.]{1,15}(?:Z|[+-][0-9:]{1,5})?)?")
TOR_COUNTRIES = re.compile(r"[a-z]{2}(?:,[a-z]{2}){0,15}")
TS_HOSTNAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DOCTOR_BACKENDS = ("podman", "bwrap", "both")
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # team.TEAM_NAME
# a team whose record is damaged: NAME  (damaged)  remove it with: sandbox-ai team delete NAME --yes
TEAM_DAMAGED_LINE = re.compile(r"(?P<name>[a-z0-9][a-z0-9_-]{0,19})\s+\(damaged\)(?:\s.*)?")
IMAGE_KINDS = ("apt", "npm", "pip")  # images extend --add-KIND / --remove-KIND
IMAGE_PACKAGE = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._+~=<>!*^:\[\],-]{0,200}")  # images.py checks each kind's own
IMAGE_MODE = re.compile(r"0?[0-7]{3}")
IMAGE_RECIPES = ("media", "android", "android-dev")  # images.RECIPES the web UI offers (the recipe tor is gone:
# its container carries Tor; android-dev: the preset android-development's image)
FRAGMENT_LIMIT = 64 << 10  # images.FRAGMENT_LIMIT


def identity_name(value):
    if not (isinstance(value, str) and IDENTITY_NAME.fullmatch(value)):
        raise Refused(400, "an identity's name is 1-32 lower-case letters, digits, '_' or '-'")
    return value


def git_url(value, what="url"):
    if not (isinstance(value, str) and NET_GIT_URL.fullmatch(value)):
        raise Refused(400, f"{what} is http(s)://[USER@]HOST[:PORT]")
    return value


def publish_kind(value):
    if value not in PUBLISH_KINDS:
        raise Refused(400, f"kind is one of {', '.join(PUBLISH_KINDS)}")
    return value


def flag(body, key):
    """BODY[KEY] as a bool (absent: False); anything else is refused."""
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise Refused(400, f"{key} is true or false")
    return value


def whole(value, what, low, high):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused(400, f"{what} is a whole number from {low} to {high}")
    return value


def ip_value(value, what, network=False):
    """An IP address (or with NETWORK an IP or CIDR network) as text, checked by ipaddress."""
    import ipaddress
    if not (isinstance(value, str) and NETWORK.fullmatch(value)) or "/" in value and not network:
        raise Refused(400, f"{what}: an IP address{' or network (CIDR)' if network else ''}")
    try:
        ipaddress.ip_network(value, strict=False) if network else ipaddress.ip_address(value)
    except ValueError:
        raise Refused(400, f"{what}: not an IP address{' or network' if network else ''}: {printable(value)[:64]}") \
            from None
    return value


def clean_path(value, what):
    """An absolute path on the host, printable, without . or .. parts (never read as an option)."""
    return no_dots(host_path(value, what), what)


def remote_source(value, what):
    """VALUE when it is a remote folder (sshfs://[USER@]HOST[:PORT]/PATH): its canonical spelling, as the launcher
    takes it (remotefs.parse; that host mounts it with sshfs); None for anything else. 400 with what is wrong."""
    remote = module("remotefs")
    if not remote.is_remote(value):
        return None
    try:
        return remote.parse(value)["source"]
    except ValueError as exc:
        raise Refused(400, f"{what}: {exc}") from None


def path_or_remote(value, what):
    """An absolute path on the host (clean_path) or a remote folder (remote_source)."""
    return remote_source(value, what) or clean_path(value, what)


def catalog_name(prefix, given=()):
    """The catalog behind PREFIX of `mcp catalog`'s lines, for people: sandbox-ai-mcp (mcp:) or axross-mcp-hub
    (hub:) of the default ones, the Nth catalog GIVEN for cN: (mcp.catalog_label)."""
    mcp = module("mcp")
    numbered = re.fullmatch(r"c([0-9]{1,2})", str(prefix))
    if numbered and int(numbered[1]) < len(given) and isinstance(given[int(numbered[1])], str):
        return printable(mcp.catalog_label(given[int(numbered[1])]))[:300]
    source = dict(mcp.CATALOGS).get(prefix)
    return printable(mcp.catalog_label(source))[:300] if source else printable(str(prefix))[:300]


def mcp_env_spec(value):
    """One variable of `mcp add --env` checked as the command checks it (mcp.parse_env_spec): NAME (that host's
    environment's, else its secret NAME), NAME=VALUE (a value that is no secret: one line, at most 1024 characters,
    nothing of a secret's name or shape) or NAME@SECRET (that host's secret SECRET). 400 with its words otherwise."""
    try:
        module("mcp").parse_env_spec(value, "a variable")
    except ValueError as exc:
        raise Refused(400, printable(str(exc))[:600]) from None
    return value


def catalog_sources(body):
    """--catalog=FILE_OR_URL options of BODY's "catalogs": absolute paths on the host or https:// URLs."""
    found = []
    for value in strings(body.get("catalogs", []), "catalogs", 8):
        if isinstance(value, str) and value.startswith("https://") and value.isprintable() and " " not in value:
            found.append(f"--catalog={one_line(value, 'a catalog', 2048)}")
        else:
            found.append(f"--catalog={clean_path(value, 'a catalog')}")
    return found


# What ends, removes or weakens something: ONE table, consulted by every route (needs(), confirmed()), each route
# naming the actions it may need (Api.routes' fourth element; tests/test_web.py checks every destructive or weakening
# route against it). GET /api/policy serves it, so the page asks for exactly these, before it sends {"confirm": true}.
NEEDS_CONFIRM = {
    "checkpoint-restore": "a restore puts the workspaces back as the checkpoint holds them: what changed since is "
                          "replaced or removed (a checkpoint of the state now is taken first, so it can be undone)",
    "checkpoint-delete": "deleting a checkpoint removes it and the content only it held, for good",
    "login-import": "every sandbox of that agent started from now on uses this session's login: the central login is "
                    "replaced (with replace: also one of another account); the session keeps its own copy",
    "login-give": "an agent login given to a running sandbox lets its agent work as that account from now on; the "
                  "two then share the login",
    "login-edit": "a changed login is what every later start that uses it logs in with (a sandbox that runs keeps "
                  "the login it was started with)",
    "secret-rename": "a renamed secret gets its new name in every login, VPN profile, trusted project file and kept "
                     "start that names it",
    "secret-send": "a secret sent to another host is kept in that host's secret store from then on: whoever controls "
                   "that host can use it",
    "session-export": "the archive holds the start's HOME as its agent left it - its transcripts, its memory, the "
                      "files it kept (logins and keys are left out) - in a new file on that host",
    "session-export-secrets": "the archive then holds the start's logins, keys and secrets too: whoever has it can "
                              "log in as you with them, wherever it goes",
    "session-grant-approvals": "the network approvals are granted again as options of that session: it reaches those "
                               "destinations at every start",
    "session-import": "the imported session's configuration gives the sandbox secrets or folders of this machine (as "
                      "listed): they reach it at its first resume",
    "show-secret-file": "the file is marked as a login or key: its text is shown on this screen, where anyone who "
                        "sees it can read it",
    "board-import": "importing a bundle from elsewhere writes its parts here (MCP and LLM servers, kits, presets, "
                    "rulesets, image recipes; replaced ones when chosen) and keeps its board",
    "start-stop": "stopping a start ends what runs in it (and its HOME, unless it is kept)",
    "kits-attach": "skills, subagents, commands or a plugin given to a running start reach its agent at once "
                   "(read-only; a plugin's code runs as the agent; kept: also its later starts)",
    "kits-detach": "taking a skill, subagent, command or plugin out of a running start takes it from its agent (and "
                   "from its later starts where it was kept)",
    "agent-restart": "restarting the agent inside the running sandbox ends what it does now (its conversation "
                     "continues; the sandbox, network and MCP servers stay)",
    "start-delete": "deleting a start removes its kept HOME",
    "gc": "gc removes the leftovers of ended starts, and kept starts unused for that long with their HOME, for good",
    "storage-apply": "the chosen items of the storage check run: caches, build output, toolchains or old transcripts "
                     "removed for good, transcripts compacted, HOMEs packed - as each one says",
    "approval-forget": "forgetting a remembered answer changes what every later start of that project gets (a "
                       "forgotten block is asked again)",
    "defaults-lower": "this lowers the protection of every later start on that host",
    "shared-wildcard": "a host wildcard below a shared service opens every name anyone rents there: the agent could "
                       "open any such server, and send data there",
    "net-allow": "net allow widens what the running sandbox reaches, at once (real DNS, ports on the host, host "
                 "wildcards, Git logins included)",
    "net-untaint": "untaint takes back the rules for tainted runs: the sandbox is treated as if no untrusted content "
                   "had entered it",
    "workspace-writable": "a writable workspace lets the agent change the directory on that host",
    "workspace-force": "a forced removal leaves what a process there still holds usable, also for writing, until it "
                       "is closed; a copy the agent took stays usable until the sandbox restarts",
    "device-add": "a device gives the agent that hardware of the host",
    "toolchain-add": "a toolchain gives the agent that compiler or interpreter, read-only, and opens the package "
                     "registries it names for the run",
    "toolchain-force": "a forced detach leaves what a process there still runs or holds of the toolchain usable "
                       "until it is closed",
    "limits-raise": "a limit above the current one (or none) lets the agent use more of that host",
    "host-remove": "removing a host ends this service's management of it",
    "vpn-remove": "removing a VPN profile stops the starts that use it from starting",
    "llm-remove": "removing an LLM server stops the starts that use it from starting",
    "kit-delete": "deleting a kit removes its folder",
    "kit-remove": "removing a skill or plugin from a kit takes it from every later start with that kit",
    "kit-add": "a skill, plugin or marketplace added to a kit is given to every later start with that kit",
    "memory-add": "memory added to a project's memory is given to every later start of that project (it may hold "
                  "private notes)",
    "mcp-attach": "an attached MCP server is reached by that start from its next start on (kept for its later "
                  "starts)",
    "mcp-restart": "restarting a running start ends what runs in it now, so that it reaches the MCP server (the agent "
                   "continues its conversation)",
    "host-edit": "changing a host's SSH target, port, key, command or sudo setting changes where and how this "
                 "service's commands run",
    "host-uninstall": "uninstalling sandbox-ai on a host removes that installation there (with --purge also the "
                      "SSH user's data, with --secrets its secrets and logins, which cannot be recovered)",
    "host-install": "installing sandbox-ai on a host puts this machine's copy (or the signed release) there, for the "
                    "SSH user or for every user, as root or with sudo",
    "secret-delete": "deleting a secret takes it from every start, VPN profile and Git login that names it",
    "secret-replace": "a secret of that name exists: its value is replaced",
    "project-trust": "a trusted project configuration gives every later start of that project its options",
    "project-forget": "a project configuration no longer trusted stops the starts of that project from starting "
                      "until it is trusted again",
    "mcp-stop": "stopping an MCP server ends it for every start that uses it now",
    "mcp-update": "the server's image is replaced by the newer one its catalog pins (its signature checked again; its "
                  "folders, variables, limits and network stay): every later start runs that image",
    "mcp-detach": "detaching an MCP server from a running start closes its relay there now: the agent's "
                  "connections to it end and its calls fail (its later starts go without it)",
    "mcp-unsigned": "an MCP server from a catalog no trusted key signed is taken without that check",
    "mcp-build": "an MCP server its catalog has no signed image of is built on that host from its source at the commit "
                 "and source archive shown, and added unsigned: nobody signed the image built",
    "mcp-writable-mount": "a writable mount lets the MCP server change that directory on the host",
    "mcp-lan": "network lan lets the MCP server reach your local network and that host, not only the internet",
    "git-login-add": "a Git login is used by the connection proxy of every sandbox that reaches that server",
    "login-replace": "a login for that server or kind exists: its token is replaced by this secret's for every later "
                     "start that uses it",
    "mcp-replace": "an MCP server of that name exists: its definition is replaced for every start that uses it",
    "mcp-copy": "an MCP server copied from another host is defined on this one, with the folders, variables and "
                "network shown (a server of that name replaced where asked), and reached by the start it is attached "
                "to",
    "login-remove": "removing a login or an identity takes it from every later start that uses it",
    "net-keep": "keeping what the run was given writes it into the project's .sandbox-ai.json: every later start of "
                "that project gets it",
    "start-restart": "restarting a running start ends what runs in it now (the agent continues its conversation)",
    "canary-tokens": "the run's canary tokens in full: decoys, but shown here they can be copied and planted",
    "canary-resume": "resuming a sandbox a canary paused lets it go on after something opened a decoy or sent a "
                     "token away",
    "llm-insecure": "plain http to an address outside your local networks is unencrypted: prompts, answers and the "
                    "key can be read on the way",
    "vpn-socks": "a SOCKS port in the sandbox lets what goes through it pass the connection proxy by",
    "vpn-replace": "replacing a VPN profile of that name changes the tunnel of every start that uses it",
    "llm-replace": "replacing an LLM server of that name changes where every start that uses it sends its prompts "
                   "and its key",
    "llm-share-key": "the key of a server shared with all hosts is kept in the secret store of each host it is sent "
                     "to from then on: whoever controls that host can use it",
    "llm-unshare": "unsharing removes the server's copies (and the keys sent) from every host it was shared with: "
                   "sessions there cannot start with it any more",
    "team-stop": "stopping a team ends what runs in every member and its board",
    "team-delete": "deleting a team removes its members with their HOMEs and its board",
    "image-file": "a host file added to an image's extension is built into the image: every later start of it can "
                  "read it",
    "image-remove": "removing an extension drops what it adds from the next build (a variant's image goes with it, "
                    "unless kept)",
    "login-logout": "logging out forgets the stored login of that agent: its sandboxes need a new login",
    "kit-hooks-trust": "trusted hooks of a kit run in every later start with that kit",
    "bridge-add": "a bridge lets one running session reach another as it allows (messages, questions, files, "
                  "observing) until either session ends or it is revoked",
    "bridge-revoke": "revoking a bridge ends it now: both sessions lose it at once",
    "bridge-resume": "resuming a bridge lifts its suspension: the sessions reach each other again as it allows",
    "notify-add": "a phone channel sends that host's questions and alarms - the kind, the run's name, a short what and, "
                  "with the detail full, destinations and paths - to the service it names",
    "notify-remove": "removing a phone channel: its questions and alarms no longer reach you there",
    "schedule-add": "a schedule starts that agent on that host by itself, on its timetable or trigger, under the "
                    "rules of a normal start; its questions are answered as its ask mode says (off: refused)",
    "schedule-remove": "removing a schedule: it starts nothing any more, and without keep_history its runs' "
                       "reports go too",
    "schedule-timer": "the timer runs sandbox-ai schedule tick every minute as that host's user (a systemd user "
                      "timer or a LaunchAgent): off, no schedule starts by itself",
    "update-mode": "the update mode decides what that host's sandbox-ai does by itself: off checks nothing, notify "
                   "announces a newer signed release, download also fetches and checks it, auto also installs it in "
                   "its window while nothing runs (a timer as that host's user)",
    # sandbox-ai spend raise: a running start's model requests may cost that much more than its budgets
    "spend-raise": "raising a budget lets the running start's model requests cost that much more (its refused "
                   "requests go again)",
    # the workspace review (sandbox-ai workspace review RUN --restore PATH) and the MCP tool pins (mcp tools NAME)
    "workspace-restore": "a restore puts the file back as it was at the start of the run: what the agent wrote there "
                         "is replaced (a file it added is removed)",
    "mcp-pin-repin": "re-pinning makes the list the server gave last the one its later lists are compared with: "
                     "what changed in it is no longer asked about",
    "mcp-pin-forget": "forgetting the pin lets the server's next list be pinned without a question (trust on first "
                      "use again)",
}
# The actions only a terminal asks for (its query carries confirm=1): no route names them.
TERMINAL_CONFIRM = ("login-logout", "kit-hooks-trust")
# The order of protection (weakest first) that decides whether a default is lowered: any step down needs a confirm.
MODE_ORDER = ("lax", "low", "medium", "high", "paranoid")
TELEMETRY_ORDER = ("off", "strict")  # telemetry is always off: no on (an old one on a host counts as off)
DEFAULT_MODE, DEFAULT_TELEMETRY = "medium", "off"
# The keys of defaults.json (agent_sandbox.DEFAULTS_KEYS but "version"; tests/test_web.py checks they agree): any
# other key is refused before anything reaches a host.
DEFAULT_KEYS = ("ask_timeout", "budget", "canaries", "checkpoint_count", "checkpoint_days", "checkpoint_every",
                "checkpoint_exclude", "checkpoint_gb", "checkpoint_mounts", "checkpoint_start", "connection_bodies",
                "desktop_notifications", "file_reads", "keep_days", "llm_traffic", "llm_traffic_limit", "log_days",
                "loop_guard", "mode", "monitor_interval", "name_style", "preset_dirs", "safeguard_mute", "subnet",
                "subnet6", "telemetry", "hide_secrets",
                # the storage check and its automatic rules (storage.py checks each value)
                "storage_check", "storage_threshold_gb", "storage_growth_gb", "storage_caches_days",
                "storage_compact_days", "storage_pack_days", "storage_logs_days", "package_min_age_days",
                "fix_questions",
                # whether a start tells its agent what its sandbox has (/sandbox/session/INTRO.md; intro.py)
                "agent_intro")
# (and the update mode's, changed by /update-mode: it makes the timer too)
FIX_QUESTIONS = ("all", "each")  # fixes.QUESTIONS: fix_questions' values
FILE_READS = ("folders", "files", "off")  # runtime.FILE_READS: file_reads' values
CONNECTION_BODIES = ("text", "all")  # runtime.CONNECTION_BODIES
LLM_TRAFFIC_MODES = ("off", "llm", "all")  # runtime.LLM_TRAFFIC_MODES: llm_traffic's values
DESKTOP_NOTIFICATIONS = ("all", "alarms", "none")  # agent_sandbox.DESKTOP_NOTIFICATIONS
NAME_STYLES = ("codename", "project")  # agent_sandbox.NAME_STYLES: name_style's values


def shared_risk(value):
    """(the risk in words, the shared suffix) of the host wildcard VALUE below a shared service, or None (an own
    domain's, or one the command refuses always: it says why)."""
    runtime = module("runtime")
    try:
        shared = runtime.shared_wildcard(value)
    except ValueError:
        return None
    return (runtime.shared_wildcard_risk(value), shared) if shared else None


# An action asked by a question of its own beside another one of the same request (CONFIRM_KEYS: the key its yes is
# sent as, else "confirm"): a wildcard below a shared service in a net allow, which the net-allow yes is not.
CONFIRM_KEYS = {"shared-wildcard": "confirm_shared_wildcard", "mcp-build": "confirm_build"}


MCP_BUILD_HINT = re.compile(r"`sandbox-ai mcp add (?P<name>[A-Za-z0-9_-]{1,32}) --build(?P<unsigned> --unsigned)?"
                            r"(?P<mounts>(?: --mount [^`\s]+)*)` builds it here")


def mcp_build_hints(text):
    """[{"name", "unsigned", "mounts": [{target, host, mode}]}] of the servers a start's refusal TEXT names as built
    with `mcp add NAME --build` (agent_sandbox.mcp_build_offers): the page offers each one's build."""
    found = []
    for match in MCP_BUILD_HINT.finditer(str(text)):
        mounts = []
        for value in match["mounts"].split(" --mount ")[1:]:
            target, _, rest = value.partition("=")
            host, _, mode = rest.partition(":")
            mounts.append({"target": target, "host": host, "mode": mode or "ro"})
        found.append({"name": match["name"], "unsigned": bool(match["unsigned"]), "mounts": mounts})
    return found[:8]


def confirmed(body, action, detail=None, code="confirm-needed", params=None):
    """Refused unless BODY carries {"confirm": true} (the key of CONFIRM_KEYS for ACTION): ACTION is a key of
    NEEDS_CONFIRM (its reason in the error, DETAIL first when given). CODE and PARAMS: the refusal's, for the page's
    own words (default: confirm-needed with the action and DETAIL)."""
    if action not in NEEDS_CONFIRM:
        raise KeyError(action)  # a route asking for an action the table does not have: a bug, never a pass
    key = CONFIRM_KEYS.get(action, "confirm")
    if body.get(key) is not True:
        why = f"{detail}: {NEEDS_CONFIRM[action]}" if detail else NEEDS_CONFIRM[action]
        raise Refused(400, f"{why}: send {{\"{key}\": true}}", confirm=action, code=code,
                      params=params if params is not None else {"action": action, "detail": detail or ""})


def one_line(value, what, limit=1024):
    if not (isinstance(value, str) and value.strip() and len(value) <= limit and value.isprintable()):
        raise Refused(400, f"{what}: one line of at most {limit} characters")
    return value.strip()


def net_items(body, key, limit=32):
    """The items of BODY[KEY] (a list of strings) for `sandbox-ai net allow|revoke`: each string may list several,
    separated by commas or spaces (as the launcher's list options take them: "pypi.org, *.example.com"), empty ones
    left out, each once; at most LIMIT in all. Each is then checked on its own and passed as --flag=ITEM."""
    items = list(dict.fromkeys(item for value in strings(body.get(key, []), key, limit)
                               for item in re.split(r"[\s,]+", value) if item))
    if len(items) > limit:
        raise Refused(400, f"{key}: at most {limit} items")
    return items


def strings(value, what, limit=64):
    if not (isinstance(value, list) and len(value) <= limit and all(isinstance(v, str) and len(v) <= 4096
                                                                       and "\0" not in v for v in value)):
        raise Refused(400, f"{what} is a list of strings")
    return value


class FanOut:
    """One question to many hosts at once (their open questions, their starts), with at most one request in flight
    per host: a host that has not answered yet (an unreachable one takes its whole timeout) is not asked again on
    top of it. A second question meanwhile (the page drawn and its poll at once, two tabs) gets that host's last
    result (if it is at most KEEP seconds old) with its age, or else waits for the request in flight, up to its
    limit: never an error only because another one asked first. ASK(name) gives a host's result or raises
    (Refused: the message for the page)."""

    KEEP = 60

    def __init__(self, ask, clock=time.monotonic, wall=time.time):
        self.ask, self.clock, self.wall = ask, clock, wall
        self.lock = threading.Lock()
        self.flight = {}  # host -> (Future, monotonic time asked)
        self.last = {}    # host -> {"result" or "error", "at": monotonic, "seen": wall time}

    def run(self, name, future):
        try:
            found = {"result": self.ask(name)}
        except Refused as exc:
            found = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one host's failure is that host's error, not the page's
            found = {"error": printable(f"{type(exc).__name__}: {exc}")[:600]}
        LOG.changed(("webui host", name), "error" in found, "webui",
                    f"{name}: " + (f"asking it failed: {found['error']}" if "error" in found else "answers"),
                    "WARNING" if "error" in found else "DEBUG")
        found.update(at=self.clock(), seen=self.wall())
        with self.lock:
            # A host forgotten meanwhile (its entry changed: forget()) keeps nothing of what the old one answered.
            if self.flight.get(name, (None,))[0] is future:
                del self.flight[name]
                self.last[name] = found
        future.set_result(found)

    def forget(self, name):
        """What is kept of NAME (its last result, the request in flight): its entry changed or went."""
        with self.lock:
            self.last.pop(name, None)
            self.flight.pop(name, None)

    def cached(self, name):
        """NAME's last result with its age ({"result" | "error", "age", "seen", "asking"}), or None: nothing asked."""
        with self.lock:
            last, flight = self.last.get(name), self.flight.get(name)
            if last is None:
                return None
            return {**last, "age": max(0.0, self.clock() - last["at"]), "asking": flight is not None}

    def usable(self, last, now):
        return last is not None and "result" in last and now - last["at"] <= self.KEEP

    def gather(self, names, seconds):
        """{host: {"result" | "error", "age": seconds since it was fetched, "seen": wall time, "asking": bool}}:
        each of NAMES asked unless a request to it still runs; waits up to SECONDS (+5) for the ones asked now, and
        for one already in flight without a recent result up to what is left of its own SECONDS (+5)."""
        waits = []  # (future, monotonic deadline)
        with self.lock:
            now = self.clock()
            for name in names:
                if name not in self.flight:
                    future = concurrent.futures.Future()
                    self.flight[name] = (future, now)
                    waits.append((future, now + seconds + 5))
                    threading.Thread(target=self.run, args=(name, future), daemon=True).start()
                elif not self.usable(self.last.get(name), now):
                    future, since = self.flight[name]
                    waits.append((future, since + seconds + 5))
        for future, deadline in waits:
            left = deadline - self.clock()
            if left > 0:
                concurrent.futures.wait([future], timeout=left)
        now, found = self.clock(), {}
        with self.lock:
            for name in names:
                flight, last = self.flight.get(name), self.last.get(name)
                if flight is None and last is not None:
                    found[name] = {**last, "age": max(0.0, now - last["at"]), "asking": False}
                elif self.usable(last, now):
                    found[name] = {**last, "age": max(0.0, now - last["at"]), "asking": True}
                else:
                    found[name] = {"error": f"no answer within {seconds} seconds", "age": 0.0, "asking": True,
                                   "seen": None}
        return found


def host_names():
    """(this machine and each host of `sandbox-ai hosts`, errors): a damaged hosts.json is named in the errors (as
    "hosts.json", which is no host's name) and this machine is asked all the same."""
    hosts = module("hosts")
    try:
        return [hosts.LOCAL, *sorted(hosts.load())], {}
    except (OSError, ValueError) as exc:
        return [hosts.LOCAL], {"hosts.json": printable(exc)[:600]}


def hosts_unreadable(exc):
    return Refused(500, f"the list of hosts (hosts.json) cannot be read: {printable(exc)[:600]}")


def hosts_file():
    """{name: entry} of hosts.json; a damaged one is Refused (500) with why, never a dropped connection."""
    try:
        return module("hosts").load()
    except (OSError, ValueError) as exc:
        raise hosts_unreadable(exc) from None


@contextlib.contextmanager
def hosts_locked():
    """hosts.json read, changed and written under its lock (hosts.hosts_file_lock: an flock on hosts.json.lock beside
    it, which `sandbox-ai hosts` and the app take as well), so that two changes at once do not lose one of them;
    one that waits too long for another writer is refused (409)."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(module("hosts").hosts_file_lock())
        except ValueError as exc:
            raise Refused(409, printable(exc)[:600]) from None
        yield


def identity_file(value):
    """A host's key as the web takes it, beyond hosts.check (an absolute path below your HOME, links resolved): a
    regular file that is there, never a link, a folder or a device (add and test make it private)."""
    if value in (None, ""):
        return value
    try:
        mode = os.lstat(os.path.expanduser(value)).st_mode
    except (OSError, ValueError, TypeError):
        raise Refused(400, f"no key file {printable(value)[:300]}") from None
    if not stat.S_ISREG(mode):
        raise Refused(400, f"{printable(value)[:300]} is not a regular file: give the key file itself")
    return value


class Api:
    """The actions of the web interface: (method, path pattern) -> handler(match, body) -> JSON. RUNNER is
    hosts.run (tests give their own)."""

    def __init__(self, runner=None, updater=None, statuses=None):
        self.runner = runner or (lambda host, args, timeout=120, input=None:
                                 module("hosts").run(host, args, input=input, timeout=timeout))
        # hosts update and hosts list's status (several commands each; tests give their own)
        self.updater = updater or (lambda host, version=None, sudo=None, from_here=False, installation=None:
                                    module("hosts").update(host, version, sudo, from_here=from_here,
                                                           installation_=installation))
        self.statuses = statuses or (lambda names, refresh=False: module("hosts").statuses(names, refresh=refresh))
        # wrong primary passwords per host: three at once, then 5 s doubling up to 5 minutes (the host's store slows
        # them down too, whatever way they come in)
        self.unlock_limiter = Limiter(free=3, first=5, maximum=300)
        self.approvals_fanout = FanOut(self.host_questions)
        # Every state (open, timed out, allowed, refused): the Approvals page's tabs, read while it is shown.
        self.approvals_all_fanout = FanOut(lambda name: self.host_questions(name, "all"))
        self.starts_fanout = FanOut(self.host_starts)
        self.every_fanout = FanOut(lambda name: self.host_starts(name, every=True))  # list --all: MCP servers too
        # hosts install: at most one per host at a time; the last result of each kept (a page whose request was cut
        # off before the install ended - a browser gives up on an answer after some minutes - asks for it)
        self.install_lock, self.installing, self.installed = threading.Lock(), {}, {}
        H = r"/api/hosts/(?P<host>[^/]+)"
        # (method, path, handler, the NEEDS_CONFIRM actions it may need)
        self.routes = [
            ("GET", r"/api/policy", self.policy, ()),
            ("GET", r"/api/approvals", self.all_approvals, ()),
            ("GET", r"/api/approvals/all", self.all_approvals_states, ()),
            ("GET", r"/api/starts", self.all_starts, ()),
            ("GET", r"/api/hosts", self.hosts, ()),
            ("POST", r"/api/hosts", self.add_host, ()),
            ("GET", r"/api/hosts-status", self.host_statuses, ()),
            ("GET", r"/api/hosts-activity", self.host_activity, ()),
            ("POST", r"/api/hosts-status", lambda params, body: self.host_statuses({"refresh": "1"}, body), ()),
            ("POST", H, self.edit_host, ("host-edit",)),
            ("DELETE", H, self.remove_host, ("host-remove",)),
            ("POST", H + r"/test", self.test_host, ()),
            ("POST", H + r"/update", self.update_host, ()),
            ("POST", H + r"/install", self.install_host, ("host-install",)),
            ("GET", H + r"/install", self.install_status, ()),
            ("POST", H + r"/uninstall-plan", self.uninstall_plan, ()),  # (read only: the host's --dry-run)
            ("POST", H + r"/uninstall", self.uninstall_host, ("host-uninstall",)),
            ("GET", H + r"/starts", self.starts, ()),
            ("POST", H + r"/starts/(?P<name>[^/]+)/(?P<action>pause|resume|stop|delete)", self.start_action,
             ("start-stop", "start-delete", "start-restart", "workspace-writable", "mcp-attach")),
            ("POST", H + r"/starts/(?P<name>[^/]+)/rename", self.rename, ()),
            # a session as one self-sufficient file (sandbox-ai board export), and such a file taken in (board import)
            ("POST", H + r"/starts/(?P<name>[^/]+)/export", self.export_bundle, ()),
            ("POST", H + r"/board-import", self.import_bundle, ("board-import",)),
            ("GET", H + r"/starts/(?P<name>[^/]+)/history", self.history, ()),
            # a start's agent memory and session files, read only, and its folder as one archive (sandbox-ai session
            # memory|files|export); a file is named as the listing names it (one path segment, encoded)
            ("GET", H + r"/starts/(?P<name>[^/]+)/memory/(?P<source>auto|home|store)", self.session_memory, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/memory/(?P<source>auto|home|store)/(?P<file>[^/]+)",
             self.session_memory, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/session-files", self.session_files, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/session-files/(?P<file>[^/]+)", self.session_files, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/session-files/(?P<file>[^/]+)/(?P<full>full)", self.session_files,
             ()),
            # a file marked as a login or key: its text only when confirmed (show-secret-file); POST, so a link
            # cannot show it
            # a search in them (sandbox-ai session files --search there): names by the file manager's patterns, text
            ("POST", H + r"/starts/(?P<name>[^/]+)/session-files-search", self.session_files_search, ()),
            ("POST", H + r"/starts/(?P<name>[^/]+)/session-files/(?P<file>[^/]+)/secret", self.session_secret_file,
             ("show-secret-file",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/memory/(?P<source>auto|home|store)/(?P<file>[^/]+)/secret",
             self.memory_secret_file, ("show-secret-file",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/session-export", self.session_export,
             ("session-export", "session-export-secrets")),
            # a session's state as a second session there (session duplicate), and an archive of session export
            # taken as a session there (session import: checked first, then confirmed)
            ("POST", H + r"/starts/(?P<name>[^/]+)/session-duplicate", self.session_duplicate,
             ("session-export-secrets", "session-grant-approvals")),
            ("POST", H + r"/session-import", self.session_import, ("session-import", "session-grant-approvals")),
            # the workspace checkpoints of a start (sandbox-ai checkpoint): listed, taken, compared, restored, deleted
            ("GET", H + r"/starts/(?P<name>[^/]+)/checkpoints", self.checkpoints, ()),
            ("POST", H + r"/starts/(?P<name>[^/]+)/checkpoints", self.take_checkpoint, ()),
            ("GET", H + r"/checkpoints/(?P<id>[^/]+)/diff", self.checkpoint_diff, ()),
            ("GET", H + r"/checkpoints/(?P<id>[^/]+)/restore", self.checkpoint_preview, ()),
            ("POST", H + r"/checkpoints/(?P<id>[^/]+)/restore", self.checkpoint_restore, ("checkpoint-restore",)),
            ("POST", H + r"/checkpoints/(?P<id>[^/]+)/delete", self.checkpoint_delete, ("checkpoint-delete",)),
            ("GET", H + r"/starts/(?P<name>[^/]+)/review", self.review_home, ()),
            ("GET", H + r"/gc", self.gc_preview, ()),
            # the storage check (sandbox-ai gc --check): its plan, of the host or of one start, and an item applied
            ("GET", H + r"/gc/check", self.storage_check, ()),
            ("GET", H + r"/gc/check/(?P<name>[^/]+)", self.storage_check, ()),
            ("POST", H + r"/gc/apply", self.storage_apply, ("storage-apply",)),
            ("GET", H + r"/gc/(?P<days>[^/]+)", self.gc_preview, ()),
            ("POST", H + r"/gc", self.gc, ("gc",)),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/attach", self.mcp_attach, ("mcp-attach", "mcp-restart")),
            # an MCP server (or a helper agent) into a running start now, and out of it (mcp attach|detach --live)
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp-live", self.mcp_live_attach, ("mcp-attach",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp-live/remove", self.mcp_live_detach, ("mcp-detach",)),
            # Skills, subagents, commands and plugins for the running start (sandbox-ai kits|plugins attach|detach), and
            # its agent started again inside the sandbox (kits restart-agent)
            ("POST", H + r"/starts/(?P<name>[^/]+)/kits-live", self.kits_live_attach, ("kits-attach",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/kits-live/remove", self.kits_live_detach, ("kits-detach",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/agent-restart", self.agent_restart, ("agent-restart",)),
            # out of a start that does not run (stopped, paused): its later starts go without it (mcp detach)
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp/remove", self.mcp_detach, ("mcp-detach",)),
            ("GET", H + r"/plugins-status", self.plugins_status, ()),
            ("GET", H + r"/approvals", self.approvals, ()),
            ("GET", H + r"/approvals-history", self.approvals_history, ()),
            ("GET", H + r"/approvals-history/(?P<run>[^/]+)", self.approvals_history, ()),
            ("GET", H + r"/remembered", self.remembered, ()),
            ("POST", H + r"/remembered/forget", self.forget, ("approval-forget",)),
            ("POST", H + r"/approvals/(?P<id>[^/]+)", self.answer, ()),
            ("POST", H + r"/approvals/(?P<id>[^/]+)/ask-again", self.ask_again, ()),
            ("GET", H + r"/doctor", self.doctor, ()),
            ("GET", H + r"/llm", self.llm, ()),
            ("GET", H + r"/llm/(?P<name>[^/]+)/models", self.llm_models, ()),
            ("GET", H + r"/llm-status", self.llm_status, ()),
            # the best tested model and harness per use case, from that host's servers (llm recommend --json)
            ("GET", H + r"/llm-recommend", self.llm_recommend, ()),
            ("GET", H + r"/llm/(?P<name>[^/]+)/status", self.llm_status, ()),
            ("POST", H + r"/llm-models", self.llm_models_of_url, ("llm-insecure",)),
            ("POST", H + r"/llm", self.add_llm, ("llm-insecure", "llm-replace")),
            ("POST", H + r"/llm/(?P<name>[^/]+)/check", self.check_llm, ()),
            # for all hosts: llm share NAME --key-to HOST,...|--no-key, or llm unshare NAME
            ("POST", H + r"/llm/(?P<name>[^/]+)/share", self.share_llm, ("llm-share-key", "llm-unshare")),
            ("DELETE", H + r"/llm/(?P<name>[^/]+)", self.remove_llm, ("llm-remove",)),
            ("GET", H + r"/defaults", self.defaults, ()),
            ("POST", H + r"/defaults", self.set_default, ("defaults-lower",)),
            ("GET", H + r"/net/(?P<name>[^/]+)", self.net_show, ()),
            # what a running start was refused, why, and what to do (sandbox-ai approvals blocked NAME there)
            ("GET", H + r"/blocked/(?P<name>[^/]+)", self.blocked, ()),
            ("POST", H + r"/net/(?P<name>[^/]+)", self.net_change, ("net-allow", "net-keep", "shared-wildcard")),
            ("POST", H + r"/net/(?P<name>[^/]+)/untaint", self.net_untaint, ("net-untaint",)),
            ("GET", H + r"/workspaces/(?P<name>[^/]+)", self.workspaces, ()),
            ("POST", H + r"/workspaces/(?P<name>[^/]+)", self.workspace_add, ("workspace-writable",)),
            ("POST", H + r"/workspaces/(?P<name>[^/]+)/remove", self.workspace_remove, ("workspace-force",)),
            # the devices of that host a start could be given (sandbox-ai devices scan there: read-only)
            ("GET", H + r"/device-scan", self.device_scan, ()),
            ("GET", H + r"/devices/(?P<name>[^/]+)", self.devices, ()),
            ("POST", H + r"/devices/(?P<name>[^/]+)", self.device_add, ("device-add",)),
            ("POST", H + r"/devices/(?P<name>[^/]+)/remove", self.device_remove, ()),
            # build toolchains: that host's store, a running start's, attach (asked first) and detach
            # sandbox-ai install --list --json on that host: what can be installed there (images, MCP servers,
            # toolchains, what presets need), its state and size; the installation itself runs in a terminal
            ("GET", H + r"/install-items", self.install_list, ()),
            ("GET", H + r"/toolchains", self.toolchains, ()),
            ("GET", H + r"/toolchains/(?P<name>[^/]+)", self.run_toolchains, ()),
            ("POST", H + r"/toolchains/(?P<name>[^/]+)", self.toolchain_attach, ("toolchain-add",)),
            ("POST", H + r"/toolchains/(?P<name>[^/]+)/remove", self.toolchain_detach, ("toolchain-force",)),
            ("GET", H + r"/limits/(?P<name>[^/]+)", self.limits, ()),
            ("POST", H + r"/limits/(?P<name>[^/]+)", self.set_limits, ("limits-raise",)),
            ("GET", H + r"/presets", self.presets, ()),
            ("GET", H + r"/models", self.cloud_models, ()),
            ("GET", H + r"/preset-categories", self.preset_categories, ()),
            ("GET", H + r"/vpn", self.vpn_list, ()),
            ("POST", H + r"/vpn", self.vpn_add, ("vpn-socks", "vpn-replace")),
            ("POST", H + r"/vpn/(?P<name>[^/]+)/check", self.vpn_check, ()),
            ("POST", H + r"/vpn/(?P<name>[^/]+)/set", self.vpn_set, ("vpn-replace",)),
            ("DELETE", H + r"/vpn/(?P<name>[^/]+)", self.vpn_remove, ("vpn-remove",)),
            ("GET", H + r"/kits", self.kits, ()),
            ("GET", H + r"/marketplaces", self.marketplaces, ()),
            ("POST", H + r"/kits", self.kit_new, ()),
            ("POST", H + r"/kits/(?P<name>[^/]+)/(?P<action>[a-z-]+)", self.kit_change, ("kit-remove", "kit-add")),
            ("DELETE", H + r"/kits/(?P<name>[^/]+)", self.kit_delete, ("kit-delete",)),
            # what the agents on that host have of their own (sandbox-ai kits host-list), one part, and an import
            ("GET", H + r"/agent-parts", self.agent_parts, ()),
            ("GET", H + r"/agent-parts/(?P<id>[^/]+)", self.agent_part, ()),
            ("POST", H + r"/agent-parts-import", self.agent_parts_import, ("kit-add", "memory-add")),
            ("GET", H + r"/canaries", self.canaries, ()),
            ("GET", H + r"/canaries/(?P<run>[^/]+)", self.canaries, ()),
            # the system monitor: what runs now (sandbox-ai top) and over a window (sandbox-ai stats usage)
            ("GET", H + r"/monitor", self.monitor, ()),
            ("GET", H + r"/monitor/usage/(?P<since>[0-9]{1,3}[mhd])", self.monitor_usage, ()),
            # what a start's model requests cost so far, its budgets (sandbox-ai spend run), and a raise of them
            ("GET", H + r"/spend/(?P<name>[^/]+)", self.spend_run, ()),
            ("POST", H + r"/spend/(?P<name>[^/]+)/raise", self.spend_raise, ("spend-raise",)),
            ("GET", H + r"/safeguards", self.safeguards, ()),
            # what runs changed in files the host runs or trusts (sandbox-ai workspace review): kept or restored per
            # file; the MCP tool definitions pinned per project (sandbox-ai mcp tools): shown, re-pinned, forgotten
            ("GET", H + r"/workspace-review", self.workspace_reviews, ()),
            ("POST", H + r"/workspace-review/keep", self.workspace_review_keep, ()),
            ("POST", H + r"/workspace-review/restore", self.workspace_review_restore, ("workspace-restore",)),
            ("GET", H + r"/mcp-tools", self.mcp_tool_pins, ()),
            ("POST", H + r"/mcp-tools/show", self.mcp_tool_pin, ()),
            ("POST", H + r"/mcp-tools/repin", self.mcp_tool_repin, ("mcp-pin-repin",)),
            ("POST", H + r"/mcp-tools/forget", self.mcp_tool_forget, ("mcp-pin-forget",)),
            ("GET", H + r"/safeguards/(?P<run>[^/]+)", self.safeguards, ()),
            ("GET", H + r"/secrets", self.secrets, ()),
            ("POST", H + r"/secrets/(?P<name>[^/]+)", self.secret_set, ("secret-replace",)),
            # another name for a secret, in everything that names it; a secret kept on another host as well
            ("POST", H + r"/secrets/(?P<name>[^/]+)/rename", self.secret_rename, ("secret-rename",)),
            ("POST", H + r"/secrets/(?P<name>[^/]+)/send", self.secret_send, ("secret-send", "secret-replace")),
            ("DELETE", H + r"/secrets/(?P<name>[^/]+)", self.secret_delete, ("secret-delete",)),
            # the primary password of the host's private files (secrets status, unlock, lock, password set)
            ("GET", H + r"/vault", self.secrets_protection, ()),
            ("POST", H + r"/vault/unlock", self.secrets_unlock, ()),
            ("POST", H + r"/vault/lock", self.secrets_lock, ()),
            ("POST", H + r"/vault/new", self.secrets_password, ()),
            # project configurations (sandbox-ai wizard trust)
            ("GET", H + r"/projects", self.projects, ()),
            ("POST", H + r"/projects/review", self.project_review, ()),
            ("POST", H + r"/projects/trust", self.project_trust, ("project-trust",)),
            ("POST", H + r"/projects/forget", self.project_forget, ("project-forget",)),
            # MCP servers (sandbox-ai mcp)
            ("GET", H + r"/mcp", self.mcp_list, ()),
            ("POST", H + r"/mcp", self.mcp_add, ("mcp-unsigned", "mcp-writable-mount", "mcp-replace", "mcp-lan",
                                                 "mcp-build")),
            ("POST", H + r"/mcp-catalog", self.mcp_catalog, ()),
            # MCP servers of another host copied to this one (sandbox-ai mcp copy, run here: it reaches both)
            ("POST", H + r"/mcp-copy", self.mcp_copy, ("mcp-copy",)),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/start", self.mcp_start, ("mcp-attach", "mcp-restart")),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/stop", self.mcp_stop, ("mcp-stop",)),
            # the newer image a server's catalog pins (sandbox-ai mcp update [NAME...]): one server, or all of yours
            ("POST", H + r"/mcp-update", self.mcp_update, ("mcp-update",)),
            # each server started as a start would, asked for its tools, stopped again (sandbox-ai mcp check): changes
            # nothing, so nothing is asked first
            ("POST", H + r"/mcp-check", self.mcp_check, ()),
            # logins and identities (sandbox-ai git-logins, publish-logins, identities): secrets only by name
            # phone notifications (sandbox-ai notify): channels with secret names, never a token
            ("GET", H + r"/notify", self.notify_list, ()),
            ("POST", H + r"/notify", self.notify_add, ("notify-add",)),
            ("POST", H + r"/notify/(?P<name>[^/]+)/remove", self.notify_remove, ("notify-remove",)),
            ("POST", H + r"/notify/(?P<name>[^/]+)/test", self.notify_test, ()),
            ("POST", H + r"/notify-link", self.notify_link, ()),
            # scheduled runs (sandbox-ai schedule): the list, a schedule's history and reports, add, run now, pause,
            # resume, remove and the timer
            ("GET", H + r"/schedules", self.schedules, ()),
            ("POST", H + r"/schedules", self.schedule_add, ("schedule-add",)),
            ("GET", H + r"/schedules/(?P<name>[^/]+)", self.schedule_show, ()),
            ("GET", H + r"/schedules/(?P<name>[^/]+)/history", self.schedule_history, ()),
            ("GET", H + r"/schedules/(?P<name>[^/]+)/report", self.schedule_report, ()),
            ("GET", H + r"/schedules/(?P<name>[^/]+)/report/(?P<run>[^/]+)", self.schedule_report, ()),
            ("POST", H + r"/schedules/(?P<name>[^/]+)/run", self.schedule_run, ()),
            ("POST", H + r"/schedules/(?P<name>[^/]+)/pause", self.schedule_pause, ()),
            ("POST", H + r"/schedules/(?P<name>[^/]+)/resume", self.schedule_resume, ()),
            ("POST", H + r"/schedules/(?P<name>[^/]+)/remove", self.schedule_remove, ("schedule-remove",)),
            ("POST", H + r"/schedules-timer", self.schedule_timer, ("schedule-timer",)),
            ("GET", H + r"/git-logins", self.git_logins, ()),
            ("POST", H + r"/git-logins", self.git_login_add, ("git-login-add", "login-replace")),
            ("POST", H + r"/git-logins/remove", self.git_login_remove, ("login-remove",)),
            ("POST", H + r"/git-logins/edit", self.git_login_edit, ("login-edit",)),
            ("POST", H + r"/publish-logins/(?P<kind>[^/]+)/edit", self.publish_login_edit, ("login-edit",)),
            ("POST", H + r"/git-logins/check", self.git_login_check, ()),
            ("GET", H + r"/publish-logins", self.publish_logins, ()),
            ("GET", H + r"/publish-logins/kinds", self.publish_kinds, ()),
            ("GET", H + r"/login-status", self.login_status, ()),
            # the logins of the sessions (login --status --sessions), and one imported as the central login (login
            # AGENT --from-session NAME --yes --expect FINGERPRINT: only the login that was shown; confirmed)
            ("GET", H + r"/session-logins", self.session_logins, ()),
            ("POST", H + r"/session-logins/import", self.import_session_login, ("login-import",)),
            # an agent login for a sandbox that runs (sandbox-ai login give): the central one, or a session's
            ("POST", H + r"/session-logins/give", self.give_session_login, ("login-give",)),
            ("POST", H + r"/publish-logins", self.publish_login_add, ("login-replace",)),
            ("POST", H + r"/publish-logins/check", self.publish_login_check, ()),
            ("POST", H + r"/publish-logins/(?P<kind>[^/]+)/remove", self.publish_login_remove, ("login-remove",)),
            ("GET", H + r"/identities", self.identities, ()),
            ("POST", H + r"/identities", self.identity_add, ()),
            ("POST", H + r"/identities-check", self.identity_check, ()),
            ("GET", H + r"/identities/(?P<name>[^/]+)", self.identity_show, ()),
            ("POST", H + r"/identities/(?P<name>[^/]+)", self.identity_set, ()),
            ("POST", H + r"/identities/(?P<name>[^/]+)/(?P<action>login|publish)", self.identity_login,
             ("login-replace",)),
            ("POST", H + r"/identities/(?P<name>[^/]+)/(?P<action>logout|unpublish)", self.identity_logout,
             ("login-remove",)),
            ("DELETE", H + r"/identities/(?P<name>[^/]+)", self.identity_remove, ("login-remove",)),
            # the network kept for later starts, the starts with MCP servers that do not run
            ("POST", H + r"/net/(?P<name>[^/]+)/keep", self.net_keep, ("net-keep",)),
            ("GET", r"/api/starts/all", lambda params, body: self.all_starts(params, body, every=True), ()),
            ("GET", H + r"/starts/all", self.starts_every, ()),
            # what a new start lacks, and what would fix each (the page asks before it opens the start's terminal)
            ("POST", H + r"/fix-plan", self.fix_plan, ()),
            # presets and their directories
            ("GET", H + r"/presets/(?P<name>[^/]+)", self.preset_show, ()),
            ("POST", H + r"/presets/(?P<name>[^/]+)/check", self.preset_check, ()),
            ("GET", H + r"/preset-dirs", self.preset_dirs, ()),
            ("POST", H + r"/preset-dirs", self.preset_dir_add, ("defaults-lower",)),
            ("POST", H + r"/preset-dirs/remove", self.preset_dir_remove, ()),
            # one run's canaries
            ("GET", H + r"/canaries/(?P<run>[^/]+)/list", self.canary_list, ()),
            ("POST", H + r"/canaries/(?P<run>[^/]+)/tokens", self.canary_tokens, ("canary-tokens",)),
            ("POST", H + r"/canaries/(?P<run>[^/]+)/resume", self.canary_resume, ("canary-resume",)),
            # a kit, an LLM server, a VPN profile in full
            ("GET", H + r"/kits/(?P<name>[^/]+)", self.kit_show, ()),
            ("GET", H + r"/llm/(?P<name>[^/]+)", self.llm_show, ()),
            ("GET", H + r"/vpn/(?P<name>[^/]+)", self.vpn_show, ()),
            # session bridges (sandbox-ai bridge), run by HOST's sandbox-ai: with --hosts and HOST:NAME it reaches the
            # sessions and bridges of its own hosts over SSH (the page asks the service's machine, "local")
            ("GET", H + r"/bridges", self.bridges, ()),
            ("GET", H + r"/bridges/(?P<id>[^/]+)", self.bridge_show, ()),
            ("POST", H + r"/bridges", self.bridge_add, ("bridge-add",)),
            ("POST", H + r"/bridges/(?P<id>[^/]+)/(?P<action>revoke|resume)", self.bridge_action,
             ("bridge-revoke", "bridge-resume")),
            # logs of a directory or project, the privacy check, the activity
            ("POST", H + r"/logs", self.logs_of, ()),
            ("POST", H + r"/privacy-check", self.privacy_check, ()),
            ("POST", H + r"/activity", self.activity, ()),
            # the doctor of one backend, the version in full, images, the demo device
            ("GET", H + r"/doctor/(?P<backend>[^/]+)", self.doctor, ()),
            ("GET", H + r"/version", self.version_of, ()),
            # the update mode (sandbox-ai update mode): shown, and changed asked first
            ("GET", H + r"/update-mode", self.update_mode, ()),
            ("POST", H + r"/update-mode", self.update_mode_set, ("update-mode",)),
            ("GET", H + r"/images", self.images, ()),
            ("GET", H + r"/demo-device", self.demo_status, ()),
            ("POST", H + r"/demo-device/start", self.demo_start, ()),
            ("POST", H + r"/demo-device/stop", self.demo_stop, ()),
            # teams of agents (sandbox-ai team; a team starts in a terminal: kind=team-start)
            ("GET", H + r"/teams", self.teams, ()),
            ("GET", H + r"/team-choices", self.team_choices, ()),
            ("GET", H + r"/teams/(?P<name>[^/]+)", self.team_show, ()),
            ("POST", H + r"/teams/(?P<name>[^/]+)/post", self.team_post, ()),
            ("POST", H + r"/teams/(?P<name>[^/]+)/stop", self.team_stop, ("team-stop",)),
            ("DELETE", H + r"/teams/(?P<name>[^/]+)", self.team_delete, ("team-delete",)),
            # the images' extensions and variants (sandbox-ai images; a build runs in a terminal: kind=build)
            ("GET", H + r"/images-history", self.image_history, ()),
            ("POST", H + r"/images-new", self.image_new, ()),
            ("POST", H + r"/images-recipe", self.image_recipe, ()),
            ("GET", H + r"/images/(?P<target>[^/]+)", self.image_show, ()),
            ("GET", H + r"/images/(?P<target>[^/]+)/packages", self.image_packages, ()),
            ("GET", H + r"/images/(?P<target>[^/]+)/history", self.image_history, ()),
            ("POST", H + r"/images/(?P<target>[^/]+)/extend", self.image_extend, ("image-file",)),
            ("POST", H + r"/images/(?P<target>[^/]+)/check", self.image_check, ()),
            ("POST", H + r"/images/(?P<target>[^/]+)/remove", self.image_remove, ("image-remove",)),
        ]

    def dispatch(self, method, path, body):
        for wanted, pattern, handler, _ in self.routes:
            match = re.fullmatch(pattern, path)
            if match and wanted == method:
                params = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
                return handler(params, body if isinstance(body, dict) else {})
        if any(re.fullmatch(pattern, path) for _, pattern, _, _ in self.routes):
            raise Refused(405, "method not allowed")
        raise Refused(404, "no such action")

    def policy(self, params, body):
        """What needs {"confirm": true} (NEEDS_CONFIRM) and the order of protection the defaults are compared in:
        the page builds its confirmations from this, the service checks the same."""
        return {"confirm": dict(NEEDS_CONFIRM), "order": {"mode": list(MODE_ORDER),
                                                           "telemetry": list(TELEMETRY_ORDER)},
                "defaults": {"mode": DEFAULT_MODE, "telemetry": DEFAULT_TELEMETRY}}

    def run(self, host, args, timeout=120, input=None):
        older = newer_needed(host_name(host), args)  # an option that host's sandbox-ai does not know: said, not run
        if older:
            raise older
        try:
            status, out, err = self.runner(host_name(host), list(args), timeout=timeout, input=input)
        except ValueError as exc:  # hosts.run: no such host, or hosts.json cannot be read
            hosts_file()
            missing = "no host" in str(exc)
            raise Refused(404 if missing else 400, printable(exc)[:600], code="no-host" if missing else None,
                          params={"host": host} if missing else None) from None
        older = too_old_said(host, args, status, err)
        if older:
            raise older
        return status, out, err

    @staticmethod
    def failed(host, args, status, out, err):
        """The refusal of a sandbox-ai command that failed on HOST: its words, and the code of why (SSH itself
        failed: unreachable; no answer in time; else the command's own failure)."""
        hosts = module("hosts")
        code = "host-unreachable" if status == 255 and host != hosts.LOCAL else "timeout" if status == 124 \
            else "command-failed"
        return Refused(502, f"sandbox-ai {args[0]} on {host}: exit status {status}: "
                            f"{printable((err or out).strip()[-600:])}", code=code,
                       params={"host": host, "command": f"sandbox-ai {args[0]}", "status": status})

    def json_of(self, host, args, timeout=120, ok=None, input=None):
        """The JSON the command printed. OK: the exit statuses that count as an answer (None: any status whose
        output is JSON, as most commands print their JSON whatever the outcome); another one is a failure, with what
        it said on stderr, even when it printed JSON (an empty list that would read as "nothing found")."""
        status, out, err = self.run(host, args, timeout, input)
        if ok is not None and status not in ok:
            raise self.failed(host, args, status, out, err)
        try:
            return json.loads(out)
        except (ValueError, RecursionError):
            raise self.failed(host, args, status, out, err) from None

    def done(self, host, args, timeout=120, input=None):
        """{"output"} of a sandbox-ai command that succeeded on HOST, and its "warnings" (launcher_warnings) when it
        said any: a host path you chose with a risk is given, and the page shows what it means."""
        status, out, err = self.run(host, args, timeout, input)
        if status != 0:
            raise self.failed(host, args, status, out, err)
        result = {"output": printable(out.strip()[-4000:])}
        said = launcher_warnings(f"{err}\n{out}")
        if said:
            result["warnings"] = said
        return result

    # hosts (the list lives on the machine of the service)
    def hosts(self, params, body):
        try:
            return module("hosts").entries()
        except (OSError, ValueError) as exc:
            raise hosts_unreadable(exc) from None

    def host_statuses(self, params, body):
        """{name: {"version", "kind", "available", "update", "error", ...} or null}: each host's sandbox-ai and
        whether an update is there, as hosts list finds it (kept 10 minutes; ?refresh=1 asks again)."""
        names = sorted(hosts_file())
        try:
            found = self.statuses(names, refresh=params.get("refresh") == "1") if names else {}
        except (OSError, ValueError) as exc:
            raise hosts_unreadable(exc) from None
        return {name: ({key: value for key, value in info.items() if key not in ("ssh", "port")} if info else None)
                for name, info in found.items()}

    def add_host(self, params, body):
        hosts = module("hosts")
        name = body.get("name")
        entry = {key: body[key] for key in ("ssh", "port", "identity", "description", "command", "update_sudo")
                 if body.get(key) not in (None, "")}
        with hosts_locked():
            current = hosts_file()
            try:
                checked = hosts.check(name, entry)
                if name in current:
                    raise ValueError(f"host {name} exists")
            except ValueError as exc:
                raise Refused(400, printable(exc)) from None
            identity_file(checked.get("identity"))
            hosts.save({**current, name: checked})
        self.forget_host(name)
        # A key file others can read (ssh refuses it): made private now, and then whether the host answers.
        fixed, warnings = hosts.protect_keys(checked)
        result = {"name": name, **checked, "fixed": fixed, "warnings": warnings}
        if fixed:
            status, out, err = self.run(name, ["--version"], 30)
            result["connection"] = {"ok": status == 0, "version": out.strip() if status == 0 else None,
                                    "error": None if status == 0 else hosts.failure(status, out, err)}
        return result

    def remove_host(self, params, body):
        hosts = module("hosts")
        notes = []
        current = hosts_file()
        if params["host"] in current:
            confirmed(body, "host-remove", f"{params['host']} ({current[params['host']]['ssh']})")
            with contextlib.suppress(Exception):  # a shared LLM server's copy (and key) leaves that host first
                shares = hosts.llm_shares() if hasattr(hosts, "llm_shares") else None
                if shares is not None:
                    shares.host_leaving(params["host"], notes.append)
        with hosts_locked():
            current = hosts_file()
            if params["host"] not in current:
                raise Refused(404, "no such host", code="no-host", params={"host": params["host"]})
            confirmed(body, "host-remove", f"{params['host']} ({current[params['host']]['ssh']})")
            removed = current.pop(params["host"])
            hosts.save(current)
        hosts.forget_status(params["host"])
        self.forget_host(params["host"])
        with contextlib.suppress(Exception):  # its shared SSH connection would serve commands for a minute more
            getattr(hosts, "stop_master", lambda entry: None)(removed)
        return {"removed": params["host"], **({"notes": [printable(n)[:600] for n in notes]} if notes else {})}

    def forget_host(self, name):
        """What the fan-outs keep of NAME (its questions, its starts): it was added, changed or removed."""
        self.approvals_fanout.forget(name)
        self.approvals_all_fanout.forget(name)
        self.starts_fanout.forget(name)

    def edit_host(self, params, body):
        """A host's settings changed (hosts.json on the machine of the service, as `sandbox-ai hosts add` writes
        it): what BODY gives replaces that setting ("" or null: left out, for identity, command and the
        description), checked as hosts.check does; the name stays. A change of where or how the commands run (ssh,
        port, identity, command, update_sudo) needs {"confirm": true}; the key is a regular file below the HOME of
        the service's user. Read, changed and written under the lock of hosts.json."""
        hosts = module("hosts")
        name = params["host"]
        fields = ("ssh", "port", "identity", "description", "command", "update_sudo")
        if set(body) - {*fields, "confirm"}:
            raise Refused(400, "a host has ssh, port, identity, description, command and update_sudo")
        with hosts_locked():
            current = hosts_file()
            if name not in current:
                raise Refused(404, "no such host", code="no-host", params={"host": name})
            before = current[name]
            changed = dict(before)
            for key in fields:
                if key not in body:
                    continue
                if body[key] in (None, "") and key in ("identity", "command", "description"):
                    changed.pop(key, None)
                else:
                    changed[key] = body[key]
            try:
                checked = hosts.check(name, changed)
            except ValueError as exc:
                raise Refused(400, printable(exc)) from None
            if checked.get("identity") != before.get("identity"):
                identity_file(checked.get("identity"))
            moved = [key for key in ("ssh", "port", "identity", "command", "update_sudo")
                     if before.get(key) != checked.get(key)]
            if moved:
                confirmed(body, "host-edit", f"{name}: " + ", ".join(
                    f"{key} {json.dumps(before.get(key))} -> {json.dumps(checked.get(key))}" for key in moved))
            current[name] = checked
            hosts.save(current)
        if moved:
            hosts.forget_status(name)
            self.forget_host(name)
            with contextlib.suppress(Exception):  # its shared connection was made for what the entry said before
                getattr(hosts, "stop_master", lambda entry: None)(before)
        fixed, warnings = hosts.protect_keys(checked)
        return {"name": name, **checked, "fixed": fixed, "warnings": warnings}

    def test_host(self, params, body):
        """As `sandbox-ai hosts test`: whether the host answers, its sandbox-ai version and its doctor; a key file
        others can read is made private first ("fixed"), and once more when ssh still refuses one as too open."""
        hosts = module("hosts")
        name = host_name(params["host"])
        local = name == hosts.LOCAL
        result = {"name": name, "ssh": None if local else False, "version": None, "doctor": None, "error": None,
                  "fixed": [], "warnings": []}
        entry = None if local else hosts_file().get(name)
        if entry:
            result["fixed"], result["warnings"] = hosts.protect_keys(entry)
        status, out, err = self.run(name, ["--version"], 30)
        if entry and status != 0 and hosts.TOO_OPEN.search(err or ""):
            more, warnings = hosts.protect_keys(entry, err)
            result["fixed"] += more
            result["warnings"] += [warning for warning in warnings if warning not in result["warnings"]]
            if more:
                status, out, err = self.run(name, ["--version"], 30)
        if status != 0:
            result["error"] = printable((err or out).strip()[-500:]) or f"exit status {status}"
            if status == 255 and not local:
                result["error"] += " (SSH: is the key in ~/.ssh/authorized_keys there, and the host reachable?)"
            return result
        result["ssh"], result["version"] = (None if local else True), out.strip()
        try:
            result["doctor"] = self.doctor_of(name, 90)
        except Refused as exc:
            result["error"] = str(exc)
        return result

    def update_host(self, params, body):
        """As `sandbox-ai hosts update NAME`: the host's own signed update, old -> new version. {"sudo": true}
        runs it as sudo -n (a system installation); when sudo wants a password, "needs_password" says so and the
        page opens a terminal (kind=update), where sudo asks."""
        name = host_name(params["host"])
        sudo, version, from_here = body.get("sudo"), body.get("version"), body.get("from_here", False)
        which = body.get("installation") or None  # (a host with the user's own and a system one: which)
        if which not in (None, "user", "system"):
            raise Refused(400, "installation is user or system (or null: the one the host's commands run)")
        chosen = {"installation": which} if which else {}
        if sudo not in (None, True, False):
            raise Refused(400, "sudo is true, false or null (the host's setting)")
        if from_here not in (True, False):
            raise Refused(400, "from_here is true or false")
        hosts = module("hosts")
        if version is not None and not (isinstance(version, str) and hosts.VERSION.fullmatch(version)):
            raise Refused(400, "version is X.Y.Z")
        if from_here:
            # this machine's copy onto the host, as hosts install --from-here does: asked first like an install
            if name == hosts.LOCAL:
                raise Refused(400, "this machine updates with: sandbox-ai update")
            if version is not None:
                raise Refused(400, "from_here installs this machine's version; version is for the signed release")
            entry = hosts_file().get(name)
            if entry is None:
                raise Refused(404, "no such host", code="no-host", params={"host": name})
            target = f"{entry.get('ssh')}{':' + str(entry['port']) if entry.get('port') not in (None, 22) else ''}"
            confirmed(body, "host-install", f"{name} ({target}): this machine's sandbox-ai, as the installation it "
                                            "has there (the user's own or the system's)")
            with self.install_lock:
                if name in self.installing:
                    raise Refused(409, f"sandbox-ai is being installed on {name}: wait for its end",
                                  code="busy", params={"host": name})
                self.installing[name] = time.monotonic()
            try:
                return self.updater(name, None, sudo, from_here=True, **chosen)
            except ValueError as exc:
                raise Refused(400, printable(exc)) from None
            finally:
                with self.install_lock:
                    self.installing.pop(name, None)
        try:
            return self.updater(name, version, sudo, **chosen)
        except ValueError as exc:
            if name != hosts.LOCAL:
                hosts_file()  # a damaged hosts.json: that, as a 500 with why
            raise Refused(404 if "no host" in str(exc) else 400, printable(exc)) from None

    def uninstall_choice(self, params, values):
        """(host, the hosts.uninstall options) of a request: installation user|system (or none: the only one), purge,
        secrets, mcp_data (booleans; the last two with purge), stop_running, remove."""
        hosts = module("hosts")
        name = host_name(params["host"])
        if name == hosts.LOCAL:
            raise Refused(400, "this machine's sandbox-ai is the one this service runs from: remove it in a terminal "
                               "there with sandbox-ai uninstall")
        if name not in hosts_file():
            raise Refused(404, "no such host", code="no-host", params={"host": name})
        which = values.get("installation") or None
        if which not in (None, "user", "system"):
            raise Refused(400, "installation is user or system")
        flags = {}
        for key in ("purge", "secrets", "mcp_data", "stop_running", "remove"):
            value = values.get(key, False)
            if value in ("1", "true"):
                value = True
            elif value in ("0", "false", ""):
                value = False
            if value not in (True, False):
                raise Refused(400, f"{key} is true or false")
            flags[key] = value
        if (flags["secrets"] or flags["mcp_data"]) and not flags["purge"]:
            raise Refused(400, "secrets and mcp_data go with purge")
        return name, which, flags

    def uninstall_result(self, name, args):
        """The JSON `sandbox-ai hosts uninstall ...` (ARGS) printed here (it reaches the host over SSH)."""
        status, out, err = self.run(module("hosts").LOCAL, args, install_route_timeout())
        try:
            result = json.loads(out)
        except (ValueError, RecursionError):
            result = None
        if not isinstance(result, dict):  # refused before it began (two installations and none chosen, no host)
            raise Refused(400 if status in (1, 2) else 502, printable((err or out).strip()[-600:])
                          or f"sandbox-ai hosts uninstall {name}: exit status {status}")
        return result

    def uninstall_plan(self, params, body):
        """What `sandbox-ai uninstall` would remove on the host (its own table: --dry-run there), for the question
        before the uninstall: {"plan", "installation", "sudo", "error"} ({installation, purge, secrets, mcp_data}:
        as the uninstall's); nothing is removed."""
        name, which, flags = self.uninstall_choice(params, body)
        return self.uninstall_result(name, uninstall_argv(name, which, flags, dry_run=True))

    def uninstall_host(self, params, body):
        """As `sandbox-ai hosts uninstall NAME --yes --json`, run by the local sandbox-ai: the installation chosen
        removed there by its own sandbox-ai uninstall (sudo -n for a system one; when sudo wants a password,
        "needs_password" says so and the page opens a terminal, kind=uninstall). Asked first (host-uninstall),
        naming the host and what goes."""
        name, which, flags = self.uninstall_choice(params, body)
        confirmed(body, "host-uninstall", f"{name}: the {which or 'only'} installation"
                  + (", with the SSH user's data" if flags["purge"] else "")
                  + (", its secrets and logins" if flags["secrets"] else ""))
        with self.install_lock:
            if name in self.installing:
                raise Refused(409, f"sandbox-ai is being installed on {name}: wait for its end", code="busy",
                              params={"host": name})
            self.installing[name] = time.monotonic()
        try:
            return self.uninstall_result(name, uninstall_argv(name, which, flags))
        finally:
            with self.install_lock:
                self.installing.pop(name, None)
            self.forget_host(name)

    def install_host(self, params, body):
        """As `sandbox-ai hosts install NAME --json`, run by the local sandbox-ai (it reaches the host over SSH):
        {"name", "before", "after", "changed", "mode", "prefix", "sudo", "root", "source", "error",
        "needs_password", "output", "notes"}. Asked first (host-install), naming the host and how; when sudo there
        wants a password, "needs_password" says so and the page offers a terminal (kind=install), where it asks."""
        hosts = module("hosts")
        name = host_name(params["host"])
        if name == hosts.LOCAL:
            raise Refused(400, "this machine installs with install.sh itself (and updates with: sandbox-ai update)")
        expected = body.get("expected")
        choice = install_choice({key: value for key, value in body.items() if key != "expected"})
        entry = hosts_file().get(name)
        if entry is None:
            raise Refused(404, "no such host", code="no-host", params={"host": name})
        # where it goes, as the question named it: an entry changed meanwhile (another page, the command line) is not
        # what was confirmed
        target = f"{entry.get('ssh')}{':' + str(entry['port']) if entry.get('port') not in (None, 22) else ''}"
        if expected is not None:
            if not (isinstance(expected, dict) and set(expected) <= {"ssh", "port"}):
                raise Refused(400, "expected is {ssh, port}: the host's SSH target as the question named it")
            if expected.get("ssh") != entry.get("ssh") or (expected.get("port") or 22) != (entry.get("port") or 22):
                raise Refused(409, f"{name} is reached at {target} now, not as the question said: nothing was "
                                   "installed; ask again", code="changed-meanwhile", params={"host": name})
        confirmed(body, "host-install", f"{name} ({target}): {install_words(choice, entry)}")
        with self.install_lock:
            if name in self.installing:
                since = round(time.monotonic() - self.installing[name])
                raise Refused(409, f"sandbox-ai is being installed on {name} (for {since} s): wait for its end",
                              code="busy", params={"host": name})
            self.installing[name] = time.monotonic()
        result, error = None, None
        try:
            status, out, err = self.run(hosts.LOCAL, install_argv(name, choice), install_route_timeout())
            try:
                result = json.loads(out)
            except (ValueError, RecursionError):
                result = None
            if not isinstance(result, dict):
                result = None
                raise Refused(502, f"sandbox-ai hosts install {name}: exit status {status}: "
                                   f"{printable((err or out).strip()[-600:])}")
        except Refused as exc:
            error = str(exc)
            raise
        finally:
            with self.install_lock:
                self.installing.pop(name, None)
                self.installed[name] = {"result": result, "error": error, "ended": time.time(), "choice": choice}
            self.forget_host(name)
        return result

    def install_status(self, params, body):
        """Whether sandbox-ai is being installed on the host now ({"running": true, "seconds": how long}) and how
        its last install here ended ({"last": {"result", "error", "ago", "choice"}} or null): for a page whose own
        request was cut off, or that was opened meanwhile."""
        name = host_name(params["host"])
        with self.install_lock:
            started, last = self.installing.get(name), self.installed.get(name)
        return {"running": started is not None,
                "seconds": round(time.monotonic() - started) if started is not None else None,
                "last": None if last is None else {"result": last["result"], "error": last["error"],
                                                   "ago": round(max(0.0, time.time() - last["ended"])),
                                                   "choice": last["choice"]}}

    # starts and sessions
    def starts(self, params, body):
        return self.json_of(params["host"], ["list", "--json"])

    def host_starts(self, name, every=False):
        listed = self.json_of(name, ["list", *(["--all"] if every else []), "--json"], STARTS_HOST_SECONDS)
        if not isinstance(listed, list):
            raise Refused(502, f"sandbox-ai list on {name}: not a list of starts")
        return [entry for entry in listed if isinstance(entry, dict)]

    def starts_every(self, params, body):
        """sandbox-ai list --all --json: the starts and the MCP servers that do not run."""
        return self.json_of(params["host"], ["list", "--all", "--json"])

    def all_starts(self, params, body, every=False):
        """The starts of every host, asked at once (at most one request in flight per host; a host still asking
        is given with its last list and how old that is): {"starts": [{"host", ...}], "errors": {host: why},
        "seen": {host: when its list was read, Unix time}, "age": {host: seconds since then, by the service's
        clock: the page counts on from when it got the answer, never comparing its clock with the service's},
        "asking": [hosts still being asked]}."""
        names, errors = host_names()
        found = (self.every_fanout if every else self.starts_fanout).gather(names, STARTS_HOST_SECONDS)
        starts, seen, age, asking = [], {}, {}, []
        for name in names:
            info = found[name]
            if info.get("asking"):
                asking.append(name)
            if "error" in info:
                errors[name] = info["error"]
            else:
                starts += [{**entry, "host": name} for entry in info["result"]]
            if info.get("seen") is not None:
                seen[name] = round(info["seen"], 3)
                age[name] = round(info["age"], 3)
        return {"starts": starts, "errors": errors, "seen": seen, "age": age, "asking": asking}

    def host_activity(self, params, body):
        """{host: {"starts", "running", "questions", "age"}} from what the fan-outs keep (GET /api/starts and the
        page's poll of GET /api/approvals): nothing is asked for it. null where nothing is known yet (or the last try
        failed); age: seconds since the older of the two was read."""
        names, _ = host_names()
        found = {}
        for name in names:
            starts, questions = self.starts_fanout.cached(name), self.approvals_fanout.cached(name)
            listed = starts.get("result") if starts else None
            asked = questions.get("result") if questions else None
            ages = [info["age"] for info in (starts, questions) if info and ("result" in info)]
            found[name] = {"starts": len(listed) if isinstance(listed, list) else None,
                           "running": sum(1 for entry in listed if entry.get("state") == "running")
                           if isinstance(listed, list) else None,
                           "questions": len(asked) if isinstance(asked, list) else None,
                           "age": round(max(ages), 3) if ages else None}
        return found

    def start_action(self, params, body):
        """pause, resume, stop or delete a start; stop (it ends what runs, and a HOME that is not kept with it) and
        delete (its kept HOME goes) need {"confirm": true}: the page asks first, naming the host and the start."""
        name, action = start_name(params["name"]), params["action"]
        if action == "delete":
            confirmed(body, "start-delete")
            return self.done(params["host"], ["delete", name, "--yes"])
        if action == "stop":
            confirmed(body, "start-stop")
            if body.get("wait") is not None:  # stop --wait SECONDS: how long to wait for it to end
                wait = whole(body["wait"], "wait", 1, 600)
                return self.done(params["host"], ["stop", name, f"--wait={wait}"], timeout=wait + 60)
        if action == "resume":
            options = self.resume_options(body, name)
            if options:
                try:
                    return self.done(params["host"], ["resume", *options, name], timeout=300)
                except Refused as exc:
                    # A server its catalog has no signed image of (the launcher names `mcp add NAME --build`): the
                    # page offers to build it there (asked), then resumes again.
                    servers = mcp_build_hints(str(exc))
                    if servers:
                        raise Refused(exc.status, str(exc), code="mcp-unpublished-start",
                                      params={"host": params["host"], "names": ", ".join(x["name"] for x in servers),
                                              **{f"server{i}": json.dumps(x) for i, x in enumerate(servers)}}) \
                            from None
                    raise
        return self.done(params["host"], [action, name], timeout=180)

    @staticmethod
    def resume_options(body, name):
        """resume --restart, --workspace DIR, --mount SOURCE:DESTINATION[:ro|rw], --mcp NAME (each kept for its later
        starts): a restart ends what runs now (start-restart), a directory or a writable mount lets the agent change
        it (workspace-writable), an MCP server is reached by it (mcp-attach); all asked first."""
        options, widens = [], []
        for value in strings(body.get("workspaces", []), "workspaces", 16):
            options.append(f"--workspace={path_or_remote(value, 'a workspace')}")
            widens.append(value)
        mounts = body.get("mounts", [])
        if not isinstance(mounts, list) or len(mounts) > 16:
            raise Refused(400, "mounts is a list of {source, destination, mode}")
        for mount in mounts:
            if not isinstance(mount, dict) or set(mount) - {"source", "destination", "mode"}:
                raise Refused(400, "a mount is {source, destination, mode: ro or rw}")
            source = path_or_remote(mount.get("source"), "a mount's source")
            destination = clean_path(mount.get("destination"), "a mount's destination")
            mode = mount.get("mode") or "ro"
            if mode not in ("ro", "rw") or ":" in (source if not remote_source(source, "") else "") + destination:
                raise Refused(400, "a mount is SOURCE:DESTINATION with ro or rw (no ':' in the paths)")
            options.append(f"--mount={source}:{destination}:{mode}")
            if mode == "rw":
                widens.append(f"{source} (writable)")
        servers = strings(body.get("mcp", []), "mcp", 16)
        for server in servers:
            if not MCP_NAME.fullmatch(server):
                raise Refused(400, "an MCP server's name is 1-32 letters, digits, '_' or '-'")
            options.append(f"--mcp={server}")
        if flag(body, "restart"):
            confirmed(body, "start-restart", name)
            options.append("--restart")
        if widens:
            confirmed(body, "workspace-writable", ", ".join(widens))
        if servers:
            confirmed(body, "mcp-attach", f"{', '.join(servers)} to {name}")
        return options

    def rename(self, params, body):
        """sandbox-ai rename OLD NEW: another name for a start (its run id stays)."""
        old, new = start_name(params["name"]), new_start_name(body.get("new"))
        return self.done(params["host"], ["rename", old, new], 60)

    def export_bundle(self, params, body):
        """sandbox-ai board export --session NAME -: the session's bundle (one JSON document; the images go along only
        from the command line or the app: --images makes an archive), for the page to offer as a download. Never a
        secret's value in it (the command's own rule); this machine's paths and names are."""
        name = start_name(params["name"])
        status, out, err = self.run(params["host"], ["board", "export", f"--session={name}", "-"], 300)
        if status != 0:
            raise self.failed(params["host"], ["board", "export"], status, out, err)
        try:
            bundle = json.loads(out)
        except (ValueError, RecursionError):
            raise Refused(502, "sandbox-ai board export: not a bundle") from None
        if not isinstance(bundle, dict) or bundle.get("schema") != "sandbox-ai-bundle":
            raise Refused(502, "sandbox-ai board export: not a bundle")
        file = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "session"
        return {"file": f"{file}.sandbox-ai-bundle.json", "bundle": bundle}

    def import_bundle(self, params, body):
        """sandbox-ai board import - --json (--check | --yes): BODY {"bundle": the bundle's document, "map": {marker or
        path: folder there}, "values": {name: value}, "as": board name, "replace": ["KIND:NAME"], "check": true} -
        the plan (check, nothing written), or, confirmed (board-import), its parts written and the board kept (never
        started)."""
        bundle = body.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("schema") != "sandbox-ai-bundle":
            raise Refused(400, "bundle is the document of a sandbox-ai board export")
        check = body.get("check", True)
        if not isinstance(check, bool):
            raise Refused(400, "check is true or false")
        args = ["board", "import", "-", "--json", "--check" if check else "--yes"]
        maps, values = body.get("map") or {}, body.get("values") or {}
        if not isinstance(maps, dict) or not isinstance(values, dict) or len(maps) > 200 or len(values) > 200:
            raise Refused(400, "map and values are objects")
        for old, new in maps.items():
            old, new = one_line(old, "a folder of the bundle", 4096), one_line(new, "a folder there", 4096)
            if not new.startswith(("/", "~/")):
                raise Refused(400, f"{printable(new)[:200]}: give the folder as an absolute path")
            args.append(f"--map={old}={new}")
        for name, value in values.items():
            if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name)):
                raise Refused(400, "a value's name is its placeholder's (lower-case letters, digits and -)")
            args.append(f"--value={name}={one_line(value, 'a value', 4096)}")
        if body.get("as") not in (None, ""):
            if not (isinstance(body["as"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", body["as"])):
                raise Refused(400, "as is a board's name: lower-case letters, digits, _ and -", code="bad-name",
                              params={"what": "board"})
            args.append(f"--as={body['as']}")
        for item in strings(body.get("replace", []), "replace", 50):
            if not re.fullmatch(r"(?:mcp|llm|kit|variant|preset|image|board):[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", item):
                raise Refused(400, "replace takes KIND:NAME (mcp, llm, kit, variant, preset, image, board)")
            args.append(f"--replace={item}")
        if body.get("unsigned") is True:  # an MCP server of a catalog not signed (as mcp import --unsigned)
            args.append("--unsigned")
        if not check:
            confirmed(body, "board-import")
        text = json.dumps(bundle)
        if len(text) > BODY_LIMIT:
            raise Refused(413, "the bundle is too large for the web interface: import it with sandbox-ai board import")
        found = self.json_of(params["host"], args, 300, input=text)
        if not isinstance(found, dict) or not isinstance(found.get("problems"), list):
            raise Refused(502, "sandbox-ai board import: not a plan")
        return found

    def checkpoints(self, params, body):
        """sandbox-ai checkpoint list NAME --json: the checkpoints of a start's workspaces, newest first ([] when it
        has none yet)."""
        args = ["checkpoint", "list", start_name(params["name"]), "--json"]
        status, out, err = self.run(params["host"], args, 60)
        if status != 0 and "has checkpoints" in (err or ""):
            return []  # (no start of that name has any to go by: as the app says it)
        if status != 0:
            raise self.failed(params["host"], args, status, out, err)
        try:
            found = json.loads(out)
        except (ValueError, RecursionError):
            raise self.failed(params["host"], args, status, out, err) from None
        return found if isinstance(found, list) else []

    def take_checkpoint(self, params, body):
        """sandbox-ai checkpoint create NAME --json [--label TEXT]: a checkpoint of its workspaces now (adds, removes
        nothing)."""
        args = ["checkpoint", "create", start_name(params["name"]), "--json"]
        if body.get("label") not in (None, ""):
            args.append(f"--label={one_line(body['label'], 'label', 500)}")
        return self.json_of(params["host"], args, 1800, ok=(0,))

    def checkpoint_diff(self, params, body):
        """sandbox-ai checkpoint diff ID --json: what changed in its folders since (at most 2000 paths per kind)."""
        return self.json_of(params["host"], ["checkpoint", "diff", checkpoint_id(params["id"]), "--json"], 600,
                            ok=(0,))

    def checkpoint_preview(self, params, body):
        """sandbox-ai checkpoint restore ID --dry-run --json: what a restore would change (nothing is written)."""
        return self.json_of(params["host"], ["checkpoint", "restore", checkpoint_id(params["id"]), "--dry-run",
                                             "--json"], 600, ok=(0,))

    def checkpoint_restore(self, params, body):
        """sandbox-ai checkpoint restore ID --yes --json [--path P]...: confirmed (checkpoint-restore), the folders
        (or the paths P) put back; a checkpoint of the state before is taken first (its id: "before")."""
        ident = checkpoint_id(params["id"])
        paths = [one_line(path, "a path", 4096) for path in strings(body.get("paths", []), "paths", 50)]
        confirmed(body, "checkpoint-restore", f"restore {ident}")
        return self.json_of(params["host"], ["checkpoint", "restore", ident, "--yes", "--json",
                                             *(f"--path={path}" for path in paths)], 1800, ok=(0,))

    def checkpoint_delete(self, params, body):
        """sandbox-ai checkpoint delete ID --yes: confirmed (checkpoint-delete), it and the content only it held go."""
        ident = checkpoint_id(params["id"])
        confirmed(body, "checkpoint-delete", f"delete {ident}")
        return self.done(params["host"], ["checkpoint", "delete", ident, "--yes"], 300)

    def history(self, params, body):
        """sandbox-ai list --history NAME --json: what happened to NAME (start, exit, pause, resume, rename)."""
        found = self.json_of(params["host"], ["list", "--history", start_name(params["name"]), "--json"], 30)
        if not isinstance(found, list):
            raise Refused(502, "sandbox-ai list --history: not a list")
        return [entry for entry in found if isinstance(entry, dict)]

    def session_memory(self, params, body):
        """sandbox-ai session memory NAME --from SOURCE --json [--file F]: the agent's memory of a start - its files,
        or one file with its text (F: a name of that list)."""
        args = ["session", "memory", start_name(params["name"]), f"--from={params['source']}", "--json"]
        if params.get("file") is not None:
            args.append(f"--file={listed_file(params['file'])}")
        return self.json_of(params["host"], args, 120, ok=(0,))

    def session_files(self, params, body):
        """sandbox-ai session files NAME --json [--file F [--full]]: the agents' session files of a start, or one as a
        conversation."""
        args = ["session", "files", start_name(params["name"]), "--json"]
        if params.get("file") is not None:
            args.append(f"--file={listed_file(params['file'])}")
            if params.get("full"):
                args.append("--full")
        return self.json_of(params["host"], args, 300, ok=(0,))

    def session_files_search(self, params, body):
        """sandbox-ai session files NAME --json --search PATTERN [--text TEXT] [--case] on the start's host: the session
        files whose name (with a /: path) matches PATTERN - *, ?, [abc], ** for any folders, a part of the name without
        a wildcard - and with TEXT those that hold it, each with its line; bounded there, a login or key never looked
        into (session_inspect.search). Read only."""
        pattern, text, case = body.get("pattern", ""), body.get("text"), body.get("case", False)
        for what, value in (("pattern", pattern), ("text", text)):
            if value is not None and (not isinstance(value, str) or len(value) > 500 or not value.isprintable()):
                raise Refused(400, f"{what}: one line of printable text, at most 500 characters")
        if not isinstance(case, bool):
            raise Refused(400, "case is true or false")
        args = ["session", "files", start_name(params["name"]), "--json", f"--search={pattern.strip()}",
                *([f"--text={text}"] if text else []), *(["--case"] if case else [])]
        found = self.json_of(params["host"], args, 300, ok=(0,))
        if not isinstance(found, dict) or not isinstance(found.get("search"), dict):
            raise Refused(502, "not what sandbox-ai session files --search says")
        return found

    def session_secret_file(self, params, body):
        """sandbox-ai session files NAME --json --file F [--full] --show-secret: confirmed (show-secret-file), the text
        of a session file marked as a login or key."""
        name, file = start_name(params["name"]), listed_file(params["file"])
        confirmed(body, "show-secret-file", f"show {file} of {name}")
        if not isinstance(body.get("full", False), bool):
            raise Refused(400, "full is true or false")
        return self.json_of(params["host"], ["session", "files", name, "--json", f"--file={file}",
                                             *(["--full"] if body.get("full") else []), "--show-secret"], 300, ok=(0,))

    def memory_secret_file(self, params, body):
        """sandbox-ai session memory NAME --from SOURCE --json --file F --show-secret: confirmed (show-secret-file),
        the text of a memory file marked as a login or key."""
        name, file = start_name(params["name"]), listed_file(params["file"])
        confirmed(body, "show-secret-file", f"show {file} of {name}")
        return self.json_of(params["host"], ["session", "memory", name, f"--from={params['source']}", "--json",
                                             f"--file={file}", "--show-secret"], 120, ok=(0,))

    def session_export(self, params, body):
        """sandbox-ai session export NAME PATH --json [--include-secrets --yes]: confirmed (session-export; with
        include_secrets: session-export-secrets, its logins and keys taken in), the start's folder as a new tar.gz at
        PATH on its host; what was left out is in the answer."""
        name = start_name(params["name"])
        path = clean_path(body.get("path"), "the archive")
        include, files = flag(body, "include_secrets"), flag(body, "with_workspace")
        confirmed(body, "session-export-secrets" if include else "session-export",
                  f"export {name} to {path}" + (" with its logins and keys" if include else ""))
        return self.json_of(params["host"], ["session", "export", name, "--json",
                                             *(["--include-secrets", "--yes"] if include else []),
                                             *(["--with-workspace"] if files else []), "--", path], 3600,
                            ok=(0,))

    def session_duplicate(self, params, body):
        """sandbox-ai session duplicate NAME NEW --json [--with-workspace] [--include-secrets] [--grant-approvals]
        [--yes]: a second session of that host from the state of NAME (not started). Its logins and keys along are
        confirmed (session-export-secrets), its network approvals granted again too (session-grant-approvals)."""
        name = start_name(params["name"])
        new = body.get("new")
        if not (isinstance(new, str) and SESSION_NAME.fullmatch(new)):
            raise Refused(400, "the new session's name is 1-40 letters, digits, '_', '.' or '-'", code="bad-name",
                          params={"what": "session"})
        include, grant, files = flag(body, "include_secrets"), flag(body, "grant_approvals"), flag(body, "with_workspace")
        if include:
            confirmed(body, "session-export-secrets", f"duplicate {name} as {new} with its logins and keys")
        if grant:
            confirmed(body, "session-grant-approvals", f"duplicate {name} as {new} with its network approvals")
        return self.json_of(params["host"], ["session", "duplicate", name, new, "--json",
                                             *(["--with-workspace"] if files else []),
                                             *(["--include-secrets"] if include else []),
                                             *(["--grant-approvals"] if grant else []),
                                             *(["--yes"] if include or grant else [])], 7200, ok=(0,))

    def session_import(self, params, body):
        """sandbox-ai session import PATH --json [--as NAME] [--map=THERE=HERE]... [--check] [--replace]
        [--no-workspace] [--grant-approvals] [--yes]: an archive of session export on that host (a path there) as a
        session of it. With check: what it starts with and what it asks (nothing kept); else confirmed
        (session-import: what it asks was shown by its check; session-grant-approvals with grant_approvals)."""
        path = clean_path(body.get("path"), "the archive")
        check, replace, grant, no_workspace = (flag(body, key) for key in ("check", "replace", "grant_approvals",
                                                                            "no_workspace"))
        name = body.get("as")
        if name is not None and not (isinstance(name, str) and SESSION_NAME.fullmatch(name)):
            raise Refused(400, "the session's name is 1-40 letters, digits, '_', '.' or '-'", code="bad-name",
                          params={"what": "session"})
        maps = body.get("map") or []
        if not isinstance(maps, list) or len(maps) > 64:
            raise Refused(400, "map is a list of [folder there, folder here] (64 at most)")
        words = []
        for pair in maps:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise Refused(400, "map is a list of [folder there, folder here]")
            there, here = clean_path(pair[0], "a folder there"), clean_path(pair[1], "its folder here")
            if "=" in there:
                raise Refused(400, "a folder with '=' in it cannot be mapped")
            words.append(f"--map={there}={here}")
        if not check:
            confirmed(body, "session-import", f"import {path}" + (f" as {name}" if name else ""))
            if grant:
                confirmed(body, "session-grant-approvals", f"import {path} with its network approvals")
        return self.json_of(params["host"], ["session", "import", "--json", *(["--as", name] if name else []), *words,
                                             *(["--check"] if check else []), *(["--replace"] if replace else []),
                                             *(["--no-workspace"] if no_workspace else []),
                                             *(["--grant-approvals"] if grant and not check else []),
                                             *([] if check else ["--yes"]), "--", path], 7200, ok=(0,))

    @staticmethod
    def gc_days(value):
        if value is None:
            return []
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 5:
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 36500:
            raise Refused(400, "older_than is a whole number of days (0-36500), or null for the host's keep_days")
        return [f"--older-than={value}"]

    @staticmethod
    def gc_plan(found):
        """What a gc (--dry-run) result removes, as a set: ("removed", id), ("leftovers", name), ("histories", name)."""
        found = found if isinstance(found, dict) else {}
        plan = set()
        for key in ("removed", "leftovers", "histories"):
            for item in found.get(key) or []:
                ident = (item.get("id") or item.get("name")) if isinstance(item, dict) else item
                plan.add((key, str(ident)))
        return plan

    def gc_preview(self, params, body):
        """sandbox-ai gc --dry-run --json [--older-than DAYS]: what a clean-up would remove (nothing is)."""
        return self.json_of(params["host"], ["gc", "--dry-run", "--json", *self.gc_days(params.get("days"))], 120)

    def gc(self, params, body):
        """sandbox-ai gc --json [--older-than DAYS]: the clean-up itself (asked first: the page shows the preview).
        With "expected" (what the preview showed: {"removed": [ids], "leftovers": [names], "histories": [names]})
        a dry run comes first: when it would now remove anything the preview did not show, nothing is removed
        (409), and the page shows the new preview."""
        days = self.gc_days(body.get("older_than"))
        confirmed(body, "gc")
        expected = body.get("expected")
        if expected is not None:
            if not (isinstance(expected, dict) and set(expected) <= {"removed", "leftovers", "histories"}
                    and all(isinstance(v, list) and len(v) <= 10000 and all(isinstance(i, str) for i in v)
                            for v in expected.values())):
                raise Refused(400, "expected is {removed: [ids], leftovers: [names], histories: [names]}")
            shown = {(key, item) for key, items in expected.items() for item in items}
            now = self.gc_plan(self.json_of(params["host"], ["gc", "--dry-run", "--json", *days], 120))
            more = sorted(now - shown)
            if more:
                raise Refused(409, "the clean-up would now remove more than the preview showed (" + ", ".join(
                    f"{key} {item}" for key, item in more[:10]) + (" ..." if len(more) > 10 else "")
                              + "): nothing was removed; preview it again")
        return self.json_of(params["host"], ["gc", "--json", *days], 300)

    def storage_check(self, params, body):
        """sandbox-ai gc --check --dry-run --json [--home NAME]: what could be freed, each with how much, its command
        and whether it is safe while starts run (nothing is applied or recorded: the page shows it)."""
        name = params.get("name")
        return self.json_of(params["host"], ["gc", "--check", "--dry-run", "--json",
                                             *(["--home", start_name(name)] if name is not None else [])], 900)

    def storage_apply(self, params, body):
        """One item of the storage check's plan: {"argv": the item's argv} run on the host (only gc's forms the plan
        names: storage_argv), asked first (the page shows the plan)."""
        confirmed(body, "storage-apply")
        return self.json_of(params["host"], [*storage_argv(body.get("argv")), "--json"], 3600)

    def review_home(self, params, body):
        """sandbox-ai resume NAME --review-home --json: what changed in the start files of its kept HOME since its
        last start ({"name", "id", "state", "pending", "review": {"since", "changes": [...], "complete"} or
        null}); the page shows it before it resumes a stopped start."""
        return self.json_of(params["host"], ["resume", start_name(params["name"]), "--review-home", "--json"], 60)

    def mcp_attach(self, params, body):
        """As `sandbox-ai mcp attach NAME START [--restart]`: the MCP server connected to an agent start (kept for
        its later starts; a running one reaches it after a restart, done now with {"restart": true})."""
        name = params["name"]
        if not MCP_NAME.fullmatch(name):  # never a leading "-": read as an option of mcp attach
            raise Refused(400, "an MCP server's name is 1-32 letters, digits, '_' or '-', starting with a letter or "
                               "digit")
        start, restart = start_name(body.get("start")), body.get("restart", False)
        if not isinstance(restart, bool):
            raise Refused(400, "restart is true or false")
        confirmed(body, "mcp-restart" if restart else "mcp-attach", f"{name} to {start}")
        return self.json_of(params["host"], ["mcp", "attach", name, start, "--json", *(["--restart"] if restart else [])],
                            timeout=300)

    @staticmethod
    def live_server(body):
        """The server BODY names for mcp attach|detach --live, as (its argv before START, what the question names):
        {"server": NAME} (sandbox-ai mcp list) or {"helper": AGENT} (one of AGENTS as a helper), one of
        them; never a leading "-" (read as an option there)."""
        server, helper = body.get("server"), body.get("helper")
        if (server is None) == (helper is None):
            raise Refused(400, f"name the server (server: NAME) or a helper agent (helper: {', '.join(AGENTS)})")
        if helper is not None:
            if helper not in AGENTS:
                raise Refused(400, f"a helper is one of {', '.join(AGENTS)}")
            return [f"--helper={helper}"], f"the helper {helper}"
        if not (isinstance(server, str) and MCP_NAME.fullmatch(server)):
            raise Refused(400, "an MCP server's name is 1-32 letters, digits, '_' or '-', starting with a letter or "
                               "digit")
        return [server], server

    def mcp_live_attach(self, params, body):
        """sandbox-ai mcp attach NAME START --live --json (or --helper AGENT START): the server reached by the running
        start now, without a restart (kept for its later starts; asked first: mcp-attach). Its JSON says whether the
        agent sees it at once (agent_restart: "maybe" for Claude Code, "required" for Codex and OpenCode)."""
        start = start_name(params["name"])
        server, shown = self.live_server(body)
        confirmed(body, "mcp-attach", f"{shown} to {start}, now while it runs")
        return self.json_of(params["host"], ["mcp", "attach", *server, start, "--live", "--json"], timeout=300)

    def mcp_live_detach(self, params, body):
        """sandbox-ai mcp detach NAME START --live --json (or --helper AGENT START): its relay in the running start
        closed now, and out of its options (asked first: mcp-detach)."""
        start = start_name(params["name"])
        server, shown = self.live_server(body)
        confirmed(body, "mcp-detach", f"{shown} from {start}")
        return self.json_of(params["host"], ["mcp", "detach", *server, start, "--live", "--json"], timeout=120)

    def kits_live_attach(self, params, body):
        """sandbox-ai kits attach START (KIT | --skill DIR | --command FILE | --agent-file FILE) --json, or with
        {"plugin": NAME@MARKETPLACE} sandbox-ai plugins attach START PLUGIN --json: given to the running start now,
        read-only (asked first: kits-attach); {"keep": true} also for its later starts, {"restart": true} its agent
        started again inside the sandbox afterwards. Its JSON says what the agent needs to see it ("reload")."""
        start = start_name(params["name"])
        keep, restart = body.get("keep", False), body.get("restart", False)
        findings = body.get("allow_findings", False)  # what the hidden-text checker finds is refused unless this
        if not isinstance(keep, bool) or not isinstance(restart, bool) or not isinstance(findings, bool):
            raise Refused(400, "keep, restart and allow_findings are true or false")
        given = [key for key in ("kit", "skill", "command", "agent_file", "plugin") if body.get(key) not in (None, "")]
        if len(given) != 1:
            raise Refused(400, "name one: kit, skill (a folder), command (a file), agent_file (a file) or plugin")
        key = given[0]
        flags = [*(["--keep"] if keep else []), *(["--restart"] if restart else []),
                 *(["--allow-findings"] if findings else []), "--json"]
        if key == "plugin":
            plugin = body["plugin"]
            if not (isinstance(plugin, str) and PLUGIN_ID.fullmatch(plugin)):
                raise Refused(400, "a plugin is NAME@MARKETPLACE")
            confirmed(body, "kits-attach", f"the plugin {plugin} to {start}, now while it runs")
            return self.json_of(params["host"], ["plugins", "attach", start, plugin, *flags], timeout=900)
        if key == "kit":
            value = body["kit"] if body["kit"] == "default" else kit_name(body["kit"])
            argv = ["kits", "attach", start, value]
        else:
            value = clean_path(body[key], key)
            argv = ["kits", "attach", start, f"--{key.replace('_', '-')}={value}"]
        confirmed(body, "kits-attach", f"{key.replace('_', ' ')} {value} to {start}, now while it runs")
        return self.json_of(params["host"], [*argv, *flags], timeout=300)

    def kits_live_detach(self, params, body):
        """sandbox-ai kits detach START NAME [--kind KIND] --json, or with {"plugin"} sandbox-ai plugins detach START
        PLUGIN --json: what attach gave the running start out of it again (and of its later starts where it was
        kept). Asked first: kits-detach."""
        start = start_name(params["name"])
        restart = body.get("restart", False)
        if not isinstance(restart, bool):
            raise Refused(400, "restart is true or false")
        flags = [*(["--restart"] if restart else []), "--json"]
        if body.get("plugin") not in (None, ""):
            plugin = body["plugin"]
            if not (isinstance(plugin, str) and PLUGIN_ID.fullmatch(plugin)):
                raise Refused(400, "a plugin is NAME@MARKETPLACE")
            confirmed(body, "kits-detach", f"the plugin {plugin} from {start}")
            return self.json_of(params["host"], ["plugins", "detach", start, plugin, *flags], timeout=120)
        name, kind = body.get("name"), body.get("kind")
        if not (isinstance(name, str) and LIVE_ENTRY.fullmatch(name)):
            raise Refused(400, "name is the skill, subagent or command (its file name), or the kit")
        if kind not in (None, "", "skills", "agents", "commands", "prompts"):
            raise Refused(400, "kind is skills, agents, commands or prompts")
        confirmed(body, "kits-detach", f"{name} from {start}")
        return self.json_of(params["host"], ["kits", "detach", start, name, *([f"--kind={kind}"] if kind else []),
                                             *flags], timeout=120)

    def agent_restart(self, params, body):
        """sandbox-ai kits restart-agent START --json: the agent of the running start started again in its tmux window,
        inside the sandbox (its network, MCP servers and kits stay; the conversation continues). Asked first:
        agent-restart."""
        start = start_name(params["name"])
        confirmed(body, "agent-restart", start)
        return self.json_of(params["host"], ["kits", "restart-agent", start, "--json"], timeout=120)

    def plugins_status(self, params, body):
        """sandbox-ai plugins --status: what the kits declare and what is installed, as it says it."""
        return self.done(params["host"], ["plugins", "--status"], 120)

    def mcp_detach(self, params, body):
        """sandbox-ai mcp detach NAME START --json (or --helper AGENT START): the server out of a kept start's options,
        so that its later starts go without it; for a start that does not run now (stopped or paused: a paused one's
        relay stays until it ends). Asked first: mcp-detach."""
        start = start_name(params["name"])
        server, shown = self.live_server(body)
        confirmed(body, "mcp-detach", f"{shown} from {start}")
        return self.json_of(params["host"], ["mcp", "detach", *server, start, "--json"], timeout=120)

    # the network of a running sandbox (sandbox-ai net), and the presets to start from
    def net_show(self, params, body):
        return self.json_of(params["host"], ["net", "show", start_name(params["name"]), "--json"], 30)

    def blocked(self, params, body):
        """sandbox-ai approvals blocked NAME --json on its host: what the running start refused, newest first."""
        return self.json_of(params["host"], ["approvals", "blocked", start_name(params["name"]), "--json"], 60)

    def net_change(self, params, body):
        """sandbox-ai net allow|revoke NAME with what BODY names; each value is checked and passed as --flag=VALUE,
        so nothing in it is read as another option. A Git login names where its token is (a variable or a secret
        of that host), never the token."""
        op = body.get("op")
        if op not in ("allow", "revoke"):
            raise Refused(400, "op is allow or revoke")
        args = ["net", op, start_name(params["name"])]
        shared = {}
        for value in net_items(body, "allow_host"):
            if not (NET_HOST.fullmatch(value) or NET_WILDCARD.fullmatch(value)):
                raise Refused(400, f"not a host name or *.DOMAIN.TLD: {printable(value)}")
            risk = shared_risk(value) if op == "allow" and value.startswith("*.") else None
            if risk:  # below a shared service: only after its own confirmation, then as that confirmation
                shared[value.lower()] = risk
                args.append(f"--allow-shared-wildcard={value}")
            else:
                args.append(f"--allow-host={value}")
        if shared:  # asked by itself (its own key): the net-allow yes is not this one
            confirmed(body, "shared-wildcard", "; ".join(risk[0] for risk in shared.values()), code="shared-wildcard",
                      params={"wildcards": ", ".join(shared),
                              "suffix": ", ".join(sorted({risk[1] for risk in shared.values()}))})
        for value in net_items(body, "allow_tcp"):
            if not NET_TCP.fullmatch(value):
                raise Refused(400, f"IP:PORT or [IPv6]:PORT: {printable(value)}")
            args.append(f"--allow-tcp={value}")
        ports = body.get("host_ports", [])
        if not isinstance(ports, list) or len(ports) > 16 or not all(
                isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in ports):
            raise Refused(400, "host_ports is a list of at most 16 ports")
        args += [f"--allow-host-port={port}" for port in ports]
        for value in net_items(body, "allow"):
            args.append(f"--allow={ip_value(value, 'allow', network=True)}")
        presets = strings(body.get("presets", []), "presets", len(NET_PRESETS))
        if not set(presets) <= set(NET_PRESETS):
            raise Refused(400, f"presets are {', '.join(NET_PRESETS)}")
        args += [f"--allow-{preset}" for preset in dict.fromkeys(presets)]
        if body.get("dns") is True:
            args.append("--dns")
        for value in net_items(body, "nameservers", 8):
            args.append(f"--nameserver={ip_value(value, 'a nameserver')}")
        login = body.get("git_login")
        if login is not None:
            if not (isinstance(login, dict) and login.get("kind") in ("env", "secret", "file")
                    and isinstance(login.get("url"), str) and NET_GIT_URL.fullmatch(login["url"])
                    and isinstance(login.get("ref"), str)
                    and (NET_REF.fullmatch(login["ref"]) if login["kind"] != "file" else login["ref"].startswith("/"))):
                raise Refused(400, "git_login is {url: http(s)://[USER@]HOST[:PORT], kind: env|secret|file, ref: NAME "
                                   "or, for a file, its absolute path on that host}")
            if login["kind"] == "file":
                clean_path(login["ref"], "the token file")
            args.append(f"--git-token-{login['kind']}={login['url']}={login['ref']}")
            if login.get("in_sandbox") is True and op == "allow":
                # A run whose proxy cannot log in (no proxy, --no-inspect, an older start): the login into the
                # sandbox's session directory instead, where the agent can read the token - only on the human's
                # word (the page asks, naming it), never by default.
                args.append("--git-token-in-sandbox")
        key = body.get("api_key")
        if key is not None and op == "revoke":
            # A key given during the run taken back by the variable it was given as (net revoke --api-key NAME: its
            # host and secret are not read, the page sends the row as it listed it).
            if not (isinstance(key, dict) and isinstance(key.get("name"), str) and NET_REF.fullmatch(key["name"])):
                raise Refused(400, "api_key is {name: the variable the key was given as}, for revoke")
            args.append(f"--api-key={key['name']}")
        elif key is not None:  # a secret of that host as an API key the run's proxy puts in for HOST (never the key)
            if not (isinstance(key, dict) and isinstance(key.get("host"), str)
                    and NET_HOST.fullmatch(key["host"]) and "." in key["host"]
                    and isinstance(key.get("name"), str) and NET_REF.fullmatch(key["name"])
                    and isinstance(key.get("secret"), str) and NET_REF.fullmatch(key["secret"])):
                raise Refused(400, "api_key is {host: a host name, name: the variable, secret: a secret of that "
                                   "host}, for allow ({name} alone for revoke)")
            args.append(f"--api-key={key['host']}={key['name']}={key['secret']}")
        if len(args) == 3:
            raise Refused(400, "nothing to change")
        if flag(body, "keep"):
            if op != "allow":
                raise Refused(400, "keep is for allow (net show --keep keeps what the run was given)")
            args.append("--keep")
        if op == "allow":
            confirmed(body, "net-allow", f"{' '.join(args[3:])} for {args[2]}")
        if "--keep" in args:  # and written into the project's .sandbox-ai.json for every later start of it
            confirmed(body, "net-keep", f"{' '.join(a for a in args[3:] if a != '--keep')} for {args[2]}")
        return self.done(params["host"], args, 60)

    def net_untaint(self, params, body):
        """sandbox-ai net untaint NAME: the run's taint taken back (asked first)."""
        name = start_name(params["name"])
        confirmed(body, "net-untaint", name)
        return self.done(params["host"], ["net", "untaint", name], 60)

    # what a running sandbox is given while it runs: host directories (sandbox-ai workspace), devices (sandbox-ai
    # devices) and its resource limits (sandbox-ai limits); each with that command's checks on its host. What widens
    # the sandbox (a writable directory, a device, a forced removal that leaves open files usable) needs
    # {"confirm": true}: the page asks first, naming the host.
    def workspaces(self, params, body):
        return self.json_of(params["host"], ["workspace", "list", start_name(params["name"]), "--json"], 30)

    def workspace_add(self, params, body):
        """sandbox-ai workspace add NAME PATH [--name=AS] [--ro]: PATH, an absolute directory or regular file on that
        host ("path"; "directory" as before), mounted at /workspaces/AS (default: its own name) in the running
        sandbox. AS: 1-64 letters, digits, '_', '.' or '-', starting with a letter or digit (the command's --name)."""
        name = start_name(params["name"])
        if ("path" in body) == ("directory" in body):
            raise Refused(400, "path is the directory or file on that host (or directory, as before; not both)")
        what = "path" if "path" in body else "directory"
        # A folder of another machine (sshfs://...): that host mounts it with sshfs, then adds it as a folder.
        path = remote_source(body.get(what), what) or no_dots(host_path(body.get(what), what), what)
        read_only = body.get("read_only", False)
        if not isinstance(read_only, bool):
            raise Refused(400, "read_only is true or false")
        given = body.get("name")
        if given in (None, ""):
            named = []
        elif isinstance(given, str) and WORKSPACE_AS.fullmatch(given):
            named = [f"--name={given}"]
        else:
            raise Refused(400, "name is 1-64 letters, digits, '_', '.' or '-', starting with a letter or digit")
        if not read_only:
            confirmed(body, "workspace-writable", path)
        return self.done(params["host"], ["workspace", "add", name, path, *named, *(["--ro"] if read_only else [])],
                         120)

    def workspace_remove(self, params, body):
        """sandbox-ai workspace remove NAME DIR|NAME [--force]: a workspace added during the run (its path in the
        sandbox, its host directory, or its name below /workspaces)."""
        name = start_name(params["name"])
        value, force = body.get("workspace"), body.get("force", False)
        if remote_source(value, "workspace"):
            value = remote_source(value, "workspace")
        elif isinstance(value, str) and value.startswith("/"):
            no_dots(host_path(value, "workspace"), "workspace")
        elif not (isinstance(value, str) and WORKSPACE_NAME.fullmatch(value) and value.strip() == value):
            raise Refused(400, "workspace is its path (in the sandbox or on the host) or its name below /workspaces")
        if not isinstance(force, bool):
            raise Refused(400, "force is true or false")
        if force:
            confirmed(body, "workspace-force", value)
        return self.done(params["host"], ["workspace", "remove", name, value, *(["--force"] if force else [])], 120)

    def devices(self, params, body):
        return self.json_of(params["host"], ["devices", "list", start_name(params["name"]), "--json"], 30)

    def device_scan(self, params, body):
        """sandbox-ai devices scan --json on that host: {"devices": [{"kind", "value", "option", "label", "nodes",
        "refused", "note", ...}], "notes"} - what the page offers to add (that host's devices, never this one's)."""
        return self.json_of(params["host"], ["devices", "scan", "--json"], 60, ok=(0,))

    def device_add(self, params, body):
        """sandbox-ai devices add NAME DEVICE: with --device's checks and the run's device rules there (an "ask"
        rule waits up to a minute for an answer in Approvals)."""
        name, device = start_name(params["name"]), device_value(body.get("device"))
        confirmed(body, "device-add", device)
        return self.done(params["host"], ["devices", "add", name, device], DEVICE_ADD_TIMEOUT)

    def device_remove(self, params, body):
        name, device = start_name(params["name"]), body.get("device")
        if not (isinstance(device, str) and DEVICE_ID.fullmatch(device)):
            device = device_value(device)
        return self.done(params["host"], ["devices", "remove", name, device], 60)

    def install_list(self, params, body):
        """sandbox-ai install --list --json on that host: {"items": [{"id", "kind", "title", "installed", "size",
        "note", "pending", "all", "needs"?}], "problems": [...]} (the catalogs are read there, over the network)."""
        return self.json_of(params["host"], ["install", "--list", "--json"], 300, ok=(0,))

    def toolchains(self, params, body):
        """sandbox-ai toolchain list --json on that host: its store ([{"name", "version", "commands", "hosts",
        "allow", "signed_by", "used_by", ...}]) - what a running start there can be given."""
        return self.json_of(params["host"], ["toolchain", "list", "--json"], 30)

    def run_toolchains(self, params, body):
        """sandbox-ai toolchain list --run NAME --json: the running start's toolchains ("source": start or added)."""
        return self.json_of(params["host"], ["toolchain", "list", "--run", start_name(params["name"]), "--json"], 30)

    def toolchain_attach(self, params, body):
        """sandbox-ai toolchain attach NAME TOOLCHAIN [--offline]: one of that host's store, mounted read-only into
        the running start; its registries are opened as `net allow` opens hosts (the run's mode decides)."""
        name, toolchain = start_name(params["name"]), toolchain_value(body.get("toolchain"))
        offline = body.get("offline", False)
        if not isinstance(offline, bool):
            raise Refused(400, "offline is true or false")
        confirmed(body, "toolchain-add", toolchain)
        return self.done(params["host"], ["toolchain", "attach", name, toolchain, *(["--offline"] if offline else [])],
                         300)

    def toolchain_detach(self, params, body):
        name, toolchain = start_name(params["name"]), toolchain_value(body.get("toolchain"))
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise Refused(400, "force is true or false")
        if force:
            confirmed(body, "toolchain-force", toolchain)
        return self.done(params["host"], ["toolchain", "detach", name, toolchain, *(["--force"] if force else [])], 150)

    def limits(self, params, body):
        """sandbox-ai limits NAME --show --json: {"memory": bytes, "memory_high": bytes, "cpus": N, "pids": N},
        each null without a limit."""
        return self.json_of(params["host"], ["limits", start_name(params["name"]), "--show", "--json"], 30)

    def set_limits(self, params, body):
        """sandbox-ai limits NAME --memory=SIZE --cpus=N --pids=N (each one given or not). A limit above the current
        one (limits --show, read first unless the request is confirmed) or none (no limit) needs {"confirm": true}:
        a huge value is no limit either. The security mode's cap is kept by the command."""
        name = start_name(params["name"])
        args, values = ["limits", name], {}
        for key in ("memory", "cpus", "pids"):
            value = body.get(key)
            if value in (None, ""):
                continue
            if not (isinstance(value, str) and LIMIT_VALUE.fullmatch(value)):
                raise Refused(400, f"{key} is none or a number (with k, m, g or t for a size), e.g. 8g, 1.5, 4096")
            args.append(f"--{key}={value}")
            values[key] = value
        if not values:
            raise Refused(400, "nothing to change: give memory, cpus or pids")
        if body.get("confirm") is not True:
            removed = [key for key, value in values.items() if value == "none"]
            if removed:
                confirmed(body, "limits-raise", f"none takes the {' and '.join(removed)} limit away")
            raised = self.raised_limits(params["host"], name, values)
            if raised:
                confirmed(body, "limits-raise", raised)
        return self.done(params["host"], args, 60)

    def raised_limits(self, host, name, values):
        """What of VALUES ({key: "8g" | "1.5" | "4096"}) is above the start's current limits, as words (None: none
        is); the current limits unreadable: all of it is (it cannot be told)."""
        try:
            current = self.json_of(host, ["limits", name, "--show", "--json"], 30)
        except Refused as exc:
            return f"the current limits could not be read ({exc})"
        if not isinstance(current, dict):
            return "the current limits could not be read"
        above = []
        for key, value in values.items():
            now = current.get(key)
            if now is None:
                continue  # no limit now: any number is one
            if isinstance(now, bool) or not isinstance(now, (int, float)):
                return f"the current {key} limit could not be read"
            if limit_number(key, value) > now:
                above.append(f"{key} {value} is above the current {key} limit")
        return "; ".join(above) or None

    # session bridges (sandbox-ai bridge): every value checked here as the command takes it, nothing an option
    def bridges(self, params, body):
        """bridge list --hosts --json: this host's bridges and those of its own hosts, each once (HOST:ID)."""
        return self.json_of(params["host"], ["bridge", "list", "--hosts", "--json"], 150, ok=(0,))

    def bridge_show(self, params, body):
        return self.json_of(params["host"], ["bridge", "show", bridge_id(params["id"]), "--json"], 150, ok=(0,))

    def bridge_add(self, params, body):
        """bridge add A B --allow ... --json (dry_run: --dry-run, nothing made, no confirmation needed). A and B: a
        session of that host, or HOST:NAME of one of its hosts (both of one host: made there; of two: relayed)."""
        a, b = (bridge_session(body.get(side), side.upper()) for side in ("a", "b"))
        caps = strings(body.get("allow"), "allow", 64)
        if not caps or any(not BRIDGE_CAP.fullmatch(cap) for cap in caps):
            raise Refused(400, "allow: message, ask, watch, files:ro:PATH or files:rw:PATH (a path without a comma "
                               "or a control character)")
        args = ["bridge", "add", a, b, "--allow=" + ",".join(caps)]
        if body.get("both") is True:
            args.append("--both")
        until = body.get("until")
        if until not in (None, ""):
            if not (isinstance(until, str) and BRIDGE_UNTIL.fullmatch(until.strip())):
                raise Refused(400, "until: 30m, 2h, 1d, HH:MM or an ISO time")
            args.append(f"--until={until.strip()}")
        ask = body.get("ask", "first")
        if ask not in ("each", "first", "never"):
            raise Refused(400, "ask: each, first or never")
        args.append(f"--ask={ask}")
        actions = body.get("observer_actions")
        if actions is not None:
            if not (isinstance(actions, list) and all(x in ("alert", "hint", "net-cut", "pause") for x in actions)):
                raise Refused(400, "observer_actions: some of alert, hint, net-cut, pause")
            args.append("--observer-actions=" + ",".join(dict.fromkeys(actions)))
        limits = body.get("limits") or {}
        if not isinstance(limits, dict) or any(key not in ("rate", "size", "ask-timeout", "daily") or
                                               not (isinstance(value, str) and BRIDGE_LIMIT.fullmatch(value))
                                               for key, value in limits.items()):
            raise Refused(400, "limits: rate, size, ask-timeout, daily, each a number (k, m, g for sizes)")
        args += [f"--limit={key}={value}" for key, value in limits.items()]
        if body.get("dry_run") is True:
            return self.json_of(params["host"], [*args, "--dry-run", "--json"], 150, ok=(0,))
        confirmed(body, "bridge-add", f"{a} -> {b}")
        return self.json_of(params["host"], [*args, "--json"], 240, ok=(0,))

    def bridge_action(self, params, body):
        ident, action = bridge_id(params["id"]), params["action"]
        confirmed(body, f"bridge-{action}", ident)
        return self.done(params["host"], ["bridge", action, ident], 150)

    # VPN profiles (sandbox-ai vpn): a profile names the secret that holds its key; the web never carries a key
    def vpn_list(self, params, body):
        return self.json_of(params["host"], ["vpn", "list", "--json"], 30)

    def vpn_add(self, params, body):
        name, kind = body.get("name"), body.get("kind")
        if not (isinstance(name, str) and VPN_NAME.fullmatch(name)):
            raise Refused(400, "a profile name is 1-32 lower-case letters, digits, '_' or '-'")
        if kind not in ("wireguard", "openvpn", "mullvad", "tailscale", "tor"):
            raise Refused(400, "kind is wireguard, openvpn, mullvad, tailscale or tor")
        args = ["vpn", "add", name]
        if kind in ("wireguard", "openvpn"):
            path = body.get("file")
            if not (isinstance(path, str) and path.startswith("/") and "\0" not in path and len(path) < 4096):
                raise Refused(400, "file is the configuration's absolute path on that host")
            args.append(f"--{kind}={path}")
        else:
            args.append(f"--{kind}")
        secret = body.get("secret")
        if kind == "mullvad" and secret is None:  # without it the command asks for the account number with getpass
            raise Refused(400, "a Mullvad profile names the secret with its account number")
        if secret is not None:
            option = {"openvpn": "--auth-secret", "mullvad": "--account-secret", "tailscale": "--auth-key-secret"}.get(kind)
            if option is None or not (isinstance(secret, str) and NET_REF.fullmatch(secret)):
                raise Refused(400, "secret names a secret of sandbox-ai on that host (not for WireGuard)")
            args.append(f"{option}={secret}")
        for key, option, kinds, pattern in (("location", "--location", ("mullvad",), VPN_LOCATION),
                                            ("exit_node", "--exit-node", ("tailscale",), NET_HOST),
                                            ("login_server", "--login-server", ("tailscale",), VPN_LOGIN_SERVER)):
            value = body.get(key)
            if value is not None:
                if kind not in kinds or not (isinstance(value, str) and pattern.fullmatch(value)):
                    raise Refused(400, f"{key} does not fit this profile")
                args.append(f"{option}={value}")
        args += self.vpn_options(kind, body)
        return self.done(params["host"], args, 180)

    @staticmethod
    def vpn_options(kind, body):
        """The further options of vpn add, each only for the kinds that have it: resolvers, check URLs, the
        provider, a replacement (force), OpenVPN's split, Mullvad's IPv6 choices, Tailscale's node name, ephemeral
        nodes and tailnet range, Tor's bridges, exit countries and SOCKS port (which passes the proxy by: asked)."""
        args = []
        for value in strings(body.get("dns", []), "dns", 8):
            args.append(f"--dns={ip_value(value, 'a resolver')}")
        for value in strings(body.get("check_urls", []), "check_urls", 8):
            if not (value.startswith("https://") and value.isprintable() and " " not in value and len(value) <= 2048):
                raise Refused(400, "a check URL is https://...")
            args.append(f"--check-url={value}")
        if body.get("provider") not in (None, ""):
            if not (isinstance(body["provider"], str) and PROVIDER.fullmatch(body["provider"])):
                raise Refused(400, "provider is a name of lower-case letters, digits, '_' or '-'")
            args.append(f"--provider={body['provider']}")
        switches = {"no_dns_probe": ("--no-dns-probe", None), "force": ("--force", None),
                    "split": ("--split", "openvpn"), "no_ipv6": ("--no-ipv6", "mullvad"),
                    "endpoint_ipv6": ("--endpoint-ipv6", "mullvad"), "ephemeral": ("--ephemeral", "tailscale"),
                    "socks": ("--socks", "tor")}
        for key, (option, only) in switches.items():
            if flag(body, key):
                if only and kind != only:
                    raise Refused(400, f"{key} is for {only}")
                if key == "socks":
                    confirmed(body, "vpn-socks", str(body.get("name")))
                if key == "force":
                    confirmed(body, "vpn-replace", str(body.get("name")))
                args.append(option)
        if body.get("hostname") not in (None, ""):
            if kind != "tailscale" or not (isinstance(body["hostname"], str) and TS_HOSTNAME.fullmatch(body["hostname"])):
                raise Refused(400, "hostname is a Tailscale node's name (lower-case letters, digits and '-')")
            args.append(f"--hostname={body['hostname']}")
        for value in strings(body.get("tailnet_allow", []), "tailnet_allow", 16):
            if kind != "tailscale":
                raise Refused(400, "tailnet_allow is for tailscale")
            args.append(f"--tailnet-allow={ip_value(value, 'tailnet_allow', network=True)}")
        bridges, bridge_secret = body.get("bridges"), body.get("bridge_secret")
        if bridges not in (None, "") or bridge_secret not in (None, "") or body.get("exit_country") not in (None, ""):
            if kind != "tor":
                raise Refused(400, "bridges, bridge_secret and exit_country are for tor")
        if bridges not in (None, "") and bridge_secret not in (None, ""):
            raise Refused(400, "bridges (a file) or bridge_secret (a secret's name), not both")
        if bridges not in (None, ""):
            args.append(f"--bridges={clean_path(bridges, 'bridges')}")
        if bridge_secret not in (None, ""):
            args.append(f"--bridge-secret={secret_name(bridge_secret)}")
        if body.get("exit_country") not in (None, ""):
            if not (isinstance(body["exit_country"], str) and TOR_COUNTRIES.fullmatch(body["exit_country"])):
                raise Refused(400, "exit_country is a country code, or several: de or de,nl")
            args.append(f"--exit-country={body['exit_country']}")
        return args

    def vpn_check(self, params, body):
        return self.done(params["host"], ["vpn", "check", vpn_name(params["name"])], 180)

    def vpn_remove(self, params, body):
        name = vpn_name(params["name"])
        confirmed(body, "vpn-remove", name)
        return self.done(params["host"], ["vpn", "remove", name], 60)

    # the canary log (sandbox-ai canaries log): every event, by which process, next to the agent's tool call and
    # model requests; redacted there, and the run's tokens never in it
    def canaries(self, params, body):
        run = params.get("run")
        if run is not None and not (CANARY_RUN.fullmatch(run)):
            raise Refused(400, "a run is 1-81 letters, digits, '_', '.' or '-'")
        document = self.json_of(params["host"], ["canaries", "log", *([run] if run else []), "--json"], 60)
        return [document] if run else document

    # the workspace review after each run (sandbox-ai workspace review): every project's runs, each change with its
    # diff, findings, whether its before-state was kept and what was decided; Keep, and Restore (confirmed)
    def workspace_reviews(self, params, body):
        return self.json_of(params["host"], ["workspace", "review", "--all", "--json"], 120, ok=(0,))

    @staticmethod
    def review_target(body):
        """(log folder, run, path) of a Keep or Restore: the review's log folder, its run id, the file as the review
        names it (its path in the sandbox)."""
        run = body.get("run")
        if not (isinstance(run, str) and REVIEW_RUN.fullmatch(run)):
            raise Refused(400, "a run is 1-128 letters, digits, '_', '.' or '-' (sandbox-ai workspace review lists them)")
        return clean_path(body.get("log_dir"), "log_dir"), run, one_line(body.get("path"), "a path", 4096)

    def workspace_review_keep(self, params, body):
        """sandbox-ai workspace review RUN --log-dir DIR --keep PATH --json: the change is recorded as wanted."""
        log_dir, run, path = self.review_target(body)
        return self.json_of(params["host"], ["workspace", "review", run, f"--log-dir={log_dir}", f"--keep={path}",
                                             "--json"], 120, ok=(0,))

    def workspace_review_restore(self, params, body):
        """sandbox-ai workspace review RUN --log-dir DIR --restore PATH --yes --json: confirmed (workspace-restore),
        the file put back as it was at the start from its kept before-state (an added one removed)."""
        log_dir, run, path = self.review_target(body)
        confirmed(body, "workspace-restore", f"restore {path}")
        return self.json_of(params["host"], ["workspace", "review", run, f"--log-dir={log_dir}", f"--restore={path}",
                                             "--yes", "--json"], 120, ok=(0,))

    # the MCP tool definitions pinned per project (sandbox-ai mcp tools)
    def mcp_tool_pins(self, params, body):
        return self.json_of(params["host"], ["mcp", "tools", "--all", "--json"], 60, ok=(0,))

    def pin_target(self, body):
        return self.mcp_server(body.get("server")), clean_path(body.get("project"), "project")

    def mcp_tool_pin(self, params, body):
        """sandbox-ai mcp tools NAME --project DIR --json: the pinned definitions, the list seen last, what differs."""
        server, project = self.pin_target(body)
        return self.json_of(params["host"], ["mcp", "tools", server, "--json", f"--project={project}"], 60, ok=(0,))

    def mcp_tool_repin(self, params, body):
        """sandbox-ai mcp tools NAME --repin: confirmed (mcp-pin-repin); refused there when the list holds hidden
        text."""
        server, project = self.pin_target(body)
        confirmed(body, "mcp-pin-repin", f"re-pin {server}")
        return self.json_of(params["host"], ["mcp", "tools", server, "--repin", "--json", f"--project={project}"], 60,
                            ok=(0,))

    def mcp_tool_forget(self, params, body):
        """sandbox-ai mcp tools NAME --forget: confirmed (mcp-pin-forget)."""
        server, project = self.pin_target(body)
        confirmed(body, "mcp-pin-forget", f"forget the pin of {server}")
        return self.json_of(params["host"], ["mcp", "tools", server, "--forget", "--json", f"--project={project}"], 60,
                            ok=(0,))

    # what the safeguards refused or ended (sandbox-ai safeguards): why, the process, the command that would allow
    # it; redacted there (no secret values), the newest SAFEGUARDS_TAIL of every run or of one
    def safeguards(self, params, body):
        run = params.get("run")
        if run is not None and not CANARY_RUN.fullmatch(run):
            raise Refused(400, "a run is 1-81 letters, digits, '_', '.' or '-'")
        args = ["safeguards", "--json", "--tail", str(SAFEGUARDS_TAIL), *(["--", run] if run else ["--all"])]
        status, out, err = self.run(params["host"], args, 60)
        if status != 0:
            raise Refused(502, f"sandbox-ai safeguards on {params['host']}: exit status {status}: "
                               f"{printable((err or out).strip()[-600:])}")
        records = []
        for line in out.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def monitor(self, params, body):
        """What runs on the host now, as its collectors recorded it (sandbox-ai top --json --no-measure: nothing is
        measured for the page, no sleep): runs with their project, sample, spark and warnings; the host's totals."""
        return self.json_of(params["host"], ["top", "--json", "--no-measure"], 60, ok=(0,))

    def monitor_usage(self, params, body):
        """The window SINCE (1-999 minutes, hours or days) of the host's series: per run its averages, peaks, totals and
        series (at most 240 points), the idle runs, the warnings, the host's (sandbox-ai stats usage)."""
        since = params["since"]
        return self.json_of(params["host"], ["stats", "usage", "--since", since, "--json", "--series", "--by", "run"],
                            90, ok=(0,))

    def spend_run(self, params, body):
        """sandbox-ai spend run NAME --json: the cost of its model requests so far, tokens, its budgets and how much of
        each is used, whether a request was refused."""
        return self.json_of(params["host"], ["spend", "run", start_name(params["name"]), "--json"], 60, ok=(0,))

    def spend_raise(self, params, body):
        """sandbox-ai spend raise NAME USD: the running start may spend USD more than its budgets (its refused requests
        go again). Needs {"confirm": true}."""
        name = start_name(params["name"])
        usd = body.get("usd")
        if isinstance(usd, str):
            try:
                usd = float(usd.strip().removeprefix("$"))
            except ValueError:
                usd = None
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not 0 < usd <= 1_000_000:
            raise Refused(400, "usd is a number of USD above 0 (at most 1000000)")
        amount = f"{usd:.6f}".rstrip("0").rstrip(".")
        confirmed(body, "spend-raise", f"USD {amount} more for {name}")
        return self.done(params["host"], ["spend", "raise", name, amount], 60)

    def presets(self, params, body):
        return self.json_of(params["host"], ["presets", "list", "--json"], 30)

    def cloud_models(self, params, body):
        """sandbox-ai models --json of the host: each agent's cloud models with their effort levels (the table its
        launcher refuses an --effort by), for the start form's Model and Effort choices."""
        return self.json_of(params["host"], ["models", "--json"], 30, ok=(0,))

    def preset_categories(self, params, body):
        """sandbox-ai presets categories --json of the host: the categories its presets are listed under, in order, with
        English and German titles and descriptions (an older sandbox-ai has none: the page keeps its own list)."""
        return self.json_of(params["host"], ["presets", "categories", "--json"], 30, ok=(0,))

    # secrets (sandbox-ai secrets) of a host: their names and where each is kept, never a value. A value is sent
    # once, as {"secret": VALUE} (a field the audit log leaves out), to `secrets set NAME` on its standard input as
    # the command reads it (one line): never on a command line, in a response or in a log.
    def secret_names(self, host):
        status, out, err = self.run(host, ["secrets", "list"], 30)
        if status != 0:
            raise self.failed(host, ["secrets", "list"], status, out, err)
        found, lines = [], out.splitlines()
        if not lines or lines[0].split() != ["NAME", "STORE"]:
            return found  # "no secrets" (the command's table has its header first)
        for line in lines[1:]:
            parts = line.split()
            if len(parts) == 2 and NET_REF.fullmatch(parts[0]):
                found.append({"name": parts[0], "store": printable(parts[1])[:32]})
        return found

    def secrets(self, params, body):
        return self.secret_names(params["host"])

    def secret_set(self, params, body):
        name, value = secret_name(params["name"]), body.get("secret")
        if not (isinstance(value, str) and 0 < len(value) <= 8192 and not any(ord(c) < 32 or ord(c) == 127
                                                                               for c in value)):
            raise Refused(400, "the value is one line of 1-8192 characters")
        if body.get("confirm") is not True and any(entry["name"] == name for entry in
                                                   self.secret_names(params["host"])):
            confirmed(body, "secret-replace", name)
        return self.done(params["host"], ["secrets", "set", name], 60, input=value + "\n")

    def secret_delete(self, params, body):
        name = secret_name(params["name"])
        confirmed(body, "secret-delete", name)
        return self.done(params["host"], ["secrets", "delete", name], 30)

    def secret_rename(self, params, body):
        """sandbox-ai secrets rename OLD NEW (asked first: secret-rename): another name for the secret, carried
        through what that host's sandbox-ai keeps (logins, identities, VPN profiles, trusted project files, kept
        starts); what still names the old one is in the answer. The value is never read here."""
        old, new = secret_name(params["name"]), secret_name(body.get("new"))
        if old == new:
            raise Refused(400, "new is another name")
        confirmed(body, "secret-rename", f"{old} to {new}")
        return self.done(params["host"], ["secrets", "rename", old, new], 120)

    def secret_send(self, params, body):
        """sandbox-ai secrets send NAME TARGET --yes [--as=NAME] [--replace] on that host (asked first: secret-send;
        with replace: secret-replace too): its secret NAME kept on TARGET, another host of its `sandbox-ai hosts`, as
        well - the value goes from that host's store over SSH on standard input, never through this service."""
        name = secret_name(params["name"])
        target = host_name(body.get("host"))
        if target == "local":
            raise Refused(400, "host is another host of sandbox-ai hosts")
        args = ["secrets", "send", name, target, "--yes"]
        if body.get("as") not in (None, ""):
            args.append(f"--as={secret_name(body.get('as'))}")
        confirmed(body, "secret-send", f"{name} to {target}")
        if flag(body, "replace"):
            confirmed(body, "secret-replace", f"{name} on {target}")
            args.append("--replace")
        return self.done(params["host"], args, 180)

    # The primary password of a host's private files (docs/SECRETS_DESIGN.md): what protects its secrets, and the
    # unlock step. The password is sent once, as {"password": TEXT} (a field the audit log leaves out), over this
    # service's HTTPS, and goes to `secrets unlock --password-stdin` there on its standard input (over SSH for
    # another host): never on a command line, in a response or in a log. Changing or removing it is the command
    # line's: it takes the current password and decrypts or rewrites everything.
    def secrets_protection(self, params, body):
        info = self.json_of(params["host"], ["secrets", "status", "--json"], 30, ok=(0,))
        if not isinstance(info, dict):
            raise Refused(502, "sandbox-ai secrets status: not understood")
        plain = info.get("plain")
        left = info.get("left")
        return {"backend": printable(str(info.get("backend")))[:32], "protection": printable(str(info.get("protection")))[:32],
                "password": info.get("password") is True, "locked": info.get("locked") if isinstance(
                    info.get("locked"), bool) else None,
                "left": left if isinstance(left, int) and not isinstance(left, bool) else None,
                "plain": len(plain) if isinstance(plain, list) else 0,
                "encrypted": info.get("encrypted") if isinstance(info.get("encrypted"), int) else 0,
                "kdf": printable(str(info["kdf"]))[:80] if info.get("kdf") else None,
                "plain_choice": info.get("plain_choice") is True}

    @staticmethod
    def primary_password(body, least=1):
        value = body.get("password")
        if not (isinstance(value, str) and least <= len(value) <= 1024 and not any(ord(c) < 32 or ord(c) == 127
                                                                                   for c in value)):
            raise Refused(400, f"the primary password is one line of {max(least, 1)}-1024 characters")
        return value

    def secrets_unlock(self, params, body):
        host, password = host_name(params["host"]), self.primary_password(body)
        args = ["secrets", "unlock", "--password-stdin"]
        if body.get("for") not in (None, ""):
            if not (isinstance(body["for"], str) and re.fullmatch(r"[1-9][0-9]{0,6}[smhd]", body["for"])):
                raise Refused(400, "for: a duration such as 30m, 8h or 2d")
            args += ["--for", body["for"]]
        wait = self.unlock_limiter.wait(host)
        if wait > 0:
            raise Refused(429, f"too many wrong primary passwords for {host}: wait {int(wait) + 1} s")
        status, out, err = self.run(host, args, 60, input=password + "\n")
        if status != 0 and "too many wrong passwords" in err:
            raise Refused(429, printable(err.strip()[-300:]))
        if status != 0 and "wrong primary password" in err:
            self.unlock_limiter.failed(host)
            raise Refused(403, f"wrong primary password for the secrets of {host}")
        if status != 0:
            raise self.failed(host, args, status, out, err)
        self.unlock_limiter.succeeded(host)
        return {"output": printable(out.strip()[-1000:])}

    def secrets_lock(self, params, body):
        return self.done(params["host"], ["secrets", "lock"], 30)

    def secrets_password(self, params, body):
        """A first primary password for the host's private files (it encrypts what is there, and unlocks)."""
        password = self.primary_password(body, least=8)
        return self.done(params["host"], ["secrets", "password", "set", "--password-stdin"], 120,
                         input=password + "\n")

    # kits (sandbox-ai kits): every change is that command on the host, so its checks apply (a skill folder is a
    # real folder with SKILL.md, nothing followed, limits, a note when it lies where sandboxes can write; built-in
    # kits read-only); the values come after "--", never read as options
    def kits(self, params, body):
        return self.json_of(params["host"], ["kits", "list", "--json"], 60)

    def marketplaces(self, params, body):
        """Every plugin marketplace on that host (sandbox-ai plugins marketplaces --json): sandbox-ai's own, the
        kits', the plugin store's and those Claude, Codex and OpenCode know there, with each plugin's state. Read
        only: nothing is fetched or run there."""
        return self.json_of(params["host"], ["plugins", "marketplaces", "--json"], 60, ok=(0,))

    def kit_new(self, params, body):
        return self.done(params["host"], ["kits", "new", *kit_agents(body), "--", kit_name(body.get("name"))], 60)

    def kit_change(self, params, body):
        """add-skill {path, as?, agents?}, new-skill {skill, description?, agents?}, add-plugin {plugin, agent?,
        source?}, add-marketplace {marketplace, source, agent?}, remove-skill {skill, agents?}, remove-plugin
        {plugin, agent?}; with "create": true the kit is created first (sandbox-ai kits new), so what is added to
        a built-in kit's selection lands in a new kit of yours. enable-plugin, disable-plugin {plugin, agent?},
        remove-marketplace {marketplace, with_plugins?}, import-marketplace {marketplace, from?: claude|codex,
        with_plugins?}. The plugin and marketplace actions also take the kit "default" (the default kit, part of
        every sandbox)."""
        action = params["action"]
        if action not in KIT_ACTIONS:
            raise Refused(404, "no such action")
        name = params["name"] if params["name"] == "default" and action in KIT_DEFAULT_ACTIONS \
            else kit_name(params["name"])
        options, values = [], []
        if action == "add-skill":
            values = [host_path(body.get("path"), "path")]
            if body.get("as") not in (None, ""):
                options.append(f"--as={kit_name(body['as'], 'as')}")
            options += kit_agents(body)
        elif action in ("new-skill", "remove-skill"):
            skill = body.get("skill")
            pattern = KIT_SKILL if action == "new-skill" else KIT_NAME
            if not (isinstance(skill, str) and pattern.fullmatch(skill)):
                raise Refused(400, "a skill's name is 1-64 lower-case letters, digits and '-'" if action == "new-skill"
                              else "no such skill")
            values = [skill]
            options += kit_agents(body)
            if action == "new-skill" and body.get("description") not in (None, ""):
                options.append(f"--description={one_line(body['description'], 'description')}")
        elif action in ("add-plugin", "remove-plugin", "enable-plugin", "disable-plugin"):
            plugin = body.get("plugin")
            if not (isinstance(plugin, str) and KIT_PLUGIN.fullmatch(plugin)):
                raise Refused(400, "a plugin is NAME@MARKETPLACE (Claude) or an npm package (OpenCode)")
            values = [plugin]
            options += kit_agents(body, many=False)
            if action == "add-plugin" and body.get("source") not in (None, ""):
                options.append(f"--source={one_line(body['source'], 'source', 2048)}")
        elif action == "add-hook":  # a hook script, a hooks.json or an OpenCode plugin module (a file on that host)
            values = [clean_path(body.get("file"), "file")]
            options += kit_agents(body)
            if body.get("as") not in (None, ""):
                if not (isinstance(body["as"], str) and HOOK_FILE.fullmatch(body["as"])):
                    raise Refused(400, "as is a file name: letters, digits, '_', '.' or '-'")
                options.append(f"--as={body['as']}")
            if body.get("event") not in (None, ""):
                if not (isinstance(body["event"], str) and HOOK_EVENT.fullmatch(body["event"])):
                    raise Refused(400, "event is a hook event: PreToolUse, PostToolUse, Stop, ...")
                options.append(f"--event={body['event']}")
            if body.get("matcher") not in (None, ""):
                options.append(f"--matcher={one_line(body['matcher'], 'matcher', 200)}")
            if body.get("timeout") not in (None, ""):
                options.append(f"--timeout={whole(body['timeout'], 'timeout', 1, 3600)}")
        elif action == "remove-hook":
            hook = body.get("file")
            if not (isinstance(hook, str) and HOOK_FILE.fullmatch(hook)):
                raise Refused(400, "file is the hook's file name in the kit")
            values = [hook]
            options += kit_agents(body)
        elif action in ("remove-marketplace", "import-marketplace"):
            values = [kit_name(body.get("marketplace"), "a marketplace's name")]
            if body.get("with_plugins") not in (None, False, True):
                raise Refused(400, "with_plugins is true or false")
            if body.get("with_plugins") is True:
                options.append("--with-plugins")
            if action == "import-marketplace" and body.get("from") not in (None, ""):
                if body["from"] not in ("claude", "codex"):
                    raise Refused(400, "from is claude or codex (OpenCode has no marketplaces)")
                options.append(f"--from={body['from']}")
        else:  # add-marketplace
            values = [kit_name(body.get("marketplace"), "a marketplace's name"),
                      one_line(body.get("source"), "source", 2048)]
            options += kit_agents(body, many=False)
        if body.get("create") not in (None, False, True):
            raise Refused(400, "create is true or false")
        if body.get("create") is True and name == "default":
            raise Refused(400, "the default kit is never created here (a start, or its first change, creates it)")
        if action in KIT_NARROWS:
            confirmed(body, "kit-remove", f"{values[0]} from {name}")
        elif action in KIT_WIDENS:
            confirmed(body, "kit-add", f"{' '.join(values[:1])} to {name}")
        first = ""
        if body.get("create") is True:
            status, out, err = self.run(params["host"], ["kits", "list", "--json"], 60)
            try:
                existing = {kit.get("name") for kit in json.loads(out).get("kits", []) if kit.get("kind") == "user"}
            except (ValueError, AttributeError, RecursionError):
                existing = set()
            if name not in existing:
                first = self.done(params["host"], ["kits", "new", "--", name], 60)["output"] + "\n"
        result = self.done(params["host"], ["kits", action, *options, "--", name, *values], 300)
        return {"output": first + result["output"]}

    # what the agents on that host have set up themselves (sandbox-ai kits host-list, host-show: read only), and the
    # parts chosen copied into a kit or a project's memory there (host-import, with its checks: nothing followed out of
    # an agent's folder, secret-looking lines refused unless allow_secrets, hooks never imported)
    def agent_parts(self, params, body):
        return self.json_of(params["host"], ["kits", "host-list", "--json"], 150, ok=(0,))

    def agent_part(self, params, body):
        return self.json_of(params["host"], ["kits", "host-show", "--json", "--", agent_part_id(params["id"])], 150,
                            ok=(0,))

    def agent_parts_import(self, params, body):
        """{parts: [ID...], kit: NAME, new?: bool} or {parts, project: DIR}; agents?: [AGENT...], allow_secrets?: bool,
        allow_findings?: bool (the hidden-text checker's). Only the parts named are imported."""
        parts = strings(body.get("parts"), "parts", 200)
        if not parts:
            raise Refused(400, "parts: the ids of the parts chosen (nothing is imported without a choice)")
        parts = [agent_part_id(part) for part in parts]
        options = []
        if (body.get("kit") in (None, "")) == (body.get("project") in (None, "")):
            raise Refused(400, "give kit (a kit of yours) or project (a project's folder on that host)")
        for key in ("new", "allow_secrets", "allow_findings"):
            if body.get(key) not in (None, False, True):
                raise Refused(400, f"{key} is true or false")
        if body.get("kit") not in (None, ""):
            kit = kit_name(body.get("kit"))
            confirmed(body, "kit-add", f"{', '.join(parts[:3])} to {kit}")
            options += [f"--kit={kit}", *(["--new"] if body.get("new") is True else [])]
        else:
            project = clean_path(body.get("project"), "project")
            confirmed(body, "memory-add", f"{', '.join(parts[:3])} to the memory of {project}")
            options.append(f"--project={project}")
        agents = strings(body.get("agents", []), "agents", len(AGENTS))
        if not all(agent in AGENTS for agent in agents):
            raise Refused(400, "an " + agent_words())
        options += [f"--agent={agent}" for agent in dict.fromkeys(agents)]
        if body.get("allow_secrets") is True:
            options.append("--allow-secrets")
        if body.get("allow_findings") is True:
            options.append("--allow-findings")
        return self.done(params["host"], ["kits", "host-import", *options, "--", *parts], 300)

    def kit_delete(self, params, body):
        name = kit_name(params["name"])
        confirmed(body, "kit-delete", name)
        return self.done(params["host"], ["kits", "delete", "--yes", "--", name], 60)

    # approvals
    @staticmethod
    def watching(names):
        """A page asks for the questions of NAMES (every 5 s): when this machine is one of them, the service keeps
        the heartbeat of a watcher meanwhile (approvals.heartbeat), so the launcher leaves its questions to the
        page (one prompt per question); it goes stale 15 s after the last page stopped asking."""
        if module("hosts").LOCAL in names:
            with contextlib.suppress(OSError):  # a state directory that cannot be written: the launcher asks too
                module("approvals").heartbeat("web")

    def approvals(self, params, body):
        self.watching([params["host"]])
        return self.json_of(params["host"], ["approvals", "list", "--json"], 30)

    def host_questions(self, name, state="open"):
        """NAME's questions in STATE (open; "all": also those timed out, allowed and refused - a host whose sandbox-ai
        is older than 0.1.98 lists its open ones only); a browser question of a host whose sandbox-ai is known to be
        older than 0.1.78 carries "host_too_old" (its version): it cannot be allowed without opening a browser there
        (--no-open), so the page says to update it instead of opening the link here first."""
        args = ["approvals", "list", "--json"]
        if state != "open" and not newer_needed(name, ["approvals", "list", "--state", state]):
            args += ["--state", state]
        listed = self.json_of(name, args, APPROVALS_HOST_SECONDS)
        if not isinstance(listed, list):
            raise Refused(502, f"sandbox-ai approvals on {name}: not a list of questions")
        older = newer_needed(name, ["approvals", "allow", "-", "--no-open"])
        questions = [question for question in listed if isinstance(question, dict)]
        for question in questions:
            if older and question.get("kind") == "browser":
                question["host_too_old"] = older.params["version"]
        return questions

    def all_approvals(self, params, body):
        """The open questions of every host (this machine and each of `sandbox-ai hosts`), asked at once:
        {"questions": [{"host", ...the question}], "errors": {host: why}, "age": {host: seconds}, "seen": {host:
        Unix time}}. A host that does not answer within APPROVALS_HOST_SECONDS is named in errors; the others are
        listed all the same. At most one request is in flight per host: one still asking is given with its last
        list (at most FanOut.KEEP seconds old). "age" is how long ago each host's list was read: its seconds_left
        were counted then (the page subtracts the age, so a slow host does not make the others' countdowns late).
        A damaged hosts.json is named in errors ("hosts.json"); this machine is asked all the same."""
        names, errors = host_names()
        self.watching(names)
        found = self.approvals_fanout.gather(names, APPROVALS_HOST_SECONDS)
        questions, age, seen = [], {}, {}
        for name in names:
            info = found[name]
            if "error" in info:
                errors[name] = info["error"]
                continue
            questions += [{**question, "host": name} for question in info["result"]]
            age[name] = round(info["age"], 3)
            seen[name] = round(info["seen"], 3)
        return {"questions": questions, "errors": errors, "age": age, "seen": seen}

    def all_approvals_states(self, params, body):
        """The questions of every host in every state (the Approvals page's tabs), as all_approvals gives the open ones."""
        names, errors = host_names()
        self.watching(names)
        found = self.approvals_all_fanout.gather(names, APPROVALS_HOST_SECONDS)
        questions, age, seen = [], {}, {}
        for name in names:
            info = found[name]
            if "error" in info:
                errors[name] = info["error"]
                continue
            questions += [{**question, "host": name} for question in info["result"]]
            age[name] = round(info["age"], 3)
            seen[name] = round(info["seen"], 3)
        return {"questions": questions, "errors": errors, "age": age, "seen": seen}

    def ask_again(self, params, body):
        """sandbox-ai approvals ask-again ID: what the run decided about question ID is forgotten, its next request
        asked anew (one still open says so)."""
        ident = params["id"]
        if not APPROVAL_ID.fullmatch(ident):
            raise Refused(400, "no such question")
        self.approvals_all_fanout.forget(params["host"])
        return self.done(params["host"], ["approvals", "ask-again", ident], 60)

    def answer(self, params, body):
        """allow or deny a question with a scope - an open one, one nobody answered in time (still open) or one
        answered already (the run's decision changed). Answered at once, as clicked: no confirmation, not even for
        "always" (the page offers to undo it); "no_open": true allows a browser question without opening the browser
        on that host (the user opened its link)."""
        ident = params["id"]
        if not APPROVAL_ID.fullmatch(ident):
            raise Refused(400, "no such question")
        decision, scope, no_open = body.get("decision"), body.get("scope"), body.get("no_open", False)
        if decision not in ("allow", "deny"):
            raise Refused(400, "decision is allow or deny")
        if scope not in (None, "once", "run", "always"):
            raise Refused(400, "scope is once, run or always")
        if not isinstance(no_open, bool) or no_open and decision != "allow":
            raise Refused(400, "no_open is true or false, and only for an allow")
        callback = self.login_callback(params["host"], ident) if decision == "allow" and no_open else None
        self.approvals_all_fanout.forget(params["host"])  # (the page reads that host's list anew)
        # An "always" waits up to approvals.VERIFY_WAIT for the sandbox before it is remembered; the call (over ssh,
        # perhaps, with a name to resolve) must outlast that clearly, or the "always" is lost.
        result = self.done(params["host"], ["approvals", decision, ident, *(["--scope", scope] if scope else []),
                                            *(["--no-open"] if no_open else [])],
                           module("approvals").VERIFY_WAIT + 60)
        if callback:
            note = self.relay_callback(params["host"], ident, callback)
            result["output"] = "\n".join(part for part in (result.get("output"), note) if part)
        # Allowed, but its name does not exist: the question is closed (denied for the run) - the page says so in
        # one line, not as "allowed" (approvals.close_missing).
        closed = module("approvals").closed_note(result.get("output")) if decision == "allow" else None
        if closed:
            result["closed"] = printable(closed[:600])
        return result

    def login_callback(self, host, ident):
        """The URL of browser question IDENT of HOST when it sends the browser back to the sandbox's loopback (its
        "callback": approvals list --json), else None: from the page's last read of HOST's questions, else - for a
        browser on this machine, where the catcher can start - read now (before the answer: then it is listed no
        more)."""
        cached = self.approvals_fanout.cached(host_name(host))
        listed = cached.get("result") if cached else None
        if not any(isinstance(q, dict) and q.get("id") == ident for q in listed or []):
            if not loopback_address(getattr(REQUEST, "address", None)):
                return None
            try:
                listed = self.json_of(host, ["approvals", "list", "--json"], APPROVALS_HOST_SECONDS)
            except Refused:
                return None
        for question in listed if isinstance(listed, list) else []:
            if isinstance(question, dict) and question.get("id") == ident and question.get("kind") == "browser" \
                    and isinstance(question.get("callback"), dict) and isinstance(question.get("url"), str):
                return question["url"]
        return None

    def relay_callback(self, host, ident, url):
        """After "Open" of a browser question whose login returns to the sandbox's loopback: where the browser runs
        on this machine (the page's client is this machine's loopback), the one-shot catcher starts here
        (`sandbox-ai approvals relay`, forwarding to HOST's sandbox, over SSH for another host); else what to do."""
        if not loopback_address(getattr(REQUEST, "address", None)):
            return ("Its login's callback goes to the sandbox's own loopback, which your browser (on another machine "
                    "than this web interface) cannot reach: use the agent's device-code login (Codex: codex login "
                    "--device-auth).")
        hosts = module("hosts")
        status, out, err = self.run(hosts.LOCAL, ["approvals", "relay", ident, "--url", url,
                                                  *([] if host == hosts.LOCAL else ["--host", host])], 30)
        return printable((out or err).strip()[-600:]) or f"the callback is not relayed (exit status {status})"

    def approvals_history(self, params, body):
        """sandbox-ai approvals history [RUN]: the questions with their answers (the command's table)."""
        run = params.get("run")
        if run is not None and not CANARY_RUN.fullmatch(run):
            raise Refused(400, "a run is 1-81 letters, digits, '_', '.' or '-'")
        args = ["approvals", "history", *([run] if run else [])]
        status, out, err = self.run(params["host"], args, 60)
        if status != 0:
            raise self.failed(params["host"], args, status, out, err)
        return {"output": printable(out.strip()[-20000:])}  # the newest answers last: those are kept

    def remembered(self, params, body):
        """sandbox-ai approvals remembered --json: {project: {"hosts", "tcp", "blocked_hosts", "blocked_tcp",
        "details"}}."""
        found = self.json_of(params["host"], ["approvals", "remembered", "--json"], 30)
        if not isinstance(found, dict):
            raise Refused(502, "sandbox-ai approvals remembered: not an object")
        return found

    def forget(self, params, body):
        """sandbox-ai approvals forget PROJECT ENTRY: one remembered answer forgotten (asked first)."""
        project = no_dots(host_path(body.get("project"), "project"), "project")
        entry = body.get("entry")
        if not (isinstance(entry, str) and REMEMBERED_ENTRY.fullmatch(entry)):
            raise Refused(400, "entry is a host name or IP:PORT, as remembered lists it")
        confirmed(body, "approval-forget", f"{entry} for {project}")
        return self.done(params["host"], ["approvals", "forget", project, entry], 30)

    # the doctor and the logs
    def doctor(self, params, body):
        backend = params.get("backend")
        if backend is not None and backend not in DOCTOR_BACKENDS:
            raise Refused(400, f"the backend is {', '.join(DOCTOR_BACKENDS)}")
        return self.doctor_of(params["host"], 120, [f"--backend={backend}"] if backend else [])

    def doctor_of(self, host, timeout, extra=()):
        """`sandbox-ai doctor --json` on HOST: its list of checks; from an older sandbox-ai that answers with text
        (or has no --json yet: then its plain `doctor`), that text as one string, which the page shows as it is.
        EXTRA: --backend=BACKEND (doctor/BACKEND)."""
        status, out, err = self.run(host, ["doctor", "--json", *extra], timeout)
        try:
            return json.loads(out)
        except (ValueError, RecursionError):
            pass
        if status != 0 and not out.strip() and "--json" in (err or ""):  # argparse: unrecognized arguments: --json
            status, out, err = self.run(host, ["doctor", *extra], timeout)
        text = printable(out.rstrip()[-20000:]) if out.strip() and status in (0, 1) else ""
        if not text:
            raise self.failed(host, ["doctor"], status, out, err)
        return text

    # LLM servers (sandbox-ai llm)
    def llm(self, params, body):
        return self.json_of(params["host"], ["llm", "list", "--json"], 30)

    def llm_models_of_url(self, params, body):
        """sandbox-ai wizard models --kind KIND --url URL [--ca FILE | --ca-of NAME] [--api-key-secret NAME] --json:
        what a server at that URL lists, before anything is registered (the page's "Test connection"; CA_OF: the CA
        an edited server keeps, by its name). Nothing is stored on the
        host; the key is named, never sent (the command reads that secret there), and no header secret goes with it,
        so a server that wants one of those answers as it answers an anonymous caller. The command refuses to send the
        key over plain http to a public address, as `llm add` does, unless insecure_http (confirmed as llm-insecure)."""
        kind, url = llm_kind(body.get("kind")), body.get("url")
        if not (isinstance(url, str) and LLM_URL.fullmatch(url)):
            raise Refused(400, "url is http(s)://HOST[:PORT][/PREFIX]")
        args = ["wizard", "models", f"--kind={kind}", f"--url={url}"]
        if body.get("ca") not in (None, ""):
            args.append(f"--ca={clean_path(body['ca'], 'ca')}")
        elif body.get("ca_of") not in (None, ""):
            # an edited server's own CA, which the form cannot send again: named, read on that host
            if not (isinstance(body["ca_of"], str) and MCP_NAME.fullmatch(body["ca_of"])):
                raise Refused(400, "ca_of is the name of an LLM server (1-32 letters, digits, '_' or '-')")
            args.append(f"--ca-of={body['ca_of']}")
        if body.get("api_key_secret") not in (None, ""):
            args.append(f"--api-key-secret={secret_name(body['api_key_secret'])}")
            if flag(body, "insecure_http"):
                confirmed(body, "llm-insecure", printable(url)[:200])
                args.append("--insecure-http")
        if flag(body, "status"):  # what it has loaded and uses now too: {"models": [...], "status": {...}}
            args.append("--status")
        return self.json_of(params["host"], [*args, "--json"], 60)

    def add_llm(self, params, body):
        name, kind, url, model = (body.get(k) for k in ("name", "kind", "url", "model"))
        if not all(isinstance(v, str) and v and len(v) <= 2048 and not v.startswith("-") for v in (name, kind, url)):
            raise Refused(400, "name, kind and url are needed")
        args = ["llm", "add", name, "--kind", kind, "--url", url]
        if isinstance(model, str) and model:
            if model.startswith("-"):
                raise Refused(400, "a model does not start with '-'")
            args += ["--model", model]
        key = body.get("api_key_secret")
        if key not in (None, ""):  # the NAME of the secret (sandbox-ai secrets there) that holds the key, never a key
            args.append(f"--api-key-secret={secret_name(key)}")
        headers = body.get("header_secrets", [])
        if not isinstance(headers, list) or len(headers) > 8:
            raise Refused(400, "header_secrets is a list of {header, secret}")
        for entry in headers:  # a header the proxy adds with a secret's value: the secret's NAME, never the value
            if not (isinstance(entry, dict) and set(entry) <= {"header", "secret"} and isinstance(entry.get("header"), str)
                    and HEADER_NAME.fullmatch(entry["header"])):
                raise Refused(400, "a header secret is {header: NAME, secret: SECRET}")
            args.append(f"--header-secret={entry['header']}={secret_name(entry.get('secret'))}")
        if body.get("ca") not in (None, ""):
            args.append(f"--ca={clean_path(body['ca'], 'ca')}")
        if body.get("vpn") not in (None, ""):
            args.append(f"--vpn={vpn_name(body['vpn'])}")
        if flag(body, "anthropic"):
            args.append("--anthropic")
        memory = body.get("memory_gb")
        if memory not in (None, ""):  # the server's memory for what fits it (0: forgotten)
            if isinstance(memory, bool) or not isinstance(memory, (int, float)) or not (memory == 0 or
                                                                                        1 <= memory <= 65536):
                raise Refused(400, "memory_gb is the server's memory in GB, 1 to 65536 (0 forgets it)")
            args.append(f"--memory={memory:g}")
        if flag(body, "replace"):
            confirmed(body, "llm-replace", printable(name)[:64])
            args.append("--replace")
        if flag(body, "insecure_http"):
            confirmed(body, "llm-insecure", f"{name} at {printable(url)[:200]}")
            args.append("--insecure-http")
        return self.done(params["host"], args, 60)

    def llm_status(self, params, body):
        """sandbox-ai llm status [NAME] --json on the host: what each server (or NAME) has loaded and uses now, as its
        API reports it. It says 1 when a server did not answer and prints the JSON all the same: that JSON counts."""
        name = params.get("name")
        if name is not None and (name.startswith("-") or not MCP_NAME.fullmatch(name)):
            raise Refused(400, "no such server")
        return self.json_of(params["host"], ["llm", "status", *([name] if name else []), "--json"], 60)

    def llm_recommend(self, params, body):
        """sandbox-ai llm recommend --json on the host: per use case of the local-model research its best tested
        choice from the models of that host's servers (what is loaded, what fits), else the nearest, with why; every
        server asked once (a few seconds each)."""
        return self.json_of(params["host"], ["llm", "recommend", "--json"], 120, ok=(0,))

    def llm_models(self, params, body):
        if params["name"].startswith("-"):
            raise Refused(400, "no such server")
        return self.json_of(params["host"], ["llm", "models", params["name"], "--json"], 60)

    def check_llm(self, params, body):
        name = params["name"]
        agent = body.get("agent")
        if agent not in (None, *AGENTS) or name.startswith("-"):
            raise Refused(400, agent_words())
        status, out, err = self.run(params["host"], ["llm", "check", name, *(["--agent", agent] if agent else [])],
                                    90)
        return {"ok": status == 0, "output": printable((out + err).strip()[-4000:])}

    def share_llm(self, params, body):
        """For all hosts: {"shared": true, "key_to": [HOST]} runs `llm share NAME --key-to HOST,...` (none: --no-key)
        on the host - the entry copied to every host of its sandbox-ai hosts, the key only to KEY_TO -, {"shared":
        false} `llm unshare NAME` (llm-unshare, confirmed first). A host that gets the key and did not have it from
        this share before (llm show --json says whom it goes to now; not read for a confirmed request) is confirmed
        first (llm-share-key). Answers
        {"ok", "output"}: a host that did not answer is said in the output, the share stands."""
        name = params["name"]
        if name.startswith("-") or not MCP_NAME.fullmatch(name):
            raise Refused(400, "no such server")
        shared = body.get("shared")
        if not isinstance(shared, bool):
            raise Refused(400, "shared is true or false")
        if not shared:
            confirmed(body, "llm-unshare", printable(name)[:64])
            args = ["llm", "unshare", name]
        else:
            hosts = module("hosts")
            key_to = body.get("key_to", [])
            if not isinstance(key_to, list) or len(key_to) > 64 or any(
                    not (isinstance(h, str) and hosts.NAME.fullmatch(h)) or h == hosts.LOCAL for h in key_to):
                raise Refused(400, "key_to is a list of hosts of that host's sandbox-ai hosts")
            key_to = sorted(set(key_to))
            before = []
            if key_to and body.get("confirm") is not True:  # (confirmed: whoever gets it now was asked about)
                status, out, _ = self.run(params["host"], ["llm", "show", name, "--json"], 30)
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    before = list((json.loads(out).get("shared") or {}).get("key_to") or []) if status == 0 else []
            new = [h for h in key_to if h not in before]
            if new:
                confirmed(body, "llm-share-key", f"{printable(name)[:64]} to {', '.join(new)}")
            args = ["llm", "share", name, *(["--key-to", ",".join(key_to)] if key_to else ["--no-key"])]
        status, out, err = self.run(params["host"], args, 600)
        if status != 0 and not out.strip():
            raise self.failed(params["host"], args, status, out, err)
        return {"ok": status == 0, "output": printable((out + err).strip()[-4000:])}

    def remove_llm(self, params, body):
        if params["name"].startswith("-"):
            raise Refused(400, "no such server")
        confirmed(body, "llm-remove", printable(params["name"])[:64])
        return self.done(params["host"], ["llm", "remove", params["name"]], 30)

    # sandbox-ai's defaults (defaults.json) on the host: the agents' telemetry, the mode, canaries
    def defaults(self, params, body):
        return self.json_of(params["host"], ["defaults", "show", "--json"], 30)

    def set_default(self, params, body):
        """defaults set KEY VALUE (as JSON) or unset KEY (null), for the keys the launcher knows (DEFAULT_KEYS; any
        other is refused). Any step down in protection from what `defaults show` says on that host now (read first,
        unless the request is confirmed) needs {"confirm": true}: the mode (MODE_ORDER), the agents' telemetry
        (TELEMETRY_ORDER), fewer canary categories (false, [] or ["none"]: no canary files; the categories are the
        host's own), a keep_days lower than now or one where there was none (kept starts are removed sooner), more
        preset directories (they change what a start reads), another subnet. Unset is a change to sandbox-ai's own
        default, compared the same way."""
        key, value = body.get("key"), body.get("value")
        if key not in DEFAULT_KEYS:
            raise Refused(400, f"no such key: the keys are {', '.join(DEFAULT_KEYS)}")
        if key == "canaries" and value is not None and not (
                value is False or isinstance(value, str)
                or isinstance(value, list) and all(isinstance(item, str) for item in value)):
            raise Refused(400, "canaries is false, a list of categories, all or none")
        if key == "telemetry" and value is not None and value not in TELEMETRY_ORDER:
            raise Refused(400, "telemetry is off or strict: the agents' telemetry is always off (there is no on)")
        if key == "hide_secrets" and value is not None and not isinstance(value, bool):
            raise Refused(400, "hide_secrets is true or false (true: logins and keys are left out of the file views)")
        if key == "agent_intro" and value is not None and not isinstance(value, bool):
            raise Refused(400, "agent_intro is true or false (false: a start tells its agent nothing about what its "
                               "sandbox has)")
        if key == "name_style" and value is not None and value not in NAME_STYLES:
            raise Refused(400, f"name_style is {' or '.join(NAME_STYLES)}")
        if key == "desktop_notifications" and value is not None and value not in DESKTOP_NOTIFICATIONS:
            raise Refused(400, f"desktop_notifications is {', '.join(DESKTOP_NOTIFICATIONS)}")
        if key == "llm_traffic" and value is not None and value not in LLM_TRAFFIC_MODES:
            raise Refused(400, f"llm_traffic is {', '.join(LLM_TRAFFIC_MODES)}")
        if key == "llm_traffic_limit" and value is not None and not (
                isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 64):
            raise Refused(400, "llm_traffic_limit is a number of MiB from 1 to 64")
        if key == "budget" and value is not None and not (
                isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1_000_000):
            raise Refused(400, "budget is a number of USD from 0 to 1000000 (a start's --budget; sandbox-ai spend)")
        if key == "file_reads" and value is not None and value not in FILE_READS:
            raise Refused(400, f"file_reads is {', '.join(FILE_READS)}")
        if key == "connection_bodies" and value is not None and value not in CONNECTION_BODIES:
            raise Refused(400, f"connection_bodies is {' or '.join(CONNECTION_BODIES)}")
        if key == "fix_questions" and value is not None and value not in FIX_QUESTIONS:
            raise Refused(400, f"fix_questions is {' or '.join(FIX_QUESTIONS)}")
        if key == "ask_timeout" and value is not None and not (
                isinstance(value, int) and not isinstance(value, bool) and 5 <= value <= 3600):
            raise Refused(400, "ask_timeout is a number of seconds from 5 to 3600")
        if key == "loop_guard" and value is not None:
            # The launcher's own check (llm.check_loop): a value it refuses would stop every start on that host.
            try:
                module("llm").check_loop(value, "the default")
            except (ValueError, TypeError, AttributeError) as exc:
                raise Refused(400, str(exc) if "loop_guard" in str(exc) else f"loop_guard: {exc}") from None
        if key.startswith("checkpoint_") and value is not None:
            # The launcher's own check (checkpoints.check_setting): a value it refuses would stop every start there.
            try:
                module("checkpoints").check_setting(key, value, "the default")
            except ValueError as exc:
                raise Refused(400, str(exc)) from None
        if key == "checkpoint_start" and value is False:  # no checkpoint at a start: what an agent does stays
            confirmed(body, "defaults-lower", "checkpoint_start false: no checkpoint of the workspaces when a start "
                                              "begins (an agent's changes cannot be undone)")
        if key.startswith("storage_") and value is not None:
            # The launcher's own check (storage.check_value): a value it refuses would stop every start there.
            why = module("storage").check_value(key, value)
            if why:
                raise Refused(400, why)
        if key == "safeguard_mute" and value is not None and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)):
            raise Refused(400, "safeguard_mute is a list of kinds (sandbox-ai safeguards kinds)")
        # How starts are named, which safeguard warnings pop up and how a local model is kept from looping protect
        # nothing (the safeguards still refuse, and every record stays listed): no step down to confirm. Fewer desktop
        # notifications are one (lowered_default): "none" silences a canary's alarm while the app is closed.
        # (agent_intro: telling the agent what its sandbox has, or not, opens and closes nothing)
        # (hide_secrets: hiding logins and keys in the file views protects; listing them again, the default, shows a
        # file's name, size and date only - its text is asked for each time: nothing to confirm either way)
        # (the storage settings: when the check runs, what it says, and its rules - each opt-in, each action of a
        # rule logged and said: no protection lowered)
        if body.get("confirm") is not True and key not in ("name_style", "safeguard_mute", "loop_guard",
                                                           "hide_secrets", "agent_intro") \
                and not key.startswith("storage_"):
            weakest = {"mode": MODE_ORDER[0]}.get(key)
            if weakest is not None and value == weakest or key == "canaries" and no_canaries(value):
                confirmed(body, "defaults-lower", f"{key} {json.dumps(value)}")  # the weakest there is: not read first
            lowered = self.lowered_default(params["host"], key, value)
            if lowered:
                confirmed(body, "defaults-lower", lowered)
        if value is None:
            return self.done(params["host"], ["defaults", "unset", key], 30)
        return self.done(params["host"], ["defaults", "set", key, json.dumps(value)], 30)

    def lowered_default(self, host, key, value):
        """Words for how VALUE (None: the default again) lowers KEY from what that host has now, or None."""
        try:
            shown = self.json_of(host, ["defaults", "show", "--json"], 30)
        except Refused as exc:
            return f"{key}: the current value could not be read ({exc})"
        shown = shown if isinstance(shown, dict) else {}
        values = shown.get("values") if isinstance(shown.get("values"), dict) else {}
        if key == "canaries":
            canary = module("canary")
            listed = lambda v: isinstance(v, list) and all(isinstance(item, str) for item in v)  # noqa: E731
            categories = shown["canary_categories"] if listed(shown.get("canary_categories")) and \
                shown["canary_categories"] else list(canary.CATEGORIES)
            default = shown["canary_default"] if listed(shown.get("canary_default")) else \
                list(canary.DEFAULT_CATEGORIES)
            try:
                before = canary_set(values.get("canaries", default), categories)
            except Refused:
                before = set(categories)  # unreadable now: every category counts as planted
            after = canary_set(default if value is None else value, categories)
            fewer = sorted(before - after)
            if not fewer:
                return None
            return ("canaries: no canary files at all" if not after else
                    f"canaries: no {' and '.join(fewer)} canary files any more")
        before = values.get(key)
        if key == "keep_days":
            # Without keep_days the stopped kept starts unused for 90 days go (sandbox-ai gc and its daily pass:
            # starts.DEFAULT_KEEP_DAYS); 0: never. Removed sooner, or at all where they never were, lowers it.
            def effective(days):
                if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                    return 90
                return None if days == 0 else days
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or not 0 <= value <= 36500):
                raise Refused(400, "keep_days is a whole number of days (1-36500), or 0: kept starts never expire")
            after, now_ = effective(value), effective(before)
            if after is None or (now_ is not None and after >= now_):
                return None
            shown = f"keep_days {value}" if value is not None else "no keep_days (the default: 90 days)"
            return (f"{shown}: kept starts unused for {after} days are removed" + (" (they never are now)"
                    if now_ is None else f", sooner than now ({now_} days)"))
        if key == "package_min_age_days":
            # The package guard asks before a download of a version younger than this (runtime.PACKAGE_DEFAULTS: 14
            # days without the key; 0: no age check). Fewer days, or none, lowers it.
            def asked_below(days):
                return 14 if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650 else days
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or not 0 <= value <= 3650):
                raise Refused(400, "package_min_age_days is a whole number of days (0-3650); 0: no age check")
            after, now_ = asked_below(value), asked_below(before)
            if after >= now_:
                return None
            shown = f"package_min_age_days {value}" if value is not None else \
                "no package_min_age_days (the default: 14 days)"
            return (f"{shown}: the package guard " + ("no longer asks about young package versions at all"
                    if after == 0 else f"asks only about package versions younger than {after} days")
                    + f" (now: {now_} days)")
        if key == "log_days":
            # Without log_days the logs older than 30 days go (sandbox-ai gc and its daily pass: statedirs.LOG_DAYS);
            # 0: never. Removed sooner, or at all where they never were, lowers it.
            def kept_for(days):
                if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                    return 30
                return None if days == 0 else days
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or not 0 <= value <= 36500):
                raise Refused(400, "log_days is a whole number of days (1-36500), or 0: logs are never removed by age")
            after, now_ = kept_for(value), kept_for(before)
            if after is None or (now_ is not None and after >= now_):
                return None
            shown = f"log_days {value}" if value is not None else "no log_days (the default: 30 days)"
            return (f"{shown}: logs older than {after} days are removed" + (" (they never are now)"
                    if now_ is None else f", sooner than now ({now_} days)"))
        if key == "preset_dirs":
            if value is None:
                return None  # fewer places presets are read from
            if not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
                raise Refused(400, "preset_dirs is a list of directories")
            kept = set(before) if isinstance(before, list) else set()
            added = [item for item in value if item not in kept]
            if added:
                return f"preset_dirs: later starts read presets from {', '.join(added)} too"
            return None
        if key == "desktop_notifications":
            # all > alarms (only a canary's alarm) > none (not even that), as the launchers read it (approvals.shows)
            now = before if before in DESKTOP_NOTIFICATIONS else "all"
            after = "all" if value is None else value
            if DESKTOP_NOTIFICATIONS.index(after) <= DESKTOP_NOTIFICATIONS.index(now):
                return None
            return (f"desktop_notifications {after} instead of {now}: " + (
                "the launchers show nothing on that host's desktop while its app is closed, not even a canary's "
                "alarm" if after == "none" else "only a canary's alarm is shown on that host's desktop while its "
                "app is closed, no question or safeguard"))
        if key in ("file_reads", "connection_bodies", "ask_timeout", "fix_questions"):
            # what the logs keep of reads and downloads, how what a start lacks is asked about (asked either way):
            return None  # no protection either way (all checked above)
        if key.startswith("checkpoint_"):
            return None  # the checkpoints' timing and limits (checked above; turning them off is asked above)
        if key in ("subnet", "subnet6"):
            if value == before:
                return None
            return f"{key}: the network of every later start changes ({json.dumps(before)} -> {json.dumps(value)})"
        order = MODE_ORDER if key == "mode" else TELEMETRY_ORDER
        default = DEFAULT_MODE if key == "mode" else DEFAULT_TELEMETRY
        before, after = values.get(key, default), default if value is None else value
        if key == "telemetry" and before == "on":  # gone since 0.1.95: the launcher ignores it, so it is off
            before = "off"
        if after not in order:
            raise Refused(400, f"{key} is one of {', '.join(order)}")
        if before not in order:
            return f"{key} {after}: the current value is not one of {', '.join(order)}"
        if order.index(after) < order.index(before):
            return f"{key} {after} instead of {before}"
        return None


    # --- the parity actions: every one is the CLI command of the same name on that host --------------------------

    def checked(self, host, args, timeout=60):
        """{"ok": exit status 0, "output"}: a check whose failure is its answer (a missing secret, a preset that
        lacks a value), not an error of the page."""
        status, out, err = self.run(host, args, timeout)
        return {"ok": status == 0, "output": printable((out + err).strip()[-8000:])}

    # project configurations (sandbox-ai wizard trust): trusted exactly as reviewed, by the digest of what was shown
    def projects(self, params, body):
        """sandbox-ai wizard trust --list --json: [{"path", "digest", "state": trusted|changed|missing|unreadable}]."""
        return self.json_of(params["host"], ["wizard", "trust", "--list", "--json"], 30)

    def project_review(self, params, body):
        """sandbox-ai wizard trust PATH --show --json: {"path", "digest", "document", "trusted", "problem"}; nothing
        is trusted. With "marks": what in the document weakens the sandbox (project_marks), for the page to mark."""
        path = clean_path(body.get("path"), "path")
        shown = self.json_of(params["host"], ["wizard", "trust", path, "--show", "--json"], 30)
        if isinstance(shown, dict) and isinstance(shown.get("document"), dict):
            shown["marks"] = self.project_marks(params["host"], shown["document"])
        return shown

    def project_marks(self, host, document):
        """What in the project file DOCUMENT weakens the sandbox, as the launcher judges it (modes.project_marks:
        options read as the launcher reads them, short forms too, every extra workspace and agent argument), measured
        against the security mode and canaries of HOST's defaults (sandbox-ai's own when that host does not say)."""
        try:
            data = self.json_of(host, ["defaults", "show", "--json"], 30)
        except Exception:  # noqa: BLE001 - not answered: measured against sandbox-ai's own defaults
            data = {}
        values = data.get("values") if isinstance(data, dict) and isinstance(data.get("values"), dict) else {}
        launcher = module("agent_sandbox")
        return launcher.MODES.project_marks(launcher, document, {k: values[k] for k in ("mode", "canaries")
                                                                 if k in values})

    def project_trust(self, params, body):
        """sandbox-ai wizard trust PATH --digest SHA256: trusted only if the file is exactly what was reviewed."""
        path, digest = clean_path(body.get("path"), "path"), body.get("digest")
        if not (isinstance(digest, str) and SHA256.fullmatch(digest)):
            raise Refused(400, "digest is the SHA-256 of what was reviewed (64 hex digits)")
        confirmed(body, "project-trust", path)
        return self.done(params["host"], ["wizard", "trust", path, f"--digest={digest}"], 30)

    def project_forget(self, params, body):
        path = clean_path(body.get("path"), "path")
        confirmed(body, "project-forget", path)
        return self.done(params["host"], ["wizard", "forget", path], 30)

    # MCP servers (sandbox-ai mcp)
    def mcp_list(self, params, body):
        """sandbox-ai mcp list --json: every defined server, running or not, with the starts it serves."""
        return self.json_of(params["host"], ["mcp", "list", "--json"], 60)

    @staticmethod
    def mcp_server(value):
        if not (isinstance(value, str) and MCP_NAME.fullmatch(value)):
            raise Refused(400, "an MCP server's name is 1-32 letters, digits, '_' or '-', starting with a letter or "
                               "digit")
        return value

    def mcp_start(self, params, body):
        """sandbox-ai mcp start NAME [--backend B] [--vpn PROFILE] [--for START [--restart] --json]: with a start it
        is attached to it as well (asked first: mcp-attach, or mcp-restart with restart)."""
        name = self.mcp_server(params["name"])
        args = ["mcp", "start"]
        backend = body.get("backend")
        if backend not in (None, "", "podman", "bwrap"):
            raise Refused(400, "backend is podman or bwrap")
        args += [f"--backend={backend}"] if backend else []
        if body.get("vpn") not in (None, ""):
            args.append(f"--vpn={vpn_name(body['vpn'])}")
        restart = flag(body, "restart")
        if body.get("for") not in (None, ""):
            start = start_name(body["for"])
            confirmed(body, "mcp-restart" if restart else "mcp-attach", f"{name} to {start}")
            return self.json_of(params["host"], [*args, f"--for={start}", "--json", *(["--restart"] if restart else []),
                                                 name], 300)
        if restart:
            raise Refused(400, "restart goes with for (a start)")
        return self.done(params["host"], [*args, name], 300)

    def mcp_copy(self, params, body):
        """sandbox-ai mcp copy NAME... --from HOST --to THIS --json, run on the service's machine (it reaches both
        hosts): {"dry_run": true} checks (what is copied, changed, and what this host cannot take as it is: nothing
        written), else asked first (mcp-copy). The choices that resolve what the check said: "as" (one server), "paths"
        ({OLD: NEW}), "drop_paths", "secrets_later", "pull_image" or "copy_image", "no_vpn", "current_pin",
        "unsigned", "replace", and "attach": a running start of this host to attach them to now."""
        target = host_name(params["host"])
        source = host_name(body.get("from"))
        if source == target:
            raise Refused(400, "copy from another host: from and to are the same")
        names = strings(body.get("names"), "names", 16)
        if not names or not all(MCP_NAME.fullmatch(name) for name in names):
            raise Refused(400, "names: the servers to copy (1-32 letters, digits, '_' or '-' each)")
        flags = ("dry_run", "secrets_later", "pull_image", "copy_image", "no_vpn", "current_pin", "unsigned", "replace")
        if not all(isinstance(body.get(key, False), bool) for key in flags):
            raise Refused(400, f"{', '.join(flags)} are true or false")
        if body.get("pull_image") and body.get("copy_image"):
            raise Refused(400, "pull_image or copy_image: one of them")
        args = ["mcp", "copy", *dict.fromkeys(names), "--from", source, "--to", target, "--json"]
        if body.get("as") not in (None, ""):
            if len(names) != 1 or not (isinstance(body["as"], str) and MCP_NAME.fullmatch(body["as"])):
                raise Refused(400, "as: a new name for the one server copied")
            args.append(f"--as={body['as']}")
        paths = body.get("paths", {})
        if not isinstance(paths, dict) or len(paths) > 16:
            raise Refused(400, "paths: {the source's folder: one of this host}")
        for old, new in paths.items():
            args.append(f"--path={no_dots(host_path(old, 'paths'), 'paths')}={no_dots(host_path(new, 'paths'), 'paths')}")
        for old in strings(body.get("drop_paths", []), "drop_paths", 16):
            args.append(f"--drop-path={no_dots(host_path(old, 'drop_paths'), 'drop_paths')}")
        for key, flag in (("secrets_later", "--secrets-later"), ("pull_image", "--pull-image"),
                          ("copy_image", "--copy-image"), ("no_vpn", "--no-vpn"), ("current_pin", "--current-pin"),
                          ("unsigned", "--unsigned")):
            if body.get(key):
                args.append(flag)
        if body.get("attach") not in (None, ""):
            args.append(f"--attach={start_name(body['attach'])}")
        if body.get("replace"):
            args += ["--replace", "--yes"]  # (asked first here: mcp-copy says so)
        if body.get("dry_run"):
            args.append("--dry-run")
        else:
            confirmed(body, "mcp-copy", f"{', '.join(names)} from {source} to {target}"
                      + (", replacing one of that name" if body.get("replace") else ""))
        return self.json_of(module("hosts").LOCAL, args, timeout=3600)

    def mcp_update(self, params, body):
        """sandbox-ai mcp update [NAME...] --json: {"check": true} says which servers have a newer image (nothing
        changed), else asked first (mcp-update) and done with --yes - the catalogs read again on that host, each
        signature checked there; "names" (none: every server of yours from a catalog), "unsigned" (a server added
        unsigned takes its unsigned catalog's image; the host's security mode still refuses that in high/paranoid)."""
        names = strings(body.get("names", []), "names", 64)
        if not all(MCP_NAME.fullmatch(name) for name in names):
            raise Refused(400, "names: MCP servers' names (1-32 letters, digits, '_' or '-' each)")
        for key in ("check", "unsigned"):
            if not isinstance(body.get(key, False), bool):
                raise Refused(400, f"{key} is true or false")
        args = ["mcp", "update", *dict.fromkeys(names), "--json", *(["--unsigned"] if body.get("unsigned") else [])]
        if body.get("check"):
            return self.json_of(params["host"], [*args, "--check"], 600, ok=(0,))
        confirmed(body, "mcp-update", ", ".join(names) or "every server of yours from a catalog")
        return self.json_of(params["host"], [*args, "--yes"], 3600, ok=(0,))

    def mcp_check(self, params, body):
        """sandbox-ai mcp check [NAME...|--all] --json: "names" (none: every defined server), "vpn" (a VPN or Tor
        profile in front of each server that reaches the network). [{"server", "result": "ok" | "failed" | "skipped",
        "tools", "seconds", "reason", "fix", "note", "log"}] - a failed server is a row, not a failure of the call."""
        names = strings(body.get("names", []), "names", 64)
        if not all(MCP_NAME.fullmatch(name) for name in names):
            raise Refused(400, "names: MCP servers' names (1-32 letters, digits, '_' or '-' each)")
        args = ["mcp", "check", *(dict.fromkeys(names) or ["--all"]), "--json"]
        if body.get("vpn") not in (None, ""):
            args.append(f"--vpn={vpn_name(body['vpn'])}")
        return self.json_of(params["host"], args, 3600, ok=(0, 1))

    def mcp_stop(self, params, body):
        name = self.mcp_server(params["name"])
        confirmed(body, "mcp-stop", name)
        return self.done(params["host"], ["mcp", "stop", name], 120)

    def mcp_catalog(self, params, body):
        """sandbox-ai mcp catalog --json [--catalog FILE_OR_URL]...: [{"entry", "prefix", "name", "network", "title",
        "published", "signature", "catalog", "env": [{name, description, secret, required}]}] - the variables each
        entry declares, for the add form; "problems": the catalogs it could not read. A host before 0.1.95 has no
        --json: its lines are read then (mcp_catalog_lines, without variables)."""
        sources = catalog_sources(body)
        try:
            status, out, err = self.run(params["host"], ["mcp", "catalog", "--json", *sources], 120)
        except Refused as exc:
            if exc.code != "host-too-old":
                raise
            return self.mcp_catalog_lines(params, body, sources)
        if status != 0:
            raise self.failed(params["host"], ["mcp", "catalog"], status, out, err)
        try:
            printed = json.loads(out)
        except (ValueError, RecursionError):
            raise self.failed(params["host"], ["mcp", "catalog"], status, out, err) from None
        printed = printed if isinstance(printed, dict) else {}
        entries = []
        for item in printed.get("entries") if isinstance(printed.get("entries"), list) else []:
            if not (isinstance(item, dict) and isinstance(item.get("prefix"), str) and isinstance(item.get("name"), str)
                    and MCP_ENTRY.fullmatch(f"{item['prefix']}:{item['name']}")):
                continue
            hosts = [str(host) for host in item.get("hosts") if isinstance(host, str)] \
                if isinstance(item.get("hosts"), list) else []
            # in the agents' words (offline, egress-all; 2026-11-24)
            network = mcp_network_words(" ".join([str(item.get("network") or "none"), ",".join(hosts)]).strip())
            variables = [{"name": v["name"], "description": printable(" ".join(str(v.get("description") or "")
                                                                              .split()))[:500],
                          "secret": v.get("secret") is True, "required": v.get("required") is True}
                         for v in (item.get("env") if isinstance(item.get("env"), list) else [])
                         if isinstance(v, dict) and isinstance(v.get("name"), str)
                         and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", v["name"])]
            entries.append({"entry": f"{item['prefix']}:{item['name']}", "prefix": item["prefix"],
                            "name": item["name"], "network": printable(network)[:300],
                            "title": printable(" ".join(str(item.get("title") or "").split()))[:300],
                            "published": item.get("published") is True,
                            "signature": item.get("signature") if item.get("signature") in ("good", "bad") else "none",
                            "catalog": printable(item["catalog"])[:300] if isinstance(item.get("catalog"), str)
                            else catalog_name(item["prefix"], body.get("catalogs") or []),
                            "env": variables[:64]})
        problems = printed.get("problems") if isinstance(printed.get("problems"), list) else []
        return {"entries": entries, "problems": [printable(str(line))[:600] for line in problems][:20],
                "output": ""}

    def mcp_catalog_lines(self, params, body, sources):
        """The catalog as a host before 0.1.95 prints it: `mcp catalog`'s lines read as entries without variables."""
        status, out, err = self.run(params["host"], ["mcp", "catalog", *sources], 120)
        if status != 0:
            raise self.failed(params["host"], ["mcp", "catalog"], status, out, err)
        entries = []
        for line in out.splitlines():
            match = MCP_CATALOG_LINE.fullmatch(line.rstrip())
            if match:
                entries.append({"entry": f"{match['prefix']}:{match['name']}", "prefix": match["prefix"],
                                "name": match["name"], "network": mcp_network_words(printable(match["network"])),
                                "title": printable(match["title"]), "published": not match["unpublished"],
                                "signature": {"signed": "good", "SIGNATURE BAD": "bad"}.get(match["signature"],
                                                                                           "none"),
                                "catalog": catalog_name(match["prefix"], body.get("catalogs") or []), "env": []})
        return {"entries": entries, "problems": [printable(line)[:600] for line in (err or "").splitlines()
                                                 if line.strip()][:20],
                "output": printable(out.strip()[-20000:])}

    def mcp_add(self, params, body):
        """sandbox-ai mcp add ENTRY with --as, --mount, --env, --network (offline, egress-all: the internet without your
        local network, allowlist: only the entry's hosts, lan: your network too - asked first: mcp-lan; none and full
        taken as offline and egress-all), --memory, --pids,
        --catalog, --no-pull, --backend, --unsigned (asked: mcp-unsigned) and --vpn; a writable mount is asked first
        (mcp-writable-mount)."""
        entry = body.get("entry")
        if not (isinstance(entry, str) and MCP_ENTRY.fullmatch(entry)):
            raise Refused(400, "entry is a catalog entry: duckduckgo or hub:binwalk")
        args = ["mcp", "add"]
        if body.get("as") not in (None, ""):
            args.append(f"--as={self.mcp_server(body['as'])}")
        mounts, writable = body.get("mounts", []), []
        if not isinstance(mounts, list) or len(mounts) > 16:
            raise Refused(400, "mounts is a list of {target, host, mode}")
        for mount in mounts:
            if not isinstance(mount, dict) or set(mount) - {"target", "host", "mode"}:
                raise Refused(400, "a mount is {target, host, mode: ro or rw}")
            # HOST default: the folder's default there (/work the start's workspace, else a folder of the server's
            # own, made by sandbox-ai there) - no folder of yours, so none that is asked about as writable.
            default = mount.get("host") == "default"
            target = clean_path(mount.get("target"), "a mount's target")
            source = "default" if default else clean_path(mount.get("host"), "a mount's host directory")
            mode = mount.get("mode") or "ro"
            if mode not in ("ro", "rw") or "=" in target + source or ":" in target + source:
                raise Refused(400, "a mount is TARGET=HOST with ro or rw (no '=' or ':' in the paths)")
            args.append(f"--mount={target}={source}:{mode}")
            if mode == "rw" and not default:
                writable.append(source)
        for value in strings(body.get("env", []), "env", 64):
            args.append(f"--env={mcp_env_spec(value)}")
        if body.get("network") not in (None, ""):
            # Either words: sent as every sandbox-ai takes them (none, full), the host may be an older one.
            network = MCP_NETWORK_ALIASES.get(body["network"], body["network"]) if isinstance(body["network"], str) \
                else body["network"]
            if network not in ("none", "full", "allowlist", "lan"):
                raise Refused(400, "network is offline, egress-all, allowlist or lan")
            if network == "lan":
                confirmed(body, "mcp-lan", entry)
            args.append(f"--network={network}")
        if body.get("memory") not in (None, ""):
            if not (isinstance(body["memory"], str) and SIZE_VALUE.fullmatch(body["memory"])):
                raise Refused(400, "memory is a size: 512m, 2g")
            args.append(f"--memory={body['memory']}")
        if body.get("pids") not in (None, ""):
            args.append(f"--pids={whole(body['pids'], 'pids', 1, 1000000)}")
        args += catalog_sources(body)
        if flag(body, "no_pull"):
            args.append("--no-pull")
        if body.get("backend") not in (None, ""):
            if body["backend"] not in ("podman", "bwrap", "both"):
                raise Refused(400, "backend is podman, bwrap or both")
            args.append(f"--backend={body['backend']}")
        if body.get("vpn") not in (None, ""):
            args.append(f"--vpn={vpn_name(body['vpn'])}")
        if flag(body, "unsigned"):
            confirmed(body, "mcp-unsigned", entry)
            args.append("--unsigned")
        if writable:
            confirmed(body, "mcp-writable-mount", ", ".join(writable))
        build = flag(body, "build")
        if build:
            if flag(body, "no_pull"):
                raise Refused(400, "no_pull is for an image that is pulled; build builds one")
            args = [*args, *self.mcp_build_words(params["host"], args, entry, body)]
        if body.get("confirm") is not True:  # the name it gets: --as, else the entry's own (without its catalog)
            local = body.get("as") or entry.rsplit(":", 1)[-1]
            defined = self.json_of(params["host"], ["mcp", "list", "--json"], 60, ok=(0,))
            if any(isinstance(server, dict) and server.get("name") == local for server in defined or []):
                confirmed(body, "mcp-replace", local)
        # it pulls the image (or builds it): that takes its time
        status, out, err = self.run(params["host"], [*args, entry], 7200 if build else 900)
        if status != 0:
            said = (err or out).strip()
            if not build and "no digest for it yet (not published)" in said:
                # no signed image yet (a pending entry): the page offers to build it from its source (build, asked)
                raise Refused(502, f"sandbox-ai mcp on {params['host']}: {printable(said[-600:])}",
                              code="mcp-unpublished", params={"host": params["host"], "entry": entry})
            if "--unsigned" not in args and MCP_UNSIGNED_HINT in said and "not signed by a trusted key" in said:
                # refused only for its unsigned catalog: the page offers to add it anyway (mcp-unsigned)
                source = MCP_UNSIGNED_SOURCE.search(said)
                raise Refused(502, f"sandbox-ai mcp on {params['host']}: {printable(said[-600:])}",
                              code="mcp-unsigned-catalog",
                              params={"host": params["host"], "entry": entry,
                                      "catalog": source["catalog"] if source else ""})
            raise self.failed(params["host"], args, status, out, err)
        return {"output": printable(out.strip()[-4000:])}

    def mcp_build_words(self, host, args, entry, body):
        """The words `mcp add ARGS ENTRY` gets to build ENTRY from its source (mcp add --build) - once asked: without
        the "commit" and "source_sha256" to build, what would be built is read on HOST (--build-plan: nothing built)
        and refused with it (mcp-build-asked: the page asks, naming the commit, the source archive's SHA-256 and that
        it is added unsigned); with them, exactly that is built (--yes) once confirmed ({"confirm_build": true})."""
        commit, sha256 = body.get("commit"), body.get("source_sha256")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) and isinstance(sha256, str) and \
                re.fullmatch(r"[0-9a-f]{64}", sha256):
            confirmed(body, "mcp-build", f"{entry} at the commit {commit} (source archive SHA-256 {sha256})")
            return ["--build", f"--commit={commit}", f"--source-sha256={sha256}", "--yes"]
        status, out, err = self.run(host, [*args, "--build-plan", entry], 900)
        if status != 0:
            raise self.failed(host, args, status, out, err)
        try:
            plan = json.loads(out)
        except (ValueError, RecursionError):
            plan = None
        if not isinstance(plan, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(plan.get("commit"))) or \
                not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("archive_sha256"))):
            raise Refused(502, f"sandbox-ai mcp add --build-plan on {host}: not a build plan")
        facts = {key: printable(str(plan.get(key) or ""))[:300] for key in
                 ("entry", "repository", "commit", "path", "archive_sha256", "catalog_label", "signature")}
        raise Refused(400, f"{facts['entry']} on {host}: built from {facts['repository']} at the commit "
                           f"{facts['commit']} ({facts['path']}, source archive SHA-256 {facts['archive_sha256']}) and "
                           f"added unsigned: {NEEDS_CONFIRM['mcp-build']}: send {{\"confirm_build\": true}} with its "
                           "commit and source_sha256", confirm="mcp-build", code="mcp-build-asked",
                      params={"host": host, **facts})

    # logins and identities: the token is always a secret of that host, named, never a value here
    # phone notifications (sandbox-ai notify on that host): its channels, by secret names
    def notify_list(self, params, body):
        return self.json_of(params["host"], ["notify", "list", "--json"], 30)

    def notify_add(self, params, body):
        """notify add with a channel's fields (notify.json's names), checked as sandbox-ai checks them before
        anything reaches the host; asked first (notify-add): that host's questions and alarms go to the service it
        names. {"check_only": true}: only checked."""
        notify = module("notify")
        fields = {key: body.get(key) for key in notify.FIELDS if body.get(key) not in (None, "")}
        try:
            args = notify.add_arguments(fields)
            channel = notify.check_channel(fields)
        except (ValueError, TypeError, AttributeError) as exc:
            raise Refused(400, printable(str(exc))[:500]) from None
        if flag(body, "check_only"):
            return {"ok": True}
        confirmed(body, "notify-add", printable(f"{channel['name']}: {notify.where(channel)}")[:300])
        return self.done(params["host"], args, 30)

    @staticmethod
    def notify_name(params):
        name = params["name"]
        if not module("notify").NAME.fullmatch(name):
            raise Refused(400, "a channel's name is letters, digits, _ . - (up to 32)")
        return name

    def notify_remove(self, params, body):
        name = self.notify_name(params)
        confirmed(body, "notify-remove", name)
        return self.done(params["host"], ["notify", "remove", name], 30)

    def notify_test(self, params, body):
        """notify test NAME: {"ok", "output"} - a message that could not be sent is its answer, not an error."""
        return self.checked(params["host"], ["notify", "test", self.notify_name(params)], 60)

    def notify_link(self, params, body):
        """notify link URL (the web interface's public https address in the messages), or --none (url null)."""
        url = body.get("url")
        if url in (None, ""):
            return self.done(params["host"], ["notify", "link", "--none"], 30)
        try:
            url = module("notify").check_link(url)
        except ValueError as exc:
            raise Refused(400, printable(str(exc))[:300]) from None
        return self.done(params["host"], ["notify", "link", url], 30)

    # scheduled runs (sandbox-ai schedule on that host): checked here as the command checks them
    def schedules(self, params, body):
        """schedule list --json: {"schedules": [...], "problems", "timer"}."""
        return self.json_of(params["host"], ["schedule", "list", "--json"], 30, ok=(0,))

    @staticmethod
    def schedule_name(params):
        name = params["name"]
        if not module("schedule").NAME.fullmatch(name):
            raise Refused(400, "a schedule's name is 1-24 lower-case letters, digits, _ and -")
        return name

    def schedule_show(self, params, body):
        return self.json_of(params["host"], ["schedule", "show", self.schedule_name(params), "--json"], 30, ok=(0,))

    def schedule_history(self, params, body):
        return self.json_of(params["host"], ["schedule", "history", self.schedule_name(params), "--json"], 30,
                            ok=(0,))

    def schedule_report(self, params, body):
        """schedule report NAME [RUN]: {"markdown"} - the run's summary and the start's report (.../report/RUN;
        default: the last)."""
        name, run = self.schedule_name(params), params.get("run")
        if run and not module("schedule").RUN_ID.fullmatch(run):
            raise Refused(400, "a run of the schedule's history (YYYYMMDD-hhmmss-xxxx)")
        status, out, err = self.run(params["host"], ["schedule", "report", name, *([run] if run else [])], 60)
        if status != 0:
            raise self.failed(params["host"], ["schedule", "report"], status, out, err)
        return {"markdown": printable(out[-(4 << 20):])}

    def schedule_add(self, params, body):
        """schedule add with the form's fields (schedule.add_arguments: checked as the command checks them); asked
        first (schedule-add): the host starts that agent by itself. {"check_only": true}: only checked."""
        fields = {key: body.get(key) for key in (
            "name", "every", "at", "cron", "on_push", "agent", "options", "agent_args", "prompt", "preset",
            "preset_values", "board", "workdir", "ask", "limit", "missed", "keep_starts", "commit", "push", "notify",
            "replace") if body.get(key) is not None}
        try:
            args = module("schedule").add_arguments(fields)
        except (ValueError, TypeError, AttributeError) as exc:
            raise Refused(400, printable(str(exc))[:500]) from None
        if flag(body, "check_only"):
            return {"ok": True}
        confirmed(body, "schedule-add", printable(f"{fields.get('name')}: {fields.get('agent') or 'board'}, ask "
                                                  f"{fields.get('ask') or 'off'}")[:300])
        return self.done(params["host"], args, 120)

    def schedule_run(self, params, body):
        return self.done(params["host"], ["schedule", "run-now", self.schedule_name(params)], 60)

    def schedule_pause(self, params, body):
        return self.done(params["host"], ["schedule", "pause", self.schedule_name(params)], 30)

    def schedule_resume(self, params, body):
        return self.done(params["host"], ["schedule", "resume", self.schedule_name(params)], 30)

    def schedule_remove(self, params, body):
        name = self.schedule_name(params)
        confirmed(body, "schedule-remove", name)
        return self.done(params["host"], ["schedule", "remove", name, *(["--keep-history"]
                                                                         if flag(body, "keep_history") else [])], 60)

    def schedule_timer(self, params, body):
        """schedule enable ({"on": true}) or disable: the user-level timer of that host; asked first."""
        on = flag(body, "on")
        confirmed(body, "schedule-timer", "on" if on else "off")
        return self.done(params["host"], ["schedule", "enable" if on else "disable"], 120)

    def git_logins(self, params, body):
        return self.json_of(params["host"], ["git-logins", "list", "--json"], 30)

    @staticmethod
    def login_server_checked(url, kind=None):
        """The server of a Git login (KIND None) or a publish login of KIND checked as sandbox-ai checks it
        (git_server: http(s)://[USER@]HOST[:PORT] without a path, plain http only to this machine; publish:
        https://HOST): 400 with its words otherwise. The page asks this first (check_only), before it stores a
        token for the login: a login refused afterwards would leave that token behind as a secret."""
        try:
            if kind is None:
                module("agent_sandbox").git_server(url, "the Git server")
            elif url is not None:
                module("publish_logins").server_host(kind, url)
        except ValueError as exc:
            raise Refused(400, printable(str(exc))) from None

    def git_login_add(self, params, body):
        """git-logins add --secret NAME [--user USER] URL (asked first: git-login-add). With {"check_only": true}
        only what is sent is checked (the URL as sandbox-ai checks it): nothing runs on the host."""
        url = git_url(body.get("url"))
        args = ["git-logins", "add", f"--secret={secret_name(body.get('secret'))}"]
        if body.get("user") not in (None, ""):
            if not (isinstance(body["user"], str) and LOGIN_USER.fullmatch(body["user"])):
                raise Refused(400, "user is 1-64 letters, digits, '.', '_' or '-'")
            args.append(f"--user={body['user']}")
        self.login_server_checked(url)
        if flag(body, "check_only"):
            # git-logins add replaces the login of that server (the same server as sandbox-ai tells them apart):
            # asked first, as the app asks and as a publish login of that kind is - in the check the page sends
            # before it stores a token and asks git-login-add (whose yes, {"confirm": true}, answers both)
            if body.get("confirm") is not True:
                server = module("agent_sandbox").git_server
                wanted = server(url, "the Git server")["url"]

                def same(entry):
                    with contextlib.suppress(ValueError, TypeError, KeyError):
                        return server(entry["url"], "the Git server")["url"] == wanted
                    return False
                if any(isinstance(entry, dict) and same(entry) for entry in
                       self.json_of(params["host"], ["git-logins", "list", "--json"], 30, ok=(0,)) or []):
                    confirmed(body, "login-replace", wanted)
            return {"ok": True}
        confirmed(body, "git-login-add", url)
        return self.done(params["host"], [*args, url], 30)

    def git_login_remove(self, params, body):
        url = git_url(body.get("url"))
        confirmed(body, "login-remove", url)
        return self.done(params["host"], ["git-logins", "remove", url], 30)

    def git_login_edit(self, params, body):
        """git-logins edit [--url=NEW] [--user=USER] [--secret=NAME] -- URL (asked first: login-edit): the login for
        URL changed in place. With {"check_only": true} only what is sent is checked: nothing runs on the host."""
        url = git_url(body.get("url"))
        args = ["git-logins", "edit"]
        if body.get("new_url") not in (None, ""):
            new = git_url(body.get("new_url"))
            self.login_server_checked(new)
            args.append(f"--url={new}")
        if body.get("user") not in (None, ""):
            if not (isinstance(body["user"], str) and LOGIN_USER.fullmatch(body["user"])):
                raise Refused(400, "user is 1-64 letters, digits, '.', '_' or '-'")
            args.append(f"--user={body['user']}")
        if body.get("secret") not in (None, ""):
            args.append(f"--secret={secret_name(body.get('secret'))}")
        if len(args) == 2:
            raise Refused(400, "nothing to change: new_url, user or secret")
        if flag(body, "check_only"):
            return {"ok": True}
        confirmed(body, "login-edit", url)
        return self.done(params["host"], [*args, "--", url], 60)

    def publish_login_edit(self, params, body):
        """publish-logins edit KIND [--secret=NAME] [--url=URL|default] (asked first: login-edit)."""
        kind = publish_kind(params["kind"])
        args = ["publish-logins", "edit"]
        if body.get("secret") not in (None, ""):
            args.append(f"--secret={secret_name(body.get('secret'))}")
        if body.get("url") == "default":
            args.append("--url=default")
        else:
            args += self.publish_url(body)
        if len(args) == 2:
            raise Refused(400, "nothing to change: secret or url (\"default\": the service's public server)")
        if body.get("url") not in (None, "", "default"):
            self.login_server_checked(body["url"], kind)
        confirmed(body, "login-edit", kind)
        return self.done(params["host"], [*args, kind], 60)

    def give_session_login(self, params, body):
        """sandbox-ai login give RUN [AGENT] [--from-session=NAME] --json on that host: an agent login for a sandbox
        that runs there - the central one, or session NAME's. With {"check_only": true} it is only shown (--dry-run:
        whose login it is, what it replaces; never a token); given (asked first: login-give) only with the
        fingerprint that was shown (--yes --expect), "restart": its agent started again inside the sandbox."""
        run = start_name(body.get("run"))
        args = ["login", "give", run]
        if body.get("agent") not in (None, ""):
            if body["agent"] not in LOGIN_AGENTS:
                raise Refused(400, agent_words(LOGIN_AGENTS))
            args.append(body["agent"])
        if body.get("from_session") not in (None, ""):
            args.append(f"--from-session={start_name(body.get('from_session'))}")
        if flag(body, "check_only"):
            return self.json_of(params["host"], [*args, "--dry-run", "--json"], 120, ok=(0,))
        fingerprint = body.get("fingerprint")
        if not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{8}", fingerprint)):
            raise Refused(400, "fingerprint is the 8 hex characters the login was shown with")
        confirmed(body, "login-give", f"the login {fingerprint} to {run}")
        return self.json_of(params["host"], [*args, "--yes", f"--expect={fingerprint}",
                                             *(["--restart"] if flag(body, "restart") else []), "--json"], 300,
                            ok=(0,))

    def git_login_check(self, params, body):
        url = body.get("url")
        return self.checked(params["host"], ["git-logins", "check", *([git_url(url)] if url not in (None, "") else [])])

    def publish_logins(self, params, body):
        return self.json_of(params["host"], ["publish-logins", "list", "--json"], 30)

    def login_status(self, params, body):
        """sandbox-ai login --status: which login each agent's sandboxes get on that host."""
        return self.done(params["host"], ["login", "--status"], 30)

    def session_logins(self, params, body):
        """sandbox-ai login --status --sessions --json: which login each running or kept session uses (its account as
        the file says, kind, expiry, a fingerprint - never a token)."""
        found = self.json_of(params["host"], ["login", "--status", "--sessions", "--json"], 120, ok=(0,))
        if not isinstance(found, dict) or not isinstance(found.get("sessions"), list):
            raise Refused(502, "sandbox-ai login --status --sessions: not a list of sessions")
        return found

    def import_session_login(self, params, body):
        """sandbox-ai login AGENT --from-session NAME --yes --expect FINGERPRINT [--replace] --json: confirmed
        (login-import), that session's login - the one shown, by its fingerprint - becomes the central login."""
        agent = body.get("agent")
        if agent not in LOGIN_AGENTS:  # (session_login.AGENTS: the agents the central store takes)
            raise Refused(400, agent_words(LOGIN_AGENTS))
        name = start_name(body.get("name"))
        fingerprint = body.get("fingerprint")
        if not (isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{8}", fingerprint)):
            raise Refused(400, "fingerprint is the 8 hex characters the list shows")
        replace = flag(body, "replace")
        confirmed(body, "login-import", f"import the {agent} login of {name}")
        return self.json_of(params["host"], ["login", agent, "--from-session", name, "--yes",
                                             f"--expect={fingerprint}", *(["--replace"] if replace else []), "--json"],
                            180, ok=(0,))

    def publish_kinds(self, params, body):
        return self.done(params["host"], ["publish-logins", "kinds"], 30)

    @staticmethod
    def publish_url(body):
        url = body.get("url")
        if url in (None, ""):
            return []
        if not (isinstance(url, str) and PUBLISH_URL.fullmatch(url)):
            raise Refused(400, "url is https://HOST (another server of that kind; no path)")
        return [f"--url={url}"]

    def publish_login_add(self, params, body):
        """publish-logins add: one that replaces the login of that kind is asked first (login-replace). With
        {"check_only": true} it is checked (its server as sandbox-ai checks it, the replacement asked) and nothing is
        added: the page asks this before it stores the token."""
        kind = publish_kind(body.get("kind"))
        args = ["publish-logins", "add", f"--secret={secret_name(body.get('secret'))}", *self.publish_url(body), kind]
        self.login_server_checked(body.get("url") or None, kind)
        if body.get("confirm") is not True and any(isinstance(entry, dict) and entry.get("kind") == kind for entry in
                                                   self.json_of(params["host"], ["publish-logins", "list", "--json"],
                                                                30, ok=(0,))):
            confirmed(body, "login-replace", kind)
        if flag(body, "check_only"):
            return {"ok": True}
        return self.done(params["host"], args, 30)

    def publish_login_remove(self, params, body):
        kind = publish_kind(params["kind"])
        confirmed(body, "login-remove", kind)
        return self.done(params["host"], ["publish-logins", "remove", kind], 30)

    def publish_login_check(self, params, body):
        kind = body.get("kind")
        return self.checked(params["host"], ["publish-logins", "check",
                                             *([publish_kind(kind)] if kind not in (None, "") else [])])

    def identities(self, params, body):
        return self.json_of(params["host"], ["identities", "list", "--json"], 30)

    def identity_show(self, params, body):
        return self.json_of(params["host"], ["identities", "show", "--json", identity_name(params["name"])], 30)

    @staticmethod
    def identity_fields(body):
        """--name, --email, --description of BODY (each given or not)."""
        args = []
        if body.get("name") not in (None, ""):
            args.append(f"--name={one_line(body['name'], 'name', 100)}")
        if body.get("email") not in (None, ""):
            if not (isinstance(body["email"], str) and EMAIL.fullmatch(body["email"])):
                raise Refused(400, "email is an address: NAME@DOMAIN")
            args.append(f"--email={body['email']}")
        if body.get("description") not in (None, ""):
            args.append(f"--description={one_line(body['description'], 'description', 300)}")
        return args

    def identity_add(self, params, body):
        name = identity_name(body.get("identity"))
        return self.done(params["host"], ["identities", "add", *self.identity_fields(body), name], 30)

    def identity_set(self, params, body):
        name = identity_name(params["name"])
        fields = self.identity_fields(body)
        if not fields:
            raise Refused(400, "nothing to change: give name, email or description")
        return self.done(params["host"], ["identities", "set", *fields, name], 30)

    def identity_login(self, params, body):
        """identities login NAME URL --secret S [--user U] | identities publish NAME KIND --secret S [--url URL]:
        a login only a start with --identity NAME uses."""
        name = identity_name(params["name"])
        secret = f"--secret={secret_name(body.get('secret'))}"
        if params["action"] == "login":
            url = git_url(body.get("url"))
            user = []
            if body.get("user") not in (None, ""):
                if not (isinstance(body["user"], str) and LOGIN_USER.fullmatch(body["user"])):
                    raise Refused(400, "user is 1-64 letters, digits, '.', '_' or '-'")
                user = [f"--user={body['user']}"]
            args, key, what = ["identities", "login", secret, *user, name, url], ("git", "url", url), url
        else:
            kind = publish_kind(body.get("kind"))
            args = ["identities", "publish", secret, *self.publish_url(body), name, kind]
            key, what = ("publish", "kind", kind), kind
        if body.get("confirm") is not True:  # one that replaces a login of the identity is asked first
            identity = self.json_of(params["host"], ["identities", "show", "--json", name], 30, ok=(0,))
            listed = identity.get(key[0]) if isinstance(identity, dict) else None
            if any(isinstance(e, dict) and e.get(key[1]) == key[2] for e in listed or []):
                confirmed(body, "login-replace", f"{what} of {name}")
        return self.done(params["host"], args, 30)

    def identity_logout(self, params, body):
        name = identity_name(params["name"])
        if params["action"] == "logout":
            value = git_url(body.get("url"))
        else:
            value = publish_kind(body.get("kind"))
        confirmed(body, "login-remove", f"{value} of {name}")
        return self.done(params["host"], ["identities", params["action"], name, value], 30)

    def identity_remove(self, params, body):
        name = identity_name(params["name"])
        confirmed(body, "login-remove", name)
        return self.done(params["host"], ["identities", "remove", name], 30)

    def identity_check(self, params, body):
        name = body.get("identity")
        return self.checked(params["host"], ["identities", "check",
                                             *([identity_name(name)] if name not in (None, "") else [])])

    # the network of a run kept for its project's later starts
    def net_keep(self, params, body):
        """sandbox-ai net show NAME --keep | --keep-all: what the run was given into the project's .sandbox-ai.json
        (asked first)."""
        name, every = start_name(params["name"]), flag(body, "all")
        confirmed(body, "net-keep", f"{name}{' (also what approvals opened once or for the run)' if every else ''}")
        return self.done(params["host"], ["net", "show", name, "--keep-all" if every else "--keep"], 60)

    # presets and their directories
    def preset_show(self, params, body):
        name = params["name"]
        if not KIT_NAME.fullmatch(name):
            raise Refused(400, "no such preset", code="no-preset", params={"preset": name})
        return self.json_of(params["host"], ["presets", "show", "--json", name], 30)

    def fix_plan(self, params, body):
        """What a new start lacks on that host and what would fix each (`sandbox-ai AGENT ... --fix-plan`: nothing
        is made): {"fixes": [{id, kind, missing, title, command, duration, unsigned, security, needed, verdict,
        why}], "questions": all|each, "mode"} - the page asks about it before it opens the start's terminal. BODY:
        the form's {agent, name, workspace, options}."""
        query = {"agent": body.get("agent"), "name": body.get("name") or "", "workspace": body.get("workspace") or "",
                 "options": json.dumps(strings(body.get("options", []), "options"))}
        if not all(isinstance(query[key], str) for key in ("name", "workspace")):
            raise Refused(400, "name and workspace are text")
        args = [*start_args(params["host"], query, self.runner), "--fix-plan"]
        older = newer_needed(params["host"], args)  # (a host whose sandbox-ai has no fix plan yet: said so)
        if older:
            raise older
        plan = self.json_of(params["host"], args, 180, ok=(0,))
        if not isinstance(plan, dict) or not isinstance(plan.get("fixes"), list):
            raise Refused(502, f"sandbox-ai on {params['host']}: not a fix plan")

        def line(value, limit=600):
            return printable(" ".join(str(value if value is not None else "").split()))[:limit]
        fixes = [{"id": line(item.get("id"), 200), "kind": line(item.get("kind"), 40),
                  "missing": line(item.get("missing")), "title": line(item.get("title")),
                  "command": line(item.get("command"), 2000), "duration": line(item.get("duration"), 80),
                  "unsigned": item.get("unsigned") is True, "security": line(item.get("security")),
                  "terminal": item.get("terminal") is True, "needed": item.get("needed") is not False,
                  # (a choice between ways to start: its answers share "choice"; "start": the start as it would be)
                  "choice": line(item.get("choice"), 200), "start": line(item.get("start"), 2000),
                  "verdict": item.get("verdict") if item.get("verdict") in ("offered", "manual", "refused")
                  else "manual", "why": line(item.get("why"))}
                 for item in plan["fixes"][:64] if isinstance(item, dict)]
        # (only an id the terminal's --fix-only takes is one the page can ask for)
        for item in fixes:
            if item["verdict"] == "offered" and not FIX_ID.fullmatch(item["id"]):
                item["verdict"], item["why"] = "manual", "run its command yourself"
        return {"fixes": fixes, "questions": plan.get("questions") if plan.get("questions") in FIX_QUESTIONS
                else FIX_QUESTIONS[0], "mode": line(plan.get("mode"), 40)}

    def preset_check(self, params, body):
        """sandbox-ai presets check NAME [--value KEY=VALUE]... [--agent AGENT]: what is missing before it starts."""
        name = params["name"]
        if not KIT_NAME.fullmatch(name):
            raise Refused(400, "no such preset", code="no-preset", params={"preset": name})
        values = body.get("values", {})
        if not isinstance(values, dict) or len(values) > 32:
            raise Refused(400, "values is {KEY: VALUE}")
        args = ["presets", "check"]
        for key, value in values.items():
            if not PRESET_KEY.fullmatch(key):
                raise Refused(400, f"no such value: {printable(key)[:64]}", code="unknown-preset-value",
                              params={"preset": name, "value": key})
            if value in (None, ""):
                continue
            args.append(f"--value={key}={one_line(value, key)}")
        agent = body.get("agent")
        if agent not in (None, "", *AGENTS):
            raise Refused(400, agent_words())
        args += [f"--agent={agent}"] if agent else []
        return self.checked(params["host"], [*args, name])

    def preset_dirs(self, params, body):
        return self.json_of(params["host"], ["presets", "dirs", "list", "--json"], 30)

    def preset_dir_add(self, params, body):
        """presets dirs add DIR: later starts read presets from there too (asked first, as preset_dirs is)."""
        directory = clean_path(body.get("directory"), "directory")
        confirmed(body, "defaults-lower", f"preset_dirs: later starts read presets from {directory} too")
        return self.done(params["host"], ["presets", "dirs", "add", directory], 30)

    def preset_dir_remove(self, params, body):
        return self.done(params["host"], ["presets", "dirs", "remove", clean_path(body.get("directory"), "directory")],
                         30)

    # one run's canaries
    @staticmethod
    def canary_run(value):
        if not CANARY_RUN.fullmatch(value):
            raise Refused(400, "a run is 1-81 letters, digits, '_', '.' or '-'")
        return value

    def canary_list(self, params, body):
        """sandbox-ai canaries list RUN --json: its canary files (tokens shortened) and what happened."""
        return self.json_of(params["host"], ["canaries", "list", self.canary_run(params["run"]), "--json"], 60)

    def canary_tokens(self, params, body):
        run = self.canary_run(params["run"])
        confirmed(body, "canary-tokens", run)
        return self.json_of(params["host"], ["canaries", "list", run, "--tokens", "--json"], 60)

    def canary_resume(self, params, body):
        run = self.canary_run(params["run"])
        confirmed(body, "canary-resume", run)
        return self.done(params["host"], ["canaries", "resume", run], 120)

    # a kit, an LLM server, a VPN profile in full
    def kit_show(self, params, body):
        name = params["name"]
        if not KIT_NAME.fullmatch(name):
            raise Refused(400, "no such kit")
        return self.json_of(params["host"], ["kits", "show", "--json", "--", name], 60)

    def llm_show(self, params, body):
        if params["name"].startswith("-"):
            raise Refused(400, "no such server")
        return self.json_of(params["host"], ["llm", "show", params["name"], "--json"], 30)

    def vpn_show(self, params, body):
        return self.json_of(params["host"], ["vpn", "show", vpn_name(params["name"]), "--json"], 30)

    VPN_SETTINGS = ("provider", "auth-secret", "account-secret", "auth-key-secret", "exit-node", "login-server",
                    "hostname", "exit-country", "bootstrap-timeout", "bootstrap-attempts", "bootstrap-stall")

    def vpn_set(self, params, body):
        """sandbox-ai vpn set NAME --SETTING=VALUE ... (asked first: vpn-replace): the profile changed in place - the
        settings `vpn show --json` names ("settings"), each one line without a control character; "none" clears one
        (a bootstrap setting: "default"). Its keys are not touched, and no value of a secret is sent: a secret is
        named."""
        name = vpn_name(params["name"])
        settings = body.get("settings")
        if not isinstance(settings, dict) or not settings or set(settings) - set(self.VPN_SETTINGS):
            raise Refused(400, f"settings is an object of {', '.join(self.VPN_SETTINGS)}")
        args = ["vpn", "set"]
        for key in self.VPN_SETTINGS:
            if key in settings:
                value = settings[key]
                if not (isinstance(value, str) and 0 < len(value) <= 300 and value.isprintable()):
                    raise Refused(400, f"{key}: one line of at most 300 characters (\"none\" clears it)")
                args.append(f"--{key}={value}")
        confirmed(body, "vpn-replace", name)
        return self.done(params["host"], [*args, "--", name], 60)

    # logs of a directory or project, the privacy check of repositories, the activity of runs
    def logs_of(self, params, body):
        """sandbox-ai logs --json [--project PROJECT] [DIRECTORY]: one log directory, or a project's (one of them:
        without, sandbox-ai would read the log directory of the directory it runs in, this service's own or the SSH
        login's, which nobody chose)."""
        if all(body.get(key) in (None, "") for key in ("project", "directory")):
            raise Refused(400, "which logs? Name the log directory or the project")
        args = ["logs", "--json"]
        if body.get("project") not in (None, ""):
            args.append(f"--project={clean_path(body['project'], 'project')}")
        if body.get("directory") not in (None, ""):
            args.append(clean_path(body["directory"], "directory"))
        return self.json_of(params["host"], args, 60)

    def privacy_check(self, params, body):
        """sandbox-ai privacy-check --json [--rules-dir DIR] [--project NAME] [--all] PATH...: what the next push of
        each repository would publish that should stay private (nothing is pushed)."""
        paths = [clean_path(value, "a repository") for value in strings(body.get("paths", []), "paths", 32)]
        if not paths:
            raise Refused(400, "paths: the repositories to check (absolute paths on that host)")
        args = ["privacy-check", "--json"]
        if body.get("rules_dir") not in (None, ""):
            args.append(f"--rules-dir={clean_path(body['rules_dir'], 'rules_dir')}")
        if body.get("project") not in (None, ""):
            if not (isinstance(body["project"], str) and KIT_NAME.fullmatch(body["project"])):
                raise Refused(400, "project is the name of a project rule file")
            args.append(f"--project={body['project']}")
        if flag(body, "all"):
            args.append("--all")
        # 0: nothing to keep private, 1: findings, 2: a repository was refused (no rules to check it with: its entry
        # says "checked": false and why in "refused"); any other status is a failure. A list that names no
        # repository checked nothing, whatever the status: never an all-clear (the page says "not checked" for a
        # refused one, and for an entry of an older sandbox-ai that says nothing of it when the status was 2).
        status, out, err = self.run(params["host"], [*args, *paths], 300)
        try:
            found = json.loads(out) if status in (0, 1, 2) else None
        except (ValueError, RecursionError):
            found = None
        if not (isinstance(found, list) and found and all(isinstance(entry, dict) for entry in found)):
            if status not in (0, 1, 2) or found is None and status != 2:
                raise self.failed(params["host"], args, status, out, err)
            raise Refused(502, f"sandbox-ai privacy-check on {params['host']}: nothing was checked: "
                               f"{printable((err or out).strip()[-600:])}", code="nothing-checked",
                          params={"host": params["host"]})
        if status == 2 and not any(entry.get("refused") for entry in found):
            for entry in found:  # an older sandbox-ai: the refused ones are not named, so none is an all-clear
                entry.setdefault("checked", False)
                entry.setdefault("refused", printable((err or "refused").strip()[-300:]))
        return found

    def activity(self, params, body):
        """sandbox-ai activity --json --tail N [--kind K]... [--grep TEXT] [--since TIME] [--session NAME |
        --project PATH | --log-dir DIR | --home DIR] [RUN]: the last events (JSON lines) as {"events": [...]}; the
        page polls it to follow."""
        args = ["activity", "--json", f"--tail={whole(body.get('tail', 200), 'tail', 1, 2000)}"]
        kinds = strings(body.get("kinds", []), "kinds", len(ACTIVITY_KINDS))
        if not set(kinds) <= set(ACTIVITY_KINDS):
            raise Refused(400, f"kinds are {', '.join(ACTIVITY_KINDS)}")
        args += [f"--kind={kind}" for kind in dict.fromkeys(kinds)]
        if body.get("grep") not in (None, ""):
            args.append(f"--grep={one_line(body['grep'], 'grep', 200)}")
        if body.get("since") not in (None, ""):
            if not (isinstance(body["since"], str) and ACTIVITY_SINCE.fullmatch(body["since"])):
                raise Refused(400, "since is 10m, 2h, 3d, 14:30, 2026-11-04 or an ISO time")
            args.append(f"--since={body['since']}")
        # where it is read (one of them; with none, sandbox-ai reads the log directory of the directory it runs in,
        # this service's own or the SSH login's: the page always names one): a session's, a project's default log
        # directory, a log directory, the transcripts of a sandbox HOME
        where = [key for key in ("session", "project", "log_dir", "home") if body.get(key) not in (None, "")]
        if len(where) != 1:  # never the directory this service (or the SSH login) happens to run in
            raise Refused(400, "where to read: one of session, project, log_dir and home")
        if where == ["session"]:
            args.append(f"--session={start_name(body['session'])}")
        elif where:
            option = {"project": "--project", "log_dir": "--log-dir", "home": "--home"}[where[0]]
            args.append(f"{option}={clean_path(body[where[0]], where[0])}")
        if body.get("run") not in (None, ""):
            args.append(self.canary_run(body["run"]) if isinstance(body["run"], str) else self.canary_run(""))
        status, out, err = self.run(params["host"], args, 60)
        if status != 0:
            raise self.failed(params["host"], args, status, out, err)
        events = []
        for line in out.splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
        return {"events": events}

    # the version in full, the images, the demo device
    def version_of(self, params, body):
        return self.done(params["host"], ["version", "--verbose"], 30)

    def update_mode(self, params, body):
        """update mode --json: the mode, the last check and what it found (a newer version), the next check, the
        timer, how a copy that does not update itself is updated."""
        return self.json_of(params["host"], ["update", "mode", "--json"], 60, ok=(0,))

    def update_mode_set(self, params, body):
        """update mode MODE [--at WINDOW] --json, asked first (update-mode): what that host does by itself."""
        mode, at = body.get("mode"), body.get("at")
        if mode not in UPDATE_MODES:
            raise Refused(400, f"mode is one of {', '.join(UPDATE_MODES)}")
        if at not in (None, "") and (mode != "auto" or not isinstance(at, str) or not UPDATE_WINDOW.fullmatch(at)):
            raise Refused(400, "at is the window of mode auto: HH:MM or HH:MM-HH:MM")
        confirmed(body, "update-mode", mode + (f", window {at}" if at else ""))
        return self.json_of(params["host"], ["update", "mode", mode, *([f"--at={at}"] if at else []), "--json"], 180,
                            ok=(0,))

    def images(self, params, body):
        return self.json_of(params["host"], ["images", "--json"], 120)

    def demo_status(self, params, body):
        return self.checked(params["host"], ["demo-device", "status"], 30)

    def demo_start(self, params, body):
        port = body.get("port")
        return self.done(params["host"], ["demo-device", "start",
                                          *([f"--port={whole(port, 'port', 1024, 65535)}"] if port not in (None, "")
                                            else [])], 60)

    def demo_stop(self, params, body):
        return self.done(params["host"], ["demo-device", "stop"], 30)

    # teams (sandbox-ai team): one sandbox per agent on a shared workspace, with a task board between them
    @staticmethod
    def team_name(value):
        if not (isinstance(value, str) and TEAM_NAME.fullmatch(value)):
            raise Refused(400, "a team's name is 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit",
                          code="bad-name", params={"what": "team"})
        return value

    def teams(self, params, body):
        """team list (it has no --json): [{"name", "members", "workspace"}] from its lines, in its order; a team whose
        record is damaged as {"name", "members": [], "workspace": "", "damaged": True} (deleted as any other)."""
        status, out, err = self.run(params["host"], ["team", "list"], 30)
        if status != 0:
            raise self.failed(params["host"], ["team", "list"], status, out, err)
        found = []
        for line in out.splitlines():
            match = team_line().fullmatch(line.rstrip())
            if match:
                found.append({"name": match["name"], "members": match["members"].split(", "),
                              "workspace": printable(match["workspace"].strip())[:4096]})
                continue
            match = TEAM_DAMAGED_LINE.fullmatch(line.rstrip())
            if match:
                found.append({"name": match["name"], "members": [], "workspace": "", "damaged": True})
        return found

    def team_choices(self, params, body):
        """What a new team on that host can have: its members (team.AGENTS) and the host's VPN profiles with their
        type (`vpn list --json` there, as the app's team dialog lists them). A listing that fails is said ("error");
        the team can still start without a VPN."""
        host = host_name(params["host"])
        try:
            profiles = list(team_vpn_profiles(host, lambda name, args, timeout=120, input=None:
                                              self.run(name, args, timeout, input)).values())
            error = None
        except Refused as exc:
            if exc.status != 502:  # no such host, ...: that, not an empty picker
                raise
            profiles, error = [], str(exc)
        return {"agents": list(team_agents()), "profiles": profiles, "error": error}

    def team_show(self, params, body):
        """team show NAME --json: its record, each member's state and the board's tasks."""
        return self.json_of(params["host"], ["team", "show", self.team_name(params["name"]), "--json"], 60, ok=(0,))

    def team_post(self, params, body):
        """team post NAME AGENT TASK: a task for a member, from you (after "--": a task is never an option)."""
        name, agent, task = self.team_name(params["name"]), body.get("agent"), body.get("task")
        if agent not in team_agents():
            raise Refused(400, agent_words(team_agents()))
        if not (isinstance(task, str) and task.strip() and len(task) <= 8000
                and all(c in "\n\t" or c.isprintable() for c in task)):
            raise Refused(400, "the task is text of at most 8000 characters (lines and tabs, no other control "
                               "characters)")
        return self.done(params["host"], ["team", "post", "--", name, agent, task.strip()], 60)

    def team_stop(self, params, body):
        name = self.team_name(params["name"])
        confirmed(body, "team-stop", name)
        return self.done(params["host"], ["team", "stop", name], 300)

    def team_delete(self, params, body):
        name = self.team_name(params["name"])
        confirmed(body, "team-delete", name)
        return self.done(params["host"], ["team", "delete", name, "--yes"], 300)

    # the images' extensions and variants (sandbox-ai images): every value checked here as a word of its kind and
    # passed as --flag=VALUE (never read as an option); images.py checks each package, file and fragment again
    @staticmethod
    def image_target(value):
        if not (isinstance(value, str) and BUILD_TARGET.fullmatch(value)):
            raise Refused(400, f"an image is an agent ({', '.join(AGENTS)}) or a variant's name (1-30 of a-z, "
                               "0-9, '_', '.' and '-')", code="bad-name", params={"what": "image"})
        return value

    @staticmethod
    def fragment_of(body):
        """BODY's Containerfile fragment (for standard input: --fragment=-), or None."""
        fragment = body.get("fragment")
        if fragment is None:
            return None
        if not (isinstance(fragment, str) and len(fragment.encode()) <= FRAGMENT_LIMIT and "\0" not in fragment):
            raise Refused(400, f"the fragment is text of at most {FRAGMENT_LIMIT >> 10} KiB")
        return fragment

    def image_show(self, params, body):
        """images extend TARGET --json without a change: its extension as it is (packages, files, fragment,
        findings)."""
        return self.json_of(params["host"], ["images", "extend", self.image_target(params["target"]), "--json"], 60,
                            ok=(0,))

    def image_packages(self, params, body):
        return self.json_of(params["host"], ["images", "packages", self.image_target(params["target"]), "--json"],
                            180, ok=(0,))

    def image_history(self, params, body):
        target = [self.image_target(params["target"])] if params.get("target") else []
        return self.json_of(params["host"], ["images", "history", *target, "--json"], 60, ok=(0,))

    def image_extend(self, params, body):
        """images extend TARGET --add-KIND=PKG --remove-KIND=PKG (apt, npm, pip) --add-file=SRC:TARGET[:MODE]
        --remove-file=TARGET --fragment=- (on standard input) | --clear-fragment --json. A host file built into the
        image is read by every later start of it: asked first (image-file), naming it."""
        target = self.image_target(params["target"])
        args = ["images", "extend", target]
        for kind in IMAGE_KINDS:
            for change in ("add", "remove"):
                for value in strings(body.get(f"{change}_{kind}", []), f"{change}_{kind}", 64):
                    if not IMAGE_PACKAGE.fullmatch(value):
                        raise Refused(400, f"{change}_{kind}: not a package name: {printable(value)[:80]}")
                    args.append(f"--{change}-{kind}={value}")
        files = body.get("add_files", [])
        if not isinstance(files, list) or len(files) > 32:
            raise Refused(400, "add_files is a list of {source, target, mode}")
        shown = []
        for entry in files:
            if not isinstance(entry, dict) or set(entry) - {"source", "target", "mode"}:
                raise Refused(400, "a file is {source: its path on that host, target: its path in the image, mode}")
            source, where = clean_path(entry.get("source"), "a file's source"), clean_path(entry.get("target"),
                                                                                            "a file's target")
            mode = entry.get("mode") or ""
            if ":" in source + where or mode and not (isinstance(mode, str) and IMAGE_MODE.fullmatch(mode)):
                raise Refused(400, "a file is SOURCE:TARGET[:MODE] (no ':' in the paths; MODE like 0644 or 0755)")
            args.append(f"--add-file={source}:{where}{':' + mode if mode else ''}")
            shown.append(f"{source} at {where}")
        for value in strings(body.get("remove_files", []), "remove_files", 32):
            args.append(f"--remove-file={clean_path(value, 'a file in the image')}")
        fragment, clear = self.fragment_of(body), flag(body, "clear_fragment")
        if fragment is not None and clear:
            raise Refused(400, "a fragment or clear_fragment, not both")
        if fragment is not None:
            args.append("--fragment=-")
        if clear:
            args.append("--clear-fragment")
        if len(args) == 3:
            raise Refused(400, "nothing to change")
        if shown:
            confirmed(body, "image-file", f"{', '.join(shown)} into {target}")
        return self.json_of(params["host"], [*args, "--json"], 120, ok=(0,), input=fragment)

    def image_check(self, params, body):
        """images check TARGET [--fragment=-] --json: its findings and the final Containerfile (exit status 1:
        errors found, which is its answer)."""
        target, fragment = self.image_target(params["target"]), self.fragment_of(body)
        return self.json_of(params["host"], ["images", "check", target, *(["--fragment=-"] if fragment is not None
                                                                          else []), "--json"], 120, ok=(0, 1),
                            input=fragment)

    def image_new(self, params, body):
        """images new NAME --from AGENT [--empty] --json: a variant (localhost/AGENT-sandbox-NAME)."""
        name, agent = self.image_target(body.get("name")), body.get("from")
        if agent not in AGENTS or name in AGENTS:
            raise Refused(400, f"a variant has a name of its own and comes from {', '.join(AGENTS)}")
        return self.json_of(params["host"], ["images", "new", name, f"--from={agent}",
                                             *(["--empty"] if flag(body, "empty") else []), "--json"], 60, ok=(0,))

    def image_recipe(self, params, body):
        """images recipe RECIPE --from AGENT [--as VARIANT] (one of IMAGE_RECIPES): a variant with a ready extension."""
        recipe, agent, variant = body.get("recipe"), body.get("from"), body.get("as")
        if recipe not in IMAGE_RECIPES or agent not in AGENTS:
            raise Refused(400, f"recipe is {' or '.join(IMAGE_RECIPES)}, from {', '.join(AGENTS)}")
        extra = [] if variant in (None, "") else [f"--as={self.image_target(variant)}"]
        return self.done(params["host"], ["images", "recipe", recipe, f"--from={agent}", *extra], 60)

    def image_remove(self, params, body):
        """images remove TARGET [--keep-image]: its extension dropped (a variant's image too, unless kept)."""
        target, keep = self.image_target(params["target"]), flag(body, "keep_image")
        confirmed(body, "image-remove", f"{target}{' (its image is kept)' if keep else ''}")
        return self.done(params["host"], ["images", "remove", target, *(["--keep-image"] if keep else [])], 120)


def no_canaries(value):
    """Whether VALUE (as `defaults set canaries` takes it) names no canary category at all, whatever the host's
    categories are: false, [], "none", ["none"]."""
    if value is False:
        return True
    items = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else None
    if items is None or not all(isinstance(item, str) for item in items):
        return False
    return {item.strip() for item in items if item.strip()} <= {"none"}


def canary_set(value, categories):
    """The canary categories VALUE names, as `defaults set canaries` takes it (canary.parse_categories): false, a list,
    "a,b", "all" or "none"; [] and ["none"] are none. Anything else: refused (400)."""
    if value is False:
        return set()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = value
    else:
        raise Refused(400, "canaries is false, a list of categories, all or none")
    names = {item.strip() for item in items if item.strip()}
    if names == {"all"}:
        return set(categories)
    if names <= {"none"}:
        return set()
    if names - set(categories):
        raise Refused(400, f"canary categories are {', '.join(categories)}, all or none")
    return names


def names_workspace(options):
    """Whether OPTIONS (a start's, before a "--") name a workspace: --workspace DIR, --workspace=DIR, -w DIR, -wDIR."""
    for word in options:
        if word == "--":
            return False
        if word in ("--workspace", "-w") or word.startswith("--workspace=") or \
                (word.startswith("-w") and not word.startswith("-wa")):
            return True
    return False


def as_given_on(host, runner=None):
    """HOST's sandbox-ai takes --workspaces-as-given (hosts.knows_workspaces_as_given), a host whose version is not
    known yet asked first: through RUNNER (Api.runner's form) when given."""
    ask = (lambda name, args, timeout: runner(name, args, timeout=timeout)) if runner else True
    return module("hosts").knows_workspaces_as_given(host, ask=ask)


FIX_ID = re.compile(r"[a-z]{2,12}:[A-Za-z0-9@._:/+=-]{1,200}")  # a fix's id as --fix-plan names it (fixes.py)


def start_args(host, query, runner=None):
    """`sandbox-ai AGENT ...` of a new start as the page's form describes it (QUERY: agent, name, workspace, options -
    a JSON list -, fix - a JSON list of the ids of the fixes the page asked about: --fix-only each)."""
    agent = query.get("agent")
    if agent not in AGENTS:
        raise Refused(400, agent_words())
    args = [agent]
    if query.get("name"):
        args += ["--name", new_start_name(query["name"])]
    options = strings(json.loads(query.get("options") or "[]"), "options")
    if query.get("workspace"):
        workspace = query["workspace"]
        # (a folder of another machine, sshfs://...: that host mounts it with sshfs for the start)
        workspace = remote_source(workspace, "the workspace") or workspace
        if not workspace.startswith(("/", "~")) and not remote_source(workspace, "the workspace") \
                or "\0" in workspace:
            raise Refused(400, "the workspace is an absolute path (or ~/..., or sshfs://[USER@]HOST/PATH)")
        args += ["--workspace", workspace]
    elif not names_workspace(options):
        # Without one the launcher takes the directory it runs in: this service's own, or the SSH login's HOME.
        raise Refused(400, "which workspace? Name the folder the agent works on (its /work), in the field or "
                           "with --workspace in the options")
    if as_given_on(host, runner):
        # The workspace is the one chosen here (or in the options): a project file in the directory the start
        # runs in (the web service's own, the SSH login's HOME) must not take the start over. Passed wherever
        # the host's sandbox-ai knows it (0.1.78, which brought that rule; an older one has neither); a host
        # whose version is not known yet (just added) is asked for it first.
        args.append("--workspaces-as-given")
    # What the start lacked and the page asked about (POST .../fix-plan): exactly those fixes, done by the start in
    # its terminal before it goes on (--fix-only ID); never --fix (everything unasked) from a page.
    fixes = strings(json.loads(query.get("fix") or "[]"), "fix", 32)
    for ident in fixes:
        if not FIX_ID.fullmatch(ident):
            raise Refused(400, "fix is a list of fix ids (KIND:NAME, as the fix plan names them)")
        args += ["--fix-only", ident]
    if any(word in ("--fix", "-fx") or word.startswith(("--fix-only", "-fo", "--fix=")) for word in options[
            :options.index("--") if "--" in options else len(options)]):
        raise Refused(400, "--fix and --fix-only are not options of the form: the page asks what the start lacks and "
                           "names the fixes itself")
    args += options
    older = newer_needed(host, args)  # an option value that host's sandbox-ai does not know (--ask allow)
    if older:
        raise older
    return args


def terminal_command(query, runner=None):
    """argv of a terminal the browser opens (QUERY: the WebSocket URL's parameters): attach to a start, a shell in a
    running one, or a new start of an agent (in its own terminal, detachable as every start)."""
    hosts = module("hosts")
    host = host_name(query.get("host", ""))
    kind = query.get("kind")
    if kind == "attach":
        return host, hosts.argv(host, ["attach", start_name(query.get("name"))], tty=True)
    if kind == "shell":  # a shell of the human in a running start, under the agent's rules (sandbox-ai shell)
        args = ["shell", start_name(query.get("name"))]
        older = newer_needed(host, args)
        if older:
            raise older
        return host, hosts.argv(host, args, tty=True)
    if kind == "start":
        args = start_args(host, query, runner)
        return host, hosts.argv(host, args, tty=True)
    if kind == "update":
        # The host's update where sudo may ask for its password (hosts update said it wants one).
        version = query.get("version") or None
        if version is not None and not hosts.VERSION.fullmatch(version):
            raise Refused(400, "version is X.Y.Z")
        if host == hosts.LOCAL:
            raise Refused(400, "this machine updates with: sandbox-ai update (in a terminal of its own)")
        which = query.get("installation") or None
        if which not in (None, "user", "system"):
            raise Refused(400, "installation is user or system")
        try:
            return host, hosts.update_terminal(host, version, **({"installation_": which} if which else {}))
        except ValueError as exc:
            raise Refused(400, printable(exc)) from None
    if kind == "uninstall":
        # The host's uninstall where sudo may ask for its password (hosts uninstall said it wants one).
        if host == hosts.LOCAL:
            raise Refused(400, "this machine: sandbox-ai uninstall (in a terminal of its own)")
        if host not in hosts_file():
            raise Refused(404, "no such host", code="no-host", params={"host": host})
        if query.get("confirm") != "1":  # asked in the page first, as the uninstall route asks (host-uninstall)
            raise Refused(400, f"{NEEDS_CONFIRM['host-uninstall']}: confirm=1", confirm="host-uninstall")
        which = query.get("installation") or None
        if which not in (None, "user", "system"):
            raise Refused(400, "installation is user or system")
        flags = {key: query.get(key) == "1" for key in ("purge", "secrets", "mcp_data")}
        try:
            return host, hosts.uninstall_terminal(host, which, flags["purge"], flags["secrets"], flags["mcp_data"])
        except ValueError as exc:
            raise Refused(400, printable(exc)) from None
    if kind == "install":
        # The host's install where sudo may ask for its password (hosts install said it wants one): the local
        # sandbox-ai's hosts install in this terminal, which asks there (ssh -tt ... sudo).
        if host == hosts.LOCAL:
            raise Refused(400, "this machine installs with install.sh itself")
        if host not in hosts_file():
            raise Refused(404, "no such host", code="no-host", params={"host": host})
        if query.get("confirm") != "1":  # asked in the page first, as the install route asks (host-install)
            raise Refused(400, f"{NEEDS_CONFIRM['host-install']}: confirm=1", confirm="host-install")
        sudo = {"1": True, "0": False, None: None, "": None}.get(query.get("sudo"), "bad")
        if sudo == "bad":
            raise Refused(400, "sudo is 1, 0 or not given")
        choice = install_choice({key: query[key] for key in ("mode", "prefix", "source", "version") if query.get(key)}
                                | ({"sudo": sudo} if sudo is not None else {}))
        return host, hosts.argv(hosts.LOCAL, install_argv(host, choice, as_json=False), tty=True)
    args = tool_command(kind, query, runner)
    if args is None:
        raise Refused(400, "kind is attach, start, update, install, uninstall, login, plugins, build, trust-hooks, "
                           "team-start, demo-device or install-items")
    older = newer_needed(host, args)  # an option that host's sandbox-ai does not know: said, not run
    if older:
        raise older
    return host, hosts.argv(host, args, tty=True)


def terminal_confirmed(query, action, detail):
    """A terminal that NEEDS_CONFIRM ACTION: its query carries confirm=1 (the page asked first, naming the host)."""
    if query.get("confirm") != "1":
        raise Refused(400, f"{detail}: {NEEDS_CONFIRM[action]}: confirm=1", confirm=action)


def team_vpn_profiles(host, runner=None):
    """{name: {"profile", "type", "provider", "ephemeral"}} of `vpn list --json` on HOST (RUNNER: Api.runner's
    form, default hosts.run): what a new team's form offers, and what its --vpn and --member-vpn are checked against.
    An entry whose name is no profile name (agent_sandbox.VPN_PROFILE_NAME: never a leading "-") is left out.
    Refused when the listing fails."""
    runner = runner or (lambda name, args, timeout=120, input=None:
                        module("hosts").run(name, args, input=input, timeout=timeout))
    try:
        status, out, err = runner(host, ["vpn", "list", "--json"], timeout=30)
    except ValueError as exc:  # hosts.run: no such host, or hosts.json cannot be read
        raise Refused(400, printable(exc)[:600]) from None
    try:
        data = json.loads(out) if status == 0 else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, list):
        said = printable((err or out or "").strip())[-300:] or f"exit status {status}"
        raise Refused(502, f"the VPN profiles of {host} could not be listed: {said}")
    pattern, found = module("agent_sandbox").VPN_PROFILE_NAME, {}
    for entry in data:
        if not (isinstance(entry, dict) and isinstance(entry.get("profile"), str)
                and re.fullmatch(pattern, entry["profile"])):
            continue
        word = {key: printable(entry[key])[:32] if isinstance(entry.get(key), str) else "?"
                for key in ("type", "provider")}
        found[entry["profile"]] = {"profile": entry["profile"], **word, "ephemeral": entry.get("ephemeral") is True}
    return found


def team_vpn_args(host, agents, query, runner=None):
    """The VPN options of a team start from QUERY: vpn (the profile in front of every member), member_vpn (a JSON
    object {agent: profile}: another one for a member of AGENTS) and shared_openvpn (1: one OpenVPN profile in
    several members, confirmed). Each profile is one of HOST's (team_vpn_profiles, asked only when one is named);
    whether one may run in several members is team start's to say (vpn.sharing), with its own reason."""
    def profile_name(value):
        if not (isinstance(value, str) and re.fullmatch(module("agent_sandbox").VPN_PROFILE_NAME, value)):
            raise Refused(400, "a VPN profile name is 1-32 letters, digits, '_' or '-', starting with a letter or "
                               "digit")
        return value

    vpn = query.get("vpn") or None
    try:
        own = json.loads(query.get("member_vpn") or "{}")
    except (ValueError, RecursionError):
        own = None
    if not isinstance(own, dict) or not all(isinstance(value, str) for value in own.values()):
        raise Refused(400, "member_vpn is a JSON object {agent: profile}")
    for agent in own:
        if agent not in agents:
            raise Refused(400, f"member_vpn: {printable(agent)[:40]} is no member of this team ({', '.join(agents)})")
    own = {agent: profile_name(profile) for agent, profile in own.items() if profile}
    shared = {"1": True, "0": False, None: False, "": False}.get(query.get("shared_openvpn"), "bad")
    if shared == "bad":
        raise Refused(400, "shared_openvpn is 1 or 0")
    named = [profile_name(vpn)] if vpn is not None else []
    named += own.values()
    if named:
        known = team_vpn_profiles(host, runner)
        missing = next((profile for profile in named if profile not in known), None)
        if missing:
            raise Refused(400, f"no VPN profile {missing} on {host}; see: sandbox-ai vpn list")
    return [*([f"--vpn={vpn}"] if vpn else []), *[f"--member-vpn={agent}={profile}" for agent, profile in own.items()],
            *(["--allow-shared-openvpn"] if shared else [])]


TEAM_MEMBER_KEYS = {"model": "--member-model", "llm": "--member-llm", "mode": "--member-mode"}


def team_member_args(agents, query):
    """Each member's role and its own model, LLM server and mode from QUERY: roles (a JSON object {agent: text}) and
    member_options (a JSON object {agent: {model, llm, mode}}), for members of AGENTS: --role=AGENT=TEXT,
    --member-model=AGENT=MODEL ... Their values are team start's to check (team.check_roles, check_member_options);
    here only their shape and that they are strings of one line."""
    def json_object(key):
        try:
            value = json.loads(query.get(key) or "{}")
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            raise Refused(400, f"{key} is a JSON object {{agent: ...}}")
        for agent in value:
            if agent not in agents:
                raise Refused(400, f"{key}: {printable(agent)[:40]} is no member of this team ({', '.join(agents)})")
        return value

    def line(value, what):
        if not isinstance(value, str) or not value.isprintable() or len(value) > 1000:
            raise Refused(400, f"{what} is one line of text (at most 1000 characters)")
        return value.strip()

    args = [f"--role={agent}={line(text, 'a role')}" for agent, text in json_object("roles").items()
            if line(text, "a role")]
    for agent, own in json_object("member_options").items():
        if not isinstance(own, dict) or set(own) - set(TEAM_MEMBER_KEYS):
            raise Refused(400, "member_options: {agent: {model, llm, mode}}")
        args += [f"{TEAM_MEMBER_KEYS[key]}={agent}={line(value, key)}" for key, value in own.items()
                 if line(value, key)]
    return args


def tool_command(kind, query, runner=None):
    """The sandbox-ai arguments of the interactive tools a terminal runs (None: no such kind): an agent's login (or
    its logout, asked first), the plugin store, an image build, a kit's hooks trusted (asked first; the command
    shows them and asks again) or revoked, the demo device. Every value is checked; nothing starts with "-"."""
    if kind == "login":
        agent = query.get("agent")
        if agent not in LOGIN_AGENTS:
            raise Refused(400, agent_words(LOGIN_AGENTS))
        logout = {"1": True, "0": False, None: False, "": False}.get(query.get("logout"), "bad")
        if logout == "bad":
            raise Refused(400, "logout is 1 or 0")
        if logout:
            terminal_confirmed(query, "login-logout", agent)
        return ["login", *(["--logout"] if logout else []), agent]
    if kind == "wizard":
        # the project wizard, interactive in its terminal (review 5 W6): a new project file (it asks for the folder),
        # or that of PATH changed; it shows what it writes and trusts and asks there
        path = query.get("path") or ""
        return ["wizard", "edit", clean_path(path, "path")] if path else ["wizard", "new"]
    if kind == "plugins":
        agent = query.get("agent") or "claude"
        if agent not in PLUGIN_AGENTS:
            raise Refused(400, agent_words(PLUGIN_AGENTS))
        update = {"1": True, "0": False, None: False, "": False}.get(query.get("update"), "bad")
        if update == "bad":
            raise Refused(400, "update is 1 or 0")
        kits = [k for k in (query.get("kits") or "").split(",") if k]
        if len(kits) > 8 or not all(KIT_NAME.fullmatch(k) for k in kits):
            raise Refused(400, "kits: names of built-in kits, comma-separated")
        only = {"1": True, "0": False, None: False, "": False}.get(query.get("marketplaces_only"), "bad")
        if only == "bad":
            raise Refused(400, "marketplaces_only is 1 or 0")
        return ["plugins", *(["--update"] if update else []), *(["--marketplaces-only"] if only else []),
                *[f"--kit={k}" for k in kits], agent]
    if kind == "build":
        target = query.get("target")
        if not (isinstance(target, str) and BUILD_TARGET.fullmatch(target)):
            raise Refused(400, "target is codex, claude, opencode, opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi, all, team, cairn, artex, vpn, tor, browser "
                                "or a "
                                "variant's name")
        only = {"1": True, "0": False, None: False, "": False}.get(query.get("extension_only"), "bad")
        if only == "bad" or only and target in ("team", "vpn", "tor"):
            raise Refused(400, "extension_only is 1 or 0, and not for team, vpn or tor")
        # --pin VERSION (one agent's own version), --uv TAG, --node IMAGE (the base images): as build checks them
        pin, uv, node = (query.get(key) or "" for key in ("pin", "uv", "node"))
        if pin and not (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin) and target in AGENTS):
            raise Refused(400, f"pin is X.Y.Z, for one agent's image ({', '.join(AGENTS)})")
        if uv and not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", uv) or \
                node and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]{0,255}", node):
            raise Refused(400, "uv is an image tag, node an image name (with a tag or an @sha256 digest)")
        if (uv or node) and target in ("vpn", "tor"):
            raise Refused(400, "uv and node are for the agents' images, not vpn or tor")
        more = [*(["--pin", pin] if pin else []), *(["--uv", uv] if uv else []), *(["--node", node] if node else [])]
        return ["build", *more, *(["--extension-only"] if only else []), *(["--variant"] if "@" in target else []),
                target]
    if kind == "trust-hooks":
        kit = query.get("kit")
        if not (isinstance(kit, str) and KIT_NAME.fullmatch(kit)):
            raise Refused(400, "kit is a kit's name (or default)")
        revoke = {"1": True, "0": False, None: False, "": False}.get(query.get("revoke"), "bad")
        agents = [a for a in (query.get("agents") or "").split(",") if a]
        if revoke == "bad" or not all(a in AGENTS for a in agents):
            raise Refused(400, "revoke is 1 or 0; agents: claude, codex, opencode (comma-separated)")
        if not revoke:
            terminal_confirmed(query, "kit-hooks-trust", kit)
        return ["kits", "trust-hooks", *[f"--agent={a}" for a in dict.fromkeys(agents)],
                *(["--revoke"] if revoke else []), "--", kit]
    if kind == "team-start":
        # sandbox-ai team start --agents=A,B[,...] --workspace=DIR [--vpn=PROFILE] [--member-vpn=AGENT=PROFILE]...
        # [--allow-shared-openvpn] NAME -- OPTIONS: the first agent leads; the options (as a start's: strings, never
        # a shell line) hold for every member
        name = query.get("team")
        if not (isinstance(name, str) and TEAM_NAME.fullmatch(name)):
            raise Refused(400, "a team's name is 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit",
                          code="bad-name", params={"what": "team"})
        agents, members = (query.get("agents") or "").split(","), team_agents()
        if len(agents) < 2 or len(set(agents)) != len(agents) or not set(agents) <= set(members):
            raise Refused(400, f"agents: two or more of {', '.join(members)}, comma-separated; the first leads")
        workspace = query.get("workspace") or ""
        if not workspace.startswith(("/", "~")) or "\0" in workspace:  # never the service's own directory (team's ".")
            raise Refused(400, "the shared workspace is an absolute path (or ~/...)")
        try:
            options = strings(json.loads(query.get("options") or "[]"), "options")
        except (ValueError, RecursionError):
            raise Refused(400, "options is a JSON list of strings") from None
        hosts = module("hosts")
        host = host_name(query.get("host") or hosts.LOCAL)
        vpns = team_vpn_args(host, agents, query, runner)
        vpns += team_member_args(agents, query)
        # Every member's /work is the team's workspace (team.py passes --workspaces-as-given itself from 0.1.78 on;
        # a remote host of that version whose team.py is older gets it here).
        given = ["--workspaces-as-given"] if host != hosts.LOCAL and as_given_on(host, runner) \
            and "--workspaces-as-given" not in options else []
        # NAME after the options: 0.1.76 and 0.1.77 took everything after NAME as NAME's own (their team start
        # refused "--agents" then); this order works on every version, as the app's does.
        return ["team", "start", f"--agents={','.join(agents)}", f"--workspace={workspace}", *vpns, name,
                "--", *given, *options]
    if kind == "demo-device":
        port = query.get("port")
        if port not in (None, ""):
            port = whole(port, "port", 1024, 65535)
        return ["demo-device", "start", *([f"--port={port}"] if port not in (None, "") else [])]
    if kind == "install-items":
        # sandbox-ai install ITEM...: each item's own command (build, mcp add, toolchain add) in this terminal, its
        # questions asked there (a pending MCP server's build); signed entries only (install.py)
        items = [i for i in (query.get("items") or "").split(",") if i]
        if not items or len(items) > INSTALL_ITEMS_LIMIT or not all(INSTALL_ITEM.fullmatch(i) for i in items):
            raise Refused(400, f"items: 1 to {INSTALL_ITEMS_LIMIT} of KIND:NAME (image:claude, mcp:duckduckgo, "
                               "toolchain:go, preset:python), comma-separated")
        return ["install", *dict.fromkeys(items)]
    return None


# --- WebSocket (RFC 6455) ---------------------------------------------------------------------------------

def unmask(data, mask):
    key = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(len(data), "big")


class WebSocket:
    """A server end over a connected (TLS) socket: frames from the browser must be masked; fragmented
    messages are put together; ping is answered; a message larger than FRAME_LIMIT closes with 1009."""

    def __init__(self, stream, hang_up=None):
        self.stream, self.send_lock, self.closed = stream, threading.Lock(), False
        self.hang_up = hang_up  # ends the connection when the browser does not answer a close

    def read_exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.stream.read(count - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def receive(self):
        """(opcode, payload) of the next message (1 text, 2 binary, 8 close)."""
        message, kind = b"", None
        while True:
            first, second = self.read_exact(2)
            fin, opcode, masked, length = first & 0x80, first & 0x0F, second & 0x80, second & 0x7F
            if first & 0x70 or not masked:
                self.close(1002)
                raise ConnectionError("protocol error")
            if length == 126:
                (length,) = struct.unpack(">H", self.read_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self.read_exact(8))
            if length > FRAME_LIMIT or len(message) + length > FRAME_LIMIT:
                self.close(1009, f"a message larger than {FRAME_LIMIT >> 20} MiB")
                raise ConnectionError("message too big")
            mask = self.read_exact(4)
            payload = unmask(self.read_exact(length), mask) if length else b""
            if opcode >= 8:  # control frames: never fragmented, at most 125 bytes
                if not fin or length > 125:
                    self.close(1002)
                    raise ConnectionError("protocol error")
                if opcode == 9:
                    self.send(payload, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode == 8:
                    self.close(1000)
                    return 8, payload
                self.close(1002)
                raise ConnectionError("protocol error")
            if opcode == 0 and kind is None or opcode in (1, 2) and kind is not None or opcode > 2:
                self.close(1002)
                raise ConnectionError("protocol error")
            kind = kind or opcode
            message += payload
            if fin:
                return kind, message

    def send(self, payload, opcode=2):
        """One frame; nothing after the close frame (opcode 8, sent once) or a failed write. Checked under the send
        lock: checked before it, another thread's frame could follow the close frame, or a second close frame."""
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 1 << 16:
            header += bytes([126]) + struct.pack(">H", length)
        else:
            header += bytes([127]) + struct.pack(">Q", length)
        with self.send_lock:
            if self.closed:
                return
            if opcode == 8:
                self.closed = True
            try:
                self.stream.write(header + payload)
                self.stream.flush()
            except (OSError, ValueError):
                self.closed = True

    def close(self, code=1000, reason=""):
        """A close frame with CODE and REASON, which the page shows (UTF-8, cut to the 123 bytes a frame has); only
        the first one is sent."""
        text = reason.encode()[:123].decode(errors="ignore").encode()
        self.send(struct.pack(">H", code) + text, 8)


def terminal_size(query):
    """(cols, rows) of a terminal's WebSocket URL parameters: 1-1000 each, else 100 and 30."""
    def one(key, default):
        value = query.get(key) or ""
        return min(1000, max(1, int(value))) if re.fullmatch(r"[0-9]{1,6}", value) else default
    return one("cols", 100), one("rows", 30)


def bridge(ws, argv, cols=100, rows=30, alive=None, every=2.0, clock=time.monotonic, used=None):
    """ARGV in a terminal of its own, its output to WS (binary frames), WS's binary frames to its input, and
    {"resize": [cols, rows]} as text frames; ends when either side does. The command's process group gets SIGHUP
    at the end: an attach then simply detaches, the start runs on. USED() is called when the user types (at most
    once a second): typing is use of the session, the output is not."""
    pid, master = pty.fork()
    if pid == 0:  # the child: the command, in the new terminal
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    # From here on the child is always ended and reaped. MASTER is closed by the last of the two threads that use
    # it: closed earlier, its number could be another connection's by the time the other thread reads or writes.
    users, users_lock, ending, reader = [2], threading.Lock(), threading.Event(), None

    def let_go():
        with users_lock:
            users[0] -= 1
            last = users[0] == 0
        if last:
            with contextlib.suppress(OSError):
                os.close(master)

    checked, pinged, typed = [clock()], [clock()], [None]

    def gone():
        """True once ALIVE says the session ended (logout, its idle or 12-hour limit, its user removed or reset):
        an open terminal ends with it. Asked every EVERY seconds, not at each key."""
        if alive is None or clock() - checked[0] < every:
            return False
        checked[0] = clock()
        return not alive()

    def write(data):
        """DATA to the command's input, False when that cannot be: it ended, or it takes nothing for WS_DEADLINE
        seconds (a blocking write would hold this thread, and so the terminal, for as long as it reads nothing)."""
        view, since = memoryview(data), clock()
        while view:
            if ws.closed:
                return False
            try:
                view = view[os.write(master, view):]
                since = clock()
            except BlockingIOError:
                if clock() - since > WS_DEADLINE:
                    return False
                ready_fds((), [master], 0.5)
            except OSError:
                return False
        return True

    def output():
        try:
            while True:
                try:
                    ready = ready_fds([master], (), 0.5)[0]
                    if ws.closed or ending.is_set():
                        break
                    if gone():
                        ws.close(1008, SESSION_ENDED)
                        break
                    if clock() - pinged[0] >= WS_PING:  # a browser that is still there answers (the read deadline)
                        pinged[0] = clock()
                        ws.send(b"", 9)
                    if not ready:
                        continue
                    try:
                        data = os.read(master, 65536)
                    except BlockingIOError:
                        continue
                except (OSError, ValueError):
                    break
                if not data:
                    break
                ws.send(data, 2)
            ws.close(1000)
            if ws.hang_up:  # the browser answers the close at once; if not, the connection ends anyway
                threading.Timer(3, ws.hang_up).start()
        finally:
            let_go()

    try:
        os.set_blocking(master, False)
        set_size(master, min(1000, max(1, int(cols))), min(1000, max(1, int(rows))))
        reader = threading.Thread(target=output, daemon=True)
        reader.start()
        while not ws.closed:
            try:
                kind, payload = ws.receive()
            except (ConnectionError, OSError):
                break
            if kind == 8:
                break
            if gone():
                ws.close(1008, SESSION_ENDED)
                break
            if kind == 2:
                if not write(payload):
                    break
                if used is not None and payload and (typed[0] is None or clock() - typed[0] >= 1):
                    typed[0] = clock()
                    used()
            elif kind == 1:
                with contextlib.suppress(ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    message = json.loads(payload)
                    cols, rows = (int(v) for v in message["resize"])
                    if 1 <= cols <= 1000 and 1 <= rows <= 1000:
                        set_size(master, cols, rows)
    finally:
        ending.set()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGHUP)
        if reader is None:
            let_go()  # the output thread's share: it never started
        else:
            reader.join(5)
        let_go()
        with contextlib.suppress(ChildProcessError):
            for _ in range(50):
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                time.sleep(0.1)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# --- the HTTP service -------------------------------------------------------------------------------------

class Service:
    """Everything but the sockets: the HTTP handler hands each request to handle() (tests call it directly)."""

    def __init__(self, names, api=None, limiter=None, sessions=None, terminal=None):
        self.names = {name.lower() for name in names}  # what the Host header may say
        self.api, self.limiter = api or Api(), limiter or Limiter()
        self.login_lock = threading.Lock()
        self.sessions = sessions or Sessions()
        self.terminal = terminal or (lambda query: terminal_command(query, runner=self.api.runner))

    @staticmethod
    def cookie(headers):
        for part in (headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE:
                return value
        return None

    def host_ok(self, headers):
        host = (headers.get("Host") or "").strip().lower()
        return bool(host) and host in self.names

    def same_origin(self, headers):
        origin = headers.get("Origin")
        if origin is None:
            referer = headers.get("Referer") or ""
            parsed = urllib.parse.urlsplit(referer)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else None
        return origin is not None and origin.lower() == "https://" + (headers.get("Host") or "").lower()

    def handle(self, method, target, headers, body, address):
        """(status, headers, body bytes) of one request. Every answer to a request of a live session says how long
        it lives without use (X-Session-Idle-Left, seconds; the page's background polls do not count as use) and at
        most (X-Session-Lifetime-Left): the page warns before the end and offers to stay logged in."""
        seen = []
        try:
            status, out_headers, data = self.respond(method, target, headers, body, address, seen)
        except Exception as exc:  # noqa: BLE001 - an error nobody expected: a 500 with its kind, never no answer
            LOG.error("webui", f"{method} {urllib.parse.urlsplit(target).path} failed unexpectedly (500)", exc)
            LOG.debug("webui", "where: " + " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
                for frame in reversed(__import__("traceback").extract_tb(exc.__traceback__)[-6:])))
            with contextlib.suppress(Exception):
                audit("error", address=address, method=method, path=urllib.parse.urlsplit(target).path,
                      why=f"{type(exc).__name__}: {printable(exc)[:300]}")
            return self.error(500, f"the service failed: {type(exc).__name__}")
        if seen and self.sessions.get(self.cookie(headers), touch=False) is seen[0]:
            idle, lifetime = self.sessions.left(seen[0])
            out_headers = [*out_headers, ("X-Session-Idle-Left", str(idle)),
                           ("X-Session-Lifetime-Left", str(lifetime))]
        return status, out_headers, data

    def respond(self, method, target, headers, body, address, seen):
        if not self.host_ok(headers):
            return self.error(421, "unknown host name; start the service with --hostname NAME for it")
        path = urllib.parse.urlsplit(target).path
        if method == "GET" and not path.startswith("/api/"):
            return self.static(path, headers)
        session = None
        try:
            try:
                users = load_users()
            except ValueError as exc:
                raise users_unreadable(exc) from None
            # The page's polls (approvals, the running starts) say X-Background: 1. They are no use of the page by
            # its user: a tab left open would otherwise keep its session from ever reaching its idle limit.
            session = self.sessions.current(self.cookie(headers), users,
                                            touch=(headers.get("X-Background") or "").strip() != "1")
            if method != "GET":
                if not self.same_origin(headers):
                    raise Refused(403, "a change must come from this page")
                if (headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                    raise Refused(415, "send JSON")
            data = None
            if body:
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError, RecursionError):  # nested too deep: RecursionError
                    raise Refused(400, "invalid JSON") from None
            if path == "/api/login" and method == "POST":
                return self.login(data if isinstance(data, dict) else {}, address)
            if session is None:
                raise Refused(401, "log in", code="login-needed")
            seen.append(session)
            if method != "GET" and not hmac.compare_digest(headers.get("X-CSRF-Token") or "", session["csrf"]):
                raise Refused(403, "the page is out of date: reload it", code="page-outdated")
            if path == "/api/me":
                return self.json(200, {"user": session["user"], "csrf": session["csrf"],
                                       "version": self.version(), "terminals": vendored()})
            if path == "/api/logout" and method == "POST":
                self.sessions.drop(self.cookie(headers))
                audit("logout", user=session["user"], address=address)
                return self.json(200, {"ok": True}, [("Set-Cookie", self.set_cookie("", 0))])
            REQUEST.address = address
            result = self.api.dispatch(method, path, data)
            if method != "GET":
                LOG.info("webui", f"{session['user']} from {address}: {method} {path}: done")
                audit("action", user=session["user"], address=address, method=method, path=path,
                      body=redacted(data))
            return self.json(200, result)
        except Refused as exc:
            LOG.log("INFO" if method != "GET" else "DEBUG", "webui",
                    f"{session['user'] if session else 'nobody logged in'} from {address}: {method} {path}: refused "
                    f"({exc.status}): {printable(exc)[:300]}")
            if method != "GET" and session is not None:
                audit("refused", user=session["user"], address=address, method=method, path=path, why=str(exc))
            return self.error(exc.status, str(exc), confirm=exc.confirm, code=exc.code, params=exc.params)
        # What no route turned into a Refused: an answer all the same.
        except (OSError, ValueError, RecursionError) as exc:
            why = printable(f"{type(exc).__name__}: {exc}")[:600]
            LOG.warning("webui", f"{method} {path} failed (500): {why}")
            with contextlib.suppress(OSError):
                audit("failed", user=session and session["user"], address=address, method=method, path=path, why=why)
            return self.error(500, f"the service could not do that: {why}")

    def login(self, data, address):
        user, password, code = data.get("user"), data.get("password"), data.get("code")
        if not all(isinstance(v, str) and 0 < len(v) <= 1024 for v in (user, password)) or \
                not (isinstance(code, str) and len(code) <= 1024):
            raise Refused(400, "user, password and code are needed")  # (a login without TOTP: an empty code)
        # ADDRESS is the socket's peer (Handler.answer): no X-Forwarded-For is read, there is no trusted proxy to
        # take it from, and a header any client can send would let it choose the address it is throttled as.
        local = (f"address:{address}", f"address-user:{address}|{user}")
        # One login check at a time: parallel tries all passed wait() before any failure was counted, so each got a
        # full password and TOTP check (thousands of codes per lockout). scrypt makes one check take ~0.1 s.
        with self.login_lock:
            return self._login(user, password, code, local, f"user:{user}", address)

    def _login(self, user, password, code, local, user_key, address):
        """LOCAL: the keys of this address (and of this address with this user), whose wait is always kept; USER_KEY
        throttles the user from every address. Only that one may not lock the user out: whoever knows the name could
        keep it locked. While it waits, a try is still checked, but each failure then locks its address at once (one
        guess per address and wait, not FREE), and the right password with its code gets in."""
        wait = self.limiter.wait(*local)
        if wait:
            audit("login-throttled", user=printable(user)[:64], address=address)
            return self.error(429, f"too many failed logins: try again in {int(wait) + 1} seconds",
                              [("Retry-After", str(int(wait) + 1))], code="too-many-logins",
                              params={"seconds": str(int(wait) + 1)})
        user_waits = self.limiter.wait(user_key) > 0
        try:
            with users_locked() as users:
                record = users.get(user) if USER_NAME.fullmatch(user) else None
                ok = check_password(password, (record or {}).get("password"))
                second = bool(ok and record.get("totp"))  # False: a login you made without TOTP (--no-totp)
                counter = (check_totp(record["totp"], code, record.get("totp_counter")) if second else
                           "password only") if ok else None
                if second and counter is not None:
                    record["totp_counter"] = counter
        except ValueError as exc:
            raise users_unreadable(exc) from None
        if not ok or counter is None:
            self.limiter.failed(*local, at_least=self.limiter.free if user_waits else 0)
            self.limiter.failed(user_key)
            audit("login-failed", user=printable(user)[:64], address=address, **({"user_throttled": True}
                                                                                  if user_waits else {}))
            raise Refused(401, "wrong user, password or code")
        self.limiter.succeeded(*local, user_key)
        token = self.sessions.new(user, address, user_fingerprint(record))
        audit("login", user=user, address=address, **({} if second else {"second_factor": False}))
        session = self.sessions.get(token)
        return self.json(200, {"user": user, "csrf": session["csrf"], "second_factor": second},
                         [("Set-Cookie", self.set_cookie(token, LIFETIME))])

    def claim_terminal(self, session):
        """One of the session's TERMINALS_PER_SESSION terminals, checked and counted at once (False: none left);
        two browser tabs opening terminals at the same moment must not both pass the check."""
        with self.sessions.lock:
            if session["terminals"] >= TERMINALS_PER_SESSION:
                return False
            session["terminals"] += 1
            return True

    def release_terminal(self, session):
        with self.sessions.lock:
            session["terminals"] -= 1

    @staticmethod
    def set_cookie(token, age):
        return f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}"

    def websocket(self, target, headers, address):
        """(argv, session) for a terminal WebSocket at TARGET, or Refused."""
        if not self.host_ok(headers):
            raise Refused(421, "unknown host name")
        if not self.same_origin(headers):
            raise Refused(403, "a terminal must come from this page")
        try:
            users = load_users()
        except ValueError as exc:
            raise users_unreadable(exc) from None
        session = self.sessions.current(self.cookie(headers), users)
        if session is None:
            raise Refused(401, "log in")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
        if not hmac.compare_digest(query.get("csrf", ""), session["csrf"]):
            raise Refused(403, "the page is out of date: reload it")
        try:
            host, argv = self.terminal(query)
        except (ValueError, RecursionError) as exc:  # options: JSON nested too deep is a RecursionError
            raise Refused(400, printable(exc)[:300]) from None
        installing = getattr(self.api, "installing", {})
        if query.get("kind") == "install" and host in installing:  # one install per host: the route's runs now
            since = round(time.monotonic() - installing[host])
            raise Refused(409, f"sandbox-ai is being installed on {host} (for {since} s): wait for its end",
                          code="busy", params={"host": host})
        if session["terminals"] >= TERMINALS_PER_SESSION:
            raise Refused(429, f"at most {TERMINALS_PER_SESSION} terminals at once")
        # What runs: a start's agent, workspace and options too (terminal_command checked them; strings only).
        started = {}
        if query.get("kind") in ("start", "team-start"):
            try:
                options = redacted(json.loads(query.get("options") or "[]"))
            except (ValueError, RecursionError):
                options = printable(query.get("options"))[:500]
            started = {"agent": query.get("agent") or query.get("agents"), "workspace": query.get("workspace"),
                       "options": options}
        audit("terminal", user=session["user"], address=address, host=host,
              kind=query.get("kind"), name=query.get("name") or query.get("team") or query.get("agent")
              or query.get("target") or query.get("kit"), **started)
        return argv, session

    @staticmethod
    def version():
        try:
            return module("agent_sandbox").__version__
        except Exception:  # noqa: BLE001 - the page works without it
            return None

    def static(self, path, headers=None):
        name = "index.html" if path in ("/", "/index.html") else path[len("/static/"):] \
            if path.startswith("/static/") else None
        if name in STATIC_FILES:
            file, kind = STATIC / name, STATIC_FILES[name]
        elif name and name.startswith("fonts/") and name[len("fonts/"):] in FONT_FILES:
            file, kind = STATIC / name, FONT_FILES[name[len("fonts/"):]]
        elif name and name.startswith("vendor/") and name[len("vendor/"):] in VENDOR_FILES:
            file, kind = vendor_dir() / name[len("vendor/"):], VENDOR_FILES[name[len("vendor/"):]]
        else:
            return self.error(404, "not found")
        try:
            data = file.read_bytes()
        except OSError:
            return self.error(404, "not found")
        # no-cache: the browser asks each time, and an unchanged file is answered with 304 and no body (xterm.js
        # alone is about 300 KB per terminal view; without an ETag it was sent again on every page load).
        tag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        if headers is not None and tag in [t.strip() for t in str(headers.get("If-None-Match") or "").split(",")]:
            return 304, [("ETag", tag), ("Cache-Control", "no-cache")], b""
        return 200, [("Content-Type", kind), ("Cache-Control", "no-cache"), ("ETag", tag)], data

    def json(self, status, data, extra=()):
        return status, [("Content-Type", "application/json"), ("Cache-Control", "no-store"), *extra], \
            json.dumps(data).encode()

    def error(self, status, message, extra=(), confirm=None, code=None, params=None):
        """{"error": MESSAGE}, "confirm": the NEEDS_CONFIRM action a refusal names (the page asks for it), and "code"
        with its "params": which common refusal it is (ERROR_CODES), for the page's own words."""
        return self.json(status, {"error": message, **({"confirm": confirm} if confirm else {}),
                                  **({"code": code, "params": params or {}} if code else {})}, extra)


# Fields whose values the audit log leaves out, by their exact name (lower case, "-" as "_"), and names ending in
# "_" and one of SECRET_ENDINGS. Not a search for "key" in a name: a defaults change ({"key": "telemetry"}) and the
# like must stay readable in the log.
SECRET_FIELDS = {"password", "passphrase", "passwd", "code", "otp", "totp", "secret", "token", "csrf", "cookie",
                 "authorization", "api_key", "apikey", "auth_key", "private_key", "secret_key", "access_token",
                 "refresh_token", "client_secret", "session"}
SECRET_ENDINGS = ("password", "secret", "token", "api_key", "private_key")


def secret_field(name):
    name = str(name).lower().replace("-", "_")
    return name in SECRET_FIELDS or name.endswith(tuple("_" + ending for ending in SECRET_ENDINGS))


def redacted(data):
    """DATA for the audit log: the values of fields that hold secrets (secret_field) replaced."""
    if isinstance(data, dict):
        return {k: "***" if secret_field(k) else redacted(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redacted(v) for v in data[:64]]
    return data if not isinstance(data, str) else data[:500]


# xterm.js for the terminals (MIT): the versions and SRI hashes the desktop app pins (its vendor/xterm.json).
XTERM = {"@xterm/xterm": ("6.0.0", "sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kI"
                                   "M2WD3sMOYg==", {"package/lib/xterm.js": "xterm.js", "package/css/xterm.css":
                                                    "xterm.css", "package/LICENSE": "LICENSE.xterm"}),
         "@xterm/addon-fit": ("0.11.0", "sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19Vw"
                                        "Qei89QCiZZP86DCPF7g==", {"package/lib/addon-fit.js": "addon-fit.js",
                                                                 "package/LICENSE": "LICENSE.addon-fit"})}


def vendor_dir():
    """Where `sandbox-ai web vendor` puts xterm.js (outside the installation, which updates replace)."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "web-vendor"


def vendor(fetch=None, out=print):
    """xterm.js and its fit add-on from registry.npmjs.org, each tarball checked against its pinned SHA-512
    before a file of it is used; only the listed files are taken from it."""
    import io
    import tarfile
    import urllib.request
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=60).read(16 << 20))
    target = vendor_dir()
    for package, (version, integrity, files) in XTERM.items():
        url = f"https://registry.npmjs.org/{package}/-/{package.split('/')[-1]}-{version}.tgz"
        data = fetch(url)
        algorithm, _, expected = integrity.partition("-")
        if base64.b64encode(hashlib.new(algorithm, data).digest()).decode() != expected:
            raise ValueError(f"{package} {version}: the download does not match its pinned {algorithm}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member, name in files.items():
                entry = archive.getmember(member)
                if not entry.isfile() or entry.size > 8 << 20:
                    raise ValueError(f"{package}: {member} is not a plain file")
                write_private(target / name, archive.extractfile(entry).read())
        out(f"{package} {version}: checked, in {target}")
    return target


def vendored():
    return all((vendor_dir() / name).is_file() for name in VENDOR_FILES)


def loopback_address(address):
    """ADDRESS (a client's) is this machine's loopback (127.0.0.0/8, ::1, ::ffff:127.x)."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_loopback)


class Reaper:
    """Deadlines for whole reads, not single ones: a socket's timeout bounds each recv, so a client that sends a byte
    just before every timeout keeps one readline (or a TLS handshake) going without end. A socket armed here is shut
    down once its deadline passes (one thread for all of them), which ends whatever read waits on it."""

    def __init__(self):
        self.due, self.condition, self.thread = {}, threading.Condition(), None

    @contextlib.contextmanager
    def bounded(self, sock, deadline):
        """Within the block, SOCK is shut down at DEADLINE (time.monotonic()); TimeoutError when it was (or the block
        ended after it)."""
        with self.condition:
            self.due[sock] = deadline
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="web-deadlines", daemon=True)
                self.thread.start()
            self.condition.notify()
        try:
            yield
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise TimeoutError("the request took too long to arrive") from None
            raise
        finally:
            with self.condition:
                self.due.pop(sock, None)
        if time.monotonic() >= deadline:  # cut off while reading: what was read is no whole request
            raise TimeoutError("the request took too long to arrive")

    def run(self):
        while True:
            with self.condition:
                now = time.monotonic()
                expired = [sock for sock, deadline in self.due.items() if deadline <= now]
                for sock in expired:
                    del self.due[sock]
                if not expired:
                    soonest = min(self.due.values(), default=None)
                    self.condition.wait(None if soonest is None else max(0.01, soonest - now))
                    continue
            for sock in expired:
                with contextlib.suppress(OSError):
                    # the plain socket's shutdown (an SSLSocket's own would drop its TLS state under its reader)
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)


REAPER = Reaper()


def address_key(address):
    """What one client's connections are counted under (Server.per_address): its IPv4 address (also when written
    IPv4-mapped), or the /64 of its IPv6 address, which one client usually has whole."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return str(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if ip.version == 6 and not ip.is_loopback:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Deadline:
    """A reader of the connection's file (RFILE) that ends at DEADLINE (time.monotonic()) whatever the per-read
    timeout: every read waits at most what is left, none starts once it is over, and the connection is shut down when
    it passes during one (Reaper): TimeoutError."""

    def __init__(self, rfile, connection, deadline):
        self.rfile, self.connection, self.deadline = rfile, connection, deadline

    def bounded(self, read, *args):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request took too long to arrive")
        before = self.connection.gettimeout()
        self.connection.settimeout(min(left, before) if before else left)
        try:
            with REAPER.bounded(self.connection, self.deadline):
                return read(*args)
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(before)

    def readline(self, *args):
        return self.bounded(self.rfile.readline, *args)

    def read(self, *args):
        return self.bounded(self.rfile.read, *args)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "sandbox-ai", ""
    service = None  # set by serve()
    # TCP_NODELAY (socketserver sets it in setup): what is written goes out at once. With Nagle's algorithm a small
    # piece waited for the browser's ACK of the one before, which the browser's system holds back (40 ms on Linux, up
    # to 200 ms on Windows): a terminal's output frame behind the frame before it, an answer's body behind its headers.
    disable_nagle_algorithm = True

    def setup(self):
        handshake = getattr(self.request, "do_handshake", None)
        if handshake is not None:  # TLS, in this connection's thread, bounded: never the accept loop's
            self.request.settimeout(HEAD_DEADLINE)
            with REAPER.bounded(self.request, time.monotonic() + HEAD_DEADLINE):  # the whole handshake
                handshake()
        # Seconds the idle connection is kept: GUEST_KEEPALIVE until a request shows a session (keep_alive); a
        # terminal sets its own (WS_DEADLINE: it may be quiet for long, its browser answers the pings).
        self.timeout = GUEST_KEEPALIVE
        super().setup()

    def keep_alive(self, headers):
        """KEEPALIVE seconds for the next request once this one carried a live session or made one (a login's
        Set-Cookie with HEADERS), else GUEST_KEEPALIVE."""
        tokens = [self.service.cookie(self.headers)] + [
            value.split(";")[0].partition("=")[2] for key, value in headers
            if key == "Set-Cookie" and value.startswith(COOKIE + "=")]
        known = any(token and self.service.sessions.get(token, touch=False) for token in tokens)
        with contextlib.suppress(OSError):
            self.connection.settimeout(KEEPALIVE if known else GUEST_KEEPALIVE)

    def log_message(self, format, *args):  # noqa: A002 - the audit log has what matters
        pass

    def handle_one_request(self):
        """One request: the idle wait for its first byte is the connection's keep-alive time; from that byte on, its
        request line and headers have HEAD_DEADLINE seconds in all (however slowly they arrive)."""
        original = self.rfile
        peek = getattr(original, "peek", None)
        if peek is not None:
            try:
                peek(1)
            except (OSError, ValueError):  # idle too long, or gone
                self.close_connection = True
                return
        self.rfile = Deadline(original, self.connection, time.monotonic() + HEAD_DEADLINE)
        try:
            super().handle_one_request()
        finally:
            self.rfile = original

    def parse_request(self):
        """The headers, still within HEAD_DEADLINE of the request's first byte (handle_one_request); the request is
        then read and answered from the connection itself (its body has a deadline of its own)."""
        head = self.rfile
        try:
            return super().parse_request()
        finally:
            self.rfile = head.rfile if isinstance(head, Deadline) else head

    def send(self, status, headers, body):
        """The answer in one write: its status line and headers (which http.server writes when they end) are taken
        into a buffer and go out with the body. In two writes the body waited for the browser's ACK of the headers
        (Nagle's algorithm; see disable_nagle_algorithm): every request of the page took 42 ms and more, on this
        machine itself, and was two TLS records."""
        wire, self.wfile = self.wfile, io.BytesIO()
        try:
            self.send_response(status)
            for key, value in [*headers, ("Content-Length", str(len(body))), ("Content-Security-Policy", CSP),
                               ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                               ("X-Frame-Options", "DENY"), ("Cross-Origin-Opener-Policy", "same-origin"),
                               ("Strict-Transport-Security", "max-age=31536000")]:
                self.send_header(key, value)
            self.end_headers()
            head = self.wfile.getvalue()
        finally:
            self.wfile = wire
        self.wfile.write(head if self.command == "HEAD" else head + body)

    def answer(self):
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            self.send(*self.service.error(411, "send a Content-Length"))
            return
        try:
            size = int(length or 0)
        except ValueError:
            size = -1
        if not 0 <= size <= BODY_LIMIT:
            self.send(*self.service.error(413, "too big"))
            self.close_connection = True
            return
        body = Deadline(self.rfile, self.connection, time.monotonic() + BODY_DEADLINE).read(size) if size else b""
        status, headers, data = self.service.handle(self.command, self.path, self.headers, body,
                                                    self.client_address[0])
        self.send(status, headers, data)
        self.keep_alive(headers)

    def do_GET(self):  # noqa: N802
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.upgrade()
        self.answer()

    do_POST = do_DELETE = do_PUT = do_PATCH = answer  # noqa: N815

    def upgrade(self):
        key = self.headers.get("Sec-WebSocket-Key") or ""
        try:
            good = self.headers.get("Sec-WebSocket-Version") == "13" and len(base64.b64decode(key, validate=True)) == 16
        except ValueError:
            good = False
        if not good:  # a broken handshake, from anyone: a plain refusal, never a WebSocket
            self.close_connection = True
            self.send(*self.service.error(400, "a WebSocket version 13 handshake is needed"))
            return
        try:
            argv, session = self.service.websocket(self.path, self.headers, self.client_address[0])
            # The limit counted at once with its check, under the lock: tabs opening terminals at the same moment
            # all passed websocket()'s check.
            if not self.service.claim_terminal(session):
                raise Refused(429, f"at most {TERMINALS_PER_SESSION} terminals at once")
        except Exception as exc:  # noqa: BLE001 - refused with an answer, before anything runs
            self.close_connection = True
            status = exc.status if isinstance(exc, Refused) else 400 if isinstance(exc, ValueError) else 500
            if status in (401, 403, 421, 503) or not isinstance(exc, Refused):
                # No WebSocket for a browser that is not logged in, another page or name, a broken handshake.
                self.send(*self.service.error(status, str(exc) if status != 500 else type(exc).__name__))
                return
            # Logged in, from this page, but not this terminal (a query that is not one, too many terminals): the
            # WebSocket opens only to say why in its close frame, as a browser shows a refused handshake as code
            # 1006 and nothing more.
            self.accept_websocket(key)
            ws = WebSocket(Stream(self.rfile, self.wfile))
            ws.close(1008, f"refused: {exc}")
            with contextlib.suppress(OSError, ConnectionError, ValueError):
                self.connection.settimeout(3)
                ws.receive()  # the browser's answer to the close
            return
        try:
            self.close_connection = True
            self.accept_websocket(key)
            cols, rows = terminal_size(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query)))
            token = self.service.cookie(self.headers)
            sessions = self.service.sessions
            # A terminal may be quiet for long, its browser is not: it answers the pings. Nothing heard for
            # WS_DEADLINE seconds (a browser that is gone), or output not taken for that long (one that reads
            # nothing: the send lock is never held longer), ends the terminal.
            self.connection.settimeout(WS_DEADLINE)
            with contextlib.suppress(OSError):
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            def alive():
                try:
                    users = load_users()
                except ValueError:  # users.json became unreadable: no session holds any more
                    return False
                return sessions.current(token, users, touch=False) is session
            bridge(WebSocket(Stream(self.rfile, self.wfile), hang_up=self.hang_up), argv, cols, rows, alive=alive,
                   used=lambda: sessions.get(token, touch=True))
        finally:
            self.service.release_terminal(session)

    def accept_websocket(self, key):
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()

    def hang_up(self):
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)


class Stream:
    """The handler's reader and writer as one stream for WebSocket."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    def read(self, count):
        return self.reader.read1(count) if hasattr(self.reader, "read1") else self.reader.read(count)

    def write(self, data):
        self.writer.write(data)

    def flush(self):
        self.writer.flush()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, context, ipv6=False, limit=None):
        self.address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        self.context = context
        self.limit = MAX_CONNECTIONS if limit is None else limit
        self.per_address = MAX_PER_ADDRESS
        self.active, self.active_lock, self.by_address = 0, threading.Lock(), {}
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        """A thread for the connection, while fewer than LIMIT are served and its address holds fewer than
        PER_ADDRESS of them: else it is closed at once. The accept loop never waits for a free place, a flood of
        connections cannot start threads without bound, and one address cannot take every place."""
        address = address_key(client_address[0] if isinstance(client_address, tuple) else str(client_address))
        with self.active_lock:
            # This machine's own address shares one count: the browser here (and a reverse proxy) is not capped.
            full = self.active >= self.limit or (not loopback_address(address)
                                                 and self.by_address.get(address, 0) >= self.per_address)
            if not full:
                self.active += 1
                self.by_address[address] = self.by_address.get(address, 0) + 1
        if full:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.finished(address)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.finished(address_key(client_address[0] if isinstance(client_address, tuple) else str(client_address)))

    def finished(self, address=None):
        with self.active_lock:
            self.active -= 1
            if address is not None:
                left = self.by_address.get(address, 1) - 1
                if left > 0:
                    self.by_address[address] = left
                else:
                    self.by_address.pop(address, None)

    def get_request(self):
        sock, address = super().get_request()
        # The handshake runs in the connection's own thread (Handler.setup): here, one client that never says
        # hello would hold every other one up.
        try:
            wrapped = self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        except (ssl.SSLError, OSError):
            sock.close()
            raise
        return wrapped, address

    def handle_error(self, request, client_address):  # noqa: D102 - a broken client is no news
        pass


# --- the certificate --------------------------------------------------------------------------------------

def cert_files():
    return config_dir() / "cert.pem", config_dir() / "key.pem"


def make_certificate(names, renew=False):
    """The service's own certificate (EC P-256, 825 days) for NAMES, made with openssl unless it exists."""
    cert, key = cert_files()
    if cert.is_file() and key.is_file() and not renew:
        return cert, key
    config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    alt = []
    for name in sorted(set(names)):
        try:
            alt.append(f"IP:{ipaddress.ip_address(name.strip('[]'))}")
        except ValueError:
            alt.append(f"DNS:{name}")
    temporary = key.with_name(f".key.{os.getpid()}.pem")
    old = os.umask(0o077)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-keyout", str(temporary), "-out", str(cert), "-days", "825",
                        "-subj", "/CN=sandbox-ai web", "-addext", "subjectAltName=" + ",".join(alt),
                        "-addext", "extendedKeyUsage=serverAuth"],
                       check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise ValueError("openssl is needed to make the certificate (or pass --cert and --key)") from None
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"openssl failed: {exc.stderr.strip()[-300:]}") from None
    finally:
        os.umask(old)
    os.replace(temporary, key)
    return cert, key


def certificate_names(cert):
    """The names (DNS: lower case) and addresses the certificate CERT is for (its subjectAltName, read with openssl),
    or None when they cannot be read."""
    try:
        done = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    found = set()
    for entry in done.stdout.replace("\n", ",").split(","):
        kind, _, value = entry.strip().partition(":")
        if kind == "DNS":
            found.add(value.strip().lower())
        elif kind in ("IP Address", "IP"):
            with contextlib.suppress(ValueError):
                found.add(str(ipaddress.ip_address(value.strip())))
    return found


def unnamed_by(cert, names):
    """Which of NAMES (service_names': a name, an address, an IPv6 address in brackets) the certificate CERT does not
    name, sorted ([] when it names them all or its names cannot be read)."""
    found = certificate_names(cert)
    if found is None:
        return []
    missing = []
    for name in names:
        bare = name.strip("[]")
        try:
            key = str(ipaddress.ip_address(bare))
        except ValueError:
            key = bare.lower().rstrip(".")
        if key not in found:
            missing.append(name)
    return sorted(missing)


def fingerprint(cert):
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def context_for(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    context.set_alpn_protocols(["http/1.1"])
    return context


def parse_listen(value):
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not 0 <= int(port) <= 65535:  # 0: any free port
        raise ValueError(f"--listen {value}: ADDRESS:PORT, e.g. 127.0.0.1:8443 or [::]:8443")
    host = host.strip("[]") or "127.0.0.1"
    ipaddress.ip_address(host)
    return host, int(port)


def service_names(host, port, extra):
    names = {"localhost", "127.0.0.1", "[::1]", host if ":" not in host else f"[{host}]", *extra}
    if ipaddress.ip_address(host).is_unspecified:
        names |= {socket.gethostname(), socket.getfqdn()}
        with contextlib.suppress(OSError):
            names |= {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
    # An IPv6 address as a browser's Host header names it: in brackets ("[fd00::7]:8443"; an address found here or
    # given as --hostname fd00::7 was compared as "fd00::7:8443", which no browser sends - refused with 421).
    names = {f"[{name}]" if ":" in name and not name.startswith("[") else name for name in names if name}
    return names, {f"{name}:{port}" for name in names}


def serve(listen=DEFAULT_LISTEN, hostnames=(), cert=None, key=None, out=print, ready=None):
    host, port = parse_listen(listen)
    if not load_users():
        raise ValueError("no login yet: sandbox-ai web user add NAME")
    names, with_port = service_names(host, port, hostnames)
    cert_given, unnamed = bool(cert or key), []
    if cert or key:
        if not (cert and key):
            raise ValueError("--cert and --key go together")
    else:
        cert, key = make_certificate(names)
        # Made at the first start, for the names of then: a name given (or found) later is not in it, and a browser
        # there says the name does not match. Said, with the command that makes one for every name (no new one
        # unasked: its fingerprint, which you trusted, would change).
        unnamed = unnamed_by(cert, names)
    server = Server((host, port), context_for(cert, key), ipv6=":" in host)
    actual = server.server_address[1]
    names, with_port = service_names(host, actual, hostnames)
    Handler.service = Service(with_port | (names if actual == 443 else set()))
    previous = {}
    try:
        if threading.current_thread() is threading.main_thread():
            # SIGTERM (systemd, kill) and SIGHUP (its terminal closed) end it as Ctrl-C does, through the clean-up
            # below: they ended the process at once, and listen.json stayed with its port (the launcher refused
            # --allow-host-port for that port while another process had the PID). Installed before listen.json is
            # written, and inside the try: a signal right after the write still removes it.
            def ended(signum, _frame):
                raise SystemExit(128 + signum)
            for sig in (signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, ended)
        write_private(listen_file(), json.dumps({"address": host, "port": actual, "pid": os.getpid()}).encode())
        audit("serve", address=host, port=actual)
        shown = host if ":" not in host else f"[{host}]"
        out(f"sandbox-ai web: https://{'localhost' if shown in ('127.0.0.1', '[::1]') else shown}:{actual}/")
        out(f"  certificate SHA-256 {fingerprint(cert)}")
        for line in no_totp_warnings(load_users(), listen):
            out(line)
        if unnamed and not cert_given:
            renew = ["sandbox-ai", "web", "cert", "--renew", "--listen", listen,
                     *[word for name in hostnames for word in ("--hostname", name)]]
            out(f"warning: the certificate does not name {printable(', '.join(unnamed))}: a browser there says the "
                f"name does not match; {printable(shlex.join(renew))} makes one that does (a new fingerprint to "
                "trust)")
        # Into a log or a pipe (nohup, systemd, | tee) at once: Python buffers a non-terminal's output, and the
        # service runs until it ends (-I: PYTHONUNBUFFERED is not read) - the address, the fingerprint and the
        # warning about logins without TOTP stayed unseen.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            sys.stdout.flush()
        if ready:
            ready(server)
        server.serve_forever()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        with contextlib.suppress(OSError):
            listen_file().unlink()
        module("approvals").end_heartbeat()  # no page here watches the questions any more
        server.server_close()


# --- the command line -------------------------------------------------------------------------------------

def ask(prompt, from_stdin):
    if from_stdin:
        line = sys.stdin.readline()
        if not line:
            raise ValueError("standard input ended")
        return line.rstrip("\n")
    return getpass.getpass(prompt)


def no_totp_warnings(users, listen):
    """The lines `web serve` says at its start about the logins without TOTP (USERS: load_users()), louder when it
    listens beyond this machine (LISTEN: ADDRESS:PORT); [] when every login has TOTP."""
    names = sorted(name for name, record in users.items() if isinstance(record, dict) and not record.get("totp"))
    if not names:
        return []
    host, _ = parse_listen(listen)
    wide = not loopback_address(host.strip("[]"))
    return [f"warning: login{'s' if len(names) > 1 else ''} without a second factor (TOTP): {', '.join(names)}"
            + (" - and this service listens beyond this machine: anyone who reaches it and learns a password gets "
               "in (sandbox-ai web user reset NAME gives a login TOTP)" if wide else
               " (sandbox-ai web user reset NAME gives a login TOTP)")]


NO_TOTP_RISK = ("a login without a second factor: whoever learns its password can do everything this service does - "
                "terminals in the sandboxes, starts, approvals - here and on every host of sandbox-ai hosts")


def enrol(name, from_stdin, out, totp=True):
    """A password and a TOTP secret for NAME, the secret confirmed with a code; TOTP False (--no-totp, your choice):
    the password only, with the warning of what that means."""
    password = ask("Password (12 characters or more): ", from_stdin)
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs {MIN_PASSWORD} characters or more")
    if ask("Again: ", from_stdin) != password:
        raise ValueError("the two passwords differ")
    if not totp:
        out(f"warning: {name}: {NO_TOTP_RISK} (add it later: sandbox-ai web user reset {name})")
        return {"password": hash_password(password), "totp": None, "totp_counter": None,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    secret = new_totp_secret()
    out("Add this to your TOTP app (it is shown only now):")
    out(f"  {otpauth_uri(name, secret)}")
    out(f"  or the secret {secret} (SHA1, 6 digits, 30 seconds)")
    sys.stdout.flush()  # shown before the code is asked for, also through a pipe
    code = (ask("Code from the app: ", True) if from_stdin else input("Code from the app: ")).strip()
    counter = check_totp(secret, code, None)
    if counter is None:
        raise ValueError("that code does not match: nothing was saved, try again")
    return {"password": hash_password(password), "totp": secret, "totp_counter": counter,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def cli(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai web", formatter_class=help_style,
                                     description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("user", help="the logins", formatter_class=help_style,
                               description="The logins of the web interface: a password and a TOTP app each.")
    users = user.add_subparsers(dest="action", required=True)
    for action, text in (("add", "a login: a password (12 characters or more) and a TOTP app"),
                         ("remove", "forget a login"), ("reset", "a new password and TOTP app for a login")):
        sub = users.add_parser(action, help=text)
        sub.add_argument("name")
        if action != "remove":
            sub.add_argument("--stdin", action="store_true", help="read the password twice and the code from "
                                                                  "standard input, one per line")
            sub.add_argument("--no-totp", dest="no_totp", action="store_true",
                             help="the password only, without a TOTP app (your choice; warned about here, in the "
                                  "list, when the service starts and at each login)")
    users.add_parser("list", help="the logins")
    commands.add_parser("vendor", help="download xterm.js for the terminals (pinned, checked)")
    run = commands.add_parser("serve", help="run the service")
    run.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS:PORT", help="the address and port to listen on")
    run.add_argument("--hostname", action="append", default=[], metavar="NAME",
                     help="a further name the browser uses for this machine (repeatable)")
    run.add_argument("--cert", type=Path, help="the TLS certificate (PEM) to serve with")
    run.add_argument("--key", type=Path, help="the TLS private key (PEM) of that certificate")
    cert = commands.add_parser("cert", help="the certificate's fingerprint")
    cert.add_argument("--renew", action="store_true", help="make a new certificate even if the current one is still valid")
    cert.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS:PORT", help="the address and port to listen on")
    cert.add_argument("--hostname", action="append", default=[], metavar="NAME", help="a name the certificate is valid for (repeatable)")
    a = parser.parse_args(argv)
    if a.command == "user":
        if a.action == "list":
            for name, record in sorted(load_users().items()):
                out(f"{name}  since {record.get('created', '?')}" + ("" if record.get("totp") else "  (no TOTP)"))
            return 0
        if not USER_NAME.fullmatch(a.name):
            raise ValueError("a user name is 1-32 lowercase letters, digits, '_', '.' or '-', starting with a letter")
        if a.action == "remove":
            with users_locked() as current:
                if current.pop(a.name, None) is None:
                    raise ValueError(f"no user {a.name}")
            audit("user-removed", user=a.name)
            out(f"removed {a.name} (sessions of the running service end at their next request)")
            return 0
        exists = a.name in load_users()
        if a.action == "add" and exists:
            raise ValueError(f"user {a.name} exists; sandbox-ai web user reset {a.name} gives it a new password")
        if a.action == "reset" and not exists:
            raise ValueError(f"no user {a.name}")
        record = enrol(a.name, a.stdin, out, totp=not a.no_totp)
        with users_locked() as current:
            current[a.name] = record
        audit("user-" + ("added" if a.action == "add" else "reset"), user=a.name,
              **({"second_factor": False} if a.no_totp else {}))
        out(f"{'added' if a.action == 'add' else 'reset'} {a.name}")
        return 0
    if a.command == "vendor":
        vendor(out=out)
        return 0
    if a.command == "cert":
        host, port = parse_listen(a.listen)
        made, _ = make_certificate(service_names(host, port, a.hostname)[0], renew=a.renew)
        out(f"{made}: SHA-256 {fingerprint(made)}")
        return 0
    serve(a.listen, a.hostname, a.cert, a.key, out)
    return 0


def main(argv=None):
    try:
        return cli(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(f"sandbox-ai web: {printable(exc)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print("sandbox-ai web: that address is in use (another service?)", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
