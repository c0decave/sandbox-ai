"""MCP servers and VPNs: when a VPN is meant to be an MCP server's way out - its own ("vpn" of an image server,
--vpn in a command server's options), the start's --vpn, or `mcp start --vpn` - the server reaches the network only
through it, on every way a server comes up, or it does not run.

What is held here (no Podman needed, always run):
  - the matrix: every way a server comes up (a start with --mcp-limits too: its own instance NAME.HASH, with its own
    VPN container and pod, its limits in the commands) x every kind of server x every VPN kind (WireGuard, OpenVPN, Tor,
    Tailscale with and without an exit node) x where its VPN comes from (none, its own, the start's, both, two that
    disagree, mcp start --vpn, mcp start --vpn under a start's VPN): the exact network options of every command
    built, against an oracle written here, not by the code under test;
  - the structure: mcp.server_network is the only code in mcp.py that names a network or a --vpn for a server,
    every command is built from its plan, and every call that starts a server (anywhere in sandbox/) hands it a VPN
    from vpn_for_servers or server_vpn - a new way that forgets the VPN fails here;
  - the runtime proofs with a fake Podman: vpn.mcp_guard (each connection's container is created in the VPN
    container's namespace by its ID, inspected, and started only then; anything else removes it unstarted),
    vpn.check_vpn_container and vpn.check_pod_vpn (the kill switch, the namespace of a command server's pod),
    mcp.verify_network (a server without its VPN is stopped; a running one is checked before it is used again),
    the wrapper's end with the VPN container, the runtime's kill-switch check and the MCP lookups' redirect.
The kernel and Podman parts: tests/test_mcp_vpn_live.py (WireGuard in network namespaces) and
tests/test_mcp_vpn_podman_live.py (the guard and the pod with real containers, a Tor profile end to end)."""
import ast
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"



KILLSWITCH_LISTING = ("table inet sandbox_ai_vpn {\n chain output { type filter hook output priority -5; policy drop;\n }\n"
                      " chain input { type filter hook input priority -5; policy drop;\n }\n"
                      " chain forward { type filter hook forward priority -5; policy drop;\n }\n}\n")

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SANDBOX / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("sandbox_mcp_vpn_test", "mcp.py")
l = m.launcher()
LOAD_VPN = l.load_vpn_module
v = LOAD_VPN()
rt = v.runtime()
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/x/server@{DIGEST}"
VPN_ID = "1" * 64
OTHER_ID = "2" * 64
INFRA_ID = "3" * 64
SERVER_ID = "4" * 64

# Two profiles of each kind: the second is the one that disagrees.
PROFILES = {
    "wg": {"type": "wireguard"}, "wg2": {"type": "wireguard"},
    "ovpn": {"type": "openvpn"}, "ovpn2": {"type": "openvpn"},
    "tor": {"type": "tor"}, "tor2": {"type": "tor"},
    "tsx": {"type": "tailscale", "tailscale": {"exit_node": "exit.example"}},
    "tsx2": {"type": "tailscale", "tailscale": {"exit_node": "exit2.example"}},
    "tsn": {"type": "tailscale", "tailscale": {"exit_node": None}},
    "tsn2": {"type": "tailscale", "tailscale": {"exit_node": None}},
}
KINDS = {"wireguard": "wg", "openvpn": "ovpn", "tor": "tor", "tailscale-exit": "tsx", "tailscale-tailnet": "tsn"}


def profile(name):
    if name not in PROFILES:
        raise ValueError(f"no VPN profile {name}")
    return {"name": name, **PROFILES[name]}


def other_kind(name):
    """The profile that disagrees with NAME: of another kind where that matters (a Tor start and a server's VPN that
    is none), else the second of its kind."""
    return {"tor": "wg", "wg": "tor"}.get(name, name + "2")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


class World:
    """The host as the matrix sees it: the definitions, the VPN profiles, and every command started (nothing runs)."""

    def __init__(self, monkeypatch, tmp_path, servers):
        self.started, self.proven, self.stopped = [], [], []
        self.vpn = LOAD_VPN()
        self.vpn.load_profile = profile
        self.vpn.check_vpn_container = lambda podman, ref, name=None, run=None: \
            self.proven.append(("container", ref, name)) or {"id": VPN_ID, "started": "t"}
        self.vpn.check_pod_vpn = lambda podman, pod, name=None, run=None: self.proven.append(("pod", pod, name)) or {}
        monkeypatch.setattr(l, "load_vpn_module", lambda: self.vpn)
        monkeypatch.setattr(m, "definitions", lambda project=None: servers)
        monkeypatch.setattr(l, "mcp_gateway", lambda a, name, directory: None)
        monkeypatch.setattr(l, "remove_stale_pod", lambda a, pod, owner: False)
        monkeypatch.setattr(m, "check_image_policy", lambda name, definition: None)
        monkeypatch.setattr(m, "check_image_mounts", lambda name, definition: None)
        monkeypatch.setattr(m, "check_tor_image_user", lambda name, image: None)
        monkeypatch.setattr(m, "wait_for_socket", lambda *args, **kwargs: None)
        monkeypatch.setattr(l, "on_machine", lambda a: False)  # (Linux here; macOS: its own test below)
        rootfs = tmp_path / "rootfs"
        rootfs.mkdir(exist_ok=True)
        monkeypatch.setattr(m, "rootfs_module", lambda: types.SimpleNamespace(
            bwrap_command=lambda checked, definition, launcher: ["bwrap", "--unshare-all", "--", "server"]))

        def start_logged(cmd, directory, **options):
            self.started.append(cmd)
            return types.SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        monkeypatch.setattr(m, "start_logged", start_logged)


ROOTFS = {"path": str(ROOT / "tests"), "sha256": "e" * 64}  # (a folder that is there)
# Kind of server -> (its definition without a VPN, its definition with its own VPN P or None where it cannot have
# one, the backend of the start).
SERVERS = {
    "image-full": (lambda: {"image": IMAGE, "network": "full"},
                   lambda p: {"image": IMAGE, "network": "full", "vpn": p}, "podman"),
    "image-none": (lambda: {"image": IMAGE, "network": "none"}, None, "podman"),
    "image-and-rootfs": (lambda: {"image": IMAGE, "rootfs": ROOTFS, "network": "full"}, None, "bwrap"),
    "rootfs-only": (lambda: {"rootfs": ROOTFS, "network": "full"}, None, "bwrap"),
    "command": (lambda: {"command": ["python3", "s.py"], "options": []},
                lambda p: {"command": ["python3", "s.py"], "options": ["--vpn", p]}, "podman"),
    "command-offline": (lambda: {"command": ["python3", "s.py"], "options": ["--offline"]}, None, "podman"),
    "helper": (lambda: {"helper": "codex", "workspace": "/tmp", "description": "x"}, None, "podman"),
}
REACHES = {"image-full", "image-and-rootfs", "rootfs-only", "command", "helper"}
# Where its VPN comes from: (own, start, explicit) - explicit is mcp start --vpn (with a start: --for START).
SOURCES = {
    "none": lambda p: (None, None, None),
    "own": lambda p: (p, None, None),
    "start": lambda p: (None, p, None),
    "both": lambda p: (p, p, None),
    "conflicting": lambda p: (other_kind(p), p, None),
    "explicit": lambda p: (None, None, p),
    "explicit-under-start": lambda p: (None, p, other_kind(p)),
}
# The ways a server comes up, and how each one decides the VPN it hands to ensure_running (agent_sandbox.main: the
# start's --mcp and --helper, a team's members, the app and the board, which run the same CLI; mcp start [--vpn]
# [--for START]; mcp attach [--live]: server_vpn; a restart: the same start again).
PATHS = ("start", "helper", "attach", "attach-live", "mcp-start", "restart", "limits")


def oracle(kind, own, start, explicit):
    """What must happen, written from the promise, not from the code: ("refused", pattern) or
    {"vpn": the profile its packets leave through or None, "form": "podman" | "bwrap" | "launcher"}."""
    reaches = kind in REACHES
    type_of = lambda name: profile(name)["type"] if name else None  # noqa: E731
    # The start's VPN goes in front of a server that reaches the network and has none of its own.
    behind = start if start and reaches and not own else None
    if start and own and reaches and type_of(start) == "tor" and type_of(own) != "tor":
        return ("refused", "not through Tor")
    if kind == "rootfs-only" and behind:
        return ("refused", "Bubblewrap only")
    if start and explicit and type_of(start) == "tor" and type_of(explicit) != "tor":
        return ("refused", "not a Tor profile")
    chosen = explicit or behind or own
    if kind == "rootfs-only" and chosen:
        return ("refused", "Bubblewrap only")
    if kind == "image-none" and chosen:
        return ("refused", "a VPN needs network full")
    if chosen and reaches and PROFILES[chosen]["type"] == "tailscale" and \
            not PROFILES[chosen]["tailscale"]["exit_node"]:
        return ("refused", "carries only the tailnet")
    if kind in ("image-full", "image-none", "image-and-rootfs", "rootfs-only"):
        return {"vpn": chosen, "form": "podman" if chosen or kind not in ("image-and-rootfs", "rootfs-only")
                else "bwrap"}
    return {"vpn": chosen, "form": "launcher"}


def cases():
    for kind, (_, with_own, backend) in SERVERS.items():
        for source, vpn_kind, path in itertools.product(SOURCES, KINDS, PATHS):
            own, start, explicit = SOURCES[source](KINDS[vpn_kind])
            if own and with_own is None:
                continue  # (such a definition is refused when it is read: mcp.validated)
            if path == "helper" and kind != "helper" or kind == "helper" and path not in ("start", "helper"):
                continue  # a helper comes up only with its lead's start (and takes no limits of its own)
            if explicit and path not in ("mcp-start", "attach"):
                continue  # (--vpn PROFILE is mcp start's and mcp attach's; a start and a live attach take its VPN)
            if path == "mcp-start" and start and not explicit:
                continue  # (mcp start without --for has no start)
            yield pytest.param(kind, source, vpn_kind, path, id=f"{kind}-{source}-{vpn_kind}-{path}")


def start_args(backend, start):
    a = l.parse_args(["codex", "--backend", backend])
    a.vpn, a.vpn_profile, a.project_document = start, (profile(start) if start else None), None
    return a


def bring_up(path, name, a, explicit):
    """The server NAME brought up as PATH brings it up (the same calls, in the same order)."""
    if path in ("start", "helper", "limits"):  # (limits: a start with --mcp-limits, its own instance NAME.HASH)
        behind = m.vpn_for_servers(a, [name])  # agent_sandbox.main (and check_mcp_names before it)
        return m.ensure_running(name, a, vpn=behind.get(name))
    if path in ("attach", "attach-live"):
        if a.vpn:
            m.check_running_vpn(name, m.server_vpn(a, name, explicit))  # mcp.attach / attach_live
        return m.ensure_running(name, a, m.server_vpn(a, name, explicit))
    if path == "mcp-start":
        if a.vpn:  # mcp start NAME --for START --vpn PROFILE
            return m.ensure_running(name, a, m.server_vpn(a, name, explicit))
        return m.ensure_running(name, a, explicit)  # mcp.cli: mcp start NAME --vpn PROFILE
    if path == "restart":
        m.ensure_running(name, a, vpn=m.vpn_for_servers(a, [name]).get(name))
        state = m.load_state(name)
        # Its supervisor ended (the start was restarted): the server comes up again as the start brings it up.
        m.save_state(name, {**state, "pid": 999999})
        return m.ensure_running(name, a, vpn=m.vpn_for_servers(a, [name]).get(name))
    raise AssertionError(path)


def network_options(command):
    """{option: [values]} of the network options in a command (podman's --network=..., the launcher's --vpn and
    --vpn-check before its first --)."""
    found = {}
    for index, token in enumerate(command):
        if token.startswith("--network="):
            found.setdefault("--network", []).append(token.split("=", 1)[1])
        elif token in ("--network", "--net", "--pod"):
            found.setdefault(token, []).append(command[index + 1])
    return found


def launcher_vpn(command):
    """The VPN the launcher of a command server (or helper) takes: the last --vpn before the first "--" (argparse),
    and whether a failed check stops it then."""
    options = command[:command.index("--")]
    vpns = [options[i + 1] for i, token in enumerate(options) if token == "--vpn"]
    checks = [options[i + 1] for i, token in enumerate(options) if token == "--vpn-check"]
    return (vpns[-1] if vpns else None), (checks[-1] if checks else None), vpns


@pytest.mark.parametrize("kind,source,vpn_kind,path", list(cases()))
def test_every_way_up_every_server_every_vpn(kind, source, vpn_kind, path, monkeypatch, tmp_path):
    """The matrix: the command that starts the server carries exactly the network the promise gives it."""
    own, start, explicit = SOURCES[source](KINDS[vpn_kind])
    plain, with_own, backend = SERVERS[kind]
    definition = with_own(own) if own else plain()
    world = World(monkeypatch, tmp_path, {"srv": definition})
    a = start_args(backend, start)
    if kind == "helper":
        a.helper_definitions = {"srv": definition}
    if path == "limits":  # a board or start with limits of its own: an instance NAME.HASH, its own VPN container
        a.mcp_limits = ["srv:memory=2g,cpus=1,pids=256"]
    instance = m.instance_of(a, "srv")
    assert (instance != "srv") == (path == "limits")
    want = oracle(kind, own, start, explicit)
    if isinstance(want, tuple):
        with pytest.raises(ValueError, match=want[1]):
            bring_up(path, "srv", a, explicit)
        assert world.started == [], "nothing is started for a server refused"
        return
    assert bring_up(path, "srv", a, explicit) in ("started", "running")
    assert world.started, "the server was started"
    for cmd in world.started:
        if want["form"] == "launcher":
            vpn, check, vpns = launcher_vpn(cmd)
            assert vpn == want["vpn"], cmd
            assert "--network" not in network_options(cmd[:cmd.index("--")]), "a launcher-run server gets no network"
            if want["vpn"] and want["vpn"] != own:
                assert check == "stop" and vpns[-1] == want["vpn"], "behind a VPN put in front: a failed check stops it"
            options = cmd[:cmd.index("--")]
            assert options[options.index("--pod-name") + 1] == m.pod_name(instance), "the instance's own pod"
            if path == "limits":
                assert options[len(options) - 1 - options[::-1].index("--memory") + 1] == "2g", "its limits"
            continue
        if want["form"] == "bwrap":
            assert "--internal-mcp-vpn" not in cmd and "podman" not in cmd and "bwrap" in cmd
            continue
        bridge = cmd[cmd.index("--internal-mcp-serve"):]
        connection = bridge[bridge.index("--") + 1:]
        # Each connection under the watch (mcp.py --internal-watch DIR INSTANCE TMP MEMORY PIDS --), outside the
        # guard: what ends its processes is recorded, its network is the plan's alone.
        assert connection[2:5] == [str(SANDBOX / "mcp.py"), "--internal-watch", str(m.state_dir(instance))], connection
        assert connection[5] == instance and connection[9] == "--", connection
        connection = connection[10:]
        if want["vpn"] is None:
            assert cmd[2:4] == [str(SANDBOX / "runtime.py"), "--internal-mcp-serve"], "no VPN wrapper"
            assert connection[:2] == ["podman", "run"], "no guard without a VPN"
            assert network_options(connection) == {"--network": [
                "none" if kind == "image-none" else l.POD_NETWORK]}
            continue
        if path == "limits":
            assert "--memory=2g" in connection and "--pids-limit=256" in connection, "its limits, in the plan's line"
        container = f"sandbox-ai-vpn-mcp-{instance}"
        # The supervisor is the VPN wrapper of that profile; each connection goes through the guard, whose run
        # line names exactly the VPN container's namespace and nothing else.
        assert cmd[2:6] == [str(SANDBOX / "vpn.py"), "--internal-mcp-vpn", want["vpn"], container], cmd
        assert connection[2:6] == [str(SANDBOX / "vpn.py"), "--internal-mcp-guard", container, "--"], connection
        run_line = connection[6:]
        assert run_line[:2] == ["podman", "run"] and network_options(run_line) == {"--network": [f"container:{container}"]}
        assert "rootfs" not in json.dumps(run_line), "behind a VPN the Podman form runs"
        v.guarded_options(run_line, container)  # what the guard takes (raises otherwise)
        # ... and as the watch hands it over: with the --cidfile it finds the container by (podman create takes it).
        watched = [*run_line[:2], "--cidfile=/state/watch/1.cid", *run_line[2:]]
        assert v.guarded_options(watched, container)[1][0] == "--cidfile=/state/watch/1.cid"
    state = m.load_state(instance)
    assert state["vpn"] == want["vpn"], "the server's state names the VPN it leaves through"
    # After the start the VPN is proven, not assumed (a server without network has nothing to prove).
    if want["vpn"] and kind in REACHES:
        expected = ("container", f"sandbox-ai-vpn-mcp-{instance}", want["vpn"]) if want["form"] == "podman" else \
            ("pod", m.pod_name(instance), want["vpn"])
        assert expected in world.proven and set(world.proven) == {expected}
    else:
        assert world.proven == []


def test_the_matrix_covers_every_server_kind_and_vpn_kind():
    """(the matrix above is generated: it must keep covering what it promises)"""
    seen = {(kind, source, vpn_kind) for kind, source, vpn_kind, _ in (c.values for c in cases())}
    assert {kind for kind, _, _ in seen} == set(SERVERS)
    assert {vpn_kind for _, _, vpn_kind in seen} == set(KINDS)
    assert {source for _, source, _ in seen} == set(SOURCES)
    assert {c.values[3] for c in cases()} == set(PATHS)
    assert len(list(cases())) > 300


# --- the structure: one place names a server's network ---------------------------------------------------------

def functions_of(path):
    tree = ast.parse(path.read_text())
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return tree, found


def enclosing(tree):
    """{id(node): the name of the function it lies in} for every node of TREE."""
    owner = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[id(child)] = inner
            visit(child, inner)
    visit(tree, None)
    return owner


def strings(node):
    """The literal text of NODE: a constant, or the constant parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant))
    return None


def test_only_server_network_names_a_network_or_a_vpn_for_a_server():
    """A --network option, or a --vpn in a command list, is written in mcp.server_network and nowhere else in
    mcp.py (checked_plan compares with them; the image's passwd check runs with no network); a new command that
    gives a server its network another way fails here."""
    tree, _ = functions_of(SANDBOX / "mcp.py")
    owner = enclosing(tree)
    allowed_network = {"server_network", "checked_plan", "image_passwd"}
    for node in ast.walk(tree):
        text, where = strings(node), owner.get(id(node))
        if text is not None and (text.startswith("--network=") or text.startswith("--net=") or
                                 text.startswith("--pod=") or text in ("--net", "--pod")):
            assert where in allowed_network, (where, text, node.lineno)
        if text == "--network":
            assert where == "cli", (where, node.lineno)  # (mcp add --network none|full: the definition's)
        if isinstance(node, (ast.List, ast.Tuple)) and any(strings(e) == "--vpn" for e in node.elts):
            assert where in ("server_network", "checked_plan"), (where, node.lineno)


def test_every_command_that_starts_a_server_is_built_from_its_plan():
    """The builders of a server's commands take its plan (and check it: checked_plan); ensure_running_locked is the
    one that makes the plan, and the only caller of the builders."""
    tree, functions = functions_of(SANDBOX / "mcp.py")
    owner = enclosing(tree)
    builders = {"image_command", "connection_command", "server_command", "helper_command"}
    for name in builders:
        arguments = [arg.arg for arg in functions[name].args.args]
        assert "plan" in arguments, name
    callers = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in builders:
            callers.setdefault(node.func.id, set()).add(owner.get(id(node)))
    assert callers == {"image_command": {"connection_command"}, "connection_command": {"ensure_running_locked"},
                       "server_command": {"ensure_running_locked"}, "helper_command": {"ensure_running_locked"}}
    # Every command that runs a launcher, or the relay on the host, lies in a builder or in ensure_running_locked.
    for node in ast.walk(tree):
        text = strings(node)
        if text == "--internal-mcp-serve" or isinstance(node, ast.Attribute) and node.attr == "launcher_script":
            assert owner.get(id(node)) in builders | {"ensure_running_locked", "gateway_arguments"}, \
                (owner.get(id(node)), node.lineno)
    source = ast.get_source_segment((SANDBOX / "mcp.py").read_text(), functions["ensure_running_locked"])
    assert source.index("server_network(instance, definition, vpn)") < source.index("status(instance)")
    assert "verified_or_stopped(instance, plan, a)" in source and source.count("verified_or_stopped(") == 3
    # An instance (NAME.HASH, a start's own limits or folders) has its own VPN container and pod: the plan is for it.
    assert "instance, definition = instance_for(name, servers[name], limits, mounts)" in source


# Every call that brings an MCP server up, anywhere in sandbox/, and the VPN it hands over: a new one fails here
# until it is added - with the VPN it must hand over.
ENSURE_RUNNING_CALLS = {
    ("agent_sandbox.py", "launch", "vpn=behind.get(name)"),
    ("mcp.py", "attach", "server_vpn(a, name, vpn)"),
    ("mcp.py", "attach_live", "server_vpn(a, name)"),
    ("mcp.py", "cli", "a.vpn"),  # mcp start NAME [--vpn]: no start, only what was given
}


def test_every_call_that_brings_a_server_up_hands_it_its_vpn():
    found = set()
    for path in sorted(SANDBOX.glob("*.py")) + sorted((ROOT / "projects").glob("*/*/*.py")):
        text = path.read_text()
        if "ensure_running(" not in text:
            continue
        tree = ast.parse(text)
        owner = enclosing(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (isinstance(node.func, ast.Attribute) and node.func.attr ==
                                               "ensure_running" or isinstance(node.func, ast.Name) and
                                               node.func.id == "ensure_running"):
                vpn = [k for k in node.keywords if k.arg == "vpn"]
                given = ast.get_source_segment(text, vpn[0].value) if vpn else \
                    ast.get_source_segment(text, node.args[2]) if len(node.args) > 2 else None
                given = f"vpn={given}" if vpn else given
                if owner.get(id(node)) == "ensure_running":
                    continue
                found.add((path.name, owner.get(id(node)), given))
    assert found == ENSURE_RUNNING_CALLS
    # agent_sandbox: the VPN of each server comes from vpn_for_servers, and the start's servers are checked first.
    text = (SANDBOX / "agent_sandbox.py").read_text()
    assert "behind = mcp.vpn_for_servers(a, names)" in text
    assert "load_mcp_module().vpn_for_servers(a, names)" in text  # check_mcp_names, before anything starts


def test_a_definition_without_its_plan_or_a_plan_that_does_not_hold_builds_nothing():
    definition = {"image": IMAGE, "network": "full", "vpn": "wg"}
    with pytest.raises(ValueError, match="network plan"):
        m.image_command(definition)
    plan = m.server_network("srv", {"image": IMAGE, "network": "full"}, "wg")
    for tampered in ({**plan, "network": f"--network={l.POD_NETWORK}"}, {**plan, "network": "--network=host"},
                     {**plan, "vpn_container": "someone-else", "network": plan["network"]},
                     {**plan, "definition": {**plan["definition"], "vpn": None}},
                     {**plan, "vpn": None}, {**plan, "kind": "command"}):
        with pytest.raises(ValueError):
            m.connection_command(tampered)
    command = m.server_network("cmd", {"command": ["x"], "options": []}, "wg")
    with pytest.raises(ValueError, match="does not hold"):
        m.server_command("cmd", {**command, "launcher": []}, l.parse_args(["codex"]), Path("/x"), Path("/y"), None)
    with pytest.raises(ValueError, match="network plan"):
        m.helper_command("cmd", command, l.parse_args(["codex"]), Path("/x"), Path("/y"), None)


def test_a_profile_that_is_no_way_out_is_refused_for_a_server_with_network(monkeypatch):
    world_vpn = LOAD_VPN()
    world_vpn.load_profile = profile
    monkeypatch.setattr(l, "load_vpn_module", lambda: world_vpn)
    with pytest.raises(ValueError, match="carries only the tailnet"):
        m.server_network("srv", {"image": IMAGE, "network": "full", "vpn": "tsn"})
    m.server_network("srv", {"image": IMAGE, "network": "full", "vpn": "tsx"})
    assert v.egress_problem(profile("tsn"), "srv") and v.egress_problem(profile("tsx")) is None
    assert all(v.egress_problem(profile(name)) is None for name in ("wg", "ovpn", "tor"))
    # A server without network has nothing to leak through a tailnet.
    assert m.server_network("off", {"command": ["x"], "options": ["--offline"]}, "tsn")["vpn"] == "tsn"
    # A non-Tor start and a server with --vpn-bypass-tcp (a Tor profile's only): refused before the start.
    assert "--vpn-bypass-tcp" in m.vpn_conflicts("s", {"command": ["x"], "options": ["--vpn-bypass-tcp",
                                                                                     "192.168.1.2:80"]}, "wg", False)


# --- the runtime proofs with a fake Podman ----------------------------------------------------------------------

class Podman:
    """A fake `podman` for vpn.py's checks: containers by ID and name, their state, network and labels, the kill
    switch's answer in each, `create` that records what it was given and `rm` that removes."""

    def __init__(self):
        self.calls, self.containers, self.pods = [], {}, {}
        self.next_id = iter(f"{n:064x}" for n in range(0xa0, 0xff))
        self.network_of_created = None  # what `inspect` says of a created container (None: what it was given)

    def add(self, cid, name, network="pasta", running=True, profile_name="wg", killswitch=True, started="t0"):
        self.containers[cid] = {"name": name, "network": network, "running": running, "profile": profile_name,
                                "killswitch": killswitch, "started": started}

    def find(self, ref):
        for cid, info in self.containers.items():
            if ref in (cid, info["name"]):
                return cid, info
        return None, None

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        args = command[1:]
        ok = lambda out="": types.SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        fail = lambda err="no such container": types.SimpleNamespace(returncode=125, stdout="", stderr=err)  # noqa
        if args[:2] == ["container", "inspect"]:
            template, refs = args[3], args[4:]
            lines = []
            for ref in refs:
                cid, info = self.find(ref)
                if cid is None:
                    return fail()
                values = {"{{.Id}}": cid, "{{.State.Running}}": "true" if info["running"] else "false",
                          "{{.State.StartedAt}}": info["started"], "{{.HostConfig.NetworkMode}}": info["network"],
                          '{{index .Config.Labels "sandbox-ai.vpn-profile"}}': info["profile"] or ""}
                lines.append("|".join(values[part] for part in template.split("|")))
            return ok("\n".join(lines) + "\n")
        if args[:2] == ["pod", "inspect"]:
            infra = self.pods.get(args[-1])
            return ok(infra + "\n") if infra else fail("no such pod")
        if args[0] == "exec":
            cid, info = self.find(args[2] if args[1] == "-i" else args[1])
            if cid is None or not info["running"]:
                return fail("container is not running")
            answer = info["killswitch"]  # (`nft list table inet sandbox_ai_vpn` in the container, judged by vpn.py)
            if answer is True:
                return ok(KILLSWITCH_LISTING)
            if answer is None:
                return types.SimpleNamespace(returncode=1, stdout="garbage", stderr="")
            return ok(KILLSWITCH_LISTING.replace("hook output priority -5; policy drop;", "hook output priority -5;"))
        if args[0] == "create":
            network = [token.split("=", 1)[1] for token in args if token.startswith("--network=")][0]
            cid = next(self.next_id)
            self.add(cid, "", network=network if self.network_of_created is None else self.network_of_created,
                     profile_name=None, running=False)
            self.created = (cid, command)
            return ok(cid + "\n")
        if args[0] == "rm":
            self.containers.pop(self.find(args[-1])[0], None)
            return ok()
        raise AssertionError(command)


class Started:
    """The `podman start --attach --interactive` process the guard runs."""

    def __init__(self, command, on_start=None):
        self.args, self.killed, self.returncode = command, False, None
        if on_start:
            on_start()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self):
        self.killed = True

    def send_signal(self, signum):
        pass


def guard_world(on_start=None):
    podman = Podman()
    podman.add(VPN_ID, "sandbox-ai-vpn-mcp-srv")
    processes = []

    def popen(command):
        podman.containers[command[-1]]["running"] = True
        processes.append(Started(command, on_start))
        return processes[-1]
    return podman, processes, popen


def run_line(container="sandbox-ai-vpn-mcp-srv"):
    plan = m.server_network("srv", {"image": IMAGE, "network": "full"}, None)
    plan = {**plan, "vpn": "wg", "vpn_container": container, "network": f"--network=container:{container}",
            "definition": {**plan["definition"], "vpn": "wg"}}
    return m.image_command(plan)


def test_the_guard_creates_the_server_in_the_checked_vpn_containers_namespace_by_its_id():
    podman, processes, popen = guard_world()
    said = []
    assert v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), {v.MCP_VPN_ID: VPN_ID}, podman, popen, said.append) == 0
    cid, created = podman.created
    # Created with the ID, never the name: a container that takes the name later is not joined.
    assert f"--network=container:{VPN_ID}" in created and "--network=container:sandbox-ai-vpn-mcp-srv" not in created
    assert created[1] == "create" and created[-1] == IMAGE
    assert processes[0].args == ["podman", "start", "--attach", "--interactive", cid]
    order = [c[1] if c[1] != "container" else "inspect" for c in podman.calls]
    # The VPN container and its kill switch, then create, inspect, start; checked again once it runs; removed.
    assert order[:4] == ["inspect", "exec", "create", "inspect"] and order[4:7] == ["inspect", "inspect", "exec"]
    assert order[-1] == "rm" and said == []


@pytest.mark.parametrize("network", ["pasta", f"container:{OTHER_ID}", "host", "", "slirp4netns", "none",
                                     f"container:{VPN_ID[:12]}", f"container:{VPN_ID}0", f"container:{INFRA_ID}"])
def test_a_server_container_in_another_network_is_removed_unstarted(network):
    podman, processes, popen = guard_world()
    podman.network_of_created = network
    with pytest.raises(ValueError, match="removed unstarted|cannot inspect"):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), {v.MCP_VPN_ID: VPN_ID}, podman, popen, lambda text: None)
    assert processes == [], "never started"
    assert podman.created[0] not in podman.containers, "removed"


@pytest.mark.parametrize("change,match", [
    (lambda p: p.containers[VPN_ID].update(running=False), "does not run"),
    (lambda p: p.containers.pop(VPN_ID), "is not there"),
    (lambda p: p.containers[VPN_ID].update(killswitch="the kill switch (nft table inet sandbox_ai_vpn) is not loaded"),
     "kill switch .* is not in place"),
    (lambda p: p.containers[VPN_ID].update(killswitch=None), "kill switch .* is not in place"),
])
def test_without_a_running_vpn_container_with_its_kill_switch_nothing_is_created(change, match):
    podman, processes, popen = guard_world()
    change(podman)
    with pytest.raises(ValueError, match=match):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), {v.MCP_VPN_ID: VPN_ID}, podman, popen, lambda text: None)
    assert not any(c[1] == "create" for c in podman.calls) and processes == []


@pytest.mark.parametrize("environ", [{}, {v.MCP_VPN_ID: ""}, {v.MCP_VPN_ID: "sandbox-ai-vpn-mcp-srv"},
                                     {v.MCP_VPN_ID: VPN_ID[:12]}, {v.MCP_VPN_ID: "g" * 64}])
def test_the_guard_needs_the_vpn_containers_id_from_its_wrapper(environ):
    podman, processes, popen = guard_world()
    with pytest.raises(ValueError, match="no VPN container was handed"):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), environ, podman, popen, lambda text: None)
    assert podman.calls == [] and processes == []


def test_a_vpn_container_of_that_id_must_be_that_id():
    podman, processes, popen = guard_world()
    podman.containers[OTHER_ID] = podman.containers.pop(VPN_ID)
    with pytest.raises(ValueError):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), {v.MCP_VPN_ID: VPN_ID}, podman, popen, lambda text: None)
    assert processes == []


@pytest.mark.parametrize("extra", [
    ["--network=host"], ["--network=pasta"], ["--net=host"], ["--pod=other"], ["--publish=8080:80"], ["-p", "80"],
    ["--dns=9.9.9.9"], ["--add-host=x:1.2.3.4"], ["--network", "host"], ["--http-proxy=true"], ["-P"],
    ["--network=container:sandbox-ai-vpn-mcp-srv"],  # twice
])
def test_a_run_line_with_any_other_network_option_is_refused(extra):
    podman, processes, popen = guard_world()
    line = run_line()
    line = [*line[:-1], *extra, line[-1]]
    with pytest.raises(ValueError):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", line, {v.MCP_VPN_ID: VPN_ID}, podman, popen, lambda text: None)
    assert podman.calls == [] and processes == []


def test_a_run_line_for_another_vpn_container_or_no_podman_run_is_refused():
    for line in (run_line("someone-else"), ["sh", "-c", "podman run x"], [*run_line()[:-1]],
                 ["podman", "exec", "x", "y"]):
        with pytest.raises(ValueError):
            v.guarded_options(line, "sandbox-ai-vpn-mcp-srv")


@pytest.mark.parametrize("meanwhile", ["restarted", "kill switch gone", "stopped"])
def test_a_vpn_container_that_changes_while_the_server_starts_stops_it(meanwhile):
    podman = Podman()
    podman.add(VPN_ID, "sandbox-ai-vpn-mcp-srv")

    def change():
        if meanwhile == "restarted":
            podman.containers[VPN_ID]["started"] = "t1"  # started again: a namespace without its tunnel
        elif meanwhile == "kill switch gone":
            podman.containers[VPN_ID]["killswitch"] = "the kill switch's output chain does not drop"
        else:
            podman.containers[VPN_ID]["running"] = False
    processes = []

    def popen(command):
        podman.containers[command[-1]]["running"] = True
        processes.append(Started(command, change))
        return processes[-1]
    with pytest.raises(ValueError):
        v.mcp_guard("sandbox-ai-vpn-mcp-srv", run_line(), {v.MCP_VPN_ID: VPN_ID}, podman, popen, lambda text: None)
    assert processes[0].killed and podman.created[0] not in podman.containers


def pod_world():
    podman = Podman()
    pod = "agent-mcp-srv"
    podman.pods[pod] = INFRA_ID
    podman.add(VPN_ID, f"{pod}-vpn", network=f"container:{INFRA_ID}")
    podman.add(SERVER_ID, f"{pod}-agent", network=f"container:{INFRA_ID}", profile_name=None)
    return podman, pod


def test_a_command_servers_pod_is_proven_to_share_the_vpns_namespace():
    podman, pod = pod_world()
    assert v.check_pod_vpn(["podman"], pod, "wg", podman) == {"infra": INFRA_ID, "vpn": VPN_ID, "server": SERVER_ID}


@pytest.mark.parametrize("change,match", [
    (lambda p: p.containers[SERVER_ID].update(network="pasta"), "server's container .* not in the pod's network"),
    (lambda p: p.containers[SERVER_ID].update(network=f"container:{OTHER_ID}"), "not in the pod's network"),
    (lambda p: p.containers[VPN_ID].update(network="pasta"), "VPN container .* not in the pod's network"),
    (lambda p: p.containers[VPN_ID].update(killswitch="no table"), "kill switch"),
    (lambda p: p.containers[VPN_ID].update(running=False), "does not run"),
    (lambda p: p.containers[VPN_ID].update(profile="other"), "not the VPN of the profile wg"),
    (lambda p: p.containers[SERVER_ID].update(running=False), "does not run"),
    (lambda p: p.pods.clear(), "pod .* is not there"),
    (lambda p: p.containers.pop(VPN_ID), "is not there"),
])
def test_a_pod_whose_server_could_leave_another_way_is_refused(change, match):
    podman, pod = pod_world()
    change(podman)
    with pytest.raises(ValueError, match=match):
        v.check_pod_vpn(["podman"], pod, "wg", podman)


def test_a_server_that_does_not_prove_its_vpn_is_stopped(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, {"srv": {"command": ["python3", "s.py"], "options": []},
                                          "img": {"image": IMAGE, "network": "full"}})

    def refuse(*args, **kwargs):
        raise ValueError("the server's container of agent-mcp-srv is not in the pod's network namespace")
    world.vpn.check_pod_vpn = refuse
    world.vpn.check_vpn_container = refuse
    stopped = []
    monkeypatch.setattr(m, "stop_locked", lambda name, timeout=30: stopped.append(name))
    a = start_args("podman", "wg")
    for name in ("srv", "img"):
        with pytest.raises(ValueError, match=f"MCP server {name} was stopped: it does not run behind the VPN wg"):
            m.ensure_running(name, a, vpn="wg")
    assert stopped == ["srv", "img"]


def test_a_running_server_is_used_again_only_while_it_has_its_vpn(monkeypatch, tmp_path):
    """Shared by several starts, or found again by a restart: before it is used it proves its VPN again; one that
    lost it is stopped and started anew (behind the VPN), never used as it is."""
    world = World(monkeypatch, tmp_path, {"srv": {"command": ["python3", "s.py"], "options": []}})
    a = start_args("podman", "wg")
    assert m.ensure_running("srv", a, vpn="wg") == "started"
    sock = l.mcp_socket_dir("srv") / "server.sock"
    sock.touch()
    monkeypatch.setattr(m, "status", lambda name: "running")
    assert m.ensure_running("srv", a, vpn="wg") == "running"
    assert world.proven == [("pod", "agent-mcp-srv", "wg")] * 2
    # Another start without a VPN uses it too (it has one): still proven first.
    assert m.ensure_running("srv", start_args("podman", None)) == "running"
    assert len(world.proven) == 3
    # A start behind another VPN: refused (it would serve that start outside its VPN).
    with pytest.raises(ValueError, match="running without the VPN wg2"):
        m.ensure_running("srv", start_args("podman", "wg2"), vpn="wg2")
    # Its VPN is gone meanwhile: stopped and started again.
    calls = []

    def lost(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("the VPN container agent-mcp-srv-vpn does not run")
        return {}
    world.vpn.check_pod_vpn = lost
    stopped = []

    def stop_locked(name, timeout=30):
        stopped.append(name)
        monkeypatch.setattr(m, "status", lambda name: "stopped")
    monkeypatch.setattr(m, "stop_locked", stop_locked)
    before = len(world.started)
    assert m.ensure_running("srv", a, vpn="wg") == "started"
    assert stopped == ["srv"] and len(world.started) == before + 1 and len(calls) == 2


def test_the_wrapper_stops_the_server_when_the_vpn_container_ends_or_its_tunnel_goes_down(monkeypatch, tmp_path):
    """The VPN container ends (its output closes), or says its tunnel went down: the bridge is stopped - no server
    stays up whose way out is gone (its connections' containers end with it)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    vpn = LOAD_VPN()
    vpn.load_profile = lambda name: {**profile("wg"), "name": name, "wireguard": {}}
    podman = Podman()
    monkeypatch.setattr(vpn, "podman_run", lambda command, **kwargs: podman(command, **kwargs)
                        if command[1:3] != ["container", "inspect"] or "{{json .Config.Labels}}" not in command
                        else types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    for ending in ("ended", "down"):
        podman.add(VPN_ID, "sandbox-ai-vpn-mcp-srv")  # (the wrapper removes it at its end)
        class Tunnel:
            def close(self):
                pass

            def follow(self, on_event, on_end=None):
                if ending == "ended":
                    on_end()
                else:
                    on_event({"event": "down", "message": "wireguard-go exited (1); the kill switch blocks all"})
        monkeypatch.setattr(vpn, "bring_up", lambda *args, **kwargs: (Tunnel(), {"tunnel": {}}, {}))
        said = []
        monkeypatch.setattr(sys, "stderr", types.SimpleNamespace(write=said.append, flush=lambda: None))
        # The bridge: a process that would run for a minute unless it is stopped.
        code = vpn.mcp_wrapper("wg", "sandbox-ai-vpn-mcp-srv", "/owner",
                               [sys.executable, "-c", "import time; time.sleep(60)"])
        assert code != 0, ending
        assert any("MCP server stopped" in line for line in said), said


# --- the VPN container's side: the kill switch's check, the lookups of an MCP server ---------------------------

LISTING = """table inet sandbox_ai_vpn {
\tchain output {
\t\ttype filter hook output priority filter - 5; policy drop;
\t\toifname "lo" accept
\t\toifname "wg0" accept
\t}
\tchain input {
\t\ttype filter hook input priority filter - 5; policy drop;
\t\tiifname "lo" accept
\t}
\tchain forward {
\t\ttype filter hook forward priority filter - 5; policy drop;
\t}
}
"""


def test_the_kill_switch_is_in_place_only_when_its_chains_drop():
    assert rt.killswitch_problem(LISTING) is None
    assert rt.killswitch_problem(LISTING.replace("priority filter - 5", "priority -5")) is None
    assert "not loaded" in rt.killswitch_problem("")
    assert "not loaded" in rt.killswitch_problem(LISTING.replace("sandbox_ai_vpn", "other"))
    for hook in ("output", "input", "forward"):
        broken = LISTING.replace(f"hook {hook} priority filter - 5; policy drop;",
                                 f"hook {hook} priority filter - 5; policy accept;")
        assert hook in rt.killswitch_problem(broken)
    # What the runtime loads is what the check accepts (WireGuard's, OpenVPN's, Tor's, Tailscale's rules).
    for rules in (rt.killswitch_rules("wg0", [("192.0.2.1", "udp", 51820)]), rt.tor_rules(),
                  rt.killswitch_rules("tailscale0", mask=0xff0000, mark=0x80000, any_marked=True)):
        assert rt.killswitch_problem(rules) is None


def test_the_kill_switch_command_answers_as_the_guard_reads_it():
    class Runner:
        def __init__(self, code, out):
            self.code, self.out = code, out

        def run(self, command, data=None, check=True):
            assert command == ["nft", "list", "table", "inet", rt.TABLE]
            return types.SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")
    for code, out, ok in ((0, LISTING, True), (1, "", False), (0, LISTING.replace("policy drop", "policy accept"),
                                                               False)):
        written = []
        assert rt.killswitch_command(types.SimpleNamespace(write=written.append), Runner(code, out)) == (0 if ok else 1)
        assert json.loads(written[0])["ok"] is ok


def test_an_mcp_servers_lookups_go_to_the_vpns_resolver():
    rules = rt.mcp_dns_rules("10.66.0.1")
    assert "meta nfproto ipv4 udp dport 53 dnat ip to 10.66.0.1:53" in rules
    assert "meta nfproto ipv4 tcp dport 53 dnat ip to 10.66.0.1:53" in rules
    assert "meta nfproto ipv6 udp dport 53 reject" in rules and "meta nfproto ipv6 tcp dport 53 reject" in rules
    assert f"meta mark {rt.CHECK_MARK:#x} return" in rules  # the runtime's own checks ask the resolvers they name
    with pytest.raises(ValueError):
        rt.mcp_dns_rules("fd00::1")
    assert rt.mcp_dns_resolver({"dns": ["fd00::53", "10.64.0.1"]}) == "10.64.0.1"
    assert rt.mcp_dns_resolver({"dns": []}) == rt.FALLBACK_DNS[0]


class Recorder:
    def __init__(self):
        self.commands, self.children = [], []

    def run(self, command, data=None, check=True):
        self.commands.append((command, data))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.mark.parametrize("mcp", [True, False])
def test_the_runtime_loads_the_redirect_only_for_an_mcp_servers_container(monkeypatch, mcp):
    runner = Recorder()
    tunnel = {"interface": "wg0", "dns": ["10.66.0.1"], "ipv4": True, "ipv6": False}
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: dict(tunnel))
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner: {})
    monkeypatch.setattr(rt, "resolve_hosts", lambda names, tunnel, tailnet=False: {})
    payload = {"profile": {"type": "wireguard"}, "secrets": {}, **({"mcp": True} if mcp else {})}
    out = []
    stdin = types.SimpleNamespace(readline=lambda: json.dumps(payload))
    assert rt.main(["check"], stdin=stdin, out=types.SimpleNamespace(write=out.append, flush=lambda: None),
                   runner=runner) == 0
    loaded = [data for command, data in runner.commands if command == ["nft", "-f", "-"]]
    ready = json.loads(out[-1])
    assert ready["event"] == "ready"
    if mcp:
        assert loaded == [rt.mcp_dns_rules("10.66.0.1")] and ready["tunnel"]["mcp_dns"] == "10.66.0.1"
    else:
        assert loaded == [] and "mcp_dns" not in ready["tunnel"]


def test_the_runtime_refuses_a_tailnet_only_profile_for_an_mcp_server(monkeypatch):
    runner = Recorder()
    monkeypatch.setattr(rt, "up_tailscale", lambda payload, runner, out=None: {"interface": "tailscale0", "dns": []})
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner: {})
    monkeypatch.setattr(rt, "resolve_hosts", lambda names, tunnel, tailnet=False: {})
    payload = {"profile": {"type": "tailscale", "tailscale": {"exit_node": None}}, "secrets": {}, "mcp": True}
    out = []
    stdin = types.SimpleNamespace(readline=lambda: json.dumps(payload))
    assert rt.main(["check"], stdin=stdin, out=types.SimpleNamespace(write=out.append, flush=lambda: None),
                   runner=runner) == 1
    assert json.loads(out[-1])["event"] == "error" and "only the tailnet" in out[-1]


def test_bring_up_marks_the_payload_of_an_mcp_servers_container(monkeypatch):
    seen = []

    class Tunnel:
        def __init__(self, command, data, say):
            seen.append(data)

        def wait_ready(self, timeout=0):
            raise ValueError("stop here")

        def close(self):
            pass
    vpn = LOAD_VPN()
    monkeypatch.setattr(vpn, "Tunnel", Tunnel)
    monkeypatch.setattr(vpn, "payload", lambda profile, action, names=(), bypass=(): {"profile": profile})
    monkeypatch.setattr(vpn, "baseline_of", lambda settings, podman=None: {})
    for mcp in (True, False):
        with pytest.raises(ValueError):
            vpn.bring_up({"type": "wireguard", "name": "wg", "checks": {}}, ["podman"], (), print, mcp=mcp)
    assert seen[0].get("mcp") is True and "mcp" not in seen[1]


def test_the_mcp_wrapper_and_the_guard_are_entry_points_of_vpn_py():
    source = (SANDBOX / "vpn.py").read_text()
    assert 'sys.argv[1] == "--internal-mcp-guard" and sys.argv[3] == "--"' in source
    assert re.search(r"mcp_guard\(sys\.argv\[2\], sys\.argv\[4:\]\)", source)


def test_a_service_container_beside_the_agent_is_proven_the_same_way():
    """(check_pod_vpn's MEMBER: what a container that joins an agent's pod - a service beside it - must pass)"""
    podman, pod = pod_world()
    podman.add("5" * 64, f"{pod}-svc", network=f"container:{INFRA_ID}", profile_name=None)
    assert v.check_pod_vpn(["podman"], pod, "wg", podman, member=f"{pod}-svc")["server"] == "5" * 64
    podman.containers["5" * 64]["network"] = "pasta"
    with pytest.raises(ValueError, match="not in the pod's network namespace"):
        v.check_pod_vpn(["podman"], pod, "wg", podman, member=f"{pod}-svc")


# --- macOS: podman machine ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("vpn_kind", ["wireguard", "openvpn", "tor", "tailscale-exit"])
@pytest.mark.parametrize("kind", ["command", "helper"])
def test_on_macos_a_server_behind_the_starts_vpn_runs_in_its_pod_and_is_proven_through_the_machine(
        kind, vpn_kind, monkeypatch, tmp_path):
    """On a Mac the servers run in podman machine's VM: a command server or helper behind the start's VPN gets the
    VPN in its own pod there (the launcher's --vpn) and is proven through the machine (podman pod inspect, podman
    exec: the same commands, which the Mac's podman hands to the VM; a remote engine is asked where it runs); an
    image server is refused there before anything starts (its bridge would run on the Mac), so none runs outside the
    VPN either."""
    plain, _, _ = SERVERS[kind]
    servers = {"srv": plain(), "img": {"image": IMAGE, "network": "full"}}
    world = World(monkeypatch, tmp_path, servers)
    monkeypatch.setattr(l, "on_machine", lambda a: True)
    monkeypatch.setattr(m, "machine_ready", lambda name, a, runner=None: {})
    seen = []
    world.vpn.check_pod_vpn = lambda podman, pod, name=None, run=None: seen.append((podman, pod, name)) or {}
    start = KINDS[vpn_kind]
    a = start_args("podman", start)
    if kind == "helper":
        a.helper_definitions = {"srv": servers["srv"]}
    assert bring_up("start", "srv", a, None) == "started"
    vpn, check, _ = launcher_vpn(world.started[-1])
    assert (vpn, check) == (start, "stop")
    assert seen == [(["podman"], "agent-mcp-srv", start)]
    a.podman_url = "ssh://core@machine/run/podman.sock"
    m.save_state("srv", {"pid": 999999})
    seen.clear()
    m.ensure_running("srv", a, vpn=start)
    assert seen[0][0] == ["podman", "--remote", "--url", a.podman_url]
    before = len(world.started)
    with pytest.raises(ValueError, match="on macOS such servers are not supported yet"):
        bring_up("start", "img", a, None)
    assert len(world.started) == before


# --- after some time: a Tor start's servers (the report of 2026-11-12) -------------------------------------------
#
# A Tor start reaches its MCP servers through sockets on this host; whatever serves there serves it. These hold that
# no server comes to serve it from outside Tor later in the run: not one running behind another VPN than its own
# definition's, not one brought back (its VPN lost, its supervisor gone) by another start or `mcp start` without
# Tor - on macOS the socket lies in the same Podman volume, so the running start's next connection would reach it.

TOR_RUN = {"run-1": {"kind": "agent", "name": "tor-second", "vpn_profile": "tor", "vpn_type": "tor",
                     "mcp_servers": ["srv", "img"]}}


def running_runs(monkeypatch, runs):
    monkeypatch.setattr(m, "net_module", lambda: types.SimpleNamespace(running=lambda: runs))


@pytest.mark.parametrize("definition", [{"image": IMAGE, "network": "full", "vpn": "tor"},
                                        {"command": ["python3", "s.py"], "options": ["--vpn", "tor"]}],
                         ids=["image", "command"])
def test_a_server_with_its_own_tor_running_behind_another_vpn_is_not_used_by_a_tor_start(definition, monkeypatch,
                                                                                         tmp_path):
    """web-fetch-tor ("vpn": the Tor profile of its own) started meanwhile with `mcp start web-fetch-tor --vpn wg`:
    a Tor start (or any start: the definition promises Tor) must not use it, its packets leave through WireGuard.
    Before the fix ensure_running checked a running server only against the VPN it was handed, which is None for a
    server with a VPN of its own - and used it."""
    World(monkeypatch, tmp_path, {"srv": definition})
    assert m.ensure_running("srv", start_args("podman", None), "wg") == "started"  # mcp start srv --vpn wg
    assert m.load_state("srv")["vpn"] == "wg"
    (l.mcp_socket_dir("srv") / "server.sock").touch()
    monkeypatch.setattr(m, "status", lambda name: "running")
    a = start_args("podman", "tor")
    behind = m.vpn_for_servers(a, ["srv"])
    assert behind == {"srv": None}, "its own Tor: the start hands no VPN in"
    with pytest.raises(ValueError, match=r"running without the VPN tor \(it runs behind wg\)"):
        m.ensure_running("srv", a, vpn=behind["srv"])
    with pytest.raises(ValueError, match=r"running without the VPN tor \(it runs behind wg\)"):
        m.ensure_running("srv", start_args("podman", None))
    # Behind its own Tor it is used again (proven first, as always).
    m.save_state("srv", {**m.load_state("srv"), "vpn": "tor"})
    assert m.ensure_running("srv", a, vpn=None) == "running"


@pytest.mark.parametrize("name", ["srv", "img"])
def test_a_server_a_running_tor_start_reaches_never_comes_back_without_tor(name, monkeypatch, tmp_path):
    """The Tor start tor-second reaches srv (a command server) and img (an image server). Their VPN is lost or their
    supervisor is gone; another start without a VPN, `mcp start` without --vpn, or a start behind WireGuard would
    bring them back outside Tor - and tor-second's next connection would go there. Refused, nothing started; behind
    Tor (that start's profile, or another Tor profile) they come back."""
    world = World(monkeypatch, tmp_path, {"srv": {"command": ["python3", "s.py"], "options": []},
                                          "img": {"image": IMAGE, "network": "full"}})
    running_runs(monkeypatch, TOR_RUN)
    with pytest.raises(ValueError, match=f"MCP server {name} serves the running start tor-second behind Tor tor, "
                                         "and would come up without a VPN now"):
        m.ensure_running(name, start_args("podman", None))
    with pytest.raises(ValueError, match=r"would come up through wg \(wireguard\) now"):
        m.ensure_running(name, start_args("podman", "wg"), vpn="wg")
    with pytest.raises(ValueError, match="would come up without a VPN"):
        m.ensure_running(name, m.launcher().parse_args(["codex", "--backend", "podman"]), None)  # mcp start NAME
    assert world.started == [], "nothing is started outside the running start's Tor"
    assert m.ensure_running(name, start_args("podman", "tor"), vpn="tor") == "started"
    m.save_state(name, {"pid": 999999})
    assert m.ensure_running(name, start_args("podman", None), "tor2") == "started"  # mcp start NAME --vpn tor2


def test_only_running_vpn_starts_that_reach_the_instance_hold_its_start(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, {"srv": {"command": ["python3", "s.py"], "options": []},
                                          "off": {"command": ["python3", "s.py"], "options": ["--offline"]}})
    runs = {"plain": {"kind": "agent", "name": "plain", "vpn_profile": None, "mcp_servers": ["srv"]},
            # its own instance (--mcp-limits): srv.HASH, not srv
            "limited": {"kind": "agent", "name": "limited", "vpn_profile": "tor", "vpn_type": "tor",
                        "mcp_servers": ["srv"], "mcp_instances": {"srv": "srv.0123456789ab"}},
            "server": {"kind": "mcp", "name": "srv", "vpn_profile": "tor", "vpn_type": "tor", "mcp_servers": ["srv"]},
            "wireguard": {"kind": "agent", "name": "wg-start", "vpn_profile": "wg", "vpn_type": "wireguard",
                          "mcp_servers": ["off"]}}
    running_runs(monkeypatch, runs)
    assert m.vpn_starts_reaching("srv") == []
    assert m.vpn_starts_reaching("srv.0123456789ab") == [("limited", "tor", "tor")]
    assert m.ensure_running("srv", start_args("podman", None)) == "started"
    assert m.ensure_running("off", start_args("podman", None)) == "started", "a server without network leaves nowhere"
    assert len(world.started) == 2
    # Behind a VPN start that is no Tor start: any VPN holds (it never comes back without one).
    runs["wireguard"]["mcp_servers"] = ["srv"]
    m.save_state("srv", {"pid": 999999})
    with pytest.raises(ValueError, match="behind the VPN wg, and would come up without a VPN"):
        m.ensure_running("srv", start_args("podman", None))
    assert m.ensure_running("srv", start_args("podman", "wg2"), vpn="wg2") == "started"


def test_behind_tor_and_only_there_a_server_is_told_tor_and_where_onion_names_map(monkeypatch, tmp_path):
    """A fetcher that refuses private targets refused every .onion name behind Tor (tor maps them into
    TOR_VIRTUAL_NET, 10.192.0.0/10), and the model went to the clearnet. Behind Tor an image server's container is
    told SANDBOX_AI_TOR=1 and that network (so it takes exactly it), in the run line the guard takes; without Tor
    never."""
    World(monkeypatch, tmp_path, {})
    definition = {"image": IMAGE, "network": "full"}
    tor = m.server_network("img", definition, "tor")
    line = m.image_command(tor)
    wanted = ["--env=SANDBOX_AI_TOR=1", f"--env=SANDBOX_AI_TOR_VIRTUAL_NETWORK={rt.TOR_VIRTUAL_NET}"]
    assert all(token in line for token in wanted), line
    assert line.index(wanted[0]) < line.index(IMAGE)
    v.guarded_options(line, tor["vpn_container"])  # the guard takes the run line as it is
    for other in ("wg", "tsx", None):
        line = m.image_command(m.server_network("img", definition, other))
        assert not any("SANDBOX_AI_TOR" in token for token in line), (other, line)
    assert v.tor_environment() == {"SANDBOX_AI_TOR": "1", "SANDBOX_AI_TOR_VIRTUAL_NETWORK": rt.TOR_VIRTUAL_NET}


def test_an_onion_name_resolves_into_the_announced_network_and_connects_through_tor():
    """The property a fetcher behind Tor relies on: tor maps .onion names (AutomapHostsOnResolve) into exactly the
    network the server is told, every lookup of its namespace reaches tor's DNSPort, and a TCP connection to an
    address of that network is handed to tor's TransPort - no rule of the kill switch lets it out or past tor."""
    import ipaddress
    network = v.tor_environment()["SANDBOX_AI_TOR_VIRTUAL_NETWORK"]
    torrc = rt.tor_config({}).splitlines()
    assert "AutomapHostsOnResolve 1" in torrc and "AutomapHostsSuffixes .onion" in torrc
    assert f"VirtualAddrNetworkIPv4 {network}" in torrc
    rules = rt.tor_rules()
    divert = rules[rules.index(" chain divert"):]
    assert f"udp dport 53 dnat ip to {rt.TOR_DNS}:53" in divert
    assert [line.strip() for line in divert.splitlines()][-3] == f"meta l4proto tcp redirect to :{rt.TOR_TRANS_PORT}"
    # Nothing exempts the virtual network from the redirect (only tor's own, loopback and IPv6 return before it).
    assert not re.search(r"\b10\.", divert)
    assert ipaddress.ip_address("10.192.0.7") in ipaddress.ip_network(network)
    assert ipaddress.ip_address("10.0.0.7") not in ipaddress.ip_network(network)
