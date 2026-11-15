"""Verification of Tor, the sandbox's browser and the browser behind Tor before the public release of 0.1.95 (user:
"insbesondere Tor-Support, Browser-Support, Browser-Tor-Support prüfen und verifizieren"), and the bugs it found -
each a permanent regression test, end to end through the real ./sandbox-ai with only Podman faked (live_fakes.py)
where the start can be run here, else by the configuration it generates (no Podman, tor or browser binaries here).

The first bug came from live use while this ran:

    vpn: tor: bootstrapped 100% (Done)
    agent-sandbox: the VPN did not come up: 127.0.53.1: DNS error code 2

tor says 100% before it has built a circuit an exit looks names up through; until then its DNSPort answers SERVFAIL
(DNS error code 2), as it does for a name one exit could not resolve. The first lookup of the agent's names
(resolve_hosts) took that as final and the start ended. Now every lookup through Tor after the bootstrap (the
checks, the agent's names, the names allowed during the run; MCP servers' Tors alike) treats SERVFAIL and no
answer as "not yet": asked again with growing pauses - up to 90 s for tor's first circuit (said as progress, "Tor:
waiting for its first circuit (DNS through Tor: SERVFAIL, try 3)"), asking check.torproject.org too to tell a
missing circuit from a failing name, then up to 30 s for each name - and only then fails, saying what was asked,
the answers and what helps. The kill switch is never touched, and every lookup goes to tor's DNSPort.

What the verification found besides (each below): Firefox behind Tor refused every .onion name itself
(network.dns.blockDotOnion), WebRTC handed a page the pod's addresses (with pasta this host's own), and Firefox
called Mozilla and Google by itself (Safe Browsing lists, push, region, prefetching, plugins; HTTP/3); the dry run
named no MCP server's VPN; the app's Browser tab of a Tor start did not say that it is not Tor Browser.

Verified by configuration only (no Podman, tor, nft namespace or browser binaries here): the kernel's handling of
the nft rules (tests/test_tor_killswitch_live.py runs them where a network namespace can be made), the browsers'
reading of the flags, Local State, user.js and policies, Podman's pod and container options."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

TOR_IMAGE = "localhost/sandbox-ai-tor:latest"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_vpn_runtime_verify_tor", "sandbox/vpn_runtime.py")

TOR_TUNNEL = {"interface": f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}", "implementation": "tor", "ipv4": True,
              "ipv6": False, "full4": True, "full6": False, "endpoints": ["the Tor network"], "dns": [rt.TOR_DNS],
              "tor": {"uid": rt.TOR_UID, "bridges": 0}}
EXIT = b'{"IsTor":true,"IP":"185.220.101.5"}'  # check.torproject.org/api/ip's answer from a Tor exit


# == tor's DNSPort right after the bootstrap: SERVFAIL is "not yet" ================================================

class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class DNSPort:
    """tor's DNSPort: SERVFAIL until CIRCUIT_AT (the clock's time; None: never), and always for the names FAILING."""

    def __init__(self, clock, circuit_at=None, failing=(), answer=("192.0.2.1",)):
        self.clock, self.circuit_at, self.failing, self.answer, self.asked = clock, circuit_at, set(failing), \
            list(answer), []

    def __call__(self, name, server, qtype, timeout=4.0, port=53):
        self.asked.append((name, server))
        self.clock.now += 0.1
        if self.circuit_at is None or self.clock.now < self.circuit_at or name in self.failing:
            raise rt.DnsError(2)
        return list(self.answer)


def test_a_servfail_answer_is_dns_error_code_2_named():
    reply = bytes.fromhex("1234") + (0x8182).to_bytes(2, "big") + bytes(8)
    with pytest.raises(rt.DnsError, match=r"^DNS error code 2 \(SERVFAIL\)$") as error:
        rt.dns_answers(reply, 0x1234, 1)
    assert error.value.rcode == 2 and isinstance(error.value, ValueError)
    assert rt.dns_answers(bytes.fromhex("1234") + (0x8183).to_bytes(2, "big") + bytes(8), 0x1234, 1) == []


def test_the_users_start_waits_for_tors_first_circuit_and_says_so():
    """The user's start: SERVFAIL for 20 s after "bootstrapped 100%", then answers. The agent's names resolve; the
    wait is said as progress, try by try."""
    clock, out = Clock(), io.StringIO()
    port = DNSPort(clock, circuit_at=20)
    dns = rt.TorDns(out=out, clock=clock, sleep=clock.sleep, ask=port)
    hosts = rt.resolve_hosts(["api.anthropic.com"], TOR_TUNNEL, lookup=dns.lookup)
    assert hosts == {"api.anthropic.com": ["192.0.2.1"]}
    said = [json.loads(line)["message"] for line in out.getvalue().splitlines()]
    assert said[0] == "Tor: waiting for its first circuit (DNS through Tor: SERVFAIL, try 1)"
    assert "Tor: waiting for its first circuit (DNS through Tor: SERVFAIL, try 3)" in said
    assert all(server == rt.TOR_DNS for _, server in port.asked)  # only tor's DNSPort, never another resolver
    assert ("check.torproject.org", rt.TOR_DNS) in port.asked  # asked too: no circuit, or a failing name?
    assert clock.slept[:3] == [2, 3, 5]  # growing pauses
    # Later names: the circuit is there, no wait.
    before = len(clock.slept)
    assert rt.resolve_hosts(["claude.ai"], TOR_TUNNEL, lookup=dns.lookup) == {"claude.ai": ["192.0.2.1"]}
    assert len(clock.slept) == before


def test_no_circuit_at_all_ends_after_about_90_s_with_what_was_asked_and_what_helps():
    clock = Clock()
    port = DNSPort(clock, circuit_at=None)
    dns = rt.TorDns(clock=clock, sleep=clock.sleep, ask=port)
    with pytest.raises(OSError) as error:
        rt.resolve_hosts(["api.anthropic.com"], TOR_TUNNEL, lookup=dns.lookup)
    text = str(error.value)
    assert 80 <= clock.now <= 100, clock.now
    assert "tor has bootstrapped, but its DNSPort 127.0.53.1 answered no lookup of api.anthropic.com" in text
    assert "SERVFAIL" in text and "check.torproject.org neither" in text
    assert "no working circuit" in text and "--bridges" in text and "clock" in text
    assert "kill switch held" in text
    assert text != "127.0.53.1: DNS error code 2"


def test_a_name_the_exits_cannot_resolve_fails_on_its_own_after_30_s():
    """tor resolves check.torproject.org (a circuit is there), but not this one name: it is asked again for 30 s,
    then the message says that the name fails, not tor."""
    clock = Clock()
    port = DNSPort(clock, circuit_at=0, failing={"broken.example.org"})
    dns = rt.TorDns(clock=clock, sleep=clock.sleep, ask=port)
    with pytest.raises(OSError, match=r"tor resolves other names, but not broken\.example\.org .*SERVFAIL"):
        rt.resolve_hosts(["broken.example.org"], TOR_TUNNEL, lookup=dns.lookup)
    assert 20 <= clock.now <= 40, clock.now
    assert dns.circuit


def test_other_errors_are_not_tried_again():
    """NXDOMAIN is an answer (no address); the kill switch's refusal (EPERM) or REFUSED end at once."""
    clock = Clock()

    def refused(name, server, qtype, timeout=4.0, port=53):
        raise PermissionError(1, "Operation not permitted")
    with pytest.raises(PermissionError):
        rt.TorDns(clock=clock, sleep=clock.sleep, ask=refused).lookup("a.example", rt.TOR_DNS, 1)

    def refused_code(name, server, qtype, timeout=4.0, port=53):
        raise rt.DnsError(5)
    with pytest.raises(rt.DnsError, match="REFUSED"):
        rt.TorDns(clock=clock, sleep=clock.sleep, ask=refused_code).lookup("a.example", rt.TOR_DNS, 1)
    assert clock.slept == []


def test_the_wait_never_goes_past_the_launchers():
    clock = Clock()
    dns = rt.TorDns(clock=clock, sleep=clock.sleep, ask=DNSPort(clock), deadline=30)
    with pytest.raises(OSError, match="no working circuit"):
        dns.lookup("api.anthropic.com", rt.TOR_DNS, 1, timeout=4)
    assert clock.now <= 30


def test_the_checks_after_the_bootstrap_wait_for_the_circuit_too(monkeypatch):
    """gather_checks behind Tor: the echo service's name, the resolver's answer and the DNS probe (whoami.akamai.net,
    answered through Tor by an exit's resolver: still compared with this host's own) all go through the patient
    lookups; the .onion probes (tor's own mapping, no circuit needed) are as patient."""
    clock = Clock()
    port = DNSPort(clock, circuit_at=15)

    def lookup(name, server, qtype, timeout=4.0, port_=53, **kw):
        if name.endswith(".onion"):
            return ["10.192.0.7"]
        return port(name, server, qtype, timeout)
    monkeypatch.setattr(rt, "dns_lookup", lookup)
    monkeypatch.setattr(rt, "probe_ipv6", lambda timeout: {"error": "Operation not permitted", "kind": "blocked"})
    dns = rt.TorDns(clock=clock, sleep=clock.sleep)
    result = rt.gather_checks({"checks": {"urls": [rt.TOR_CHECK], "dns_probe": "whoami.akamai.net", "timeout": 2}},
                              TOR_TUNNEL, None, lambda url, address, timeout: (200, EXIT), dns=dns)
    assert result["ipv4"]["ip"] == "185.220.101.5" and result["ipv4"]["vpn"] is True
    assert result["dns"]["answers"] == {rt.TOR_DNS: True}
    assert result["dns"]["egress"] == ["192.0.2.1"]
    assert result["dns"]["onion"] == {"tor": ["10.192.0.7"], "outside": ["10.192.0.7"]}
    assert all(server == rt.TOR_DNS for _, server in port.asked)


def test_a_name_allowed_during_the_run_is_asked_again_within_the_launchers_30_s(tmp_path, monkeypatch):
    clock = Clock()
    port = DNSPort(clock, circuit_at=4)
    monkeypatch.setattr(rt, "dns_lookup", port)
    monkeypatch.setattr(rt, "_check_mark", None)
    state = tmp_path / "serve.json"
    rt.save_serve_state(TOR_TUNNEL, False, state)
    assert json.loads(state.read_text())["tor"] is True
    out = io.StringIO()
    assert rt.resolve_command(io.StringIO('["pypi.org"]'), out, state, clock=clock, sleep=clock.sleep) == 0
    assert json.loads(out.getvalue()) == {"hosts": {"pypi.org": ["192.0.2.1"]}}
    # Never asked past the launcher's 30 s: its patience and one lookup's timeout within them.
    assert rt.TOR_DNS_RESOLVE_PATIENCE + 4 < 30
    port.circuit_at = None
    out = io.StringIO()
    assert rt.resolve_command(io.StringIO('["pypi.org"]'), out, state, clock=clock, sleep=clock.sleep) == 1
    assert "tor resolves other names, but not pypi.org" in json.loads(out.getvalue())["error"]


# --- end to end: the user's start through ./sandbox-ai, its Tor container running the real vpn_runtime.py --------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image(TOR_IMAGE, "localhost/claude-sandbox:latest")
    added = made.run("vpn", "add", "tor-test", "--tor", "--check-url", "https://check.torproject.org/api/ip",
                     "--no-dns-probe")
    assert added.returncode == 0, added.stderr
    return made


def tor_events(host):
    path = host.directory / "tor-container" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


DONE = [[[1, 50, "Loading relay descriptors"], [3, 100, "Done"]]]


def test_e2e_the_users_start_goes_on_once_tor_answers(host):
    """The user's start (./sandbox-ai claude --vpn tor-test): tor at 100%, then SERVFAIL for 20 s - before, "the VPN
    did not come up: 127.0.53.1: DNS error code 2"; now it waits, says so, and the sandbox runs."""
    result = host.run("claude", "--vpn", "tor-test", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20",
        "FAKE_TOR_DNS": json.dumps({"servfail_for": 20})})
    err = result.stderr
    assert result.returncode == 0, err
    assert "vpn: tor: bootstrapped 100% (Done)" in err
    assert "DNS error code 2" not in err
    assert "agent-sandbox: vpn: Tor: waiting for its first circuit (DNS through Tor: SERVFAIL, try 1)" in err
    assert "fake podman: the sandbox runs" in err
    events = tor_events(host)
    asked = [e for e in events if "dns" in e]
    assert any(e["answer"] == "SERVFAIL" for e in asked) and asked[-1]["answer"] == "ok"
    # Every lookup went to tor's DNSPort (the .onion probe's outside resolver is redirected there by the rules).
    assert {e["server"] for e in asked if not e["dns"].endswith(".onion")} == {rt.TOR_DNS}
    # The kill switch was loaded once, before tor, and never flushed or deleted while the lookups waited.
    nft = [e["nft"] for e in events if "nft" in e]
    assert [c for c in nft if c[:2] == ["nft", "-f"]] and not any(c[1] in ("flush", "delete") for c in nft)
    first_load = next(i for i, e in enumerate(events) if e.get("nft", [None, None])[:2] == ["nft", "-f"])
    assert first_load < next(i for i, e in enumerate(events) if "dns" in e)


def test_e2e_the_users_message_an_agent_name_tor_answers_servfail_at_first(host):
    """The user's message exactly: the check through Tor passed, then tor's DNSPort answered SERVFAIL for the
    agent's own name (api.anthropic.com) - before: "the VPN did not come up: 127.0.53.1: DNS error code 2" at the
    first answer. Now that name is asked again and the start goes on."""
    result = host.run("claude", "--vpn", "tor-test", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20",
        "FAKE_TOR_DNS": json.dumps({"slow": {"api.anthropic.com": 8}})})
    err = result.stderr
    assert "DNS error code 2" not in err
    assert result.returncode == 0, err
    assert "agent-sandbox: vpn: Tor: api.anthropic.com has no answer through Tor yet (SERVFAIL, try 1); asking " \
           "again" in err
    assert "fake podman: the sandbox runs" in err


def test_e2e_a_tor_without_a_circuit_stops_the_start_with_a_clear_message(host):
    result = host.run("claude", "--vpn", "tor-test", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "60",
        "FAKE_TOR_DNS": json.dumps({"servfail_for": 100000})}, timeout=240)
    err = result.stderr
    assert result.returncode != 0
    assert "tor has bootstrapped, but its DNSPort 127.0.53.1 answered no lookup" in err
    assert "no working circuit" in err and "fake podman: the sandbox runs" not in err


def test_e2e_vpn_check_of_a_tor_profile_waits_the_same_way(host):
    """sandbox-ai vpn check (and an MCP server's Tor: the same vpn_runtime.py in its container) waits as well."""
    result = host.run("vpn", "check", "tor-test", env={
        "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20",
        "FAKE_TOR_DNS": json.dumps({"servfail_for": 10})})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Tor: waiting for its first circuit (DNS through Tor: SERVFAIL, try 1)" in result.stdout + result.stderr


# == C) the browser behind Tor: no way around the kill switch, nothing of its own on the network ===================
#
# The browser's container shares the pod's network namespace (no network option of its own, no port published) and
# so every packet of it meets the agent's firewall (the redirect of every TCP connection to the connection proxy,
# the allow-list, the approvals) and Tor's kill switch (tor's own uid alone leaves; TCP to the TransPort, port 53 to
# the DNSPort, other UDP and IPv6 dropped). What the browser could still do by itself is checked here in what the
# launcher generates and what the runtime starts it with (no browser binaries here: verified by configuration).

browser_runtime = load("sandbox_browser_runtime_verify_tor", "sandbox/browser_runtime.py")


@pytest.fixture
def browser_host(host):
    host.add_image("localhost/sandbox-ai-browser:latest", labels={"sandbox-ai.role": "browser"})
    return host


def plan_of(host, *options):
    result = host.run("claude", *options, "--dry-run")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


def started(cfg, tmp_path, monkeypatch):
    """browser_runtime.main with CFG (as the launcher wrote it) in a stand-in container: the browser's command line
    and its profile folder."""
    mount, home = tmp_path / "mount", tmp_path / "home"
    (mount / "vnc").mkdir(parents=True, exist_ok=True)
    (mount / "config.json").write_text(json.dumps(cfg))
    seen, stop = [], []
    runtime = types.SimpleNamespace(start_gui=lambda c: [], stop_process=lambda p: None, VNC_DIR=None)

    def popen(argv, **kwargs):
        seen.append(argv)
        return types.SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(browser_runtime.signal, "signal", lambda number, handler: stop.append(handler))
    monkeypatch.setattr(browser_runtime.os, "environ", dict(browser_runtime.os.environ))
    assert browser_runtime.main(mount, home, runtime, popen, lambda _: stop[0]()) == 0
    profile = mount / "profile" if cfg["profile"] in ("session", "dir") else home / "profile"
    return seen[0], profile


def user_prefs(profile):
    found = {}
    for line in (profile / "user.js").read_text().splitlines():
        if line.startswith("user_pref("):
            name, _, value = line[len("user_pref("):-2].partition(", ")
            found[json.loads(name)] = json.loads(value)
    return found


def test_e2e_chromium_behind_tor_shares_the_kill_switch_and_leaks_nothing_of_its_own(browser_host, tmp_path,
                                                                                      monkeypatch):
    plan, err = plan_of(browser_host, "--vpn", "tor-test", "--browser", "--browser-control", "both")
    browser, vpn = plan["browser"], plan["vpn"]
    # The pod's network, which is Tor's: its kill switch and its DNS.
    assert vpn["type"] == "tor" and vpn["tunnel"].startswith("transparent: every TCP connection through Tor")
    assert "UDP other than DNS dropped, IPv6 blocked" in vpn["tunnel"]
    assert "only tor's own connections leave" in vpn["kill_switch"]
    assert "Tor" in browser["network"] and browser["capabilities"].startswith("none")
    assert browser["read_only"] is True and browser["user"] == "0:0"
    assert browser["display"]["tcp"] is False and browser["tcp_ports"] == [9222]
    assert browser["cdp"] == "http://127.0.0.1:9222"
    assert browser["config"]["tor"] is True and browser["config"]["cdp_port"] == 9222
    assert "not Tor Browser" in err and "not Tor Browser" in browser["tor"]
    assert any("WebRTC gathers no address" in line for line in browser["settings"])
    assert plan["nameservers"] == [] and "10.192.0.0/10" in plan["allow"]  # .onion's virtual network allowed
    # What the runtime starts Chromium with, from that configuration.
    argv, profile = started(browser["config"], tmp_path, monkeypatch)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in argv
    assert "--disable-quic" in argv and "--disable-background-networking" in argv
    assert "--disable-component-update" in argv and "--disable-breakpad" in argv and "--no-pings" in argv
    assert sum(1 for word in argv if word.startswith("--disable-features=")) == 1  # (a second one would win)
    assert "--remote-debugging-address=127.0.0.1" in argv and "--remote-debugging-port=9222" in argv
    # Nothing that would take it past the pod's network or resolver: no proxy of its own, no resolver rules.
    for word in argv:
        assert not word.startswith(("--proxy-server", "--proxy-pac-url", "--host-resolver-rules",
                                    "--remote-debugging-address=0", "--enable-features"))
    state = json.loads((profile / "Local State").read_text())
    assert state["dns_over_https"] == {"mode": "off", "templates": ""}


def test_e2e_firefox_behind_tor_loads_onion_names_and_has_no_webrtc(browser_host, tmp_path, monkeypatch):
    plan, _ = plan_of(browser_host, "--vpn", "tor-test", "--browser", "firefox")
    assert plan["browser"]["config"]["tor"] is True and plan["browser"]["config"]["cdp_port"] is None
    assert plan["browser"]["tcp_ports"] == [] and plan["browser"]["cdp"] is None  # human: no DevTools port at all
    argv, profile = started(plan["browser"]["config"], tmp_path, monkeypatch)
    assert argv[0] == "firefox-esr" and not any("remote-debugging" in word for word in argv)
    prefs = user_prefs(profile)
    # Before: Firefox refused every .onion name itself (network.dns.blockDotOnion, true by default), so no onion
    # site loaded behind Tor; WebRTC's candidates were a page's to read; HTTP/3, prefetching, Safe Browsing's lists,
    # the push service, region lookups, crash reports and plugin downloads went out by themselves.
    assert prefs["network.dns.blockDotOnion"] is False
    assert prefs["media.peerconnection.enabled"] is False and prefs["media.peerconnection.ice.no_host"] is True
    assert prefs["network.trr.mode"] == 5 and prefs["network.http.http3.enable"] is False
    assert prefs["network.dns.disablePrefetch"] is True and prefs["network.prefetch-next"] is False
    assert prefs["network.predictor.enabled"] is False and prefs["network.http.speculative-parallel-limit"] == 0
    assert prefs["browser.safebrowsing.provider.google4.updateURL"] == "" and \
        prefs["browser.safebrowsing.downloads.remote.enabled"] is False
    assert prefs["dom.push.connection.enabled"] is False and prefs["browser.region.network.url"] == ""
    assert prefs["breakpad.reportURL"] == "" and prefs["media.gmp-manager.updateEnabled"] is False
    assert prefs["toolkit.telemetry.enabled"] is False and prefs["app.normandy.enabled"] is False
    # The image's policies (Firefox's own DNS over HTTPS off and locked, telemetry, studies, updates) stay.
    text = (ROOT / "sandbox" / "Containerfile.browser").read_text()
    assert '"DNSOverHTTPS": {"Enabled": false, "Locked": true}' in text and '"DisableTelemetry": true' in text
    assert '"DnsOverHttpsMode": "off", "QuicAllowed": false' in text  # Chromium's, for an image built from now on


def test_e2e_without_tor_firefox_gets_its_defaults_back_and_chromium_keeps_webrtc(browser_host, tmp_path,
                                                                                  monkeypatch):
    """A profile kept from a Tor run (session, a folder of yours) used without Tor: .onion refused again, WebRTC
    back; the settings that keep it off the network stay."""
    folder = tmp_path / "profile"
    plan, _ = plan_of(browser_host, "--vpn", "tor-test", "--browser", "firefox", "--browser-profile", str(folder))
    (tmp_path / "1" / "mount").mkdir(parents=True)
    folder.mkdir(mode=0o700)
    (tmp_path / "1" / "mount" / "profile").symlink_to(folder)  # the folder, as its container mounts it
    _, profile = started(plan["browser"]["config"], tmp_path / "1", monkeypatch)
    assert user_prefs(folder)["network.dns.blockDotOnion"] is False
    plan, _ = plan_of(browser_host, "--browser", "firefox", "--browser-profile", str(folder))
    assert plan["browser"]["config"]["tor"] is False and plan["browser"]["tor"] is None
    (tmp_path / "2" / "mount").mkdir(parents=True)
    (tmp_path / "2" / "mount" / "profile").symlink_to(folder)
    started(plan["browser"]["config"], tmp_path / "2", monkeypatch)
    prefs = user_prefs(folder)
    assert prefs["network.dns.blockDotOnion"] is True and prefs["media.peerconnection.enabled"] is True
    assert prefs["network.trr.mode"] == 5 and prefs["dom.push.connection.enabled"] is False
    plan, _ = plan_of(browser_host, "--browser")
    argv, _ = started(plan["browser"]["config"], tmp_path / "3", monkeypatch)
    assert not any(word.startswith("--force-webrtc-ip-handling-policy") for word in argv)


def test_a_link_in_the_profile_is_never_written_through(tmp_path):
    """The profile is the browser's (and a site's, if it breaks in): a user.js or Local State that is a link is
    replaced, never followed."""
    profile, target = tmp_path / "profile", tmp_path / "outside.txt"
    profile.mkdir()
    target.write_text("keep")
    (profile / "user.js").symlink_to(target)
    (profile / "Local State").symlink_to(target)
    browser_runtime.settings({"kind": "firefox", "tor": True}, profile)
    browser_runtime.settings({"kind": "chromium", "tor": True}, profile)
    assert target.read_text() == "keep"
    assert not (profile / "user.js").is_symlink() and not (profile / "Local State").is_symlink()
    # A Local State of its own is kept, only DNS over HTTPS is set.
    (profile / "Local State").write_text(json.dumps({"browser": {"x": 1}, "dns_over_https": {"mode": "secure"}}))
    browser_runtime.settings({"kind": "chromium"}, profile)
    assert json.loads((profile / "Local State").read_text()) == {
        "browser": {"x": 1}, "dns_over_https": {"mode": "off", "templates": ""}}


def test_e2e_the_tor_container_of_a_start_loads_its_kill_switch_first_and_only_lets_the_bypass_past(host):
    """A) What the real vpn_runtime.py in the start's Tor container loads (its nft table, its torrc), from a start
    with --vpn-bypass-tcp and a profile with --socks: every TCP to the TransPort, port 53 to the DNSPort, other UDP
    and IPv6 dropped, past Tor only tor's own TCP and exactly the address and port given."""
    assert host.run("vpn", "add", "tor-socks", "--tor", "--socks", "--check-url",
                    "https://check.torproject.org/api/ip", "--no-dns-probe").returncode == 0
    result = host.run("claude", "--vpn", "tor-socks", "--vpn-bypass-tcp", "192.168.1.20:1234", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20", "FAKE_TOR_DNS": "{}"})
    assert result.returncode == 0, result.stderr
    table = (host.directory / "tor-container" / "nft-table").read_text()
    assert table == rt.tor_rules(rt.TOR_UID, (), ["192.168.1.20:1234"])
    output = table.split(" chain output {")[1].split(" }")[0]
    assert "policy drop;" in output and "meta nfproto ipv6 reject" in output
    assert f"meta skuid {rt.TOR_UID} meta nfproto ipv4 meta l4proto tcp accept" in output
    assert [line.strip() for line in output.splitlines() if "accept" in line and "skuid" not in line
            and "lo" not in line and "dnat" not in line] == ["ip daddr 192.168.1.20 tcp dport 1234 accept"]
    divert = table.split(" chain divert {")[1].split(" }")[0]
    assert f"udp dport 53 dnat ip to {rt.TOR_DNS}:53" in divert and "meta l4proto tcp redirect to :9040" in divert
    assert "udp" not in divert.replace("udp dport 53", "")  # no other UDP is redirected: it meets the drop
    torrc = (host.directory / "tor-container" / "run" / "torrc").read_text()
    for line in ("TransPort 127.0.0.1:9040 IsolateDestAddr", f"DNSPort {rt.TOR_DNS}:53",
                 "SocksPort 127.0.0.1:9050 IsolateDestAddr", "AutomapHostsSuffixes .onion",
                 "VirtualAddrNetworkIPv4 10.192.0.0/10", "ClientUseIPv6 0", "ControlPort 0", "ClientOnly 1"):
        assert line in torrc.splitlines(), line
    events = tor_events(host)
    first_load = next(i for i, e in enumerate(events) if e.get("nft", [None, None])[:2] == ["nft", "-f"])
    assert first_load < next(i for i, e in enumerate(events) if "tor" in e)  # the kill switch before tor runs


def test_e2e_an_address_the_tor_project_does_not_confirm_stops_the_start(host):
    """A) The check against check.torproject.org: an answer without "IsTor": true stops the start (on_failure
    stop) - the agent never runs."""
    result = host.run("claude", "--vpn", "tor-test", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20",
        "FAKE_TOR_DNS": json.dumps({"echo": {"IsTor": False, "IP": "198.51.100.7"}})})
    assert result.returncode != 0
    assert "the Tor Project says 198.51.100.7 is not a Tor exit" in result.stderr
    assert "fake podman: the sandbox runs" not in result.stderr


def test_e2e_board_with_tor_browser_and_mcp_servers_plans_each_server_behind_tor(browser_host):
    """D) A start (the Board's) with Tor, the browser and MCP servers: the dry run says the browser is behind the
    start's Tor and each server that reaches the network behind a Tor too - its own profile, or the start's (each
    server in a Tor container of its own); before, the plan named no server's VPN at all."""
    import test_live_regressions_tor as tor_tests
    catalog = tor_tests.tor_catalog(browser_host)
    browser_host.add_image(*[f"ghcr.io/c0decave/mcp-{name}@{tor_tests.DIGEST}" for name in ("whatweb", "osint-mcp")])
    assert browser_host.run("mcp", "add", "whatweb", "--vpn", "tor-test", "--catalog", catalog, "--unsigned",
                            "--no-pull").returncode == 0
    assert browser_host.run("mcp", "add", "osint-mcp", "--catalog", catalog, "--unsigned",
                            "--no-pull").returncode == 0
    plan, err = plan_of(browser_host, "--vpn", "tor-test", "--browser", "firefox", "--mcp", "whatweb",
                        "--mcp", "osint-mcp")
    assert {entry["name"]: entry["vpn"] for entry in plan["mcp"]} == {"whatweb": "tor-test", "osint-mcp": "tor-test"}
    assert plan["browser"]["config"]["tor"] is True and plan["vpn"]["type"] == "tor"
    assert "not Tor Browser" in err
    # Without the start's Tor: whatweb keeps its own, osint-mcp has none - and the browser is not behind Tor.
    plan, _ = plan_of(browser_host, "--browser", "--mcp", "whatweb", "--mcp", "osint-mcp")
    assert {entry["name"]: entry["vpn"] for entry in plan["mcp"]} == {"whatweb": "tor-test", "osint-mcp": None}
    assert plan["browser"]["config"]["tor"] is False and plan["browser"]["tor"] is None


def test_the_run_record_tells_the_apps_browser_tab_that_it_is_behind_tor(tmp_path):
    """D) The app's Browser tab of a Tor session: run.json's browser says "tor" (sandbox-ai list --json passes it on),
    and the tab says that this is not Tor Browser (the app's tests/test_browser_view.py)."""
    browser = load("sandbox_browser_verify_tor", "sandbox/browser.py")
    starts = load("sandbox_starts_verify_tor", "sandbox/starts.py")
    a = types.SimpleNamespace(browser="firefox", browser_control=None, browser_profile=None,
                              browser_container={"name": "browser"}, vpn_profile={"type": "tor", "name": "tor-test"}, control_dir=tmp_path / "claude-1")
    found = browser.record(a, "agent-claude-1")
    assert found["tor"] is True and starts.record_browser({"browser": found})["tor"] is True
    a.vpn_profile = {"type": "wireguard", "name": "w"}
    assert browser.record(a, "agent-claude-1")["tor"] is False
    assert starts.record_browser({"browser": {**found, "tor": "yes"}})["tor"] is False  # only a real true counts


def test_e2e_the_pod_publishes_no_port_and_forwards_nothing_of_the_host(browser_host):
    """CDP on 127.0.0.1:9222 is the pod's loopback alone: the pod is made with pasta forwarding no port in either
    direction (-t/-u/-T/-U none) and nothing published; the browser's container has no network of its own."""
    browser_host.run("claude", "--vpn", "tor-test", "--browser", "--browser-control", "both", env={
        "FAKE_TOR_SCHEDULE": json.dumps(DONE), "FAKE_TOR_SPEED": "20", "FAKE_TOR_DNS": "{}"})
    pod = browser_host.stopped_at()
    assert pod[:2] == ["pod", "create"], pod
    assert "pasta:--no-map-gw,-t,none,-u,none,-T,none,-U,none" in pod
    assert not any(word.startswith(("-p", "--publish", "--expose")) for word in pod)
    assert "9222" not in " ".join(pod)
