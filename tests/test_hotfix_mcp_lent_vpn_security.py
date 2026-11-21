"""Hotfix 0.1.96, the security review of the lent tunnel (vpn_runtime.Lender / Borrower, vpn.py, mcp.py).

The threat model: the agent and every MCP server are untrusted; nothing may leave outside the tunnel; the agent gains
no network beyond its allow-list; one misbehaving server must not take the others' network away. What the review
found and what holds now, each tried here with real sockets or the real functions:

  - a lender whose connections are all in use is THERE: a borrower that is refused ("too many connections") is not a
    borrower whose lender is gone - before, one server that held the lender's connections for 15 s ended every
    other server's VPN container of the run ("down");
  - one borrower takes a part of the lender's connections at most (its relay has slots of its own), its relays
    never start a thread per packet without a bound, and neither the lender's nor a borrower's accept loop ends on an
    error that passes (no descriptor, no thread);
  - what a lender connects to is told by networks named here, not only by Python's tables (multicast was "global");
  - a borrower's look at its lender checks the profile each time (a volume of the run's name used again, macOS);
  - a far end that never closes keeps no connection (and no slot) once the server's program has closed its side;
  - a DNS answer of 4 kB crosses the socket (the answer's line was held to the request's limit);
  - a lookup whose resolver sends other answers without end times out;
  - the folder of the lent tunnel is made anew and is never a link; `--vpn-lent-by` is in no preset, no project
    file, no server's options and no abbreviation;
  - the lender's socket protocol: garbage, unicode digits, mapped IPv6, lists, numbers, too long, too deep - all
    refused, and every slot comes back."""
import base64
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("mcp_for_lent_vpn_security", "sandbox/mcp.py")
v = m.launcher().load_vpn_module()
rt = v.runtime()
TOKEN = "5a" * 32  # a run's token (tests/test_hotfix_mcp_lent_vpn_cut.py: what it is for, and who is denied)


@pytest.fixture(autouse=True)
def run_token(monkeypatch):
    """This process is the lender's and the borrowers' at once: the run's token is known to both."""
    monkeypatch.setattr(rt, "_lend_token", TOKEN)


@pytest.fixture
def folder():
    short = Path(tempfile.mkdtemp(prefix="lend-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def wireguard(name="wg"):
    v.save_profile({"version": 1, "name": name, "type": "wireguard", "provider": None,
                    "wireguard": {"addresses": ["10.66.1.2/32"], "private_key_secret": f"VPN_{name.upper()}_KEY",
                                  "preshared_key_secrets": {}, "peers": []}})
    return v.load_profile(name)


def waited(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def free_slots(lender):
    return lender.slots._value  # (a BoundedSemaphore's count: what the tests look at, nothing else does)


def held_lender(folder, limit=1, profile="wg"):
    """(lender, the far ends) of a Lender whose "public" connections are socket pairs the test holds open."""
    far = []

    def connect(target):
        near, other = socket.socketpair()
        far.append(other)
        return near
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, profile, path=folder / "lend.sock", connect=connect,
                       local=lambda address: False, limit=limit).start()
    return lender, far


# --- one server cannot take the others' lender away ---------------------------------------------------------------

def test_a_lender_whose_connections_are_all_in_use_is_there_and_a_refusal_says_so(folder, monkeypatch):
    monkeypatch.setattr(rt, "LEND_HALF_OPEN", 0.2)
    lender, far = held_lender(folder, limit=1)
    held = []
    try:
        held.append(rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")[1])
        for _ in range(300):  # (whichever comes first, the request or the lender's close: never "did not answer")
            # (a look - ping - is answered on a spare place now: tests/test_lent_tunnel_busy.py)
            with pytest.raises(rt.LenderRefused, match="too many connections"):
                rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")
        assert issubclass(rt.LenderRefused, OSError), "what a relay catches for a connection that fails"
        assert rt.lender_gone("wg", folder / "lend.sock") is None, "full, not gone"
        held.pop().close()
        # The far end (held open by the test, as a server that never closes) keeps the slot no longer than a moment.
        assert waited(lambda: free_slots(lender) == 1)
        assert rt.lender_gone("wg", folder / "lend.sock") is None
        assert "'wg'" in rt.lender_gone("other", folder / "lend.sock"), "another profile's tunnel is not this one's"
    finally:
        for sock in held + far:
            sock.close()
        lender.close()
    assert "did not answer" in rt.lender_gone("wg", folder / "lend.sock")


def test_a_refusal_is_read_even_when_the_lender_closed_before_the_request_was_sent(folder, monkeypatch):
    """A lender that is full says so and closes at once, without reading: the request then meets a closed socket
    (EPIPE). That was "did not answer" - a full lender taken for a gone one after all, now and then."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(folder / "lend.sock"))
    server.listen(4)
    closed = threading.Event()

    def refuse_at_once():
        conn, _ = server.accept()
        rt.refuse(conn, "too many connections through the start's tunnel at once")
        closed.set()
    threading.Thread(target=refuse_at_once, daemon=True).start()
    send = rt.send_line

    def late(sock, document):
        if document.get("op") == "ping":
            assert closed.wait(5)
        return send(sock, document)
    monkeypatch.setattr(rt, "send_line", late)
    try:
        with pytest.raises(rt.LenderRefused, match="too many connections"):
            rt.ask_lender({"op": "ping"}, folder / "lend.sock")
    finally:
        server.close()


def serving(monkeypatch, folder, payload):
    """rt.main(["serve"]) of a borrowing container in a thread, its kernel stood in for: (thread, the result's list,
    its output, the write end of its standard input)."""
    class Runner(rt.Runner):
        def run(self, command, data=None, check=True):
            return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(rt, "LEND_SOCKET", folder / "lend.sock")
    monkeypatch.setattr(rt.Borrower, "start", lambda self: self)
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner, **kw: {})
    monkeypatch.setattr(rt, "LEND_PING", 0)  # (a look at the lender each time the loop comes round)
    read, write = os.pipe()
    os.write(write, (json.dumps(payload) + "\n").encode())
    out, result = io.StringIO(), []
    stdin = open(read)
    thread = threading.Thread(target=lambda: result.append(rt.main(["serve"], stdin=stdin, out=out, runner=Runner())),
                              daemon=True)
    thread.start()
    assert waited(lambda: '"ready"' in out.getvalue(), 20), out.getvalue()
    return thread, result, out, write


def ticked(write, thread, times=8):
    """The serving loop comes round TIMES times (an empty line each: no request), unless it ended."""
    for _ in range(times):
        if not thread.is_alive():
            return
        try:
            os.write(write, b"\n")
        except OSError:
            return
        time.sleep(0.05)


BORROWING = {"profile": {"name": "wg", "type": "wireguard", "wireguard": {}}, "secrets": {"lend_token": TOKEN},
             "checks": {"ip": False}, "borrow": {"run": "codex-1"}}


def test_a_server_that_holds_every_connection_of_the_lender_does_not_end_the_other_servers(monkeypatch, folder):
    """The lender serves LEND_LIMIT connections at once. A borrower looked at it every few seconds and took "too many
    connections" for "gone": three such looks, and the VPN containers of ALL the run's other servers said "down" and
    ended - one server (a crawler, or a hostile one) took the network of every other one away."""
    lender, far = held_lender(folder, limit=1)
    held = []
    thread, result, out, write = serving(monkeypatch, folder, BORROWING)
    try:
        held.append(rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")[1])
        ticked(write, thread)
        assert thread.is_alive() and '"down"' not in out.getvalue(), out.getvalue()
    finally:
        os.close(write)
        thread.join(10)
        for sock in held + far:
            sock.close()
        lender.close()
    assert result == [0], "it served until its launcher closed its input"


def test_a_borrower_whose_lender_is_gone_or_lends_another_profile_ends_as_before(monkeypatch, folder):
    lender, far = held_lender(folder)
    thread, result, out, write = serving(monkeypatch, folder, BORROWING)
    try:
        lender.close()
        ticked(write, thread)
        thread.join(10)
        assert result == [3] and "the start that lent its tunnel is gone" in out.getvalue()
    finally:
        os.close(write)
    # The same folder (macOS: the volume of a run's name, used again by its next start) with another profile's lender.
    lender, far = held_lender(folder)
    thread, result, out, write = serving(monkeypatch, folder, BORROWING)
    try:
        lender.close()
        other, _ = held_lender(folder, profile="other")
        try:
            ticked(write, thread)
            thread.join(10)
        finally:
            other.close()
        assert result == [3] and "'other'" in out.getvalue(), out.getvalue()
    finally:
        os.close(write)


def test_one_borrower_takes_only_its_own_share_of_connections(folder):
    """A borrower's relay has slots of its own (fewer than the lender's): what comes beyond them is closed at once and
    never reaches the lender - so one server cannot hold all the lender's connections against the others."""
    assert rt.BORROW_LIMIT * 2 <= rt.LEND_LIMIT, "one server alone never fills the lender"
    asked, gate = [], threading.Event()

    def ask(request, path=None, timeout=10):
        asked.append(request)
        gate.wait(10)
        raise OSError("the test's lender is busy")
    borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443), ask=ask, limit=2)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    borrower.sockets = [server]
    threading.Thread(target=borrower.accept_tcp, args=(server,), daemon=True).start()
    clients = [socket.create_connection(server.getsockname(), timeout=5) for _ in range(5)]
    try:
        assert waited(lambda: len(asked) == 2)
        closed = 0
        for client in clients:
            client.settimeout(0.5)
            try:
                closed += client.recv(1) == b""
            except (socket.timeout, ConnectionResetError) as exc:
                closed += isinstance(exc, ConnectionResetError)
        assert closed == 3 and len(asked) == 2, "three were closed at once; the lender was asked for two"
        gate.set()
        assert waited(lambda: borrower.slots._value == 2), "their slots came back"
    finally:
        gate.set()
        for client in clients:
            client.close()
        borrower.close()


def test_a_flood_of_lookups_starts_no_thread_per_packet_beyond_the_borrowers_share(folder):
    asked, gate = [], threading.Event()

    def ask(request, path=None, timeout=10):
        asked.append(request)
        gate.wait(10)
        raise OSError("busy")
    borrower = rt.Borrower(path=folder / "lend.sock", ask=ask, limit=3)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    borrower.sockets = [udp]
    threading.Thread(target=borrower.serve_udp, args=(udp,), daemon=True).start()
    before = threading.active_count()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for number in range(200):
            client.sendto(rt.dns_query_packet("example.com", 1, number), udp.getsockname())
        assert waited(lambda: len(asked) == 3)
        time.sleep(0.3)
        assert len(asked) == 3 and threading.active_count() - before <= 3, "the others were dropped, not queued"
    finally:
        gate.set()
        client.close()
        borrower.close()


def test_a_far_end_that_never_closes_keeps_no_slot_once_the_servers_program_is_gone(folder, monkeypatch):
    """The server's program closed its connection (or ended); the far end says nothing and never closes. The lender
    waited for it for ever: its slot, and the relay's, were gone for good - enough such far ends (anyone's web
    server) and the run's servers had no network left."""
    monkeypatch.setattr(rt, "LEND_HALF_OPEN", 0.3)
    lender, far = held_lender(folder, limit=2)
    borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443), limit=2)
    program, relayed = socket.socketpair()
    try:
        assert borrower.hand(borrower.relay, relayed)
        program.sendall(b"GET / HTTP/1.0\r\n\r\n")
        assert waited(lambda: len(far) == 1) and far[0].recv(100).startswith(b"GET /")
        assert free_slots(lender) == 1 and borrower.slots._value == 1
        program.close()
        assert waited(lambda: free_slots(lender) == 2 and borrower.slots._value == 2), "both slots are free again"
        far[0].settimeout(2)
        assert far[0].recv(10) == b"", "and the far end was told (closed)"
    finally:
        for sock in far:
            sock.close()
        lender.close()


def test_a_connection_that_is_open_both_ways_or_still_says_something_is_kept(folder, monkeypatch):
    """Only silence after one direction's end closes a connection: an idle one open both ways stays (a websocket, an
    event stream), and so does one whose far end still sends after the program's half-close (a download)."""
    monkeypatch.setattr(rt, "LEND_HALF_OPEN", 0.3)
    lender, far = held_lender(folder, limit=2)
    held = []
    try:
        idle = rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")[1]
        held.append(idle)
        idle.settimeout(None)
        time.sleep(1.0)  # (silent both ways for longer than LEND_HALF_OPEN)
        idle.sendall(b"ping")
        far[0].settimeout(2)
        assert far[0].recv(10) == b"ping", "open both ways: kept"
        far[0].sendall(b"pong")
        idle.settimeout(2)
        assert idle.recv(10) == b"pong"
        half = rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 80}, folder / "lend.sock")[1]
        held.append(half)
        half.settimeout(2)
        half.sendall(b"request")
        half.shutdown(socket.SHUT_WR)
        far[1].settimeout(2)
        assert far[1].recv(10) == b"request" and far[1].recv(10) == b"", "the half-close is passed on"
        got = b""
        for number in range(8):  # (0.8 s of an answer in pieces: longer than LEND_HALF_OPEN, never silent that long)
            far[1].sendall(b"%d" % number)
            got += half.recv(10)
            time.sleep(0.1)
        assert got == b"01234567"
        far[1].close()
        assert half.recv(10) == b""
    finally:
        for sock in held + far:
            sock.close()
        lender.close()


class FlakyServer:
    """A listening socket whose accept() fails as the kernel's does when it passes (ERRORS, one after another), then
    hands out CONNECTIONS, then is closed."""

    def __init__(self, errors, connections):
        self.errors, self.connections, self.closed = list(errors), list(connections), False

    def accept(self):
        if self.errors:
            raise self.errors.pop(0)
        if self.connections:
            return self.connections.pop(0), None
        self.closed = True
        raise OSError(errno.EBADF, "closed")

    def fileno(self):
        return -1 if self.closed else 3

    def close(self):
        self.closed = True


def test_the_lenders_accept_loop_outlives_an_error_that_passes(folder, monkeypatch):
    """No descriptor left for a moment (EMFILE), a connection reset before it was taken: the loop ended on any
    OSError, and with it the network of every server of the run, for good."""
    monkeypatch.setattr(rt, "ACCEPT_PAUSE", 0.01)
    near, far = socket.socketpair()
    lender = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock")
    lender.server = FlakyServer([OSError(errno.EMFILE, "too many open files"), ConnectionAbortedError()], [near])
    thread = threading.Thread(target=lender.serve, daemon=True)
    thread.start()
    far.settimeout(5)
    rt.send_line(far, {"op": "ping", "token": TOKEN})
    assert json.loads(rt.read_line(far))["profile"] == "wg", "served after the errors"
    far.close()
    thread.join(5)
    assert not thread.is_alive(), "a closed socket ends it"


def test_no_thread_to_be_had_refuses_that_connection_and_keeps_the_slot_and_the_loop(folder, monkeypatch):
    monkeypatch.setattr(rt, "ACCEPT_PAUSE", 0.01)
    first, first_far = socket.socketpair()
    second, second_far = socket.socketpair()
    lender = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock", limit=1)
    lender.server = FlakyServer([], [first, second])
    real, failed = threading.Thread.start, []

    def start(self):
        if getattr(self, "_target", None) == lender.handle and not failed:
            failed.append(True)
            raise RuntimeError("can't start new thread")
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    thread = threading.Thread(target=lender.serve, daemon=True)
    real(thread)
    first_far.settimeout(5)
    assert "error" in json.loads(rt.read_line(first_far)), "said, not left hanging"
    second_far.settimeout(5)
    rt.send_line(second_far, {"op": "ping", "token": TOKEN})
    assert json.loads(rt.read_line(second_far))["ok"] is True, "the slot of the refused one came back"
    for sock in (first_far, second_far):
        sock.close()
    thread.join(5)
    assert failed and waited(lambda: free_slots(lender) == 1)


def test_a_borrowers_accept_loop_outlives_an_error_that_passes(folder, monkeypatch):
    monkeypatch.setattr(rt, "ACCEPT_PAUSE", 0.01)
    relayed = []
    borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443),
                           ask=lambda request, path=None, timeout=10: relayed.append(request) or
                           (_ for _ in ()).throw(OSError("no lender")))
    near, far = socket.socketpair()
    server = FlakyServer([OSError(errno.ENFILE, "file table overflow")], [near])
    borrower.sockets = [server]
    thread = threading.Thread(target=borrower.accept_tcp, args=(server,), daemon=True)
    thread.start()
    far.settimeout(5)
    assert far.recv(1) == b"" and relayed == [{"op": "tcp", "address": "93.184.216.34", "port": 443}]
    far.close()
    thread.join(5)
    assert not thread.is_alive()


# --- what a lender connects to ------------------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "0.0.0.0", "0.1.2.3", "10.64.0.1", "100.64.0.1", "100.100.100.100", "100.127.255.254", "127.0.0.1", "127.0.54.1",
    "169.254.169.254", "169.254.170.2", "172.16.0.1", "172.31.255.254", "192.0.0.8", "192.0.0.9", "192.0.0.170",
    "192.0.2.1", "192.168.0.1", "198.18.0.1", "198.19.255.254", "198.51.100.7", "203.0.113.5",
    "224.0.0.1", "224.0.0.251", "233.252.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255"])
def test_no_address_of_a_special_network_is_lent_whatever_this_python_calls_global(address):
    """Multicast (224.0.0.0/4) is "global" to ipaddress here; the networks are named in the runtime itself, so that
    the VPN image's Python decides nothing."""
    with pytest.raises(ValueError, match="no public address"):
        rt.lend_target(address, 443, local=lambda a: False)


@pytest.mark.parametrize("address", [
    "::ffff:1.1.1.1", "::1.1.1.1", "1.1.1.1 ", " 1.1.1.1", "1.1.1.1\n", "01.1.1.1", "1.1.1", "1.1.1.1.", "16843009",
    "0x1010101", "1.1.1.1/32", "1.1.1.1:443", "１.１.１.１", "1.1.1.١", "", None, 16843009, 1.1, ["1.1.1.1"],
    {"a": "1.1.1.1"}, True, "example.com", "[1.1.1.1]", "1.1.1.1%lo", "1.1.1.1\x00"])
def test_only_a_plain_ipv4_address_is_an_address(address):
    with pytest.raises(ValueError, match="not an IPv4 address"):
        rt.lend_target(address, 443, local=lambda a: False)


@pytest.mark.parametrize("port", [0, -1, 65536, 1 << 40, "443", 443.0, None, True, [443], "44３"])
def test_only_a_port_is_a_port(port):
    with pytest.raises(ValueError, match="not a port"):
        rt.lend_target("1.1.1.1", port, local=lambda a: False)


def test_public_addresses_are_lent_on_every_port():
    """(Port 25 included: an image server behind a VPN of its own has every port too - the lender narrows nothing a
    server's own VPN container gave; said in the review.)"""
    for address, port in (("1.1.1.1", 443), ("93.184.216.34", 80), ("8.8.8.8", 25), ("9.9.9.9", 65535)):
        assert rt.lend_target(address, port, local=lambda a: False) == (address, port)


def test_the_kill_switch_holds_a_lent_connection_to_the_tunnel_as_any_other_packet():
    """The lender's sockets carry CHECK_MARK (the agent's firewall lets them by). The VPN's kill switch does not know
    that mark: such a packet leaves through the tunnel's interface or not at all - never to the VPN's own endpoint
    over the uplink (only the tunnel's own mark goes there), never to the network beside the tunnel. Tailscale's
    "any marked packet" is tailscaled's mark under its mask, which CHECK_MARK is not."""
    assert rt.CHECK_MARK != rt.MARK
    mark, mask = rt.TAILSCALE_MARK
    assert rt.CHECK_MARK & mask != mark
    for rules in (rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820)]),
                  rt.killswitch_rules("tailscale0", mark=mark, mask=mask, any_marked=True)):
        assert f"{rt.CHECK_MARK:#x}" not in rules
        output = rules.split(" chain output", 1)[1].split(" }", 1)[0]
        assert "policy drop" in output
        for line in (line.strip() for line in output.splitlines() if line.strip().endswith(" accept")):
            assert line.startswith(('oifname "lo"', 'oifname "wg0"', 'oifname "tailscale0"', "ct status dnat ",
                                    "ip6 hoplimit 255 icmpv6 ", "meta mark ")), line
            if line.startswith("meta mark "):
                assert line.startswith((f"meta mark {rt.MARK:#x} ", f"meta mark and {mask:#x} == {mark:#x} ")), line


# --- the socket's protocol ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    b"", b"{}", b"[]", b"null", b"7", b'"tcp"', b"{", b"\xff\xfe\x00", b'{"op":"tcp"}', b'{"op":["tcp"]}',
    b'{"op":"tcp","address":"1.1.1.1"}', b'{"op":"tcp","address":"127.0.0.1","port":22}',
    b'{"op":"tcp","address":"1.1.1.1","port":"22"}', b'{"op":"tcp","address":["1.1.1.1"],"port":22}',
    b'{"op":"dns"}', b'{"op":"dns","query":"!!!"}', b'{"op":"dns","query":"AAAA"}', b'{"op":"dns","query":7}',
    b'{"op":"exec","command":"id"}', b'{"op":"ping","op":"shell"}', b"[" * 3000 + b"]" * 3000,
    b'{"op":"tcp","address":"1.1.1.1","port":1e400}', b'{"op":"tcp","address":"1.1.1.1","port":NaN}',
    b'{"op":"TCP","address":"1.1.1.1","port":443}', b'{"op":"tcp ","address":"1.1.1.1","port":443}'])
def test_what_is_no_request_is_refused_and_its_slot_comes_back(folder, line):
    reached = []
    lender = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock", limit=2,
                       connect=lambda target: reached.append(target), lookup=lambda q, r: reached.append(q),
                       local=lambda a: False).start()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(folder / "lend.sock"))
        sock.sendall(line + b"\n")
        answer = json.loads(rt.read_line(sock, 1 << 16))
        assert "error" in answer and "ok" not in answer, answer
        assert sock.recv(1) == b"", "closed"
        sock.close()
        assert reached == [] and waited(lambda: free_slots(lender) == 2)
        assert rt.lender_gone("wg", folder / "lend.sock") is None, "it goes on serving"
    finally:
        lender.close()


def test_a_request_that_is_too_long_or_ends_early_is_refused(folder):
    lender = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock", limit=2).start()
    try:
        for data, why in ((b"x" * (rt.LEND_LINE + 2), "too long"), (b'{"op":"ping"', "ended early")):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(folder / "lend.sock"))
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            assert why in json.loads(rt.read_line(sock))["error"]
            sock.close()
        assert waited(lambda: free_slots(lender) == 2)
    finally:
        lender.close()


def test_a_dns_answer_of_four_kilobytes_crosses_the_socket(folder):
    """EDNS answers reach 4096 bytes: in base64 more than the request's line may have. The answer's line has a limit
    of its own."""
    big = bytes(range(256)) * 16  # 4096 bytes
    query = rt.dns_query_packet("example.com", 16, 0x4242)
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock",
                       lookup=lambda q, resolver: q[:2] + big[2:]).start()
    try:
        answer = rt.Borrower(path=folder / "lend.sock").lookup(query)
        assert answer == query[:2] + big[2:]
        with pytest.raises(OSError):  # (an answer no resolver gives over UDP: still bounded)
            lender.lookup = lambda q, resolver: b"x" * (200 << 10)
            rt.Borrower(path=folder / "lend.sock").lookup(query)
    finally:
        lender.close()


def test_a_lookup_ends_at_its_time_whatever_the_resolver_sends(monkeypatch):
    """Answers to other questions, without end: the lookup waited 0.1 s for each, for ever, and held its slot."""
    class Chatty:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, address):
            self.address = address

        def send(self, data):
            pass

        def settimeout(self, seconds):
            assert seconds > 0

        def recv(self, size):
            time.sleep(0.01)
            return b"\xde\xad" + b"\x00" * 30
    monkeypatch.setattr(rt, "lent_socket", lambda kind=None: Chatty())
    begun = time.monotonic()
    with pytest.raises(OSError):
        rt.lent_lookup(rt.dns_query_packet("example.com", 1, 7), "10.66.0.1", timeout=0.3)
    assert time.monotonic() - begun < 3


def test_a_truncated_answer_is_asked_again_over_tcp_through_the_tunnel(monkeypatch):
    """The lender asked over UDP only. A stub without EDNS (glibc's) gets 512 bytes, sees "truncated" and asks again
    over TCP - which the relay handed to the lender, which asked over UDP again: truncated again, the name never
    resolved. Now a truncated answer is asked again over TCP (a marked socket, through the tunnel as the first)."""
    import struct
    query = rt.dns_query_packet("example.com", 16, 0x77)
    truncated = query[:2] + b"\x83\x80" + query[4:]  # (QR, TC, RD)
    full = query[:2] + b"\x81\x80" + query[4:] + b"x" * 2000
    made, tcp_fails = [], []

    class Fake:
        def __init__(self, kind):
            self.kind, self.sent, self.address = kind, b"", None
            self.stream = struct.pack("!H", len(full)) + full
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, address):
            if self.kind == socket.SOCK_STREAM and tcp_fails:
                raise ConnectionRefusedError("no TCP there")
            self.address = address

        def settimeout(self, seconds):
            assert seconds is None or seconds > 0

        def send(self, data):
            self.sent += data

        sendall = send

        def recv(self, size):
            if self.kind == socket.SOCK_DGRAM:
                return truncated
            piece, self.stream = self.stream[:min(size, 700)], self.stream[min(size, 700):]
            return piece
    monkeypatch.setattr(rt, "lent_socket", lambda kind=socket.SOCK_STREAM: Fake(kind))
    assert rt.lent_lookup(query, "10.66.0.1", tcp=True) == full
    assert [fake.kind for fake in made] == [socket.SOCK_DGRAM, socket.SOCK_STREAM]
    assert made[1].address == ("10.66.0.1", 53) and made[1].sent == struct.pack("!H", len(query)) + query
    del made[:]
    assert rt.lent_lookup(query, "10.66.0.1") == truncated, "asked over UDP: the UDP answer, as its asker takes it"
    assert [fake.kind for fake in made] == [socket.SOCK_DGRAM]
    tcp_fails.append(True)
    assert rt.lent_lookup(query, "10.66.0.1", tcp=True) == truncated, "no TCP at the resolver: the answer as it came"


def test_a_question_that_came_over_tcp_is_told_so_to_the_lender(folder):
    import struct
    asked = []

    def lookup(query, resolver, tcp=False):
        asked.append(tcp)
        return query[:2] + b"\x81\x80" + query[4:]
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock",
                       lookup=lookup).start()
    borrower = rt.Borrower(path=folder / "lend.sock")
    program, relayed = socket.socketpair()
    try:
        query = rt.dns_query_packet("example.com", 1, 0x99)
        assert borrower.lookup(query)[:2] == query[:2]
        threading.Thread(target=borrower.serve_dns_tcp, args=(relayed,), daemon=True).start()
        program.settimeout(5)
        program.sendall(struct.pack("!H", len(query)) + query)
        size = struct.unpack("!H", rt.read_exact(program, 2))[0]
        assert rt.read_exact(program, size)[:2] == query[:2]
        assert asked == [False, True]
        # ... and only a true "tcp" counts (a request is the relay's own, but nothing in it is trusted for more).
        rt.ask_lender({"op": "dns", "query": base64.b64encode(query).decode(), "tcp": "yes"},
                      folder / "lend.sock")[1].close()
        assert asked == [False, True, False]
    finally:
        program.close()
        lender.close()


# --- the host's side ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("run", ["../x", "..", ".", "", "a/b", "/abs", "a\x00b", "a b", "-rf", ".hidden", "a" * 101,
                                 "run\n", None, 7, ["codex-1"], "a,ro=false", "x:y"])
def test_a_run_is_named_plainly_or_not_at_all(home, run):
    for function in (v.check_run, v.lend_dir, v.lend_volume, lambda r: v.lend_mount(r),
                     lambda r: v.lend_mount(r, machine=True), lambda r: v.lent_tunnel_up(r) or v.check_run(r)):
        with pytest.raises(ValueError, match="not a run's name"):
            function(run)


def test_the_folder_of_a_lent_tunnel_is_made_anew_private_and_never_a_link(home):
    """A folder a killed start of the name left goes with what is in it; a link in its place (to a folder of yours:
    the lender's container would have mounted THAT, writable, and made it 0700) is removed, not followed."""
    import argparse
    import contextlib
    wireguard()
    a = argparse.Namespace(backend="podman", podman_url=None, run_id="codex-1")
    if sys.platform == "darwin":
        pytest.skip("macOS: the socket lies in a Podman volume")
    folder = v.lend_dir("codex-1")
    (folder / "socket").mkdir(parents=True)
    (folder / "socket" / "lend.sock").write_text("stale")
    (folder / "token").write_text("an old run's")
    with contextlib.ExitStack() as cleanup:
        assert v.prepare_lending(a, ["podman"], cleanup)[0] == ("lender", "codex-1", False)
        assert folder.is_dir() and not folder.is_symlink()
        # Made anew: the empty folder of the socket (what the VPN containers mount), this run's own token, and the
        # lock its launcher holds while it runs.
        assert sorted(path.name for path in folder.iterdir()) == ["lock", "socket", "token"]
        assert list((folder / "socket").iterdir()) == [] and (folder / "token").read_text() != "an old run's"
        assert oct(folder.stat().st_mode & 0o777) == "0o700" and oct(folder.parent.stat().st_mode & 0o777) == "0o700"
        assert oct((folder / "socket").stat().st_mode & 0o777) == "0o700"
    assert not folder.exists(), "gone with the start"
    elsewhere = home / "home" / "documents"
    elsewhere.mkdir(mode=0o755)
    (elsewhere / "letter.txt").write_text("mine")
    folder.symlink_to(elsewhere, target_is_directory=True)
    with contextlib.ExitStack() as cleanup:
        v.prepare_lending(a, ["podman"], cleanup)
        assert folder.is_dir() and not folder.is_symlink(), "a folder of its own, not the link"
        assert oct(elsewhere.stat().st_mode & 0o777) == "0o755", "what the link named was not touched"
    assert (elsewhere / "letter.txt").read_text() == "mine"
    # The folder above it a link: nothing is lent through it.
    shutil.rmtree(folder.parent)
    folder.parent.symlink_to(elsewhere, target_is_directory=True)
    with contextlib.ExitStack() as cleanup, pytest.raises(ValueError, match="is a link"):
        v.prepare_lending(a, ["podman"], cleanup)
    assert sorted(path.name for path in elsewhere.iterdir()) == ["letter.txt"]


def test_no_preset_project_file_or_server_names_the_run_to_borrow_from(home):
    """--vpn-lent-by is the launcher's own word to a server's sandbox (mcp.server_network): whoever else could say it
    would put a sandbox behind another run's tunnel."""
    l = m.launcher()
    presets = load("presets_for_lent_vpn_security", "sandbox/presets.py")
    assert "--vpn-lent-by" in l.CONFIG_FORBIDDEN and "--vpn-lent-by" in m.forbidden()
    for origin in ("built-in", "extra", "yours"):
        assert "--vpn-lent-by" in presets.refused_flags(origin)
        document = {"format": presets.FORMAT, "version": 1, "id": "x", "title": "x", "description": "x",
                    "options": ["--vpn-lent-by=codex-1"]}
        assert any("--vpn-lent-by is not allowed" in problem for problem in presets.problems(document, origin=origin))
    for argv in (["--vpn-lent", "codex-1"], ["--vpn-l=codex-1"]):  # (no abbreviation slips past the lists)
        with pytest.raises(SystemExit):
            l.parse_args(["codex", "--vpn", "wg", *argv])
    wireguard()
    with pytest.raises(ValueError, match="for the sandbox of an MCP server"):
        v.validate(l.parse_args(["codex", "--vpn", "wg", "--vpn-lent-by", "codex-1"]))


def test_the_folder_of_the_lent_tunnel_is_no_sandboxs_to_see_by_any_spelling(home):
    """A workspace, a --mount, a --state-dir, an MCP image server's folder: the folder itself, one above it, one in
    it, and a link to it are all said (vpn.check_paths; the launcher's own check_given_path says sandbox-ai's folders
    whatever is in them) - given with the warning: you decide what a sandbox gets."""
    l = m.launcher()
    wireguard()
    folder = v.lend_dir("codex-1")
    folder.mkdir(parents=True)
    link = home / "home" / "innocent"
    link.symlink_to(folder, target_is_directory=True)
    above = home / "home" / "above"
    above.symlink_to(folder.parent.parent.parent, target_is_directory=True)  # (.../sandbox-ai)
    for path in (folder, folder.parent, folder.parent.parent, folder.parent.parent.parent.parent, folder / "sub",
                 link, above, Path(str(folder) + "/../codex-1")):
        said = v.check_paths([path], "--mount", warn=lambda _text: None)
        assert said and "where the VPN profiles keep" in said[0] and "read-only" in said[0], path
    for path in (folder, folder.parent, link, above):
        for writable in (False, True):
            assert any("sandbox-ai's own folder" in text for text in l.check_given_path(
                Path(path).resolve(), "--mount", writable, warn=lambda _text: None)), path
