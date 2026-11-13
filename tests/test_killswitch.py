"""The kill switch (sandbox-ai net cut): what each mode lets a running sandbox reach and what it refuses, that it never
widens, that "off" gives back exactly what the run had, and that what is open already is closed - in the supervisor's
state, its firewall rules, its relays of host ports, its DNS stub and questions, the connection proxy (real sockets),
and the command line (a real control socket). The rules in the kernel: tests/test_killswitch_live.py."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_net_killswitch", ROOT / "sandbox/net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
rt = net.launcher().RUNTIME

# A run of Claude whose provider has two addresses, an --llm server in the local network, another on the host's
# loopback (11435), a host port forwarded at the start (8080).
SETTINGS = {"host_ports": [8080, 11435],
            "api": {"https": ["192.0.2.10", "2001:db8::10"], "tcp": [["10.0.0.5", 11434]], "loopback": [11435],
                    "names": ["api.anthropic.com", "console.anthropic.com"]},
            "tor": False, "bypass": [], "machine_host": None}
DESTINATIONS = {
    "provider": ("192.0.2.10", 443, "tcp"),
    "provider over IPv6": ("2001:db8::10", 443, "tcp"),
    "provider on another port": ("192.0.2.10", 80, "tcp"),
    "provider over UDP (QUIC)": ("192.0.2.10", 443, "udp"),
    "llm server": ("10.0.0.5", 11434, "tcp"),
    "llm server, another port": ("10.0.0.5", 22, "tcp"),
    "llm server on the host": ("127.0.0.1", 11435, "tcp"),
    "host port": ("127.0.0.1", 8080, "tcp"),
    "host port over IPv6": ("::1", 8080, "tcp"),
    "LAN address": ("192.168.1.20", 22, "tcp"),
    "LAN over UDP": ("192.168.1.20", 5353, "udp"),
    "LAN over IPv6": ("fd00:1::20", 80, "tcp"),
    "link-local": ("169.254.1.2", 80, "tcp"),
    "internet host": ("198.51.100.7", 443, "tcp"),
    "internet over UDP": ("198.51.100.7", 443, "udp"),
    "internet over IPv6": ("2606:4700::1111", 443, "tcp"),
    "DNS on the internet": ("1.1.1.1", 53, "udp"),
    "DNS over TCP": ("1.1.1.1", 53, "tcp"),
    "DNS in the LAN": ("192.168.1.1", 53, "udp"),
    "DNS stub": ("127.0.0.1", 53, "udp"),
    "own service": ("127.0.0.1", 3000, "tcp"),
    "own service over UDP": ("127.0.0.1", 8080, "udp"),
}
ALWAYS = {"DNS stub", "own service", "own service over UDP"}  # the sandbox's own loopback: no network
EXPECTED = {
    "remote": ALWAYS | {"llm server", "llm server, another port", "llm server on the host", "host port",
                        "host port over IPv6", "LAN address", "LAN over UDP", "LAN over IPv6", "link-local"},
    "local": ALWAYS | {"provider", "provider over IPv6", "provider on another port", "provider over UDP (QUIC)",
                       "internet host", "internet over UDP", "internet over IPv6", "DNS on the internet",
                       "DNS over TCP"},
    "api": ALWAYS | {"provider", "provider over IPv6", "llm server", "llm server on the host"},
    "all": ALWAYS,
}


def view(mode, **changes):
    return {**rt.cut_settings({**SETTINGS, **changes}), "mode": mode}


def reached(cut, destinations=DESTINATIONS):
    return {name for name, (address, port, protocol) in destinations.items()
            if rt.cut_allows(cut, address, port, protocol)}


# --- what each mode allows and refuses --------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", rt.CUT_MODES)
def test_each_mode_reaches_exactly_its_part(mode):
    assert reached(view(mode)) == EXPECTED[mode], sorted(reached(view(mode)) ^ EXPECTED[mode])


def test_no_cut_takes_nothing_away():
    assert reached(None) == set(DESTINATIONS) and reached({"mode": None}) == set(DESTINATIONS)


def test_an_unknown_mode_or_address_refuses():
    assert reached({**view("api"), "mode": "everything"}) == set()
    assert not rt.cut_allows(view("remote"), "not an address", 443)
    assert not rt.cut_name_allows(view("api"), "not an address", 443, "api.anthropic.com")


def test_api_needs_the_providers_name_on_its_shared_addresses():
    cut = view("api")
    assert rt.cut_name_allows(cut, "192.0.2.10", 443, "api.anthropic.com")
    assert rt.cut_name_allows(cut, "192.0.2.10", 443, "API.Anthropic.com.")
    # The same CDN address under another name, or with no name at all (plain TCP, TLS without SNI): refused.
    for name in ("evil.example", "anthropic.com.evil.example", "", None):
        assert not rt.cut_name_allows(cut, "192.0.2.10", 443, name), name
    # An --llm server by its address and port, or on the host, needs no name; other modes ask for none.
    assert rt.cut_name_allows(cut, "10.0.0.5", 11434, None) and rt.cut_name_allows(cut, "127.0.0.1", 11435, None)
    for mode in ("remote", "local", "all"):
        assert rt.cut_name_allows(view(mode), "192.0.2.10", 443, None)


def test_behind_tor_only_the_bypass_is_local_and_api_is_the_provider_through_tor():
    tor = {"tor": True, "bypass": [["192.168.1.20", 1234]], "host_ports": [], "api": {**SETTINGS["api"], "tcp": [],
                                                                                    "loopback": []}}
    places = {"bypass": ("192.168.1.20", 1234, "tcp"), "bypass, another port": ("192.168.1.20", 22, "tcp"),
              "bypass over UDP": ("192.168.1.20", 1234, "udp"), "LAN address": ("192.168.1.21", 1234, "tcp"),
              "onion (Tor's virtual network)": ("10.192.0.5", 443, "tcp"), "provider": ("192.0.2.10", 443, "tcp"),
              "internet host": ("198.51.100.7", 443, "tcp"), "Tor's DNSPort": ("127.0.53.1", 53, "udp"),
              "DNS stub": ("127.0.0.1", 53, "udp")}
    assert reached(view("remote", **tor), places) == {"bypass", "DNS stub"}
    assert reached(view("local", **tor), places) == {"bypass, another port", "bypass over UDP", "LAN address",
                                                     "onion (Tor's virtual network)", "provider", "internet host",
                                                     "Tor's DNSPort", "DNS stub"}
    assert reached(view("api", **tor), places) == {"provider", "DNS stub"}
    assert reached(view("all", **tor), places) == {"DNS stub"}


def test_tors_socksport_is_the_internet_for_the_cut(tmp_path):
    """A Tor profile with --socks: the agent reaches Tor's SocksPort on loopback directly (not through the proxy), and
    tor, whose own connections pass the cut, carries what it sends there to the internet. Every mode that cuts the
    internet cuts the way into Tor as well - in the supervisor's view and in the firewall's chain."""
    tor = {"tor": True, "tor_socks": 9050, "bypass": [["192.168.1.20", 1234]], "host_ports": [],
           "api": {**SETTINGS["api"], "tcp": [], "loopback": []}}
    socks = {"SocksPort": ("127.0.0.1", 9050, "tcp")}
    assert reached(view("local", **tor), socks) == {"SocksPort"}
    for mode in ("remote", "api", "all"):
        assert reached(view(mode, **tor), socks) == set(), mode
        found = rules(mode, **tor)  # "drop" there stands for the rejection
        socks_rule = "ct original ip daddr 127.0.0.1 meta l4proto tcp ct original proto-dst 9050 drop"
        assert socks_rule in found and found.index(socks_rule) < found.index("ct original ip daddr 127.0.0.0/8 return")
    assert not any("9050" in line for line in rules("local", **tor))
    # Without a SocksPort (or without Tor) the port is the sandbox's own, as before.
    assert reached(view("all", **{**tor, "tor_socks": None}), socks) == {"SocksPort"}
    assert rt.cut_settings({**SETTINGS, "tor_socks": 9050})["tor_socks"] is None
    # The launcher hands the supervisor the port when the Tor profile has one.
    l = net.launcher()
    a = l.parse_args(["claude", "--workspace", str(tmp_path)])
    a.vpn_profile = {"type": "tor", "tor": {"socks": True}}
    assert l.killswitch_config(a, {})["tor_socks"] == 9050
    a.vpn_profile = {"type": "tor", "tor": {"socks": False}}
    assert l.killswitch_config(a, {})["tor_socks"] is None


def test_macos_relays_to_the_mac_count_as_the_hosts_ports():
    cut = {"machine_host": "192.168.127.254"}
    assert rt.cut_allows(view("remote", **cut), "192.168.127.254", 8080)
    assert not rt.cut_allows(view("local", **cut), "192.168.127.254", 8080)
    assert rt.cut_allows(view("api", **cut), "192.168.127.254", 11435)
    assert not rt.cut_allows(view("api", **cut), "192.168.127.254", 8080)
    assert not rt.cut_allows(view("all", **cut), "192.168.127.254", 11435)


ALLOW_LISTS = [
    {"loopback": [8080, 11435], "nets": ["192.168.1.0/24"], "https": ["192.0.2.10/32"], "tcp": [["10.0.0.5", 11434]]},
    {"loopback": [], "https": ["192.0.2.10/32", "198.51.100.7/32"], "nets": [], "tcp": []},
    {"all": True, "loopback": [8080]},
    {"loopback": [], "nets": ["0.0.0.0/0", "::/0"], "https": [], "tcp": [["1.1.1.1", 53]]},
]


@pytest.mark.parametrize("mode", rt.CUT_MODES)
@pytest.mark.parametrize("allow", ALLOW_LISTS)
def test_a_mode_never_widens_what_the_proxy_may_reach(mode, allow):
    for name, (address, port, protocol) in DESTINATIONS.items():
        if protocol != "tcp":
            continue
        before = rt.allowed_destination(allow, address, port)
        after = rt.allowed_destination({**allow, "cut": view(mode)}, address, port)
        assert not after or before, (name, mode)
        assert after == (before and rt.cut_allows(view(mode), address, port)), name


def test_settings_are_checked():
    with pytest.raises(ValueError):
        rt.cut_settings({"host_ports": [0]})
    with pytest.raises(ValueError):
        rt.cut_settings({"api": {"https": ["api.example"]}})
    with pytest.raises(ValueError):
        rt.cut_settings({"api": {"tcp": [["10.0.0.5"]]}})
    with pytest.raises(ValueError):
        rt.cut_settings({"bypass": [["fe80::1%eth0", 1]]})
    with pytest.raises(ValueError):
        rt.cut_settings({"host_ports": list(range(1, 100))})
    settings = rt.cut_settings({"api": {"names": ["API.example.org.", "not a name", 5]}})
    assert settings["api"]["names"] == ["api.example.org"] and rt.cut_settings(None)["host_ports"] == []


# --- the firewall's cut chain -------------------------------------------------------------------------------------------

EMPTY = "".join(["flush chain inet agent_sandbox cut\n"] + [
    f"add chain inet agent_sandbox cut_{mode}\nflush chain inet agent_sandbox cut_{mode}\n" for mode in rt.CUT_MODES])


def chains(cut, vpn_mark=None):
    """{chain: [rules]} of cut_rules(CUT): every mode's chain is made and emptied first, then filled. What a mode
    forbids is rejected (rt.CUT_REJECT), never only dropped; written "drop" here, for reading."""
    text = rt.cut_rules(cut, vpn_mark)
    assert text.startswith(EMPTY) and not any(line.endswith("drop") for line in text.splitlines())
    text = text.replace(rt.CUT_REJECT, "drop")
    found = {"cut": []}
    for line in text[len(EMPTY):].splitlines():
        assert line.startswith("add rule inet agent_sandbox cut"), line
        found.setdefault(line.split()[4], []).append(line.split(" ", 5)[5])
    return found


def rules(mode, **changes):
    """The rules of MODE's chain; the cut chain jumps to it after what always passes."""
    found = chains(view(mode, **changes))
    assert found["cut"][-1] == f"jump cut_{mode}" and set(found) == {"cut", f"cut_{mode}"}
    return found[f"cut_{mode}"]


def test_no_cut_is_an_empty_chain():
    assert rt.cut_rules(None) == rt.cut_rules({"mode": None}) == rt.cut_rules({"modes": []}) == EMPTY
    with pytest.raises(ValueError):
        rt.cut_rules({**view("api"), "mode": "everything"})
    with pytest.raises(ValueError):
        rt.cut_rules(view("api"), vpn_mark="meta mark 1 accept; flush ruleset")


def test_every_mode_lets_resets_and_neighbour_discovery_pass_first():
    for mode in rt.CUT_MODES:
        found = chains(view(mode))["cut"]
        assert found[0].startswith("ip6 hoplimit 255 icmpv6 type") and found[1] == "tcp flags & rst == rst return"
        assert found[2] == "meta l4proto { icmp, ipv6-icmp } ct state related return"
        assert found[3:] == [f"jump cut_{mode}"]
    assert chains(view("all"), vpn_mark="meta mark 0xca6c")["cut"][0] == "meta mark 0xca6c return"


def test_several_cuts_hold_together_each_in_its_chain():
    """A run cut by several (a shared MCP server): every mode's chain is jumped to; each drops what it forbids, so
    only what all of them keep passes. "all" among them is all."""
    found = chains({**view("api"), "modes": ["remote", "api"]})
    assert found["cut"][-2:] == ["jump cut_remote", "jump cut_api"] and found["cut_api"][-1] == "drop"
    assert found["cut_remote"][-1] == "drop"
    found = chains({**view("api"), "modes": ["api", "all", "remote"]})
    assert found["cut"][-1] == "jump cut_all" and set(found) == {"cut", "cut_all"}
    both = {**view("remote"), "modes": ["remote", "local"]}
    assert reached(both) == ALWAYS  # the local network and the internet cut: nothing but the sandbox's own
    both = {**view("remote"), "modes": ["remote", "api"]}
    assert reached(both) == ALWAYS | {"llm server", "llm server on the host"}  # only what both keep
    assert rt.cut_label(["api", "remote"]) == "remote+api" and rt.cut_label(["api", "all"]) == "all"
    assert rt.cut_label([]) is None and rt.cut_reason(both).startswith("cut by the kill switch (remote+api: ")


def test_remote_rules():
    found = rules("remote")
    assert not any("proto-dst { 8080" in line for line in found)  # the host's ports stay
    assert any("proto-dst 53 drop" in line for line in found)  # lookups leave for the internet
    local = "ct original ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 224.0.0.0/4, " \
            "255.255.255.255/32 } return"
    assert local in found and "ct original ip6 daddr { fc00::/7, fe80::/10, ff00::/8 } return" in found
    assert found[-1] == "drop" and found.index("ct original ip daddr 127.0.0.0/8 return") < found.index(local)


def test_local_rules():
    found = rules("local")
    assert "ct original ip daddr 127.0.0.1 meta l4proto tcp ct original proto-dst { 8080, 11435 } drop" in found
    assert "ct original ip6 daddr ::1 meta l4proto tcp ct original proto-dst { 8080, 11435 } drop" in found
    assert not any("proto-dst 53" in line for line in found)  # lookups on the internet go on
    assert found[-2:] == ["ct original ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, "
                          "224.0.0.0/4, 255.255.255.255/32 } drop",
                          "ct original ip6 daddr { fc00::/7, fe80::/10, ff00::/8 } drop"]


def test_api_rules():
    found = rules("api")
    assert "ct original ip daddr 127.0.0.1 meta l4proto tcp ct original proto-dst { 8080 } drop" in found
    assert "ct original ip daddr { 192.0.2.10 } meta l4proto tcp ct original proto-dst 443 return" in found
    assert "ct original ip6 daddr { 2001:db8::10 } meta l4proto tcp ct original proto-dst 443 return" in found
    assert "ct original ip daddr 10.0.0.5 meta l4proto tcp ct original proto-dst 11434 return" in found
    assert any("proto-dst 53 drop" in line for line in found) and found[-1] == "drop"


def test_all_rules():
    found = rules("all")
    assert found[-3:] == ["ct original ip daddr 127.0.0.0/8 return", "ct original ip6 daddr ::1 return", "drop"]
    assert "ct original ip daddr != 127.0.0.1 meta l4proto { tcp, udp } ct original proto-dst 53 drop" in found


def test_tor_and_macos_rules():
    tor = {"tor": True, "bypass": [["192.168.1.20", 1234]]}
    found = rules("remote", **tor)
    assert "ct original ip daddr 192.168.1.20 meta l4proto tcp ct original proto-dst 1234 return" in found
    assert not any("10.0.0.0/8" in line for line in found) and found[-1] == "drop"  # past Tor: no LAN of its own
    found = rules("local", **tor)
    assert found[-1] == "ct original ip daddr 192.168.1.20 meta l4proto tcp ct original proto-dst 1234 drop"
    found = rules("api", machine_host="192.168.127.254")
    assert "ct original ip daddr 192.168.127.254 meta l4proto tcp ct original proto-dst { 11435 } return" in found
    assert "ct original ip daddr 192.168.127.254 drop" in found
    assert "ct original ip daddr 192.168.127.254 return" in rules("remote", machine_host="192.168.127.254")


def test_the_launchers_firewall_has_the_cut_chain_first():
    launcher = net.launcher()
    text = launcher.firewall_rules(["192.168.1.0/24"], ["192.0.2.10"], proxy={"port": 15001, "loopback": [8080]},
                                   vpn_mark="meta mark 0xca6c", vpn_rules=["ip daddr 127.0.53.1 drop"],
                                   check_mark=0xca6d)
    lines = text.splitlines()
    assert lines.index(" chain cut {") < lines.index(" chain output { type filter hook output priority 0; policy drop;")
    output = lines[lines.index(" chain output { type filter hook output priority 0; policy drop;"):]
    # Only the VPN container's own checks come before it; the VPN's rules, loopback and established ones after it.
    assert output[1:4] == ["  meta mark 0xca6d accept", "  jump cut", "  ip daddr 127.0.53.1 drop"]
    assert output.index("  jump cut") < output.index('  oifname "lo" accept') < \
        output.index("  ct state established,related accept")


# --- the supervisor: its state, firewall, allow-list, relays, DNS stub and questions --------------------------------

@pytest.fixture
def network(tmp_path):
    applied = []
    found = rt.RuntimeNetwork({"loopback": [8080, 11435], "https": ["192.0.2.10/32"]}, tmp_path / "allow.json",
                              applied.append)
    found.cut_settings = rt.cut_settings(SETTINGS)
    found.applied, found.allow_path = applied, tmp_path / "allow.json"
    return found


def test_off_gives_back_exactly_what_the_run_had(network):
    network.change({"op": "allow", "nets": ["192.168.1.0/24"], "https": ["198.51.100.7"], "dns": ["1.1.1.1"],
                    "tcp": [["10.0.0.9", 5432]]})
    before_rules, before_allow = network.applied[-1], network.allow_path.read_text()
    before_shown, before_servers = network.shown(), network.nameservers()
    assert before_shown["cut"] is None and before_servers == ["1.1.1.1"]
    for mode in rt.CUT_MODES:
        shown = network.set_cut(mode)
        assert shown["cut"] == mode and json.loads(network.allow_path.read_text())["cut"]["mode"] == mode
        runtime_part = network.applied[-1].split("flush chain inet agent_sandbox cut")[0]
        assert runtime_part == before_rules.split("flush chain inet agent_sandbox cut")[0]  # nothing else changed
        assert network.applied[-1].rstrip().endswith(rt.CUT_REJECT) or mode == "local"
        assert shown["cuts"] == {"self": mode}
        assert {k: v for k, v in shown.items() if k not in ("cut", "cuts")} == \
            {k: v for k, v in before_shown.items() if k not in ("cut", "cuts")}
        assert network.nameservers() == (["1.1.1.1"] if mode == "local" else [])
    shown = network.set_cut("off")
    assert network.applied[-1] == before_rules and network.allow_path.read_text() == before_allow
    assert shown == before_shown and network.nameservers() == before_servers
    assert network.set_cut(None)["cut"] is None and network.applied[-1] == before_rules


def test_what_is_allowed_during_a_cut_stays_cut(network):
    network.set_cut("all")
    network.change({"op": "allow", "tcp": [["198.51.100.7", 443]]})
    allow = json.loads(network.allow_path.read_text())
    assert ["198.51.100.7", 443] in allow["tcp"] and allow["cut"]["mode"] == "all"
    assert not rt.allowed_destination(allow, "198.51.100.7", 443)
    assert network.applied[-1].rstrip().endswith(f"add rule inet agent_sandbox cut_all {rt.CUT_REJECT}")
    network.allow_once("198.51.100.8", 443, timer=lambda seconds, function: type("T", (), {
        "daemon": True, "start": lambda self: None})())
    assert not rt.allowed_destination(json.loads(network.allow_path.read_text()), "198.51.100.8", 443)
    network.set_cut("off")
    assert rt.allowed_destination(json.loads(network.allow_path.read_text()), "198.51.100.7", 443)


def test_a_cut_the_firewall_refuses_changes_nothing(network):
    def refused(script):
        raise OSError("nft: no")
    network.apply_rules = refused
    with pytest.raises(OSError):
        network.set_cut("all")
    assert network.cut_mode is None and network.shown()["cut"] is None
    assert not network.allow_path.exists() or "cut" not in json.loads(network.allow_path.read_text())


def test_cut_refusals(network, tmp_path):
    with pytest.raises(ValueError, match="remote, local, api, all or off"):
        network.set_cut("everything")
    old = rt.RuntimeNetwork({}, None, lambda script: None)
    with pytest.raises(ValueError, match="older than 0.1.94"):
        old.set_cut("all")
    assert old.shown()["killswitch"] is False and network.shown()["killswitch"] is True


def test_questions_the_cut_forbids_are_refused_unasked(network, tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "approvals", network, timeout=1)
    network.set_cut("api")
    answer = broker.ask("connect", {"destination": "198.51.100.7:443", "address": "198.51.100.7", "port": 443})
    assert answer["decision"] == "deny" and "kill switch (api" in answer["reason"]
    answer = broker.ask("dns", {"name": "example.org"})
    assert answer["decision"] == "deny" and "kill switch" in answer["reason"]
    assert list((tmp_path / "approvals").iterdir()) == []  # nobody was asked
    network.set_cut("local")
    assert network.cut_refusal("dns", {"name": "example.org"}) is None  # looked up on the internet: allowed
    assert network.cut_refusal("connect", {"address": "192.168.1.20", "port": 22})
    assert network.cut_refusal("connect", {"address": "198.51.100.7", "port": 443}) is None
    assert network.cut_refusal("connect", {"address": "x", "port": "y"})


def unix_echo(path):
    """A host relay's socket (as net.py serves it): each line comes back as "host:" and the line, until closed."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(8)

    def serve(connection):
        with connection, contextlib.suppress(OSError):
            for line in connection.makefile("rb"):
                connection.sendall(b"host:" + line)

    def accept():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return server


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ended(sock, wait=5.0):
    """True once SOCK's peer ended the connection (end of file or a reset) within WAIT seconds."""
    sock.settimeout(wait)
    try:
        return sock.recv(100) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


def talk(sock, line):
    sock.sendall(line)
    sock.settimeout(5)
    return sock.recv(100)


def test_the_relays_of_host_ports_close_what_the_cut_forbids(network):
    directory = Path(tempfile.mkdtemp(prefix="cut-", dir="/tmp"))
    try:
        port, listen = 9123, free_port()
        server = unix_echo(directory / f"relay-{port}.sock")
        network.relay_dir = directory
        network.relay_factory = lambda port, where: rt.PortRelay(port, where, listen_port=listen)
        network.change({"op": "allow", "host_ports": [port]})
        first = socket.create_connection(("127.0.0.1", listen), timeout=5)
        assert talk(first, b"ping\n") == b"host:ping\n"
        network.set_cut("remote")  # the host's ports stay
        assert talk(first, b"two\n") == b"host:two\n"
        network.set_cut("api")  # not the --llm server's port: closed, and new connections refused
        assert ended(first)
        try:  # the relay resets it at once: so soon, at times, that connect() itself sees the reset
            second = socket.create_connection(("127.0.0.1", listen), timeout=5)
        except ConnectionResetError:
            second = socket.socket()
        else:
            assert ended(second)
        network.set_cut("off")
        third = socket.create_connection(("127.0.0.1", listen), timeout=5)
        assert talk(third, b"back\n") == b"host:back\n"
        for sock in (first, second, third):
            sock.close()
        network.close_relays()
        server.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_control_socket_takes_the_cut(network, tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="cut-", dir="/tmp"))
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock", network, stop), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (directory / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert net.ask(directory, {"op": "cut", "mode": "api"})["cut"] == "api"
        assert net.ask(directory, {"op": "show"})["cut"] == "api"
        for bad in ("everything", 5, ["all"]):
            with pytest.raises(ValueError, match="remote, local, api, all or off"):
                net.ask(directory, {"op": "cut", "mode": bad})
        assert network.cut_mode == "api"
        assert net.ask(directory, {"op": "cut", "mode": "off"})["cut"] is None
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


# --- the connection proxy: new connections refused, open ones closed ------------------------------------------------

def tcp_echo():
    """A plain TCP service (as anything the agent reaches): each line comes back, until the client closes."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def serve(connection):
        with connection, contextlib.suppress(OSError):
            for line in connection.makefile("rb"):
                connection.sendall(b"echo:" + line)

    def accept():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return server


class CutProxy:
    """The real ProxyServer; the firewall's redirect replaced by DESTINATION, its connections going to ECHO."""

    def __init__(self, tmp_path, destination, allow):
        self.echo = tcp_echo()
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        target = self.echo.getsockname()
        self.server = rt.ProxyServer({"allow": allow}, self.fd, run_dir=tmp_path, destination=lambda sock: destination,
                                     connect=lambda address, timeout=None: socket.create_connection(target, timeout))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def cut(self, mode):
        allow = dict(self.server.config["allow"])
        allow.pop("cut", None)
        self.server.config["allow"] = {**allow, **({"cut": view(mode)} if mode else {})}

    def records(self, until, wait=5.0):
        deadline = time.monotonic() + wait
        while True:
            found = [json.loads(line) for line in self.log.read_text().splitlines()]
            if any(until(r) for r in found) or time.monotonic() > deadline:
                return found
            time.sleep(0.05)

    def close(self):
        self.listener.close()
        self.echo.close()  # the log stays open: the proxy's threads may still write their last records


def test_the_proxy_closes_open_connections_and_refuses_new_ones(tmp_path):
    proxy = CutProxy(tmp_path, ("192.168.1.20", 5000), {"nets": ["192.168.1.0/24"], "loopback": []})
    try:
        kept, cut = proxy.connect(), proxy.connect()
        assert talk(kept, b"one\n") == b"echo:one\n" and talk(cut, b"one\n") == b"echo:one\n"
        proxy.cut("remote")  # the local network stays: nothing is closed
        time.sleep(rt.CUT_POLL * 3)
        assert talk(kept, b"two\n") == b"echo:two\n"
        proxy.cut("local")  # the local network is cut: the open connections end within a look or two
        assert ended(kept, wait=rt.CUT_POLL * 8) and ended(cut, wait=rt.CUT_POLL * 8)
        record = next(r for r in proxy.records(lambda r: r.get("event") == "cut") if r.get("event") == "cut")
        assert record["destination"] == "192.168.1.20:5000" and "kill switch (local" in record["reason"]
        try:  # refused with a reset: at times so soon that connect() itself sees it
            late = proxy.connect()
        except ConnectionResetError:
            late = socket.socket()
        else:
            assert ended(late)
        denied = [r for r in proxy.records(lambda r: r.get("event") == "denied") if r.get("event") == "denied"]
        assert denied and "kill switch (local" in denied[-1]["reason"]
        proxy.cut(None)  # lifted: what the run may reach is there again
        again = proxy.connect()
        assert talk(again, b"three\n") == b"echo:three\n"
        for sock in (kept, cut, late, again):
            sock.close()
    finally:
        proxy.close()


def client_hello(name):
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        client.do_handshake()
    return outgoing.read()


def test_api_through_the_proxy_needs_the_providers_name(tmp_path):
    proxy = CutProxy(tmp_path, ("192.0.2.10", 443), {"https": ["192.0.2.10/32"], "loopback": []})
    try:
        proxy.cut("api")
        with proxy.connect() as client:
            client.sendall(client_hello("evil.example"))  # another name on the provider's (CDN) address
            assert ended(client)
        record = next(r for r in proxy.records(lambda r: "names none" in r.get("reason", ""))
                      if "names none" in r.get("reason", ""))
        assert record["event"] == "denied" and record["host"] == "evil.example"
        with proxy.connect() as client:
            client.sendall(client_hello("api.anthropic.com"))  # the provider: on to its TLS (the echo speaks none)
            ended(client)
        found = proxy.records(lambda r: r.get("host") == "api.anthropic.com")
        assert any(r.get("host") == "api.anthropic.com" for r in found)
        assert not any(r.get("host") == "api.anthropic.com" and "kill switch" in r.get("reason", "") for r in found)
    finally:
        proxy.close()


def test_the_proxy_reads_the_cut_from_its_allow_list_file(tmp_path, network):
    """As in a run: the supervisor writes the cut into allow.json, the proxy (another process) reads it there."""
    network.set_cut("all")
    server = rt.ProxyServer({"allow_file": str(network.allow_path), "allow": {}}, os.open(os.devnull, os.O_WRONLY),
                            run_dir=tmp_path)
    assert server.allow()["cut"]["mode"] == "all"
    assert not rt.allowed_destination(server.allow(), "192.0.2.10", 443)
    network.set_cut("off")
    assert "cut" not in server.allow() and rt.allowed_destination(server.allow(), "192.0.2.10", 443)


# --- the command line: sandbox-ai net cut ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_login_runtime(tmp_path, monkeypatch):
    """As in test_net.py: the runs of a real login session stay out; PIDs are judged as on the host."""
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(net.liveness(), "inside_sandbox", lambda *a, **k: False)

    def no_podman(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(net.liveness(), "RUNNER", no_podman)
    net.liveness()._cache.clear()


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Register runs, each with a real control_server thread (its own RuntimeNetwork with the kill switch) unless
    started by an older version; in a short directory (socket paths are limited to 107 bytes)."""
    short = Path(tempfile.mkdtemp(prefix="cut-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    started, networks = [], {}

    def add(name, supervisor=True, ready=True, **fields):
        directory = net.launcher().runs_dir() / name
        directory.mkdir(parents=True)
        record = {"run": name, "agent": "claude", "pid": os.getpid(), "workspace": str(tmp_path / name),
                  "session_dir": str(tmp_path), "started": "now", "session": None, "net_cut": 1, **fields}
        (directory / "run.json").write_text(json.dumps({k: v for k, v in record.items() if v is not None}))
        if supervisor:
            network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
            network.cut_settings = rt.cut_settings(SETTINGS)
            stop = threading.Event()
            thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock", network, stop),
                                      daemon=True)
            thread.start()
            started.append((stop, thread))
            networks[name] = network
            deadline = time.monotonic() + 5
            while not (directory / "control.sock").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if ready:  # as the supervisor once its socket listens: what waited is set, a cut is asked of it from now
                rt.starting_cuts(network, directory)
        return directory
    add.networks = networks
    yield add
    for stop, thread in started:
        stop.set()
        thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def cut(*argv):
    lines = []
    status = net.main(["cut", *argv], out=lines.append)
    return status, lines


def listed():
    lines = []
    assert net.main(["list", "--json"], out=lines.append) == 0
    return {entry["run"]: entry for entry in json.loads("\n".join(lines))}


def test_cut_and_lift_one_run(runs, capsys):
    directory = runs("claude-1")
    status, lines = cut("api")  # the only run: its name may be left out
    assert status == 0, capsys.readouterr().err
    assert lines == ["claude-1: kill switch api: only its model API stays reachable",
                     "undo: sandbox-ai net cut claude-1 off"]
    assert runs.networks["claude-1"].cut_mode == "api" and json.loads((directory / "cut.json").read_text())["mode"] == "api"
    assert listed()["claude-1"]["cut"] == "api" and listed()["claude-1"]["killswitch"] is True
    shown = []
    assert net.main(["show", "--json"], out=shown.append) == 0 and json.loads("\n".join(shown))["cut"] == "api"
    shown = []
    assert net.main(["show"], out=shown.append) == 0
    assert "kill switch: api: only its model API stays reachable" in shown[0] and "sandbox-ai net cut claude-1 off" in shown[1]
    text = []
    assert net.main(["list"], out=text.append) == 0 and text[0].split()[-1] == "CUT" and text[1].split()[-1] == "api"
    status, lines = cut("claude-1", "off")
    assert status == 0 and lines == ["claude-1: kill switch off: the cut is lifted: it reaches again what it may reach"]
    assert runs.networks["claude-1"].cut_mode is None and not (directory / "cut.json").exists()
    assert listed()["claude-1"]["cut"] is None
    text = []
    assert net.main(["list"], out=text.append) == 0 and "CUT" not in text[0]  # the column only while a cut holds


def test_cut_parsing_and_refusals(runs, capsys):
    runs("claude-1")
    assert cut("everything")[0] == 1 and "no kill switch mode everything" in capsys.readouterr().err
    assert cut("claude-1", "api", "more")[0] == 1 and "give [RUN] MODE" in capsys.readouterr().err
    assert cut("--all-runs", "claude-1", "api")[0] == 1 and "--all-runs takes the MODE alone" in capsys.readouterr().err
    assert cut("nobody", "api")[0] == 1 and "no start named nobody" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        net.main(["cut"], out=lambda line: None)
    assert runs.networks["claude-1"].cut_mode is None


def test_a_run_of_an_older_version_is_refused_with_what_to_do(runs, monkeypatch):
    runs("old-1", supervisor=False, net_cut=None)
    status, lines = cut("old-1", "all")
    assert status == 1 and "older than 0.1.94, which has no kill switch" in lines[0]
    assert "sandbox-ai stop old-1" in lines[0] and "start it again with 0.1.94 or newer" in lines[0]
    # A record that says it has one, but a supervisor that knows no "cut" (an older runtime): the same advice.
    (runs("odd-1", supervisor=False) / net.CUT_READY).touch()
    monkeypatch.setattr(net, "ask", lambda *a, **k: (_ for _ in ()).throw(
        ValueError("the sandbox refused: op must be allow or revoke")))
    status, lines = cut("odd-1", "all")
    assert status == 1 and "older than 0.1.94" in lines[0]


def test_all_runs_cuts_every_run_at_once_and_says_what_failed(runs):
    runs("claude-1")
    runs("codex-2", agent="codex")
    runs("old-3", supervisor=False, net_cut=None)
    lines = []
    status = net.main(["cut", "--all-runs", "all", "--json"], out=lines.append)
    report = json.loads("\n".join(lines))
    assert status == 1 and report["mode"] == "all"
    by_run = {entry["run"]: entry for entry in report["runs"]}
    assert by_run["claude-1"]["ok"] and by_run["claude-1"]["cut"] == "all"
    assert by_run["codex-2"]["ok"] and by_run["codex-2"]["cut"] == "all"
    assert not by_run["old-3"]["ok"] and "0.1.94" in by_run["old-3"]["error"] and by_run["old-3"]["cut"] is None
    assert runs.networks["claude-1"].cut_mode == runs.networks["codex-2"].cut_mode == "all"
    status, lines = cut("--all-runs", "off")
    assert status == 1 and "claude-1: kill switch off: the cut is lifted: it reaches again what it may reach" in lines
    assert any(line.startswith("old-3: NOT lifted") for line in lines)
    assert runs.networks["claude-1"].cut_mode is None and runs.networks["codex-2"].cut_mode is None


def test_a_cut_holds_for_the_runs_mcp_servers_helpers_and_team(runs):
    runs("lead-1", mcp_servers=["files", "team-alpha"])
    runs("mcp-files", kind="mcp", mcp_name="files", agent=None)
    runs("mcp-team-alpha", kind="mcp", agent=None)
    runs("helper-2", helper_of="lead-1", mcp_servers=["search"])
    runs("mcp-search", kind="mcp", mcp_name="search")
    runs("member-3", mcp_servers=["team-alpha"])
    runs("other-4", mcp_servers=["unrelated"])
    status, lines = cut("lead-1", "all")
    assert status == 0, lines
    cut_now = {name for name, network in runs.networks.items() if network.cut_mode == "all"}
    assert cut_now == {"lead-1", "mcp-files", "mcp-team-alpha", "helper-2", "mcp-search", "member-3"}
    assert lines[0] == "lead-1: kill switch all: everything is cut, its model API too"
    assert "mcp-files: kill switch all (it acts for lead-1): everything is cut, its model API too" in lines
    assert lines[-1] == "undo: sandbox-ai net cut lead-1 off"
    assert cut("lead-1", "off")[0] == 0 and not any(n.cut_mode for n in runs.networks.values())
    status, lines = cut("lead-1", "api", "--only")
    assert status == 0 and {n for n, network in runs.networks.items() if network.cut_mode} == {"lead-1"}


def test_related_runs_follow_helpers_and_teams_once():
    records = {"a": {"run": "a", "mcp_servers": ["x", "team-t"]}, "mcp-x": {"run": "mcp-x", "kind": "mcp"},
               "b": {"run": "b", "helper_of": "a"}, "c": {"run": "c", "helper_of": "b"},
               "d": {"run": "d", "mcp_servers": ["team-t"]}, "e": {"run": "e", "helper_of": "a", "mcp_servers": ["x"]},
               "bwrap-mcp": {"run": "bwrap-mcp", "kind": "mcp", "mcp_name": "x"}, "f": {"run": "f"}}
    assert sorted(run for run, _ in net.related_runs(records, records["a"])) == ["b", "bwrap-mcp", "c", "d", "e",
                                                                                  "mcp-x"]
    assert net.related_runs(records, records["f"]) == []


def test_related_runs_find_the_instance_a_start_has_with_limits_of_its_own():
    """--mcp-limits: a start reaching server x in its own instance x.HASH (run.json mcp_instances) is cut with that
    instance's sandbox, not with the shared x of the other starts."""
    records = {"a": {"run": "a", "mcp_servers": ["x"], "mcp_instances": {"x": "x.0123abcd"}},
               "mcp-x": {"run": "mcp-x", "kind": "mcp"},
               "mcp-x.0123abcd": {"run": "mcp-x.0123abcd", "kind": "mcp", "mcp_name": "x.0123abcd"},
               "b": {"run": "b", "mcp_servers": ["x"]}}
    assert [run for run, _ in net.related_runs(records, records["a"])] == ["mcp-x.0123abcd"]
    assert [run for run, _ in net.related_runs(records, records["b"])] == ["mcp-x"]
    records["a"]["mcp_instances"] = "damaged"  # a run.json someone wrote: as without instances
    assert [run for run, _ in net.related_runs(records, records["a"])] == ["mcp-x"]


def test_the_modes_and_texts_agree_with_the_runtimes():
    assert net.CUT_MODES == rt.CUT_MODES
    assert {mode: net.CUT_TEXT[mode] for mode in rt.CUT_MODES} == rt.CUT_TEXT


def test_register_run_says_the_run_has_a_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    l = net.launcher()
    a = l.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    l.validate(a)
    record = json.loads((l.register_run(a, "claude-1234abcd", tmp_path / "session") / "run.json").read_text())
    assert record["net_cut"] == 1 and "mcp_name" not in record


# --- review fixes: shared servers, paused runs, runs that join a cut, the limits, the proxy's retry ----------------

def modes_now(runs):
    return {name: network.cut_mode for name, network in runs.networks.items()}


def test_a_shared_server_keeps_every_runs_cut_and_the_strictest_holds(runs):
    """A and B reach the same MCP server: lifting or loosening B's cut never loosens what A's cut holds there."""
    runs("a-1", mcp_servers=["browser"])
    runs("b-2", mcp_servers=["browser"])
    runs("mcp-browser", kind="mcp", mcp_name="browser")
    assert cut("--all-runs", "all")[0] == 0
    status, lines = cut("b-2", "off")
    assert status == 0 and modes_now(runs) == {"a-1": "all", "b-2": None, "mcp-browser": "all"}
    assert any(line.startswith("mcp-browser: still cut (all) (it acts for b-2): the cut of a-1, mcp-browser holds")
               for line in lines), lines
    assert cut("b-2", "remote")[0] == 0 and modes_now(runs)["mcp-browser"] == "all"
    assert runs.networks["mcp-browser"].cuts == {"a-1": "all", "b-2": "remote", "mcp-browser": "all"}
    assert cut("--all-runs", "off")[0] == 0 and not any(modes_now(runs).values())
    # Each run's own cut: the server holds both, together; one lifted leaves the other's.
    assert cut("a-1", "api")[0] == 0 and cut("b-2", "remote")[0] == 0
    assert modes_now(runs)["mcp-browser"] == "remote+api"
    status, lines = cut("b-2", "off")
    assert modes_now(runs) == {"a-1": "api", "b-2": None, "mcp-browser": "api"}
    assert json.loads((net.launcher().runs_dir() / "mcp-browser" / "cut.json").read_text())["cuts"] == {"a-1": "api"}
    assert listed()["mcp-browser"]["cut"] == "api"
    report = []
    assert net.main(["cut", "a-1", "all", "--json"], out=report.append) == 0
    entry = next(e for e in json.loads("\n".join(report))["runs"] if e["run"] == "mcp-browser")
    assert entry["cut"] == "all" and entry["cuts"] == {"a-1": "all"} and entry["for"] == "a-1"


def test_a_paused_run_is_cut_as_it_resumes(runs):
    directory = runs("claude-1")
    (directory / "paused").write_text("")
    status, lines = cut("claude-1", "all")
    assert status == 0 and "is paused: nothing leaves it now; the kill switch all is set as it resumes" in lines[0]
    assert runs.networks["claude-1"].cut_mode is None
    assert json.loads((directory / net.CUT_PENDING).read_text()) == [{"op": "cut", "mode": "all", "by": ["claude-1"]}]
    report = []
    assert net.main(["cut", "claude-1", "api", "--json"], out=report.append) == 0
    assert json.loads("\n".join(report))["runs"][0]["pending"] is True
    (directory / "paused").unlink()  # resumed (sandbox-ai resume thaws it, then:)
    done = net.apply_pending_cut(directory)
    assert [error for _, error in done] == [None, None] and not (directory / net.CUT_PENDING).exists()
    assert runs.networks["claude-1"].cut_mode == "api" and net.cut_state(directory) == "api"
    assert net.apply_pending_cut(directory) == []  # nothing waits any more


def test_a_cut_racing_the_resume_is_never_left_waiting(runs, monkeypatch):
    """The cut sees the run paused just before its resume thaws it and sets what waits (finding nothing yet): the
    request must not be kept for a resume that has passed - the run would go on uncut. The resume waits for the cut
    (the run's cut lock) and then sets it."""
    directory = runs("claude-1")
    (directory / "paused").write_text("")
    paused, resumes = net.run_paused, []

    def resume():
        (directory / "paused").unlink()
        net.apply_pending_cut(directory)

    def seen_paused(record):
        found = paused(record)
        if found and not resumes:  # the resume runs right after this look
            resumes.append(threading.Thread(target=resume))
            resumes[0].start()
            resumes[0].join(0.5)
        return found
    monkeypatch.setattr(net, "run_paused", seen_paused)
    status, lines = cut("claude-1", "all")
    assert status == 0, lines
    resumes[0].join(10)
    assert not resumes[0].is_alive()
    assert runs.networks["claude-1"].cut_mode == "all" and net.cut_state(directory) == "all"
    assert not (directory / net.CUT_PENDING).exists()


def test_the_resume_of_a_paused_run_sets_the_waiting_cut(runs, monkeypatch):
    directory = runs("claude-1")
    (directory / "paused").write_text("")
    assert cut("claude-1", "all")[0] == 0
    starts = net.starts_module()
    loader = starts.module
    monkeypatch.setattr(starts, "module", lambda name: net if name == "net" else loader(name))
    monkeypatch.setattr(net, "launcher_process", lambda record: (os.getpid(), None))
    monkeypatch.setattr(loader("sessions"), "signal_tree", lambda *a, **k: None)
    monkeypatch.setattr(starts, "record", lambda *a, **k: None)
    starts.pause_run({"name": "claude-1", "id": "claude-1", "run": "claude-1"},
                     {**json.loads((directory / "run.json").read_text()), "directory": str(directory),
                      "backend": "bwrap"}, False)
    assert runs.networks["claude-1"].cut_mode == "all" and not (directory / "paused").exists()


def test_a_run_that_joins_a_cut_run_is_cut_before_its_agent_runs(runs):
    runs("lead-1", mcp_servers=["files"])
    assert cut("lead-1", "api")[0] == 0
    assert net.joined_cuts("other-9") == {}  # nothing it acts for is cut
    directory = runs("mcp-files", kind="mcp", mcp_name="files", supervisor=False)
    assert net.joined_cuts("mcp-files", directory) == {"lead-1": "api"}
    assert json.loads((directory / net.CUT_FILE).read_text())["cuts"] == {"lead-1": "api"}
    # The supervisor sets them before the agent runs; one it cannot set stops the start.
    network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    network.cut_settings = rt.cut_settings(SETTINGS)
    assert rt.start_cuts(network, {"lead-1": "api"})["cuts"] == {"lead-1": "api"} and network.cut_mode == "api"
    with pytest.raises(RuntimeError, match="no kill switch of its own"):
        rt.start_cuts(rt.RuntimeNetwork({}, None, lambda script: None), {"lead-1": "api"})
    with pytest.raises(RuntimeError, match="not readable"):
        rt.start_cuts(network, {"lead-1": "everything"})

    def refused(script):
        raise OSError("nft: no")
    network.apply_rules = refused
    with pytest.raises(RuntimeError, match="could not be set"):
        rt.start_cuts(network, {"lead-1": "all"})
    assert rt.start_cuts(network, {}) is None


def test_a_cut_while_a_joining_run_starts_waits_for_its_supervisor(runs):
    """The run it acts for is cut after the joining run read the cuts (joined_cuts) but before its supervisor
    listens: never "NOT cut" and running uncut - the request waits, the supervisor sets it before the agent runs."""
    runs("lead-1", mcp_servers=["files"])
    directory = runs("mcp-files", kind="mcp", mcp_name="files", supervisor=False)
    assert net.joined_cuts("mcp-files", directory) == {}  # nothing cut yet
    status, lines = cut("lead-1", "api")
    assert status == 0, lines
    assert any("mcp-files" in line and "is starting" in line for line in lines), lines
    status, lines = cut("lead-1", "all")  # a second one waits behind the first, in its order
    assert status == 0 and json.loads((directory / net.CUT_STARTING).read_text())[-1]["mode"] == "all"
    # Its supervisor: listening, then CUT_READY, then what waited, before the agent runs.
    network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    network.cut_settings = rt.cut_settings(SETTINGS)
    shown = rt.starting_cuts(network, directory)
    assert shown["cuts"] == {"lead-1": "all"} and network.cut_mode == "all"
    assert (directory / net.CUT_READY).exists() and net.cut_state(directory) == "all"
    # From now on a cut is asked of it, not kept: without a listening socket that is an honest failure.
    status, lines = cut("lead-1", "off")
    assert status == 1 and any("mcp-files: NOT lifted" in line for line in lines), lines


def test_a_run_that_registers_while_the_cut_is_on_its_way_is_cut_too(runs, monkeypatch):
    """`net cut` read the runs before the server registered, and the server read the cuts (joined_cuts) before the
    lead's was confirmed: the cut looks for such runs again once it is confirmed."""
    runs("lead-1", mcp_servers=["files"])
    real = net.cut_one

    def joining_meanwhile(record, *args, **kwargs):
        if record.get("run") == "lead-1" and "mcp-files" not in runs.networks:
            directory = runs("mcp-files", kind="mcp", mcp_name="files")
            assert net.joined_cuts("mcp-files", directory) == {}  # the lead's cut is not confirmed yet
        return real(record, *args, **kwargs)
    monkeypatch.setattr(net, "cut_one", joining_meanwhile)
    status, lines = cut("lead-1", "api")
    assert status == 0, lines
    assert runs.networks["mcp-files"].cuts == {"lead-1": "api"}
    assert any(line.startswith("mcp-files: kill switch api (it acts for lead-1)") for line in lines), lines


def test_a_cut_kept_for_a_start_that_cannot_be_set_stops_it(tmp_path):
    (tmp_path / net.CUT_STARTING).write_text(json.dumps([{"op": "cut", "mode": "all", "by": "lead-1"}]))
    without = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)  # no kill switch of its own
    with pytest.raises(RuntimeError, match="could not be set"):
        rt.starting_cuts(without, tmp_path)
    network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    network.cut_settings = rt.cut_settings(SETTINGS)
    for broken in ("{not json", json.dumps({"mode": "all"}), json.dumps([{"mode": "everything", "by": "x"}])):
        (tmp_path / net.CUT_STARTING).write_text(broken)
        with pytest.raises(RuntimeError, match="does not start"):
            rt.starting_cuts(network, tmp_path)
    (tmp_path / net.CUT_STARTING).unlink()
    assert rt.starting_cuts(network, tmp_path) is None and (tmp_path / net.CUT_READY).exists()
    assert rt.CUT_READY == net.CUT_READY and rt.CUT_STARTING == net.CUT_STARTING and rt.CUT_STATE == net.CUT_FILE


def test_the_supervisor_sets_waiting_cuts_before_the_agent_and_holds_the_socket_s_cuts_until_then():
    import inspect
    source = inspect.getsource(rt.inner_bootstrap)
    assert source.index("listening.wait(") < source.index("starting_cuts(network)") < \
        source.index("cuts_ready.set()") < source.index("subprocess.Popen(cmd")
    server = inspect.getsource(rt.control_server)
    assert server.index("listening.set()") < server.index("listener.accept()")
    assert server.index('request.get("op") == "cut"') < server.index("cuts_ready.wait(") < \
        server.index("network.set_cut(mode")


def test_a_server_attached_while_its_run_is_cut_gets_the_cut(runs):
    lead = runs("lead-1")
    assert cut("lead-1", "all")[0] == 0
    runs("mcp-late", kind="mcp", mcp_name="late")
    record = json.loads((lead / "run.json").read_text())
    (lead / "run.json").write_text(json.dumps({**record, "mcp_servers": ["late"]}))  # attached live
    runs_now = net.running()
    assert net.propagate_cut(runs_now["lead-1"], runs_now) == [("mcp-late", None)]
    assert runs.networks["mcp-late"].cuts == {"lead-1": "all"}
    assert net.propagate_cut(runs_now["lead-1"], net.running()) == []  # holds there already


def test_too_many_host_ports_start_without_a_kill_switch_and_say_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    l = net.launcher()
    a = l.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    a.allow_host_port = [str(port) for port in range(20000, 20000 + rt.CUT_PORTS + 1)]
    assert l.killswitch_config(a, {}) is None
    assert "this run has no kill switch (sandbox-ai net cut): killswitch: at most 64 host ports" in \
        capsys.readouterr().err
    a.allow_host_port = ["3080"]
    a.killswitch = None
    record = json.loads((l.register_run(a, "claude-1234abcd", tmp_path / "session") / "run.json").read_text())
    assert "net_cut" not in record  # `net cut` says it has none, as for a run of an older version
    # The supervisor never fails a start for it either: it runs without a kill switch then.
    with pytest.raises(ValueError):
        rt.cut_settings({"api": {"https": [f"192.0.2.{n % 250}" for n in range(rt.CUT_API_LIMIT + 1)]}})


def test_the_proxy_looks_again_when_closing_failed(tmp_path):
    server = rt.ProxyServer({"allow": {"cut": view("all")}}, os.open(os.devnull, os.O_WRONLY), run_dir=tmp_path)
    calls, stop = [], threading.Event()

    def close_cut(cut):
        calls.append(cut["mode"])
        if len(calls) == 1:
            raise OSError("for the test")
        stop.set()
        return 0
    server.close_cut = close_cut
    server.watch_cut(stop, every=0.01)
    assert calls == ["all", "all"]  # the failed look was tried again


def test_a_cut_rejects_what_it_forbids_and_lets_its_own_rejection_back():
    """The first run in a real kernel: every cut connection waited ("direct") instead of being refused. A dropped TCP
    SYN leaves connect() sending it again and again - the agent hangs, and nothing tells a cut connection from one on
    its way out. So what a cut forbids is rejected, and the rejection's own ICMP error (related to the connection it
    ends, whose original destination is the forbidden one) passes the cut before any mode's chain."""
    for modes in (["remote"], ["local"], ["api"], ["all"], ["remote", "api"]):
        text = rt.cut_rules({**view("api"), "modes": modes})
        verdicts = [line.rsplit(" ", 1)[1] for line in text.splitlines() if line.startswith("add rule")]
        assert "drop" not in verdicts, modes
        assert any(line.endswith(rt.CUT_REJECT) for line in text.splitlines()), modes
        lines = text.splitlines()
        related = lines.index("add rule inet agent_sandbox cut meta l4proto { icmp, ipv6-icmp } ct state related "
                              "return")
        assert related < min(i for i, line in enumerate(lines) if " jump cut_" in line)
    assert rt.CUT_REJECT == "reject with icmpx type admin-prohibited"  # as Tor's kill switch refuses (its live test)


# --- MCP image servers: their containers reach the network on their own, outside any supervisor's cut -----------------
#
# A catalog (image) server with network runs one container per connection, started by a bridge on the host with a
# network of its own (pasta, or its VPN container's): there is no supervisor of its own that a cut could reach
# (related_runs finds only runs). Before: `net cut RUN all` left the agent's fetch tool reaching the internet. The
# run's own relay (the supervisor's, which every request of the agent passes) refuses such a server's requests while
# any cut holds.

def short_dir():
    return Path(tempfile.mkdtemp(prefix="ks-mcp-", dir="/tmp"))


def fake_mcp_server(path):
    """A server socket at PATH answering each request with its method; (stop, thread)."""
    listener = rt.listen_unix(str(path))
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            def answer(connection=connection):
                with connection:
                    for line in connection.makefile("rb"):
                        request = json.loads(line)
                        if "id" in request:
                            connection.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                                            "result": {"method": request.get("method")}})
                                                + "\n").encode())
            threading.Thread(target=answer, daemon=True).start()
        listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return stop, thread


def ask_mcp(path, method, **params):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(path))
        message = {"jsonrpc": "2.0", "id": 7, "method": method, **({"params": params} if params else {})}
        client.sendall((json.dumps(message) + "\n").encode())
        line = client.makefile("rb").readline()
        return json.loads(line) if line else None


def test_a_cut_refuses_the_requests_of_an_mcp_image_server_that_reaches_the_network(network):
    base = short_dir()
    try:
        for name in ("fetch", "files"):
            (base / "up" / name).mkdir(parents=True)
        servers = [fake_mcp_server(base / "up" / name / "server.sock") for name in ("fetch", "files")]
        relays = rt.McpRelays(run_dir=base / "run", upstream_dir=base / "up", log=base / "calls.jsonl",
                              cut=network.cut_view)
        relays.open("fetch", {"server": "fetch", "rules": [], "network": True})  # an image server with network
        relays.open("files", {"server": "files", "rules": []})  # one without (or one with a supervisor of its own)
        fetch, files = base / "run/fetch/server.sock", base / "run/files/server.sock"
        call = {"name": "fetch", "arguments": {"url": "https://example.org"}}
        assert ask_mcp(fetch, "tools/call", **call)["result"] == {"method": "tools/call"}  # no cut: as before
        for mode in rt.CUT_MODES:
            network.set_cut(mode)
            refused = ask_mcp(fetch, "tools/call", **call)
            assert "error" in refused and "kill switch" in refused["error"]["message"], (mode, refused)
            for method in ("resources/read", "prompts/get", "completion/complete"):
                assert "error" in ask_mcp(fetch, method), (mode, method)
            # The handshake and the lists still pass (they reach no network), and a server without network as well.
            for method in ("initialize", "ping", "tools/list", "resources/list"):
                assert ask_mcp(fetch, method)["result"] == {"method": method}, (mode, method)
            assert ask_mcp(files, "tools/call", **call)["result"] == {"method": "tools/call"}, mode
        network.set_cut("off")
        assert ask_mcp(fetch, "tools/call", **call)["result"] == {"method": "tools/call"}  # lifted: as before
        events = [json.loads(line) for line in (base / "calls.jsonl").read_text().splitlines()]
        assert any(e.get("event") == "refused" and "kill switch" in e.get("reason", "") for e in events)
        for name in relays.names():
            relays.detach(name)
        for stop, thread in servers:
            stop.set()
            thread.join(5)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_launcher_marks_the_mcp_servers_a_cut_must_refuse_in_the_runs_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg/sandbox-ai").mkdir(parents=True)
    (tmp_path / "cfg/sandbox-ai/mcp.json").write_text(json.dumps({
        "fetch": {"image": "docker.io/mcp/fetch@sha256:" + "a" * 64, "network": "full"},
        "offline-img": {"image": "docker.io/mcp/time@sha256:" + "b" * 64},
        "web-search": {"command": ["python3", "-I", "/opt/x.py"], "options": ["--allow-github"]}}))
    l = net.launcher()
    a = l.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    assert l.mcp_relay_config(a, "fetch").get("network") is True
    # No network, or a sandbox and supervisor of its own (a command server, a helper): its own cut holds there.
    for name in ("offline-img", "web-search", "demo"):
        assert "network" not in l.mcp_relay_config(a, name), name


def test_a_server_attached_live_keeps_the_mark():
    base = short_dir()
    original = rt.detached_root
    try:
        (base / "up").mkdir()
        relays = rt.McpRelays(run_dir=base / "run", log=base / "calls.jsonl", live=True, cut=lambda: view("all"))
        fd = os.open(base / "up", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        rt.detached_root = lambda fd: True
        answer = rt.mcp_control({"op": "mcp-open", "name": "fetch",
                                 "relay": {"server": "fetch", "rules": [], "network": True}}, relays, [fd])
        assert answer["opened"]
        assert relays.relays["fetch"].gateway.cut_refusal("tools/call")
        assert relays.relays["fetch"].gateway.cut_refusal("tools/list") is None
        relays.detach("fetch")
    finally:
        rt.detached_root = original
        shutil.rmtree(base, ignore_errors=True)
