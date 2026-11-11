"""The run's kill switch (sandbox-ai net cut) in the kernel: tests/killswitch_probe.py loads the sandbox's firewall as
the launcher writes it, with the connection proxy's redirect, in a network namespace of its own, then the cut chain of
each mode as the supervisor loads it, and tries every way out with nftables. Each mode keeps exactly its part of what
the run may reach (judged by the destination the connection was meant for, also when the redirect handed it to the
proxy), never more, also behind Tor, with IPv6 and with two cuts at once; the tunnel's own packets pass;
connections opened before the cut stop at once. Needs nft, ip and a namespace with CAP_NET_ADMIN
(an unprivileged user namespace, or sudo -n as in CI); skipped without them."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_tor_killswitch_live import PREFIX

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "killswitch_probe.py"
LAUNCHER = ROOT / "sandbox" / "agent_sandbox.py"
pytestmark = pytest.mark.skipif(PREFIX is None, reason="needs nft, ip and a network namespace with CAP_NET_ADMIN")


@pytest.fixture(scope="module")
def report():
    done = subprocess.run([*PREFIX, sys.executable, "-I", str(PROBE), str(LAUNCHER)], capture_output=True, text=True,
                          timeout=600)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["report"]


# What no cut takes: the sandbox's own loopback (no network), and the tunnel's own packets (the VPN's mark; Tor's are
# its own user's, which root cannot be here): they carry only what passed the cut.
OWN = {"tcp own 127.0.0.1:4321": "connected", "udp dns stub 127.0.0.1:53": "sent",
       "udp tunnel (marked) 198.51.100.9:51820": "sent"}
# Without a cut: every TCP connection goes to the proxy (it checks the destination itself), UDP as the firewall allows.
NONE = {"tcp provider 192.0.2.10:443": "proxy", "tcp provider 192.0.2.10:80": "proxy", "tcp llm 10.0.0.5:11434": "proxy",
        "tcp lan 192.168.1.20:22": "proxy", "udp6 lan [fd00:1::20]:5353": "sent", "tcp6 lan [fd00:1::20]:80": "proxy",
        "tcp bypass 192.168.1.20:1234": "proxy",
        "tcp internet 198.51.100.7:443": "proxy", "tcp host port 127.0.0.1:8080": "proxy",
        "udp lan 192.168.1.20:5353": "sent", "udp internet 198.51.100.7:443": "sent", "udp dns 1.1.1.1:53": "sent",
        "udp other resolver 127.0.53.1:53": "sent", **OWN}
LAN = {"tcp llm 10.0.0.5:11434", "tcp lan 192.168.1.20:22", "udp6 lan [fd00:1::20]:5353", "tcp6 lan [fd00:1::20]:80",
       "tcp bypass 192.168.1.20:1234", "udp lan 192.168.1.20:5353"}
INTERNET = {"tcp provider 192.0.2.10:443", "tcp provider 192.0.2.10:80", "tcp internet 198.51.100.7:443",
            "udp internet 198.51.100.7:443", "udp dns 1.1.1.1:53", "udp other resolver 127.0.53.1:53"}
KEPT = {
    "remote": LAN | {"tcp host port 127.0.0.1:8080"},
    "local": INTERNET,
    "api": {"tcp provider 192.0.2.10:443", "tcp llm 10.0.0.5:11434"},
    "all": set(),
    # Behind Tor only the bypass target is the local network: everything else lies past Tor.
    "tor remote": {"tcp bypass 192.168.1.20:1234", "tcp host port 127.0.0.1:8080"},
    "tor local": INTERNET | LAN - {"tcp bypass 192.168.1.20:1234"},
    "tor api": {"tcp provider 192.0.2.10:443", "tcp llm 10.0.0.5:11434"},
    "tor all": set(),
    # Two cuts at once (a server shared by two runs): only what both keep.
    "remote+api": {"tcp llm 10.0.0.5:11434"},
}


def test_without_a_cut_the_run_reaches_what_it_may(report):
    assert report["none"] == NONE


@pytest.mark.parametrize("mode", sorted(KEPT))
def test_each_mode_keeps_exactly_its_part_and_never_widens(report, mode):
    assert set(report[mode]) == set(NONE)
    for name, outcome in report[mode].items():
        if name in OWN:
            assert outcome == OWN[name], (mode, name, outcome)  # never a cut's
        elif name in KEPT[mode]:
            assert outcome == NONE[name], (mode, name, outcome)  # as without a cut: nothing more
        else:
            assert outcome == "refused", (mode, name, outcome)


def test_connections_open_before_the_cut_stop_at_once(report):
    held = {"host port": {"local", "api", "all"}, "lan": {"local", "api", "all"}, "provider": {"remote", "all"}}
    for name, outcome in report["established"].items():
        what, mode = name.rsplit(" ", 1)
        assert outcome == ("held" if mode in held[what] else "delivered"), (name, outcome)
