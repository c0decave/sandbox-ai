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
import fcntl
import getpass
import hashlib
import hmac
import http.server
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
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
STATIC = HERE / "web"
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "i18n.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8", "themes.json": "application/json",
                "help.json": "application/json", "icon.svg": "image/svg+xml"}
VENDOR_FILES = {"xterm.js": "text/javascript; charset=utf-8", "xterm.css": "text/css; charset=utf-8",
                "addon-fit.js": "text/javascript; charset=utf-8"}
DEFAULT_LISTEN = "127.0.0.1:8443"
USER_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")  # a start's name or id (START_ID)
NEW_START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")  # a name given to a start (SESSION_NAME)
MCP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
APPROVAL_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}")  # never a leading "-": read as an option there
AGENTS = ("claude", "codex", "opencode")
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


def listen_file():
    """Where the running service says which port it has: the launcher refuses --allow-host-port for it."""
    return config_dir() / "listen.json"


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

def audit(event, **fields):
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    path = state_dir() / "audit.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


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

    def drop_user(self, user):
        with self.lock:
            for key in [k for k, s in self.items.items() if s["user"] == user]:
                del self.items[key]


# --- what the browser may ask for ---------------------------------------------------------------------------

# The codes of the common refusals (Refused's CODE, the answer's "code" with its "params"): the page shows its own
# text for a code it knows (German too), with the service's English message below it; any other refusal is shown as
# its message. A code is never a reason to pass anything: it only says which of these it is.
ERROR_CODES = ("no-host", "confirm-needed", "bad-path", "bad-name", "host-unreachable", "timeout", "command-failed",
               "unknown-preset-value", "no-preset", "login-needed", "page-outdated", "too-many-logins", "busy",
               "changed-meanwhile", "nothing-checked", "host-too-old", "mcp-unsigned-catalog")

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


NEWER = (
    ((0, 1, 80), "--ask allow", asks_allow),
    ((0, 1, 78), "approvals allow --no-open", lambda a: a[:2] == ["approvals", "allow"] and "--no-open" in a),
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
    ((0, 1, 78), "plugins marketplaces", lambda a: a[:2] == ["plugins", "marketplaces"]),
    ((0, 1, 78), "plugins --marketplaces-only", lambda a: a[:1] == ["plugins"] and "--marketplaces-only" in a),
    ((0, 1, 78), "kits enable-plugin, disable-plugin, remove-marketplace and import-marketplace",
     lambda a: a[:1] == ["kits"] and a[1:2] and a[1] in KIT_NEW_ACTIONS),
    ((0, 1, 78), "plugins and marketplaces of the default kit changed by sandbox-ai kits",
     lambda a: a[:1] == ["kits"] and a[1:2] and a[1] in KIT_DEFAULT_ACTIONS and "--" in a
     and a[a.index("--") + 1:a.index("--") + 2] == ["default"]),
)


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
# open a whole top-level domain). The command refuses public and shared suffixes itself (runtime.wildcard_suffix).
NET_WILDCARD = re.compile(r"\*\.(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}")
NET_TCP = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:.]+\]):[0-9]{1,5}")
NET_GIT_URL = re.compile(r"https?://(?:[A-Za-z0-9._-]{1,64}@)?(?:[A-Za-z0-9.-]{1,253}|\[::1\])(?::[0-9]{1,5})?/?")
NET_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


# What a running sandbox is given while it runs (sandbox-ai workspace, devices, limits): never a leading "-".
DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9_+:.@-][A-Za-z0-9_+:.@/-]{0,250}")
USB_SELECTOR = re.compile(r"usb:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}(?::[!-~]{1,126})?")  # usb:VENDOR:PRODUCT[:SERIAL]
DEVICE_ID = re.compile(r"[0-9]{1,9}")  # a grant's id, as devices list shows it
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


def start_name(value):
    if not (isinstance(value, str) and START_NAME.fullmatch(value)):
        raise Refused(400, "a start's name is 1-48 letters, digits, '_', '.' or '-'", code="bad-name",
                      params={"what": "start"})
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


def kit_name(value, what="a kit's name"):
    if not (isinstance(value, str) and KIT_NAME.fullmatch(value)) or value in ("default", "built-in"):
        raise Refused(400, f"{what} is 1-64 letters, digits, '_', '.' or '-' (not default or built-in)")
    return value


def kit_agents(body, many=True):
    """--agent=AGENT options of BODY's "agents" (a list) or "agent" (one)."""
    values = strings(body.get("agents", []), "agents", 3) if many else (
        [body["agent"]] if body.get("agent") is not None else [])
    if not all(value in AGENTS for value in values):
        raise Refused(400, "an agent is claude, codex or opencode")
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
BUILD_TARGET = re.compile(r"[a-z0-9][a-z0-9_.-]{0,29}")  # codex, claude, opencode, all, team, vpn, tor or a variant
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
# a line of team list: NAME  MEMBERS  WORKSPACE
TEAM_LINE = re.compile(r"(?P<name>[a-z0-9][a-z0-9_-]{0,19})\s+"
                       r"(?P<members>(?:claude|codex|opencode)(?:, (?:claude|codex|opencode))*)\s+(?P<workspace>/.*)")
# a team whose record is damaged: NAME  (damaged)  remove it with: sandbox-ai team delete NAME --yes
TEAM_DAMAGED_LINE = re.compile(r"(?P<name>[a-z0-9][a-z0-9_-]{0,19})\s+\(damaged\)(?:\s.*)?")
IMAGE_KINDS = ("apt", "npm", "pip")  # images extend --add-KIND / --remove-KIND
IMAGE_PACKAGE = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._+~=<>!*^:\[\],-]{0,200}")  # images.py checks each kind's own
IMAGE_MODE = re.compile(r"0?[0-7]{3}")
IMAGE_RECIPES = ("media", "tor")
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


def catalog_name(prefix, given=()):
    """The catalog behind PREFIX of `mcp catalog`'s lines, for people: sandbox-ai-mcp (mcp:) or axross-mcp-hub
    (hub:) of the default ones, the Nth catalog GIVEN for cN: (mcp.catalog_label)."""
    mcp = module("mcp")
    numbered = re.fullmatch(r"c([0-9]{1,2})", str(prefix))
    if numbered and int(numbered[1]) < len(given) and isinstance(given[int(numbered[1])], str):
        return printable(mcp.catalog_label(given[int(numbered[1])]))[:300]
    source = dict(mcp.CATALOGS).get(prefix)
    return printable(mcp.catalog_label(source))[:300] if source else printable(str(prefix))[:300]


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
    "start-stop": "stopping a start ends what runs in it (and its HOME, unless it is kept)",
    "start-delete": "deleting a start removes its kept HOME",
    "gc": "gc removes the leftovers of ended starts, and kept starts unused for that long with their HOME, for good",
    "approval-allow-always": "an allow always is remembered for the project: every later start of it gets this "
                             "without asking",
    "approval-deny-always": "a deny always is remembered for the project: every later question like it is refused",
    "approval-forget": "forgetting a remembered answer changes what every later start of that project gets (a "
                       "forgotten block is asked again)",
    "defaults-lower": "this lowers the protection of every later start on that host",
    "net-allow": "net allow widens what the running sandbox reaches, at once (real DNS, ports on the host, host "
                 "wildcards, Git logins included)",
    "net-untaint": "untaint takes back the rules for tainted runs: the sandbox is treated as if no untrusted content "
                   "had entered it",
    "workspace-writable": "a writable workspace lets the agent change the directory on that host",
    "workspace-force": "a forced removal leaves what a process there still holds usable, also for writing, until it "
                       "is closed; a copy the agent took stays usable until the sandbox restarts",
    "device-add": "a device gives the agent that hardware of the host",
    "limits-raise": "a limit above the current one (or none) lets the agent use more of that host",
    "host-remove": "removing a host ends this service's management of it",
    "vpn-remove": "removing a VPN profile stops the starts that use it from starting",
    "llm-remove": "removing an LLM server stops the starts that use it from starting",
    "kit-delete": "deleting a kit removes its folder",
    "kit-remove": "removing a skill or plugin from a kit takes it from every later start with that kit",
    "kit-add": "a skill, plugin or marketplace added to a kit is given to every later start with that kit",
    "mcp-attach": "an attached MCP server is reached by that start from its next start on (kept for its later "
                  "starts)",
    "mcp-restart": "restarting a running start ends what runs in it now, so that it reaches the MCP server (the agent "
                   "continues its conversation)",
    "host-edit": "changing a host's SSH target, port, key, command or sudo setting changes where and how this "
                 "service's commands run",
    "host-install": "installing sandbox-ai on a host puts this machine's copy (or the signed release) there, for the "
                    "SSH user or for every user, as root or with sudo",
    "secret-delete": "deleting a secret takes it from every start, VPN profile and Git login that names it",
    "secret-replace": "a secret of that name exists: its value is replaced",
    "project-trust": "a trusted project configuration gives every later start of that project its options",
    "project-forget": "a project configuration no longer trusted stops the starts of that project from starting "
                      "until it is trusted again",
    "mcp-stop": "stopping an MCP server ends it for every start that uses it now",
    "mcp-detach": "detaching an MCP server from a running start closes its relay there now: the agent's "
                  "connections to it end and its calls fail (its later starts go without it)",
    "mcp-unsigned": "an MCP server from a catalog no trusted key signed is taken without that check",
    "mcp-writable-mount": "a writable mount lets the MCP server change that directory on the host",
    "git-login-add": "a Git login is used by the connection proxy of every sandbox that reaches that server",
    "login-replace": "a login for that server or kind exists: its token is replaced by this secret's for every later "
                     "start that uses it",
    "mcp-replace": "an MCP server of that name exists: its definition is replaced for every start that uses it",
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
    "team-stop": "stopping a team ends what runs in every member and its board",
    "team-delete": "deleting a team removes its members with their HOMEs and its board",
    "image-file": "a host file added to an image's extension is built into the image: every later start of it can "
                  "read it",
    "image-remove": "removing an extension drops what it adds from the next build (a variant's image goes with it, "
                    "unless kept)",
    "login-logout": "logging out forgets the stored login of that agent: its sandboxes need a new login",
    "kit-hooks-trust": "trusted hooks of a kit run in every later start with that kit",
}
# The actions only a terminal asks for (its query carries confirm=1): no route names them.
TERMINAL_CONFIRM = ("login-logout", "kit-hooks-trust")
# The order of protection (weakest first) that decides whether a default is lowered: any step down needs a confirm.
MODE_ORDER = ("lax", "low", "medium", "high", "paranoid")
TELEMETRY_ORDER = ("on", "off", "strict")
DEFAULT_MODE, DEFAULT_TELEMETRY = "medium", "off"
# The keys of defaults.json (agent_sandbox.DEFAULTS_KEYS but "version"; tests/test_web.py checks they agree): any
# other key is refused before anything reaches a host.
DEFAULT_KEYS = ("canaries", "keep_days", "mode", "name_style", "preset_dirs", "safeguard_mute", "subnet", "subnet6",
                "telemetry")
NAME_STYLES = ("codename", "project")  # agent_sandbox.NAME_STYLES: name_style's values


def confirmed(body, action, detail=None):
    """Refused unless BODY carries {"confirm": true}: ACTION is a key of NEEDS_CONFIRM (its reason in the error,
    DETAIL first when given)."""
    if action not in NEEDS_CONFIRM:
        raise KeyError(action)  # a route asking for an action the table does not have: a bug, never a pass
    if body.get("confirm") is not True:
        why = f"{detail}: {NEEDS_CONFIRM[action]}" if detail else NEEDS_CONFIRM[action]
        raise Refused(400, f"{why}: send {{\"confirm\": true}}", confirm=action, code="confirm-needed",
                      params={"action": action, "detail": detail or ""})


def one_line(value, what, limit=1024):
    if not (isinstance(value, str) and value.strip() and len(value) <= limit and value.isprintable()):
        raise Refused(400, f"{what}: one line of at most {limit} characters")
    return value.strip()


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
        self.updater = updater or (lambda host, version=None, sudo=None, from_here=False:
                                    module("hosts").update(host, version, sudo, from_here=from_here))
        self.statuses = statuses or (lambda names, refresh=False: module("hosts").statuses(names, refresh=refresh))
        self.approvals_fanout = FanOut(self.host_questions)
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
            ("GET", H + r"/starts", self.starts, ()),
            ("POST", H + r"/starts/(?P<name>[^/]+)/(?P<action>pause|resume|stop|delete)", self.start_action,
             ("start-stop", "start-delete", "start-restart", "workspace-writable", "mcp-attach")),
            ("POST", H + r"/starts/(?P<name>[^/]+)/rename", self.rename, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/history", self.history, ()),
            ("GET", H + r"/starts/(?P<name>[^/]+)/review", self.review_home, ()),
            ("GET", H + r"/gc", self.gc_preview, ()),
            ("GET", H + r"/gc/(?P<days>[^/]+)", self.gc_preview, ()),
            ("POST", H + r"/gc", self.gc, ("gc",)),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/attach", self.mcp_attach, ("mcp-attach", "mcp-restart")),
            # an MCP server (or a helper agent) into a running start now, and out of it (mcp attach|detach --live)
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp-live", self.mcp_live_attach, ("mcp-attach",)),
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp-live/remove", self.mcp_live_detach, ("mcp-detach",)),
            # out of a start that does not run (stopped, paused): its later starts go without it (mcp detach)
            ("POST", H + r"/starts/(?P<name>[^/]+)/mcp/remove", self.mcp_detach, ("mcp-detach",)),
            ("GET", H + r"/plugins-status", self.plugins_status, ()),
            ("GET", H + r"/approvals", self.approvals, ()),
            ("GET", H + r"/approvals-history", self.approvals_history, ()),
            ("GET", H + r"/approvals-history/(?P<run>[^/]+)", self.approvals_history, ()),
            ("GET", H + r"/remembered", self.remembered, ()),
            ("POST", H + r"/remembered/forget", self.forget, ("approval-forget",)),
            ("POST", H + r"/approvals/(?P<id>[^/]+)", self.answer, ("approval-allow-always", "approval-deny-always")),
            ("GET", H + r"/doctor", self.doctor, ()),
            ("GET", H + r"/llm", self.llm, ()),
            ("GET", H + r"/llm/(?P<name>[^/]+)/models", self.llm_models, ()),
            ("GET", H + r"/llm-status", self.llm_status, ()),
            ("GET", H + r"/llm/(?P<name>[^/]+)/status", self.llm_status, ()),
            ("POST", H + r"/llm-models", self.llm_models_of_url, ("llm-insecure",)),
            ("POST", H + r"/llm", self.add_llm, ("llm-insecure", "llm-replace")),
            ("POST", H + r"/llm/(?P<name>[^/]+)/check", self.check_llm, ()),
            ("DELETE", H + r"/llm/(?P<name>[^/]+)", self.remove_llm, ("llm-remove",)),
            ("GET", H + r"/defaults", self.defaults, ()),
            ("POST", H + r"/defaults", self.set_default, ("defaults-lower",)),
            ("GET", H + r"/net/(?P<name>[^/]+)", self.net_show, ()),
            ("POST", H + r"/net/(?P<name>[^/]+)", self.net_change, ("net-allow", "net-keep")),
            ("POST", H + r"/net/(?P<name>[^/]+)/untaint", self.net_untaint, ("net-untaint",)),
            ("GET", H + r"/workspaces/(?P<name>[^/]+)", self.workspaces, ()),
            ("POST", H + r"/workspaces/(?P<name>[^/]+)", self.workspace_add, ("workspace-writable",)),
            ("POST", H + r"/workspaces/(?P<name>[^/]+)/remove", self.workspace_remove, ("workspace-force",)),
            ("GET", H + r"/devices/(?P<name>[^/]+)", self.devices, ()),
            ("POST", H + r"/devices/(?P<name>[^/]+)", self.device_add, ("device-add",)),
            ("POST", H + r"/devices/(?P<name>[^/]+)/remove", self.device_remove, ()),
            ("GET", H + r"/limits/(?P<name>[^/]+)", self.limits, ()),
            ("POST", H + r"/limits/(?P<name>[^/]+)", self.set_limits, ("limits-raise",)),
            ("GET", H + r"/presets", self.presets, ()),
            ("GET", H + r"/vpn", self.vpn_list, ()),
            ("POST", H + r"/vpn", self.vpn_add, ("vpn-socks", "vpn-replace")),
            ("POST", H + r"/vpn/(?P<name>[^/]+)/check", self.vpn_check, ()),
            ("DELETE", H + r"/vpn/(?P<name>[^/]+)", self.vpn_remove, ("vpn-remove",)),
            ("GET", H + r"/kits", self.kits, ()),
            ("GET", H + r"/marketplaces", self.marketplaces, ()),
            ("POST", H + r"/kits", self.kit_new, ()),
            ("POST", H + r"/kits/(?P<name>[^/]+)/(?P<action>[a-z-]+)", self.kit_change, ("kit-remove", "kit-add")),
            ("DELETE", H + r"/kits/(?P<name>[^/]+)", self.kit_delete, ("kit-delete",)),
            ("GET", H + r"/canaries", self.canaries, ()),
            ("GET", H + r"/canaries/(?P<run>[^/]+)", self.canaries, ()),
            ("GET", H + r"/safeguards", self.safeguards, ()),
            ("GET", H + r"/safeguards/(?P<run>[^/]+)", self.safeguards, ()),
            ("GET", H + r"/secrets", self.secrets, ()),
            ("POST", H + r"/secrets/(?P<name>[^/]+)", self.secret_set, ("secret-replace",)),
            ("DELETE", H + r"/secrets/(?P<name>[^/]+)", self.secret_delete, ("secret-delete",)),
            # project configurations (sandbox-ai wizard trust)
            ("GET", H + r"/projects", self.projects, ()),
            ("POST", H + r"/projects/review", self.project_review, ()),
            ("POST", H + r"/projects/trust", self.project_trust, ("project-trust",)),
            ("POST", H + r"/projects/forget", self.project_forget, ("project-forget",)),
            # MCP servers (sandbox-ai mcp)
            ("GET", H + r"/mcp", self.mcp_list, ()),
            ("POST", H + r"/mcp", self.mcp_add, ("mcp-unsigned", "mcp-writable-mount", "mcp-replace")),
            ("POST", H + r"/mcp-catalog", self.mcp_catalog, ()),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/start", self.mcp_start, ("mcp-attach", "mcp-restart")),
            ("POST", H + r"/mcp/(?P<name>[^/]+)/stop", self.mcp_stop, ("mcp-stop",)),
            # logins and identities (sandbox-ai git-logins, publish-logins, identities): secrets only by name
            ("GET", H + r"/git-logins", self.git_logins, ()),
            ("POST", H + r"/git-logins", self.git_login_add, ("git-login-add",)),
            ("POST", H + r"/git-logins/remove", self.git_login_remove, ("login-remove",)),
            ("POST", H + r"/git-logins/check", self.git_login_check, ()),
            ("GET", H + r"/publish-logins", self.publish_logins, ()),
            ("GET", H + r"/publish-logins/kinds", self.publish_kinds, ()),
            ("GET", H + r"/login-status", self.login_status, ()),
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
            # logs of a directory or project, the privacy check, the activity
            ("POST", H + r"/logs", self.logs_of, ()),
            ("POST", H + r"/privacy-check", self.privacy_check, ()),
            ("POST", H + r"/activity", self.activity, ()),
            # the doctor of one backend, the version in full, images, the demo device
            ("GET", H + r"/doctor/(?P<backend>[^/]+)", self.doctor, ()),
            ("GET", H + r"/version", self.version_of, ()),
            ("GET", H + r"/images", self.images, ()),
            ("GET", H + r"/demo-device", self.demo_status, ()),
            ("POST", H + r"/demo-device/start", self.demo_start, ()),
            ("POST", H + r"/demo-device/stop", self.demo_stop, ()),
            # teams of agents (sandbox-ai team; a team starts in a terminal: kind=team-start)
            ("GET", H + r"/teams", self.teams, ()),
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
        except ValueError:
            raise self.failed(host, args, status, out, err) from None

    def done(self, host, args, timeout=120, input=None):
        status, out, err = self.run(host, args, timeout, input)
        if status != 0:
            raise self.failed(host, args, status, out, err)
        return {"output": printable(out.strip()[-4000:])}

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
        return {"removed": params["host"]}

    def forget_host(self, name):
        """What the fan-outs keep of NAME (its questions, its starts): it was added, changed or removed."""
        self.approvals_fanout.forget(name)
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
                return self.updater(name, None, sudo, from_here=True)
            except ValueError as exc:
                raise Refused(400, printable(exc)) from None
            finally:
                with self.install_lock:
                    self.installing.pop(name, None)
        try:
            return self.updater(name, version, sudo)
        except ValueError as exc:
            if name != hosts.LOCAL:
                hosts_file()  # a damaged hosts.json: that, as a 500 with why
            raise Refused(404 if "no host" in str(exc) else 400, printable(exc)) from None

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
            except ValueError:
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
                return self.done(params["host"], ["resume", *options, name], timeout=300)
        return self.done(params["host"], [action, name], timeout=180)

    @staticmethod
    def resume_options(body, name):
        """resume --restart, --workspace DIR, --mount SOURCE:DESTINATION[:ro|rw], --mcp NAME (each kept for its later
        starts): a restart ends what runs now (start-restart), a directory or a writable mount lets the agent change
        it (workspace-writable), an MCP server is reached by it (mcp-attach); all asked first."""
        options, widens = [], []
        for value in strings(body.get("workspaces", []), "workspaces", 16):
            options.append(f"--workspace={clean_path(value, 'a workspace')}")
            widens.append(value)
        mounts = body.get("mounts", [])
        if not isinstance(mounts, list) or len(mounts) > 16:
            raise Refused(400, "mounts is a list of {source, destination, mode}")
        for mount in mounts:
            if not isinstance(mount, dict) or set(mount) - {"source", "destination", "mode"}:
                raise Refused(400, "a mount is {source, destination, mode: ro or rw}")
            source = clean_path(mount.get("source"), "a mount's source")
            destination = clean_path(mount.get("destination"), "a mount's destination")
            mode = mount.get("mode") or "ro"
            if mode not in ("ro", "rw") or ":" in source + destination:
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

    def history(self, params, body):
        """sandbox-ai list --history NAME --json: what happened to NAME (start, exit, pause, resume, rename)."""
        found = self.json_of(params["host"], ["list", "--history", start_name(params["name"]), "--json"], 30)
        if not isinstance(found, list):
            raise Refused(502, "sandbox-ai list --history: not a list")
        return [entry for entry in found if isinstance(entry, dict)]

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
        {"server": NAME} (sandbox-ai mcp list) or {"helper": AGENT} (claude, codex or opencode as a helper), one of
        them; never a leading "-" (read as an option there)."""
        server, helper = body.get("server"), body.get("helper")
        if (server is None) == (helper is None):
            raise Refused(400, "name the server (server: NAME) or a helper agent (helper: claude, codex or opencode)")
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

    def net_change(self, params, body):
        """sandbox-ai net allow|revoke NAME with what BODY names; each value is checked and passed as --flag=VALUE,
        so nothing in it is read as another option. A Git login names where its token is (a variable or a secret
        of that host), never the token."""
        op = body.get("op")
        if op not in ("allow", "revoke"):
            raise Refused(400, "op is allow or revoke")
        args = ["net", op, start_name(params["name"])]
        for value in strings(body.get("allow_host", []), "allow_host", 32):
            if not (NET_HOST.fullmatch(value) or NET_WILDCARD.fullmatch(value)):
                raise Refused(400, f"not a host name or *.DOMAIN.TLD: {printable(value)}")
            args.append(f"--allow-host={value}")
        for value in strings(body.get("allow_tcp", []), "allow_tcp", 32):
            if not NET_TCP.fullmatch(value):
                raise Refused(400, f"IP:PORT or [IPv6]:PORT: {printable(value)}")
            args.append(f"--allow-tcp={value}")
        ports = body.get("host_ports", [])
        if not isinstance(ports, list) or len(ports) > 16 or not all(
                isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in ports):
            raise Refused(400, "host_ports is a list of at most 16 ports")
        args += [f"--allow-host-port={port}" for port in ports]
        for value in strings(body.get("allow", []), "allow", 32):
            args.append(f"--allow={ip_value(value, 'allow', network=True)}")
        presets = strings(body.get("presets", []), "presets", len(NET_PRESETS))
        if not set(presets) <= set(NET_PRESETS):
            raise Refused(400, f"presets are {', '.join(NET_PRESETS)}")
        args += [f"--allow-{preset}" for preset in dict.fromkeys(presets)]
        if body.get("dns") is True:
            args.append("--dns")
        for value in strings(body.get("nameservers", []), "nameservers", 8):
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
        path = no_dots(host_path(body.get(what), what), what)
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
        if isinstance(value, str) and value.startswith("/"):
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
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def presets(self, params, body):
        return self.json_of(params["host"], ["presets", "list", "--json"], 30)

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
            except (ValueError, AttributeError):
                existing = set()
            if name not in existing:
                first = self.done(params["host"], ["kits", "new", "--", name], 60)["output"] + "\n"
        result = self.done(params["host"], ["kits", action, *options, "--", name, *values], 300)
        return {"output": first + result["output"]}

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

    def host_questions(self, name):
        """NAME's open questions; a browser question of a host whose sandbox-ai is known to be older than 0.1.78
        carries "host_too_old" (its version): it cannot be allowed without opening a browser there (--no-open), so
        the page says to update it instead of opening the link here first."""
        listed = self.json_of(name, ["approvals", "list", "--json"], APPROVALS_HOST_SECONDS)
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

    def answer(self, params, body):
        """allow or deny a question with a scope. An "always" (a rule remembered for the project, allowed or
        denied) needs {"confirm": true}; "no_open": true allows a browser question without opening the browser
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
        if scope == "always":
            confirmed(body, f"approval-{decision}-always")
        # An "always" waits up to approvals.VERIFY_WAIT for the sandbox before it is remembered; the call (over ssh,
        # perhaps, with a name to resolve) must outlast that clearly, or the "always" is lost.
        return self.done(params["host"], ["approvals", decision, ident, *(["--scope", scope] if scope else []),
                                          *(["--no-open"] if no_open else [])],
                         module("approvals").VERIFY_WAIT + 60)

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
        except ValueError:
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

    def llm_models(self, params, body):
        if params["name"].startswith("-"):
            raise Refused(400, "no such server")
        return self.json_of(params["host"], ["llm", "models", params["name"], "--json"], 60)

    def check_llm(self, params, body):
        name = params["name"]
        agent = body.get("agent")
        if agent not in (None, *AGENTS) or name.startswith("-"):
            raise Refused(400, "agent is claude, codex or opencode")
        status, out, err = self.run(params["host"], ["llm", "check", name, *(["--agent", agent] if agent else [])],
                                    90)
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
        if key == "name_style" and value is not None and value not in NAME_STYLES:
            raise Refused(400, f"name_style is {' or '.join(NAME_STYLES)}")
        if key == "safeguard_mute" and value is not None and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)):
            raise Refused(400, "safeguard_mute is a list of kinds (sandbox-ai safeguards kinds)")
        # How starts are named and which safeguard warnings pop up protect nothing (the safeguards still refuse,
        # and every record stays listed): no step down to confirm.
        if body.get("confirm") is not True and key not in ("name_style", "safeguard_mute"):
            weakest = {"mode": MODE_ORDER[0], "telemetry": TELEMETRY_ORDER[0]}.get(key)
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
            if value is None:
                return None  # no keep_days: kept starts are never removed for their age
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 36500:
                raise Refused(400, "keep_days is a whole number of days (1-36500)")
            if isinstance(before, bool) or not isinstance(before, int):
                return f"keep_days {value}: kept starts unused for {value} days are removed (there is no keep_days now)"
            if value < before:
                return f"keep_days {value} instead of {before}: kept starts are removed sooner"
            return None
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
        if key in ("subnet", "subnet6"):
            if value == before:
                return None
            return f"{key}: the network of every later start changes ({json.dumps(before)} -> {json.dumps(value)})"
        order = MODE_ORDER if key == "mode" else TELEMETRY_ORDER
        default = DEFAULT_MODE if key == "mode" else DEFAULT_TELEMETRY
        before, after = values.get(key, default), default if value is None else value
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

    def mcp_stop(self, params, body):
        name = self.mcp_server(params["name"])
        confirmed(body, "mcp-stop", name)
        return self.done(params["host"], ["mcp", "stop", name], 120)

    def mcp_catalog(self, params, body):
        """sandbox-ai mcp catalog [--catalog FILE_OR_URL]...: its lines (the command has no --json) read as
        [{"entry", "prefix", "name", "network", "title", "published", "signature"}]; "problems": the catalogs it
        could not read (its stderr), "output": what it printed."""
        status, out, err = self.run(params["host"], ["mcp", "catalog", *catalog_sources(body)], 120)
        if status != 0:
            raise self.failed(params["host"], ["mcp", "catalog"], status, out, err)
        entries = []
        for line in out.splitlines():
            match = MCP_CATALOG_LINE.fullmatch(line.rstrip())
            if match:
                entries.append({"entry": f"{match['prefix']}:{match['name']}", "prefix": match["prefix"],
                                "name": match["name"], "network": printable(match["network"]),
                                "title": printable(match["title"]), "published": not match["unpublished"],
                                "signature": {"signed": "good", "SIGNATURE BAD": "bad"}.get(match["signature"],
                                                                                           "none"),
                                "catalog": catalog_name(match["prefix"], body.get("catalogs") or [])})
        return {"entries": entries, "problems": [printable(line)[:600] for line in (err or "").splitlines()
                                                 if line.strip()][:20],
                "output": printable(out.strip()[-20000:])}

    def mcp_add(self, params, body):
        """sandbox-ai mcp add ENTRY with --as, --mount, --env, --network, --memory, --pids, --catalog, --no-pull,
        --backend, --unsigned (asked: mcp-unsigned) and --vpn; a writable mount is asked first (mcp-writable-mount)."""
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
            target, source = clean_path(mount.get("target"), "a mount's target"), clean_path(mount.get("host"), "a mount's host directory")
            mode = mount.get("mode") or "ro"
            if mode not in ("ro", "rw") or "=" in target + source or ":" in target + source:
                raise Refused(400, "a mount is TARGET=HOST with ro or rw (no '=' or ':' in the paths)")
            args.append(f"--mount={target}={source}:{mode}")
            if mode == "rw":
                writable.append(source)
        for value in strings(body.get("env", []), "env", 16):
            args.append(f"--env={secret_name(value)}")
        if body.get("network") not in (None, ""):
            if body["network"] not in ("none", "full"):
                raise Refused(400, "network is none or full")
            args.append(f"--network={body['network']}")
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
        if body.get("confirm") is not True:  # the name it gets: --as, else the entry's own (without its catalog)
            local = body.get("as") or entry.rsplit(":", 1)[-1]
            defined = self.json_of(params["host"], ["mcp", "list", "--json"], 60, ok=(0,))
            if any(isinstance(server, dict) and server.get("name") == local for server in defined or []):
                confirmed(body, "mcp-replace", local)
        status, out, err = self.run(params["host"], [*args, entry], 900)  # it pulls the image: that takes its time
        if status != 0:
            said = (err or out).strip()
            if "--unsigned" not in args and MCP_UNSIGNED_HINT in said and "not signed by a trusted key" in said:
                # refused only for its unsigned catalog: the page offers to add it anyway (mcp-unsigned)
                source = MCP_UNSIGNED_SOURCE.search(said)
                raise Refused(502, f"sandbox-ai mcp on {params['host']}: {printable(said[-600:])}",
                              code="mcp-unsigned-catalog",
                              params={"host": params["host"], "entry": entry,
                                      "catalog": source["catalog"] if source else ""})
            raise self.failed(params["host"], args, status, out, err)
        return {"output": printable(out.strip()[-4000:])}

    # logins and identities: the token is always a secret of that host, named, never a value here
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
            return {"ok": True}
        confirmed(body, "git-login-add", url)
        return self.done(params["host"], [*args, url], 30)

    def git_login_remove(self, params, body):
        url = git_url(body.get("url"))
        confirmed(body, "login-remove", url)
        return self.done(params["host"], ["git-logins", "remove", url], 30)

    def git_login_check(self, params, body):
        url = body.get("url")
        return self.checked(params["host"], ["git-logins", "check", *([git_url(url)] if url not in (None, "") else [])])

    def publish_logins(self, params, body):
        return self.json_of(params["host"], ["publish-logins", "list", "--json"], 30)

    def login_status(self, params, body):
        """sandbox-ai login --status: which login each agent's sandboxes get on that host."""
        return self.done(params["host"], ["login", "--status"], 30)

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
            raise Refused(400, "agent is claude, codex or opencode")
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
        except ValueError:
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
            match = TEAM_LINE.fullmatch(line.rstrip())
            if match:
                found.append({"name": match["name"], "members": match["members"].split(", "),
                              "workspace": printable(match["workspace"].strip())[:4096]})
                continue
            match = TEAM_DAMAGED_LINE.fullmatch(line.rstrip())
            if match:
                found.append({"name": match["name"], "members": [], "workspace": "", "damaged": True})
        return found

    def team_show(self, params, body):
        """team show NAME --json: its record, each member's state and the board's tasks."""
        return self.json_of(params["host"], ["team", "show", self.team_name(params["name"]), "--json"], 60, ok=(0,))

    def team_post(self, params, body):
        """team post NAME AGENT TASK: a task for a member, from you (after "--": a task is never an option)."""
        name, agent, task = self.team_name(params["name"]), body.get("agent"), body.get("task")
        if agent not in AGENTS:
            raise Refused(400, "agent is claude, codex or opencode")
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
            raise Refused(400, "an image is an agent (claude, codex, opencode) or a variant's name (1-30 of a-z, "
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
            raise Refused(400, "a variant has a name of its own and comes from claude, codex or opencode")
        return self.json_of(params["host"], ["images", "new", name, f"--from={agent}",
                                             *(["--empty"] if flag(body, "empty") else []), "--json"], 60, ok=(0,))

    def image_recipe(self, params, body):
        """images recipe media|tor --from AGENT [--as VARIANT]: a variant with a ready extension."""
        recipe, agent, variant = body.get("recipe"), body.get("from"), body.get("as")
        if recipe not in IMAGE_RECIPES or agent not in AGENTS:
            raise Refused(400, f"recipe is {' or '.join(IMAGE_RECIPES)}, from claude, codex or opencode")
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


def terminal_command(query, runner=None):
    """argv of a terminal the browser opens (QUERY: the WebSocket URL's parameters): attach to a start, or a new
    start of an agent (in its own terminal, detachable as every start)."""
    hosts = module("hosts")
    host = host_name(query.get("host", ""))
    kind = query.get("kind")
    if kind == "attach":
        return host, hosts.argv(host, ["attach", start_name(query.get("name"))], tty=True)
    if kind == "start":
        agent = query.get("agent")
        if agent not in AGENTS:
            raise Refused(400, "agent is claude, codex or opencode")
        args = [agent]
        if query.get("name"):
            args += ["--name", new_start_name(query["name"])]
        options = strings(json.loads(query.get("options") or "[]"), "options")
        if query.get("workspace"):
            workspace = query["workspace"]
            if not workspace.startswith(("/", "~")) or "\0" in workspace:
                raise Refused(400, "the workspace is an absolute path (or ~/...)")
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
        args += options
        older = newer_needed(host, args)  # an option value that host's sandbox-ai does not know (--ask allow)
        if older:
            raise older
        return host, hosts.argv(host, args, tty=True)
    if kind == "update":
        # The host's update where sudo may ask for its password (hosts update said it wants one).
        version = query.get("version") or None
        if version is not None and not hosts.VERSION.fullmatch(version):
            raise Refused(400, "version is X.Y.Z")
        if host == hosts.LOCAL:
            raise Refused(400, "this machine updates with: sandbox-ai update (in a terminal of its own)")
        try:
            return host, hosts.update_terminal(host, version)
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
        raise Refused(400, "kind is attach, start, update, install, login, plugins, build, trust-hooks, team-start or "
                           "demo-device")
    older = newer_needed(host, args)  # an option that host's sandbox-ai does not know: said, not run
    if older:
        raise older
    return host, hosts.argv(host, args, tty=True)


def terminal_confirmed(query, action, detail):
    """A terminal that NEEDS_CONFIRM ACTION: its query carries confirm=1 (the page asked first, naming the host)."""
    if query.get("confirm") != "1":
        raise Refused(400, f"{detail}: {NEEDS_CONFIRM[action]}: confirm=1", confirm=action)


def tool_command(kind, query, runner=None):
    """The sandbox-ai arguments of the interactive tools a terminal runs (None: no such kind): an agent's login (or
    its logout, asked first), the plugin store, an image build, a kit's hooks trusted (asked first; the command
    shows them and asks again) or revoked, the demo device. Every value is checked; nothing starts with "-"."""
    if kind == "login":
        agent = query.get("agent")
        if agent not in AGENTS:
            raise Refused(400, "agent is claude, codex or opencode")
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
        if agent not in AGENTS:
            raise Refused(400, "agent is claude, codex or opencode")
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
            raise Refused(400, "target is codex, claude, opencode, all, team, vpn, tor or a variant's name")
        only = {"1": True, "0": False, None: False, "": False}.get(query.get("extension_only"), "bad")
        if only == "bad" or only and target in ("team", "vpn", "tor"):
            raise Refused(400, "extension_only is 1 or 0, and not for team, vpn or tor")
        # --pin VERSION (one agent's own version), --uv TAG, --node IMAGE (the base images): as build checks them
        pin, uv, node = (query.get(key) or "" for key in ("pin", "uv", "node"))
        if pin and not (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin) and target in AGENTS):
            raise Refused(400, "pin is X.Y.Z, for one agent's image (claude, codex or opencode)")
        if uv and not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", uv) or \
                node and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]{0,255}", node):
            raise Refused(400, "uv is an image tag, node an image name (with a tag or an @sha256 digest)")
        if (uv or node) and target in ("vpn", "tor"):
            raise Refused(400, "uv and node are for the agents' images, not vpn or tor")
        more = [*(["--pin", pin] if pin else []), *(["--uv", uv] if uv else []), *(["--node", node] if node else [])]
        return ["build", *more, *(["--extension-only"] if only else []), target]
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
        # sandbox-ai team start NAME --agents A,B[,C] [--workspace DIR] -- OPTIONS: the first agent leads; the
        # options (as a start's: strings, never a shell line) hold for every member
        name = query.get("team")
        if not (isinstance(name, str) and TEAM_NAME.fullmatch(name)):
            raise Refused(400, "a team's name is 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit",
                          code="bad-name", params={"what": "team"})
        agents = (query.get("agents") or "").split(",")
        if not 2 <= len(agents) <= 3 or len(set(agents)) != len(agents) or not set(agents) <= set(AGENTS):
            raise Refused(400, "agents: two or three of claude, codex and opencode, comma-separated; the first leads")
        workspace = query.get("workspace") or ""
        if not workspace.startswith(("/", "~")) or "\0" in workspace:  # never the service's own directory (team's ".")
            raise Refused(400, "the shared workspace is an absolute path (or ~/...)")
        try:
            options = strings(json.loads(query.get("options") or "[]"), "options")
        except ValueError:
            raise Refused(400, "options is a JSON list of strings") from None
        # Every member's /work is the team's workspace (team.py passes --workspaces-as-given itself from 0.1.78 on;
        # a remote host of that version whose team.py is older gets it here).
        hosts = module("hosts")
        host = host_name(query.get("host") or hosts.LOCAL)
        given = ["--workspaces-as-given"] if host != hosts.LOCAL and as_given_on(host, runner) \
            and "--workspaces-as-given" not in options else []
        # NAME after the options: 0.1.76 and 0.1.77 took everything after NAME as NAME's own (their team start
        # refused "--agents" then); this order works on every version, as the app's does.
        return ["team", "start", f"--agents={','.join(agents)}", f"--workspace={workspace}", name,
                "--", *given, *options]
    if kind == "demo-device":
        port = query.get("port")
        if port not in (None, ""):
            port = whole(port, "port", 1024, 65535)
        return ["demo-device", "start", *([f"--port={port}"] if port not in (None, "") else [])]
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
                select.select([], [master], [], 0.5)
            except OSError:
                return False
        return True

    def output():
        try:
            while True:
                try:
                    ready, _, _ = select.select([master], [], [], 0.5)
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
            result = self.api.dispatch(method, path, data)
            if method != "GET":
                audit("action", user=session["user"], address=address, method=method, path=path,
                      body=redacted(data))
            return self.json(200, result)
        except Refused as exc:
            if method != "GET" and session is not None:
                audit("refused", user=session["user"], address=address, method=method, path=path, why=str(exc))
            return self.error(exc.status, str(exc), confirm=exc.confirm, code=exc.code, params=exc.params)
        except (OSError, ValueError) as exc:  # what no route turned into a Refused: an answer all the same
            why = printable(f"{type(exc).__name__}: {exc}")[:600]
            with contextlib.suppress(OSError):
                audit("failed", user=session and session["user"], address=address, method=method, path=path, why=why)
            return self.error(500, f"the service could not do that: {why}")

    def login(self, data, address):
        user, password, code = data.get("user"), data.get("password"), data.get("code")
        if not all(isinstance(v, str) and 0 < len(v) <= 1024 for v in (user, password, code)):
            raise Refused(400, "user, password and code are needed")
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
                counter = check_totp(record["totp"], code, record.get("totp_counter")) if ok else None
                if ok and counter is not None:
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
        audit("login", user=user, address=address)
        session = self.sessions.get(token)
        return self.json(200, {"user": user, "csrf": session["csrf"]},
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
        self.send_response(status)
        for key, value in [*headers, ("Content-Length", str(len(body))), ("Content-Security-Policy", CSP),
                           ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                           ("X-Frame-Options", "DENY"), ("Cross-Origin-Opener-Policy", "same-origin"),
                           ("Strict-Transport-Security", "max-age=31536000")]:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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
    return {name for name in names if name}, {f"{name}:{port}" for name in names if name}


def serve(listen=DEFAULT_LISTEN, hostnames=(), cert=None, key=None, out=print, ready=None):
    host, port = parse_listen(listen)
    if not load_users():
        raise ValueError("no login yet: sandbox-ai web user add NAME")
    names, with_port = service_names(host, port, hostnames)
    if cert or key:
        if not (cert and key):
            raise ValueError("--cert and --key go together")
    else:
        cert, key = make_certificate(names)
    server = Server((host, port), context_for(cert, key), ipv6=":" in host)
    actual = server.server_address[1]
    names, with_port = service_names(host, actual, hostnames)
    Handler.service = Service(with_port | (names if actual == 443 else set()))
    write_private(listen_file(), json.dumps({"address": host, "port": actual, "pid": os.getpid()}).encode())
    audit("serve", address=host, port=actual)
    shown = host if ":" not in host else f"[{host}]"
    out(f"sandbox-ai web: https://{'localhost' if shown in ('127.0.0.1', '[::1]') else shown}:{actual}/")
    out(f"  certificate SHA-256 {fingerprint(cert)}")
    if ready:
        ready(server)
    try:
        server.serve_forever()
    finally:
        with contextlib.suppress(OSError):
            if json.loads(listen_file().read_text()).get("pid") == os.getpid():
                listen_file().unlink()
        module("approvals").end_heartbeat()  # no page here watches the questions any more
        server.server_close()


def web_port():
    """The port the running service listens on (None: none runs), for the launcher's check."""
    try:
        record = json.loads(listen_file().read_text())
        os.kill(int(record["pid"]), 0)
        return int(record["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- the command line -------------------------------------------------------------------------------------

def ask(prompt, from_stdin):
    if from_stdin:
        line = sys.stdin.readline()
        if not line:
            raise ValueError("standard input ended")
        return line.rstrip("\n")
    return getpass.getpass(prompt)


def enrol(name, from_stdin, out):
    """A password and a TOTP secret for NAME, the secret confirmed with a code."""
    password = ask("Password (12 characters or more): ", from_stdin)
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs {MIN_PASSWORD} characters or more")
    if ask("Again: ", from_stdin) != password:
        raise ValueError("the two passwords differ")
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


def cli(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai web", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("user", help="the logins")
    users = user.add_subparsers(dest="action", required=True)
    for action in ("add", "remove", "reset"):
        sub = users.add_parser(action)
        sub.add_argument("name")
        if action != "remove":
            sub.add_argument("--stdin", action="store_true", help="read the password twice and the code from "
                                                                  "standard input, one per line")
    users.add_parser("list")
    commands.add_parser("vendor", help="download xterm.js for the terminals (pinned, checked)")
    run = commands.add_parser("serve", help="run the service")
    run.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS:PORT")
    run.add_argument("--hostname", action="append", default=[], metavar="NAME",
                     help="a further name the browser uses for this machine (repeatable)")
    run.add_argument("--cert", type=Path)
    run.add_argument("--key", type=Path)
    cert = commands.add_parser("cert", help="the certificate's fingerprint")
    cert.add_argument("--renew", action="store_true")
    cert.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS:PORT")
    cert.add_argument("--hostname", action="append", default=[], metavar="NAME")
    a = parser.parse_args(argv)
    if a.command == "user":
        if a.action == "list":
            for name, record in sorted(load_users().items()):
                out(f"{name}  since {record.get('created', '?')}")
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
        record = enrol(a.name, a.stdin, out)
        with users_locked() as current:
            current[a.name] = record
        audit("user-" + ("added" if a.action == "add" else "reset"), user=a.name)
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
