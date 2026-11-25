#!/usr/bin/env python3
"""An MCP server over Streamable HTTP on this host's loopback, as a stdio MCP server for sandbox-ai's relay.

What the relay (runtime.py --internal-mcp-serve, the gateway with the MCP rules, the call records and the questions)
runs for each connection of a server whose definition has an "endpoint" (mcp.py): Burp's MCP extension in your own
Burp, sandbox-ai's Burp host mode (burp.py). It runs on this host, as you, never in an agent's sandbox: the agent
reaches the endpoint only through the relay, and nothing but this program connects to it. Each JSON-RPC message on
stdin is POSTed to the endpoint (Accept: application/json and text/event-stream; the session's Mcp-Session-Id and
MCP-Protocol-Version); every message of the answer - a JSON body or an SSE stream - goes to stdout as one line. A
request runs in a thread of its own, so a call Burp holds for your approval does not hold a ping or a cancellation.
A session the server forgot (HTTP 404: Burp's extension ends sessions idle for 30 minutes) is opened again with the
client's own initialize and the request sent once more. The URL must be http on loopback (127.0.0.1, localhost,
[::1]): the extension refuses any other Host header. Server-initiated messages outside an answer (the GET stream)
are not read: the extension sends none. Standard library only.

    python3 -I mcp_http.py URL
"""
import http.client
import json
import re
import sys
import threading
from urllib.parse import urlsplit

VERSION = "1"
MESSAGE_LIMIT = 8 << 20  # bytes of one message from the client
ANSWER_LIMIT = 16 << 20  # bytes of one HTTP answer of the server
TIMEOUT = 3600  # seconds one request may take (a call can wait for your approval in Burp)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
NOT_ANSWERING = -32000


def checked_url(url):
    """(host, port, path) of URL, an http URL on loopback; ValueError otherwise."""
    if not isinstance(url, str) or len(url) > 300 or any(c in url for c in "\r\n\t\0 "):
        raise ValueError("the endpoint must be an http URL such as http://127.0.0.1:9876/mcp")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "http" or parts.hostname not in LOOPBACK or not port or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise ValueError(f"the endpoint {url} is not http://127.0.0.1:PORT/PATH: only this host's loopback, plain "
                         "http, a port, no user, query or fragment (Burp's extension accepts no other Host)")
    path = parts.path or "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,200}", path) or "/../" in path + "/":
        raise ValueError(f"the endpoint's path {path!r} is not a plain path")
    return parts.hostname, port, path


class SessionGone(Exception):
    pass


class NotAnswering(Exception):
    pass


def parse_answer(content_type, body):
    """The JSON-RPC messages of an answer: a JSON body (one message or a batch) or an SSE stream (its data lines)."""
    kind = (content_type or "").split(";")[0].strip().lower()
    text = body.decode("utf-8", "replace")
    if kind == "application/json":
        found = json.loads(text) if text.strip() else []
        return found if isinstance(found, list) else [found]
    if kind == "text/event-stream":
        messages, data = [], []
        for line in text.replace("\r\n", "\n").split("\n"):
            if line == "":
                if data:
                    messages.append(json.loads("\n".join(data)))
                    data = []
            elif line.startswith("data:"):
                data.append(line[6:] if line.startswith("data: ") else line[5:])
        if data:
            messages.append(json.loads("\n".join(data)))
        return messages
    raise ValueError(f"the server answered with {kind or 'no content type'}")


class Bridge:
    def __init__(self, url, out=None, timeout=TIMEOUT):
        self.url = url
        self.host, self.port, self.path = checked_url(url)
        self.out = out or sys.stdout
        self.timeout = timeout
        self.session = None
        self.protocol = None
        self.initialize = None  # the client's own initialize request: sent again for a session the server forgot
        self.write_lock = threading.Lock()
        self.session_lock = threading.Lock()

    def write(self, message):
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self.write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def post(self, message, method="POST"):
        """(messages, session id) of the server's answer to MESSAGE."""
        headers = {"Accept": "application/json, text/event-stream", "User-Agent": f"sandbox-ai-mcp-http/{VERSION}"}
        body = None
        if message is not None:
            body = json.dumps(message, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            try:
                connection.request(method, self.path, body=body, headers=headers)
                answer = connection.getresponse()
            except (ConnectionError, OSError) as exc:
                raise NotAnswering(f"{self.url} does not answer ({exc.__class__.__name__}: {exc})") from None
            data = answer.read(ANSWER_LIMIT + 1)
            if len(data) > ANSWER_LIMIT:
                raise ValueError(f"the server's answer is larger than {ANSWER_LIMIT >> 20} MiB")
            if answer.status == 404 and self.session and method == "POST":
                raise SessionGone()
            if answer.status in (202, 204) or method == "DELETE":
                return [], answer.getheader("Mcp-Session-Id")
            if answer.status != 200:
                raise ValueError(f"the server answered HTTP {answer.status}: "
                                 f"{data[:200].decode('utf-8', 'replace').strip()}")
            return parse_answer(answer.getheader("Content-Type"), data), answer.getheader("Mcp-Session-Id")
        finally:
            connection.close()

    def reopen(self):
        """A new session with the client's own initialize (its answer is not passed on: the client has one)."""
        self.session = None
        messages, session = self.post(self.initialize)
        self.session = session
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def forward(self, message):
        request_id = message.get("id")
        is_request = "method" in message and request_id is not None
        try:
            if message.get("method") == "initialize":
                with self.session_lock:
                    self.initialize = message
                    self.session = None
                    messages, session = self.post(message)
                    self.session = session or self.session
                    for found in messages:
                        result = found.get("result") if isinstance(found, dict) else None
                        if found.get("id") == request_id and isinstance(result, dict) and \
                                isinstance(result.get("protocolVersion"), str):
                            self.protocol = result["protocolVersion"][:40]
            else:
                try:
                    messages, _ = self.post(message)
                except SessionGone:
                    if self.initialize is None:
                        raise
                    with self.session_lock:
                        self.reopen()
                    messages, _ = self.post(message)
        except NotAnswering as exc:
            if is_request:
                self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": NOT_ANSWERING, "message": (
                    f"{exc}: is Burp running with its MCP extension loaded and enabled (its MCP tab)? "
                    "sandbox-ai mcp burp extension shows the jar to load")}})
            return
        except Exception as exc:  # noqa: BLE001 - the request fails, the connection goes on
            if is_request:
                self.write({"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32603, "message": f"the MCP endpoint {self.url}: {exc}"[:2000]}})
            return
        for found in messages:
            if isinstance(found, dict):
                self.write(found)

    def serve(self, stream):
        threads = []
        try:
            while True:
                line = stream.readline(MESSAGE_LIMIT + 1)
                if not line:
                    return 0
                if len(line) > MESSAGE_LIMIT:
                    print("sandbox-ai mcp-http: a message larger than 8 MiB was dropped", file=sys.stderr)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if not isinstance(message, dict):
                    self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                if message.get("method") == "initialize" or "method" not in message or message.get("id") is None:
                    self.forward(message)  # in order: the session first, notifications and answers as they come
                    continue
                thread = threading.Thread(target=self.forward, args=(message,), name="mcp-http", daemon=True)
                thread.start()
                threads = [t for t in threads if t.is_alive()] + [thread]
        finally:
            for thread in threads:
                thread.join(5)
            if self.session:
                try:
                    self.timeout = 5
                    self.post(None, method="DELETE")
                except Exception:  # noqa: BLE001 - the server ends the session itself when idle
                    pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: mcp_http.py URL", file=sys.stderr)
        return 2
    try:
        bridge = Bridge(argv[0])
    except ValueError as exc:
        print(f"sandbox-ai mcp-http: {exc}", file=sys.stderr)
        return 2
    return bridge.serve(sys.stdin)


if __name__ == "__main__":
    sys.exit(main())
