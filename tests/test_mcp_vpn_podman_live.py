"""MCP servers behind a VPN with real containers (rootless Podman), as sandbox-ai starts them:

  - the guard (vpn.py --internal-mcp-guard, what the bridge runs for each connection of an image server behind a
    VPN): with a VPN container whose kill switch is not loaded, or that does not run, or under another ID, nothing
    is created; with its kill switch in place the server's container runs in exactly that container's network
    namespace (the same namespace inode) and nothing leaves - while the same server started without a VPN reaches
    the internet (the control);
  - a command server's pod (vpn.check_pod_vpn): Podman shows its VPN container and its server in the infra
    container's namespace (container:INFRA-ID), and a server's container outside the pod, or a VPN container
    without its kill switch, is refused;
  - with SANDBOX_AI_LIVE_NETWORK=1, a Tor profile end to end (vpn.py --internal-mcp-vpn, the wrapper an image
    server with "vpn" runs): the server's lookups are answered by Tor's DNSPort (an .onion name maps into Tor's
    virtual network, whatever resolver it asks), the Tor Project sees a Tor exit, IPv6 and the host's own address
    are not reached; and when the VPN container is killed, the server is stopped.

Needs Podman and the Tor image (`sandbox-ai build tor`, or SANDBOX_AI_BUILD_TOR=1 builds it here; its python3,
nft and sleep serve as the VPN container's stand-in and as the server's image); skipped without, and CI's
killswitch job fails when it is skipped. The tunnel itself (WireGuard in the kernel): tests/test_mcp_vpn_live.py."""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
PROBE_IMAGE = "localhost/sandbox-ai-mcp-vpn-probe:live"  # the server: the probe on the Tor image
PLAIN_IMAGE = "localhost/sandbox-ai-mcp-vpn-plain:live"  # the Tor image without its entry point (infra, stand-ins)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SANDBOX / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("sandbox_mcp_vpn_podman_live", "mcp.py")
v = m.launcher().load_vpn_module()
rt = v.runtime()
PODMAN = shutil.which("podman")


def podman(*args, check=True, **kwargs):
    done = subprocess.run([PODMAN, *args], capture_output=True, text=True, timeout=300, **kwargs)
    if check and done.returncode != 0:
        raise AssertionError(f"podman {' '.join(args)} failed: {done.stderr[-1500:]}")
    return done.stdout.strip()


def ready():
    """None when Podman, the Tor image and the probe image are there (built here as needed), else why not."""
    if not PODMAN:
        return "needs Podman"
    if subprocess.run([PODMAN, "image", "exists", v.TOR_IMAGE], capture_output=True).returncode != 0:
        if os.environ.get("SANDBOX_AI_BUILD_TOR") != "1":
            return "needs the Tor image (sandbox-ai build tor, or SANDBOX_AI_BUILD_TOR=1)"
        built = subprocess.run([str(ROOT / "sandbox-ai"), "build", "tor"], capture_output=True, text=True,
                               timeout=1800)
        if built.returncode != 0:
            return f"sandbox-ai build tor failed: {(built.stdout + built.stderr)[-1500:]}"
    for tag, lines in ((PROBE_IMAGE, ["COPY probe.py /probe.py", 'ENTRYPOINT ["/usr/bin/python3", "-I", "/probe.py"]']),
                       (PLAIN_IMAGE, ["ENTRYPOINT []"])):
        context = Path(tempfile.mkdtemp(prefix="mcp-vpn-probe-image-"))
        shutil.copy(ROOT / "tests" / "mcp_vpn_server_probe.py", context / "probe.py")
        (context / "Containerfile").write_text("\n".join([f"FROM {v.TOR_IMAGE}", *lines, ""]))
        built = subprocess.run([PODMAN, "build", "--pull=never", "-t", tag, str(context)], capture_output=True,
                               text=True, timeout=600)
        shutil.rmtree(context, ignore_errors=True)
        if built.returncode != 0:
            return f"the image {tag} could not be built: {built.stderr[-1500:]}"
    return None


MISSING = ready()
pytestmark = pytest.mark.skipif(MISSING is not None, reason=MISSING or "")


def connection_command(server, work, vpn=True):
    """What the bridge runs for one connection of the image server SERVER (mcp.connection_command), with the probe
    image in place of the catalog's (whose reference must be pinned by digest)."""
    definition = {"image": "ghcr.io/sandbox-ai/probe@sha256:" + "0" * 64, "network": "full",
                  "mounts": [{"target": "/work", "host": str(work), "mode": "ro"}]}
    plan = m.server_network(server, definition, "live" if vpn else None)
    command = m.connection_command(plan)
    assert command[-1] == definition["image"]
    return [*command[:-1], PROBE_IMAGE], plan


def probe_containers():
    return podman("ps", "-a", "--filter", f"ancestor={PROBE_IMAGE}", "--format", "{{.ID}}").split()


def stand_in(name, network=None, pod=None, killswitch=True):
    """A VPN container's stand-in: the image's python3 and nft with the runtime mounted as the VPN container has it,
    NET_ADMIN only, the label of its profile; its kill switch (the runtime's rules, here for a tunnel that is not
    there: nothing leaves but loopback) loaded when KILLSWITCH."""
    podman("run", "-d", "--rm", "--name", name, "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--user=0:0",
           "--security-opt=no-new-privileges", "--label", "sandbox-ai.vpn-profile=live",
           *(["--pod", pod] if pod else [f"--network={network or m.launcher().POD_NETWORK}"]),
           "--mount", f"type=bind,src={SANDBOX / 'vpn_runtime.py'},dst=/vpn/vpn_runtime.py,ro",
           "--entrypoint=/usr/bin/sleep", PLAIN_IMAGE, "infinity")
    if killswitch:
        load_killswitch(name)
    return podman("container", "inspect", "--format", "{{.Id}}", name)


def load_killswitch(name):
    done = subprocess.run([PODMAN, "exec", "-i", name, "nft", "-f", "-"], input=rt.killswitch_rules("wg0"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def netns_of(name):
    return podman("exec", name, "readlink", "/proc/self/ns/net")


@pytest.fixture
def work(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    work.chmod(0o755)
    return work


@pytest.fixture
def names():
    made = []
    yield made
    for name in made:
        podman("rm", "--force", "--time", "0", name, check=False)
        podman("pod", "rm", "--force", "--time", "0", name, check=False)
    for cid in probe_containers():
        podman("rm", "--force", "--time", "0", cid, check=False)


def guard(command, vpn_id, timeout=180):
    return subprocess.run(command, env={**os.environ, v.MCP_VPN_ID: vpn_id}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def test_the_guard_runs_the_server_in_the_vpn_containers_namespace_and_nothing_leaves(work, names):
    server = f"live{uuid.uuid4().hex[:6]}"
    container = v.mcp_container(server)
    names.append(container)
    (work / "targets.json").write_text(json.dumps({"mode": "probe", "tcp": {
        "internet": ["1.1.1.1", 443], "internet other": ["9.9.9.9", 53],
        "ipv6": ["2606:4700:4700::1111", 443]}, "dns": {"public resolver": ["example.com", "1.1.1.1"]}}))
    command, plan = connection_command(server, work)
    assert plan["vpn_container"] == container and command[3] == "--internal-mcp-guard"

    # Its kill switch not loaded yet (a VPN container whose tunnel is not up): nothing is created.
    vpn_id = stand_in(container, killswitch=False)
    done = guard(command, vpn_id)
    assert done.returncode != 0 and "kill switch" in done.stderr, done.stderr
    assert probe_containers() == []

    # Its kill switch in place: the server runs in exactly its namespace, and nothing leaves.
    load_killswitch(container)
    done = guard(command, vpn_id)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["netns"] == netns_of(container), "the server's container shares the VPN container's namespace"
    for name, outcome in report["attempts"].items():
        assert outcome in ("refused", "timeout") or str(outcome).startswith("failed"), (name, outcome)
    assert probe_containers() == [], "the connection's container is gone with the connection"

    # Another ID (a container that took the name later, say): refused.
    done = guard(command, "f" * 64)
    assert done.returncode != 0 and probe_containers() == []

    # The VPN container stopped: refused, nothing created.
    podman("rm", "--force", "--time", "0", container)
    done = guard(command, vpn_id)
    assert done.returncode != 0 and "not there" in done.stderr, done.stderr
    assert probe_containers() == []


@pytest.mark.skipif(os.environ.get("SANDBOX_AI_LIVE_NETWORK") != "1", reason="SANDBOX_AI_LIVE_NETWORK=1: the internet")
def test_the_same_server_without_a_vpn_reaches_the_internet(work, names):
    """(the control: the probe sees a way out where there is one)"""
    (work / "targets.json").write_text(json.dumps({"mode": "probe", "tcp": {"internet": ["1.1.1.1", 443]},
                                                   "dns": {"public resolver": ["example.com", "1.1.1.1"]}}))
    command, plan = connection_command("plain", work, vpn=False)
    assert plan["vpn"] is None and command[:2] == ["podman", "run"]
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["attempts"]["internet"] == "connected", report
    assert isinstance(report["attempts"]["public resolver"], list) and report["attempts"]["public resolver"], report


def test_a_command_servers_pod_shares_the_vpn_containers_namespace(names):
    """The pod as the launcher makes it for --vpn (agent_sandbox.pod_commands, vpn.agent_up): Podman shows the VPN
    container and the server's container in the infra container's namespace, which is what check_pod_vpn proves."""
    pod = f"agent-mcp-live{uuid.uuid4().hex[:6]}"
    names.extend([f"{pod}-agent", f"{pod}-vpn", pod])
    podman("pod", "create", "--name", pod, "--infra-image", PLAIN_IMAGE, "--infra-command", "/usr/bin/sleep infinity",
           "--userns=host", "--share=net,ipc,uts", "--network", m.launcher().POD_NETWORK)
    stand_in(v.container_name(pod), pod=pod)
    podman("run", "-d", "--rm", "--pod", pod, "--name", f"{pod}-agent", "--entrypoint=/usr/bin/sleep", PLAIN_IMAGE,
           "infinity")
    found = v.check_pod_vpn([PODMAN], pod, "live")
    assert found["infra"] == podman("pod", "inspect", "--format", "{{.InfraContainerID}}", pod)
    assert netns_of(f"{pod}-agent") == netns_of(v.container_name(pod))
    with pytest.raises(ValueError, match="not the VPN of the profile other"):
        v.check_pod_vpn([PODMAN], pod, "other")
    # The kill switch gone: refused.
    podman("exec", v.container_name(pod), "nft", "delete", "table", "inet", rt.TABLE)
    with pytest.raises(ValueError, match="kill switch"):
        v.check_pod_vpn([PODMAN], pod, "live")
    load_killswitch(v.container_name(pod))
    # The server's container outside the pod (its own network): refused.
    podman("rm", "--force", "--time", "0", f"{pod}-agent")
    podman("run", "-d", "--rm", "--name", f"{pod}-agent", f"--network={m.launcher().POD_NETWORK}",
           "--entrypoint=/usr/bin/sleep", PLAIN_IMAGE, "infinity")
    with pytest.raises(ValueError, match="not in the pod's network namespace"):
        v.check_pod_vpn([PODMAN], pod, "live")


# --- a Tor profile end to end -------------------------------------------------------------------------------------

def host_address():
    """This host's own address on its uplink (what an MCP server must not reach past Tor)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]


@pytest.fixture
def tor_profile(tmp_path):
    environment = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    added = subprocess.run([str(ROOT / "sandbox-ai"), "vpn", "add", "live", "--tor"], env=environment,
                           capture_output=True, text=True)
    assert added.returncode == 0, added.stderr
    return environment


def wrapper(environment, container, command):
    """vpn.py --internal-mcp-vpn as mcp.ensure_running starts it for an image server with "vpn", with COMMAND (here
    one connection, through the guard) in place of the bridge."""
    return subprocess.Popen([sys.executable, "-I", str(SANDBOX / "vpn.py"), "--internal-mcp-vpn", "live", container,
                             "/live-test-owner", "--", *command], env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.skipif(os.environ.get("SANDBOX_AI_LIVE_NETWORK") != "1", reason="SANDBOX_AI_LIVE_NETWORK=1: through Tor")
def test_a_server_behind_a_tor_profile_reaches_only_tor(work, names, tor_profile, monkeypatch):
    # (the plan is made here, as mcp.ensure_running makes it: with the profile, so it knows it is a Tor profile)
    monkeypatch.setenv("XDG_CONFIG_HOME", tor_profile["XDG_CONFIG_HOME"])
    server = f"tor{uuid.uuid4().hex[:6]}"
    container = v.mcp_container(server)
    names.append(container)
    address = host_address()
    arrived = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen(8)
    listener.settimeout(1)
    port = listener.getsockname()[1]

    def accept():
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                arrived.append(listener.accept()[1])
            except OSError:
                continue
    threading.Thread(target=accept, daemon=True).start()
    (work / "targets.json").write_text(json.dumps({
        "mode": "tor", "onion": rt.ONION_PROBE, "tor_virtual": rt.TOR_VIRTUAL_NET,
        "tcp": {"this host": [address, port], "ipv6": ["2606:4700:4700::1111", 443]}, "dns": {}}))
    command, _ = connection_command(server, work)
    process = wrapper(tor_profile, container, command)
    out, err = process.communicate(timeout=900)
    assert process.returncode == 0, err[-3000:]
    report = json.loads(out.strip().splitlines()[-1])
    attempts = report["attempts"]
    assert attempts["onion mapped by tor"] is True, attempts  # its lookups reached Tor's DNSPort, not 1.1.1.1
    # A fetcher behind Tor is told so, and where .onion names map: it takes them (the report of 2026-11-12: every
    # .onion was refused as a private address), and an onion service answers it through Tor.
    assert report["env"] == {"SANDBOX_AI_TOR": "1", "SANDBOX_AI_TOR_VIRTUAL_NETWORK": rt.TOR_VIRTUAL_NET}, report
    assert attempts["onion in the announced network"] is True, attempts
    assert isinstance(attempts["onion http"], int), attempts
    assert not (isinstance(attempts["onion AAAA"], list) and attempts["onion AAAA"]), \
        f"tor maps .onion names to IPv6 addresses as well, which a fetcher refuses: {attempts}"
    assert attempts["tor check"].get("IsTor") is True, attempts
    assert attempts["ipv6"] in ("refused", "timeout"), attempts
    time.sleep(2)
    # A connection past Tor would come from this host itself (or its network); through Tor, if at all, from an exit.
    past = [peer for peer in arrived if peer[0] == address or ipaddress.ip_address(peer[0]).is_private]
    assert past == [], f"the host's own address was reached past Tor: {past}"
    assert probe_containers() == []


@pytest.mark.skipif(os.environ.get("SANDBOX_AI_LIVE_NETWORK") != "1", reason="SANDBOX_AI_LIVE_NETWORK=1: through Tor")
def test_the_end_of_the_vpn_container_stops_the_server(work, names, tor_profile):
    server = f"tor{uuid.uuid4().hex[:6]}"
    container = v.mcp_container(server)
    names.append(container)
    (work / "targets.json").write_text(json.dumps({"mode": "wait"}))
    command, _ = connection_command(server, work)
    process = wrapper(tor_profile, container, command)
    try:
        # The server runs once its first line is out (the tunnel is up, the guard has started it).
        line = process.stdout.readline()
        assert line, process.stderr.read()[-3000:]
        assert json.loads(line)["netns"] == netns_of(container)
        podman("kill", container)
        process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
    err = process.stderr.read()
    assert "MCP server stopped" in err, err[-3000:]
    deadline = time.monotonic() + 60
    while probe_containers() and time.monotonic() < deadline:
        time.sleep(1)
    assert probe_containers() == [], "the server's container is gone with its VPN"
