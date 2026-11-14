"""Review round 2 (security boundary): hostile inputs against the proxy, the rules and the control surfaces."""
import contextlib
import json
import os
import socket
import ssl
import threading
import time

import pytest

from test_proxy import Proxy, pki, rt  # noqa: F401 - the fixture is used by name


class UpgradingServer:
    """A plain HTTP server that switches protocols on any Upgrade request and then records every raw byte."""

    def __init__(self, always=False):
        self.always = always
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.seen = b""
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        with contextlib.suppress(OSError):
            connection, _ = self.listener.accept()
            with connection:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += connection.recv(65536)
                upgrade = [line.split(b":", 1)[1].strip() for line in data.split(b"\r\n")
                           if line.lower().startswith(b"upgrade:")]
                if upgrade or self.always:
                    upgrade = upgrade or [b"h2c"]
                    connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: "
                                       + upgrade[0] + b"\r\n\r\n")
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                connection.settimeout(3)
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    self.seen += chunk

    def close(self):
        self.listener.close()


def read_head(client):
    answer = b""
    with contextlib.suppress(OSError):
        while b"\r\n\r\n" not in answer:
            chunk = client.recv(65536)
            if not chunk:
                break
            answer += chunk
    return answer


@pytest.mark.parametrize("upgrade,always", [(b"h2c", False), (b"H2C", False), (b"foo/1", False),
                                            (b"websocket, h2c", False), (b"h2c", True)])
def test_an_upgrade_other_than_websocket_does_not_open_a_raw_tunnel_past_the_rules(tmp_path, pki, upgrade, always):
    """After "101 Switching Protocols" the proxy only relays bytes; an h2c (HTTP/2) upgrade let every later request
    on the connection pass the --allow-http rules unchecked. ALWAYS: a server that switches even when not asked."""
    server = UpgradingServer(always)
    rules = [{"host": "api.example", "methods": ["GET"], "path": "/repos/**", "addresses": ["127.0.0.1"]}]
    proxy = Proxy(tmp_path, pki, server.port, {"http_rules": rules})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
            client.sendall(b"GET /repos/x HTTP/1.1\r\nHost: api.example\r\nConnection: Upgrade, HTTP2-Settings\r\n"
                           b"Upgrade: " + upgrade + b"\r\nHTTP2-Settings: AAMAAABkAARAAAAAAAIAAAAA\r\n\r\n")
            answer = read_head(client)
            with contextlib.suppress(OSError):
                client.sendall(b"POST /gists HTTP/1.1\r\nHost: api.example\r\nContent-Length: 0\r\n\r\n")
                client.recv(100)
        time.sleep(0.5)
        assert b"POST /gists" not in server.seen
        assert not answer.startswith(b"HTTP/1.1 101")
    finally:
        proxy.close()
        server.close()


def test_a_websocket_upgrade_still_works(tmp_path, pki):
    server = UpgradingServer()
    proxy = Proxy(tmp_path, pki, server.port)
    try:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
            client.sendall(b"GET /ws HTTP/1.1\r\nHost: api.example\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                           b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
            assert read_head(client).startswith(b"HTTP/1.1 101")
            client.sendall(b"frame")
            time.sleep(0.3)
        time.sleep(0.3)
        assert server.seen == b"frame"
    finally:
        proxy.close()
        server.close()


def control_ask(socket_path, request):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(client.makefile().readline())


def serve_control(tmp_path, **options):
    socket_path, stop = tmp_path / "control.sock", threading.Event()
    network = rt.RuntimeNetwork({"loopback": [3000]}, tmp_path / "allow.json", lambda script: None)
    thread = threading.Thread(target=rt.control_server, args=(socket_path, network, stop), kwargs=options,
                              daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return socket_path, stop, thread


def test_a_kill_switch_request_keeps_the_runs_security_mode_on_the_control_socket(tmp_path):
    """The "cut" branch assigned the request's mode to the same name as the run's security mode: every later request
    went by a string (net allow and mcp attach failed with an AttributeError) or by none (a strict mode's limits
    were gone)."""
    socket_path, stop, thread = serve_control(tmp_path, mode={"name": "strict", "refuses": ["--allow-host"]})
    try:
        assert "widens the security mode strict" in control_ask(socket_path, {"op": "allow",
                                                                             "https": ["192.0.2.7"]})["error"]
        for cut in ("off", None):
            control_ask(socket_path, {"op": "cut", "mode": cut, "by": "self"})
            answer = control_ask(socket_path, {"op": "allow", "https": ["192.0.2.7"]})
            assert not answer["ok"] and "widens the security mode strict" in answer["error"], answer
            assert control_ask(socket_path, {"op": "allow", "tcp": [["192.0.2.9", 22]]})["ok"]
    finally:
        stop.set()
        thread.join(5)


class RecordingServer:
    """Records the raw bytes of every connection; answers nothing."""

    def __init__(self, context=None):
        self.context = context
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.seen = b""
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        with contextlib.suppress(OSError):
            while True:
                connection, _ = self.listener.accept()
                threading.Thread(target=self.take, args=(connection,), daemon=True).start()

    def take(self, connection):
        with contextlib.suppress(OSError):
            if self.context is not None:
                connection = self.context.wrap_socket(connection, server_side=True)
        with connection, contextlib.suppress(OSError):
            connection.settimeout(3)
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                self.seen += chunk

    def close(self):
        self.listener.close()


DENIED_POST = b"/gists HTTP/1.1\r\nHost: api.example\r\nContent-Length: 0\r\n\r\n"


@pytest.mark.parametrize("pieces", [
    [b"post " + DENIED_POST], [b"Post " + DENIED_POST], [b"\nPOST " + DENIED_POST], [b"\r\n\nPOST " + DENIED_POST],
    [b"POST ", DENIED_POST], [b"po", b"st " + DENIED_POST], [b"\n", b"POST " + DENIED_POST],
], ids=["lower", "mixed", "lf", "crlf-lf", "split-after-space", "split-lower", "split-lf"])
@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
def test_http_on_another_port_cannot_hide_from_the_rules(tmp_path, pki, pieces, tls):
    """A request the proxy did not take for HTTP (a method in lower case, a bare LF before it, or its request line cut
    after the method) was relayed as plain TCP (or TLS) on any port but 80 and 443, past every HTTP rule; servers such
    as Go's net/http and nginx read it as the request it is."""
    server = RecordingServer(pki["server"] if tls else None)
    rules = [{"host": "api.example", "methods": ["GET"], "path": "/repos/**", "addresses": ["192.0.2.80"]}]
    proxy = Proxy(tmp_path, pki, server.port, {"http_rules": rules})
    try:
        with contextlib.ExitStack() as stack:
            client = stack.enter_context(socket.create_connection(("127.0.0.1", proxy.port), timeout=5))
            if tls:
                context = ssl.create_default_context(cafile=str(pki["ca"]))
                client = stack.enter_context(context.wrap_socket(client, server_hostname="example.test"))
            for piece in pieces:
                client.sendall(piece)
                time.sleep(0.4)
            with contextlib.suppress(OSError):
                client.recv(100)
        time.sleep(0.3)
        assert b"/gists" not in server.seen
    finally:
        proxy.close()
        server.close()


def test_an_allow_once_for_an_http_question_covers_only_the_request_it_showed(tmp_path):
    """Two requests an ask rule holds, to one endpoint but with another query or body, made while the first is asked:
    each gets its question. The second waited for the first one's and passed by its "allow once": the human allows
    what a question shows, never another request (as for MCP and tool calls)."""
    import types
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=30, poll=0.02)
    proxy = types.SimpleNamespace(asker=broker.ask)
    verdict = {"decision": "ask", "rule": "posts", "ruleset": "rules.json", "reason": "asked"}
    results = {}

    def ask(name, url, body):
        results[name] = rt.ProxyServer.http_ask(proxy, verdict, "POST", url, "api.example", "/posts", body)

    def files(count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = sorted((tmp_path / "a").glob("*.json"))
            if len(found) >= count:
                return [json.loads(path.read_text()) for path in found]
            time.sleep(0.02)
        raise AssertionError(f"not {count} questions")
    first = threading.Thread(target=ask, args=("harmless", "https://api.example/posts?draft=1", {"text": "hello"}),
                             daemon=True)
    first.start()
    harmless = files(1)[0]
    others = []
    for name, url, body in (("body", "https://api.example/posts?draft=1", {"text": "delete my account"}),
                            ("query", "https://api.example/posts?publish=all", {"text": "hello"})):
        others.append(threading.Thread(target=ask, args=(name, url, body), daemon=True))
        others[-1].start()
    time.sleep(0.5)
    (tmp_path / "a" / f"{harmless['id']}.answer").write_text(json.dumps({"decision": "allow", "scope": "once"}))
    first.join(10)
    assert results["harmless"]["decision"] == "allow"
    time.sleep(0.3)
    assert "body" not in results and "query" not in results  # still asked: the first answer was not theirs
    for question in files(3):
        if question["id"] != harmless["id"]:
            (tmp_path / "a" / f"{question['id']}.answer").write_text(json.dumps({"decision": "deny", "scope": "once"}))
    for thread in others:
        thread.join(10)
    assert results["body"]["decision"] == "deny" and results["query"]["decision"] == "deny"


@pytest.mark.parametrize("host,allowed", [(b"example.test", True), (b"EXAMPLE.test.:443", True),
                                          (b"other.example", False), (b"other.example:443", False)])
def test_the_kill_switch_at_api_holds_for_the_host_header_too(tmp_path, pki, host, allowed):
    """With the kill switch at "api" a connection must name one of the model API's hosts in its TLS handshake; the
    Host header of each request on it named any other site behind the same front end (domain fronting) and went
    there."""
    from test_proxy import Handler, serve
    upstream = serve(Handler, pki["server"])
    port = upstream.server_address[1]
    cut = {"modes": ["api"], "api": {"https": ["192.0.2.10"], "names": ["example.test"], "tcp": [], "loopback": []}}
    log = tmp_path / "connections.jsonl"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    server = rt.ProxyServer({"body_limit": 65536, "allow": {"https": ["192.0.2.10/32"], "cut": cut}}, fd,
                            run_dir=pki["run"], destination=lambda sock: ("192.0.2.10", 443),
                            upstream=pki["upstream"],
                            connect=lambda address, timeout=None: socket.create_connection(("127.0.0.1", port), timeout))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server.serve([listener])
    try:
        context = ssl.create_default_context(cafile=str(pki["ca"]))
        with socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=10) as raw, \
                context.wrap_socket(raw, server_hostname="example.test") as tls:
            tls.sendall(b"GET /x HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n")
            answer = tls.recv(100)
        assert answer.startswith(b"HTTP/1.1 200") == allowed, answer
        if not allowed:
            assert answer.startswith(b"HTTP/1.1 403")
    finally:
        listener.close()
        os.close(fd)
        upstream.shutdown()
