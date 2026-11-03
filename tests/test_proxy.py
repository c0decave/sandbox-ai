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
import threading
import time

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
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})


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
        record = next(r for r in proxy.records() if r.get("push"))
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
