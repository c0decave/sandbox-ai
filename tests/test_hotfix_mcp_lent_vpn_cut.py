"""Hotfix 0.1.96, what the security review of the lent tunnel left open (tests/test_hotfix_mcp_lent_vpn_security.py:
"not changed, reported") - a start's WireGuard / Mullvad / persistent Tailscale tunnel, lent to its own MCP servers:

  1. `sandbox-ai net cut` is an emergency stop, and the lent connections passed the start's firewall BEFORE its cut
     chain (they carried the checks' mark): a cut start's MCP servers went on reaching the internet through its
     tunnel. Now they carry a mark of their own (vpn_runtime.LEND_MARK), which the start's firewall accepts only
     AFTER the cut chain: every mode judges a lent packet by exactly the rules that judge the agent's own ("all" and
     "remote" stop it, "api" all but the start's own model API, "local" none - it reaches the internet only), and a
     lent TCP connection that was open before is ended with a reset (the agent's are closed by its connection proxy,
     which a lent one never passes).
  2. The lender started for EVERY such start and its failure stopped the start. Now a start without servers that
     borrow its tunnel goes on with a warning; a server attached to it later (mcp attach --live, mcp start --for) is
     refused with the reason. With such servers at its start it is still refused: they would reach nothing.
  3. Whoever had the mount could ask the lender. Now every request carries the run's token (256 random bits a run,
     on standard input only - never the environment, a command line or a log), compared in constant time: a
     borrower another run left behind, or any other container, is denied and ends.
  4. A start killed with SIGKILL (or a reboot) left vpn/.lend/RUN behind for good. `sandbox-ai gc` removes the
     folders (macOS: the volumes) of runs that have ended - never one of a live run.

The same in the kernel (real nftables, real WireGuard): tests/test_mcp_lent_vpn_live.py, CI's killswitch job."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import inspect
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("mcp_for_lent_vpn_cut", "sandbox/mcp.py")
l = m.launcher()
v = l.load_vpn_module()
rt = v.runtime()   # the VPN container's runtime (vpn_runtime.py)
srt = l.RUNTIME    # the sandbox's supervisor (runtime.py): the kill switch's rules
TOKEN, OTHER_TOKEN = "5a" * 32, "c3" * 32
FETCH = {"command": ["uvx", "mcp-server-fetch"], "options": ["--allow-pypi", "--egress-all-allow"]}
PRIVATE = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
WIREGUARD = f"""[Interface]
PrivateKey = {PRIVATE}
Address = 10.66.1.2/32
DNS = 10.66.0.1

[Peer]
PublicKey = {PEER}
AllowedIPs = 0.0.0.0/0
Endpoint = 198.51.100.7:51820
"""


@pytest.fixture
def folder():
    """A short folder for the lender's socket (a Unix socket's path has at most 107 bytes)."""
    short = Path(tempfile.mkdtemp(prefix="lend-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def profile(name, kind, **extra):
    sections = {"wireguard": {"addresses": ["10.66.1.2/32"], "private_key_secret": f"VPN_{name.upper()}_KEY",
                              "preshared_key_secrets": {}, "peers": []},
                "openvpn": {"config_file": "/dev/null", "remotes": [], "auth": False},
                "tailscale": {"ephemeral": False, "exit_node": "exit.example"}, "tor": {}}
    v.save_profile({"version": 1, "name": name, "type": kind, "provider": None, kind: {**sections[kind], **extra}})
    return v.load_profile(name)


# === 1. sandbox-ai net cut stops the lent connections ================================================================

def test_lent_sockets_carry_a_mark_of_their_own_which_no_kill_switch_of_a_vpn_knows():
    """Not the checks' mark (which passes the start's firewall before its cut chain), not the tunnel's own (which
    passes the VPN's kill switch past the tunnel), not tailscaled's."""
    assert len({rt.LEND_MARK, rt.CHECK_MARK, rt.MARK}) == 3
    mark, mask = rt.TAILSCALE_MARK
    assert rt.LEND_MARK & mask != mark
    assert inspect.signature(rt.lent_socket).parameters["mark"].default == rt.LEND_MARK
    for rules in (rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820)]),
                  rt.killswitch_rules("tailscale0", mark=mark, mask=mask, any_marked=True), rt.borrow_rules()):
        assert f"{rt.LEND_MARK:#x}" not in rules, "through the tunnel like any other packet, or not at all"


def output_chain(text):
    lines = text.splitlines()
    start = lines.index(" chain output { type filter hook output priority 0; policy drop;")
    return lines[start:lines.index(" }", start)]


def test_the_starts_firewall_accepts_a_lent_packet_only_after_the_cut_chain():
    """The finding: `meta mark CHECK accept` came before `jump cut`, and the lent sockets carried CHECK."""
    lent, checks = f"  meta mark {rt.LEND_MARK:#x} accept", f"  meta mark {rt.CHECK_MARK:#x} accept"
    text = l.firewall_rules(["192.168.1.0/24"], ["192.0.2.10"], proxy={"port": 15001, "loopback": [8080]},
                            vpn_mark="meta mark 0xca6c", check_mark=rt.CHECK_MARK, lend_mark=rt.LEND_MARK)
    output = output_chain(text)
    assert output[1:4] == [checks, "  jump cut", lent], output[:6]
    assert output.count(lent) == 1 and text.count(lent) == 1, "nowhere else, and never before the cut"
    # Not handed to the agent's connection proxy either (it is the server's connection, checked in its own sandbox).
    proxy = text.split(" chain proxy {", 1)[1]
    assert proxy.index(f"meta mark {rt.LEND_MARK:#x} return") < proxy.index("redirect to :15001")
    # A start that lends nothing has no such door at all.
    plain = l.firewall_rules(["192.168.1.0/24"], ["192.0.2.10"], proxy={"port": 15001, "loopback": []},
                             check_mark=rt.CHECK_MARK)
    assert f"{rt.LEND_MARK:#x}" not in plain
    with pytest.raises((ValueError, TypeError)):
        l.firewall_rules([], [], lend_mark="0xca6e accept; flush ruleset")


def test_only_a_start_that_lends_its_tunnel_gets_the_mark_into_its_firewall_and_kill_switch(home):
    for name, kind in (("wg", "wireguard"), ("tor", "tor"), ("ovpn", "openvpn"), ("ts", "tailscale")):
        profile(name, kind)
    profile("tse", "tailscale", ephemeral=True)

    def start(*argv):
        a = l.parse_args(["codex", *argv])
        v.validate(a)
        return a
    for name in ("wg", "ts"):
        a = start("--vpn", name)
        assert a.vpn_lend_mark == rt.LEND_MARK, name
        assert l.killswitch_config(a, {})["lend_mark"] == rt.LEND_MARK
    for argv in (["--vpn", "tor"], ["--vpn", "tse"],
                 ["--vpn", "wg", "--vpn-lent-by", "codex-1", "--mcp-socket-dir", str(home / "sock")],
                 ["--vpn", "wg", "--mcp-socket-dir", str(home / "own")]):
        a = start(*argv)
        assert a.vpn_lend_mark is None, argv
        assert l.killswitch_config(a, {})["lend_mark"] is None
    assert l.killswitch_config(l.parse_args(["codex"]), {})["lend_mark"] is None


SETTINGS = {"host_ports": [8080], "api": {"https": ["160.79.104.10"], "tcp": [["10.0.0.5", 11434]], "loopback": [],
                                          "names": ["api.anthropic.com"]},
            "tor": False, "bypass": [], "machine_host": None}
VPN_MARK = "meta mark 0xca6c"


def view(*modes):
    return {**srt.cut_settings(SETTINGS), "mode": srt.cut_label(list(modes)), "modes": list(modes)}


def chain_rules(text):
    """{chain: [rule]} of a cut_rules script."""
    found = {}
    for line in text.splitlines():
        if line.startswith("add rule inet agent_sandbox "):
            chain, rule = line[len("add rule inet agent_sandbox "):].split(" ", 1)
            found.setdefault(chain, []).append(rule)
    return found


LENT_RESET = f"meta mark {rt.LEND_MARK:#x} meta l4proto tcp reject with tcp reset"


@pytest.mark.parametrize("modes", [(mode,) for mode in srt.CUT_MODES] + [("remote", "api"), ("local", "api")])
def test_every_mode_judges_a_lent_packet_by_exactly_the_rules_that_judge_the_agents_own(modes):
    """The cut chain of a start that lends is the cut chain of any start, plus - before each rule that rejects - the
    same match for a lent TCP packet, answered with a reset. Nothing else: no rule lets a lent packet by that the
    agent's own packet would not pass."""
    plain = srt.cut_rules(view(*modes), VPN_MARK)
    lent = srt.cut_rules(view(*modes), VPN_MARK, rt.LEND_MARK)
    assert [line for line in lent.splitlines() if "tcp reset" not in line] == plain.splitlines()
    lines = lent.splitlines()
    rejects = [i for i, line in enumerate(lines) if line.endswith(srt.CUT_REJECT)]
    resets = [i for i, line in enumerate(lines) if line.endswith(LENT_RESET)]
    assert rejects and [i + 1 for i in resets] == rejects, "each rejecting rule has its twin right before it"
    for i in rejects:
        assert lines[i - 1] == lines[i][:-len(srt.CUT_REJECT)] + LENT_RESET, lines[i - 1]
    assert "tcp reset" not in plain and srt.cut_rules(view(*modes), VPN_MARK, None) == plain
    assert "tcp reset" not in srt.cut_rules(None, VPN_MARK, rt.LEND_MARK), "no cut: nothing is rejected"


def matches(rule, address, protocol, port, mark):
    """Whether the match part of RULE (the forms cut_mode_rules writes, and lent_reject's mark) takes a packet of a
    connection first opened to ADDRESS:PORT over PROTOCOL carrying MARK; (taken, the rule's verdict)."""
    ip = ipaddress.ip_address(address)
    text = rule
    for verdict in (srt.CUT_REJECT, "reject with tcp reset", "return"):
        if text.endswith(verdict):
            text = text[:-len(verdict)].strip()
            break
    else:
        raise AssertionError(f"a rule without a known verdict: {rule}")

    def members(value):
        return [part.strip() for part in value.strip("{} ").split(",")]
    patterns = [
        (r"ct original (ip6?) daddr (!= )?(\{[^}]*\}|\S+)", lambda g: (ip.version == (4 if g[0] == "ip" else 6)) and (
            any(ip in ipaddress.ip_network(net, strict=False) for net in members(g[2])) != bool(g[1]))),
        (r"meta l4proto (\{[^}]*\}|\S+)", lambda g: protocol in members(g[0])),
        (r"ct original proto-dst (\{[^}]*\}|\S+)", lambda g: str(port) in members(g[0])),
        (r"meta nfproto (ipv4|ipv6)", lambda g: ip.version == int(g[0][-1])),
        (r"meta mark (0x[0-9a-f]+)", lambda g: mark == int(g[0], 16)),
    ]
    taken = True
    while text:
        for pattern, test in patterns:
            found = re.match(pattern + r"\s*", text)
            if found:
                taken = taken and test(found.groups())
                text = text[found.end():]
                break
        else:
            raise AssertionError(f"a match this test does not know: {text!r} in {rule!r}")
    return taken, verdict


def fate(modes, address, protocol, port, mark=None, lend_mark=rt.LEND_MARK):
    """What the cut chain does with such a packet: "passes" (on to the output chain's own rules), "reset" or
    "rejected"."""
    chains = chain_rules(srt.cut_rules(view(*modes), VPN_MARK, lend_mark))
    for rule in chains["cut"]:
        if rule.startswith("jump "):
            for inner in chains[rule[len("jump "):]]:
                taken, verdict = matches(inner, address, protocol, port, mark)
                if taken and verdict != "return":
                    return "reset" if "tcp reset" in verdict else "rejected"
                if taken:
                    break
    return "passes"


# What an MCP server asks of the lent tunnel: the internet (TCP to public addresses), and its lookups at the tunnel's
# resolver - a public one, or (Mullvad) one with a private address.
LENT = {"the internet": ("93.184.216.34", "tcp", 443), "the internet, another port": ("93.184.216.34", "tcp", 8080),
        "the start's model API": ("160.79.104.10", "tcp", 443), "the API's address, another port": ("160.79.104.10",
                                                                                                    "tcp", 80),
        "a lookup at a public resolver": ("1.1.1.1", "udp", 53), "a lookup over TCP": ("1.1.1.1", "tcp", 53),
        "a lookup at the tunnel's private resolver": ("10.64.0.1", "udp", 53)}
KEPT = {"all": set(), "remote": set(),
        "local": {"the internet", "the internet, another port", "the start's model API",
                  "the API's address, another port", "a lookup at a public resolver", "a lookup over TCP"},
        "api": {"the start's model API"}}


@pytest.mark.parametrize("mode", srt.CUT_MODES)
def test_what_each_mode_does_to_a_lent_connection(mode):
    """all: everything stops. remote: the internet stops - which is all a lent connection reaches. local: they go on
    (the internet is not cut). api: only the start's own model API stays - a helper with another provider loses its
    network. TCP that stops is reset (the connection ends), a lookup is rejected."""
    found = {name: fate([mode], *target, mark=rt.LEND_MARK) for name, target in LENT.items()}
    expected = {name: "passes" if name in KEPT[mode] else "reset" if LENT[name][1] == "tcp" else "rejected"
                for name in LENT}
    assert found == expected


@pytest.mark.parametrize("mode", srt.CUT_MODES)
def test_a_lent_connection_fares_as_the_agents_own_to_the_same_place(mode):
    """The rule of thumb for the modes: whatever `net cut` says of the agent's traffic to a destination holds for
    its MCP servers' through the tunnel - the supervisor's own judgment (cut_allows) and the firewall's agree."""
    for name, (address, protocol, port) in LENT.items():
        allowed = srt.cut_allows(view(mode), address, port, protocol)
        assert (fate([mode], address, protocol, port, mark=rt.LEND_MARK) == "passes") == allowed, (mode, name)
        # ... and the agent's own packet meets the same rules, refused the old way (never with a reset).
        assert fate([mode], address, protocol, port) == ("passes" if allowed else "rejected"), (mode, name)


def test_two_cuts_at_once_keep_only_what_both_keep_of_a_lent_connection():
    assert fate(["local", "api"], *LENT["the start's model API"], mark=rt.LEND_MARK) == "passes"
    assert fate(["local", "api"], *LENT["the internet"], mark=rt.LEND_MARK) == "reset"
    assert fate(["remote", "api"], *LENT["the start's model API"], mark=rt.LEND_MARK) == "reset"


def test_the_supervisor_ends_lent_connections_with_the_cut_it_sets(tmp_path):
    """The supervisor's own state (RuntimeNetwork.set_cut, what `sandbox-ai net cut` reaches): with the lent mark of
    its config the cut chain it loads carries the resets; "off" takes them away with the rest."""
    applied = []
    network = srt.RuntimeNetwork({"loopback": [], "https": ["160.79.104.10/32"]}, tmp_path / "allow.json",
                                 applied.append)
    network.cut_settings, network.vpn_mark = srt.cut_settings(SETTINGS), VPN_MARK
    a = l.parse_args(["codex"])
    a.vpn_mark, a.vpn_lend_mark = VPN_MARK, rt.LEND_MARK  # (what vpn.validate gives a start that lends)
    network.lend_mark = srt.cut_lend_mark(l.killswitch_config(a, {}))
    assert network.lend_mark == rt.LEND_MARK
    network.set_cut("all")
    assert f"add rule inet agent_sandbox cut_all {LENT_RESET}" in applied[-1]
    assert applied[-1].rstrip().endswith(f"add rule inet agent_sandbox cut_all {srt.CUT_REJECT}")
    network.set_cut("off")
    assert "tcp reset" not in applied[-1] and "jump" not in applied[-1]
    # A run that lends nothing: as before.
    plain = srt.RuntimeNetwork({"loopback": []}, tmp_path / "allow2.json", applied.append)
    plain.cut_settings = srt.cut_settings(SETTINGS)
    plain.set_cut("all")
    assert "tcp reset" not in applied[-1]


@pytest.mark.parametrize("bad", ["0xca6e", 0, -1, 1 << 32, True, 51822.0, [51822], "51822 accept"])
def test_only_a_number_is_a_mark(bad):
    with pytest.raises(ValueError, match="bad mark"):
        srt.cut_lend_mark({"lend_mark": bad})
    with pytest.raises(ValueError):
        srt.cut_rules(view("all"), VPN_MARK, bad)


def test_a_lent_connection_that_says_nothing_still_meets_the_cut():
    """An idle connection sends no packet the cut could answer: it probes its far end (keepalive). No limit on
    unanswered data is set: that would end a connection whose far end only reads slowly."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert rt.keep_lent_alive(sock) is sock
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            idle, interval, count = rt.LEND_KEEPALIVE
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == idle
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == interval
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == count
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 0
    assert rt.LEND_KEEPALIVE[0] <= 30, "within half a minute of the cut"
    assert "keep_lent_alive(sock)" in inspect.getsource(rt.lent_connection)


# === 2. a lender that cannot start stops only a start that needs it ==================================================

class Runner(rt.Runner):
    def run(self, command, data=None, check=True):
        return subprocess.CompletedProcess(command, 0, "", "")


def piped(document):
    """Standard input as the launcher gives it: the payload's line, then closed (the start ended)."""
    read, write = os.pipe()
    os.write(write, (json.dumps(document) + "\n").encode())
    os.close(write)
    return open(read)


def served(monkeypatch, payload, socket_path):
    """rt.main(["serve"]) of a start's VPN container with PAYLOAD, its kernel stood in for: (exit status, events)."""
    monkeypatch.setattr(rt, "LEND_SOCKET", socket_path)
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner, **kw: {})
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: {"interface": "wg0", "dns": ["10.66.0.1"]})
    monkeypatch.setattr(rt, "save_serve_state", lambda tunnel, tailnet: None)
    out = io.StringIO()
    with piped(payload) as stdin:
        status = rt.main(["serve"], stdin=stdin, out=out, runner=Runner())
    return status, [json.loads(line) for line in out.getvalue().splitlines()], out.getvalue()


def lending(required, token=TOKEN):
    return {"profile": {"name": "wg", "type": "wireguard", "wireguard": {}}, "secrets": {"lend_token": token},
            "lend": {"required": required}, "checks": {"ip": False}}


def test_the_vpn_container_of_a_start_without_such_servers_comes_up_without_its_lender(monkeypatch, folder):
    """Before: any failure of the lender (its folder not mounted, its socket not bound) was the start's failure, for
    every WireGuard, Mullvad and persistent Tailscale start - also the many that have no MCP server at all."""
    missing = folder / "missing" / "lend.sock"
    status, events, text = served(monkeypatch, lending(False), missing)
    assert status == 0, text
    ready = events[-1]
    assert ready["event"] == "ready" and ready["tunnel"]["lends"] is False
    assert "the lent tunnel's socket" in ready["tunnel"]["lend_error"], "it says why"
    # With servers that need it now, the start does not go on: they would reach nothing.
    status, events, text = served(monkeypatch, lending(True), missing)
    assert status == 1 and events[-1]["event"] == "error" and '"ready"' not in text
    assert "cannot be lent to the start's MCP servers" in events[-1]["message"]
    # A launcher that does not say (an older one) is taken at its strictest.
    status, events, text = served(monkeypatch, {**lending(True), "lend": True}, missing)
    assert status == 1 and '"ready"' not in text
    # ... and where the lender starts, it lends, whatever was required.
    for required in (False, True):
        status, events, text = served(monkeypatch, lending(required), folder / "lend.sock")
        assert status == 0 and events[-1]["tunnel"]["lends"] is True and "lend_error" not in events[-1]["tunnel"]


def test_the_reason_a_tunnel_is_not_lent_never_shows_a_secret(monkeypatch, folder):
    def no_lender(self):
        raise OSError(f"bind failed for the run with {TOKEN} and the key {PRIVATE}")
    monkeypatch.setattr(rt.Lender, "start", no_lender)
    payload = lending(False)
    payload["secrets"]["private_key"] = PRIVATE
    status, events, text = served(monkeypatch, payload, folder / "lend.sock")
    assert status == 0 and events[-1]["tunnel"]["lends"] is False
    assert TOKEN not in text and PRIVATE not in text and "[secret]" in events[-1]["tunnel"]["lend_error"]


def start_namespace(run="codex-1a2b3c4d"):
    return argparse.Namespace(run_id=run, backend="podman", podman_url=None, vpn_lends=True)


def test_the_launcher_refuses_a_start_whose_servers_need_the_tunnel_and_lets_any_other_go_on(home):
    wg = profile("wg", "wireguard")
    said = []
    ready = {"tunnel": {"interface": "wg0", "lends": False, "lend_error": "the socket\x1b[31m cannot be bound"}}
    a = start_namespace()
    with contextlib.ExitStack() as cleanup:
        v.prepare_lending(a, ["podman"], cleanup)
        with pytest.raises(ValueError, match="could not be lent to the start's MCP servers.*would reach nothing"):
            v.not_lent(a, wg, ready, True, said.append)
        assert a.vpn_lends is True and said == [] and v.lent_tunnel_problem(a.run_id) == v.NOT_LENDING
        v.not_lent(a, wg, ready, False, said.append)
        assert a.vpn_lends is False
        assert len(said) == 1 and said[0].startswith("warning: the tunnel of wg is not lent to MCP servers")
        assert "The start goes on" in said[0] and "mcp attach NAME START --live" in said[0]
        assert "\x1b" not in said[0], "what the container said is shown plainly"
        # What a server attached later is told: the reason, not "it has ended".
        problem = v.lent_tunnel_problem(a.run_id)
        assert "could not lend the tunnel when it started (the socket" in problem and "restart that start" in problem
        assert not v.lent_tunnel_up(a.run_id) and not v.lent_tunnel_up(a.run_id, machine=True)
    log = (home / "state/sandbox-ai/launcher.log").read_text()
    assert "is not lent to MCP servers" in log and "WARNING" in log


def test_a_networked_server_attached_to_a_start_that_does_not_lend_is_refused_with_the_reason(home, monkeypatch):
    """`sandbox-ai mcp attach NAME START --live` and `mcp start NAME --for START` both start the server as the
    running start would (ensure_running): refused before anything of it starts, saying why the tunnel is not lent."""
    wg = profile("wg", "wireguard")
    monkeypatch.setattr(m, "definitions", lambda project=None: {"fetch": FETCH, "files": {
        "command": ["x"], "options": ["--offline"]}})
    started = []
    monkeypatch.setattr(m, "start_logged", lambda *args, **kwargs: started.append(args) or pytest.fail("started"))
    a = l.parse_args(["codex", "--vpn", "wg"])
    a.vpn_profile, a.project_document, a.run_id = wg, None, "codex-1a2b3c4d"
    start = start_namespace(a.run_id)
    with contextlib.ExitStack() as cleanup:
        v.prepare_lending(start, ["podman"], cleanup)
        v.not_lent(start, wg, {"tunnel": {"lends": False, "lend_error": "its folder is not mounted"}}, False,
                   lambda text: None)
        behind = m.server_vpn(a, "fetch")
        assert m.lent_of(behind) == (True, a.run_id)
        with pytest.raises(ValueError) as refused:
            m.ensure_running("fetch", a, behind)
        text = str(refused.value)
        assert "borrows the tunnel of the run codex-1a2b3c4d (--vpn wg), which does not lend it now" in text
        assert "could not lend the tunnel when it started (its folder is not mounted)" in text
        assert "restart that start" in text and started == []
        # A server that reaches no network needs no tunnel: nothing of this concerns it.
        assert m.server_vpn(a, "files") is None
    # The run has ended: what is said then is that, as before.
    with pytest.raises(ValueError, match="it has ended, or it began before its tunnel was lent"):
        m.ensure_running("fetch", a, m.server_vpn(a, "fetch"))


# --- the same through ./sandbox-ai ---------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    found = Host(tmp_path / "host")
    found.env["SANDBOX_AI_SECRETS"] = "file"
    found.env["FAKE_VPN_DIR"] = str(found.directory / "vpn-containers")
    return found


def add_wireguard(host, name="wg"):
    conf = host.directory / f"{name}.conf"
    conf.write_text(WIREGUARD)
    conf.chmod(0o600)
    done = host.run("vpn", "add", name, "--wireguard", str(conf))
    assert done.returncode == 0, done.stderr
    checks = host.home / ".config/sandbox-ai/vpn/checks.json"
    checks.write_text(json.dumps({"urls": ["https://echo.invalid/json"], "dns_probe": None, "timeout": 1,
                                  "attempts": 1}))
    checks.chmod(0o600)


def add_fetch(host):
    path = host.home / ".config/sandbox-ai/mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetch": FETCH}))


def events(host):
    path = Path(host.env["FAKE_VPN_DIR"]) / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def vpn_runs(host):
    return [call for call in host.calls() if call[:1] == ["run"] and "localhost/sandbox-ai-vpn:latest" in call]


NO_LENDER = "the socket of the lent tunnel cannot be bound here"


def test_e2e_a_start_without_mcp_servers_goes_on_when_its_tunnel_cannot_be_lent(host):
    add_wireguard(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_VPN_LEND_FAIL": NO_LENDER}, timeout=300)
    said = done.stdout + done.stderr
    ups = [event for event in events(host) if event.get("up")]
    assert [(event["up"], event["lend"], event["required"]) for event in ups] == [("wireguard", True, False)], said
    assert f"warning: the tunnel of wg is not lent to MCP servers ({NO_LENDER})" in done.stderr, said[-3000:]
    assert "The start goes on" in done.stderr and "the VPN did not come up" not in said
    # It went on to its agent's container (the fake ends there).
    runs = [call for call in host.calls() if call[:1] == ["run"] and "localhost/codex-sandbox:latest" in call]
    assert runs, "the agent's container was started"
    assert [path.name for path in (host.home / ".local/share/sandbox-ai/vpn/.lend").iterdir()] == [".lock"], \
        "nothing of the run is left"


def test_e2e_a_start_with_a_networked_mcp_server_is_refused_when_its_tunnel_cannot_be_lent(host):
    add_wireguard(host)
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", "--mcp", "fetch",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_VPN_LEND_FAIL": NO_LENDER}, timeout=300)
    said = done.stdout + done.stderr
    assert done.returncode != 0, said[-3000:]
    assert f"the tunnel cannot be lent to the start's MCP servers: {NO_LENDER}" in done.stderr, said[-3000:]
    ups = [event for event in events(host) if event.get("up")]
    assert [(event["up"], event["required"]) for event in ups] == [("wireguard", True)], "no server borrowed anything"
    assert len(vpn_runs(host)) == 1
    assert not any(call[:1] == ["run"] and "localhost/codex-sandbox:latest" in call for call in host.calls()), \
        "neither the agent nor the server started"
    assert not (host.home / ".local/share/sandbox-ai/vpn/wg/.holder").exists(), "the profile's lock is free again"


# === 3. the run's token =============================================================================================

def held(folder, token=TOKEN, **kw):
    reached = []
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock", token=token,
                       connect=lambda target: reached.append(target) or pytest.fail("connected"),
                       lookup=lambda query, resolver: reached.append(resolver) or pytest.fail("asked"), **kw).start()
    return lender, reached


def test_no_lender_without_a_token():
    for token in (None, "", "short", "5A" * 32, "5a" * 33, 5, ["5a" * 32]):
        with pytest.raises(ValueError, match="no token") as refused:
            rt.Lender({"interface": "wg0", "dns": []}, "wg", token=token)
        assert "5a5a" not in str(refused.value).lower()
    assert rt.Lender({"interface": "wg0", "dns": []}, "wg", token=TOKEN).token == TOKEN


@pytest.mark.parametrize("request_", [{"op": "ping"}, {"op": "tcp", "address": "93.184.216.34", "port": 443},
                                      {"op": "dns", "query": "AAAAAAAAAAAAAAAAAAAA"}])
@pytest.mark.parametrize("token", [None, OTHER_TOKEN, "", TOKEN[:-1], TOKEN + "0", TOKEN.upper(), 5, [TOKEN],
                                   {"t": TOKEN}, "\ud800" * 64, "5a" * 4000])
def test_every_request_without_the_runs_token_is_denied_and_nothing_is_done(folder, request_, token):
    lender, reached = held(folder)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(folder / "lend.sock"))
        document = {**request_, **({} if token is None else {"token": token})}
        try:
            sock.sendall(json.dumps(document).encode() + b"\n")
        except OSError:
            pass
        answer = json.loads(rt.read_line(sock, rt.LEND_ANSWER))
        assert sock.recv(10) == b"", "closed"
        sock.close()
        if isinstance(token, str) and len(token) > rt.LEND_LINE // 2:
            assert "error" in answer and "ok" not in answer  # (too long a line: no request at all)
        else:
            assert answer == {"error": rt.LEND_DENIED, "denied": True}, "nothing about the tunnel, not even its profile"
        assert reached == []
        # ... and the same request with the token is served (or refused for its own reason: never denied).
        assert lender.slots.acquire(timeout=5), "its slot came back"
        lender.slots.release()
    finally:
        lender.close()


def test_the_runs_own_borrower_is_served_and_a_foreign_one_is_denied_with_real_sockets(folder, monkeypatch):
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock",
                       token=TOKEN).start()
    try:
        facts, sock = rt.ask_lender({"op": "ping"}, folder / "lend.sock", token=TOKEN)
        sock.close()
        assert facts["profile"] == "wg" and "token" not in facts
        for token in (OTHER_TOKEN, None):
            monkeypatch.setattr(rt, "_lend_token", None)
            with pytest.raises(rt.LenderDenied, match="denied it: this is not the token"):
                rt.ask_lender({"op": "ping"}, folder / "lend.sock", token=token)
        assert not issubclass(rt.LenderDenied, rt.LenderRefused) and issubclass(rt.LenderDenied, OSError)
        # A borrowing container: use_lend_token once, every request of its relay carries it.
        monkeypatch.setattr(rt, "_lend_token", None)
        rt.use_lend_token(TOKEN)
        assert rt.lender_gone("wg", folder / "lend.sock") is None
        assert rt.lender_facts("wg", folder / "lend.sock", wait=0)["profile"] == "wg"
        with pytest.raises(ValueError, match="no token"):
            rt.use_lend_token("not one")
    finally:
        lender.close()


def test_a_borrower_another_run_left_behind_ends_and_reaches_nothing_meanwhile(folder, monkeypatch):
    """The folder (macOS: the volume) of a run's name, used again by its next start: the new lender has a new token.
    The old borrower's look is denied - for it the lender is gone (not "full": it ends) - and so is every connection
    and every lookup of its relay."""
    lender, reached = held(folder, token=OTHER_TOKEN)
    monkeypatch.setattr(rt, "_lend_token", TOKEN)  # (the old run's)
    try:
        gone = rt.lender_gone("wg", folder / "lend.sock")
        assert gone is not None and "denied" in gone
        waited = []
        with pytest.raises(ValueError, match="is not lent to this server"):
            rt.lender_facts("wg", folder / "lend.sock", wait=30, sleep=waited.append)
        assert waited == [], "at once: waiting changes nothing"
        borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443))
        left, right = socket.socketpair()
        borrower.relay(right)
        assert left.recv(100) == b"", "closed at once"
        left.close()
        with pytest.raises(OSError, match="denied"):
            borrower.lookup(rt.dns_query_packet("example.com", 1, 1))
        assert reached == []
    finally:
        lender.close()


def test_the_token_is_compared_in_constant_time(folder, monkeypatch):
    compared = []
    real = rt.hmac.compare_digest
    monkeypatch.setattr(rt.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    assert rt.token_matches(TOKEN, TOKEN) and not rt.token_matches(OTHER_TOKEN, TOKEN)
    assert compared == [(TOKEN.encode(), TOKEN.encode()), (OTHER_TOKEN.encode(), TOKEN.encode())]
    source = inspect.getsource(rt.token_matches)
    assert "==" not in source and "compare_digest" in source
    lender, _ = held(folder)
    try:
        compared.clear()
        for token in (TOKEN, OTHER_TOKEN):
            with contextlib.suppress(OSError):
                rt.ask_lender({"op": "ping"}, folder / "lend.sock", token=token)[1].close()
        assert len(compared) == 2, "each request"
    finally:
        lender.close()


def test_a_borrowing_container_without_the_token_borrows_nothing_and_its_output_never_shows_one(monkeypatch, folder):
    lender, _ = held(folder)
    monkeypatch.setattr(rt.Borrower, "start", lambda self: self)
    monkeypatch.setattr(rt, "_lend_token", None)
    monkeypatch.setattr(rt, "LEND_READY", 0)
    borrowing = {"profile": {"name": "wg", "type": "wireguard", "wireguard": {}}, "checks": {"ip": False},
                 "borrow": {"run": "codex-1"}}
    try:
        for secrets_ in ({}, {"lend_token": "nothing"}, {"lend_token": None}):
            monkeypatch.setattr(rt, "_lend_token", None)
            status, found, text = served(monkeypatch, {**borrowing, "secrets": secrets_}, folder / "lend.sock")
            assert status == 1 and found[-1]["event"] == "error" and "no token" in found[-1]["message"], text
        # Another run's token: denied, said without it.
        monkeypatch.setattr(rt, "_lend_token", None)
        status, found, text = served(monkeypatch, {**borrowing, "secrets": {"lend_token": OTHER_TOKEN}},
                                     folder / "lend.sock")
        assert status == 1 and "is not lent to this server" in found[-1]["message"], text
        assert OTHER_TOKEN not in text and TOKEN not in text
        # Its own run's: borrowed.
        monkeypatch.setattr(rt, "_lend_token", None)
        status, found, text = served(monkeypatch, {**borrowing, "secrets": {"lend_token": TOKEN}},
                                     folder / "lend.sock")
        assert status == 0 and found[-1]["event"] == "ready" and TOKEN not in text
    finally:
        lender.close()


def test_the_launcher_makes_a_token_for_each_run_in_a_file_only_the_user_reads_and_no_container_mounts(home):
    profile("wg", "wireguard")
    tokens = []
    for run in ("codex-1", "codex-2"):
        with contextlib.ExitStack() as cleanup:
            lend, token = v.prepare_lending(start_namespace(run), ["podman"], cleanup)
            tokens.append(token)
            path = v.lend_dir(run) / "token"
            assert rt.LEND_TOKEN.fullmatch(token) and path.read_text() == token
            assert oct(path.stat().st_mode & 0o777) == "0o400" and path.stat().st_uid == os.getuid()
            assert v.lend_token(run) == token
            # What the VPN containers mount is the folder of the socket beside it, never the run's folder.
            for lender in (True, False):
                mount = v.lend_mount(run, lender=lender)[1]
                assert f"src={v.lend_socket_dir(run)}," in mount and v.lend_socket_dir(run) != v.lend_dir(run)
            assert not (v.lend_socket_dir(run) / "token").exists()
            assert v.lend_socket_dir(run).parent == v.lend_dir(run)
        assert not v.lend_dir(run).exists()
        with pytest.raises(ValueError, match="does not lend its tunnel now"):
            v.lend_token(run)
    assert tokens[0] != tokens[1], "a new one for each run"


def test_a_token_file_that_is_not_the_users_own_private_one_is_no_token(home):
    profile("wg", "wireguard")
    with contextlib.ExitStack() as cleanup:
        _lend, token = v.prepare_lending(start_namespace("codex-1"), ["podman"], cleanup)
        path = v.lend_dir("codex-1") / "token"
        path.chmod(0o440)
        with pytest.raises(ValueError, match="does not lend its tunnel now") as refused:
            v.lend_token("codex-1")
        assert token not in str(refused.value)
        path.chmod(0o600)
        path.write_text("not a token " + token)
        with pytest.raises(ValueError, match="holds no token") as refused:
            v.lend_token("codex-1")
        assert token not in str(refused.value), "what it holds is never shown"
        path.unlink()
        elsewhere = home / "elsewhere"
        elsewhere.write_text(token)
        elsewhere.chmod(0o400)
        path.symlink_to(elsewhere)
        with pytest.raises(ValueError, match="does not lend its tunnel now"):
            v.lend_token("codex-1")
        assert not v.lent_tunnel_up("codex-1")
    with pytest.raises(ValueError, match="not a run's name"):
        v.lend_token("../codex-1")


def test_the_token_goes_to_the_containers_on_standard_input_only(home, monkeypatch):
    """bring_up(): the lender gets its run's token with the profile's secrets, a borrower gets nothing but the token -
    in the payload written to the container's standard input; the command line and the log never hold it."""
    wg = profile("wg", "wireguard")
    given = []

    class Tunnel:
        def __init__(self, command, data, say=None):
            given.append((command, data))

        def wait_ready(self, timeout=None):
            return {"event": "ready", "tunnel": {"interface": "wg0", "lends": True}, "checks": {}}
    monkeypatch.setattr(v, "Tunnel", Tunnel)
    monkeypatch.setattr(v, "payload", lambda profile, action, names=(), bypass=(): {
        "version": 1, "action": action, "profile": profile, "secrets": {"private_key": PRIVATE}, "resolve": [],
        "checks": {"ip": False}})
    monkeypatch.setattr(v, "baseline_of", lambda checks, podman=None: {})
    monkeypatch.setattr(v, "evaluate", lambda *args, **kw: {"failures": [], "warnings": [], "notes": []})
    monkeypatch.setattr(v, "report", lambda *args, **kw: None)
    monkeypatch.setattr(v, "log_record", lambda *args, **kw: None)
    with contextlib.ExitStack() as cleanup:
        lend, token = v.prepare_lending(start_namespace("codex-1"), ["podman"], cleanup)
        lender = v.container_command(["podman"], "agent-codex-1-vpn", wg, pod="agent-codex-1", lend=lend)
        v.bring_up(wg, lender, (), lambda text: None, lend={"token": token, "required": True})
        borrower = v.container_command(["podman"], "agent-mcp-f-vpn", wg, pod="agent-mcp-f",
                                       lend=("borrower", "codex-1", False))
        v.bring_up(wg, borrower, (), lambda text: None, mcp=True, borrow="codex-1")
        (_, lent), (_, borrowed) = given
        assert lent["lend"] == {"required": True}
        assert lent["secrets"] == {"private_key": PRIVATE, "lend_token": token}
        assert borrowed["borrow"] == {"run": "codex-1"} and borrowed["secrets"] == {"lend_token": token}, \
            "no key of the profile, only its run's token"
        for command, data in given:
            assert not any(token in str(part) for part in command)
            assert token not in json.dumps({key: value for key, value in data.items() if key != "secrets"})
        with pytest.raises(ValueError, match="no token"):
            v.bring_up(wg, lender, (), lambda text: None, lend={"token": None, "required": False})
    with pytest.raises(ValueError, match="does not lend its tunnel now"):
        v.bring_up(wg, borrower, (), lambda text: None, mcp=True, borrow="codex-1")
    assert len(given) == 2, "no container was started for a run that lends nothing"
    assert token not in (home / "state/sandbox-ai/launcher.log").read_text()
    assert rt.secret_values(lent) == sorted({PRIVATE, token}, key=len, reverse=True), "redacted wherever it shows"


def test_an_image_servers_wrapper_borrows_through_the_volume_on_macos(home, monkeypatch):
    """vpn.py --internal-mcp-borrow (a catalog server's VPN container) runs with this host's Podman: on macOS that is
    podman machine's VM, where the start's lender has its socket in the run's volume - the wrapper mounted the Mac's
    folder, where no socket is."""
    wg = profile("wg", "wireguard")
    seen = []

    def bring_up(profile, command, names, say, **kwargs):
        seen.append(command)
        raise ValueError("stop here")
    monkeypatch.setattr(v, "bring_up", bring_up)
    monkeypatch.setattr(v, "podman_run", lambda command, **kw: subprocess.CompletedProcess(command, 1, "", ""))
    for platform, mount in (("darwin", "type=volume,src=sandbox-ai-lend-codex-1,dst=/run/sandbox-ai-lend,ro"),
                            ("linux", f"type=bind,src={v.lend_socket_dir('codex-1')},dst=/run/sandbox-ai-lend,ro")):
        monkeypatch.setattr(v.sys, "platform", platform)
        with pytest.raises(ValueError, match="stop here"):
            v.mcp_wrapper(wg["name"], "sandbox-ai-vpn-mcp-f.0123abcd", "/owner", ["true"], lent_by="codex-1")
        assert mount in seen[-1], (platform, seen[-1])


def test_e2e_the_runs_token_reaches_lender_and_borrower_on_standard_input_and_nothing_else_holds_it(host):
    add_wireguard(host)
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", "--mcp", "fetch",
                    env={"FAKE_PODMAN_RUN": "0:0"}, timeout=300)
    said = done.stdout + done.stderr
    seen = events(host)
    tokens = [event for event in seen if "token" in event]
    assert len(tokens) == 2 and tokens[0]["container"] != tokens[1]["container"], (seen, said[-2000:])
    assert tokens[0]["token"] == tokens[1]["token"], "the server's borrower has its start's token"
    assert not any(event["token_in_argv"] or event["token_in_env"] for event in tokens)
    ups = [event for event in seen if event.get("up")]
    assert ups[0]["required"] is True, "this start has a server that needs the tunnel"
    assert any(event.get("lender") == "ping" for event in seen), "and with it the borrower was served"
    kept = sorted((Path(host.env["FAKE_VPN_DIR"]) / "tokens").iterdir())
    token = kept[0].read_text()
    assert rt.LEND_TOKEN.fullmatch(token) and kept[1].read_text() == token
    assert hashlib.sha256(token.encode()).hexdigest()[:16] == tokens[0]["token"]
    # Nowhere else: not in what was said, not in a podman command line, not in the launcher's log, not left on disk.
    assert token not in said and token not in host.log_path.read_text()
    for path in (host.home / ".local/state").rglob("*"):
        if path.is_file():
            assert token not in path.read_text(errors="replace"), path
    assert [path.name for path in (host.home / ".local/share/sandbox-ai/vpn/.lend").iterdir()] == [".lock"], \
        "its file went with the start"


# === 4. sandbox-ai gc removes what killed starts left ================================================================

def leftover(run, root=None):
    """What a start of RUN that was killed left: its folder with the socket's folder, a socket file, its token and
    the lock its launcher held."""
    directory = (root or v.lend_root()) / run
    (directory / "socket").mkdir(parents=True)
    (directory / "socket" / "lend.sock").write_text("")
    (directory / "token").write_text(TOKEN)
    (directory / "token").chmod(0o400)
    (directory / "lock").write_text("")
    return directory


def test_gc_removes_the_folder_of_an_ended_run_and_never_one_of_a_live_run(home):
    profile("wg", "wireguard")
    dead = leftover("codex-0000dead0000")
    orphaned = leftover("codex-0000beef0000")   # its launcher is gone, its pod still runs: left alone
    asked = []

    def alive(run):
        asked.append(run)
        return run == "codex-0000beef0000"
    with contextlib.ExitStack() as cleanup:
        _lend, token = v.prepare_lending(start_namespace("codex-0000a11e0000"), ["podman"], cleanup)
        live = v.lend_dir("codex-0000a11e0000")
        # A dry run names it and removes nothing.
        assert v.lend_leftovers(alive, remove=False) == ["codex-0000dead0000"]
        assert dead.exists() and (dead / "token").exists()
        # Even if nothing else knew the live run (its record gone, Podman silent): its launcher holds its folder.
        assert v.lend_leftovers(lambda run: False, remove=False) == ["codex-0000beef0000", "codex-0000dead0000"]
        assert v.lend_leftovers(alive) == ["codex-0000dead0000"]
        assert not dead.exists() and orphaned.exists() and live.exists()
        assert v.lend_token("codex-0000a11e0000") == token and (live / "socket").is_dir()
        assert v.lend_leftovers(lambda run: False) == ["codex-0000beef0000"]
        assert live.exists(), "held by a launcher that runs: never removed"
        assert v.lend_leftovers(alive) == []
    assert sorted(set(asked)) == ["codex-0000a11e0000", "codex-0000beef0000", "codex-0000dead0000"]


def test_gc_touches_only_runs_folders_and_follows_no_link(home):
    profile("wg", "wireguard")
    root = v.lend_root()
    leftover("codex-0000dead0000")
    documents = home / "documents"
    documents.mkdir()
    (documents / "letter.txt").write_text("mine")
    (root / "codex-0000a1b20000").symlink_to(documents, target_is_directory=True)
    (root / ".hidden").mkdir()
    (root / "not a run").mkdir()
    (root / "notes.txt").write_text("mine too")
    assert v.lend_leftovers(lambda run: False) == ["codex-0000a1b20000", "codex-0000dead0000", "notes.txt"]
    assert (documents / "letter.txt").read_text() == "mine", "the link went, not what it named"
    assert sorted(path.name for path in root.iterdir()) == [".hidden", ".lock", "not a run"]
    # The folder of the lent tunnels a link itself: nothing is done through it.
    shutil.rmtree(root)
    (documents / "codex-0000dead0000").mkdir()
    root.symlink_to(documents, target_is_directory=True)
    assert v.lend_leftovers(lambda run: False) == [] and (documents / "codex-0000dead0000").is_dir()
    assert sorted(path.name for path in documents.iterdir()) == ["codex-0000dead0000", "letter.txt"]
    root.unlink()
    root.mkdir()
    # No folder of lent tunnels at all (never lent anything): nothing to do, nothing made.
    shutil.rmtree(root)
    assert v.lend_leftovers(lambda run: pytest.fail("asked")) == [] and not root.exists()


def test_gc_removes_the_volume_of_an_ended_run_on_macos_without_force(home, monkeypatch):
    """macOS: the socket lies in a Podman volume (sandbox-ai-lend-RUN). `podman volume rm` without --force: Podman
    itself refuses one a container still uses."""
    profile("wg", "wireguard")
    leftover("codex-0000dead0000")
    calls = []

    def podman_run(command, **kwargs):
        calls.append(command)
        listed = "sandbox-ai-lend-codex-0000dead0000\nsandbox-ai-lend-codex-0000a11e0000\nsandbox-ai-lend-../x\n" \
                 "my-own-volume\nsandbox-ai-lend-codex-0000feed0000\n"
        return subprocess.CompletedProcess(command, 0, listed if command[1:3] == ["volume", "ls"] else "", "")
    monkeypatch.setattr(v, "podman_run", podman_run)
    with contextlib.ExitStack() as cleanup:
        v.prepare_lending(start_namespace("codex-0000a11e0000"), ["podman"], cleanup)
        calls.clear()
        assert v.lend_leftovers(lambda run: False, ["podman"], remove=False) == [
            "codex-0000dead0000", "codex-0000feed0000"]
        assert [call[1:3] for call in calls] == [["volume", "ls"]], "a dry run removes nothing"
        calls.clear()
        assert v.lend_leftovers(lambda run: False, ["podman"]) == ["codex-0000dead0000", "codex-0000feed0000"]
    removed = [call for call in calls if call[1:3] == ["volume", "rm"]]
    assert removed == [["podman", "volume", "rm", "sandbox-ai-lend-codex-0000dead0000"],
                       ["podman", "volume", "rm", "sandbox-ai-lend-codex-0000feed0000"]]
    assert not any("--force" in call for call in removed)
    # Linux: no volume is looked at.
    calls.clear()
    assert v.lend_leftovers(lambda run: False) == [] and calls == []


def test_gc_asks_the_registered_runs_and_podman_who_still_runs(home, monkeypatch):
    """starts.gc: a run counts as ended only when nothing says otherwise - a run registered (or orphaned: its pod
    still runs), a pod Podman lists with a running container, or Podman not answering."""
    profile("wg", "wireguard")
    st = l.load_starts_module()
    for run in ("codex-00000000dead", "codex-00000000beef", "codex-00000000cafe", "codex-00000000f00d"):
        leftover(run)
    monkeypatch.setattr(st.module("net"), "running", lambda orphans=None: (
        orphans.update({"codex-00000000beef": {"orphaned": True}}) if orphans is not None else None) or {
            "codex-00000000cafe": {"run": "codex-00000000cafe"}})
    liveness = st.module("liveness")
    monkeypatch.setattr(liveness, "pod_live", lambda pod, url=None, fresh=False: pod == "agent-codex-00000000f00d")
    result = {}
    st.leftover_lends(result, dry_run=True)
    assert result == {"lends": ["codex-00000000dead"]}
    monkeypatch.setattr(liveness, "pod_live", lambda pod, url=None, fresh=False: None)  # Podman does not answer
    st.leftover_lends(result, dry_run=False)
    assert result == {"lends": []}, "left alone while it cannot be told"
    assert len([path for path in v.lend_root().iterdir() if path.is_dir()]) == 4
    monkeypatch.setattr(liveness, "pod_live", lambda pod, url=None, fresh=False: False)
    st.leftover_lends(result, dry_run=False)
    assert result == {"lends": ["codex-00000000dead", "codex-00000000f00d"]}
    assert sorted(path.name for path in v.lend_root().iterdir() if path.is_dir()) == [
        "codex-00000000beef", "codex-00000000cafe"]


HOLDER = """import fcntl, os, sys
fd = os.open(os.path.join(sys.argv[1], "lock"), os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.readline()
"""


def test_e2e_sandbox_ai_gc_removes_a_killed_starts_leftover_and_leaves_a_running_starts_alone(host):
    add_wireguard(host)
    root = host.home / ".local/share/sandbox-ai/vpn/.lend"
    dead, live = leftover("codex-0123456789ab", root), leftover("codex-ba9876543210", root)
    # The launcher of the second one still runs: it holds its folder (vpn.hold_lend_folder).
    holder = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(live)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        done = host.run("gc", "--dry-run", "--json")
        assert done.returncode == 0, done.stderr[-2000:]
        assert json.loads(done.stdout)["lends"] == ["codex-0123456789ab"]
        assert dead.exists(), "a dry run removes nothing"
        done = host.run("gc")
        assert done.returncode == 0, done.stderr[-2000:]
        assert "removed what 1 ended run(s) left of their lent VPN tunnel: codex-0123456789ab" in done.stdout
        assert not dead.exists() and (live / "token").exists() and (live / "socket" / "lend.sock").exists()
        assert "nothing to remove" in host.run("gc").stdout
    finally:
        holder.communicate("\n", timeout=30)
    # Its launcher has ended too (killed: nothing cleaned up): now it is a leftover.
    done = host.run("gc", "--json")
    assert json.loads(done.stdout)["lends"] == ["codex-ba9876543210"] and not live.exists()
    assert root.is_dir(), "the folder of the lent tunnels itself stays"


def test_a_start_and_gc_never_meet_in_one_folder(home):
    """prepare_lending makes the run's folder and takes its lock while it holds the lock of the folder above; gc
    judges and removes under the same lock: a folder gc found left over is never one a start makes anew just then."""
    profile("wg", "wireguard")
    source = inspect.getsource(v.prepare_lending)
    assert source.index("lend_root_lock(make=True)") < source.index("lend_folder(run)") < \
        source.index("hold_lend_folder(directory)")
    with v.lend_root_lock(make=True) as there:
        assert there
        fd = os.open(v.lend_root() / ".lock", os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    with contextlib.ExitStack() as cleanup:
        _lend, token = v.prepare_lending(start_namespace("codex-1"), ["podman"], cleanup)
        assert v.lend_folder_held(v.lend_dir("codex-1")) and not v.lend_folder_held(v.lend_dir("codex-2"))
        with pytest.raises(ValueError, match="in use by another start"):
            v.hold_lend_folder(v.lend_dir("codex-1"))
        # A second start of the same run's name never makes the folder anew under the first one: its lender's
        # socket and its token stay.
        with contextlib.ExitStack() as second, pytest.raises(ValueError, match="runs already and lends its tunnel"):
            v.prepare_lending(start_namespace("codex-1"), ["podman"], second)
        assert v.lend_token("codex-1") == token and v.lend_socket_dir("codex-1").is_dir()
    assert not v.lend_folder_held(v.lend_dir("codex-1")) and not v.lend_dir("codex-1").exists()
