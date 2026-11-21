"""Leaks and performance, review 2 (0.1.99): core and what runs for days - docs/reviews/leaks-perf-2.md. Each test
failed before its fix; the soaks hold what did not grow (connections, threads, descriptors, memory) where it is."""
import argparse
import contextlib
import gc
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import socket
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_leaks2", "sandbox/runtime.py")
llm = load("sandbox_llm_leaks2", "sandbox/llm.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
import model_mock  # noqa: E402

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("NO_COLOR", "1")


def descriptors():
    return len(os.listdir("/proc/self/fd"))


# --- the watch of the agent's connection while the --llm server answers ---------------------------------------------

def watch_threads():
    return [thread for thread in threading.enumerate() if thread.name == "llm-client-watch"]


def test_a_watch_that_is_stopped_ends_at_once_and_lets_go_of_the_agents_connection():
    """LlmClientWatch.stop() only set a flag: its thread stayed in poll() on the agent's socket until the poll's time
    was over (0.5 s) - and a poll holds the socket open in the kernel: the relay's close of the agent's connection
    after the answer went out as a FIN only then. Every answer of the --llm server whose end is the connection's
    (Connection: close, HTTP/1.0, an answer without a length) took up to half a second longer than the server took."""
    ours, theirs = socket.socketpair()
    upstream, other = socket.socketpair()
    try:
        before = descriptors()
        watch = rt.LlmClientWatch(ours, upstream, every=30)
        time.sleep(0.1)  # (in its poll)
        assert len(watch_threads()) == 1
        started = time.monotonic()
        watch.stop()
        ours.close()
        theirs.settimeout(5)
        assert theirs.recv(1) == b""  # the close arrives: nothing holds the socket any more
        assert time.monotonic() - started < 2
        deadline = time.monotonic() + 2
        while watch_threads() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not watch_threads() and not watch.fired
        assert descriptors() == before - 1  # (ours is closed; what woke the watch is closed too)
    finally:
        for sock in (ours, theirs, upstream, other):
            sock.close()


def test_a_watch_still_ends_the_servers_connection_when_the_agent_goes():
    ours, theirs = socket.socketpair()
    upstream, other = socket.socketpair()
    try:
        before = descriptors()
        watch = rt.LlmClientWatch(ours, upstream, every=30)
        theirs.close()
        other.settimeout(5)
        assert other.recv(1) == b"" and watch.fired
        watch.stop()  # (after it fired: nothing to wake, nothing left open)
        watch.stop()
        deadline = time.monotonic() + 2
        while watch_threads() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not watch_threads()
        assert descriptors() == before - 1
    finally:
        for sock in (ours, theirs, upstream, other):
            sock.close()


def test_a_watch_without_a_descriptor_to_wake_it_works_as_before(monkeypatch):
    """Out of descriptors (EMFILE for the pipe): the watch runs without its wake-up, as it did."""
    def no_pipe():
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(rt.os, "pipe", no_pipe)
    ours, theirs = socket.socketpair()
    upstream, other = socket.socketpair()
    try:
        watch = rt.LlmClientWatch(ours, upstream, every=0.05)
        watch.stop()
        deadline = time.monotonic() + 2
        while watch_threads() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not watch_threads() and not watch.fired
        again = rt.LlmClientWatch(ours, upstream, every=0.05)
        theirs.close()
        other.settimeout(5)
        assert other.recv(1) == b"" and again.fired
    finally:
        for sock in (ours, theirs, upstream, other):
            sock.close()


class LlmRelay:
    """The real ProxyServer with an --llm entry, the LLM traffic log (pipe and file) and the spend meter, its
    redirect replaced by the mock server (tests/devtools/model_mock.py)."""

    def __init__(self, tmp_path, port, config=None):
        assert llm.main(["add", "box", "--kind", "llamacpp", "--url", f"http://127.0.0.1:{port}", "--model",
                         "qwen3-8b"], out=lambda _: None) == 0
        a = argparse.Namespace(llm="box", agent="opencode", llm_param=[])
        a.llm_addresses = ["127.0.0.1"]
        run_dir = tmp_path / "run"
        rt.create_ca(run_dir, tmp_path / "ca", bundles=())
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        logs = tmp_path / "logs"
        logs.mkdir()
        spend = {"run": "run-9", "name": "box", "project": "/p", "project_key": "p-1", "agent": "opencode",
                 "prices": {}, "budgets": []}
        config = {"body_limit": 65536, "allow": {"loopback": [port]}, "llm": llm.proxy_entry(a), "spend": spend,
                  **(config or {})}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=run_dir,
                                     destination=lambda sock: ("127.0.0.1", port))
        self.spend_fd = rt.open_spend_log({"spend": "spend-20261117-100000-run-9.jsonl"}, logs)
        self.server.spend = rt.SpendMeter(spend, self.spend_fd, run_dir / rt.SPEND_CONTROL)
        read_fd, self.write_fd = os.pipe()
        self.path = tmp_path / "llm-traffic-20261113-100000-run-7.jsonl"
        self.writer = threading.Thread(target=rt.LlmTrafficFile(read_fd, self.path).serve, daemon=True)
        self.writer.start()
        self.server.traffic = rt.LlmTraffic({"mode": "llm", "run": "run-7"}, self.write_fd, self.server.own_secrets)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def post(self, document, path="/v1/chat/completions", drop=None):
        """The answer to DOCUMENT, read to the connection's end (DROP: only that many bytes of it, then gone)."""
        body = json.dumps(document).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as sock:
            sock.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         b"Authorization: Bearer agent-placeholder-token\r\n"
                         + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
                if drop is not None and len(data) >= drop:
                    break
        return data

    def close(self):
        self.listener.close()
        self.server.traffic.close()
        os.close(self.write_fd)
        self.writer.join(5)
        os.close(self.fd)
        os.close(self.spend_fd)


@pytest.fixture
def relay(tmp_path):
    server, port = model_mock.start(models=("qwen3-8b",), chunks=20, reply="word " * 60)
    found = LlmRelay(tmp_path, port)
    try:
        yield found
    finally:
        found.close()
        server.shutdown()
        server.server_close()


@needs_openssl
def test_an_llm_answer_ends_when_the_server_ended_it_not_half_a_second_later(relay):
    """End to end: a streamed answer of the --llm server read to the end of its connection. 509 ms a request before
    (2.9 ms without the relay), whatever the server took; the relay's own time now (a few milliseconds)."""
    request = {"model": "qwen3-8b", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    assert b"[DONE]" in relay.post(request)
    took = []
    for _ in range(5):
        started = time.monotonic()
        assert b"[DONE]" in relay.post(request)
        took.append(time.monotonic() - started)
    assert min(took) < 0.3, took  # (the fastest of five: a busy machine slows some, the watch slowed every one)


# --- sandbox-ai web serve: an answer in one piece -----------------------------------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch):
    """The real `sandbox-ai web serve` on 127.0.0.1 (a free port) with its own certificate and one login."""
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    import ssl
    w = load("sandbox_webui_leaks2", "sandbox/webui.py")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(w, "SCRYPT", {"n": 1 << 10, "r": 8, "p": 1})
    secret = w.new_totp_secret()
    with w.users_locked() as users:
        users["admin"] = {"password": w.hash_password("correct horse battery", n=1 << 10, r=8, p=1), "totp": secret,
                          "totp_counter": None}
    started, holder = threading.Event(), {}

    def ready(server):
        holder["server"] = server
        started.set()
    thread = threading.Thread(target=w.serve, kwargs={"listen": "127.0.0.1:0", "out": lambda line: None,
                                                      "ready": ready}, daemon=True)
    thread.start()
    assert started.wait(30)
    port = holder["server"].server_address[1]
    context = ssl.create_default_context(cafile=str(w.cert_files()[0]))
    try:
        yield {"w": w, "port": port, "context": context, "secret": secret, "server": holder["server"],
               "origin": f"https://localhost:{port}"}
    finally:
        holder["server"].shutdown()
        thread.join(10)


def web_request(web, method, path, body=None, headers=None, connection=None):
    import http.client
    own = connection is None
    if own:
        connection = http.client.HTTPSConnection("localhost", web["port"], context=web["context"], timeout=30)
    sent = {"Origin": web["origin"], **(headers or {})}
    if body is not None:
        sent["Content-Type"] = "application/json"
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent)
    response = connection.getresponse()
    data = response.read()
    if own:
        connection.close()
    return response.status, dict(response.getheaders()), data


def web_login(web):
    w = web["w"]
    status, headers, body = web_request(web, "POST", "/api/login", {
        "user": "admin", "password": "correct horse battery", "code": w.totp(web["secret"], int(time.time() // 30))})
    assert status == 200, body
    return headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]


def test_the_web_interface_answers_in_one_piece_without_waiting_for_the_browsers_ack(web):
    """An answer's headers and its body went out in two writes with Nagle's algorithm on: the body waited for the
    browser's ACK of the headers, which a browser's system holds back (40 ms on Linux, up to 200 ms on Windows) -
    42 ms for every request of the page, its polls and each click, on this machine itself; under a millisecond now."""
    import http.client
    cookie, _ = web_login(web)
    connection = http.client.HTTPSConnection("localhost", web["port"], context=web["context"], timeout=30)
    took = []
    try:
        for _ in range(12):
            started = time.monotonic()
            status, _, body = web_request(web, "GET", "/api/me", headers={"Cookie": cookie}, connection=connection)
            took.append(time.monotonic() - started)
            assert status == 200 and json.loads(body)["user"] == "admin"
    finally:
        connection.close()
    assert sorted(took)[len(took) // 2] < 0.03, took  # (each one took 0.042 s and more)
    status, _, page = web_request(web, "GET", "/")
    assert status == 200 and page.startswith(b"<!doctype html>") and page.rstrip().endswith(b"</html>")


def test_the_web_interfaces_connections_send_small_pieces_at_once(web):
    """TCP_NODELAY on every connection: a terminal's output frame (a key's echo) never waits behind the one before."""
    seen = []
    handler = web["w"].Handler
    original = handler.setup

    def setup(self):
        original(self)
        seen.append(self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
    handler.setup = setup
    try:
        assert web_request(web, "GET", "/api/me")[0] == 401
    finally:
        handler.setup = original
    assert seen and all(seen)


def test_an_answer_of_the_web_interface_is_written_once(web):
    """send(): the status line, the headers and the body in one write (one TLS record for a small answer)."""
    w = web["w"]
    writes = []

    class Wire:
        def write(self, data):
            writes.append(bytes(data))
            return len(data)

        def flush(self):
            pass
    handler = w.Handler.__new__(w.Handler)
    handler.wfile, handler.request_version, handler.command = Wire(), "HTTP/1.1", "GET"
    handler.requestline, handler.client_address, handler.close_connection = "GET / HTTP/1.1", ("127.0.0.1", 1), False
    handler.send(200, [("Content-Type", "application/json")], b'{"ok": true}')
    assert len(writes) == 1 and writes[0].startswith(b"HTTP/1.1 200 ") and writes[0].endswith(b'\r\n\r\n{"ok": true}')
    assert b"Content-Length: 12\r\n" in writes[0] and b"Content-Security-Policy: " in writes[0]
    del writes[:]
    handler.command = "HEAD"
    handler.send(200, [("Content-Type", "application/json")], b'{"ok": true}')
    assert len(writes) == 1 and writes[0].endswith(b"\r\n\r\n") and b"Content-Length: 12\r\n" in writes[0]
    assert handler.wfile.__class__ is Wire  # (the handler's own stream again)


# --- the relays pass on what they have at once (no Nagle's algorithm between the two ends) -------------------------

def two_piece_server():
    """A server on 127.0.0.1 that reads a request of two lines and answers with two lines, each written by itself a
    millisecond apart (a head, then a body), without delay of its own: what an HTTP server and its client do."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def handle(connection):
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream = connection.makefile("rb")
        with connection:
            with contextlib.suppress(OSError):
                while stream.readline() and stream.readline():
                    connection.sendall(b"HTTP/1.1 200 OK\n")
                    time.sleep(0.001)
                    connection.sendall(b"body\n")

    def accept():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return listener


def two_piece_exchanges(port, count=10):
    """Seconds each of COUNT exchanges with two_piece_server took through 127.0.0.1:PORT, without the first three (a
    new connection's first segments are acknowledged at once, whatever comes later)."""
    took = []
    with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream = client.makefile("rb")
        for _ in range(count):
            started = time.monotonic()
            client.sendall(b"POST / HTTP/1.1\n")
            time.sleep(0.001)
            client.sendall(b'{"a": 1}\n')
            assert stream.readline() == b"HTTP/1.1 200 OK\n" and stream.readline() == b"body\n"
            took.append(time.monotonic() - started)
        stream.close()
    return took[3:]


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_a_relayed_host_port_answers_as_fast_as_the_port_itself():
    """`sandbox-ai net allow --allow-host-port` (and on a Mac every host port and --llm server of a start): the
    sandbox's end (PortRelay) and the host's (net.serve_relay) copied the bytes with Nagle's algorithm on their TCP
    sides - a request's body waited for the server's ACK of its head, the answer's body for the agent's: 83 ms an
    exchange instead of 2.5 ms, and a streamed answer's tokens in bursts 40 ms apart."""
    import tempfile
    net = load("sandbox_net_leaks2", "sandbox/net.py")
    relays = net.launcher().RUNTIME
    directory = Path(tempfile.mkdtemp(prefix="lp2-", dir="/tmp"))  # a short socket path
    host = two_piece_server()
    port, listen = host.getsockname()[1], free_port()
    relay = None
    try:
        threading.Thread(target=net.serve_relay, args=(directory, port, os.getpid(), net.process_state(os.getpid())[1]),
                         kwargs={"idle": 0.2}, daemon=True).start()
        deadline = time.monotonic() + 10
        while not (directory / f"relay-{port}.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        relay = relays.PortRelay(port, directory, listen_port=listen)
        took = two_piece_exchanges(listen)
        assert min(took) < 0.03, took  # (the fastest of seven: each took 0.08 s and more)
    finally:
        if relay is not None:
            relay.close()
        host.close()
        shutil.rmtree(directory, ignore_errors=True)  # (the host's end sees its socket gone and ends)


def test_join_streams_turns_nagles_algorithm_off_on_its_tcp_sides():
    """Both ends, whatever they are: a TCP socket gets TCP_NODELAY, a Unix socket (which has none) stays as it is."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    ours, theirs = socket.socketpair()
    seen = {}
    original = rt.relay

    def relay(first, second, counts, **options):
        seen["tcp"] = first.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        seen["sockets"] = (first, second)
    rt.relay = relay
    try:
        assert not accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        rt.join_streams(accepted, ours)
        assert seen["tcp"] and seen["sockets"] == (accepted, ours)
        assert accepted.fileno() == -1 and ours.fileno() == -1  # closed, as before
    finally:
        rt.relay = original
        for sock in (listener, client, accepted, ours, theirs):
            sock.close()


def test_a_lent_tunnels_connections_pass_on_what_they_have_at_once():
    """vpn_runtime.splice (a team member or an MCP server on another start's VPN: the borrower's end and the
    lender's): the same two ends, the same wait - 83 ms an exchange through the borrower's side alone."""
    vpn = load("sandbox_vpn_runtime_leaks2", "sandbox/vpn_runtime.py")
    host = two_piece_server()
    front = socket.socket()
    front.bind(("127.0.0.1", 0))
    front.listen()

    def borrower():
        with contextlib.suppress(OSError):
            connection, _ = front.accept()
            lent, lender = socket.socketpair()  # (the lender's Unix socket)
            out = socket.create_connection(host.getsockname(), timeout=10)
            out.settimeout(None)
            threading.Thread(target=vpn.splice, args=(lender, out), daemon=True).start()
            vpn.splice(connection, lent)
    threading.Thread(target=borrower, daemon=True).start()
    try:
        took = two_piece_exchanges(front.getsockname()[1])
        assert min(took) < 0.03, took
    finally:
        host.close()
        front.close()


# --- the launcher log's states: read once per process, not once per start -------------------------------------------

def fresh_launchlog(name):
    """sandbox/launchlog.py as a process of its own would have it (its memory is the module's)."""
    return load(name, "sandbox/launchlog.py")


def test_a_listing_parses_the_logged_states_once_not_once_per_start(tmp_path, monkeypatch):
    """`sandbox-ai list` (and the shell completion of a start's name) asks each start's status, and each said its
    state through changed(): the whole launcher.states.json read and parsed again for every start - with 1000 starts
    a megabyte of JSON parsed 1000 times, half of `list`'s 1.1 s. Parsed again only when the file changed."""
    log = fresh_launchlog("launchlog_leaks2_a")
    for index in range(300):
        assert log.changed(("session", f"s{index}"), "stopped", "session", f"s{index}: stopped") is True
    parsed, real = [], log.json.loads

    def loads(text, *args, **kwargs):
        if len(text) > 2000:
            parsed.append(len(text))
        return real(text, *args, **kwargs)
    monkeypatch.setattr(log.json, "loads", loads)
    lines = log.path().read_text().count("\n")
    for index in range(300):  # the next poll, in this process (the app, the web interface)
        assert log.changed(("session", f"s{index}"), "stopped", "session", f"s{index}: stopped") is False
    assert parsed == [] and log.path().read_text().count("\n") == lines
    other = fresh_launchlog("launchlog_leaks2_b")  # the next poll, a process of its own: one parse for all of them
    for index in range(300):
        assert other.changed(("session", f"s{index}"), "stopped", "session", f"s{index}: stopped") is False
    assert len(parsed) == 1 and log.path().read_text().count("\n") == lines
    assert other.state(("session", "s7")) == "stopped" and other.state(("session", "none"), "?") == "?"
    assert len(parsed) == 1


def test_a_state_another_process_changed_is_seen_at_once(tmp_path):
    """What another process wrote meanwhile counts, as before: its change is not said again here, and a change here
    is built on its file (none of its states is lost)."""
    one, two = fresh_launchlog("launchlog_leaks2_c"), fresh_launchlog("launchlog_leaks2_d")
    assert one.changed(("session", "a"), "running", "session", "a: running") is True
    assert two.changed(("session", "a"), "running", "session", "a: running") is False
    assert two.changed(("session", "a"), "stopped", "session", "a: stopped") is True
    assert two.changed(("session", "b"), "running", "session", "b: running") is True
    assert one.state(("session", "a")) == "stopped"  # (its own memory says running: the file counts)
    assert one.changed(("session", "a"), "stopped", "session", "a: stopped") is False
    assert one.changed(("session", "c"), "paused", "session", "c: paused") is True
    kept = json.loads(one.states_path().read_text())
    assert {key: entry["v"] for key, entry in kept.items()} == {
        '["session", "a"]': "stopped", '["session", "b"]': "running", '["session", "c"]': "paused"}
    # A file that went (forget_states, a cleaned state directory) or was damaged: read as empty, as before.
    one.states_path().write_text("{ not json")
    assert two.changed(("session", "b"), "running", "session", "b: running") is True
    one.states_path().unlink()
    assert one.changed(("session", "c"), "paused", "session", "c: paused") is True
    assert list(json.loads(one.states_path().read_text())) == ['["session", "c"]']


def test_what_is_handed_out_of_the_states_is_a_copy(tmp_path):
    log = fresh_launchlog("launchlog_leaks2_e")
    for index in range(5):
        log.changed(("k", index), index, "area", None)
    with log._states_file() as known:
        known["scratch"] = {"v": 1, "t": 0}
        del known["scratch"]  # (unchanged in the end: nothing is written)
    with log._states_file() as known:
        assert "scratch" not in known and len(known) == 5


def test_more_live_states_than_are_kept_are_not_said_again_at_every_poll(tmp_path, monkeypatch):
    """Past STATES_KEPT (1000) the oldest state was dropped for each new one - and with more starts, hosts and images
    than that the oldest was one of them: said again at the next poll, which dropped the next one, and so on. With
    1010 kept starts every `sandbox-ai list` wrote 1010 lines into launcher.log (its history gone within a minute
    of the app's polls), rewrote the 68 KB file 1010 times and took 2.4 s instead of 0.3 s. A state is dropped for
    its age now (STATES_AGE since it was said), the oldest first; never more than STATES_MOST are kept."""
    log = fresh_launchlog("launchlog_leaks2_f")

    def poll(module, count=25):
        monkeypatch.setattr(module, "STATES_KEPT", 20)
        return [index for index in range(count)
                if module.changed(("session", f"s{index}"), "stopped", "session", f"s{index}: stopped")]
    assert poll(log) == list(range(25))  # each said once
    lines, written = log.path().read_text().count("\n"), log.states_path().stat().st_mtime_ns
    for name in ("launchlog_leaks2_g", "launchlog_leaks2_h"):  # the next polls, a process each
        assert poll(fresh_launchlog(name)) == []
    assert log.path().read_text().count("\n") == lines and log.states_path().stat().st_mtime_ns == written
    assert len(json.loads(log.states_path().read_text())) == 25


def test_old_states_go_first_and_the_file_never_holds_more_than_the_most(tmp_path, monkeypatch):
    log = fresh_launchlog("launchlog_leaks2_i")
    monkeypatch.setattr(log, "STATES_KEPT", 5)
    monkeypatch.setattr(log, "STATES_MOST", 12)
    now = [1_800_000_000.0]
    monkeypatch.setattr(log.time, "time", lambda: now[0])
    for index in range(8):  # said long ago (starts deleted since, hosts removed)
        log.changed(("old", index), "x", "area", None)
    assert len(json.loads(log.states_path().read_text())) == 8  # (within their age: all kept)
    now[0] += log.STATES_AGE + 60
    log.changed(("new", 0), "x", "area", None)
    kept = json.loads(log.states_path().read_text())
    assert len(kept) == 5 and '["new", 0]' in kept and '["old", 7]' in kept and '["old", 3]' not in kept
    for index in range(1, 30):  # more new ones than the file may ever hold: the oldest go, whatever their age
        now[0] += 1
        log.changed(("new", index), "x", "area", None)
    kept = json.loads(log.states_path().read_text())
    assert len(kept) == 12 and '["new", 29]' in kept and '["new", 18]' in kept and '["new", 17]' not in kept
    assert not any(key.startswith('["old"') for key in kept)


# --- the launcher's parser, used again for the options of many starts -----------------------------------------------

def test_options_parsed_with_a_parser_used_again_read_as_with_a_parser_of_their_own(tmp_path, monkeypatch):
    """parse_args(argv, parser): what parse_args adds to an option's list (a preset's hosts, a login's host) is that
    call's own - argparse hands out the default list itself, which a parser used again would keep filling."""
    net = load("sandbox_net_leaks2_parser", "sandbox/net.py")
    l = net.launcher()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
    parser = l.build_parser("claude")
    cases = [["claude"], ["claude", "--token-proxy"], ["claude", "--preset", "github", "--allow-host", "a.example.org"],
             ["claude", "--profile", "frontier"], ["claude", "--workspace", f"{tmp_path},{tmp_path}/x", "--", "-p", "hi"],
             ["claude"], ["claude", "--allow-host-port", "8080"], ["claude"]]
    for argv in cases:
        try:
            own = vars(l.parse_args(list(argv)))
        except SystemExit:
            with pytest.raises(SystemExit):
                l.parse_args(list(argv), parser)
            continue
        again = vars(l.parse_args(list(argv), parser))
        assert again == own, argv
    first = l.parse_args(["claude"], parser)
    first.presets.append("made-up")
    first.allow_host.append("made-up.example.org")
    second = l.parse_args(["claude"], parser)
    assert "made-up" not in second.presets and "made-up.example.org" not in second.allow_host
    assert all(action.default == [] for action in parser._actions if isinstance(action.default, list)), \
        "the parser's own defaults stay as they were built"


# --- redaction: a long word after a quote is looked at once ----------------------------------------------------------

# SECRET_TEXT[0] as it was (without its lookahead): every match the same.
KEYS_BEFORE = re.compile(r'("[A-Za-z0-9_-]*(?:token|secret|passw|api_?key|apikey|authorization|credential'
                         r'|session|cookie|code_verifier|private|signature)[A-Za-z0-9_-]*"\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
KEY_PIECES = ['"', '"', '":', '": ', '" : "', ":", " ", "\n", "\\", '\\"', "token", "TOKEN", "secret", "passw",
              "password", "api_key", "apikey", "api-key", "authorization", "credential", "session", "cookie",
              "code_verifier", "private", "signature", "x", "_", "-", "9", "ab", "ſecret", "toKen", "İ",
              "ı", "é", "access_", "_id", "v", "value", "{", "}", ",", "a" * 40, "\t", " ", "'", "="]


def test_the_key_pattern_with_its_lookahead_matches_what_it_matched():
    """The names of JSON keys: a quote, then word characters up to the closing quote and a colon. The lookahead says
    just that before the names are tried, so every match is the same; without it a long run of word characters after
    a quote (base64url, hex, an identifier: no key at all) was walked once for each name it could end with."""
    import random
    assert '"(?=[A-Za-z0-9_-]*"\\s*:)' in rt.SECRET_TEXT[0][0].pattern
    assert rt.SECRET_TEXT[0][0].pattern.replace('(?=[A-Za-z0-9_-]*"\\s*:)', "") == KEYS_BEFORE.pattern
    assert rt.SECRET_TEXT[0][0].flags == KEYS_BEFORE.flags
    chosen = random.Random(4)
    for _ in range(40000):
        text = "".join(chosen.choice(KEY_PIECES) for _ in range(chosen.randint(1, 12)))
        assert rt.SECRET_TEXT[0][0].sub(r'\1"[redacted]"', text) == KEYS_BEFORE.sub(r'\1"[redacted]"', text), text
    for character in [chr(code) for code in range(0x3000)] + ["K", "ẞ", "ﬀ"]:
        for text in (f'"{character}token": "v"', f'"x{character}secret" : "v"', f'"api{character}key":"v"',
                     f'"se{character}sion": "v"', f'"token{character}": "v"', f'"token"{character}: "v"',
                     f'"to{character}en": "v"'):
            assert rt.SECRET_TEXT[0][0].sub(r'\1"[redacted]"', text) == KEYS_BEFORE.sub(r'\1"[redacted]"', text), text
    assert rt.redact_text('{"access_token": "abc", "note": "hello"}') == '{"access_token": "[redacted]", "note": "hello"}'


def test_a_long_word_after_a_quote_is_redacted_in_a_fraction_of_the_time():
    """A model request with what no white space breaks - Codex's encrypted reasoning (base64url), a hash, a minified
    identifier: 1.3 ms for 8 KB of it (0.01 ms for 8 KB of prose), under the proxy's log lock and holding the GIL."""
    blob = "gAAAAAB" + "Zm9vYmFyYmF6cXV4" * 4000  # 64 KB
    body = json.dumps({"model": "m", "input": [{"type": "reasoning", "encrypted_content": blob},
                                               {"type": "message", "content": "x" * 30000}], "api_key": "k" * 12})
    assert rt.redact_text(body) == body.replace('"api_key": "kkkkkkkkkkkk"', '"api_key": "[redacted]"')
    assert rt.SECRET_TEXT[0][0].sub(r'\1"[redacted]"', body) == KEYS_BEFORE.sub(r'\1"[redacted]"', body)

    def best(pattern):
        times = []
        for _ in range(5):
            started = time.perf_counter()
            pattern.sub(r'\1"[redacted]"', body)
            times.append(time.perf_counter() - started)
        return min(times)
    assert best(rt.SECRET_TEXT[0][0]) * 4 < best(KEYS_BEFORE)  # (about 0.03x here)


# --- the launcher's activity timeline: the destinations it counts stay bounded ----------------------------------------

def connection_records(log, records):
    with open(log, "a") as out:
        for record in records:
            out.write(json.dumps({"time": "2026-11-18T10:00:00Z", **record}) + "\n")


def refused(number):
    return {"connection": number, "event": "denied", "reason": "not allowed by the network options",
            "destination": f"10.{number >> 16 & 255}.{number >> 8 & 255}.{number & 255}:{1024 + number % 60000}"}


def test_the_timeline_counts_repeats_for_a_bounded_number_of_destinations(tmp_path):
    """The collector (in the launcher, for as long as the run: days) kept one entry for every destination the
    connection log ever named, and looked at each of them at every poll (2 s): an agent that tries address after
    address (a scan, a crawler) made the launcher's memory and each poll grow for the rest of the run - 20000
    destinations: 9 MB of entries, 22 ms a poll instead of 2. The longest quiet ones are forgotten beyond
    REPEAT_TARGETS."""
    activity = load("sandbox_activity_leaks2", "sandbox/activity.py")
    name = "connections-20261118-100000-run-1.jsonl"
    source = activity.Connections(tmp_path, name, from_start=True)
    limit = activity.REPEAT_TARGETS
    assert 1000 <= limit <= 10000
    connection_records(tmp_path / name, [refused(number) for number in range(limit * 3)])
    events = source.poll([64 << 20], now=1000.0)
    assert len(events) == limit * 3 and all(event["summary"].startswith("denied: ") for event in events)  # each, once
    assert len(source.repeats) <= limit
    for step in range(5):  # and it stays so, poll after poll
        connection_records(tmp_path / name, [refused(limit * 3 + step * 700 + number) for number in range(700)])
        assert len(source.poll([64 << 20], now=1010.0 + step)) == 700
        assert len(source.repeats) <= limit


def test_what_was_counted_for_a_forgotten_destination_is_said_first_and_it_is_new_when_it_comes_back(tmp_path):
    activity = load("sandbox_activity_leaks2_b", "sandbox/activity.py")
    name = "connections-20261118-100000-run-1.jsonl"
    source = activity.Connections(tmp_path, name, from_start=True)
    limit = activity.REPEAT_TARGETS
    busy = {"connection": 1, "event": "http", "method": "GET", "url": "https://example.org/a", "host": "example.org",
            "status": 200}
    connection_records(tmp_path / name, [busy] * 5)
    first = source.poll([64 << 20], now=1000.0)
    assert [event["summary"] for event in first] == ["allowed: GET https://example.org/a → 200"]
    assert source.repeats[("allowed", "", "example.org")]["count"] == 4
    connection_records(tmp_path / name, [refused(number) for number in range(limit + 10)])
    events = source.poll([64 << 20], now=1001.0)  # (within the minute: its four repeats were not said yet)
    said = [event for event in events if event["details"].get("repeats")]
    assert [(event["summary"], event["details"]["count"]) for event in said] == [
        ("allowed: 4 more request(s) to example.org (GET 4)", 4)]
    assert ("allowed", "", "example.org") not in source.repeats and len(source.repeats) == limit
    connection_records(tmp_path / name, [busy, busy])
    again = source.poll([64 << 20], now=1002.0)
    assert [event["summary"] for event in again] == ["allowed: GET https://example.org/a → 200"]
    assert source.repeats[("allowed", "", "example.org")]["count"] == 1
    # Below the limit nothing changes: counted, said once a minute and at the end.
    assert source.poll([64 << 20], now=1003.0) == []
    late = source.poll([64 << 20], now=1002.0 + activity.REPEAT_WINDOW)
    assert [event["summary"] for event in late] == ["allowed: 1 more request(s) to example.org (GET 1)"]


# --- sandbox-ai llm traffic: a text without control characters is not walked character by character ------------------

def safe_before(value, newlines=False):
    """llm_traffic.safe as it was."""
    return "".join(c if c.isprintable() or (newlines and c in "\n\t") else
                   f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}" for c in str(value))


def test_traffic_text_for_the_terminal_is_escaped_as_before_and_at_once_when_there_is_nothing_to_escape():
    """`sandbox-ai llm traffic` shows megabytes of what the model and the agent wrote; each character of it was
    looked at in Python: two thirds of the command's time (a 57 MB log: 3.3 s). A text with nothing to escape -
    nearly all of them - is known as such in one look."""
    import random
    traffic = load("sandbox_llm_traffic_leaks2", "sandbox/llm_traffic.py")
    chosen = random.Random(6)
    pieces = ["word ", "é", "日本", "\n", "\t", "\x1b[31m", "\x00", "\r", "\x7f", "‮", " ", "\ud800", "x" * 20,
              " ", "{\"a\": 1}", " ", "﻿", "\U0001f600", "\x85"]
    for _ in range(4000):
        text = "".join(chosen.choice(pieces) for _ in range(chosen.randint(0, 12)))
        for newlines in (False, True):
            assert traffic.safe(text, newlines) == safe_before(text, newlines), repr(text)
    assert traffic.safe(12) == "12" and traffic.safe(None) == "None" and traffic.safe("") == ""
    assert traffic.safe("a\nb") == "a\\x0ab" and traffic.safe("a\nb\tc", newlines=True) == "a\nb\tc"
    text = ("The quick brown fox jumps over the lazy dog.\n\tdef f(x): return x * 2  # Größe, 日本語\n" * 12000)

    def best(function):
        times = []
        for _ in range(3):
            started = time.perf_counter()
            function(text, True)
            times.append(time.perf_counter() - started)
        return min(times)
    assert traffic.safe(text, True) == text
    assert best(traffic.safe) * 5 < best(safe_before)  # (about 0.01x here)


# --- soaks: what the long-running processes hold after many connections is what they held before ---------------------
#
# The review's own soaks ran 10-20 thousand requests each (docs/reviews/leaks-perf-2.md has their numbers: resident
# memory, descriptors and threads flat). These are their short forms, kept as regression tests: a few hundred
# requests, connections that drop at every stage - afterwards no thread, no descriptor, no listed connection and no
# memory of the proxy's own is left. A leak per connection shows as a count that is not what it was.

# What may differ between the two counts of a soak: the test process is not the soak's alone (an earlier test's
# server thread ends late, a log's compression opens a file). A leak per connection would be the number of
# connections - 45 to 120 here.
SLACK = 3


def traced_growth(action, rounds):
    """Bytes of Python memory (tracemalloc) that ROUNDS more calls of ACTION left allocated, after as many calls to
    warm up (caches filled, pools grown, each thread's buffer for its next read allocated while it is traced: the
    traffic log's reader holds a megabyte for it); the garbage collected first."""
    import tracemalloc
    tracemalloc.start()
    try:
        for _ in range(rounds):
            action()
        time.sleep(0.3)
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(rounds):
            action()
        time.sleep(0.3)  # (the connections' threads end)
        gc.collect()
        return tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()


def wait_until(condition, wait=15.0):
    deadline = time.monotonic() + wait
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


@needs_openssl
def test_soak_the_proxy_holds_nothing_after_requests_and_connections_that_drop(tmp_path):
    """HTTPS through the real proxy: requests on new connections, on a kept one, and connections that go away at
    every stage (a part of a hello, after the handshake, in the middle of a request's body, before the answer)."""
    import ssl
    import struct
    proxy_tests = pytest.importorskip("test_proxy")
    prt = proxy_tests.rt
    session, upstream_dir = tmp_path / "session-run", tmp_path / "upstream-run"
    prt.create_ca(session, tmp_path / "session-ca", bundles=())
    prt.create_ca(upstream_dir, tmp_path / "upstream-ca", bundles=())
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(prt.server_certificate(upstream_dir, "example.test")), str(upstream_dir / "leaf.key"))
    pki = {"run": session, "ca": tmp_path / "session-ca" / "ca.pem", "server": server,
           "upstream": ssl.create_default_context(cafile=str(upstream_dir / "ca.pem"))}
    upstream = proxy_tests.serve(proxy_tests.Handler, server)
    upstream.handle_error = lambda request, address: None  # (a client that went away: no traceback of the stand-in)
    proxy = proxy_tests.Proxy(tmp_path, pki, upstream.server_address[1])
    context = ssl.create_default_context(cafile=str(pki["ca"]))
    get = b"GET /v1/items?q=x HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n"
    body = json.dumps({"messages": [{"role": "user", "content": "The quick brown fox. " * 400}]}).encode()
    post = b"POST /p HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    step = [0]

    def one():
        step[0] += 1
        kind = step[0] % 6
        if kind == 0:
            assert proxy_tests.https_request(proxy.port, pki["ca"], get).startswith(b"HTTP/1.1 200")
            return
        if kind == 1:
            assert proxy_tests.https_request(proxy.port, pki["ca"], post).startswith(b"HTTP/1.1 201")
            return
        raw = socket.create_connection(("127.0.0.1", proxy.port))
        if kind == 2:  # a part of a ClientHello, then gone
            raw.sendall(b"\x16\x03\x01\x02\x00\x01")
            raw.close()
            return
        tls = context.wrap_socket(raw, server_hostname="example.test")
        if kind == 4:  # a part of a request's body, then a reset
            tls.sendall(b"POST /p HTTP/1.1\r\nHost: example.test\r\nContent-Length: 100000\r\n\r\nabc")
        elif kind == 5:  # the request sent, gone before the answer
            tls.sendall(get)
        tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        tls.close()
    try:
        one()
        assert wait_until(lambda: not proxy.server.live)
        threads, open_files = threading.active_count(), descriptors()
        grown = traced_growth(one, 120)
        assert wait_until(lambda: not proxy.server.live and threading.active_count() <= threads + SLACK), \
            (proxy.server.live, [thread.name for thread in threading.enumerate()])
        assert wait_until(lambda: descriptors() <= open_files + SLACK), (descriptors(), open_files)
        assert len(proxy.server.contexts) == 1 and len(proxy.server.__dict__.get("logged_bodies", {})) <= prt.LOG_BODY_SEEN
        assert grown < 200_000, grown  # (10 KB here; 120 connections that each kept their 8 KB body: 320 KB)
    finally:
        proxy.close()
        upstream.shutdown()
        upstream.server_close()


@needs_openssl
def test_soak_the_llm_relay_holds_nothing_after_streams_and_agents_that_go_away(relay):
    """The --llm path with its watch of the agent's connection, the spend meter and the traffic log: streamed
    answers read whole, answers given up after their first bytes, a model the start does not offer."""
    step = [0]
    tools = [{"type": "function", "function": {"name": f"tool_{i}", "description": "does a thing",
                                               "parameters": {"type": "object", "properties": {}}}} for i in range(8)]

    def one():
        step[0] += 1
        kind = step[0] % 4
        document = {"model": "qwen3-8b", "stream": kind != 1, "tools": tools, "messages": [
            {"role": "system", "content": "You are helpful. " * 40},
            {"role": "user", "content": f"question {step[0]} " + "context " * 100}]}
        if kind == 3:
            relay.post(document, drop=200)  # the agent goes after the answer's first bytes
        elif kind == 2:
            relay.post({**document, "model": f"other-{step[0]}"})
        else:
            assert b" 200 " in relay.post(document)[:20]
    one()
    assert wait_until(lambda: not relay.server.live and not watch_threads())
    # (the traffic log's writer opens its file at its first record and keeps it: counted before, not as a leak)
    assert wait_until(lambda: relay.path.exists() and not relay.server.traffic.queue)
    threads, open_files = threading.active_count(), descriptors()
    grown = traced_growth(one, 80)
    assert wait_until(lambda: not relay.server.live and not watch_threads() and threading.active_count() <= threads + SLACK), \
        (relay.server.live, [thread.name for thread in threading.enumerate()])
    assert wait_until(lambda: descriptors() <= open_files + SLACK), (descriptors(), open_files)
    assert wait_until(lambda: not relay.server.traffic.queue)
    assert grown < 200_000, grown
    assert relay.server.spend.requests >= 80  # (each answered request was counted)


def test_soak_the_mcp_relay_holds_nothing_after_servers_that_end_and_agents_that_go_away():
    """mcp_serve with a server per connection: calls answered, a server that crashes on a call, an agent that goes
    away while the server still works - no thread, no descriptor and no process of a server is left."""
    import io
    import subprocess
    import tempfile
    server = (r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "tools/call" and message["params"]["name"] == "crash":
        sys.exit(3)
    if "id" in message:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
""")
    directory = Path(tempfile.mkdtemp(prefix="lp2m-", dir="/tmp"))  # a short socket path
    path, out = directory / "s.sock", io.StringIO()
    threading.Thread(target=rt.mcp_serve, args=(str(path), [sys.executable, "-c", server]),
                     kwargs={"cwd": "/", "out": out}, daemon=True).start()
    assert wait_until(path.exists)

    def children():
        listed = subprocess.run(["ps", "--ppid", str(os.getpid()), "-o", "pid=,comm="], capture_output=True,
                                text=True).stdout.splitlines()
        return [line for line in listed if not line.split()[-1].startswith("ps")]

    def one(kind):
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(path))
        client.settimeout(20)
        stream = client.makefile("rb")
        try:
            client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n')
            assert stream.readline()
            if kind == 2:
                return  # the agent goes with its server idle
            client.sendall(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "crash" if kind == 1 else "echo", "arguments": {}}}).encode() + b"\n")
            answer = json.loads(stream.readline())
            assert ("error" in answer) == (kind == 1), answer
        finally:
            stream.close()
            client.close()
    try:
        one(0)
        assert wait_until(lambda: not children())
        threads, open_files = threading.active_count(), descriptors()
        for index in range(45):
            one(index % 3)
        assert wait_until(lambda: not children() and threading.active_count() <= threads + SLACK, 30), \
            (children(), [thread.name for thread in threading.enumerate()])
        assert wait_until(lambda: descriptors() <= open_files + SLACK), (descriptors(), open_files)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_soak_the_web_interface_holds_nothing_after_requests_and_connections_that_drop(web):
    """`sandbox-ai web serve`: pages and API requests with and without a login, refused logins, and connections
    that go away before, in and after the TLS handshake and in the middle of a request."""
    import struct
    cookie, _ = web_login(web)
    step = [0]

    def one():
        step[0] += 1
        kind = step[0] % 7
        if kind == 0:
            assert web_request(web, "GET", "/")[0] == 200
        elif kind == 1:
            assert web_request(web, "GET", "/api/me", headers={"Cookie": cookie})[0] == 200
        elif kind == 2:
            assert web_request(web, "GET", "/api/me")[0] == 401
        elif kind == 3:
            assert web_request(web, "POST", "/api/login", {"user": f"nobody{step[0]}", "password": "x",
                                                            "code": "000000"})[0] in (401, 429)
        else:
            raw = socket.create_connection(("127.0.0.1", web["port"]), timeout=10)
            if kind == 4:
                raw.sendall(b"\x16\x03\x01\x02\x00\x01")  # a part of a hello
                raw.close()
                return
            tls = web["context"].wrap_socket(raw, server_hostname="localhost")
            if kind == 5:
                tls.sendall(b"POST /api/login HTTP/1.1\r\nHost: localhost\r\nContent-Length: 500\r\n\r\n{")
            tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            tls.close()
    one()
    assert wait_until(lambda: web["server"].active == 0)
    threads, open_files = threading.active_count(), descriptors()
    grown = traced_growth(one, 105)
    assert wait_until(lambda: web["server"].active == 0 and threading.active_count() <= threads + SLACK), \
        (web["server"].active, [thread.name for thread in threading.enumerate()])
    assert wait_until(lambda: descriptors() <= open_files + SLACK), (descriptors(), open_files)
    assert web["server"].by_address == {} and len(web["w"].Handler.service.sessions.items) == 1
    assert grown < 200_000, grown
