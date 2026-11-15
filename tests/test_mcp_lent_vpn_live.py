"""A start's WireGuard tunnel, lent to its own MCP servers, in the kernel: tests/mcp_lent_vpn_probe.py brings a real
WireGuard tunnel up in "the start's pod" with sandbox/vpn_runtime.py's own `serve` as the start's VPN container runs
it (kill switch, kernel WireGuard, routes, the lender on its Unix socket), loads the start's firewall as the
launcher writes it, and runs the same runtime as the borrowing VPN container of two servers, each in a network
namespace of its own: an MCP image server (its process in the VPN container's namespace) and a command server (its
sandbox's firewall with the relay's rules, its connections handed on by the connection proxy's user). The run's
folder, its token and the profile's lock are made by sandbox/vpn.py as a start makes them; the kill switch is set by
the supervisor's own RuntimeNetwork.set_cut (what `sandbox-ai net cut` reaches). Proven here:

  - a server's connections reach the internet with the TUNNEL's address (10.66.0.2: the exit is the WireGuard peer's
    side), its lookups the tunnel's resolver whatever resolver it asks - and nothing leaves by its own uplink, nor
    by the start's: not to the LAN or the host beside it, not the VPN's endpoint, not the tunnel's private side, no
    UDP, no IPv6. (Without the borrowing container - the first phase - every one of these ways is open.)
  - the start's own programs gain nothing from the lent mark: their connections go to the connection proxy;
  - only the run's token is served by the lender; a second sandbox with the same key is refused;
  - `sandbox-ai net cut` "all" and "remote" stop a lent connection that was open before (nothing more arrives, and
    it ENDS, at once), refuse new ones and lookups; "api" keeps only the start's model API; "local" keeps them (but
    a lookup at the tunnel's private resolver stops, as the agent's own does); lifted, everything is back;
  - a cut connection that says nothing ends at its next keepalive probe;
  - when the lender is killed, each borrower says "down" and ends, its kill switch stays, nothing leaves.

Not here: Podman's part (the containers, the mounts: tests/test_mcp_vpn_podman_live.py's kind) and the launcher's own
start (tests/test_hotfix_mcp_lent_vpn*.py through ./sandbox-ai with Podman faked).
Needs nft, ip, unshare, nsenter, wg, mount and a namespace with CAP_NET_ADMIN and CAP_SYS_ADMIN where WireGuard's
kernel module is loaded (sudo -n as in CI's killswitch job); skipped without them, and that job fails when it is
skipped."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_mcp_vpn_live import PREFIX

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "mcp_lent_vpn_probe.py"
RUNTIME = ROOT / "sandbox" / "vpn_runtime.py"
TUNNEL, RESOLVER = "10.66.0.2", "10.66.0.1"
INTERNET, API = "93.184.216.34", "160.79.104.10"
UPLINK = {"start": "192.168.50.2", "image": "192.168.60.2", "command": "192.168.70.2"}
VPN_ANSWER, LAN_ANSWER = "198.51.100.7", "192.0.2.66"
THROUGH = f"from {TUNNEL}"


def mount_namespace():
    """Whether a mount namespace can be made there too (each VPN container's own /run, and the lent tunnel's folder
    mounted into it): CAP_SYS_ADMIN, which sudo has."""
    if PREFIX is None or not shutil.which("mount"):
        return False
    try:
        return subprocess.run([*PREFIX, "unshare", "--mount", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not mount_namespace(), reason="needs nft, ip, unshare, nsenter, wg, mount and a "
                                "network and a mount namespace with CAP_NET_ADMIN, CAP_SYS_ADMIN and WireGuard's "
                                "kernel module")


@pytest.fixture(scope="module")
def report():
    done = subprocess.run([*PREFIX, sys.executable, "-I", str(PROBE), str(RUNTIME)], capture_output=True, text=True,
                          timeout=900)
    assert done.returncode == 0, f"the probe failed ({done.returncode}): {done.stderr[-3000:]}"
    found = json.loads(done.stdout.strip().splitlines()[-1])
    assert "setup" not in found, f"the probe's setup failed: {found['setup']}"
    return found


def phase(report, name):
    """The phase NAME of the report; one that failed in the probe fails here with what it said."""
    found = report.get(name)
    assert found is not None, f"the probe never came to the phase {name!r}; it has: {sorted(report)}"
    assert "error" not in found, f"the phase {name!r} failed in the probe: {found['error']} {found.get('where')}"
    return found


def wrong(found, expected):
    """Each attempt whose outcome is not one of those expected ({name: outcome or tuple of outcomes or a function}),
    with both: one CI run shows all of them."""
    lines = []
    for name, want in expected.items():
        got = found.get(name)
        fine = want(got) if callable(want) else got in want if isinstance(want, tuple) else got == want
        if not fine:
            lines.append(f"{name}: {got!r}, expected {want.__doc__ if callable(want) else repr(want)}")
    return lines


def failed(got):
    """a lookup that failed"""
    return isinstance(got, str) and got.startswith("failed")


def not_reached(got):
    """no answer of the far end (closed, refused or timeout)"""
    return got in ("closed", "refused", "timeout")


def peers(arrived, *kinds):
    return sorted({entry[1] for entry in arrived if not kinds or entry[0] in kinds})


def lines(arrived):
    return [entry[4] for entry in arrived if entry[0] == "line"]


def test_without_the_borrowing_container_every_way_is_open(report):
    """(the control: a leak would show - every connection arrives from the namespace's own address)"""
    before = phase(report, "before")
    own = f"from {UPLINK['image']}"
    problems = wrong(before["results"], {
        "tcp internet": own, "tcp api": own, "tcp own gateway": own, "tcp the start's gateway": own,
        "tcp the vpn endpoint": own, "tcp6 own gateway": "from fd00:60::2", "udp internet": "sent",
        "dns own resolver": [LAN_ANSWER], "dns public resolver": [LAN_ANSWER]})
    assert not problems, "without the VPN container:\n" + "\n".join(problems)
    assert UPLINK["image"] in peers(before["arrived"])


def test_the_start_comes_up_and_lends_its_tunnel(report):
    start = phase(report, "start")
    assert start["events"][-1]["event"] == "ready" and start["lends"] is True, start["events"]
    assert json.loads(start["killswitch"]) == {"ok": True}
    # Its firewall: the checks' mark before the cut chain, the lent mark right after it - never before.
    marks = start["firewall"]
    assert marks[:3] == ["meta mark 0xca6d accept", "jump cut", "meta mark 0xca6e accept"], marks
    assert "meta mark 0xca6e return" in marks, "past the connection proxy's redirect"


def test_only_the_runs_token_is_served(report):
    """Whoever reaches the lender's socket without the run's token (another run's borrower, any other process) gets
    nothing and learns nothing; with it, a connection leaves through the tunnel."""
    start = phase(report, "start")
    assert start["token file mode"] == "0o400" and start["socket mode"] == "0o600"
    assert start["mounted folder at first"] == [], "the token is not in the folder the containers mount"
    asked = start["asked"]
    problems = [f"{who}: {answer!r}" for who, answer in asked.items()
                if not who.startswith("the run's token") and not answer.startswith("LenderDenied")]
    assert not problems, "served without the run's token:\n" + "\n".join(problems)
    assert asked["the run's token: ping"] == "served (live)", asked
    assert asked["the run's token: tcp"] == f"served ({THROUGH})", asked
    assert [entry[:3] for entry in start["asked arrived"]] == [["tcp", TUNNEL, INTERNET]], \
        f"only the request with the token reached the internet: {start['asked arrived']}"


def test_a_second_sandbox_with_the_same_key_is_refused(report):
    refused = phase(report, "start")["second sandbox"]
    assert refused.startswith("VPN profile live is in use by another sandbox (the session lent-probe, PID "), refused
    assert "a WireGuard key works in one sandbox at a time" in refused


@pytest.mark.parametrize("server", ["image", "command"])
def test_a_servers_traffic_leaves_through_the_starts_tunnel_and_only_there(report, server):
    found = phase(report, server)
    assert found["events"][-1]["event"] == "ready", found["events"]
    assert found["tunnel"]["interface"] == "lo" and found["tunnel"]["lent"]["interface"] == "wg0", found["tunnel"]
    assert json.loads(found["killswitch"]) == {"ok": True}
    expected = {
        # The internet: reached, and the far end saw the tunnel's address - the exit is the WireGuard peer's side.
        "tcp internet": THROUGH, "tcp api": THROUGH,
        # Lookups: the tunnel's resolver, whatever resolver the server asks.
        "dns own resolver": [VPN_ANSWER], "dns public resolver": [VPN_ANSWER],
        # Everything that is not the internet: not lent.
        "tcp own gateway": not_reached, "tcp the start's gateway": not_reached,
        "tcp the tunnel's private side": not_reached, "tcp the vpn endpoint": not_reached,
        "udp internet": ("refused", "sent"), "udp own gateway": ("refused", "sent")}
    if server == "image":
        expected.update({"tcp6 own gateway": not_reached, "udp6 own gateway": ("refused", "sent")})
    else:
        # The sandbox's own resolver is the relay's; its programs never reach the relay's port themselves.
        expected.update({"dns the relay's resolver": [VPN_ANSWER], "tcp the relay itself": not_reached})
    problems = wrong(found["results"], expected)
    assert not problems, f"the {server} server behind the lent tunnel:\n" + "\n".join(problems)
    # Who arrived outside: the tunnel's address only - nothing by the server's uplink, nothing by the start's.
    arrived = found["arrived"]
    assert peers(arrived) == [TUNNEL], f"something left past the tunnel: {arrived}"
    assert sorted(entry[2:4] for entry in arrived if entry[0] == "tcp") == [[API, 443], [INTERNET, 8000]], arrived
    lookups = [entry for entry in arrived if entry[0] == "dns"]
    assert lookups and all(entry == ["dns", TUNNEL, RESOLVER] for entry in lookups), lookups
    assert not [entry for entry in arrived if entry[0] == "udp"], arrived


def test_the_starts_own_programs_gain_nothing_from_the_lent_mark(report):
    """The agent cannot mark a packet (no CAP_NET_ADMIN): its connections go to the connection proxy as ever (nothing
    listens there in the probe: refused), its other packets meet the allow-list."""
    agent = phase(report, "agent")
    problems = wrong(agent["results"], {"tcp internet": "refused", "tcp api, another port": "refused",
                                        "udp internet": "refused"})
    assert not problems, "the start's own programs:\n" + "\n".join(problems)
    assert agent["arrived"] == [], agent["arrived"]


STOPPED = {"all": {"internet", "api"}, "remote": {"internet", "api"}, "api": {"internet"}, "local": set()}


@pytest.mark.parametrize("mode", ["all", "remote", "api", "local"])
def test_net_cut_holds_a_lent_connection_that_was_open_before(report, mode):
    """The emergency stop. What the cut forbids: nothing the server says after it arrives outside, and the
    connection ends (the server sees it closed - it does not wait for minutes). What the cut keeps goes on."""
    found = phase(report, f"cut {mode}")
    assert found["cut"] == mode
    problems = []
    for name in ("internet", "api"):
        if found[f"open {name}"] != {"first": THROUGH} or "echo" not in found[f"before {name}"]:
            problems.append(f"before the cut the connection to {name} did not work: {found[f'open {name}']} "
                            f"{found[f'before {name}']}")
            continue
        after, said = found[f"after {name}"], f"after {mode} {name}"
        if name in STOPPED[mode]:
            if said in lines(found["arrived"]):
                problems.append(f"{name}: what the server said after the cut ARRIVED outside ({after})")
            if "ended" not in after:
                problems.append(f"{name}: the cut connection did not end within 10 s ({after}): it is held, but "
                                "the server waits")
        elif after != {"echo": f"echo {said}"} or said not in lines(found["arrived"]):
            problems.append(f"{name}: {mode} keeps it, but it did not go on ({after})")
    assert not problems, f"net cut {mode}, connections open before:\n" + "\n".join(problems) + \
        (f"\nthe cut chain: {found.get('rules')}" if mode == "all" else "")
    assert peers(found["arrived"]) in ([], [TUNNEL]), found["arrived"]


@pytest.mark.parametrize("mode", ["all", "remote", "api", "local"])
def test_net_cut_refuses_new_lent_connections_and_lookups_and_off_gives_them_back(report, mode):
    found = phase(report, f"cut {mode}")
    kept = lambda name: THROUGH if name not in STOPPED[mode] else not_reached  # noqa: E731
    # A lookup: cut by every mode but "local" - and by "local" here too, because this tunnel's resolver (as
    # Mullvad's) has a private address, which is the local network to the kill switch: as for the agent's own.
    problems = wrong(found["new"], {"tcp internet": kept("internet"), "tcp api": kept("api"), "dns": failed})
    assert not problems, f"net cut {mode}, new connections:\n" + "\n".join(problems)
    new = [entry[2:4] for entry in found["arrived"] if entry[0] == "tcp"]
    expected = [[target, port] for name, target, port in (("internet", INTERNET, 8000), ("api", API, 443))
                if name not in STOPPED[mode]]
    assert new == expected, f"net cut {mode}: connections that arrived outside {new}, expected {expected}"
    assert not [entry for entry in found["arrived"] if entry[0] == "dns"], found["arrived"]
    problems = wrong(found["lifted"], {"tcp internet": THROUGH, "tcp api": THROUGH, "dns": [VPN_ANSWER]})
    assert not problems, f"net cut {mode} lifted (off):\n" + "\n".join(problems)


def test_net_cut_holds_a_command_servers_lent_connections_too(report):
    found = phase(report, "cut all, command server")
    problems = []
    for name in ("internet", "api"):
        if found[f"open {name}"] != {"first": THROUGH} or "echo" not in found[f"before {name}"]:
            problems.append(f"before the cut the connection to {name} did not work: {found[f'open {name}']}")
        if f"after all {name}" in lines(found["arrived"]):
            problems.append(f"{name}: what the server said after the cut ARRIVED outside")
        if "ended" not in found[f"after {name}"]:
            problems.append(f"{name}: the cut connection did not end within 10 s ({found[f'after {name}']})")
    problems += wrong(found["new"], {"tcp internet": not_reached, "tcp api": not_reached, "dns": failed})
    problems += wrong(found["lifted"], {"tcp internet": THROUGH, "tcp api": THROUGH, "dns": [VPN_ANSWER]})
    assert not problems, "net cut all, the command server:\n" + "\n".join(problems)
    assert not [entry for entry in found["arrived"] if entry[0] in ("tcp", "dns")], found["arrived"]


def test_a_cut_connection_that_says_nothing_ends_too(report):
    """Nothing is sent after the cut: the connection's own keepalive probe meets it (vpn_runtime.LEND_KEEPALIVE)."""
    idle = phase(report, "idle")
    assert idle["open"] == {"first": THROUGH} and "echo" in idle["before"], idle
    assert "ended" in idle["after"], f"a cut connection that says nothing stayed open for {idle['seconds']} s: {idle}"
    assert idle["seconds"] <= 30, idle
    assert idle["arrived"] == [], idle["arrived"]


def test_when_the_lender_is_killed_the_borrowers_end_and_nothing_leaves(report):
    """The start's VPN container is gone (SIGKILL: no cleanup): each server's VPN container notices, says "down" and
    ends (exit status 3); its kill switch stays in its namespace, and the server reaches nothing - by no way."""
    found = phase(report, "lender killed")
    assert found["lender exit"] == -9, found["lender exit"]
    problems = []
    for server in ("image", "command"):
        ended = found[server]
        if ended.get("exit") != 3:
            problems.append(f"{server}: its VPN container did not end with status 3 within 90 s: {ended}")
            continue
        last = (ended["last"] or [{}])[0]
        if last.get("event") != "down" or "the start that lent its tunnel is gone" not in str(last.get("message")):
            problems.append(f"{server}: it did not say why it ended: {last}")
        if json.loads(ended["killswitch"]) != {"ok": True}:
            problems.append(f"{server}: its kill switch is gone: {ended['killswitch']}")
    for name, outcome in {**found["results"], **{f"command: {key}": value
                                                 for key, value in found["command results"].items()}}.items():
        if str(outcome).startswith("from ") or isinstance(outcome, list):
            problems.append(f"{name}: {outcome!r} - reached without the start's tunnel")
    assert not problems, "after the lender was killed:\n" + "\n".join(problems)
    assert found["arrived"] == [] and found["command arrived"] == [], (found["arrived"], found["command arrived"])


def test_nothing_of_the_lent_tunnel_is_left_on_the_host(report):
    assert report.get("left behind") is False
