"""Behind Tor every lookup is answered with IPv4 only - the CI's killswitch job (run 43739790958, step "Tor
container") found an MCP server behind a Tor profile getting an IPv6 address for an .onion name:

    'onion via 1.1.1.1': ['10.207.93.73'], 'onion mapped by tor': True, 'onion in the announced network': True,
    'onion AAAA': ['fea7:679b:3ed:106d:bfac:86d0:a0cb:8436'], 'onion http': 200

A fetcher's getaddrinfo asks for AAAA too: it got a link-local address (tor's VirtualAddrNetworkIPv6, fe80::/10)
outside the network it is told (SANDBOX_AI_TOR_VIRTUAL_NETWORK) and refused the name, or (IPv6 first: Happy
Eyeballs, Node, Go) tried an address nothing here reaches - the Tor sandbox has no IPv6 - before the IPv4 one. A
name of the internet got the exits' real IPv6 addresses the same way.

The root cause is in tor itself, not in a flag the torrc lacks: tor's DNSPort (src/feature/client/dnsserv.c, 0.4.8
and 0.4.9 alike - bookworm's is 0.4.9.11) sets each request's families from its question type - an AAAA question is
"IPv6 only, prefer IPv6" whatever the DNSPort's flags say (NoIPv6Traffic, NoPreferIPv6Automap, IPv4Traffic are
overwritten) - and AutomapHostsOnResolve then maps the name to an IPv6 virtual address (connection_edge.c); the
IPv6 virtual network cannot be switched off (VirtualAddrNetworkIPv6 must be a /104 or larger). So tor's DNSPort
moved off port 53 (TOR_DNS_PORT, asked by nothing else) and a front on tor's address and port 53 (TorDnsFront, in
the Tor container's runtime) hands every question to it, an AAAA question asked as A: its answer keeps tor's answer
code (NXDOMAIN, SERVFAIL while tor has no circuit yet - the patience of TorDns is unchanged) with no address. The
SocksPort (a profile's socks) gets NoPreferIPv6Automap: a SOCKS RESOLVE of an .onion name is mapped to IPv4 too.

Through real UDP sockets on loopback: a stand-in for tor's DNSPort that answers as tor 0.4.8 does (AAAA of an .onion
name: an fe80::/10 address), the front in front of it, and the runtime's own dns_lookup asking."""
import contextlib
import importlib.util
import ipaddress
from pathlib import Path
import socket
import struct
import threading
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vpn_runtime_tor_dns_ipv4_only", ROOT / "sandbox" / "vpn_runtime.py")
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

ONION = rt.ONION_PROBE
ONION_V4 = "10.207.93.73"
ONION_V6 = "fea7:679b:3ed:106d:bfac:86d0:a0cb:8436"  # the CI's answer


def question(packet):
    """(name, qtype) of a DNS packet's first question."""
    offset, labels = 12, []
    while packet[offset]:
        labels.append(packet[offset + 1:offset + 1 + packet[offset]].decode())
        offset += 1 + packet[offset]
    return ".".join(labels), struct.unpack("!H", packet[offset + 1:offset + 3])[0], offset + 5


def reply(query, rcode=0, records=()):
    """tor's answer to QUERY: RCODE, and RECORDS [(qtype, rdata)] for its name (a pointer to the question's)."""
    _name, _qtype, end = question(query)
    flags = 0x8000 | (struct.unpack("!H", query[2:4])[0] & 0x7900) | 0x0080 | rcode
    out = query[:2] + struct.pack("!HHHHH", flags, 1, len(records), 0, 0) + query[12:end]
    for rtype, rdata in records:
        out += struct.pack("!HHHIH", 0xC00C, rtype, 1, 60, len(rdata)) + rdata
    return out


class FakeTorDnsPort:
    """tor 0.4.8's DNSPort as far as it matters here: .onion names automapped (A: TOR_VIRTUAL_NET, AAAA: its
    link-local VirtualAddrNetworkIPv6), other names as an exit resolves them (both families), NXDOMAIN for
    nx.example, SERVFAIL for every name while WARMING (no circuit yet), nothing at all while SILENT."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self.asked, self.warming, self.silent = [], False, False
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                query, peer = self.sock.recvfrom(4096)
            except OSError:
                return
            name, qtype, _end = question(query)
            self.asked.append((name, qtype))
            if self.silent:
                continue
            if self.warming:
                answer = reply(query, 2)
            elif name == "nx.example":
                answer = reply(query, 3)
            elif name.endswith(".onion"):
                address = ONION_V4 if qtype == 1 else ONION_V6
                answer = reply(query, 0, [(qtype, ipaddress.ip_address(address).packed)])
            else:
                address = "192.0.2.1" if qtype == 1 else "2001:db8::1"
                answer = reply(query, 0, [(qtype, ipaddress.ip_address(address).packed)])
            with contextlib.suppress(OSError):
                self.sock.sendto(answer, peer)

    def close(self):
        self.sock.close()


@pytest.fixture
def tor_dns():
    """(tor's DNSPort stand-in, the front's port)"""
    tor = FakeTorDnsPort()
    front = rt.TorDnsFront(address="127.0.0.1", port=0, tor=tor.address, wait=1.5).start()
    try:
        yield tor, front.sockets[0].getsockname()[1]
    finally:
        front.close()
        tor.close()


def raw(port, query, timeout=4.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(query, ("127.0.0.1", port))
        return sock.recv(4096)


# == the torrc ======================================================================================================

def test_tors_own_dnsport_is_off_port_53_and_the_front_takes_tors_address():
    lines = rt.tor_config({}).splitlines()
    assert f"DNSPort {rt.TOR_DNS}:{rt.TOR_DNS_PORT}" in lines and rt.TOR_DNS_PORT != 53
    assert f"DNSPort {rt.TOR_DNS}:53" not in lines
    # What stays: .onion names mapped into the announced IPv4 network, the TransPort, IPv4 to the Tor network only.
    for line in ("AutomapHostsOnResolve 1", "AutomapHostsSuffixes .onion",
                 f"VirtualAddrNetworkIPv4 {rt.TOR_VIRTUAL_NET}", "ClientUseIPv6 0", "ClientPreferIPv6ORPort 0",
                 f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT} IsolateDestAddr", "SocksPort 0"):
        assert line in lines, line
    # The kill switch hands every lookup to the front, on tor's address and port 53.
    assert f"udp dport 53 dnat ip to {rt.TOR_DNS}:53" in rt.tor_rules()


def test_a_socks_resolve_of_an_onion_name_is_mapped_to_ipv4():
    """tor's SocksPort prefers an IPv6 automap by default (PreferIPv6Automap): a SOCKS5 RESOLVE of an .onion name
    (tor-resolve, a client that resolves through the proxy) got an IPv6 virtual address."""
    lines = rt.tor_config({"socks": True}).splitlines()
    assert f"SocksPort 127.0.0.1:{rt.TOR_SOCKS_PORT} IsolateDestAddr NoPreferIPv6Automap" in lines


# == the front ======================================================================================================

def test_without_the_front_tor_maps_an_onion_name_to_ipv6_as_the_ci_saw(tor_dns):
    tor, _port = tor_dns
    assert rt.dns_lookup(ONION, "127.0.0.1", 28, port=tor.address[1]) == [ONION_V6]  # tor's own answer


def test_an_onion_name_has_no_ipv6_address_through_the_front(tor_dns):
    tor, port = tor_dns
    assert rt.dns_lookup(ONION, "127.0.0.1", 28, port=port) == []
    assert rt.dns_lookup(ONION, "127.0.0.1", 1, port=port) == [ONION_V4]
    # tor was asked for IPv4 only: it mapped no IPv6 virtual address at all.
    assert tor.asked == [(ONION, 1), (ONION, 1)]


def test_a_name_of_the_internet_has_no_ipv6_address_through_the_front(tor_dns):
    _tor, port = tor_dns
    assert rt.dns_lookup("example.org", "127.0.0.1", 28, port=port) == []
    assert rt.dns_lookup("example.org", "127.0.0.1", 1, port=port) == ["192.0.2.1"]


def test_the_aaaa_answer_is_tors_answer_code_for_the_question_asked(tor_dns):
    _tor, port = tor_dns
    query = rt.dns_query_packet(ONION, 28, 0x1234)[:2] + b"\x01\x20" + rt.dns_query_packet(ONION, 28, 0)[4:]
    answer = raw(port, query)
    ident, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", answer[:12])
    assert (ident, qd, an, ns, ar) == (0x1234, 1, 0, 0, 0)
    assert flags & 0x8000 and flags & 0x0100 and not flags & 0x0200 and flags & 0xF == 0  # QR, RD, not TC, NOERROR
    assert question(answer)[:2] == (ONION, 28)  # the question as asked, not tor's A
    assert answer[12:] == query[12:]  # (the question exactly, nothing after it)
    nx = raw(port, rt.dns_query_packet("nx.example", 28, 7))
    assert struct.unpack("!H", nx[2:4])[0] & 0xF == 3  # NXDOMAIN stays NXDOMAIN


def test_servfail_before_tors_first_circuit_passes_for_aaaa_as_for_a(tor_dns):
    """The hotfix's patience (TorDns: SERVFAIL right after the bootstrap is "not yet") sees what tor says."""
    tor, port = tor_dns
    tor.warming = True
    for qtype in (1, 28):
        with pytest.raises(rt.DnsError) as caught:
            rt.dns_lookup("api.anthropic.com", "127.0.0.1", qtype, port=port)
        assert caught.value.rcode == 2
    tor.warming = False
    assert rt.dns_lookup("api.anthropic.com", "127.0.0.1", 1, port=port) == ["192.0.2.1"]


def test_no_answer_from_tor_is_no_answer_from_the_front(tor_dns):
    """tor silent (no circuit, a slow exit): the asker sees no answer, as from tor itself ("no answer from
    127.0.53.1"), never an answer the front made up - TorDns asks again."""
    tor, port = tor_dns
    tor.silent = True
    for qtype in (1, 28):
        with pytest.raises(TimeoutError, match="no answer"):
            rt.dns_lookup(ONION, "127.0.0.1", qtype, timeout=2.5, port=port)


def test_other_questions_go_to_tor_unchanged(tor_dns):
    tor, port = tor_dns
    for qtype in (1, 12, 255):  # A, PTR, ANY: as asked, byte for byte (with EDNS)
        query = rt.dns_query_packet("example.org", qtype, 99)
        query = query[:10] + b"\x00\x01" + query[12:] + b"\x00\x00\x29\x04\xd0\x00\x00\x00\x00\x00\x00"
        answer = raw(port, query)
        assert answer[:2] == query[:2] and question(answer)[1] == qtype
    assert [q for _, q in tor.asked] == [1, 12, 255]


def test_a_query_the_front_cannot_read_goes_to_tor_as_it_is():
    """Two questions, a short packet, an answer instead of a question: tor decides (FORMERR, or its first one)."""
    asked = []
    front = rt.TorDnsFront(ask=lambda query: asked.append(query) or None)
    two = rt.dns_query_packet("a.onion", 28, 5)
    two = two[:4] + b"\x00\x02" + two[6:] + two[12:]
    for query in (two, b"\x00\x05\x01", reply(rt.dns_query_packet("a.onion", 28, 5))):
        assert front.answer(query) is None
    assert asked == [two, reply(rt.dns_query_packet("a.onion", 28, 5))]  # (3 bytes: no question at all, dropped)


def test_the_front_keeps_serving_after_a_failing_send_and_at_its_limit(tor_dns):
    tor, _port = tor_dns
    front = rt.TorDnsFront(address="127.0.0.1", port=0, tor=tor.address, wait=1.0, limit=1)
    gate = threading.Event()
    front.answer = lambda query: gate.wait(5) and None  # the one slot busy
    front.start()
    try:
        port = front.sockets[0].getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for ident in range(5):  # beyond the slot: dropped, as a resolver under load drops
                sock.sendto(rt.dns_query_packet(ONION, 1, ident), ("127.0.0.1", port))
        time.sleep(0.3)
        gate.set()
        del front.answer
        time.sleep(0.3)
        assert rt.dns_lookup(ONION, "127.0.0.1", 28, port=port) == []
    finally:
        front.close()


# == the Tor container ==============================================================================================

def test_tor_comes_up_with_the_front_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN", tmp_path / "vpn")
    monkeypatch.setattr(rt, "own_data_directory", lambda uid: None)
    monkeypatch.setattr(rt, "wait_bootstrap", lambda *a, **kw: 100)
    order = []

    class Runner:
        children = []

        def run(self, command, data=None, check=True):
            order.append(command[0])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        def spawn(self, command, log):
            order.append(command[0])
            return types.SimpleNamespace(args=command, poll=lambda: None, returncode=None)

    front = types.SimpleNamespace(start=lambda: order.append("front"))
    tunnel = rt.up_tor({"profile": {"name": "t", "type": "tor", "tor": {}}}, Runner(), uid_of=lambda: rt.TOR_UID,
                       front=front)
    # The kill switch first, then the front (on tor's address and port 53), then tor.
    assert order.index("nft") < order.index("front") < order.index("tor")
    assert tunnel["dns"] == [rt.TOR_DNS] and tunnel["ipv6"] is False
    # By default the front is the real one, on tor's address and port 53, asking tor's DNSPort.
    made = rt.TorDnsFront()
    assert (made.address, made.port, made.tor) == (rt.TOR_DNS, 53, (rt.TOR_DNS, rt.TOR_DNS_PORT))


def test_the_fronts_sockets_carry_the_checks_mark(tor_dns, monkeypatch):
    """The agent's firewall (and `sandbox-ai net cut`) judged the question at TOR_DNS; the question to tor and the
    answer carry CHECK_MARK, as tor's own packets passed by its uid when it answered there itself."""
    tor, _port = tor_dns
    made = []
    real = rt.check_socket
    monkeypatch.setattr(rt, "check_socket", lambda family, kind: made.append((family, kind)) or real(family, kind))
    front = rt.TorDnsFront(address="127.0.0.1", port=0, tor=tor.address, wait=1.0).start()
    try:
        assert made == [(socket.AF_INET, socket.SOCK_DGRAM)]  # the front's own socket: its answers
        assert rt.dns_lookup(ONION, "127.0.0.1", 28, port=front.sockets[0].getsockname()[1]) == []
        assert made[1:] == [(socket.AF_INET, socket.SOCK_DGRAM)] * 2  # (dns_lookup's, then the question to tor)
    finally:
        front.close()
