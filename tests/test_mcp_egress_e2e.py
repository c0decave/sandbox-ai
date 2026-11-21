"""End to end through ./sandbox-ai (tests/live_fakes.py's user, with a fake Podman that records every call and keeps
what nft was given): `sandbox-ai mcp start` of a catalog server with network, then a connection to it through the
real bridge - its container is created, its namespace gets the egress rules, and only then does it start; the
rules refuse the metadata address, your network and this host, and reach the internet. An allowlist server whose host
answers with a private address is refused before anything of it is made; a host without nft starts none.

What only real Podman shows (podman init making the namespace, nsenter into it, the kernel's nft): a live test."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402
import test_mcp_egress as unit  # noqa: E402  (the oracle: verdict)

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/c0decave/sandbox-ai-mcp-web-fetch@sha256:" + "a" * 64
SERVER_ID = "5" * 64

# podman as the connection's guard meets it: create, init, inspect, unshare nsenter nft, start (an echo server: each
# line back), rm; every call logged (EGRESS_PODMAN_LOG), what nft was given kept (EGRESS_NFT).
PODMAN = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["EGRESS_PODMAN_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
nft = os.environ["EGRESS_NFT"]
if args[:2] == ["image", "exists"]:
    sys.exit(0)
if args[:1] == ["create"]:
    print("SERVER_ID")
elif args[:1] == ["init"] or args[:1] == ["rm"]:
    pass
elif args[:2] == ["container", "inspect"]:
    print("pasta|/run/user/1000/netns/netns-egress|" + os.environ["EGRESS_RESOLV"] + "|false")
elif args[:1] == ["unshare"] and args[-2:] == ["-f", "-"]:
    with open(nft, "w") as out:
        out.write(sys.stdin.read())
elif args[:1] == ["unshare"] and "list" in args:
    print(open(nft).read() if os.path.exists(nft) else "", end="")
elif args[:1] == ["start"]:
    for line in sys.stdin:
        message = json.loads(line)
        if "id" in message:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message.get("method")}}),
                  flush=True)
else:
    print("fake podman: not this: " + " ".join(args), file=sys.stderr)
    sys.exit(125)
'''


@pytest.fixture()
def host(tmp_path):
    found = Host(tmp_path / "h")
    podman = found.bin / "podman"
    podman.write_text(PODMAN.replace("PYTHON", sys.executable, 1).replace("SERVER_ID", SERVER_ID))
    podman.chmod(0o755)
    for tool in ("nft", "nsenter"):  # (found on PATH: the guard runs them inside podman unshare)
        (found.bin / tool).write_text("#!/bin/sh\nexit 0\n")
        (found.bin / tool).chmod(0o755)
    resolv = found.directory / "resolv.conf"
    resolv.write_text("nameserver 169.254.1.1\n")
    found.env.update(EGRESS_PODMAN_LOG=str(found.directory / "egress-podman.log"),
                     EGRESS_NFT=str(found.directory / "nft-loaded"), EGRESS_RESOLV=str(resolv),
                     SANDBOX_AI_MCP_NO_IMAGE_CHECK="1")
    config = found.home / ".config/sandbox-ai"
    config.mkdir(parents=True)
    (config / "mcp.json").write_text(json.dumps({
        "web": {"image": IMAGE, "network": "full", "source": "mcp:web-fetch"},
        # (its one host answers with a loopback address here - as a rebinding name would)
        "ddg": {"image": IMAGE, "network": "allowlist", "hosts": ["localhost.localdomain:443"]},
    }))
    yield found
    for name in ("web", "ddg"):
        found.run("mcp", "stop", name, timeout=60)


def calls(host):
    path = Path(host.env["EGRESS_PODMAN_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def connect(host, name, requests):
    """One connection through the real bridge (runtime.py --internal-mcp-bridge), as an agent makes it."""
    sock = Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "mcp" / name / "sock" / "server.sock"
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-bridge", str(sock)],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
                          timeout=60, env=host.env)
    return done


def test_e2e_a_server_with_network_starts_only_behind_its_egress_rules(host):
    started = host.run("mcp", "start", "web", timeout=120)
    assert started.returncode == 0, started.stderr
    done = connect(host, "web", [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert '"echo": "initialize"' in done.stdout, (done.stdout, done.stderr)
    words = [call[0] if call[0] != "container" else "inspect" for call in calls(host)
             if call[:2] != ["image", "exists"]]
    assert words == ["create", "init", "inspect", "unshare", "unshare", "start", "rm"], calls(host)
    create = calls(host)[[c[0] for c in calls(host)].index("create")]
    networks = [token for token in create if token.startswith("--network")]
    assert len(networks) == 1 and networks[0].startswith("--network=pasta:"), create
    rules = Path(host.env["EGRESS_NFT"]).read_text()
    for address in ("169.254.169.254", "192.168.1.1", "10.0.0.1", "100.64.0.1", "fd00::1", "fe80::1"):
        assert unit.verdict(rules, address, "tcp", 80) == "reject", address
    assert unit.verdict(rules, "169.254.1.1", "udp", 53) == "accept", "pasta's resolver, port 53 only"
    assert unit.verdict(rules, "169.254.1.1", "tcp", 80) == "reject"
    assert unit.verdict(rules, "93.184.216.34", "tcp", 443) == "accept"


def test_e2e_an_allowlist_host_that_answers_with_a_private_address_is_refused(host):
    started = host.run("mcp", "start", "ddg", timeout=120)
    assert started.returncode == 0, started.stderr
    done = connect(host, "ddg", [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert '"echo"' not in done.stdout
    assert not any(call[:1] in (["create"], ["start"]) for call in calls(host)), "nothing of it was made"
    log = "".join(path.read_text(errors="replace") for path in
                  (host.home / ".local/state/sandbox-ai").rglob("*.log"))
    assert "no public address" in log or "has no public address" in done.stderr + log, log[-2000:]


def test_e2e_a_host_without_nft_starts_no_server_with_network(host):
    for tool in ("nft", "nsenter"):
        (host.bin / tool).unlink()
    started = host.run("mcp", "start", "web", timeout=120)
    assert started.returncode != 0 and "nft is not on this host" in started.stderr, started.stderr
    assert calls(host) == []
