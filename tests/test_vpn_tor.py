"""VPN profile type "tor": the profile (bridges, exit countries, SOCKS), the generated torrc, the kill switch and the
redirects as nft text, the bootstrap wait, the checks (IsTor, tor's DNSPort, .onion mapping) and the agent's
firewall, without Podman or tor. The bypass tests are named after what an agent would try."""
import argparse
import contextlib
import importlib.util
import ipaddress
import json
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

    def follow(self, on_event):
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
    for cap in ("SETUID", "SETGID", "NET_BIND_SERVICE"):
        assert f"--cap-add={cap}" in command
    assert "--cap-add=NET_RAW" not in command and "--device=/dev/net/tun" not in command
    assert f"--tmpfs=/var/lib/tor:rw,nosuid,nodev,mode=0700,uid={rt.TOR_UID},gid={rt.TOR_UID}" in command
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
    monkeypatch.setattr(v, "podman_run", lambda command, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    FakeTunnel.inside = tor_inside()
    assert v.mcp_wrapper("anon", "sandbox-ai-vpn-mcp-x", "/owner", [sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert FakeTunnel.last.data["profile"]["type"] == "tor" and FakeTunnel.last.closed


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
    if refused:
        with pytest.raises(ValueError, match="the user tor's kill switch lets out past Tor"):
            mcp.check_tor_image_user("osint", "ghcr.io/x/osint@sha256:0", inspect)
    else:
        mcp.check_tor_image_user("osint", "ghcr.io/x/osint@sha256:0", inspect)
    failed = lambda: subprocess.CompletedProcess([], 125, "", "no such image")  # noqa: E731
    mcp.check_tor_image_user("osint", "missing", failed)  # podman's own error comes at the start
