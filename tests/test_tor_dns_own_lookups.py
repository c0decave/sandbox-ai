"""Behind Tor the sandbox's lookups (--dns) share TorDnsFront's TOR_DNS_FRONT_LIMIT (256) places in flight: an agent
that floods DNS filled them, and the Tor container's own lookups - the checks during the run, the launcher's requests,
`resolve` - were dropped with the agent's. They go straight to tor's DNSPort now (tor_own_lookup: as the front
hands a question on, an AAAA one asked as A and answered with no address), past the front; only the outside
resolver's probe still takes the redirect to the front, which is what it checks. Each check says how many questions
the front dropped since the last one (dns.front), and a check during the run that failed for it says so - a failed
check is said, never a reason to end the run (only a leak is). Through real UDP sockets on loopback (the front and a
stand-in for tor's DNSPort), the launcher's verdict, and end to end through the real ./sandbox-ai with a Tor profile
(tests/live_fakes.py)."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("vpn_runtime_tor_own_lookups", "vpn_runtime.py")
s = load("agent_sandbox_tor_own_lookups", "agent_sandbox.py")
v = s.load_vpn_module()

from test_tor_dns_ipv4_only import FakeTorDnsPort, ONION, ONION_V4  # noqa: E402

TOR_TUNNEL = {"interface": f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}", "implementation": "tor", "ipv4": True,
              "ipv6": False, "full4": True, "full6": False, "endpoints": ["the Tor network"], "dns": [rt.TOR_DNS],
              "tor": {"uid": rt.TOR_UID, "bridges": 0}}


def tor_profile():
    return {"version": 1, "name": "anon", "type": "tor", "provider": "tor",
            "tor": {"bridges": 0, "bridges_secret": None, "bridges_secret_owned": False, "exit_country": [],
                    "socks": False}}


@pytest.fixture
def full_front(monkeypatch):
    """tor's DNSPort stand-in and a front on loopback whose places are all taken (the agent's flood)."""
    tor = FakeTorDnsPort()
    front = rt.TorDnsFront(address="127.0.0.1", port=0, tor=tor.address, wait=1.0, limit=2)
    gate = threading.Event()
    front.answer = lambda query: gate.wait(10) and None  # every place busy until the gate opens
    front.start()
    port = front.sockets[0].getsockname()[1]
    # The container's view: tor's address is this loopback, port 53 the front's, TOR_DNS_PORT tor's own.
    monkeypatch.setattr(rt, "TOR_DNS", "127.0.0.1")
    monkeypatch.setattr(rt, "TOR_DNS_PORT", tor.address[1])
    real = rt.dns_lookup
    monkeypatch.setattr(rt, "dns_lookup", lambda name, server, qtype, timeout=4.0, port=53:
                        real(name, server, qtype, timeout=timeout, port=front.sockets[0].getsockname()[1]
                             if port == 53 else port))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for ident in range(20):  # the flood: 2 taken, 18 dropped
            sock.sendto(rt.dns_query_packet("flood.example", 1, ident), ("127.0.0.1", port))
    time.sleep(0.3)
    try:
        yield tor, front, port
    finally:
        gate.set()
        front.close()
        tor.close()


def test_the_containers_own_lookups_pass_a_full_front(full_front):
    tor, front, port = full_front
    # The agent's way (the front, port 53): no answer while it is full.
    with pytest.raises(TimeoutError):
        rt.dns_lookup("example.org", "127.0.0.1", 1, timeout=0.5)
    # The container's own: straight to tor's DNSPort, answered.
    assert rt.tor_own_lookup("example.org", "127.0.0.1", 1, timeout=2) == ["192.0.2.1"]
    assert rt.tor_own_lookup(ONION, "127.0.0.1", 1, timeout=2) == [ONION_V4]
    # AAAA as the front answers it: asked as A, no address (tor would give an IPv6 one).
    assert rt.tor_own_lookup(ONION, "127.0.0.1", 28, timeout=2) == []
    assert tor.asked[-1] == (ONION, 1)
    # TorDns (the checks', the launcher's requests', resolve's) asks that way by default.
    assert rt.TorDns(circuit=True).lookup("example.org", "127.0.0.1", 1, timeout=2) == ["192.0.2.1"]
    # The front counted what it dropped; read once, then from zero again.
    load = front.load()
    assert load["limit"] == 2 and load["dropped"] >= 18
    assert front.load()["dropped"] in (0, 1)  # (the timed-out lookup above was one more)


def test_a_lookup_sent_elsewhere_still_takes_the_redirect():
    """The outside resolver's .onion probe checks that the redirect to the front works: it is not taken past it."""
    asked = []
    real = rt.dns_lookup
    rt.dns_lookup = lambda name, server, qtype, timeout=4.0, port=53: asked.append((server, port, qtype)) or []
    try:
        rt.tor_own_lookup(ONION, rt.OUTSIDE_RESOLVER, 1)
        rt.tor_own_lookup(ONION, rt.TOR_DNS, 28)
        rt.tor_own_lookup(ONION, rt.TOR_DNS, 1, port=rt.TOR_DNS_PORT)
    finally:
        rt.dns_lookup = real
    assert asked == [(rt.OUTSIDE_RESOLVER, 53, 1), (rt.TOR_DNS, rt.TOR_DNS_PORT, 1), (rt.TOR_DNS, rt.TOR_DNS_PORT, 1)]


def test_each_check_says_what_the_front_dropped(monkeypatch):
    monkeypatch.setattr(rt, "probe_ipv6", lambda timeout: {"error": "Operation not permitted", "kind": "blocked"})
    monkeypatch.setattr(rt, "dns_lookup", lambda name, server, qtype, timeout=4.0, port=53:
                        ["10.192.0.7"] if name.endswith(".onion") else ["192.0.2.1"])
    monkeypatch.setattr(rt, "TOR_FRONT", types.SimpleNamespace(load=lambda: {"limit": 256, "dropped": 7}))
    result = rt.gather_checks({"checks": {"ip": False, "timeout": 2}}, TOR_TUNNEL, None,
                              dns=rt.TorDns(circuit=True))
    assert result["dns"]["front"] == {"limit": 256, "dropped": 7}
    monkeypatch.setattr(rt, "TOR_FRONT", None)  # (vpn check, a front of a test): nothing said
    result = rt.gather_checks({"checks": {"ip": False, "timeout": 2}}, TOR_TUNNEL, None,
                              dns=rt.TorDns(circuit=True))
    assert "front" not in result["dns"]


def inside(onion_outside, dropped):
    return {"ipv4": {"ip": "185.220.101.5", "vpn": True, "url": rt.TOR_CHECK},
            "ipv6": {"error": "Operation not permitted", "kind": "blocked"},
            "dns": {"servers": [rt.TOR_DNS], "configured": True, "routes": {}, "answers": {rt.TOR_DNS: True},
                    "onion": {"tor": ["10.192.0.7"], "outside": onion_outside},
                    **({"front": {"limit": 256, "dropped": dropped}} if dropped is not None else {})}}


def test_a_check_during_the_run_that_met_a_full_front_says_why_and_ends_nothing(tmp_path):
    said, stopped = [], []
    a = types.SimpleNamespace(log_dir=str(tmp_path), run_id="r1", vpn_check="stop")
    verdict = v.rechecked(a, tor_profile(), {"tunnel": TOR_TUNNEL}, {"checks": inside("no answer", 4000)},
                          said.append, stop=lambda: stopped.append(1))
    assert stopped == [] and verdict["leaks"] == []
    text = "\n".join(said)
    assert "the resolver in front of tor was full" in text and "4000 questions" in text, text
    assert "the sandbox's own lookups" in text
    # All fine, but questions were dropped: a warning that says so.
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, inside(["10.192.0.7"], 12), {})
    assert verdict["failures"] == [] and any("12 questions" in w for w in verdict["warnings"])
    # Nothing dropped: nothing said; an older container (no "front"): as before.
    for dropped in (0, None):
        verdict = v.evaluate(tor_profile(), TOR_TUNNEL, inside(["10.192.0.7"], dropped), {})
        assert verdict["failures"] == [] and verdict["warnings"] == []
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, inside("no answer", None), {})
    assert verdict["failures"] and "full" not in " ".join(verdict["failures"])


@pytest.mark.parametrize("load", [{"limit": "x", "dropped": -1}, {"dropped": True}, "junk", None])
def test_a_front_load_the_launcher_cannot_read_is_left_out(load):
    verdict = v.evaluate(tor_profile(), TOR_TUNNEL, {**inside(["10.192.0.7"], None),
                                                     "dns": {**inside(["10.192.0.7"], None)["dns"], "front": load}},
                         {})
    assert verdict["failures"] == [] and verdict["warnings"] == []


# --- end to end: the real ./sandbox-ai with a Tor profile ------------------------------------------------------------

def test_e2e_the_tor_containers_own_lookups_go_to_tors_dnsport(tmp_path):
    from test_live_regressions_tor import Host, TOR_IMAGE, start_through_tor, tor_events
    host = Host(tmp_path)
    host.add_image(TOR_IMAGE)
    added = host.run("vpn", "add", "tor-test", "--tor", "--check-url", "https://localhost/ip", "--no-dns-probe")
    assert added.returncode == 0, added.stderr
    result = start_through_tor(host, {})
    assert result.returncode == 0, result.stderr
    asked = [e for e in tor_events(host) if "dns" in e]
    own = [e for e in asked if e["server"] == rt.TOR_DNS]
    outside = [e for e in asked if e["server"] == rt.OUTSIDE_RESOLVER]
    assert own and {e["port"] for e in own} == {rt.TOR_DNS_PORT}, asked  # past the front
    assert outside and {e["port"] for e in outside} == {53}  # the redirect to the front, as checked
