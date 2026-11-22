"""MCP servers with network reach the internet, not your network (the MCP review, group B, 2026-11-17: playwright,
whatweb, web-fetch and the "allowlist" servers ran with --network full and could reach 169.254.169.254, the LAN and
this host).

Held here, without Podman or a kernel:
  - the rules (vpn_runtime.egress_rules) judged by an oracle written here - a small interpreter of the nft lines they
    are made of, not the code under test: the metadata address, RFC 1918, carrier-grade NAT, loopback, link-local,
    IPv6 unique-local and link-local, multicast and this host's own addresses refused; a public address reached; an
    allowlist server reaches its pinned hosts on their ports and nothing else (no DNS); --network lan reaches your
    network but never the metadata service; --allow-private and --allow-tcp open exactly what they name; behind a VPN
    the lookups pass to be redirected, and the VPN's own packets pass;
  - DNS answers that point into your network (rebinding) are never pinned, and a host with nothing else is refused;
  - the guard of each connection without a VPN (mcp_egress.guard) with a fake Podman: created, initialised, the rules
    loaded into its namespace and listed back, started only then - anything else removes it unstarted;
  - Bubblewrap: the sandbox waits on its gate until slirp4netns and the rules are in place;
  - behind a VPN: the VPN container loads the table (and pins the hosts it resolved through the tunnel), each
    connection checks it.
The kernel's part (nft in a real namespace, Podman's init, pasta) needs a live test with real Podman."""
import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SANDBOX / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_vpn_runtime_egress_test", "vpn_runtime.py")
eg = load("sandbox_mcp_egress_test", "mcp_egress.py")
PASTA = "pasta:--no-map-gw,-t,none,-u,none,-T,none,-U,none"


# --- the oracle: what an nft output chain of these lines does with one packet ----------------------------------------

def parse_set(text):
    text = text.strip()
    items = [part.strip() for part in text[1:-1].split(",")] if text.startswith("{") else [text]
    return [ipaddress.ip_network(item, strict=False) for item in items if item]


def verdict(rules, address, proto="tcp", port=443, mark=0, uid=1000, icmp=None, hoplimit=64):
    """accept or reject: the output chain of RULES (one table) for a packet to ADDRESS:PORT over PROTO (icmpv6:
    ICMP its type's nft name, HOPLIMIT its hop limit), as nft would judge it - each line understood here or the
    test fails."""
    ip = ipaddress.ip_address(address)
    loopback = ip.is_loopback
    chain = re.search(r"chain output \{ type filter hook output priority (-?\d+); policy (accept|drop);\n(.*?)\n }",
                      rules, re.S)
    assert chain, rules
    assert int(chain[1]) < -100, "the rules must judge the destination before any NAT redirect"
    for line in chain[3].splitlines():
        line = line.strip()
        action = "accept" if line.endswith("accept") else "reject" if "reject" in line else None
        assert action, line
        condition = line[:-len("accept")].strip() if action == "accept" else line[:line.index("reject")].strip()
        matched = True
        rest = condition
        while rest:
            if rest.startswith('oifname "lo"'):
                matched &= loopback
                rest = rest[len('oifname "lo"'):].strip()
            elif rest.startswith("meta mark != 0x0"):
                matched &= mark != 0
                rest = rest[len("meta mark != 0x0"):].strip()
            elif rest.startswith("meta skuid "):
                value, _, rest = rest[len("meta skuid "):].partition(" ")
                matched &= uid == int(value)
            elif rest.startswith(("ip daddr ", "ip6 daddr ")):
                family, _, rest = rest.partition(" daddr ")
                if rest.startswith("{"):
                    end = rest.index("}") + 1
                    networks, rest = parse_set(rest[:end]), rest[end:].strip()
                else:
                    value, _, rest = rest.partition(" ")
                    networks = parse_set(value)
                matched &= (ip.version == (4 if family == "ip" else 6)) and any(
                    ip.version == net.version and ip in net for net in networks)
            elif rest.startswith("icmpv6 type {"):
                end = rest.index("}") + 1
                types = [part.strip() for part in rest[len("icmpv6 type {"):end - 1].split(",")]
                matched &= ip.version == 6 and proto == "icmpv6" and icmp in types
                rest = rest[end:].strip()
            elif re.match(r"ip6 hoplimit \d+", rest):
                found = re.match(r"ip6 hoplimit (\d+)", rest)
                matched &= ip.version == 6 and hoplimit == int(found[1])
                rest = rest[found.end():].strip()
            elif re.match(r"(tcp|udp) dport \d+", rest):
                found = re.match(r"(tcp|udp) dport (\d+)", rest)
                matched &= proto == found[1] and port == int(found[2])
                rest = rest[found.end():].strip()
            else:
                raise AssertionError(f"the oracle does not know: {rest!r} (in {line!r})")
        if matched:
            return action
    return chain[2]


PUBLIC = ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "1.1.1.1")
LAN = ("192.168.1.1", "10.0.0.5", "172.16.3.4", "100.64.0.1", "127.0.0.1", "169.254.169.254", "169.254.1.1",
       "fd12:3456::1", "fe80::1", "224.0.0.251", "ff02::1", "::ffff:192.168.1.1", "0.0.0.0", "255.255.255.255",
       "64:ff9b::a00:1")


@pytest.mark.parametrize("address", PUBLIC)
def test_an_internet_server_reaches_a_public_address(address):
    assert verdict(rt.egress_rules("internet"), address) == "accept"


@pytest.mark.parametrize("address", [a for a in LAN if not ipaddress.ip_address(a).is_loopback])
def test_an_internet_server_never_reaches_your_network_nor_the_metadata_service(address):
    rules = rt.egress_rules("internet")
    for proto, port in (("tcp", 80), ("tcp", 443), ("udp", 53), ("udp", 5353), ("tcp", 22)):
        assert verdict(rules, address, proto, port) == "reject", (address, proto, port)


ND = ("nd-neighbor-solicit", "nd-neighbor-advert", "nd-router-solicit")


def test_ipv6_neighbour_discovery_passes_in_every_kind_and_nothing_else_of_the_link():
    """Backlog A (security review 1, finding 7): the rules refuse fe80::/10 and ff00::/8, which IPv6's neighbour
    discovery needs (solicited-node multicast, the router's link-local address) - a server with network would have no
    IPv6 at all. Neighbour and router solicitations and neighbour advertisements now pass, only as the kernel sends
    them (hop limit 255: never routed); every other packet to those networks is refused as before. (A live test of
    IPv6 needs a dual-stack host with nested containers: noted in the review.)"""
    for kind, extra in (("internet", {}), ("lan", {}), ("hosts", {"pins": [("2606:2800:220:1::1", 443)]})):
        rules = rt.egress_rules(kind, **extra)
        for icmp, address in (("nd-neighbor-solicit", "ff02::1:ff00:1"), ("nd-neighbor-solicit", "fe80::1"),
                              ("nd-neighbor-advert", "fe80::1"), ("nd-router-solicit", "ff02::2")):
            assert verdict(rules, address, "icmpv6", 0, icmp=icmp, hoplimit=255) == "accept", (kind, icmp)
            assert verdict(rules, address, "icmpv6", 0, icmp=icmp, hoplimit=64) == "reject" or kind == "lan", \
                (kind, icmp, "a routed one is no neighbour discovery")
        if kind != "lan":
            assert verdict(rules, "fe80::1", "icmpv6", 0, icmp="echo-request", hoplimit=255) == "reject"
            assert verdict(rules, "fe80::1", "tcp", 22) == "reject" and verdict(rules, "ff02::1", "udp", 5353) == \
                "reject"
        assert verdict(rules, "192.168.1.1", "icmpv6", 0, icmp="nd-neighbor-solicit", hoplimit=255) == "reject" \
            or kind == "lan"
    assert rt.egress_problem("table inet " + rt.EGRESS_TABLE + "\n" + rt.egress_rules("internet")) is None


def test_the_metadata_address_is_refused_in_every_kind():
    for kind, extra in (("internet", {}), ("lan", {}), ("hosts", {"pins": [("93.184.216.34", 443)]})):
        rules = rt.egress_rules(kind, **extra)
        assert verdict(rules, "169.254.169.254", "tcp", 80) == "reject", kind
        assert verdict(rules, "fd00:ec2::254", "tcp", 80) == "reject", kind


def test_this_hosts_own_addresses_are_refused():
    rules = rt.egress_rules("internet", own=["203.0.113.7", "198.51.99.4", "2a01:4f8::5", "192.168.1.20"])
    assert verdict(rules, "198.51.99.4", "tcp", 8080) == "reject"
    assert verdict(rules, "2a01:4f8::5", "tcp", 22) == "reject"
    assert verdict(rules, "198.51.99.5", "tcp", 443) == "accept"
    assert "192.168.1.20" not in rules, "an own address inside a refused network is merged into it (nft: no overlap)"


def test_dns_reaches_the_namespaces_own_resolver_on_port_53_only():
    rules = rt.egress_rules("internet", resolvers=["169.254.1.1", "192.168.1.1"])
    for resolver in ("169.254.1.1", "192.168.1.1"):
        assert verdict(rules, resolver, "udp", 53) == "accept"
        assert verdict(rules, resolver, "tcp", 53) == "accept"
        assert verdict(rules, resolver, "tcp", 80) == "reject"
    assert verdict(rules, "192.168.1.2", "udp", 53) == "reject"


def test_an_allowlist_server_reaches_its_hosts_and_nothing_else():
    rules = rt.egress_rules("hosts", pins=[("93.184.216.34", 443), ("2606:2800:220:1::1", 443)],
                            resolvers=["192.168.1.1"])
    assert verdict(rules, "93.184.216.34", "tcp", 443) == "accept"
    assert verdict(rules, "2606:2800:220:1::1", "tcp", 443) == "accept"
    assert verdict(rules, "93.184.216.34", "tcp", 80) == "reject", "only the port its entry names"
    assert verdict(rules, "1.1.1.1", "tcp", 443) == "reject", "another host is refused"
    assert verdict(rules, "192.168.1.1", "udp", 53) == "reject", "no DNS of its own: its names are in /etc/hosts"
    assert verdict(rules, "8.8.8.8", "udp", 53) == "reject"


def test_a_private_address_is_never_pinned():
    with pytest.raises(ValueError, match="no public address"):
        rt.egress_rules("hosts", pins=[("10.0.0.5", 443)])


def test_the_lan_opt_in_reaches_your_network_but_not_the_metadata_service():
    rules = rt.egress_rules("lan")
    for address in ("192.168.1.1", "10.0.0.5", "fd12:3456::1", "93.184.216.34"):
        assert verdict(rules, address, "tcp", 8080) == "accept", address
    for address in ("169.254.169.254", "169.254.170.2", "100.100.100.200"):
        assert verdict(rules, address, "tcp", 80) == "reject", address


def test_allow_private_and_allow_tcp_open_exactly_what_they_name():
    rules = rt.egress_rules("internet", allow_private=["192.168.50.0/24"], allow_tcp=["10.0.0.7:8080",
                                                                                      "[fd00::7]:9000"])
    assert verdict(rules, "192.168.50.20", "tcp", 22) == "accept"
    assert verdict(rules, "192.168.51.20", "tcp", 22) == "reject"
    assert verdict(rules, "10.0.0.7", "tcp", 8080) == "accept"
    assert verdict(rules, "10.0.0.7", "tcp", 8081) == "reject"
    assert verdict(rules, "fd00::7", "tcp", 9000) == "accept"
    assert verdict(rules, "169.254.169.254", "tcp", 80) == "reject"
    hosts = rt.egress_rules("hosts", pins=[("93.184.216.34", 443)], allow_tcp=["192.168.1.30:8888"])
    assert verdict(hosts, "192.168.1.30", "tcp", 8888) == "accept"
    assert verdict(hosts, "192.168.1.31", "tcp", 8888) == "reject"


def test_behind_a_vpn_lookups_pass_to_their_redirect_and_the_vpns_own_packets_pass():
    rules = rt.egress_rules("internet", dns="redirected", exempt_uids=[rt.TOR_UID])
    assert verdict(rules, "192.168.1.1", "udp", 53) == "accept", "redirected to the tunnel's resolver after this"
    assert verdict(rules, "10.64.0.1", "tcp", 80) == "reject"
    assert verdict(rules, "10.64.0.1", "udp", 51820, mark=rt.MARK) == "accept", "the tunnel's own packets"
    assert verdict(rules, "192.168.1.1", "tcp", 9001, uid=rt.TOR_UID) == "accept", "tor's own"
    hosts = rt.egress_rules("hosts", pins=[("93.184.216.34", 443)], dns="redirected")
    assert verdict(hosts, "1.1.1.1", "udp", 53) == "reject", "an allowlist server has no DNS behind a VPN either"


def test_rules_that_cannot_be_written_are_refused():
    for bad in (lambda: rt.egress_rules("everything"), lambda: rt.egress_rules("internet", dns="maybe"),
                lambda: rt.egress_rules("internet", allow_tcp=["host:80"]),
                lambda: rt.egress_rules("internet", allow_tcp=["10.0.0.1:0"]),
                lambda: rt.egress_rules("internet", allow_private=["not a network"])):
        with pytest.raises(ValueError):
            bad()


def test_the_check_of_a_listing():
    assert rt.egress_problem("", "internet")
    assert rt.egress_problem("table inet other {}", "internet")
    listing = rt.egress_rules("internet").replace("priority -150", "priority mangle")
    assert rt.egress_problem(listing, "internet") is None
    assert rt.egress_problem(rt.egress_rules("lan"), "internet"), "a lan table is not an internet server's"
    assert rt.egress_problem(rt.egress_rules("hosts", pins=[("1.1.1.1", 443)]), "hosts") is None
    assert rt.egress_problem(rt.egress_rules("lan"), "lan") is None


# --- pinning: what an allowlist server's names point to --------------------------------------------------------------

def test_dns_answers_that_point_into_your_network_are_never_pinned():
    answers = {"api.example.com": ["93.184.216.34", "192.168.1.10", "::1"], "evil.example.com": ["10.0.0.1"]}
    pins, names, dropped = eg.pinned(["api.example.com:443"], lambda name, port: answers[name])
    assert pins == [("93.184.216.34", 443)] and names == {"api.example.com": ["93.184.216.34"]}
    assert dropped == ["api.example.com -> 192.168.1.10", "api.example.com -> ::1"]
    with pytest.raises(ValueError, match="evil.example.com has no public address.*10.0.0.1"):
        eg.pinned(["evil.example.com:443"], lambda name, port: answers[name])
    with pytest.raises(ValueError, match="no public address"):
        eg.pinned(["gone.example.com:443"], lambda name, port: [])


def test_the_hosts_file_names_only_the_pinned_hosts(tmp_path):
    path = eg.write_hosts(tmp_path / "s" / "egress-hosts", {"api.example.com": ["93.184.216.34"]})
    text = path.read_text()
    assert "93.184.216.34\tapi.example.com" in text and "localhost" in text and len(text.splitlines()) == 3
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("bad", ["api.example.com", "localhost:443", "api.example.com:0", "a b.com:443",
                                 "api.example.com:443/x"])
def test_a_host_must_be_name_and_port(bad):
    with pytest.raises(ValueError):
        rt.egress_host(bad)


def test_this_hosts_addresses_are_read_from_the_kernel(tmp_path):
    fib = tmp_path / "fib_trie"
    fib.write_text("Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 192.168.1.20\n        /32 host LOCAL\n"
                   "     |-- 192.168.1.255\n        /32 link BROADCAST\n     |-- 198.51.99.4\n        /32 host LOCAL\n")
    inet6 = tmp_path / "if_inet6"
    inet6.write_text("2a0104f8000000000000000000000005 02 40 00 80 eth0\n"
                     "00000000000000000000000000000001 01 80 10 80 lo\n")
    assert eg.host_addresses(fib, inet6) == ["192.168.1.20", "198.51.99.4", "::1", "2a01:4f8::5"]
    assert eg.host_addresses(tmp_path / "none", tmp_path / "none") == []


def test_the_resolvers_of_a_resolv_conf():
    assert eg.resolvers("# x\nnameserver 169.254.1.1\nnameserver fe80::1%eth0\nsearch lan\nnameserver bad\n") == \
        ["169.254.1.1", "fe80::1"]


def test_a_host_without_nft_starts_no_networked_server():
    assert eg.prerequisite_problem("podman", which=lambda tool: None).startswith("nft and nsenter are not")
    assert "nft is not" in eg.prerequisite_problem("bwrap", which=lambda tool: None)
    assert eg.prerequisite_problem("podman", which=lambda tool: "/usr/bin/" + tool) is None


# --- the guard of each connection (Podman, no VPN) -------------------------------------------------------------------

SERVER_ID = "4" * 64


class FakePodman:
    """podman create|init|inspect|unshare nsenter nft|start|rm, recorded; nft keeps what was loaded."""

    def __init__(self, mode="pasta", key="/run/user/1000/netns/netns-1", listing=None, fail=None):
        self.calls, self.loaded, self.mode, self.key = [], None, mode, key
        self.listing, self.fail = listing, fail or {}

    def run(self, command, input=None, **options):
        args = command[1:]
        self.calls.append(args)
        out, code = "", 0
        if args[:1] == ["create"]:
            out = SERVER_ID + "\n"
        elif args[:2] == ["container", "inspect"]:
            out = f"{self.mode}|{self.key}|/r/resolv.conf|false\n"
        elif args[:1] == ["unshare"] and args[-2:] == ["-f", "-"]:
            self.loaded = input
        elif args[:1] == ["unshare"] and "list" in args:
            out = self.listing if self.listing is not None else (self.loaded or "")
        for word, status in self.fail.items():
            if word in args[:3]:
                code = status
        return subprocess.CompletedProcess(command, code, out, "failed" if code else "")

    def popen(self, command):
        self.calls.append(command[1:])
        return types.SimpleNamespace(wait=lambda: 0, poll=lambda: 0, send_signal=lambda signum: None)

    def words(self):
        return [call[0] if call[0] != "container" else "inspect" for call in self.calls]


def run_line(*extra, network=PASTA):
    return ["podman", "run", "--rm", "-i", f"--network={network}", *extra, "--cidfile=/s/x.cid",
            "ghcr.io/x/web@sha256:" + "a" * 64]


def guarded(fake, config, command, lookup=lambda name, port: ["93.184.216.34"]):
    return eg.guard({"network": PASTA, **config}, command, run=fake.run, popen=fake.popen, own=["198.51.99.4"],
                    lookup=lookup, read=lambda path: "nameserver 169.254.1.1\n")


def test_each_connection_gets_the_rules_before_the_server_runs():
    fake = FakePodman()
    assert guarded(fake, {"kind": "internet"}, run_line()) == 0
    assert fake.words() == ["create", "init", "inspect", "unshare", "unshare", "start", "rm"]
    assert fake.calls[3][:3] == ["unshare", "nsenter", "--net=/run/user/1000/netns/netns-1"]
    assert fake.calls[5] == ["start", "--attach", "--interactive", SERVER_ID]
    assert verdict(fake.loaded, "169.254.169.254", "tcp", 80) == "reject"
    assert verdict(fake.loaded, "192.168.1.1", "tcp", 80) == "reject"
    assert verdict(fake.loaded, "198.51.99.4", "tcp", 80) == "reject", "this host's own address"
    assert verdict(fake.loaded, "169.254.1.1", "udp", 53) == "accept", "pasta's resolver"
    assert verdict(fake.loaded, "93.184.216.34", "tcp", 443) == "accept"


@pytest.mark.parametrize("fake, why", [
    (FakePodman(mode="host"), "not as created"),
    (FakePodman(key=""), "not as created"),
    (FakePodman(listing=""), "not loaded"),
    (FakePodman(listing="table inet sandbox_ai_mcp_egress {\n chain output { type filter hook output priority "
                        "-150; policy accept;\n }\n}\n"), "do not refuse your local network"),
    (FakePodman(fail={"init": 125}), "podman init failed"),
    (FakePodman(fail={"nsenter": 1}), "loading the egress rules failed"),
])
def test_a_connection_whose_rules_are_not_in_place_never_runs(fake, why):
    with pytest.raises(ValueError, match=why):
        guarded(fake, {"kind": "internet"}, run_line())
    assert "start" not in fake.words() and fake.words()[-1] == "rm", "removed unstarted"


@pytest.mark.parametrize("command, why", [
    (run_line("--network=host"), "exactly --network="),
    (run_line(network="host"), "exactly --network="),
    (run_line("--add-host=x:1.2.3.4"), "exactly --network="),
    (run_line("--publish=8080:80"), "exactly --network="),
    (["podman", "run", "--rm", "ghcr.io/x/web@sha256:" + "a" * 64], "exactly one"),
    (["docker", "run", f"--network={PASTA}", "x/y"], "podman run line"),
])
def test_a_run_line_with_another_network_is_refused_before_anything_is_made(command, why):
    fake = FakePodman()
    with pytest.raises(ValueError, match=why):
        guarded(fake, {"kind": "internet"}, command)
    assert fake.calls == []


def test_an_allowlist_connection_gets_its_hosts_pinned_and_its_hosts_file(tmp_path):
    fake = FakePodman()
    hosts_file = tmp_path / "egress-hosts"
    config = {"kind": "hosts", "hosts": ["api.example.com:443"], "hosts_file": str(hosts_file)}
    with pytest.raises(ValueError, match="must mount its /etc/hosts"):
        guarded(fake, config, run_line())
    command = run_line(f"--volume={hosts_file}:/etc/hosts:ro")
    assert guarded(fake, config, command, lookup=lambda name, port: ["93.184.216.34", "10.0.0.9"]) == 0
    assert verdict(fake.loaded, "93.184.216.34", "tcp", 443) == "accept"
    assert verdict(fake.loaded, "10.0.0.9", "tcp", 443) == "reject", "a rebinding answer is not pinned"
    assert verdict(fake.loaded, "1.1.1.1", "tcp", 443) == "reject"
    assert verdict(fake.loaded, "169.254.1.1", "udp", 53) == "reject"
    assert "93.184.216.34\tapi.example.com" in hosts_file.read_text() and "10.0.0.9" not in hosts_file.read_text()


def test_the_guard_says_why_and_fails_as_podman_does(capsys, monkeypatch):
    config = eg.encoded({"kind": "internet", "network": PASTA})
    monkeypatch.setattr(eg, "guard", lambda config, command: (_ for _ in ()).throw(ValueError("no rules")))
    assert eg.main(["--internal-egress-guard", config, "--", "podman", "run", "x"]) == 125
    assert "MCP server refused: no rules" in capsys.readouterr().err
    with pytest.raises(ValueError):
        eg.decoded(base64.urlsafe_b64encode(b'{"kind": "all"}').decode())


# --- mcp.py: definitions, the plan, each connection's command -------------------------------------------------------

m = load("sandbox_mcp_for_egress_test", "mcp.py")
l = m.launcher()
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/x/web@{DIGEST}"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    return tmp_path


def test_the_networks_a_definition_may_name(home):
    for network in ("none", "full", "lan"):
        assert m.validated_image({"image": IMAGE, "network": network}, "t")["network"] == network
    hosts = {"image": IMAGE, "network": "allowlist", "hosts": ["api.example.com:443"]}
    assert m.validated_image(hosts, "t")["hosts"] == ["api.example.com:443"]
    opened = {"image": IMAGE, "network": "full", "allow_private": ["192.168.50.0/24"], "allow_tcp": ["10.0.0.7:8080"]}
    assert m.validated_image(opened, "t") == opened
    for bad, why in (({"network": "everything"}, "network must be"), ({"network": "allowlist"}, "needs its hosts"),
                     ({"network": "allowlist", "hosts": ["nohost"]}, "NAME:PORT"),
                     ({"network": "full", "hosts": ["a.example.com:443"]}, "only an allowlist server"),
                     ({"network": "none", "allow_private": ["10.0.0.0/8"]}, "without network"),
                     ({"network": "full", "allow_private": ["nonsense"]}, "allow_private"),
                     ({"network": "full", "allow_tcp": ["10.0.0.7"]}, "allow_tcp"),
                     ({"network": "full", "allow_private": ["0.0.0.0/0"]}, "allow_private")):
        with pytest.raises(ValueError, match=why):
            m.validated_image({"image": IMAGE, **bad}, "t")
    vpn = m.validated_image({**hosts, "vpn": "corp"}, "t")
    assert vpn["vpn"] == "corp", "an allowlist server may have a VPN too"


def test_the_plan_of_a_server_with_network_carries_its_egress(home):
    full = m.server_network("web", {"image": IMAGE, "network": "full", "allow_tcp": ["10.0.0.7:8080"]})
    assert full["egress"] == {"kind": "internet", "hosts": [], "allow_private": [], "allow_tcp": ["10.0.0.7:8080"],
                              "network": l.POD_NETWORK}
    hosts = m.server_network("ddg", {"image": IMAGE, "network": "allowlist", "hosts": ["html.duckduckgo.com:443"]})
    assert hosts["egress"]["kind"] == "hosts" and hosts["egress"]["hosts"] == ["html.duckduckgo.com:443"]
    assert hosts["egress"]["hosts_file"] == str(m.state_dir("ddg") / "egress-hosts")
    assert m.server_network("lan", {"image": IMAGE, "network": "lan"})["egress"]["kind"] == "lan"
    assert m.server_network("bw", {"image": IMAGE, "network": "none"})["egress"] is None
    assert m.server_network("demo", m.BUILTIN["demo"])["egress"] is None, "a command server: its own sandbox's rules"


def test_each_connection_of_a_server_with_network_runs_through_the_guard(home):
    plan = m.server_network("web", {"image": IMAGE, "network": "full"})
    command = m.connection_command(plan)
    assert command[:4] == [sys.executable, "-I", str(SANDBOX / "mcp_egress.py"), "--internal-egress-guard"]
    assert eg.decoded(command[4]) == plan["egress"] and command[5] == "--"
    assert command[6:8] == ["podman", "run"] and f"--network={l.POD_NETWORK}" in command
    # The guard takes the run line as it is built (one network option, nothing that widens it).
    assert eg.run_line(command[6:], l.POD_NETWORK)[2] == IMAGE
    none = m.connection_command(m.server_network("bw", {"image": IMAGE, "network": "none"}))
    assert none[:2] == ["podman", "run"] and "--network=none" in none, "without network: no guard needed"


def test_an_allowlist_servers_connections_mount_its_hosts_file(home):
    plan = m.server_network("ddg", {"image": IMAGE, "network": "allowlist", "hosts": ["html.duckduckgo.com:443"]})
    command = m.connection_command(plan)
    assert f"--volume={m.state_dir('ddg') / 'egress-hosts'}:/etc/hosts:ro" in command


def test_a_plan_whose_egress_was_taken_away_builds_no_run_line(home):
    plan = m.server_network("web", {"image": IMAGE, "network": "full"})
    with pytest.raises(ValueError, match="network plan"):
        m.connection_command({**plan, "egress": None})
    with pytest.raises(ValueError, match="network plan"):
        m.connection_command({**plan, "egress": {**plan["egress"], "kind": "lan"}})


ALLOWLIST = {"id": "mcp:duckduckgo", "name": "duckduckgo", "title": "DuckDuckGo search", "kind": "oci",
             "image": "ghcr.io/c0decave/sandbox-ai-mcp-duckduckgo", "digest": DIGEST, "network": "allowlist",
             "hosts": ["html.duckduckgo.com:443"]}
FULL = {"id": "mcp:web-fetch", "name": "web-fetch", "title": "Fetch", "kind": "oci",
        "image": "ghcr.io/c0decave/sandbox-ai-mcp-web-fetch", "digest": DIGEST, "network": "full"}


def test_an_allowlist_entry_is_added_with_only_its_hosts(home):
    definition = m.definition_from_entry(ALLOWLIST)
    assert definition["network"] == "allowlist" and definition["hosts"] == ["html.duckduckgo.com:443"]
    wide = m.definition_from_entry(ALLOWLIST, network="full")
    assert wide["network"] == "full" and "hosts" not in wide
    assert m.definition_from_entry(FULL)["network"] == "full"
    assert m.definition_from_entry(FULL, network="lan")["network"] == "lan"
    with pytest.raises(ValueError, match="has no hosts"):
        m.definition_from_entry(FULL, network="allowlist")
    opened = m.definition_from_entry(FULL, allow_private=["192.168.1.0/24"], allow_tcp=["10.0.0.7:8080"])
    assert opened["allow_private"] == ["192.168.1.0/24"] and opened["allow_tcp"] == ["10.0.0.7:8080"]


def test_what_add_says_of_each_network(home, monkeypatch):
    path = home / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [ALLOWLIST, FULL]}))
    sources = [("t", str(path))]
    said = []
    m.add("duckduckgo", catalogs=sources, pull=False, out=said.append, unsigned=True)
    assert any("only html.duckduckgo.com:443" in line for line in said), said
    said.clear()
    m.add("web-fetch", catalogs=sources, pull=False, out=said.append, unsigned=True)
    assert any("the internet, not your local network" in line for line in said), said
    said.clear()
    m.add("web-fetch", local="lanfetch", network="lan", catalogs=sources, pull=False, out=said.append, unsigned=True)
    assert any(line.startswith("sandbox-ai mcp: warning:") and "your local network" in line for line in said), said
    said.clear()
    m.add("web-fetch", local="opened", allow_tcp=["192.168.1.30:8888"], catalogs=sources, pull=False,
          out=said.append, unsigned=True)
    assert any("warning" in line and "192.168.1.30:8888" in line for line in said), said


def test_set_switches_a_full_server_to_its_declared_hosts(home):
    path = home / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [ALLOWLIST]}))
    m.write_definition("duckduckgo", {**m.definition_from_entry(ALLOWLIST, network="full"), "catalog": str(path)})
    old, new = m.set_definition("duckduckgo", {"network": "allowlist"}, entry=ALLOWLIST)
    assert old["network"] == "full" and new["network"] == "allowlist" and new["hosts"] == ["html.duckduckgo.com:443"]
    _, back = m.set_definition("duckduckgo", {"network": "full"}, entry=ALLOWLIST)
    assert "hosts" not in back
    _, opened = m.set_definition("duckduckgo", {"allow_private": ["192.168.50.0/24"], "allow_tcp": []})
    assert opened["allow_private"] == ["192.168.50.0/24"] and "allow_tcp" not in opened
    with pytest.raises(ValueError, match="without network"):
        m.set_definition("duckduckgo", {"network": "none", "allow_private": ["10.0.0.0/8"]})


def test_a_full_server_of_an_allowlist_entry_is_told_to_switch(home):
    note = m.egress_note("duckduckgo", {"image": IMAGE, "network": "full", "source": "mcp:duckduckgo"},
                         entry=ALLOWLIST)
    assert "sandbox-ai mcp set duckduckgo --network allowlist" in note and "html.duckduckgo.com:443" in note
    assert m.egress_note("web", {"image": IMAGE, "network": "full"}, entry=FULL) is None
    assert m.egress_note("x", {"image": IMAGE, "network": "lan"}).startswith("MCP server x reaches your local network")


def test_the_network_shown_for_people():
    assert m.network_label({"image": IMAGE, "network": "full"}) == "internet (no LAN)"
    assert m.network_label({"image": IMAGE, "network": "allowlist", "hosts": ["a.example.com:443"]}) == \
        "only its hosts (a.example.com:443)"
    assert m.network_label({"image": IMAGE, "network": "lan"}) == "internet + LAN"
    assert m.network_label({"image": IMAGE}) == "none"
    assert m.network_label({"image": IMAGE, "network": "full", "allow_tcp": ["10.0.0.7:80"]}) == \
        "internet (no LAN) + 10.0.0.7:80"


# --- Bubblewrap: the sandbox waits until its rules are in place ------------------------------------------------------

rootfs = load("sandbox_mcp_rootfs_for_egress_test", "mcp_rootfs.py")


def rootfs_definition(tmp_path, network="full", **egress):
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    definition = {"rootfs": {"path": str(tree), "sha256": "e" * 64, "entrypoint": ["/usr/bin/server"],
                             "user": 10001, "workdir": "/tmp", "env": {}}, "network": network}
    config = eg.egress_config({"network": network, **egress}, tmp_path / "egress-hosts")
    return {**definition, **egress, "egress": config}


class FakeBwrap:
    """bwrap and slirp4netns as Popen sees them: bwrap says its child's PID on --info-fd and keeps a copy of its
    --block-fd (the gate: has it been opened?); slirp4netns says it is ready."""

    def __init__(self, monkeypatch, fail_load=None, slirp_ready=True):
        self.events, self.gate, self.loaded = [], None, None
        self.killed = False
        fake = self
        # (PID 4242 does not exist: slirp4netns is handed it as before, the namespace lookup is tested on its own)
        monkeypatch.setattr(rootfs, "slirp_target", lambda pid: ([str(pid)], ()))

        class Process:
            def __init__(self, argv):
                self.args = argv

            def wait(self):
                if self.args[0] == "bwrap":
                    fake.events.append("wait")
                return 0

            def kill(self):
                fake.killed = True
                fake.events.append("kill")

            def terminate(self):
                pass

        def popen(argv, **options):
            if argv[0] == "bwrap":
                if fake.gate is not None:
                    os.close(fake.gate)
                fake.argv = argv
                fake.args = os.read(int(argv[argv.index("--args") + 1]), 65536).split(b"\0")
                # bwrap's own form: pretty-printed over several lines (the CI of sandbox-ai-mcp 2026-11-21: only
                # the first line was read, "{", so every Bubblewrap server with network ran without it)
                os.write(int(argv[argv.index("--info-fd") + 1]),
                         b'{\n    "child-pid": 4242,\n    "cgroup-namespace": 4026532835,\n'
                         b'    "ipc-namespace": 4026532833\n}\n')
                fake.gate = os.dup(int(argv[argv.index("--block-fd") + 1]))
                fake.events.append("bwrap")
            else:
                assert argv[0] == "slirp4netns" and "4242" in argv
                if slirp_ready:
                    os.write(int(argv[argv.index("--ready-fd") + 1]), b"1")
                fake.events.append("slirp4netns")
            return Process(argv)

        def load(pid, rules, kind, **options):
            import select
            assert select.select([fake.gate], [], [], 0)[0] == [], "the server's gate is opened only after this"
            fake.events.append("rules")
            fake.loaded = (pid, rules, kind)
            if fail_load:
                raise ValueError(fail_load)
        monkeypatch.setattr(rootfs.subprocess, "Popen", popen)
        monkeypatch.setattr(rootfs, "egress_module", lambda: types.SimpleNamespace(
            **{name: getattr(eg, name) for name in ("pins_of", "rules_for", "write_hosts", "runtime")},
            load_into_sandbox=load))

    def close(self):
        if self.gate is not None:
            os.close(self.gate)
            self.gate = None

    def opened(self):
        """The gate was opened: bwrap read a byte (a gate only closed - the launcher gone - is EOF, which bwrap
        takes as no start: --block-fd reads until data or the end)."""
        import select
        return bool(select.select([self.gate], [], [], 0)[0]) and os.read(self.gate, 1) == b"1"


@pytest.fixture()
def fakes():
    """FakeBwrap's made in a test, their copy of the gate closed after it."""
    made = []
    yield made
    for fake in made:
        fake.close()


def test_a_bwrap_server_runs_only_once_its_rules_are_loaded(tmp_path, monkeypatch, fakes):
    fake = FakeBwrap(monkeypatch)
    fakes.append(fake)
    assert rootfs.run(rootfs_definition(tmp_path), {}) == 0
    assert fake.events == ["bwrap", "slirp4netns", "rules", "wait"] and fake.opened()
    pid, rules, kind = fake.loaded
    assert pid == 4242 and kind == "internet"
    assert verdict(rules, "169.254.169.254", "tcp", 80) == "reject"
    assert verdict(rules, "10.0.2.2", "tcp", 80) == "reject", "slirp4netns's way to this host"
    assert verdict(rules, "10.0.2.3", "udp", 53) == "accept", "slirp4netns's resolver"
    assert verdict(rules, "93.184.216.34", "tcp", 443) == "accept"


def test_a_bwrap_server_whose_rules_fail_never_runs(tmp_path, monkeypatch, capsys, fakes):
    fake = FakeBwrap(monkeypatch, fail_load="nft did not load")
    fakes.append(fake)
    assert rootfs.run(rootfs_definition(tmp_path), {}) == 125
    assert fake.events == ["bwrap", "slirp4netns", "rules", "kill", "wait"] and not fake.opened(), \
        "killed (and reaped) without its gate ever opened"
    assert "nft did not load" in capsys.readouterr().err


def test_a_bwrap_server_with_network_but_no_egress_never_starts(tmp_path, monkeypatch, capsys, fakes):
    fake = FakeBwrap(monkeypatch)
    fakes.append(fake)
    definition = {**rootfs_definition(tmp_path), "egress": None}
    assert rootfs.run(definition, {}) == 125 and fake.events == []
    assert "egress" in capsys.readouterr().err


def test_a_bwrap_allowlist_server_gets_its_pinned_hosts(tmp_path, monkeypatch, fakes):
    fake = FakeBwrap(monkeypatch)
    fakes.append(fake)
    monkeypatch.setattr(eg, "system_lookup", lambda name, port: ["93.184.216.34", "192.168.1.9"])
    definition = rootfs_definition(tmp_path, "allowlist", hosts=["api.example.com:443"])
    definition["egress"] = eg.egress_config(definition, tmp_path / "egress-hosts")
    assert rootfs.run(definition, {}) == 0
    _, rules, kind = fake.loaded
    assert kind == "hosts" and verdict(rules, "93.184.216.34", "tcp", 443) == "accept"
    assert verdict(rules, "192.168.1.9", "tcp", 443) == "reject"
    args = fake.args
    at = args.index(b"/etc/hosts")
    assert args[at - 2:at] == [b"--ro-bind", str(tmp_path / "egress-hosts").encode()]
    assert "93.184.216.34\tapi.example.com" in (tmp_path / "egress-hosts").read_text()


# --- behind a VPN or Tor: the VPN container loads the rules, each connection checks them -----------------------------

class VpnRunner:
    """The VPN container's commands, recorded; nft keeps what was loaded and lists it back."""

    def __init__(self):
        self.commands, self.children, self.tables = [], [], {}

    def run(self, command, data=None, check=True):
        self.commands.append((command, data))
        out = ""
        if command[:2] == ["nft", "-f"] and data and rt.EGRESS_TABLE in data:
            self.tables[rt.EGRESS_TABLE] = data
        if command[:3] == ["nft", "list", "table"]:
            out = self.tables.get(command[-1], "")
        return types.SimpleNamespace(returncode=0 if out or command[:3] != ["nft", "list", "table"] else 1,
                                     stdout=out, stderr="")


@pytest.mark.parametrize("kind", ["wireguard", "tor"])
def test_the_vpn_container_of_a_server_loads_its_egress_and_pins_its_hosts(monkeypatch, kind):
    tunnel = {"interface": "wg0", "dns": ["10.66.0.1"], "ipv4": True, "tor": kind == "tor"}
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: dict(tunnel))
    monkeypatch.setattr(rt, "up_tor", lambda payload, runner, out=None: dict(tunnel))
    monkeypatch.setattr(rt, "TorDns", lambda **kw: types.SimpleNamespace(lookup=None))
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner, **kw: {"dns": {}})
    monkeypatch.setattr(rt, "resolve_hosts", lambda names, tunnel, tailnet, **kw:
                        {name: ["93.184.216.34", "10.1.2.3"] for name in names})
    payload = {"profile": {"type": kind, "name": "p"}, "secrets": {}, "resolve": ["api.example.com"], "mcp": True,
               "mcp_egress": {"kind": "hosts", "hosts": ["api.example.com:443"], "allow_private": [],
                              "allow_tcp": []}}
    read, write = os.pipe()
    os.write(write, json.dumps(payload).encode() + b"\n")
    os.close(write)
    out, runner = [], VpnRunner()
    with os.fdopen(read) as stdin:
        assert rt.main(["serve"], stdin, types.SimpleNamespace(write=out.append, flush=lambda: None), runner) == 0
    ready = [json.loads(line) for line in out][-1]
    assert ready["event"] == "ready", out
    assert ready["tunnel"]["egress"] == {"kind": "hosts", "pins": {"api.example.com": ["93.184.216.34"]},
                                         "dropped": ["api.example.com -> 10.1.2.3"]}
    rules = runner.tables[rt.EGRESS_TABLE]
    assert verdict(rules, "93.184.216.34", "tcp", 443) == "accept"
    assert verdict(rules, "10.1.2.3", "tcp", 443) == "reject", "a private answer through the tunnel: not pinned"
    assert verdict(rules, "100.64.0.7", "tcp", 443) == "reject"
    assert verdict(rules, "1.1.1.1", "udp", 53) == "reject"
    if kind == "tor":
        assert verdict(rules, "1.1.1.1", "tcp", 9001, uid=rt.TOR_UID) == "accept", "tor's own connections pass"


def test_an_internet_server_behind_a_vpn_never_reaches_the_networks_behind_the_tunnel():
    runner = VpnRunner()
    said = rt.load_mcp_egress({"kind": "internet"}, {}, runner)
    assert said == {"kind": "internet", "pins": {}, "dropped": []}
    rules = runner.tables[rt.EGRESS_TABLE]
    for address in ("100.64.0.7", "fd7a:115c:a1e0::5", "10.64.0.1", "192.168.1.1", "169.254.169.254"):
        assert verdict(rules, address, "tcp", 80) == "reject", address
    assert verdict(rules, "10.64.0.1", "udp", 53) == "accept", "redirected to the tunnel's resolver"
    assert verdict(rules, "93.184.216.34", "tcp", 443) == "accept"
    with pytest.raises(ValueError, match="kind"):
        rt.load_mcp_egress({"kind": "everything"}, {}, runner)


v = load("sandbox_vpn_for_egress_test", "vpn.py")
VPN_ID = "1" * 64


def test_each_connection_behind_a_vpn_checks_the_egress_rules_first(monkeypatch):
    calls = []
    table = {"listing": rt.egress_rules("internet", dns="redirected")}

    def run(command, **options):
        calls.append(command[1:])
        args = command[1:]
        out = ""
        if args[:2] == ["container", "inspect"]:
            out = (f"{VPN_ID}|true|t|pasta|p\n" if VPN_ID in args or "x" not in args[-1] else "")
            if args[-1] == SERVER_ID:
                out = f"container:{VPN_ID}\n" if "NetworkMode" in args[3] else "true\n"
        elif args[:1] == ["exec"] and "sandbox_ai_mcp_egress" in args:
            out = table["listing"]
        elif args[:1] == ["exec"]:
            out = rt.killswitch_rules("wg0")
        elif args[:1] == ["create"]:
            out = SERVER_ID + "\n"
        return subprocess.CompletedProcess(command, 0 if out or args[:1] in (["rm"],) else 1, out, "")
    monkeypatch.setattr(v, "podman_run", run)
    started = []
    popen = lambda command: started.append(command) or types.SimpleNamespace(  # noqa: E731
        poll=lambda: 0, wait=lambda: 0, kill=lambda: None, send_signal=lambda s: None)
    command = ["podman", "run", "--rm", "-i", f"--network=container:sandbox-ai-vpn-mcp-web", "ghcr.io/x/y@" + DIGEST]
    environ = {v.MCP_VPN_ID: VPN_ID, eg.EGRESS_ENV: json.dumps({"kind": "internet"})}
    assert v.mcp_guard("sandbox-ai-vpn-mcp-web", command, environ=environ, run=run, popen=popen) == 0
    assert started and any("sandbox_ai_mcp_egress" in call for call in calls)
    table["listing"] = ""
    started.clear()
    before = len(calls)
    with pytest.raises(ValueError, match="egress rules"):
        v.mcp_guard("sandbox-ai-vpn-mcp-web", command, environ=environ, run=run, popen=popen, say=lambda text: None)
    assert not started and not any(call[:1] == ["create"] for call in calls[before:]), "nothing of it was made"
    with pytest.raises(ValueError, match="not readable"):
        v.mcp_guard("sandbox-ai-vpn-mcp-web", command, environ={**environ, eg.EGRESS_ENV: "{}"}, run=run,
                    popen=popen)


def test_the_wrapper_writes_the_hosts_the_vpn_container_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "check_vpn_egress", lambda podman, ref, kind, run=None: None)
    said = []
    egress = {"kind": "hosts", "hosts": ["api.example.com:443"], "hosts_file": str(tmp_path / "egress-hosts")}
    ready = {"tunnel": {"egress": {"kind": "hosts", "pins": {"api.example.com": ["93.184.216.34"]},
                                   "dropped": ["api.example.com -> 10.1.2.3"]}}}
    v.mcp_egress_ready(["podman"], VPN_ID, egress, ready, said.append)
    assert "93.184.216.34\tapi.example.com" in (tmp_path / "egress-hosts").read_text()
    assert said == ["not pinned (a private address, refused): api.example.com -> 10.1.2.3"]
    with pytest.raises(ValueError, match="did not load"):
        v.mcp_egress_ready(["podman"], VPN_ID, egress, {"tunnel": {}}, said.append)


def test_mcp_list_tells_a_full_server_of_an_allowlist_entry_to_switch(home, capsys):
    path = home / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [ALLOWLIST]}))
    entries, _ = m.catalog_entries([("t", str(path))])  # (read once: remembered, as mcp add or mcp catalog read it)
    m.write_definition("duckduckgo", {**m.definition_from_entry(ALLOWLIST, network="full"), "catalog": str(path)})
    found = [e for e in m.server_list() if e["name"] == "duckduckgo"][0]
    assert found["network"] == "internet (no LAN)"
    assert "sandbox-ai mcp set duckduckgo --network allowlist" in found["egress_note"]
    assert m.cli(["list"]) == 0
    assert "duckduckgo: MCP server duckduckgo reaches the whole internet" in capsys.readouterr().out
    m.write_definition("duckduckgo", {**m.definition_from_entry(ALLOWLIST), "catalog": str(path)})
    found = [e for e in m.server_list() if e["name"] == "duckduckgo"][0]
    assert found["network"] == "only its hosts (html.duckduckgo.com:443)" and found["egress_note"] is None


def test_mcp_add_and_set_take_the_network_options_on_the_command_line(home):
    path = home / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [FULL]}))
    assert m.cli(["add", "web-fetch", "-c", str(path), "--no-pull", "--unsigned", "--allow-tcp", "192.168.1.30:8888",
                  "--network", "full"]) == 0
    assert m.read_config()[1]["web-fetch"]["allow_tcp"] == ["192.168.1.30:8888"]
    assert m.cli(["set", "web-fetch", "--allow-private", "192.168.50.0/24"]) == 0
    assert m.read_config()[1]["web-fetch"]["allow_private"] == ["192.168.50.0/24"]
    assert m.cli(["set", "web-fetch", "--no-allow"]) == 0
    assert not {"allow_private", "allow_tcp"} & set(m.read_config()[1]["web-fetch"])
    assert m.cli(["set", "web-fetch", "--network", "lan"]) == 0
    assert m.read_config()[1]["web-fetch"]["network"] == "lan"
    with pytest.raises(ValueError, match="not both"):
        m.cli(["set", "web-fetch", "--no-allow", "--allow-tcp", "10.0.0.1:80"])


doctor = load("sandbox_doctor_for_egress_test", "doctor.py")


def test_doctor_says_where_the_servers_reach_and_what_to_change():
    servers = [{"name": "ddg", "network": "internet (no LAN)", "egress_note": "MCP server ddg reaches the whole "
                "internet ...: sandbox-ai mcp set ddg --network allowlist keeps it to them"},
               {"name": "bw", "network": "none", "egress_note": None}, {"name": "demo", "network": None}]
    state, title, detail, fix = doctor.check_mcp_network(servers, problem="")
    assert (state, title) == ("warn", "MCP network") and detail == "ddg: internet (no LAN)" and "--network allowlist" in fix
    assert doctor.check_mcp_network(servers[1:], problem="")[0] == "skip"
    assert doctor.check_mcp_network([{**servers[0], "egress_note": None}], problem="")[0] == "ok"
    failed = doctor.check_mcp_network(servers, problem="nft is not on this host")
    assert failed[0] == "fail" and "nftables" in failed[3]
    assert doctor.group_of("MCP network") == "Engine"


def test_mcp_export_says_what_of_your_network_a_server_reaches(home):
    copier = m.copy_module()
    network = copier.network_of({"image": IMAGE, "network": "allowlist", "hosts": ["a.example.com:443"],
                                 "allow_tcp": ["192.168.1.30:8888"], "allow_private": ["10.1.0.0/16"]})
    assert network == {"network": "allowlist", "vpn": None,
                       "options": ["--allow-private 10.1.0.0/16", "--allow-tcp 192.168.1.30:8888"]}


class NamespaceLibc:
    """libc for enter_namespaces: records setns, answers NS_GET_USERNS with OWNER (or fails with ERRNO)."""

    def __init__(self, owner=None, errno_=None):
        self.calls, self.owner, self.errno_ = [], owner, errno_

    def ioctl(self, fd, request):
        self.calls.append(("ioctl", request))
        if self.owner is None:
            import ctypes
            ctypes.set_errno(self.errno_)
            return -1
        return os.dup(self.owner)

    def setns(self, fd, flag):
        self.calls.append(("setns", os.readlink(f"/proc/self/fd/{fd}"), flag))
        return 0

    def capget(self, header, data):
        return 0

    def capset(self, header, data):
        return 0

    def prctl(self, *args):
        return 0


def test_the_egress_helper_enters_the_user_namespace_that_owns_the_network_one(tmp_path):
    """MCP catalog CI 45006851694 (2026-11-22): every server with network under Bubblewrap failed with
    "setns(CLONE_NEWNET): Operation not permitted". bwrap --unshare-user --uid U moves the sandboxed process into a
    second, nested user namespace (so it holds no capability); its network namespace belongs to the first one, where
    a process entering only the nested one has no right. The owner of the network namespace is entered now."""
    owner = os.open(tmp_path, os.O_RDONLY)  # stands in for the fd NS_GET_USERNS returns
    try:
        libc = NamespaceLibc(owner=owner)
        eg.enter_namespaces(os.getpid(), libc)
        net = os.readlink(f"/proc/{os.getpid()}/ns/net")
        assert libc.calls == [("ioctl", 0xB701), ("setns", str(tmp_path), 0x10000000), ("setns", net, 0x40000000)]
        # A kernel without NS_GET_USERNS (before 4.9): the process's own user namespace, as before.
        import errno
        libc = NamespaceLibc(errno_=errno.ENOTTY)
        eg.enter_namespaces(os.getpid(), libc)
        user = os.readlink(f"/proc/{os.getpid()}/ns/user")
        assert libc.calls == [("ioctl", 0xB701), ("setns", user, 0x10000000), ("setns", net, 0x40000000)]
    finally:
        os.close(owner)


def test_a_bwrap_server_whose_network_never_comes_up_never_runs(tmp_path, monkeypatch, capsys, fakes):
    """Catalog CI 45011158325: slirp4netns failed ("setns(CLONE_NEWNET): Operation not permitted", "child failed(1)")
    and the server ran all the same, with no network - its readiness was read and never checked. A slirp4netns that
    does not say it is ready stops the start now."""
    fake = FakeBwrap(monkeypatch, slirp_ready=False)
    fakes.append(fake)
    assert rootfs.run(rootfs_definition(tmp_path), {}) == 125
    assert "rules" not in fake.events and not fake.opened() and fake.killed
    assert "slirp4netns" in capsys.readouterr().err


def test_slirp4netns_joins_the_user_namespace_that_owns_the_servers_network(tmp_path, monkeypatch):
    """slirp4netns handed only the server's PID joins that process's user namespace - bwrap's nested one, which has
    no right over the network namespace (the CI's "setns(CLONE_NEWNET): Operation not permitted"). It is handed the
    network namespace and its owner instead (NS_GET_USERNS), by descriptor; without NS_GET_USERNS, the PID as before."""
    import errno
    import fcntl
    owner = os.open(tmp_path, os.O_RDONLY)
    try:
        monkeypatch.setattr(fcntl, "ioctl", lambda fd, request, *rest: os.dup(owner) if request == 0xB701 else 0)
        args, fds = rootfs.slirp_target(os.getpid())
        try:
            net, user = fds
            assert os.readlink(f"/proc/self/fd/{net}") == os.readlink(f"/proc/{os.getpid()}/ns/net")
            assert os.readlink(f"/proc/self/fd/{user}") == str(tmp_path)
            assert args == ["--netns-type=path", f"--userns-path=/proc/self/fd/{user}", f"/proc/self/fd/{net}"]
        finally:
            for fd in fds:
                os.close(fd)

        def old_kernel(fd, request, *rest):
            raise OSError(errno.ENOTTY, "no NS_GET_USERNS")
        monkeypatch.setattr(fcntl, "ioctl", old_kernel)
        assert rootfs.slirp_target(os.getpid()) == ([str(os.getpid())], ())
    finally:
        os.close(owner)
