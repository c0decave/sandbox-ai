#!/usr/bin/env python3
"""sandbox-ai's relay of one OAuth login callback into the sandbox that asked.

An agent's browser login sends the browser back to a server of the agent on the sandbox's own loopback: Codex's
`codex login` (without --device-auth) to http://127.0.0.1:1455/auth/callback, Claude Code's to
http://localhost:PORT/callback, an MCP server's OAuth to its own port. A browser outside the sandbox never reaches it.
When the agent opens such a URL (its xdg-open: a browser question, approvals) and the human allows it, sandbox-ai
starts a ONE-SHOT catcher on the machine where the browser opens:

- bound to 127.0.0.1:PORT (or [::1]:PORT for a redirect to [::1]) only - the port of the URL's redirect_uri,
  redirect_url or callback parameter, nothing else;
- it takes exactly one request to the callback's path (GET or POST, a top-level navigation, Host 127.0.0.1|localhost|
  [::1]:PORT) within WAIT_SECONDS - and when the login URL has a `state`, only the one that carries that state (in its
  query or a form POST's body): another path, another Host, a script's request, another state or a second request
  are refused, and the catcher keeps waiting for the login's own (another local process or a page cannot use the one
  request up); the state reaches the catcher's process in its environment, never on its command line;
- that one request (method, path, query, body; never the browser's cookies) goes to the sandbox's 127.0.0.1:PORT
  through the run's control socket (runtime.oauth_callback; on another host over its SSH connection: `sandbox-ai
  approvals callback ID` there), never through a tunnel; the host's sandbox-ai checks it against the question again
  (an allowed browser question of a running sandbox, the same port and path, once: ID.callback);
- the agent's answer page comes back to the browser (inert: no script runs), and the catcher closes.

The port busy on this machine, or no way to forward: the answer says to use the agent's device-code login instead
(codex login --device-auth). What happened is recorded beside the question (approvals/ID.callback: the run's activity
and report) and in launcher.log.

Standard library only.
"""
import base64
import contextlib
import errno
import hmac
import html
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = Path(__file__).resolve().parent
CALLBACK_PARAMS = ("redirect_uri", "redirect_url", "callback")
# The host names a callback may name, and the address its catcher (and the sandbox's side) uses for each.
LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}
WAIT_SECONDS = 600          # the catcher waits this long for the one request, then closes
LATE_SECONDS = 60           # the host's side takes it until WAIT_SECONDS + this after the answer
CONNECTION_SECONDS = 15     # one browser connection: its request read within
FORWARD_SECONDS = 60        # the sandbox's answer (the agent may exchange the code first)
HEAD_LIMIT = 16 << 10
BODY_LIMIT = 1 << 20
METHODS = ("GET", "POST")
FORWARDED_HEADERS = ("accept", "accept-language", "content-type", "user-agent")
RECORD = ".callback"        # approvals/ID.callback: what became of the question's callback
# approvals/ID.listener: the socket (and its process) that listened on the callback's port in the sandbox when the
# human allowed the login, as the supervisor said (pin_listener). The callback goes to that socket or to none: a
# process of the sandbox that takes the port afterwards does not get the login's code (security review SAI-26).
PINNED = ".listener"
PIN_SECONDS = 5             # the supervisor's answer to "who listens there" (none in time: not pinned)
REQUEST_LIMIT = 4 << 20     # `approvals callback ID`: its request on stdin
DEVICE_HINT = "use the agent's device-code login instead (Codex: codex login --device-auth)"
BUSY = "port busy"          # a record's outcome: no catcher could listen (start); no relay, so it uses up none
TAKE_SECONDS = 3.0          # an older catcher of this user on the port closes within (start)
# The agent's page in the human's browser: shown, never run (no script, no form, no frame, nothing loaded).
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; " \
           "frame-ancestors 'none'; base-uri 'none'; sandbox"
REASONS = {200: "OK", 302: "Found", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           408: "Request Timeout", 410: "Gone", 411: "Length Required", 413: "Content Too Large",
           421: "Misdirected Request", 502: "Bad Gateway"}
ID = re.compile(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}")
STATE_VARIABLE = "SANDBOX_AI_CALLBACK_STATE"  # the login's state, for the catcher's process (start, catch)
STATE_LIMIT = 1024
LOADING = threading.RLock()
_modules = {}


def module(name):
    """sandbox/NAME.py, loaded once by its path."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_oauth_relay", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def log():
    return module("launchlog")


def shown(value, limit=300):
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


# --- which URLs carry a callback to the sandbox's loopback --------------------------------------------------------------

def callback_target(url):
    """{"port", "path", "bind", "host", "url", "param"} of the callback to a loopback server that URL (http or https)
    sends the browser back to - a redirect_uri, redirect_url or callback parameter of its query naming
    http://127.0.0.1|localhost|[::1]:PORT/PATH - or None (none, an https or other host's callback, no port, several
    different ones)."""
    if not isinstance(url, str) or len(url) > 8192:
        return None
    try:
        query = urllib.parse.urlsplit(url).query
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=200)
    except ValueError:
        return None
    found = {}
    for name, value in pairs:
        if name.lower() not in CALLBACK_PARAMS:
            continue
        target = loopback_callback(value)
        if target is not None:
            target["param"] = name.lower()
            found.setdefault((target["port"], target["path"], target["bind"]), target)
    if len(found) != 1:
        return None
    target = next(iter(found.values()))
    # The login's state (OAuth: the callback carries it back): its one value, or None (none, several, unusable).
    states = {value for name, value in pairs if name == "state"}
    state = next(iter(states)) if len(states) == 1 else None
    target["state"] = state if state and len(state) <= STATE_LIMIT and re.fullmatch(r"[!-~]+", state) else None
    return target


def target_state(target):
    """The state a callback must carry for TARGET: its own ("state"), or the catcher process's (STATE_VARIABLE: the
    state never goes on a command line); None: any."""
    state = target.get("state") if isinstance(target, dict) else None
    return state or os.environ.get(STATE_VARIABLE) or None


def loopback_callback(value):
    """The target of one callback VALUE (http://127.0.0.1|localhost|[::1]:PORT/PATH), or None."""
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,2048}", value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme != "http" or host not in LOOPBACK or port is None or not 1 <= port <= 65535 \
            or parts.username is not None or parts.password is not None or "\\" in value:
        return None
    path = parts.path or "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,1000}", path):
        return None
    shown_host = "[::1]" if host == "::1" else host
    return {"port": port, "path": path, "bind": LOOPBACK[host], "host": host,
            "url": f"http://{shown_host}:{port}{path}"}


def question_note(url):
    """What a browser question says of URL's callback (approvals.describe), or ""."""
    target = callback_target(url)
    if target is None:
        return ""
    return (f"; its login callback goes to {target['url']} in the sandbox: when you open it, sandbox-ai relays that "
            f"one request into this sandbox (port {target['port']} on your machine, {WAIT_SECONDS // 60} minutes at "
            f"most); if that port is busy there, {DEVICE_HINT}")


def fields(url):
    """What `approvals list --json` adds for a browser question with a callback (the app and the web start the
    catcher where the browser is): {"callback": {"url", "port", "path"}} or {}."""
    target = callback_target(url)
    return {"callback": {"url": target["url"], "port": target["port"], "path": target["path"]}} if target else {}


# --- the catcher ----------------------------------------------------------------------------------------------------------

def bind(target):
    """A listening socket on the callback's loopback address and port only (OSError when it is busy)."""
    family = socket.AF_INET6 if target["bind"] == "::1" else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == "posix":  # (a port in TIME_WAIT; never a second listener: that needs SO_REUSEPORT)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((target["bind"], int(target["port"])))
        listener.listen(8)
    except OSError:
        listener.close()
        raise
    return listener


class Refusal(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


class Catcher:
    """Takes the one callback request on LISTENER (bind(TARGET)) and answers it with FORWARD(request) - request
    {"method", "target", "host", "headers", "body"}, answer {"status", "content_type", "body" (bytes), "location"} -
    then closes; closes after WAIT seconds without it too. serve() returns what happened: {"outcome": "relayed" |
    "failed" | "timed out", "status", "why", "refused"}."""

    def __init__(self, listener, target, forward, wait=WAIT_SECONDS, clock=time.monotonic):
        self.listener, self.target, self.forward, self.clock = listener, target, forward, clock
        self.deadline = clock() + wait
        self.refused = 0
        self.closing = None  # why it closes before its time (close(): a newer login took its port)
        self.hosts = {f"{name}:{target['port']}" for name in ("127.0.0.1", "localhost", "[::1]")}
        self.state = target_state(target)

    def serve(self):
        try:
            while True:
                left = self.deadline - self.clock()
                if self.closing:
                    return {"outcome": "closed", "why": self.closing, "refused": self.refused}
                if left <= 0:
                    return {"outcome": "timed out", "why": f"no callback within {WAIT_SECONDS // 60} minutes",
                            "refused": self.refused}
                self.listener.settimeout(min(left, 0.5))
                try:
                    connection, peer = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    time.sleep(0.05)
                    continue
                with connection:
                    done = self.handle(connection, peer)
                if done is not None:
                    return {**done, "refused": self.refused}
        finally:
            with contextlib.suppress(OSError):
                self.listener.close()

    def close(self, why):
        """Close before the one request came (between two connections; a request being relayed is finished)."""
        self.closing = why

    def handle(self, connection, peer):
        """One browser connection: None when it was refused (the catcher waits on), else what became of the one."""
        try:
            if not ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).is_loopback:
                raise Refusal(403, "only this computer's browser")
            request = self.read(connection)
        except Refusal as exc:
            self.refused += 1
            log().info("approvals", f"login callback on port {self.target['port']}: a request refused ({exc})")
            send(connection, exc.status, page("Not relayed", str(exc)))
            return None
        except (OSError, ValueError) as exc:
            self.refused += 1
            log().info("approvals", f"login callback on port {self.target['port']}: a request refused ({exc})")
            return None
        # The one request: whatever comes of it, the catcher closes after it.
        try:
            answer = self.forward(request)
        except (OSError, ValueError) as exc:
            why = shown(exc, 500)
            send(connection, 502, page("The login callback was not relayed",
                                       f"sandbox-ai could not hand it to the sandbox: {why}. Start the login again "
                                       f"and {DEVICE_HINT}."))
            return {"outcome": "failed", "why": why}
        send_answer(connection, answer)
        return {"outcome": "relayed", "status": answer.get("status")}

    def carries_state(self, target, headers, body):
        """Whether the request (its TARGET's query, or a form POST's BODY) carries the login's state, once."""
        found = []
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            found += [value for name, value in urllib.parse.parse_qsl(target.partition("?")[2], keep_blank_values=True,
                                                                       max_num_fields=200) if name == "state"]
            if body and headers.get("content-type", "").split(";")[0].strip().lower() == \
                    "application/x-www-form-urlencoded":
                found += [value for name, value in urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True,
                                                                           max_num_fields=200) if name == "state"]
        return len(found) == 1 and hmac.compare_digest(found[0].encode(), self.state.encode())

    def read(self, connection):
        """The browser's request on CONNECTION, checked: Refusal for anything but the one callback."""
        end = time.monotonic() + CONNECTION_SECONDS
        data = b""
        while b"\r\n\r\n" not in data:
            left = end - time.monotonic()
            if left <= 0:
                raise Refusal(408, "the request took too long")
            connection.settimeout(left)
            chunk = connection.recv(4096)
            if not chunk:
                raise ValueError("the connection ended")
            data += chunk
            if len(data) > HEAD_LIMIT + BODY_LIMIT:
                raise Refusal(413, "the request is too long")
        head, _, body = data.partition(b"\r\n\r\n")
        if len(head) > HEAD_LIMIT:
            raise Refusal(413, "the request's head is too long")
        lines = head.decode("latin-1").split("\r\n")
        match = re.fullmatch(r"([A-Z]{1,10}) (/[!-~]{0,8191}) HTTP/1\.[01]", lines[0])
        if not match:
            raise Refusal(400, "not an HTTP request for a path")
        method, target = match.groups()
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep or not re.fullmatch(r"[!-~]{1,100}", name):
                raise Refusal(400, "a header is malformed")
            headers.setdefault(name.strip().lower(), value.strip())
        if headers.get("host", "").lower() not in self.hosts:
            raise Refusal(421, f"only requests for {self.target['url']} are taken here")
        path = target.split("?", 1)[0]
        if path != self.target["path"]:
            raise Refusal(404, f"only the login callback ({self.target['path']}) is relayed here")
        if method not in METHODS:
            raise Refusal(405, f"only {' or '.join(METHODS)}")
        # A top-level navigation of the browser only: a page's script (fetch, an image, a frame) is no callback.
        if headers.get("sec-fetch-mode", "navigate") != "navigate" or \
                headers.get("sec-fetch-dest", "document") != "document":
            raise Refusal(403, "only the browser's own navigation to the callback")
        if headers.get("transfer-encoding"):
            raise Refusal(411, "send a Content-Length")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            raise Refusal(400, "the Content-Length is no number") from None
        if not 0 <= length <= BODY_LIMIT or (length and method != "POST"):
            raise Refusal(413, "the body is too long (or not a POST's)")
        while len(body) < length:
            left = end - time.monotonic()
            if left <= 0:
                raise Refusal(408, "the request took too long")
            connection.settimeout(left)
            chunk = connection.recv(65536)
            if not chunk:
                raise ValueError("the connection ended")
            body += chunk
        if self.state is not None and not self.carries_state(target, headers, body[:length]):
            raise Refusal(404, "not this login's callback (its state is another): the catcher waits for its own")
        return {"method": method, "target": target, "host": headers["host"].lower(),
                "headers": {name: headers[name] for name in FORWARDED_HEADERS
                            if name in headers and re.fullmatch(r"[ -~]{0,1000}", headers[name])},
                "body": body[:length]}


def page(title, text):
    return (f"<!doctype html><meta charset=utf-8><title>sandbox-ai: {html.escape(title)}</title>"
            f"<body style=\"font-family:sans-serif;max-width:40em;margin:3em auto\"><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(text)}</p></body>").encode()


def send(connection, status, body, content_type="text/html; charset=utf-8"):
    head = [f"HTTP/1.1 {status} {REASONS.get(status, 'Answer')}", f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}", f"Content-Security-Policy: {PAGE_CSP}", "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer", "Cache-Control: no-store", "Connection: close"]
    with contextlib.suppress(OSError):
        connection.settimeout(10)
        connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)


def send_answer(connection, answer):
    """The agent's answer to the browser: its status and page (inert, PAGE_CSP); a redirect elsewhere as a page with
    the link (nothing is followed without the human; sandbox-ai sets no cookie)."""
    status = answer.get("status") if isinstance(answer.get("status"), int) else 502
    body = answer.get("body") if isinstance(answer.get("body"), bytes) else b""
    kind = str(answer.get("content_type") or "")
    if not re.fullmatch(r"[!-~][ -~]{0,199}", kind) or not kind.split(";")[0].strip().lower().startswith(
            ("text/", "application/json", "image/")):
        kind = "text/plain; charset=utf-8"
    if 300 <= status < 400:
        location = answer.get("location") if isinstance(answer.get("location"), str) else ""
        text = "The agent in the sandbox has the login callback; it sent your browser on to: " + shown(location, 2000)
        status, kind = 200, "text/html; charset=utf-8"
        body = page("Login callback relayed", text) + (
            f"<p><a href=\"{html.escape(location)}\" rel=noreferrer>{html.escape(shown(location, 200))}</a></p>"
            .encode() if re.fullmatch(r"https?://[!-~]{1,2000}", location) else b"")
    if not 100 <= status <= 599:
        status = 502
    send(connection, status, body, kind)


# --- the host's side: one request into the run that asked ---------------------------------------------------------------

APPROVALS = None  # approvals.py when it loaded this module (its answered_question and net), else loaded here


def approvals():
    return APPROVALS if APPROVALS is not None else module("approvals")


def net():
    return approvals().net()


def find_question(ident, runs=None):
    """(run, record, question with "result") of the answered browser question IDENT of a running sandbox, or
    ValueError."""
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError("not a question ID")
    found = approvals().answered_question(ident, runs if runs is not None else net().running())
    if found is None:
        raise ValueError(f"no answered question {ident} of a running sandbox")
    name, record, question = found
    if question.get("kind") != "browser":
        raise ValueError(f"question {ident} is no browser question")
    if (question.get("result") or {}).get("decision") != "allow":
        raise ValueError(f"question {ident} was not allowed")
    return name, record, question


def record_path(record, ident):
    return Path(record["directory"]) / "approvals" / f"{ident}{RECORD}"


def write_record(path, details, fd=None):
    """Write DETAILS (JSON) to the question's record PATH: into FD (made with O_EXCL by relay_once), else only when
    there is none yet."""
    data = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **details}).encode()
    try:
        if fd is None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        else:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
    except FileExistsError:
        return False
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
    return True


def read_record(path):
    """The record PATH (ID.callback) as a dict ({} when it is none: not there, a link, not JSON)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return {}
    try:
        found = json.loads(os.read(fd, 1 << 16))
    except (OSError, ValueError, RecursionError):
        return {}
    finally:
        os.close(fd)
    return found if isinstance(found, dict) else {}


def pin_listener(ident, target, record_dir, ask=None):
    """Ask the run's supervisor who listens on the callback's port in the sandbox now (control op "oauth-listener")
    and keep its one socket for the callback (PINNED, written once); a few words for the answer's note, "" when
    nothing was pinned: no listener yet, more than one (the supervisor refuses the callback then), or a sandbox
    started by a sandbox-ai before 0.1.117 (its supervisor does not know the question)."""
    try:
        answer = (ask or net().ask)(record_dir, {"op": "oauth-listener", "port": target["port"]}, timeout=PIN_SECONDS)
    except (OSError, ValueError):
        return ""
    found = answer.get("listeners") if isinstance(answer, dict) else None
    found = [entry for entry in found if isinstance(entry, dict) and isinstance(entry.get("inode"), int)
             and not isinstance(entry.get("inode"), bool)] if isinstance(found, list) else []
    if len(found) != 1:
        return ""
    entry = found[0]
    kept = {"question": ident, "port": target["port"], "inode": entry["inode"],
            **({"pid": entry["pid"]} if isinstance(entry.get("pid"), int) else {}),
            **({"comm": shown(entry["comm"], 64)} if isinstance(entry.get("comm"), str) else {}),
            **({"exe": shown(entry["exe"], 300)} if isinstance(entry.get("exe"), str) else {})}
    if not write_record(Path(record_dir) / "approvals" / f"{ident}{PINNED}", kept):
        return ""
    who = f"{kept['comm']} (pid {kept['pid']})" if "comm" in kept and "pid" in kept else "the process listening there"
    log().info("approvals", f"question {ident}: login callback port {target['port']}: in the sandbox {who} listens "
                            f"(socket {kept['inode']}); the callback goes to that socket only")
    return f"; in the sandbox {who} takes it, and only that process's socket"


def take_once(marker):
    """The question's one relay taken: its record MARKER made exclusively (the descriptor), or FileExistsError when
    it was taken before. A record that only says a start found the port busy (BUSY: no catcher was started by it,
    nothing was relayed - the question's catcher held the port, as when "Open here" is clicked twice) is no relay:
    it is replaced, by one taker at a time (a lock beside it)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(marker, flags, 0o600)
    except FileExistsError:
        if read_record(marker).get("outcome") != BUSY:
            raise
    try:
        import fcntl
    except ImportError:
        raise FileExistsError(str(marker)) from None
    lock = os.open(marker.with_name(".callback.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read_record(marker).get("outcome") != BUSY:  # (another taker was first)
            raise FileExistsError(str(marker))
        os.unlink(marker)
        return os.open(marker, flags, 0o600)
    finally:
        os.close(lock)


def relay_once(ident, request, runs=None, ask=None, clock=time.time):
    """The one callback REQUEST (the catcher's) into the sandbox whose browser question IDENT asked: checked against
    the question (allowed, its callback's port and path, within WAIT_SECONDS + LATE_SECONDS of the answer), taken once
    (ID.callback, O_EXCL), sent through the run's control socket (ASK: net.ask). The sandbox's answer
    {"status", "content_type", "body" (bytes), "location"}, or ValueError."""
    name, record, question = find_question(ident, runs)
    target = callback_target(question.get("url"))
    if target is None:
        raise ValueError(f"question {ident}'s URL has no callback to the sandbox's loopback")
    # (from another machine's catcher too, `approvals callback ID`: JSON of any shape)
    if not isinstance(request, dict) or not isinstance(request.get("method"), str) or \
            request["method"] not in METHODS or not isinstance(request.get("target"), str) or \
            isinstance(request.get("port"), bool) or not isinstance(request.get("port"), int) or \
            not isinstance(request.get("headers") or {}, dict) or \
            not isinstance(request.get("body") or b"", (bytes, bytearray)) or \
            not isinstance(request.get("host") or "", str):
        raise ValueError("not a callback request")
    path = request["target"].split("?", 1)[0]
    if request["port"] != target["port"] or path != target["path"]:
        raise ValueError(f"question {ident}'s callback is {target['url']}, not port {shown(request.get('port'), 8)} "
                         f"path {shown(path, 200)}")
    answer_file = Path(record["directory"]) / "approvals" / f"{ident}.answer"
    try:
        answered = answer_file.stat().st_mtime
    except OSError:
        answered = clock()
    if clock() - answered > WAIT_SECONDS + LATE_SECONDS:
        raise ValueError(f"question {ident} was answered more than {WAIT_SECONDS // 60} minutes ago; start the login "
                         "again")
    marker = record_path(record, ident)
    try:
        fd = take_once(marker)
    except FileExistsError:
        raise ValueError(f"question {ident}'s callback was relayed already (one request per question)") from None
    body = request.get("body") or b""
    details = {"question": ident, "port": target["port"], "path": target["path"], "method": request["method"]}
    pinned = read_record(Path(record["directory"]) / "approvals" / f"{ident}{PINNED}")
    pinned = {key: pinned[key] for key in ("inode", "comm") if key in pinned} \
        if isinstance(pinned.get("inode"), int) and not isinstance(pinned.get("inode"), bool) else None
    try:
        try:
            answer = (ask or net().ask)(record["directory"], {
                "op": "oauth-callback", "port": target["port"],
                "address": "localhost" if target["host"] == "localhost" else target["bind"],
                "method": request["method"], "target": request["target"], "host": request.get("host") or
                f"{'[::1]' if target['bind'] == '::1' else '127.0.0.1'}:{target['port']}",
                "headers": {k: v for k, v in (request.get("headers") or {}).items() if k in FORWARDED_HEADERS},
                "body": base64.b64encode(body).decode("ascii"),
                # The socket that listened when the login was allowed: the supervisor hands it to no other.
                **({"listener": pinned} if pinned else {})}, timeout=FORWARD_SECONDS)
            data = base64.b64decode(answer.get("body") or "", validate=True)
        except (OSError, ValueError) as exc:
            details.update(outcome="failed", why=shown(exc, 500))
            raise ValueError(shown(exc, 500)) from None
        status = answer.get("status") if isinstance(answer.get("status"), int) else 502
        took = answer.get("listener") if isinstance(answer.get("listener"), dict) else {}
        details.update(outcome="relayed", status=status,
                       followed=[shown(p, 200) for p in answer.get("followed") or []][:3],
                       **({"listener": {key: shown(took[key], 300) for key in ("pid", "comm", "exe") if key in took}}
                          if took else {}))
        return {"status": status, "content_type": shown(answer.get("content_type") or "", 200), "body": data,
                **({"location": shown(answer["location"], 2000)} if answer.get("location") else {})}
    finally:
        write_record(marker, details, fd)
        log().info("approvals", f"{shown(name)}: question {ident}: login callback {target['url']} "
                                f"{details.get('outcome')}" + (f" ({details['status']})" if "status" in details else "")
                   + (f": {details['why']}" if details.get("why") else ""))


def wire_request(request):
    """REQUEST as JSON-safe (its body in base64), for `approvals callback ID` on another host."""
    return {**request, "body": base64.b64encode(request.get("body") or b"").decode("ascii")}


def wire_answer(answer):
    return {**answer, "body": base64.b64encode(answer.get("body") or b"").decode("ascii")}


def unwire(data, what):
    try:
        found = json.loads(data)
        if not isinstance(found, dict):
            raise ValueError
        found["body"] = base64.b64decode(found.get("body") or "", validate=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(f"{what} is not readable") from None
    return found


def callback_command(ident, source=None, out=None):
    """`sandbox-ai approvals callback ID`: one callback request (JSON, body in base64) on SOURCE (stdin) relayed into
    the run of question ID (relay_once); its answer as JSON on OUT (stdout). How a catcher on another machine (the
    app's, the web's) reaches this host's sandbox, over the host's SSH connection."""
    source = source or sys.stdin.buffer
    data = source.read(REQUEST_LIMIT + 1)
    if len(data) > REQUEST_LIMIT:
        raise ValueError("the callback request is too long")
    request = unwire(data, "the callback request")
    answer = relay_once(ident, request)
    (out or (lambda text: sys.stdout.write(text + "\n")))(json.dumps(wire_answer(answer)))
    return 0


def remote_forward(host, ident, runner=None):
    """FORWARD of a catcher here for question IDENT of another HOST: `sandbox-ai approvals callback ID` there through
    hosts.run (its SSH connection), the request on its stdin."""
    def forward(request):
        status, out, err = (runner or module("hosts").run)(host, ["approvals", "callback", ident],
                                                           input=json.dumps(wire_request(request)),
                                                           timeout=FORWARD_SECONDS + 30)
        if status != 0:
            raise ValueError(f"{shown(host, 80)}: {shown((err or out).strip()[-300:] or f'exit status {status}', 300)}")
        return unwire((out or "").strip().splitlines()[-1] if (out or "").strip() else "", "the host's answer")
    return forward


# --- starting a catcher -----------------------------------------------------------------------------------------------------

def start(ident, url, host=None, popen=None, record_dir=None, wait=None):
    """Start the catcher for question IDENT's URL on this machine, before the browser opens it: (True, note) when it
    listens, (False, why: busy, use the device-code login) when not, (None, "") when URL has no callback. HOST: the
    question is another host's (forwarded over SSH, remote_forward); else this machine's run (relay_once).
    RECORD_DIR: the run's directory here (its record says a busy port too). The catcher is a process of its own
    (it outlives the command that answered), handed the bound socket."""
    target = callback_target(url)
    if target is None or not isinstance(ident, str) or not ID.fullmatch(ident):
        return None, ""
    where = f"port {target['port']} on this computer"
    earlier = ""
    try:
        listener = bind(target)
    except OSError as exc:
        # Busy by a catcher of this user's own? This question's: it listens already (asked twice). An earlier
        # login's (the login started again within its ten minutes): the human's newest "Open" counts, it gives way.
        older = own_catcher(target["port"], target["bind"]) if exc.errno == errno.EADDRINUSE else None
        if older is not None and older["ident"] == ident:
            return True, (f"its login callback ({target['url']}) is relayed into the sandbox once: {where} takes "
                          "that one request already")
        listener = take_over(target, older) if older is not None else None
        if listener is None:
            why = f"{where} is busy ({shown(exc.strerror or exc, 120)})"
            if record_dir is not None:
                write_record(Path(record_dir) / "approvals" / f"{ident}{RECORD}",
                             {"question": ident, "port": target["port"], "path": target["path"], "outcome": BUSY,
                              "why": why})
            log().info("approvals", f"question {ident}: login callback {target['url']} not relayed: {why}")
            return False, f"its login callback ({target['url']}) cannot be relayed: {why}; {DEVICE_HINT}"
        earlier = f" (the catcher of the earlier login on that port, question {older['ident']}, was closed)"
        log().info("approvals", f"question {ident}: login callback {target['url']}: the catcher of question "
                                f"{older['ident']} on {where} gave way")
    try:
        shown_target = {key: value for key, value in target.items() if key != "state"}  # (argv: everyone's to read)
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "--internal-catch", str(listener.fileno()),
                   ident, json.dumps(shown_target), *(["--host", host] if host else []),
                   *(["--wait", str(float(wait))] if wait else [])]
        env = {key: value for key, value in os.environ.items() if key != STATE_VARIABLE}
        if target.get("state"):
            env[STATE_VARIABLE] = target["state"]
        (popen or subprocess.Popen)(command, pass_fds=(listener.fileno(),), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                    close_fds=True, env=env)
    except OSError as exc:
        return False, f"its login callback ({target['url']}) cannot be relayed ({shown(exc, 200)}); {DEVICE_HINT}"
    finally:
        listener.close()
    log().info("approvals", f"question {ident}: login callback {target['url']}: catcher on {where} for "
                            f"{WAIT_SECONDS // 60} minutes" + (f" (forwarded to {shown(host, 80)})" if host else ""))
    # This machine's run: which socket in the sandbox takes the callback is kept now, while the human looks at it.
    took = pin_listener(ident, target, record_dir) if record_dir is not None and not host else ""
    return True, (f"its login callback ({target['url']}) is relayed into the sandbox once: {where} takes that one "
                  f"request for {WAIT_SECONDS // 60} minutes at most, then closes{earlier}{took}")


# The catchers of this user, one record per port and loopback address (PORT.json, PORT-v6.json): {"pid", "ident",
# "port"}, written by the catcher while it listens. What start() asks when a port is busy; a record alone ends
# nothing - the process it names must be a catcher of sandbox-ai for that question (is_catcher).

def catchers_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "callbacks"


def catcher_file(port, address="127.0.0.1"):
    return catchers_dir() / f"{int(port)}{'-v6' if address == '::1' else ''}.json"


def command_words(pid, run=subprocess.run):
    """The command line of process PID as words ([] when it is gone or cannot be asked)."""
    try:
        data = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        return [word.decode("utf-8", "replace") for word in data.split(b"\0") if word]
    except OSError:
        pass
    if sys.platform.startswith("linux"):
        return []
    try:  # (macOS: no /proc)
        done = run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5, check=False,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    return done.stdout.split() if done.returncode == 0 else []


def is_catcher(pid, ident, words=None):
    """Process PID is a catcher of sandbox-ai for question IDENT (its command line: oauth_relay.py --internal-catch
    FD IDENT ...)."""
    words = command_words(pid) if words is None else words
    if "--internal-catch" not in words:
        return False
    at = words.index("--internal-catch")
    return any(word.endswith("oauth_relay.py") for word in words[:at]) and words[at + 2:at + 3] == [ident]


def own_catcher(port, address="127.0.0.1"):
    """{"pid", "ident"} of this user's running catcher on PORT of the loopback ADDRESS, or None."""
    record = read_record(catcher_file(port, address))
    pid, ident = record.get("pid"), record.get("ident")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or pid == os.getpid() or \
            not isinstance(ident, str) or not ID.fullmatch(ident) or not is_catcher(pid, ident):
        return None
    return {"pid": pid, "ident": ident}


def take_over(target, older, seconds=TAKE_SECONDS):
    """The listening socket on TARGET's port once the catcher OLDER (own_catcher) closed, or None when it did not
    within SECONDS (it relays its request right now: that one goes on)."""
    try:
        os.kill(older["pid"], signal.SIGTERM)
    except OSError:
        return None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return bind(target)
        except OSError:
            time.sleep(0.05)
    return None


def register(target, ident):
    """This process is the catcher on TARGET's port now (its record, replaced at once; best effort)."""
    path = catcher_file(target["port"], target["bind"])
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with contextlib.suppress(OSError):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "ident": ident, "port": target["port"]}).encode())
        finally:
            os.close(fd)
        os.replace(temporary, path)


def unregister(target):
    path = catcher_file(target["port"], target["bind"])
    with contextlib.suppress(OSError):
        if read_record(path).get("pid") == os.getpid():
            os.unlink(path)


def catch(fd, ident, target, host=None, wait=WAIT_SECONDS):
    """--internal-catch: the catcher process (start) on the socket FD."""
    listener = socket.socket(fileno=fd)
    send_on = remote_forward(host, ident) if host else (lambda request: relay_once(ident, request))

    def forward(request):
        return send_on({**request, "port": target["port"]})
    catcher = Catcher(listener, target, forward, wait=wait)
    with contextlib.suppress(ValueError, OSError):  # (a newer login's start asks this one to give way: start())
        signal.signal(signal.SIGTERM, lambda *_: catcher.close("a newer login took its port"))
    register(target, ident)
    try:
        outcome = catcher.serve()
    finally:
        unregister(target)
    if outcome["outcome"] in ("timed out", "closed") and not host:
        with contextlib.suppress(OSError, ValueError):
            _, record, _ = find_question(ident)
            write_record(record_path(record, ident), {"question": ident, "port": target["port"],
                                                      "path": target["path"], **outcome})
    log().info("approvals", f"question {ident}: login callback {target['url']}: catcher closed "
                            f"({outcome['outcome']}" + (f", {outcome['refused']} other request(s) refused"
                                                        if outcome.get("refused") else "") + ")")
    return 0


def relay_command(ident, url, host=None, out=print):
    """`sandbox-ai approvals relay ID --url URL [--host HOST]`: the app and the web interface open the page of another
    host's question here (allow --no-open there): the catcher starts here, forwarding to HOST. 0 when it listens."""
    if callback_target(url) is None:
        raise ValueError("the URL has no callback to http://127.0.0.1|localhost|[::1]:PORT/PATH")
    record_dir = None
    if not host or host == "local":
        _, record, question = find_question(ident)
        if question.get("url") != url:
            raise ValueError(f"question {ident} asked to open another URL")
        record_dir, host = record["directory"], None
    ok, note = start(ident, url, host, record_dir=record_dir)
    out(note)
    return 0 if ok else 1


def add_commands(commands):
    """`approvals relay` and `approvals callback` (the app's, the web interface's and another machine's catcher)."""
    relay = commands.add_parser("relay", help="relay a login's callback once (app, web)",
                                description="Start the one-shot catcher for the login callback of browser question "
                                            "ID on this computer, before its page opens here: it takes one request "
                                            "to the callback's port and path and relays it into the sandbox that "
                                            "asked (on another host over its SSH connection).")
    relay.add_argument("id")
    relay.add_argument("--url", required=True, help="the question's URL (as approvals list --json gives it)")
    relay.add_argument("--host", default=None, help="the host whose sandbox asked (default: this computer)")
    callback = commands.add_parser("callback", help="one callback request into question ID's sandbox")
    callback.add_argument("id")


def command(args, out=print):
    if args.command == "relay":
        return relay_command(args.id, args.url, args.host, out=out)
    return callback_command(args.id, out=out)


def main(argv):
    if len(argv) >= 3 and argv[0] == "--internal-catch":
        try:
            host = argv[argv.index("--host") + 1] if "--host" in argv else None
            target = json.loads(argv[3])
            fd = int(argv[1])
            wait = min(float(argv[argv.index("--wait") + 1]), WAIT_SECONDS) if "--wait" in argv else WAIT_SECONDS
        except (ValueError, IndexError, RecursionError):
            return 2
        if not isinstance(target, dict):
            return 2
        return catch(fd, argv[2], target, host, wait)
    print("oauth_relay.py: run by sandbox-ai approvals (allow, relay, callback)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
