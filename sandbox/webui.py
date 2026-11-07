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
address and per user. A session lives 30 minutes without use and 12 hours at most, in memory only; its cookie is
HttpOnly, Secure, SameSite=Strict. Every change needs the session's CSRF token and a same-origin request; the Host
header must be one of the service's names. Every action is written to the audit log
(~/.local/state/sandbox-ai/web/audit.jsonl). The browser gets JSON from a fixed set of actions, each run as
`sandbox-ai ARGS` with its arguments quoted one by one (locally, or over SSH: hosts.py); nothing it sends becomes
a shell command. No sandbox can reach the service: the launcher refuses --allow-host-port for its port.

Standard library only (openssl for the certificate).
"""
import argparse
import base64
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
                "app.css": "text/css; charset=utf-8", "themes.json": "application/json",
                "icon.svg": "image/svg+xml"}
VENDOR_FILES = {"xterm.js": "text/javascript; charset=utf-8", "xterm.css": "text/css; charset=utf-8",
                "addon-fit.js": "text/javascript; charset=utf-8"}
DEFAULT_LISTEN = "127.0.0.1:8443"
USER_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
START_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")
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


_modules = {}


def module(name):
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
    """Failed logins per key (an address, a user): after FREE failures each further try waits, doubling up to
    MAXIMUM seconds; a success clears the key."""

    def __init__(self, free=5, first=30, maximum=900, clock=time.monotonic):
        self.free, self.first, self.maximum, self.clock = free, first, maximum, clock
        self.failures, self.lock = {}, threading.Lock()

    def wait(self, *keys):
        """Seconds until KEYS may try again (0: now)."""
        now = self.clock()
        with self.lock:
            return max([0] + [max(0, until - now) for key in keys for _, until in [self.failures.get(key, (0, 0))]])

    def failed(self, *keys):
        now = self.clock()
        with self.lock:
            for key in keys:
                count, _ = self.failures.get(key, (0, 0))
                count += 1
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

class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def host_name(value):
    hosts = module("hosts")
    if value != hosts.LOCAL and not (isinstance(value, str) and hosts.NAME.fullmatch(value)):
        raise Refused(400, "no such host name")
    return value


NET_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*")
NET_TCP = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:.]+\]):[0-9]{1,5}")
NET_GIT_URL = re.compile(r"https?://(?:[A-Za-z0-9._-]{1,64}@)?(?:[A-Za-z0-9.-]{1,253}|\[::1\])(?::[0-9]{1,5})?/?")
NET_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


CANARY_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
VPN_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
VPN_LOCATION = re.compile(r"[a-z]{2}(?:-[a-z0-9-]{1,20})?")
VPN_LOGIN_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?")


def vpn_name(value):
    if not (isinstance(value, str) and VPN_NAME.fullmatch(value)):
        raise Refused(400, "a profile name is 1-32 lower-case letters, digits, '_' or '-'")
    return value


def start_name(value):
    if not (isinstance(value, str) and START_NAME.fullmatch(value)):
        raise Refused(400, "a start's name is 1-48 letters, digits, '_', '.' or '-'")
    return value


# Kits (sandbox-ai kits): the names as kits.py has them; every value is checked here and again by the command.
KIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KIT_SKILL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # a new skill's name (Claude's rule)
KIT_PLUGIN = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._^~<>=*+-]{0,200}")  # NAME@MARKETPLACE or an npm package
KIT_ACTIONS = ("add-skill", "new-skill", "add-plugin", "add-marketplace", "remove-skill", "remove-plugin")


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
        raise Refused(400, f"{what} is an absolute path on that host")
    return value


def one_line(value, what, limit=1024):
    if not (isinstance(value, str) and value.strip() and len(value) <= limit and value.isprintable()):
        raise Refused(400, f"{what}: one line of at most {limit} characters")
    return value.strip()


def strings(value, what, limit=64):
    if not (isinstance(value, list) and len(value) <= limit and all(isinstance(v, str) and len(v) <= 4096
                                                                       and "\0" not in v for v in value)):
        raise Refused(400, f"{what} is a list of strings")
    return value


class Api:
    """The actions of the web interface: (method, path pattern) -> handler(match, body) -> JSON. RUNNER is
    hosts.run (tests give their own)."""

    def __init__(self, runner=None, updater=None, statuses=None):
        self.runner = runner or (lambda host, args, timeout=120, input=None:
                                 module("hosts").run(host, args, input=input, timeout=timeout))
        # hosts update and hosts list's status (several commands each; tests give their own)
        self.updater = updater or (lambda host, version=None, sudo=None: module("hosts").update(host, version, sudo))
        self.statuses = statuses or (lambda names, refresh=False: module("hosts").statuses(names, refresh=refresh))
        self.routes = [
            ("GET", r"/api/hosts", self.hosts),
            ("POST", r"/api/hosts", self.add_host),
            ("GET", r"/api/hosts-status", self.host_statuses),
            ("POST", r"/api/hosts-status", lambda params, body: self.host_statuses({"refresh": "1"}, body)),
            ("DELETE", r"/api/hosts/(?P<host>[^/]+)", self.remove_host),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/test", self.test_host),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/update", self.update_host),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/starts", self.starts),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/starts/(?P<name>[^/]+)/(?P<action>pause|resume|stop|delete)",
             self.start_action),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/mcp/(?P<name>[^/]+)/attach", self.mcp_attach),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/approvals", self.approvals),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/approvals/(?P<id>[^/]+)", self.answer),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/doctor", self.doctor),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/logs", self.logs),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/llm", self.llm),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/llm/(?P<name>[^/]+)/models", self.llm_models),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/llm", self.add_llm),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/llm/(?P<name>[^/]+)/check", self.check_llm),
            ("DELETE", r"/api/hosts/(?P<host>[^/]+)/llm/(?P<name>[^/]+)", self.remove_llm),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/defaults", self.defaults),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/defaults", self.set_default),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/net/(?P<name>[^/]+)", self.net_show),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/net/(?P<name>[^/]+)", self.net_change),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/presets", self.presets),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/vpn", self.vpn_list),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/vpn", self.vpn_add),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/vpn/(?P<name>[^/]+)/check", self.vpn_check),
            ("DELETE", r"/api/hosts/(?P<host>[^/]+)/vpn/(?P<name>[^/]+)", self.vpn_remove),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/kits", self.kits),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/kits", self.kit_new),
            ("POST", r"/api/hosts/(?P<host>[^/]+)/kits/(?P<name>[^/]+)/(?P<action>[a-z-]+)", self.kit_change),
            ("DELETE", r"/api/hosts/(?P<host>[^/]+)/kits/(?P<name>[^/]+)", self.kit_delete),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/canaries", self.canaries),
            ("GET", r"/api/hosts/(?P<host>[^/]+)/canaries/(?P<run>[^/]+)", self.canaries),
        ]

    def dispatch(self, method, path, body):
        for wanted, pattern, handler in self.routes:
            match = re.fullmatch(pattern, path)
            if match and wanted == method:
                params = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
                return handler(params, body if isinstance(body, dict) else {})
        if any(re.fullmatch(pattern, path) for _, pattern, _ in self.routes):
            raise Refused(405, "method not allowed")
        raise Refused(404, "no such action")

    def run(self, host, args, timeout=120, input=None):
        status, out, err = self.runner(host_name(host), list(args), timeout=timeout, input=input)
        return status, out, err

    def json_of(self, host, args, timeout=120):
        status, out, err = self.run(host, args, timeout)
        try:
            return json.loads(out)
        except ValueError:
            raise Refused(502, f"sandbox-ai {args[0]} on {host}: exit status {status}: "
                               f"{printable((err or out).strip()[-600:])}") from None

    def done(self, host, args, timeout=120, input=None):
        status, out, err = self.run(host, args, timeout, input)
        if status != 0:
            raise Refused(502, f"sandbox-ai {args[0]} on {host}: exit status {status}: "
                               f"{printable((err or out).strip()[-600:])}")
        return {"output": printable(out.strip()[-4000:])}

    # hosts (the list lives on the machine of the service)
    def hosts(self, params, body):
        return module("hosts").entries()

    def host_statuses(self, params, body):
        """{name: {"version", "kind", "available", "update", "error", ...} or null}: each host's sandbox-ai and
        whether an update is there, as hosts list finds it (kept 10 minutes; ?refresh=1 asks again)."""
        hosts = module("hosts")
        names = sorted(hosts.load())
        found = self.statuses(names, refresh=params.get("refresh") == "1") if names else {}
        return {name: ({key: value for key, value in info.items() if key not in ("ssh", "port")} if info else None)
                for name, info in found.items()}

    def add_host(self, params, body):
        hosts = module("hosts")
        name = body.get("name")
        entry = {key: body[key] for key in ("ssh", "port", "identity", "description", "command", "update_sudo")
                 if body.get(key) not in (None, "")}
        try:
            checked = hosts.check(name, entry)
            current = hosts.load()
            if name in current:
                raise ValueError(f"host {name} exists")
            hosts.save({**current, name: checked})
        except ValueError as exc:
            raise Refused(400, printable(exc)) from None
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
        current = hosts.load()
        if params["host"] not in current:
            raise Refused(404, "no such host")
        del current[params["host"]]
        hosts.save(current)
        hosts.forget_status(params["host"])
        return {"removed": params["host"]}

    def test_host(self, params, body):
        """As `sandbox-ai hosts test`: whether the host answers, its sandbox-ai version and its doctor; a key file
        others can read is made private first ("fixed"), and once more when ssh still refuses one as too open."""
        hosts = module("hosts")
        name = host_name(params["host"])
        local = name == hosts.LOCAL
        result = {"name": name, "ssh": None if local else False, "version": None, "doctor": None, "error": None,
                  "fixed": [], "warnings": []}
        entry = None if local else hosts.load().get(name)
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
            result["doctor"] = self.json_of(name, ["doctor", "--json"], 90)
        except Refused as exc:
            result["error"] = str(exc)
        return result

    def update_host(self, params, body):
        """As `sandbox-ai hosts update NAME`: the host's own signed update, old -> new version. {"sudo": true}
        runs it as sudo -n (a system installation); when sudo wants a password, "needs_password" says so and the
        page opens a terminal (kind=update), where sudo asks."""
        name = host_name(params["host"])
        sudo, version = body.get("sudo"), body.get("version")
        if sudo not in (None, True, False):
            raise Refused(400, "sudo is true, false or null (the host's setting)")
        hosts = module("hosts")
        if version is not None and not (isinstance(version, str) and hosts.VERSION.fullmatch(version)):
            raise Refused(400, "version is X.Y.Z")
        try:
            return self.updater(name, version, sudo)
        except ValueError as exc:
            raise Refused(404 if "no host" in str(exc) else 400, printable(exc)) from None

    # starts and sessions
    def starts(self, params, body):
        return self.json_of(params["host"], ["list", "--json"])

    def start_action(self, params, body):
        name, action = start_name(params["name"]), params["action"]
        if action == "delete":
            if body.get("confirm") is not True:
                raise Refused(400, "deleting a start removes its kept HOME: send {\"confirm\": true}")
            return self.done(params["host"], ["delete", name, "--yes"])
        return self.done(params["host"], [action, name], timeout=180)

    def mcp_attach(self, params, body):
        """As `sandbox-ai mcp attach NAME START [--restart]`: the MCP server connected to an agent start (kept for
        its later starts; a running one reaches it after a restart, done now with {"restart": true})."""
        name = params["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise Refused(400, "an MCP server's name is 1-32 letters, digits, '_' or '-'")
        start, restart = start_name(body.get("start")), body.get("restart", False)
        if not isinstance(restart, bool):
            raise Refused(400, "restart is true or false")
        return self.json_of(params["host"], ["mcp", "attach", name, start, "--json", *(["--restart"] if restart else [])],
                            timeout=300)

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
            if not NET_HOST.fullmatch(value):
                raise Refused(400, f"not a host name: {printable(value)}")
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
        if body.get("dns") is True:
            args.append("--dns")
        login = body.get("git_login")
        if login is not None:
            if not (isinstance(login, dict) and login.get("kind") in ("env", "secret")
                    and isinstance(login.get("url"), str) and NET_GIT_URL.fullmatch(login["url"])
                    and isinstance(login.get("ref"), str) and NET_REF.fullmatch(login["ref"])):
                raise Refused(400, "git_login is {url: http(s)://[USER@]HOST[:PORT], kind: env|secret, ref: NAME}")
            args.append(f"--git-token-{login['kind']}={login['url']}={login['ref']}")
        if len(args) == 3:
            raise Refused(400, "nothing to change")
        return self.done(params["host"], args, 60)

    # VPN profiles (sandbox-ai vpn): a profile names the secret that holds its key; the web never carries a key
    def vpn_list(self, params, body):
        return self.json_of(params["host"], ["vpn", "list", "--json"], 30)

    def vpn_add(self, params, body):
        name, kind = body.get("name"), body.get("kind")
        if not (isinstance(name, str) and VPN_NAME.fullmatch(name)):
            raise Refused(400, "a profile name is 1-32 lower-case letters, digits, '_' or '-'")
        if kind not in ("wireguard", "openvpn", "mullvad", "tailscale"):
            raise Refused(400, "kind is wireguard, openvpn, mullvad or tailscale")
        args = ["vpn", "add", name]
        if kind in ("wireguard", "openvpn"):
            path = body.get("file")
            if not (isinstance(path, str) and path.startswith("/") and "\0" not in path and len(path) < 4096):
                raise Refused(400, "file is the configuration's absolute path on that host")
            args.append(f"--{kind}={path}")
        else:
            args.append(f"--{kind}")
        secret = body.get("secret")
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
        return self.done(params["host"], args, 180)

    def vpn_check(self, params, body):
        return self.done(params["host"], ["vpn", "check", vpn_name(params["name"])], 180)

    def vpn_remove(self, params, body):
        return self.done(params["host"], ["vpn", "remove", vpn_name(params["name"])], 60)

    # the canary log (sandbox-ai canaries log): every event, by which process, next to the agent's tool call and
    # model requests; redacted there, and the run's tokens never in it
    def canaries(self, params, body):
        run = params.get("run")
        if run is not None and not (CANARY_RUN.fullmatch(run)):
            raise Refused(400, "a run is 1-81 letters, digits, '_', '.' or '-'")
        document = self.json_of(params["host"], ["canaries", "log", *([run] if run else []), "--json"], 60)
        return [document] if run else document

    def presets(self, params, body):
        return self.json_of(params["host"], ["presets", "list", "--json"], 30)

    # kits (sandbox-ai kits): every change is that command on the host, so its checks apply (a skill folder is a
    # real folder with SKILL.md, nothing followed, limits, a note when it lies where sandboxes can write; built-in
    # kits read-only); the values come after "--", never read as options
    def kits(self, params, body):
        return self.json_of(params["host"], ["kits", "list", "--json"], 60)

    def kit_new(self, params, body):
        return self.done(params["host"], ["kits", "new", *kit_agents(body), "--", kit_name(body.get("name"))], 60)

    def kit_change(self, params, body):
        """add-skill {path, as?, agents?}, new-skill {skill, description?, agents?}, add-plugin {plugin, agent?,
        source?}, add-marketplace {marketplace, source, agent?}, remove-skill {skill, agents?}, remove-plugin
        {plugin, agent?}; with "create": true the kit is created first (sandbox-ai kits new), so what is added to
        a built-in kit's selection lands in a new kit of yours."""
        name, action = kit_name(params["name"]), params["action"]
        if action not in KIT_ACTIONS:
            raise Refused(404, "no such action")
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
        elif action in ("add-plugin", "remove-plugin"):
            plugin = body.get("plugin")
            if not (isinstance(plugin, str) and KIT_PLUGIN.fullmatch(plugin)):
                raise Refused(400, "a plugin is NAME@MARKETPLACE (Claude) or an npm package (OpenCode)")
            values = [plugin]
            options += kit_agents(body, many=False)
            if action == "add-plugin" and body.get("source") not in (None, ""):
                options.append(f"--source={one_line(body['source'], 'source', 2048)}")
        else:  # add-marketplace
            values = [kit_name(body.get("marketplace"), "a marketplace's name"),
                      one_line(body.get("source"), "source", 2048)]
            options += kit_agents(body, many=False)
        if body.get("create") not in (None, False, True):
            raise Refused(400, "create is true or false")
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
        if body.get("confirm") is not True:
            raise Refused(400, "deleting a kit removes its folder: send {\"confirm\": true}")
        return self.done(params["host"], ["kits", "delete", "--yes", "--", kit_name(params["name"])], 60)

    # approvals
    def approvals(self, params, body):
        return self.json_of(params["host"], ["approvals", "list", "--json"], 30)

    def answer(self, params, body):
        ident = params["id"]
        if not APPROVAL_ID.fullmatch(ident):
            raise Refused(400, "no such question")
        decision, scope = body.get("decision"), body.get("scope")
        if decision not in ("allow", "deny"):
            raise Refused(400, "decision is allow or deny")
        if scope not in (None, "once", "run", "always"):
            raise Refused(400, "scope is once, run or always")
        # An "always" waits up to approvals.VERIFY_WAIT for the sandbox before it is remembered; the call (over ssh,
        # perhaps, with a name to resolve) must outlast that clearly, or the "always" is lost.
        return self.done(params["host"], ["approvals", decision, ident, *(["--scope", scope] if scope else [])],
                         module("approvals").VERIFY_WAIT + 60)

    # the doctor and the logs
    def doctor(self, params, body):
        return self.json_of(params["host"], ["doctor", "--json"], 120)

    def logs(self, params, body):
        return self.json_of(params["host"], ["logs", "--json"], 60)

    # LLM servers (sandbox-ai llm)
    def llm(self, params, body):
        return self.json_of(params["host"], ["llm", "list", "--json"], 30)

    def add_llm(self, params, body):
        name, kind, url, model = (body.get(k) for k in ("name", "kind", "url", "model"))
        if not all(isinstance(v, str) and v and len(v) <= 2048 and not v.startswith("-") for v in (name, kind, url)):
            raise Refused(400, "name, kind and url are needed")
        args = ["llm", "add", name, "--kind", kind, "--url", url]
        if isinstance(model, str) and model:
            if model.startswith("-"):
                raise Refused(400, "a model does not start with '-'")
            args += ["--model", model]
        return self.done(params["host"], args, 60)

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
        return self.done(params["host"], ["llm", "remove", params["name"]], 30)

    # sandbox-ai's defaults (defaults.json) on the host: the agents' telemetry, the mode, canaries
    def defaults(self, params, body):
        return self.json_of(params["host"], ["defaults", "show", "--json"], 30)

    def set_default(self, params, body):
        key, value = body.get("key"), body.get("value")
        if not (isinstance(key, str) and re.fullmatch(r"[a-z_]{1,32}", key)):
            raise Refused(400, "no such key")
        if value is None:
            return self.done(params["host"], ["defaults", "unset", key], 30)
        return self.done(params["host"], ["defaults", "set", key, json.dumps(value)], 30)


def terminal_command(query):
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
            args += ["--name", start_name(query["name"])]
        if query.get("workspace"):
            workspace = query["workspace"]
            if not workspace.startswith(("/", "~")) or "\0" in workspace:
                raise Refused(400, "the workspace is an absolute path (or ~/...)")
            args += ["--workspace", workspace]
        options = json.loads(query.get("options") or "[]")
        args += strings(options, "options")
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
    raise Refused(400, "kind is attach, start or update")


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
                self.close(1009)
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
        if self.closed and opcode != 8:
            return
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 1 << 16:
            header += bytes([126]) + struct.pack(">H", length)
        else:
            header += bytes([127]) + struct.pack(">Q", length)
        with self.send_lock:
            try:
                self.stream.write(header + payload)
                self.stream.flush()
            except (OSError, ValueError):
                self.closed = True

    def close(self, code=1000):
        if not self.closed:
            self.send(struct.pack(">H", code), 8)
            self.closed = True


def bridge(ws, argv, cols=100, rows=30, alive=None, every=2.0, clock=time.monotonic):
    """ARGV in a terminal of its own, its output to WS (binary frames), WS's binary frames to its input, and
    {"resize": [cols, rows]} as text frames; ends when either side does. The command's process group gets SIGHUP
    at the end: an attach then simply detaches, the start runs on."""
    pid, master = pty.fork()
    if pid == 0:  # the child: the command, in the new terminal
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    set_size(master, cols, rows)
    checked, pinged = [clock()], [clock()]

    def gone():
        """True once ALIVE says the session ended (logout, its idle or 12-hour limit, its user removed or reset):
        an open terminal ends with it. Asked every EVERY seconds, not at each key."""
        if alive is None or clock() - checked[0] < every:
            return False
        checked[0] = clock()
        return not alive()

    def output():
        while True:
            try:
                ready, _, _ = select.select([master], [], [], 0.5)
                if ws.closed:
                    break
                if gone():
                    ws.close(1008)
                    break
                if clock() - pinged[0] >= WS_PING:  # a browser that is still there answers (the read deadline)
                    pinged[0] = clock()
                    ws.send(b"", 9)
                if not ready:
                    continue
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            ws.send(data, 2)
        ws.close(1000)
        if ws.hang_up:  # the browser answers the close at once; if not, the connection ends anyway
            threading.Timer(3, ws.hang_up).start()

    reader = threading.Thread(target=output, daemon=True)
    reader.start()
    try:
        while not ws.closed:
            try:
                kind, payload = ws.receive()
            except (ConnectionError, OSError):
                break
            if kind == 8:
                break
            if gone():
                ws.close(1008)
                break
            if kind == 2:
                os.write(master, payload)
            elif kind == 1:
                with contextlib.suppress(ValueError, TypeError, KeyError):
                    message = json.loads(payload)
                    cols, rows = (int(v) for v in message["resize"])
                    if 1 <= cols <= 1000 and 1 <= rows <= 1000:
                        set_size(master, cols, rows)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGHUP)
        reader.join(5)
        with contextlib.suppress(OSError):
            os.close(master)
        with contextlib.suppress(ChildProcessError):
            for _ in range(50):
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                time.sleep(0.1)
            else:
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
        self.terminal = terminal or (lambda query: terminal_command(query))

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
        """(status, headers, body bytes) of one request."""
        if not self.host_ok(headers):
            return self.error(421, "unknown host name; start the service with --hostname NAME for it")
        path = urllib.parse.urlsplit(target).path
        if method == "GET" and not path.startswith("/api/"):
            return self.static(path, headers)
        session = self.sessions.current(self.cookie(headers), load_users())
        try:
            if method != "GET":
                if not self.same_origin(headers):
                    raise Refused(403, "a change must come from this page")
                if (headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                    raise Refused(415, "send JSON")
            data = None
            if body:
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise Refused(400, "invalid JSON") from None
            if path == "/api/login" and method == "POST":
                return self.login(data or {}, address)
            if session is None:
                raise Refused(401, "log in")
            if method != "GET" and not hmac.compare_digest(headers.get("X-CSRF-Token") or "", session["csrf"]):
                raise Refused(403, "the page is out of date: reload it")
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
            return self.error(exc.status, str(exc))

    def login(self, data, address):
        user, password, code = data.get("user"), data.get("password"), data.get("code")
        if not all(isinstance(v, str) and 0 < len(v) <= 1024 for v in (user, password, code)):
            raise Refused(400, "user, password and code are needed")
        keys = (f"address:{address}", f"user:{user}")
        # One login check at a time: parallel tries all passed wait() before any failure was counted, so each got a
        # full password and TOTP check (thousands of codes per lockout). scrypt makes one check take ~0.1 s.
        with self.login_lock:
            return self._login(user, password, code, keys, address)

    def _login(self, user, password, code, keys, address):
        wait = self.limiter.wait(*keys)
        if wait:
            audit("login-throttled", user=printable(user)[:64], address=address)
            return self.error(429, f"too many failed logins: try again in {int(wait) + 1} seconds",
                              [("Retry-After", str(int(wait) + 1))])
        with users_locked() as users:
            record = users.get(user) if USER_NAME.fullmatch(user) else None
            ok = check_password(password, (record or {}).get("password"))
            counter = check_totp(record["totp"], code, record.get("totp_counter")) if ok else None
            if ok and counter is not None:
                record["totp_counter"] = counter
        if not ok or counter is None:
            self.limiter.failed(*keys)
            audit("login-failed", user=printable(user)[:64], address=address)
            raise Refused(401, "wrong user, password or code")
        self.limiter.succeeded(*keys)
        token = self.sessions.new(user, address, user_fingerprint(record))
        audit("login", user=user, address=address)
        session = self.sessions.get(token)
        return self.json(200, {"user": user, "csrf": session["csrf"]},
                         [("Set-Cookie", self.set_cookie(token, LIFETIME))])

    @staticmethod
    def set_cookie(token, age):
        return f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}"

    def websocket(self, target, headers, address):
        """(argv, session) for a terminal WebSocket at TARGET, or Refused."""
        if not self.host_ok(headers):
            raise Refused(421, "unknown host name")
        if not self.same_origin(headers):
            raise Refused(403, "a terminal must come from this page")
        session = self.sessions.current(self.cookie(headers), load_users())
        if session is None:
            raise Refused(401, "log in")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
        if not hmac.compare_digest(query.get("csrf", ""), session["csrf"]):
            raise Refused(403, "the page is out of date: reload it")
        try:
            host, argv = self.terminal(query)
        except ValueError as exc:
            raise Refused(400, printable(exc)) from None
        if session["terminals"] >= TERMINALS_PER_SESSION:
            raise Refused(429, f"at most {TERMINALS_PER_SESSION} terminals at once")
        audit("terminal", user=session["user"], address=address, host=host,
              kind=query.get("kind"), name=query.get("name") or query.get("agent"))
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

    def error(self, status, message, extra=()):
        return self.json(status, {"error": message}, extra)


def redacted(data):
    """DATA for the audit log: values of keys that may hold secrets replaced."""
    if isinstance(data, dict):
        return {k: "***" if re.search(r"pass|secret|token|key|code", k, re.I) else redacted(v) for k, v in data.items()}
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


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "sandbox-ai", ""
    service = None  # set by serve()
    timeout = 120  # seconds an idle HTTP connection is kept (a terminal clears it: it may be quiet for long)

    def setup(self):
        handshake = getattr(self.request, "do_handshake", None)
        if handshake is not None:  # TLS, in this connection's thread, bounded: never the accept loop's
            self.request.settimeout(30)
            handshake()
        super().setup()

    def log_message(self, format, *args):  # noqa: A002 - the audit log has what matters
        pass

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
        body = self.rfile.read(size) if size else b""
        self.send(*self.service.handle(self.command, self.path, self.headers, body, self.client_address[0]))

    def do_GET(self):  # noqa: N802
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.upgrade()
        self.answer()

    do_POST = do_DELETE = do_PUT = do_PATCH = answer  # noqa: N815

    def upgrade(self):
        try:
            argv, session = self.service.websocket(self.path, self.headers, self.client_address[0])
            key = self.headers.get("Sec-WebSocket-Key") or ""
            if self.headers.get("Sec-WebSocket-Version") != "13" or len(base64.b64decode(key, validate=True)) != 16:
                raise Refused(400, "a WebSocket version 13 handshake is needed")
        except (Refused, ValueError) as exc:
            self.send(*self.service.error(getattr(exc, "status", 400), str(exc)))
            self.close_connection = True
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        size = [int(query[k]) if query.get(k, "").isdigit() else d for k, d in (("cols", 100), ("rows", 30))]
        session["terminals"] += 1
        try:
            token = self.service.cookie(self.headers)
            sessions = self.service.sessions
            # A terminal may be quiet for long, its browser is not: it answers the pings. Nothing heard for
            # WS_DEADLINE seconds (a browser that is gone), or output not taken for that long (one that reads
            # nothing: the send lock is never held longer), ends the terminal.
            self.connection.settimeout(WS_DEADLINE)
            with contextlib.suppress(OSError):
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            bridge(WebSocket(Stream(self.rfile, self.wfile), hang_up=self.hang_up), argv, *size,
                   alive=lambda: sessions.current(token, load_users(), touch=False) is session)
        finally:
            session["terminals"] -= 1


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

    def __init__(self, address, context, ipv6=False):
        self.address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        self.context = context
        super().__init__(address, Handler)

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
