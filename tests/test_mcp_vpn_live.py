"""An MCP server behind a WireGuard VPN, in the kernel: tests/mcp_vpn_probe.py brings a real WireGuard tunnel up
between two network namespaces with sandbox/vpn_runtime.py's own setup (`vpn_runtime.py serve` with the payload of
an MCP image server's VPN container: kill switch, kernel WireGuard, routes, the redirect of its lookups), and a
process in the VPN's namespace - where an MCP server's container runs (--network=container:<the VPN container>) -
tries every way out. Proven here:

  - its connections reach the internet with the tunnel's address (10.66.0.2), never with the uplink's;
  - its lookups go to the VPN's resolver through the tunnel, whatever resolver it asks (the LAN's, a public one);
    over IPv6 none leaves;
  - nothing else leaves: not the LAN or the host behind the uplink (192.168.50.1, what pasta's gateway is), not the
    VPN's endpoint on another port, no UDP past the tunnel, no IPv6 at all (the tunnel has no IPv6 address);
  - with the VPN's server gone, with the tunnel's interface gone and a default route over the uplink offered
    again, and after the VPN's process ended: nothing leaves at all - no fallback to the uplink.
Without the VPN (the first phase) every one of these ways is open, so the test sees a leak where there is one.

Not here: Podman's part (the server's container in the VPN container's namespace, by its ID; the pod of a command
server) - tests/test_mcp_vpn_podman_live.py; pasta itself (the uplink is a veth pair here).
Needs nft, ip, unshare, nsenter, wg and a namespace with CAP_NET_ADMIN where WireGuard's kernel module is loaded
(sudo -n as in CI's killswitch job); skipped without them, and that job fails when it is skipped."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import ci_annotations

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "mcp_vpn_probe.py"
RUNTIME = ROOT / "sandbox" / "vpn_runtime.py"
TUNNEL, OWN, TARGET, LAN = "10.66.0.2", "192.168.50.2", "203.0.113.10", "192.168.50.1"
VPN_ANSWER, LAN_ANSWER = "198.51.100.7", "192.0.2.66"


def namespace_command():
    """How to run a command as root in a new network namespace with WireGuard here, or None."""
    if not all(shutil.which(tool) for tool in ("nft", "ip", "unshare", "nsenter", "wg")):
        return None
    candidates = [["unshare", "--net"]] if os.geteuid() == 0 else []
    if shutil.which("sudo"):
        candidates.append(["sudo", "-n", "unshare", "--net"])
    for prefix in candidates:
        try:
            done = subprocess.run([*prefix, "ip", "link", "add", "w0", "type", "wireguard"], capture_output=True,
                                  timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return prefix
    return None


PREFIX = namespace_command()
pytestmark = pytest.mark.skipif(PREFIX is None, reason="needs nft, ip, unshare, nsenter, wg and a network namespace "
                                                       "with CAP_NET_ADMIN and WireGuard's kernel module")


def report_text(found, stderr=""):
    """The probe's report for a failed CI step's annotations (tests/ci_annotations.py), what a failure needs first:
    what the server tried with the VPN up and who arrived outside, both ends' addresses, routes, connections and
    counters, the rules loaded - then the rest."""
    document = {}
    for phase in ("up", "before"):
        if isinstance(found.get(phase), dict):
            document[f"{phase}: results"] = found[phase].get("results")
            document[f"{phase}: arrived"] = found[phase].get("arrived")
        if phase == "up":
            for where, commands in (found.get("facts") or {}).items():
                document.update({f"{where}$ {command}": said for command, said in commands.items()})
            document["rules"] = found.get("rules") or ""
            document["events"] = found.get("events")
    document.update({key: value for key, value in found.items()
                     if key not in ("up", "before", "facts", "rules", "events")})
    if stderr.strip():
        document["the probe's standard error"] = stderr[-3000:]
    return ci_annotations.sections(document)


def said(found):
    """FOUND as an assertion's message: as text. (pytest writes an object given as the message with reprlib - four
    entries of a dict, then "..." - and that was all CI showed of the eleven attempts.)"""
    return json.dumps(found)


@pytest.fixture(scope="module")
def report():
    done = subprocess.run([*PREFIX, sys.executable, "-I", str(PROBE), str(RUNTIME)], capture_output=True, text=True,
                          timeout=600)
    try:
        found = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = None
    ci_annotations.report("mcp-vpn", report_text(found if isinstance(found, dict) else {"stdout": done.stdout[-3000:]},
                                                 done.stderr))
    assert done.returncode == 0, done.stderr[-3000:]
    assert isinstance(found, dict), f"the probe printed no report: {done.stdout[-1000:]!r}"
    assert found.get("up"), f"the VPN did not come up: {said(found.get('events'))} {found.get('stderr', '')}"
    return found


def peers(phase):
    return {peer for _, peer, _ in phase["arrived"]}


def test_without_the_vpn_every_way_is_open(report):
    """(the control: a leak would show)"""
    before = report["before"]["results"]
    assert before["tcp internet"] == before["tcp lan"] == before["tcp6 lan"] == "connected", said(before)
    assert before["dns lan resolver"] == [LAN_ANSWER] and before["dns public resolver"] == [LAN_ANSWER], said(before)
    assert OWN in peers(report["before"]) and "fd00:50::2" in peers(report["before"])


def test_the_vpn_comes_up_with_its_kill_switch_and_the_redirect(report):
    assert report["events"][-1]["event"] == "ready"
    assert report["events"][-1]["tunnel"]["mcp_dns"] == "10.66.0.1"
    assert report["events"][-1]["tunnel"]["mcp_dns_source"] == TUNNEL
    assert json.loads(report["killswitch"]) == {"ok": True}
    rules = report["rules"]
    assert "table inet sandbox_ai_vpn" in rules and "table inet sandbox_ai_mcp_dns" in rules
    # A redirected lookup leaves with the tunnel's own address (the kernel loaded the rule as it was written).
    assert f"snat ip to {TUNNEL}" in rules and "chain dns_source" in rules, rules


def test_its_connections_leave_only_through_the_tunnel_with_the_tunnels_address(report):
    up = report["up"]
    assert up["results"]["tcp internet"] == "connected"
    arrived = [entry for entry in up["arrived"] if entry[0] == "tcp"]
    assert arrived == [["tcp", TUNNEL, TARGET]], arrived  # the tunnel's address, and nothing else came


def test_its_lookups_go_to_the_vpns_resolver_whatever_it_asks(report):
    """The resolver on the uplink's own network (what pasta gives a container of a host whose resolver is on its
    LAN) and a public one: both answered by the VPN's resolver, and both asked with the TUNNEL's address - the lookup
    at the LAN's resolver was first routed to the uplink, with the uplink's address, which the VPN's server drops
    (CI: "no answer from 192.168.50.1")."""
    up = report["up"]
    facts = "\n".join(f"{where}$ {command}\n{text}" for where, commands in (report.get("facts") or {}).items()
                      for command, text in commands.items() if "conntrack" in command or "link show" in command)
    assert up["results"]["dns lan resolver"] == [VPN_ANSWER], f"{said(up)}\n{facts}"
    assert up["results"]["dns public resolver"] == [VPN_ANSWER], f"{said(up)}\n{facts}"
    lookups = [entry for entry in up["arrived"] if entry[0] == "dns"]
    assert len(lookups) >= 2 and all(entry == ["dns", TUNNEL, "10.66.0.1"] for entry in lookups), said(up["arrived"])
    assert up["results"]["dns6 lan resolver"].startswith("failed"), said(up["results"])


def test_nothing_else_leaves_lan_host_endpoint_udp_ipv6(report):
    results = report["up"]["results"]
    for name in ("tcp lan", "tcp endpoint other port", "tcp6 lan", "tcp6 internet"):
        assert results[name] in ("refused", "timeout"), (name, results[name])
    for name in ("udp lan", "udp internet", "udp6 lan"):
        assert results[name] in ("refused", "sent"), (name, results[name])
    assert OWN not in peers(report["up"]) and not any(":" in peer for peer in peers(report["up"])), \
        said(report["up"])
    udp = [entry for entry in report["up"]["arrived"] if entry[0] == "udp"]
    assert all(entry[1] == TUNNEL for entry in udp), udp  # (through the tunnel is fine; past it nothing)


@pytest.mark.parametrize("phase", ["peer down", "tunnel gone", "runtime ended"])
def test_with_the_tunnel_down_nothing_leaves_at_all(report, phase):
    found = report[phase]
    assert found["arrived"] == [], (phase, found["arrived"])
    results = found["results"]
    for name, outcome in results.items():
        if name.startswith(("tcp", "dns")):
            assert outcome in ("refused", "timeout") or str(outcome).startswith("failed"), (phase, name, outcome)


def test_the_kill_switch_outlives_the_vpns_process(report):
    assert report["runtime exit"] == 0
    assert json.loads(report["killswitch after"]) == {"ok": True}
