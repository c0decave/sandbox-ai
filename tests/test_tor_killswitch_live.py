"""The Tor kill switch in the kernel: tests/tor_killswitch_probe.py loads sandbox/vpn_runtime.py's rules with nftables
in a network namespace of its own and tries every way out. Nothing may leave past Tor but exactly the address and port
--vpn-bypass-tcp names (TCP only): not another port of that machine, not another machine of the network, not UDP, not
IPv6, not the internet. Needs nft, ip and a namespace with CAP_NET_ADMIN (an unprivileged user namespace, or
sudo -n as in CI); skipped without them."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "tor_killswitch_probe.py"
RUNTIME = ROOT / "sandbox" / "vpn_runtime.py"


def namespace_command():
    """How to run a command as root in a new network namespace here, or None."""
    if not (shutil.which("nft") and shutil.which("ip") and shutil.which("unshare")):
        return None
    candidates = [["unshare", "--net"]] if os.geteuid() == 0 else []
    candidates += [["unshare", "--user", "--map-root-user", "--net"]]
    if shutil.which("sudo"):
        candidates.append(["sudo", "-n", "unshare", "--net"])
    for prefix in candidates:
        try:
            done = subprocess.run([*prefix, "ip", "link", "add", "d0", "type", "dummy"], capture_output=True,
                                  timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return prefix
    return None


PREFIX = namespace_command()
pytestmark = pytest.mark.skipif(PREFIX is None, reason="needs nft, ip and a network namespace with CAP_NET_ADMIN")


def probe(bypass, **options):
    done = subprocess.run([*PREFIX, sys.executable, "-I", str(PROBE), str(RUNTIME), json.dumps(bypass),
                           json.dumps(options)],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["results"]


def test_without_a_bypass_nothing_leaves_past_tor():
    results = probe([])
    for name, where in results.items():
        if name.startswith("tcp "):
            assert where == "tor", (name, where)  # every IPv4 TCP connection goes to tor's TransPort
        elif name in ("udp 192.168.1.20:53",):
            assert where == "tor", (name, where)  # a lookup goes to tor's DNSPort
        else:
            assert where == "refused", (name, where)  # other UDP and all IPv6


def test_the_bypass_lets_exactly_its_address_and_port_out():
    results = probe(["192.168.1.20:1234"])
    assert results.pop("tcp 192.168.1.20:1234") == "direct"
    for name, where in results.items():
        assert where != "direct", (name, where)  # nothing else of the network, nor the internet, past Tor
        if name.startswith("tcp "):
            assert where == "tor", (name, where)
    assert results["udp 192.168.1.20:1234"] == "refused"  # TCP only
    assert results["tcp6 [fd00:1::131]:1234"] == "refused"


def test_with_tor_gone_during_the_run_nothing_leaves():
    """Tor died, or its container ended, during the run: the kill switch is the namespace's and stays; with nothing
    behind tor's ports every connection is refused - nothing falls back past Tor."""
    results = probe([], tor=False)
    for name, where in results.items():
        if name == "udp 192.168.1.20:53":
            continue  # (redirected to tor's DNSPort on loopback, where nothing answers: no error comes back)
        assert where == "refused", (name, where)


def test_another_table_that_accepts_everything_opens_nothing_past_tor():
    """Another part of the run loading its own rules into the namespace later (the agent's firewall with
    --egress-all-allow, a run-time allow) cannot open what the kill switch drops: an accept in another base chain is
    not final, the kill switch's drop is."""
    assert probe([], other_table=True) == probe([])
