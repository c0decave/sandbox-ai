"""VPN profile type "tor": the profile (bridges, exit countries, SOCKS), the generated torrc, the kill switch and the
redirects as nft text, the bootstrap wait, the checks (IsTor, tor's DNSPort, .onion mapping) and the agent's
firewall, without Podman or tor. The bypass tests are named after what an agent would try."""
import argparse
import contextlib
import importlib.util
import ipaddress
import json
import socket
from pathlib import Path
import stat
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_tor_test", "agent_sandbox.py")
rt = load("vpn_runtime_tor_test", "vpn_runtime.py")
v = s.load_vpn_module()

# Shaped like real bridge lines (documentation addresses, made-up fingerprints and certificates).
CERT_A = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdEfGhIjKlMnOpQrStUvWxYz01234"
CERT_B = "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210+/ZyXwVuTsRqPoNmLkJiHgFeDcBa98765"
FP_A = "0123456789ABCDEF0123456789ABCDEF01234567"
FP_B = "89ABCDEF0123456789ABCDEF0123456789ABCDEF"
BRIDGES = f"""# from bridges.torproject.org
obfs4 192.0.2.10:443 {FP_A} cert={CERT_A} iat-mode=0

Bridge obfs4 198.51.100.20:9001 {FP_B} cert={CERT_B} iat-mode=1
"""
ONION = ["10.192.0.7"]
TOR_TUNNEL = {"interface": f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}", "implementation": "tor", "ipv4": True,
              "ipv6": False, "full4": True, "full6": False, "endpoints": ["the Tor network"], "dns": [rt.TOR_DNS],
              "tor": {"uid": rt.TOR_UID, "bridges": 0}}
HOST = {"ipv4": "198.18.0.5", "ipv6": "2001:db8:ff::5", "dns_egress": ["198.18.53.53"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def secrets_dir(home):
    return home / "data" / "sandbox-ai" / "secrets"


def bridges_file(home, text=BRIDGES, mode=0o600):
    path = home / "bridges.txt"
    path.write_text(text)
    path.chmod(mode)
    return path


def tor_profile(**tor):
    return {"version": 1, "name": "anon", "type": "tor", "provider": "tor",
            "tor": {"bridges": 0, "bridges_secret": None, "bridges_secret_owned": False, "exit_country": [],
                    "socks": False, **tor}}


# --- the profile -------------------------------------------------------------------------------------------------

def test_a_plain_tor_profile(home, capsys):
    assert v.main(["add", "anon", "--tor"]) == 0
    profile = v.load_profile("anon")
    assert profile["type"] == "tor" and profile["provider"] == "tor"
    assert profile["tor"] == {"bridges": 0, "bridges_secret": None, "bridges_secret_owned": False, "transports": [],
                              "exit_country": [], "socks": False}
    facts = v.describe(profile)
    assert facts["type"] == "tor" and facts["socks"] == "off" and facts["transparent"] is True
    assert facts["dns"] == [rt.TOR_DNS] and facts["ipv6"] is False and facts["bridges"] == 0
    assert facts["endpoints"] == ["the Tor network"] and facts["exit_country"] == "any"
    assert v.main(["show", "anon"]) == 0 and v.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "VPN profile anon added: tor, tor, the Tor network" in out and "anon  tor  tor  the Tor network" in out
    assert v.main(["remove", "anon"]) == 0 and not v.profile_path("anon").exists()


def test_socks_and_exit_countries(home):
    assert v.main(["add", "anon", "--tor", "--socks", "--exit-country", "DE,nl"]) == 0
    profile = v.load_profile("anon")
    assert profile["tor"]["socks"] is True and profile["tor"]["exit_country"] == ["de", "nl"]
    facts = v.describe(profile)
    assert facts["socks"].startswith(f"127.0.0.1:{rt.TOR_SOCKS_PORT}") and "connection proxy" in facts["socks"]
    assert facts["exit_country"] == ["de", "nl"]


def test_bridges_from_a_private_file_go_to_the_secret_store(home, capsys):
    path = bridges_file(home)
    assert v.main(["add", "anon", "--tor", "--bridges", str(path)]) == 0
    stored = (secrets_dir(home) / "VPN_ANON_BRIDGES").read_text()
    assert stored == (f"Bridge obfs4 192.0.2.10:443 {FP_A} cert={CERT_A} iat-mode=0\n"
                      f"Bridge obfs4 198.51.100.20:9001 {FP_B} cert={CERT_B} iat-mode=1\n")
    assert stat.S_IMODE((secrets_dir(home) / "VPN_ANON_BRIDGES").stat().st_mode) == 0o600
    profile = v.load_profile("anon")
    assert profile["tor"]["bridges"] == 2 and profile["tor"]["transports"] == ["obfs4"]
    assert profile["tor"]["bridges_secret"] == "VPN_ANON_BRIDGES" and profile["tor"]["bridges_secret_owned"]
    # Bypass: a bridge's address is its secret. Neither the profile, show, list nor the dry-run facts carry one.
    v.main(["show", "anon", "--json"])
    v.main(["list", "--json"])
    shown = capsys.readouterr().out + v.profile_path("anon").read_text() + json.dumps(v.describe(profile))
    for piece in ("192.0.2.10", "198.51.100.20", FP_A, CERT_A, CERT_B):
        assert piece not in shown
    assert "2 bridges (obfs4)" in shown and "you can delete" in shown
    data = v.payload(profile, "serve")
    assert data["secrets"] == {"tor_bridges": stored.strip()} and "192.0.2.10" not in json.dumps(data["profile"])
    assert v.main(["remove", "anon"]) == 0 and not (secrets_dir(home) / "VPN_ANON_BRIDGES").exists()


def test_bridges_from_a_secret_stay_when_the_profile_goes(home):
    with pytest.raises(ValueError, match="no secret MY_BRIDGES"):
        v.main(["add", "anon", "--tor", "--bridge-secret", "MY_BRIDGES"])
    v.secret_store().FileStore().set("MY_BRIDGES", BRIDGES)
    assert v.main(["add", "anon", "--tor", "--bridge-secret", "MY_BRIDGES"]) == 0
    profile = v.load_profile("anon")
    assert profile["tor"]["bridges"] == 2 and profile["tor"]["bridges_secret"] == "MY_BRIDGES"
    assert not profile["tor"]["bridges_secret_owned"]
    assert v.payload(profile, "check")["secrets"] == {"tor_bridges": BRIDGES.strip()}
    v.main(["remove", "anon"])
    assert v.secret_store().FileStore().get("MY_BRIDGES") == BRIDGES.strip()  # yours, not the profile's


@pytest.mark.parametrize("options, message", [
    (["--exit-country", "deu"], "two-letter country codes"),
    (["--exit-country", "d1"], "two-letter country codes"),
    (["--dns", "9.9.9.9"], "tor's own DNSPort"),
])
def test_tor_options_that_are_refused(home, options, message):
    with pytest.raises(ValueError, match=message):
        v.main(["add", "anon", "--tor", *options])
    assert not v.profile_path("anon").exists()


def test_tor_options_belong_to_tor(home, tmp_path):
    source = tmp_path / "x.conf"
    source.write_text("[Interface]\n")
    for options in (["--socks"], ["--exit-country", "de"], ["--bridge-secret", "X"]):
        with pytest.raises(ValueError, match="are for --tor"):
            v.main(["add", "corp", "--wireguard", str(source), *options])
    with pytest.raises(SystemExit):  # one source of bridges
        v.main(["add", "anon", "--tor", "--bridges", "a", "--bridge-secret", "B"])


@pytest.mark.parametrize("text, message", [
    ("snowflake 192.0.2.3:1 " + FP_A, "snowflake"),
    ("meek_lite 192.0.2.3:1 " + FP_A, "is not supported"),
    (f"obfs4 [2001:db8::1]:443 {FP_A} cert={CERT_A} iat-mode=0", "IPv6 bridges"),
    (f"obfs4 192.0.2.10 {FP_A} cert={CERT_A} iat-mode=0", "IPV4:PORT"),
    (f"obfs4 192.0.2.10:70000 {FP_A} cert={CERT_A} iat-mode=0", "port"),
    (f"obfs4 127.0.0.1:443 {FP_A} cert={CERT_A} iat-mode=0", "not a bridge's address"),
    (f"obfs4 192.0.2.10:443 {FP_A} iat-mode=0", "cert= and iat-mode="),
    (f"obfs4 192.0.2.10:443 {FP_A} cert={CERT_A} iat-mode=0 ExitNodes=x;y", "KEY=VALUE"),
    (f"192.0.2.10:443 {FP_A} cert={CERT_A}", "options belong to a transport"),
    # Bypass: a torrc directive smuggled in as a bridge line.
    ("ControlPort 9051", "is not supported"),
    ("# only a comment\n", "no bridge line"),
])
def test_bridge_lines_that_are_refused_without_echoing_them(home, text, message):
    path = bridges_file(home, text)
    with pytest.raises(ValueError, match=message) as error:
        v.main(["add", "anon", "--tor", "--bridges", str(path)])
    assert "192.0.2.10" not in str(error.value) and FP_A not in str(error.value) and CERT_A not in str(error.value)
    assert not (secrets_dir(home) / "VPN_ANON_BRIDGES").exists()


def test_a_bridges_file_others_can_read_is_refused(home):
    with pytest.raises(ValueError, match="chmod 600"):
        v.main(["add", "anon", "--tor", "--bridges", str(bridges_file(home, mode=0o644))])


def test_plain_bridges_without_a_transport(home):
    bridges = rt.parse_bridges(f"203.0.113.9:9001 {FP_A.lower()}\n203.0.113.10:443\n")
    assert [rt.bridge_line(b) for b in bridges] == [f"Bridge 203.0.113.9:9001 {FP_A}", "Bridge 203.0.113.10:443"]
    assert rt.parse_bridges("") == []
    with pytest.raises(ValueError, match="more than 32"):
        rt.parse_bridges("\n".join(f"203.0.113.{n}:443" for n in range(1, 40)))


# --- the generated torrc -----------------------------------------------------------------------------------------

def test_torrc_without_bridges():
    text = rt.tor_config(tor_profile()["tor"])
    lines = text.splitlines()
    for line in (f"User {rt.TOR_USER}", "DataDirectory /var/lib/tor", "Log notice stdout", "SafeLogging 1",
                 "ClientOnly 1", "ClientUseIPv6 0", "ClientPreferIPv6ORPort 0",
                 f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT} IsolateDestAddr", f"DNSPort {rt.TOR_DNS}:53",
                 "AutomapHostsOnResolve 1", "AutomapHostsSuffixes .onion",
                 f"VirtualAddrNetworkIPv4 {rt.TOR_VIRTUAL_NET}", "SocksPort 0", "ControlPort 0"):
        assert line in lines, line
    assert "ExitNodes" not in text and "UseBridges" not in text and "Bridge " not in text
    assert rt.TOR_VIRTUAL_NET == "10.192.0.0/10" and ipaddress.ip_network(rt.TOR_VIRTUAL_NET)


def test_torrc_with_socks_exit_countries_and_bridges():
    bridges = rt.parse_bridges(BRIDGES)
    text = rt.tor_config(tor_profile(socks=True, exit_country=["de", "nl"], bridges=2)["tor"], bridges)
    lines = text.splitlines()
    assert f"SocksPort 127.0.0.1:{rt.TOR_SOCKS_PORT} IsolateDestAddr" in lines and "SocksPort 0" not in lines
    assert "ExitNodes {de},{nl}" in lines and "StrictNodes 1" in lines
    assert "UseBridges 1" in lines and "ClientTransportPlugin obfs4 exec /usr/bin/obfs4proxy" in lines
    assert f"Bridge obfs4 192.0.2.10:443 {FP_A} cert={CERT_A} iat-mode=0" in lines
    assert f"Bridge obfs4 198.51.100.20:9001 {FP_B} cert={CERT_B} iat-mode=1" in lines
    # Bypass: a country code that would carry another directive into the torrc.
    with pytest.raises(ValueError, match="country"):
        rt.tor_config(tor_profile(exit_country=["de}\nControlPort 9051"])["tor"])


# --- the kill switch and the redirects ------------------------------------------------------------------------

def chain(rules, name):
    return rules.split(f" chain {name} ")[1].split("\n }\n")[0]


def test_kill_switch_lets_only_tors_own_tcp_out():
    rules = rt.tor_rules()
    output = chain(rules, "output").splitlines()
    assert "policy drop" in output[0] and output[-1] == "  reject with icmpx type admin-prohibited"
    assert '  oifname "lo" accept' in output
    assert f"  meta skuid {rt.TOR_UID} meta nfproto ipv4 meta l4proto tcp accept" in output
    # Everything redirected to tor's ports on loopback passes; nothing else but tor's own connections.
    assert "  ct status dnat ip daddr 127.0.0.0/8 accept" in output
    accepts = [line for line in output if line.endswith("accept")]
    assert all("lo" in line or "dnat" in line or f"skuid {rt.TOR_UID}" in line for line in accepts)
    # Bypass: IPv6 (Tor's TransPort is IPv4 here). Nothing of it leaves, not even tor's own.
    assert output.index("  meta nfproto ipv6 reject with icmpx type admin-prohibited") < \
        output.index(f"  meta skuid {rt.TOR_UID} meta nfproto ipv4 meta l4proto tcp accept")
    # Bypass: UDP (QUIC, a DNS resolver of the agent's choosing): no accept names it; the policy drops it.
    assert "udp" not in chain(rules, "output")
    assert "ct state" not in chain(rules, "output")
    assert "forward priority -5; policy drop" in rules


def test_every_tcp_connection_and_every_lookup_goes_to_tor():
    divert = chain(rt.tor_rules(), "divert").splitlines()
    assert "type nat hook output priority -90" in divert[0]  # after the agent's own redirect to its proxy (-100)
    assert divert[1:] == [f"  meta skuid {rt.TOR_UID} return",
                          "  ip daddr 127.0.0.0/8 return",
                          "  meta nfproto ipv6 return",
                          f"  udp dport 53 dnat ip to {rt.TOR_DNS}:53",
                          f"  meta l4proto tcp redirect to :{rt.TOR_TRANS_PORT}"]


def test_with_bridges_tor_reaches_only_the_bridges():
    output = chain(rt.tor_rules(bridges=rt.parse_bridges(BRIDGES)), "output")
    assert f"  meta skuid {rt.TOR_UID} ip daddr 192.0.2.10 tcp dport 443 accept" in output
    assert f"  meta skuid {rt.TOR_UID} ip daddr 198.51.100.20 tcp dport 9001 accept" in output
    assert "meta l4proto tcp accept" not in output


def test_the_agents_firewall_hands_only_the_proxys_connections_to_tor():
    closed = v.tor_agent_rules(dns_open=False, proxy_uid=s.PROXY_UID)
    assert closed == [f"ct status dnat ip daddr 127.0.0.1 tcp dport {rt.TOR_TRANS_PORT} meta skuid {s.PROXY_UID} accept",
                      f"ip daddr 127.0.0.1 tcp dport {rt.TOR_TRANS_PORT} drop", f"ip daddr {rt.TOR_DNS} drop"]
    assert v.tor_agent_rules(dns_open=True, proxy_uid=s.PROXY_UID) == closed[:2]
    rules = s.firewall_rules([], ["203.0.113.5"], proxy={"port": s.PROXY_PORT, "loopback": []},
                             vpn_mark=f"meta skuid {rt.TOR_UID}", vpn_rules=closed)
    output = chain(rules, "output").splitlines()
    # Bypass: the agent talking to tor's ports on loopback directly. The drops come before "oifname lo accept".
    lo = output.index('  oifname "lo" accept')
    assert all(output.index(f"  {rule}") < lo for rule in closed)
    assert f"  meta skuid {rt.TOR_UID} accept" in output and f"  meta skuid {rt.TOR_UID} return" in rules
    with pytest.raises(ValueError, match="firewall rule"):
        s.firewall_rules([], [], vpn_rules=["accept\n }\n chain x {"])


# --- the bootstrap ----------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, log, lines):
        self.now, self.log, self.lines = 0.0, log, list(lines)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.lines:
            with open(self.log, "a") as stream:
                stream.write(self.lines.pop(0) + "\n")


def progress_lines(*percents):
    return [f"Nov 06 12:00:0{i}.000 [notice] Bootstrapped {p}% (step_{p}): Step {p}" for i, p in enumerate(percents)]


def test_bootstrap_waits_for_100_percent(tmp_path):
    log = tmp_path / "tor.log"
    log.write_text("Nov 06 12:00:00.000 [notice] Tor 0.4.8 opening log file.\n")
    clock, out = Clock(log, progress_lines(5, 50, 50, 100)), []
    stream = types.SimpleNamespace(write=out.append, flush=lambda: None)
    process = types.SimpleNamespace(poll=lambda: None, returncode=None)
    assert rt.wait_bootstrap(log, process, 60, stream, clock=clock, sleep=clock.sleep) == 100
    messages = [json.loads(line)["message"] for line in out]
    assert messages == ["tor: bootstrapped 0%", "tor: bootstrapped 5% (Step 5)", "tor: bootstrapped 50% (Step 50)",
                        "tor: bootstrapped 100% (Step 100)"]
    assert rt.bootstrap_progress("x\nBootstrapped 100% (done): Done\n") == (100, "Done")
    assert rt.bootstrap_progress("nothing yet") == (0, None)


def test_bootstrap_timeout_says_how_far_it_got_without_the_bridges(tmp_path):
    log = tmp_path / "tor.log"
    log.write_text("")
    clock = Clock(log, progress_lines(10) + ["[warn] Problem bootstrapping. Stuck at 10%: no route to 192.0.2.10:443"])
    process = types.SimpleNamespace(poll=lambda: None, returncode=None)
    with pytest.raises(RuntimeError, match=r"within 30 s \(it got to 10%: Step 10\).*--bridges") as error:
        rt.wait_bootstrap(log, process, 30, types.SimpleNamespace(write=lambda t: None, flush=lambda: None),
                          secrets=["192.0.2.10:443", "192.0.2.10"], clock=clock, sleep=clock.sleep)
    assert "192.0.2.10" not in str(error.value) and "[secret]" in str(error.value)


def test_bootstrap_stops_when_tor_exits(tmp_path):
    log = tmp_path / "tor.log"
    log.write_text("[err] Reading config failed--see warnings above.\n")
    process = types.SimpleNamespace(poll=lambda: 1, returncode=1)
    with pytest.raises(RuntimeError, match=r"(?s)tor exited \(1\) at 0%.*Reading config failed"):
        rt.wait_bootstrap(log, process, 30, types.SimpleNamespace(write=lambda t: None, flush=lambda: None),
                          clock=lambda: 0.0, sleep=lambda seconds: None)


class FakeRunner:
    def __init__(self):
        self.commands, self.children = [], []

    def run(self, command, data=None, check=True):
        self.commands.append((command, data))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def spawn(self, command, log):
        self.commands.append((command, None))
        process = types.SimpleNamespace(args=command, poll=lambda: None, returncode=None)
        self.children.append(process)
        return process


def test_tor_comes_up_behind_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN", tmp_path / "vpn")
    given = []
    monkeypatch.setattr(rt, "own_data_directory", lambda uid: given.append(uid))
    waited = []
    monkeypatch.setattr(rt, "wait_bootstrap", lambda log, process, timeout, out, secrets: waited.append(secrets) or 100)
    runner = FakeRunner()
    stored = "\n".join(rt.bridge_line(b) for b in rt.parse_bridges(BRIDGES)) + "\n"
    payload = {"profile": tor_profile(socks=True, bridges=2, exit_country=["de"]), "secrets": {"tor_bridges": stored}}
    tunnel = rt.up_tor(payload, runner, uid_of=lambda: rt.TOR_UID)
    commands = [c for c, _ in runner.commands]
    # The kill switch is in place before tor starts.
    assert commands.index(["nft", "-f", "-"]) < next(i for i, c in enumerate(commands) if c[0] == "tor")
    rules = dict((tuple(c), d) for c, d in runner.commands)[("nft", "-f", "-")]
    assert f"meta skuid {rt.TOR_UID} ip daddr 192.0.2.10 tcp dport 443 accept" in rules
    tor = next(c for c in commands if c[0] == "tor")
    assert given == [rt.TOR_UID]  # the data directory is debian-tor's before tor starts
    assert tor == ["tor", "-f", str(tmp_path / "vpn" / "torrc"), "--defaults-torrc", str(tmp_path / "vpn" / "torrc-defaults")]
    torrc = tmp_path / "vpn" / "torrc"
    assert stat.S_IMODE(torrc.stat().st_mode) == 0o600 and f"cert={CERT_A}" in torrc.read_text()
    # Bridge lines only in the private torrc, never on a command line.
    assert not any("192.0.2.10" in " ".join(c) for c in commands)
    assert "192.0.2.10" in waited[0] and CERT_A in waited[0]  # what the bootstrap's messages hide
    assert tunnel["dns"] == [rt.TOR_DNS] and tunnel["full4"] and not tunnel["ipv6"] and not tunnel["full6"]
    assert tunnel["endpoints"] == ["2 bridges"] and tunnel["tor"]["socks"] == f"127.0.0.1:{rt.TOR_SOCKS_PORT}"
    assert "192.0.2.10" not in json.dumps(tunnel)
    with pytest.raises(RuntimeError, match="rebuild it"):
        rt.up_tor(payload, FakeRunner(), uid_of=lambda: 107)
    with pytest.raises(ValueError, match="none arrived"):
        rt.up_tor({**payload, "secrets": {}}, FakeRunner(), uid_of=lambda: rt.TOR_UID)


def test_the_runtime_dispatches_tor_and_hides_its_bridges(monkeypatch):
    out = []
    stream = types.SimpleNamespace(write=out.append, flush=lambda: None)

    def boom(payload, runner, out=None):
        raise RuntimeError("tor: could not connect to 192.0.2.10:443 (" + FP_A + ")")
    monkeypatch.setattr(rt, "up_tor", boom)
    payload = {"profile": tor_profile(bridges=2), "secrets": {"tor_bridges": BRIDGES}}
    assert rt.main(["check"], types.SimpleNamespace(readline=lambda: json.dumps(payload)), stream) == 1
    event = json.loads(out[-1])
    assert event["event"] == "error" and "192.0.2.10" not in event["message"] and FP_A not in event["message"]


# --- the checks ------------------------------------------------------------------------------------------------

def test_the_echo_reads_check_torprojects_answer():
    assert rt.parse_echo(b'{"IsTor":true,"IP":"185.220.101.5"}') == ("185.220.101.5", True)
    assert rt.parse_echo(b'{"IsTor":false,"IP":"198.18.0.5"}') == ("198.18.0.5", False)


def test_checks_from_inside_ask_tors_dnsport_and_an_outside_resolver(monkeypatch):
    asked = []

    def lookup(name, server, qtype, timeout=4.0, port=53):
        asked.append((name, server))
        if name.endswith(".onion"):
            return ONION
        return ["116.202.120.181"]
    monkeypatch.setattr(rt, "dns_lookup", lookup)
    monkeypatch.setattr(rt, "probe_ipv6", lambda timeout: {"error": "Operation not permitted", "kind": "blocked"})
    url = rt.TOR_CHECK
    result = rt.gather_checks({"checks": {"urls": [url], "dns_probe": None, "timeout": 2}}, TOR_TUNNEL, FakeRunner(),
                              lambda url, address, timeout: (200, b'{"IsTor":true,"IP":"185.220.101.5"}'))
    assert result["ipv4"] == {"ip": "185.220.101.5", "vpn": True, "url": url}
    assert result["dns"]["routes"] == {} and result["dns"]["answers"] == {rt.TOR_DNS: True}
    assert result["dns"]["onion"] == {"tor": ONION, "outside": ONION}
    assert (rt.ONION_PROBE, rt.TOR_DNS) in asked and (rt.ONION_PROBE, rt.OUTSIDE_RESOLVER) in asked
    # IPv6 through Tor's resolver has no AAAA to ask for: a literal IPv6 address must be unreachable instead.
    assert result["ipv6"] == {"error": "Operation not permitted", "kind": "blocked"}


def test_the_ipv6_probe_of_a_tor_connection():
    def refused(address, timeout):
        raise PermissionError(1, "Operation not permitted")
    assert rt.probe_ipv6(2, refused)["kind"] == "blocked"

    def unreachable(address, timeout):
        raise OSError(101, "Network is unreachable")
    assert rt.probe_ipv6(2, unreachable)["kind"] == "blocked"
    closed = []
    connected = rt.probe_ipv6(2, lambda address, timeout: types.SimpleNamespace(close=lambda: closed.append(address)))
    assert connected["ip"] == rt.IPV6_PROBE and closed == [(rt.IPV6_PROBE, 443)]
    # Connected over IPv6 although Tor carries none: that is the leak the evaluation reports.
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, tor_inside(ipv6=connected), HOST)
    assert any("although the VPN carries no IPv6" in failure for failure in verdict["failures"])


def tor_inside(ipv4=None, ipv6=None, dns=None):
    return {"ipv4": ipv4 or {"ip": "185.220.101.5", "vpn": True, "url": rt.TOR_CHECK},
            "ipv6": ipv6 or {"error": "Operation not permitted", "kind": "blocked"},
            "dns": {"servers": [rt.TOR_DNS], "configured": True, "routes": {}, "answers": {rt.TOR_DNS: True},
                    "egress": ["185.220.101.6"], "onion": {"tor": ONION, "outside": ONION}, **(dns or {})}}


def test_a_clean_tor_connection_passes():
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, tor_inside(), HOST)
    assert verdict["failures"] == [] and verdict["warnings"] == []
    assert "IPv4: 185.220.101.5 seen by check.torproject.org (a Tor exit, says the Tor Project)" in verdict["notes"]
    assert "IPv6: blocked (the VPN carries none)" in verdict["notes"]


@pytest.mark.parametrize("case, expected", [
    ({"ipv4": {"ip": "198.18.0.5", "vpn": False, "url": rt.TOR_CHECK}}, "this host's own address 198.18.0.5"),
    ({"ipv4": {"ip": "203.0.113.50", "vpn": False, "url": rt.TOR_CHECK}}, "the Tor Project says 203.0.113.50 is not a Tor exit"),
    ({"ipv4": {"ip": "203.0.113.50", "vpn": None, "url": "https://api64.ipify.org/"}}, "did not confirm that 203.0.113.50 is a Tor exit"),
    ({"ipv4": {"error": "timed out", "kind": "blocked"}}, "IPv4: no connection through the VPN"),
    ({"ipv6": {"ip": "2001:db8:aa::1", "vpn": None, "url": rt.TOR_CHECK}}, "although the VPN carries no IPv6"),
    ({"dns": {"answers": {rt.TOR_DNS: "timed out"}}}, f"the resolver {rt.TOR_DNS} does not answer"),
    ({"dns": {"onion": {"tor": "timed out", "outside": ONION}}}, "tor's DNSPort"),
    # Bypass: a resolver outside Tor still answers (the redirect is missing).
    ({"dns": {"onion": {"tor": ONION, "outside": []}}}, f"a lookup sent to {rt.OUTSIDE_RESOLVER}"),
    ({"dns": {"onion": {"tor": ONION, "outside": ["198.18.7.7"]}}}, f"a lookup sent to {rt.OUTSIDE_RESOLVER}"),
    ({"dns": {"egress": ["198.18.0.5"]}}, "DNS leak"),
])
def test_what_stops_a_tor_start(case, expected):
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, tor_inside(**case), HOST)
    assert any(expected in failure for failure in verdict["failures"]), verdict


class FakeTunnel:
    def __init__(self, command, data, say):
        self.data, self.closed = data, False
        FakeTunnel.last = self

    def wait_ready(self, timeout=0):
        FakeTunnel.timeout = timeout
        return {"tunnel": TOR_TUNNEL, "checks": FakeTunnel.inside, "hosts": {}}

    def follow(self, on_event, on_end=None):
        pass

    def close(self):
        self.closed = True


def test_failed_checks_stop_unless_warn_only(home, monkeypatch):
    v.main(["add", "anon", "--tor"])
    profile = v.load_profile("anon")
    monkeypatch.setattr(v, "Tunnel", FakeTunnel)
    monkeypatch.setattr(v, "host_baseline", lambda settings: HOST)
    said = []
    FakeTunnel.inside = tor_inside()
    v.bring_up(profile, ["podman"], (), said.append)
    assert FakeTunnel.timeout == v.READY_TIMEOUT + rt.BOOTSTRAP_TIMEOUT  # tor's bootstrap takes its time
    assert f"anon: tor (tor, TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}) is up; endpoints the Tor network; kill switch on" \
        in said
    FakeTunnel.inside = tor_inside(ipv4={"ip": "203.0.113.50", "vpn": False, "url": rt.TOR_CHECK})
    with pytest.raises(ValueError, match="checks failed.*not a Tor exit"):
        v.bring_up(profile, ["podman"], (), said.append)
    assert FakeTunnel.last.closed
    said.clear()
    v.bring_up(profile, ["podman"], (), said.append, warn_only=True)
    assert any(line.startswith("FAILED: IPv4: the Tor Project says") for line in said)


def test_tors_check_settings(home):
    settings = v.check_settings(tor_profile())
    assert settings["urls"] == [rt.TOR_CHECK] and settings["timeout"] == 30


def test_the_tor_container(home):
    command = v.container_command(["podman"], "agent-x-vpn", tor_profile(), pod="agent-x")
    assert "--cap-drop=ALL" in command and "--cap-add=NET_ADMIN" in command
    # tor binds port 53 on loopback, then becomes debian-tor (whose uid the kill switch knows).
    for cap in ("SETUID", "SETGID", "NET_BIND_SERVICE", "CHOWN"):
        assert f"--cap-add={cap}" in command
    assert "--cap-add=NET_RAW" not in command and "--device=/dev/net/tun" not in command
    # Podman's --tmpfs knows no uid= or gid= ("unknown mount option"): the runtime gives it to debian-tor.
    tmpfs = [c for c in command if c.startswith("--tmpfs=/var/lib/tor")]
    assert tmpfs == ["--tmpfs=/var/lib/tor:rw,nosuid,nodev,noexec,mode=0700"]
    assert not any("uid=" in c or "gid=" in c for c in command if c.startswith("--tmpfs"))
    # Tor is not a VPN: its own image, never the VPN image.
    assert command[-4:] == [v.TOR_IMAGE, "-I", "/vpn/vpn_runtime.py", "serve"] and v.VPN_IMAGE not in command
    assert v.image(tor_profile()) == v.TOR_IMAGE and v.build_command(tor_profile()) == "sandbox-ai build tor"
    assert v.mark_expression(tor_profile()) == f"meta skuid {rt.TOR_UID}"


def test_the_tor_image_has_tor_with_its_fixed_uid():
    recipe = (ROOT / "sandbox" / "Containerfile.tor").read_text()
    for part in (" tor ", "tor-geoipdb", "obfs4proxy", f"--uid {rt.TOR_UID}", "--home-dir /var/lib/tor", "nftables",
                 "iproute2", "python3", "LABEL sandbox-ai.role=tor", '"/vpn/vpn_runtime.py"'):
        assert part in recipe, part
    # The user exists before the package, which keeps an existing debian-tor (a system uid).
    assert recipe.index(f"--uid {rt.TOR_UID}") < recipe.index("tor-geoipdb")
    assert 100 <= rt.TOR_UID <= 999 and rt.TOR_UID != s.PROXY_UID


def test_tor_and_the_vpn_share_no_image():
    """Tor is not a VPN: the Tor image has no VPN software, the VPN image no Tor (and no debian-tor)."""
    instructions = lambda name: "\n".join(  # noqa: E731 - the recipe without its comments
        line for line in (ROOT / "sandbox" / name).read_text().splitlines() if not line.lstrip().startswith("#"))
    tor, vpn = instructions("Containerfile.tor"), instructions("Containerfile.vpn")
    for part in ("wireguard", "openvpn", "tailscale", "golang"):
        assert part not in tor.lower(), part
    for part in (" tor ", "tor-geoipdb", "obfs4proxy", "debian-tor", str(rt.TOR_UID)):
        assert part not in vpn, part


# --- the plan, MCP servers ------------------------------------------------------------------------------------

def test_the_plan_shows_tor_never_a_bridge(home):
    bridges_path = bridges_file(home)
    v.main(["add", "anon", "--tor", "--socks", "--bridges", str(bridges_path)])
    a = argparse.Namespace(vpn="anon", vpn_profile=v.load_profile("anon"), vpn_check="stop")
    plan = v.plan(a)
    assert plan["type"] == "tor" and plan["transparent"] is True and plan["dns"] == [rt.TOR_DNS]
    assert plan["socks"].startswith(f"127.0.0.1:{rt.TOR_SOCKS_PORT}") and plan["bridges"] == 2
    assert f"meta skuid {rt.TOR_UID}" in plan["kill_switch"] and "IPv6" in plan["kill_switch"]
    assert plan["checks"]["ip_echo"] == [rt.TOR_CHECK] and "100%" in plan["checks"]["tor"]
    text = json.dumps(plan)
    for piece in ("192.0.2.10", "198.51.100.20", FP_A, CERT_A, "VPN_ANON_BRIDGES"):
        assert piece not in text


def test_mcp_servers_take_a_tor_profile(home, monkeypatch):
    v.main(["add", "anon", "--tor"])
    mcp = load("sandbox_mcp_tor_test", "mcp.py")
    image = "ghcr.io/a/b@sha256:" + "0" * 64
    assert mcp.validated_image({"image": image, "network": "full", "vpn": "anon"}, "t")["vpn"] == "anon"
    monkeypatch.setattr(v, "Tunnel", FakeTunnel)
    monkeypatch.setattr(v, "host_baseline", lambda settings: HOST)
    def podman(command, **kw):
        if command[1:3] == ["container", "inspect"] and "{{json .Config.Labels}}" not in command:
            return types.SimpleNamespace(returncode=0, stderr="", stdout="|".join(
                ["e" * 64, "true", "2026-11-12 10:00:00 +0000 UTC", "pasta", "anon"]) + "\n")
        if command[1] == "exec":  # `nft list table inet sandbox_ai_vpn` in the Tor container: the kill switch
            return types.SimpleNamespace(returncode=0, stderr="", stdout="table inet sandbox_ai_vpn {\n" + "".join(
                f" chain {h} {{ type filter hook {h} priority -5; policy drop;\n }}\n"
                for h in ("output", "input", "forward")) + "}\n")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(v, "podman_run", podman)
    FakeTunnel.inside = tor_inside()
    assert v.mcp_wrapper("anon", "sandbox-ai-vpn-mcp-x", "/owner", [sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert FakeTunnel.last.data["profile"]["type"] == "tor" and FakeTunnel.last.closed
    assert FakeTunnel.last.data["mcp"] is True  # (Tor's own rules hand its lookups to the DNSPort)


def test_doctor_does_not_ask_tor_for_a_tunnel_device(home, tmp_path, monkeypatch):
    """Tor has no tunnel device: with only tor profiles /dev/net/tun is not needed; a WireGuard one still needs it."""
    doctor = load("sandbox_doctor_tor_test", "doctor.py")
    assert v.main(["add", "anon", "--tor"]) == 0
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(doctor, "run", lambda command, timeout=30: (0, ""))
    assert doctor.check_vpn(tun=tmp_path / "no-tun")[0] == "ok"
    conf = tmp_path / "wg.conf"
    key = "a" * 42 + "A="
    conf.write_text(f"[Interface]\nPrivateKey = {key}\nAddress = 10.66.0.2/32\nDNS = 10.66.0.1\n\n[Peer]\n"
                    f"PublicKey = {key}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.7:51820\n")
    conf.chmod(0o600)
    assert v.main(["add", "wg", "--wireguard", str(conf)]) == 0
    assert doctor.check_vpn(tun=tmp_path / "no-tun")[:3] == ("fail", "VPN", "/dev/net/tun is missing")


def test_doctor_checks_the_images_the_profiles_need(home, tmp_path, monkeypatch):
    """Tor has an image of its own: a Tor profile needs localhost/sandbox-ai-tor, a WireGuard one the VPN image;
    the hint names the build of each image missing."""
    doctor = load("sandbox_doctor_tor_images_test", "doctor.py")
    assert v.main(["add", "anon", "--tor"]) == 0
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    present, asked = set(), []

    def run(command, timeout=30):
        asked.append(command[-1])
        return (0 if command[-1] in present else 1), ""
    monkeypatch.setattr(doctor, "run", run)
    assert doctor.check_vpn(tun=tmp_path / "no-tun") == ("warn", "VPN", f"{v.TOR_IMAGE} missing", "sandbox-ai build tor")
    assert asked == [v.TOR_IMAGE]  # only Tor profiles: the VPN image is not needed
    present.add(v.TOR_IMAGE)
    status, _, detail, _ = doctor.check_vpn(tun=tmp_path / "no-tun")
    assert status == "ok" and "Tor only" in detail and "WireGuard" not in detail
    conf = tmp_path / "wg.conf"
    key = "a" * 42 + "A="
    conf.write_text(f"[Interface]\nPrivateKey = {key}\nAddress = 10.66.0.2/32\n\n[Peer]\n"
                    f"PublicKey = {key}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.7:51820\n")
    conf.chmod(0o600)
    assert v.main(["add", "wg", "--wireguard", str(conf)]) == 0
    assert doctor.check_vpn(tun=tmp_path / "no-tun") == ("warn", "VPN", f"{v.VPN_IMAGE} missing", "sandbox-ai build vpn")
    present.clear()
    assert doctor.check_vpn(tun=tmp_path / "no-tun") == (
        "warn", "VPN", f"{v.TOR_IMAGE} and {v.VPN_IMAGE} missing", "sandbox-ai build tor; sandbox-ai build vpn")


def test_a_tor_start_needs_the_tor_image(home, monkeypatch):
    """The start asks for the image its profile runs in and names the build that makes it."""
    v.main(["add", "anon", "--tor"])
    asked = []

    def podman_run(command, **kwargs):
        asked.append(command)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(v, "podman_run", podman_run)
    launcher = types.SimpleNamespace(podman_command=lambda a: ["podman"], printable=str)
    monkeypatch.setattr(v, "launcher", lambda: launcher)
    a = argparse.Namespace(vpn="anon", vpn_profile=v.load_profile("anon"), vpn_check="stop")
    with pytest.raises(ValueError, match=r"The Tor image localhost/sandbox-ai-tor:latest is missing; build it with: "
                                         r"sandbox-ai build tor"):
        v.agent_up(a, "session", "/home", [], contextlib.ExitStack())
    assert asked == [["podman", "image", "exists", v.TOR_IMAGE]]
    assert v.plan(a)["image"] == v.TOR_IMAGE


@pytest.mark.parametrize("user, refused", [("950", True), ("950:950", True), ("debian-tor", True), ("tor", True),
                                           ("", False), ("root", False), ("10001", False), ("node:node", False)])
def test_an_image_running_as_tors_user_cannot_go_behind_tor(user, refused):
    """Tor's kill switch lets tor's own uid out past Tor; an MCP image that runs as it would bypass the tunnel (it
    shares the VPN container's network namespace and cannot change its uid: no capabilities)."""
    import subprocess
    mcp = load("sandbox_mcp_tor_test", "mcp.py")
    inspect = lambda: subprocess.CompletedProcess([], 0, user + "\n", "")  # noqa: E731
    passwd = lambda: "root:x:0:0::/root:/bin/sh\nnode:x:1000:1000::/home/node:/bin/sh\n"  # noqa: E731
    if refused:
        with pytest.raises(ValueError, match="the user tor's kill switch lets out past Tor"):
            mcp.check_tor_image_user("osint", "ghcr.io/x/osint@sha256:0", inspect, passwd)
    else:
        mcp.check_tor_image_user("osint", "ghcr.io/x/osint@sha256:0", inspect, passwd)
    # The user cannot be told: refused (it could be tor's). Before, an inspect that failed or timed out (a busy
    # Podman) let the server go behind Tor unchecked, though its image then ran as whatever user it names.
    failed = lambda: subprocess.CompletedProcess([], 125, "", "no such image")  # noqa: E731
    with pytest.raises(ValueError, match="cannot tell which user its image runs as.*no such image"):
        mcp.check_tor_image_user("osint", "missing", failed)

    def timed_out():
        raise subprocess.TimeoutExpired(["podman", "image", "inspect"], 30)
    with pytest.raises(ValueError, match="cannot tell which user its image runs as"):
        mcp.check_tor_image_user("osint", "ghcr.io/x/osint@sha256:0", timed_out, passwd)


def test_an_image_user_whose_uid_is_tors_cannot_go_behind_tor():
    """Bypass: an image whose USER is an innocent name that its own /etc/passwd maps to uid 950 (tor's): its packets
    would pass the kill switch as tor's. The uid is read from the image's passwd; an unknown one is refused too."""
    import subprocess
    mcp = load("sandbox_mcp_tor_uid_test", "mcp.py")
    inspect = lambda: subprocess.CompletedProcess([], 0, "scraper\n", "")  # noqa: E731
    with pytest.raises(ValueError, match=r"'scraper' \(uid 950\).*past Tor"):
        mcp.check_tor_image_user("osint", "img", inspect, lambda: "scraper:x:950:950::/:/bin/sh\n")
    with pytest.raises(ValueError, match="whose uid its /etc/passwd does not say"):
        mcp.check_tor_image_user("osint", "img", inspect, lambda: None)
    mcp.check_tor_image_user("osint", "img", inspect, lambda: "scraper:x:1001:1001::/:/bin/sh\n")


def test_the_image_passwd_is_read_without_starting_the_image(tmp_path):
    import io
    import subprocess
    import tarfile
    mcp = load("sandbox_mcp_tor_passwd_test", "mcp.py")
    data = b"app:x:950:950::/:/bin/sh\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("passwd")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    commands = []

    def run(command):
        commands.append(command)
        if command[1] == "create":
            return subprocess.CompletedProcess(command, 0, b"c0ffee\n", b"")
        if command[1] == "cp":
            return subprocess.CompletedProcess(command, 0, buffer.getvalue(), b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")
    assert mcp.image_passwd("img", run) == data.decode()
    assert commands[0][:2] == ["podman", "create"] and "--network=none" in commands[0]
    assert ["podman", "cp", "c0ffee:/etc/passwd", "-"] in commands and ["podman", "rm", "--force", "c0ffee"] in commands
    assert not any(command[1] in ("run", "start") for command in commands)
    assert mcp.image_passwd("img", lambda command: subprocess.CompletedProcess(command, 125, b"", b"")) is None


def test_the_image_passwd_is_read_bounded_through_the_one_shared_reader(monkeypatch):
    """mcp.image_passwd reads as services.image_file reads (one helper): only one regular file of at most 1 MiB, a
    directory or more than that is no answer, and the copy is streamed and cut off (services.copy_bounded) - an image
    behind Tor whose /etc/passwd is gigabytes never fills the launcher's memory."""
    import io
    import subprocess
    import tarfile
    mcp = load("sandbox_mcp_tor_passwd_bounded_test", "mcp.py")

    def stream(*members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                info.type, info.size = kind, len(data) if kind == tarfile.REGTYPE else 0
                archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return buffer.getvalue()

    def answering(data):
        def run(command):
            if command[1] == "create":
                return subprocess.CompletedProcess(command, 0, b"c0ffee\n", b"")
            if command[1] == "cp":
                return subprocess.CompletedProcess(command, 0, data, b"")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return run
    assert mcp.image_passwd("img", answering(stream(("passwd", tarfile.REGTYPE, b"a" * ((1 << 20) + 1))))) is None
    assert mcp.image_passwd("img", answering(stream(("passwd", tarfile.DIRTYPE, b""),
                                                    ("passwd/x", tarfile.REGTYPE, b"x\n")))) is None
    assert mcp.image_passwd("img", answering(stream(("passwd", tarfile.FIFOTYPE, b"")))) is None
    services = mcp.services_module()
    seen = []
    monkeypatch.setattr(services, "image_file", lambda *args, **kwargs: seen.append((args, kwargs)) or "root:x:0:0\n")
    assert mcp.image_passwd("img") == "root:x:0:0\n"
    (image, path, podman, *_), kwargs = seen[0]
    assert (image, path, podman) == ("img", "/etc/passwd", ["podman"]) and kwargs.get("run") is None, \
        "without a runner the copy streams through copy_bounded"


# --- the guarantee during the run --------------------------------------------------------------------------------

def test_an_exit_that_is_no_tor_exit_is_a_leak_and_ends_the_run(home, tmp_path):
    """Behind Tor any address the Tor Project does not know as an exit is traffic past Tor, whether it is this host's
    own or not (a home address that changed, a provider's NAT, another VPN on the host): a leak, which ends the run
    unless --vpn-check warn. Judged without asking check.torproject.org from this host's own address."""
    said, stopped, asked = [], [], []
    a = types.SimpleNamespace(log_dir=str(tmp_path), run_id="r1", vpn_check="stop")
    seen = tor_inside(ipv4={"ip": "203.0.113.50", "vpn": False, "url": rt.TOR_CHECK})
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, seen, {})
    assert verdict["leaks"] == ["IPv4: the Tor Project says 203.0.113.50 is not a Tor exit"]
    verdict = v.rechecked(a, tor_profile(), {"tunnel": TOR_TUNNEL}, {"checks": seen}, said.append,
                          baseline=lambda settings: asked.append(settings) or HOST, stop=lambda: stopped.append(1))
    assert verdict["leaks"] and stopped == [1] and asked == []
    assert "the run is stopped: traffic leaves past Tor (start with --vpn-check warn to only be told)" in said
    # A Tor exit: nothing to say, nothing stopped; a check that got no answer is said, not a leak (Tor is slow).
    stopped.clear()
    assert not v.rechecked(a, tor_profile(), {"tunnel": TOR_TUNNEL}, {"checks": tor_inside()}, said.append,
                           stop=lambda: stopped.append(1))["failures"]
    stalled = tor_inside(ipv4={"error": "timed out", "kind": "blocked"})
    verdict = v.rechecked(a, tor_profile(), {"tunnel": TOR_TUNNEL}, {"checks": stalled}, said.append,
                          stop=lambda: stopped.append(1))
    assert verdict["failures"] and not verdict["leaks"] and stopped == []


def test_tor_is_checked_again_soon_after_the_start_and_never_only_at_it(home):
    settings = v.check_settings(tor_profile())
    assert settings["recheck"] == rt.RECHECK and settings["first_recheck"] == rt.FIRST_RECHECK == 60
    checks = v.profiles_dir() / "checks.json"
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text(json.dumps({"recheck": 0}))  # "only at the start" does not hold for Tor
    checks.chmod(0o600)
    assert v.check_settings(tor_profile())["recheck"] == rt.RECHECK
    checks.write_text(json.dumps({"recheck": 3600}))
    assert v.check_settings(tor_profile())["first_recheck"] == 60
    assert "first_recheck" not in v.check_settings({"type": "wireguard", "name": "w"})


def test_the_checks_sockets_carry_their_mark(monkeypatch):
    """The VPN container marks the sockets of its checks and lookups (CHECK_MARK) once it serves, so that the agent's
    firewall in the namespace lets them pass to the tunnel; without CAP_NET_ADMIN (the baseline's plain container)
    they stay unmarked rather than fail."""
    marks = []

    class Socket:
        fail = False

        def __init__(self, family, kind):
            self.family, self.kind = family, kind

        def setsockopt(self, level, option, value):
            if self.fail:
                raise PermissionError(1, "Operation not permitted")
            marks.append((option, value))

        def settimeout(self, value):
            pass

        def connect(self, address):
            self.address = address

        def close(self):
            pass
    monkeypatch.setattr(rt.socket, "socket", Socket)
    monkeypatch.setattr(rt, "_check_mark", None)
    rt.check_connection(("203.0.113.5", 443), 5)
    assert marks == []  # not serving: no mark
    rt.use_check_mark()
    sock = rt.check_connection(("2001:db8::5", 443), 5)
    assert marks == [(getattr(rt.socket, "SO_MARK", 36), rt.CHECK_MARK)] and sock.address == ("2001:db8::5", 443)
    Socket.fail = True
    assert rt.check_socket(rt.socket.AF_INET, rt.socket.SOCK_DGRAM).kind == rt.socket.SOCK_DGRAM
    assert rt.CHECK_MARK not in (rt.MARK, rt.TAILSCALE_MARK[0]) and rt.CHECK_MARK & rt.TAILSCALE_MARK[1] == 0


def test_the_agents_firewall_lets_the_marked_checks_pass_before_its_own_rules():
    rules = s.firewall_rules([], [], proxy={"port": s.PROXY_PORT, "loopback": []}, vpn_mark=f"meta skuid {rt.TOR_UID}",
                             vpn_rules=v.tor_agent_rules(False, s.PROXY_UID), check_mark=rt.CHECK_MARK)
    output = chain(rules, "output").splitlines()
    assert output.index(f"  meta mark {rt.CHECK_MARK:#x} accept") < output.index(f"  ip daddr {rt.TOR_DNS} drop")
    proxy = chain(rules, "proxy").splitlines()
    assert proxy.index(f"  meta mark {rt.CHECK_MARK:#x} return") < next(
        i for i, line in enumerate(proxy) if "redirect to" in line)
    assert "meta mark" not in s.firewall_rules([], [])  # without a VPN: no exemption at all


# --- what a Tor start reaches past Tor: nothing, unless it says so -----------------------------------------------

def tor_args(*extra):
    v.main(["add", "anon", "--tor", "--force"])
    return s.parse_args(["codex", "--backend", "podman", "--vpn", "anon", *extra])


@pytest.mark.parametrize("extra, refused", [
    ([], None),
    (["--allow-host-port", "3000"], "the host's port 3000 would be reached past Tor"),
    (["--allow-host-port", "3000", "--allow-host-port", "11434"], "the host's ports 3000, 11434 would be reached"),
    (["--allow-host-port", "3000", "--vpn-bypass-host-ports"], None),
])
def test_a_tor_start_reaches_the_hosts_ports_only_when_it_says_so(home, extra, refused):
    """Bypass: a host port is pasta's forward to your loopback, outside the namespace and its kill switch; a local
    proxy, your own Tor's or VPN's SocksPort, a server that fetches URLs there would reach the internet from your
    own address."""
    a = tor_args(*extra)
    if refused:
        with pytest.raises(ValueError, match=refused):
            v.validate(a)
    else:
        v.validate(a)
        assert a.vpn_check_mark == rt.CHECK_MARK


def test_the_bypass_belongs_to_a_vpn(home):
    a = s.parse_args(["codex", "--backend", "podman", "--vpn-bypass-host-ports"])
    with pytest.raises(ValueError, match="goes with --vpn"):
        v.validate(a)
    assert "--vpn-bypass-host-ports" in s.SHORT_OPTIONS and "--vpn-bypass-host-ports" in s.MODES.WEAKENING_OPTIONS


def test_an_llm_server_in_the_local_network_is_refused_behind_tor(home, monkeypatch):
    llm = s.load_llm_module()
    monkeypatch.setattr(s, "load_llm_module", lambda: llm)  # (a fresh module at each call; vpn.py sees these)
    found = {"name": "lan", "url": "http://192.168.1.20:8000",
             "parsed": {"address": "192.168.1.20", "loopback": False, "host": "192.168.1.20", "port": 8000}}
    monkeypatch.setattr(llm, "selection", lambda a: (found, "m"))
    a = tor_args()
    a.llm = "lan"
    with pytest.raises(ValueError, match="lies in your local network, which a Tor start cannot reach") as refused:
        v.validate(a)
    assert "--vpn-bypass-tcp 192.168.1.20:8000" in str(refused.value)
    a = tor_args()
    a.llm = "lan"
    a.vpn_bypass_tcp = ["192.168.1.20:8000"]
    v.validate(a)  # exactly this server past Tor, as asked
    assert a.vpn_bypass_tcp == ["192.168.1.20:8000"] and "192.168.1.20:8000" in a.allow_tcp
    a = tor_args()
    a.llm = "lan"
    a.vpn_bypass_tcp = ["192.168.1.20:8001"]  # another port of it: not this server
    with pytest.raises(ValueError, match="lies in your local network"):
        v.validate(a)
    a = tor_args()
    found.update(url="https://llm.example.org", parsed={"address": None, "loopback": False, "host": "llm.example.org"})
    v.validate(a)  # a server on the internet: through Tor like everything else


def test_the_host_does_not_ask_an_llm_server_for_its_context_beside_a_vpn_start():
    llm = s.load_llm_module()

    class Client:
        def json(self, *args, **kwargs):
            raise AssertionError("asked from the host")
    endpoint = {"kind": "ollama", "url": "https://llm.example.org", "contexts": {}, "vpn": None}
    assert llm.context_window(endpoint, "m", client=Client(), behind_vpn=True) is None
    assert llm.context_window(endpoint, "m", client=Client()) is None  # without a VPN it asks (and fails here)


def test_bypass_targets_are_addresses_of_the_local_network_only():
    assert rt.bypass_target("192.168.1.20:1234") == ("192.168.1.20", 1234)
    assert rt.bypass_target({"address": "10.1.2.3", "port": 22}) == ("10.1.2.3", 22)
    for value in ("1.1.1.1:443", "100.64.0.1:80", "127.0.0.1:8000", "[fd00::1]:80", "192.168.1.1", "192.168.1.1:0",
                  "192.168.1.1:70000", "host.lan:80"):
        with pytest.raises(ValueError, match="--vpn-bypass-tcp"):
            rt.bypass_target(value)


def test_the_kill_switch_lets_exactly_the_bypass_past_tor():
    rules = rt.tor_rules(bypass=[{"address": "192.168.1.20", "port": 1234}])
    output, divert = chain(rules, "output").splitlines(), chain(rules, "divert").splitlines()
    accept = "  ip daddr 192.168.1.20 tcp dport 1234 accept"
    assert accept in output and output.index(accept) < output.index("  reject with icmpx type admin-prohibited")
    back = "  ip daddr 192.168.1.20 tcp dport 1234 return"
    assert back in divert and divert.index(back) < next(i for i, line in enumerate(divert) if "redirect to" in line)
    assert "192.168" not in rt.tor_rules()
    with pytest.raises(ValueError):
        rt.tor_rules(bypass=["8.8.8.8:53"])


def test_bypass_needs_a_tor_profile_and_reaches_the_payload(home, monkeypatch):
    a = tor_args()
    a.vpn_bypass_tcp = ["192.168.1.20:1234", "192.168.1.20:1234"]
    v.validate(a)
    assert a.vpn_bypass_tcp == ["192.168.1.20:1234"]
    assert v.payload(a.vpn_profile, "serve", (), a.vpn_bypass_tcp)["bypass"] == [
        {"address": "192.168.1.20", "port": 1234}]
    assert "bypass" not in v.payload(a.vpn_profile, "serve", ())
    a = tor_args()
    a.vpn = None
    a.vpn_bypass_tcp = ["192.168.1.20:1234"]
    with pytest.raises(ValueError, match="--vpn-bypass-tcp goes with --vpn"):
        v.validate(a)
    a = tor_args()
    a.vpn_bypass_tcp = ["8.8.8.8:53"]
    with pytest.raises(ValueError, match="only an address of your local network"):
        v.validate(a)


def test_the_data_directory_becomes_debian_tors(tmp_path, monkeypatch):
    data = tmp_path / "tor"
    data.mkdir(mode=0o755)
    owned, real_chmod = [], rt.os.chmod

    def chown(path, uid, gid, follow_symlinks=True):
        owned.append((str(path), uid, gid, follow_symlinks))

    def chmod(path, mode):  # as in the container: no CAP_FOWNER, so no chmod once the directory is debian-tor's
        if owned:
            raise PermissionError(1, "Operation not permitted", str(path))
        real_chmod(path, mode)
    monkeypatch.setattr(rt.os, "chown", chown)
    monkeypatch.setattr(rt.os, "chmod", chmod)
    rt.own_data_directory(rt.TOR_UID, data)
    assert owned == [(str(data), rt.TOR_UID, rt.TOR_UID, False)]
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="no directory"):
        rt.own_data_directory(rt.TOR_UID, link)


def test_the_bypass_counts_as_opening_a_port_for_modes_policy_and_trust(home, monkeypatch):
    """The review of 0.1.94: --vpn-bypass-tcp was added to --allow-tcp after the mode's and the policy's checks."""
    a = tor_args("--mode", "high", "--vpn-bypass-tcp", "192.168.1.20:22")
    a.mode_origin = "--mode"
    with pytest.raises(ValueError, match="--vpn-bypass-tcp 192.168.1.20:22 widens the security mode high"):
        s.MODES.check(s, a, {})
    assert ("--vpn-bypass-tcp", "192.168.1.20:22") in s.MODES.weakening_options(s, ["--vpn-bypass-tcp",
                                                                                    "192.168.1.20:22"])
    a = tor_args("--vpn-bypass-tcp", "192.168.1.20:22")
    a.policy = {"forbid": ["--allow-tcp"]}
    with pytest.raises(ValueError, match="administrator's policy forbids"):
        v.validate(a)
    a = tor_args("--vpn-bypass-tcp", "192.168.1.20:22")
    a.policy = {"forbid": ["--allow-tcp=192.168.1.20:22"]}
    with pytest.raises(ValueError, match="forbids --allow-tcp=192.168.1.20:22"):
        v.validate(a)
    a = tor_args("--vpn-bypass-tcp", "192.168.1.20:22")
    a.policy = {"forbid": ["--allow-tcp=10.0.0.5:22"]}
    v.validate(a)  # another target forbidden: this one is not


def test_the_metadata_service_is_never_past_tor():
    for value in ("169.254.169.254:80", "169.254.170.2:80"):
        with pytest.raises(ValueError, match="metadata"):
            rt.bypass_target(value)
    assert rt.bypass_target("169.254.10.10:80") == ("169.254.10.10", 80)


def test_the_tor_researchers_search_servers_go_behind_its_tor(home, monkeypatch):
    """The Tor researcher's search servers (duckduckgo with its allow-list, searxng with network full) reach the
    network only through the start's Tor; a server without network needs none."""
    v.main(["add", "anon", "--tor", "--force"])
    mcp = load("sandbox_mcp_tor_search_test", "mcp.py")
    image = "ghcr.io/c0decave/sandbox-ai-mcp-{}@sha256:" + "0" * 64
    # (an allow-list entry is added with network full: mcp.definition_from_entry)
    servers = {"duckduckgo": {"image": image.format("duckduckgo"), "network": "full"},
               "searxng": {"image": image.format("searxng"), "network": "full"},
               "yara": {"image": "ghcr.io/c0decave/axross-mcp-yara@sha256:" + "1" * 64, "network": "none"}}
    monkeypatch.setattr(mcp, "definitions", lambda document=None: servers)
    a = tor_args()
    behind = mcp.vpn_for_servers(a, list(servers))
    assert behind == {"duckduckgo": "anon", "searxng": "anon", "yara": None}


def test_the_address_check_tries_again_after_an_error_behind_tor():
    """A Tor circuit just after bootstrap can fail once (a TLS connection closed at once by an exit) and work a few
    seconds later: the check tries again (TOR_ATTEMPTS) before "no connection"; an answer ends it at once, and an
    address that is not a Tor exit is never tried past. Other VPNs: again only after an error that is not blocked."""
    answers = [OSError("TLS/SSL connection has been closed (EOF)"), (200, b'{"IsTor": true, "IP": "185.220.101.1"}')]
    slept = []

    def get(url, address, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    result = rt.echo_retried(["https://check.torproject.org/api/ip"], 4, lambda host, family: ["116.202.120.181"],
                             get, 1, tor=True, attempts=4, sleep=slept.append)
    assert result == {"ip": "185.220.101.1", "vpn": True, "url": "https://check.torproject.org/api/ip", "attempts": 2}
    assert slept == [rt.RETRY_DELAYS[0]]
    failing = lambda url, address, timeout: (_ for _ in ()).throw(OSError("closed"))  # noqa: E731
    slept.clear()
    result = rt.echo_retried(["https://check.torproject.org/api/ip"], 4, lambda host, family: ["116.202.120.181"],
                             failing, 1, tor=True, attempts=4, sleep=slept.append)
    assert "error" in result and result["attempts"] == 4 and slept == list(rt.RETRY_DELAYS)
    slept.clear()
    blocked = lambda url, address, timeout: (_ for _ in ()).throw(OSError(1, "Operation not permitted"))  # noqa: E731
    result = rt.echo_retried(["https://example.org/ip"], 4, lambda host, family: ["192.0.2.7"], blocked, 1,
                             tor=False, attempts=2, sleep=slept.append)
    assert result["kind"] == "blocked" and "attempts" not in result and slept == [], "a kill switch stays a kill switch"


def test_outside_tor_a_timeout_is_tried_again_a_refusal_never():
    """Outside Tor too a fresh tunnel (WireGuard before its first handshake) lets a connection time out once: a
    timeout is tried again; what the kill switch refuses here (EPERM, EACCES, no route) never is."""
    resolve = lambda host, family: ["192.0.2.7"]  # noqa: E731
    answers = [socket.timeout("timed out"), (200, b'{"ip": "198.51.100.4"}')]

    def get(url, address, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    slept = []
    result = rt.echo_retried(["https://example.org/ip"], 4, resolve, get, 1, tor=False, attempts=2,
                             sleep=slept.append)
    assert result.get("ip") == "198.51.100.4" and result["attempts"] == 2 and slept == [rt.RETRY_DELAYS[0]]
    for errno_ in (1, 13, 101, 113):
        slept.clear()
        refused = lambda url, address, timeout, e=errno_: (_ for _ in ()).throw(OSError(e, "refused"))  # noqa: E731
        result = rt.echo_retried(["https://example.org/ip"], 4, resolve, refused, 1, tor=False, attempts=3,
                                 sleep=slept.append)
        assert result["kind"] == "blocked" and slept == [], errno_
    # One service timed out, the other was refused by the kill switch: refused it is, not tried again.
    mixed = iter([socket.timeout("timed out"), OSError(1, "Operation not permitted")])
    slept.clear()
    result = rt.echo_retried(["https://a.example/ip", "https://b.example/ip"], 4, resolve,
                             lambda url, address, timeout: (_ for _ in ()).throw(next(mixed)), 1, tor=False,
                             attempts=3, sleep=slept.append)
    assert result["kind"] == "blocked" and slept == []


def test_the_retries_end_before_the_launcher_stops_waiting():
    """attempts x timeout x services + the delays may be more than the launcher waits for the VPN (ready_timeout):
    it would say "did not come up in time" instead of the reason. No attempt starts that could not end before the
    deadline (what is left of it, less what the other checks take), and the answer says so."""
    now = [0.0]

    def clock():
        return now[0]

    def get(url, address, timeout):
        now[0] += timeout
        raise socket.timeout("timed out")
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    result = rt.echo_retried(["https://example.org/ip"], 4, lambda host, family: ["192.0.2.7"], get, 30, tor=True,
                             attempts=4, sleep=sleep, deadline=80, clock=clock)
    # 30 s, then 3 s and 30 s (63), then 6 s and 30 s would end at 99 > 80: two attempts only
    assert result["attempts"] == 2 and slept == [3] and now[0] <= 80
    assert result.get("cut_short") is True
    evaluated = v.evaluate({"type": "wireguard", "name": "w"}, {"ipv4": True, "full4": True},
                           {"ipv4": {**result, "kind": "failed"}}, {})
    assert any("2 attempts" in line and "time" in line for line in evaluated["failures"]), evaluated


def test_the_launcher_hands_the_container_how_long_it_waits(home):
    """The payload's checks say how long the launcher waits for the VPN to be ready (ready_timeout): the container's
    first check keeps its retries within it."""
    profile = {"name": "w", "type": "wireguard", "wireguard": {"private_key_secret": "k"}}
    seconds = v.check_settings(profile).get("ready_seconds")
    assert seconds == v.ready_timeout(profile)
    assert v.check_settings({"name": "t", "type": "tor", "tor": {}})["ready_seconds"] == \
        v.ready_timeout({"type": "tor"})


def test_gather_checks_passes_the_deadline_to_the_first_check_only(monkeypatch):
    seen = []
    monkeypatch.setattr(rt, "echo_retried", lambda *args, **kwargs: seen.append(kwargs.get("deadline")) or
                        {"error": "x", "kind": "failed"})
    monkeypatch.setattr(rt, "echo", lambda *args, **kwargs: {"error": "x", "kind": "failed"})
    monkeypatch.setattr(rt, "route_device", lambda server, runner: "wg0")
    monkeypatch.setattr(rt, "dns_lookup", lambda *args, **kwargs: ["192.0.2.1"])
    payload = {"checks": {"urls": ["https://example.org/ip"], "timeout": 8, "ready_seconds": 120, "dns_probe": None}}
    tunnel = {"dns": ["10.0.0.1"]}
    rt.gather_checks(payload, tunnel, None, started=100.0)
    rt.gather_checks(payload, tunnel, None)  # a check during the run: no launcher waits for it
    assert seen[0] is not None and 100 < seen[0] < 220 and seen[1] is None, seen


# --- after some time (the report of 2026-11-12) ----------------------------------------------------------------

def test_an_answer_no_tor_exit_confirms_is_a_leak_during_the_run(home, tmp_path):
    """Behind Tor the address seen is not compared with this host's own during the run (it is not asked): an echo
    service of the profile's own that answers without "IsTor" could be this host's own address. Only a confirmed exit
    counts - anything else stops the run (--vpn-check stop), as an exit that is no Tor exit does."""
    said, stopped = [], []
    a = types.SimpleNamespace(log_dir=str(tmp_path), run_id="r1", vpn_check="stop")
    seen = tor_inside(ipv4={"ip": "198.18.0.5", "vpn": None, "url": "https://api64.ipify.org/"})
    verdict = v.rechecked(a, tor_profile(), {"tunnel": TOR_TUNNEL}, {"checks": seen}, said.append,
                          stop=lambda: stopped.append(1))
    assert verdict["leaks"] == ["IPv4: api64.ipify.org did not confirm that 198.18.0.5 is a Tor exit (no "
                                '"IsTor": true)'] and stopped == [1]
    # The retried address check: an error (an exit that closed the connection) is tried again; nothing but an answer
    # with "IsTor": true passes - an answer without it is never retried into a pass.
    answers = [OSError("EOF"), (200, b'{"IP": "198.18.0.5"}')]

    def get(url, address, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    result = rt.echo_retried([rt.TOR_CHECK], 4, lambda host, family: ["116.202.120.181"], get, 1, tor=True,
                             attempts=4, sleep=lambda seconds: None)
    assert result["vpn"] is None and result["attempts"] == 2
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, tor_inside(ipv4=result), {})
    assert verdict["failures"] and verdict["leaks"]


class EndingTunnel:
    def __init__(self):
        self.closed = False

    def follow(self, on_event, on_end=None):
        self.on_event, self.on_end = on_event, on_end

    def close(self):
        self.closed = True


def test_a_tor_container_that_ends_during_the_run_is_said_and_logged_and_nothing_restarts_it(home, tmp_path,
                                                                                            monkeypatch):
    """Tor (or its container) gone during the run: its kill switch is a table of the pod's namespace and stays; the
    start neither restarts it nor brings anything up in its place - it says so and writes it to vpn.jsonl. The run's
    own end (the cleanup closing the tunnel) is no such event."""
    v.main(["add", "anon", "--tor"])
    commands = []
    monkeypatch.setattr(v, "podman_run", lambda command, **kwargs: commands.append(command) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    launcher = types.SimpleNamespace(podman_command=lambda a: ["podman"], printable=str,
                                     pod_commands=lambda a, pod, session, home: [["podman", "pod", "create", pod]],
                                     signals_deferred=contextlib.nullcontext)
    monkeypatch.setattr(v, "launcher", lambda: launcher)
    tunnel = EndingTunnel()
    ready = {"tunnel": TOR_TUNNEL, "checks": tor_inside(), "hosts": {}}
    monkeypatch.setattr(v, "bring_up", lambda *args, **kwargs: (tunnel, ready, {}))
    said = []
    monkeypatch.setattr(sys, "stderr", types.SimpleNamespace(write=said.append, flush=lambda: None))
    a = argparse.Namespace(vpn="anon", vpn_profile=v.load_profile("anon"), vpn_check="stop", pod_name=None,
                           run_id="claude-r1", agent="claude", log_dir=str(tmp_path), vpn_bypass_tcp=None)
    with contextlib.ExitStack() as cleanup:
        assert v.agent_up(a, "session", "/home", [], cleanup) == {}
        created = len(commands)
        tunnel.on_end()  # the Tor container ended
        assert len(commands) == created, "nothing is started or recreated in its place"
        text = "".join(said)
        assert "the VPN container ended during the run; its kill switch stays" in text and "past Tor" in text
        record = json.loads((tmp_path / "vpn.jsonl").read_text().splitlines()[-1])
        assert record["event"] == "ended" and record["run"] == "claude-r1"
        tunnel.on_event({"event": "down", "message": "tor exited (1); the kill switch blocks all traffic now"})
        record = json.loads((tmp_path / "vpn.jsonl").read_text().splitlines()[-1])
        assert record["event"] == "down" and "tor exited" in record["failures"][0]
        said.clear()
    assert tunnel.closed
    tunnel.on_end()  # after the run's own end: nothing more is said
    assert "VPN container ended" not in "".join(said)


def test_behind_tor_the_agent_and_a_command_server_are_told_tor_and_its_onion_network(home):
    """agent_environment: SANDBOX_AI_TOR and the network tor maps .onion names into, only for a Tor start (a command
    MCP server's sandbox is one such start)."""
    a = tor_args()
    v.validate(a)
    a.run_id = "codex-r1"
    env = s.agent_environment(a)
    assert env["SANDBOX_AI_TOR"] == "1" and env["SANDBOX_AI_TOR_VIRTUAL_NETWORK"] == rt.TOR_VIRTUAL_NET
    plain = s.parse_args(["codex", "--backend", "podman"])
    plain.run_id = "codex-r2"
    assert not any(key.startswith("SANDBOX_AI_TOR") for key in s.agent_environment(plain))



def test_the_kill_switch_check_works_with_the_runtime_a_running_container_has_mounted():
    """Regression: the check of a running VPN container's kill switch ran `vpn_runtime.py killswitch` inside it - but
    a running container keeps the runtime file it mounted when it started (a bind-mounted file keeps its inode; an
    update replaces the file), and an older one knew no such command ("internal entry point"): every check failed,
    and every MCP server behind it was stopped and started again, over and over. Now the container's nft lists the
    table and this version judges it; the command never runs the container's runtime."""
    commands = []
    listing = "table inet sandbox_ai_vpn {\n" + "".join(
        f" chain {h} {{ type filter hook {h} priority -5; policy drop;\n }}\n" for h in ("output", "input", "forward")) + "}\n"

    def podman(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["container", "inspect"]:
            return types.SimpleNamespace(returncode=0, stderr="", stdout="|".join(
                ["a" * 64, "true", "2026-11-12 10:00:00 +0000 UTC", "pasta", "anon"]) + "\n")
        if command[1] == "exec":
            return types.SimpleNamespace(returncode=0, stdout=listing, stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    v.check_vpn_container(["podman"], "sandbox-ai-vpn-mcp-x", "anon", run=podman)
    executed = [c for c in commands if c[1] == "exec"]
    assert executed and all("vpn_runtime.py" not in " ".join(c) for c in executed)
    assert executed[0][2:] == ["a" * 64, "nft", "list", "table", "inet", "sandbox_ai_vpn"]
    with pytest.raises(ValueError, match="output chain does not drop"):
        listing = listing.replace("hook output priority -5; policy drop;", "hook output priority -5;")
        v.check_vpn_container(["podman"], "sandbox-ai-vpn-mcp-x", "anon", run=podman)
