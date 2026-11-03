"""Opt-in escape tests (--agent-live): an adversarial probe runs as the agent
inside each backend and tries to reach the host. The host plants canaries
first and checks afterwards that none was touched.

Run on a machine with the images built:
    pytest tests/test_escape_live.py --agent-live [--agent-backend podman|bwrap]
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import uuid

import pytest

from test_agent_sandbox_live import backend, launch  # noqa: F401  (fixture and helper)

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("escape_probe.py")
pytestmark = pytest.mark.agent_live


def host_ipv4_addresses():
    """The host's own non-loopback IPv4 addresses (LOCAL /32 entries)."""
    addresses, previous = set(), ""
    for line in Path("/proc/net/fib_trie").read_text().splitlines():
        if "/32 host LOCAL" in line:
            address = previous.split()[-1]
            if not address.startswith("127."):
                addresses.add(address)
        previous = line
    return sorted(addresses)


class Listener:
    """TCP listener that records every connection it accepts."""

    def __init__(self, address):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((address, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.address, self.port = address, self.sock.getsockname()[1]
        self.accepted, self.stop = [], threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                connection, peer = self.sock.accept()
            except OSError:
                continue
            self.accepted.append(peer)
            with contextlib.suppress(OSError):
                connection.sendall(b"L")  # an answer: what reaches it exchanges data
            connection.close()

    def close(self):
        self.stop.set()
        self.thread.join()
        self.sock.close()


@pytest.fixture
def canaries(tmp_path, monkeypatch):
    token = "escape-canary-" + uuid.uuid4().hex
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / f"{token}.txt"
    secret.write_text(token)
    before = {path: path.stat().st_mtime_ns for path in outside.iterdir()}
    # A host process and a host environment variable carrying the token.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", token])
    monkeypatch.setenv("SANDBOX_ESCAPE_CANARY", token)
    # An abstract Unix socket lives in the network namespace, not the filesystem.
    abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abstract.bind("\0" + token)
    abstract.listen(1)
    listeners = [Listener("127.0.0.1")] + [Listener(address) for address in host_ipv4_addresses()]
    yield {"token": token, "outside": outside, "secret": secret, "before": before, "listeners": listeners}
    sleeper.kill()
    abstract.close()
    for listener in listeners:
        listener.close()


def run_probe(request, backend, tmp_path, canaries, options=(), allowed=()):
    workspace = tmp_path / "work"
    workspace.mkdir()
    token = canaries["token"]
    (workspace / f"{token}-workspace").write_text(token)
    probe = workspace / "probe.py"
    probe.write_text(PROBE.read_text())
    host_tcp = [[l.address, l.port] for l in canaries["listeners"] if (l.address, l.port) not in allowed]
    (workspace / "probe.json").write_text(json.dumps({
        "canary": token, "workspace_canary": f"/work/{token}-workspace",
        "host_canary_path": str(canaries["secret"]),
        "absent_paths": [str(canaries["outside"]), str(Path.home()), str(Path.home() / ".ssh"),
                         f"/run/user/{os.getuid()}", str(tmp_path)],
        "host_tcp": host_tcp + [["10.0.2.2", 22], ["10.0.2.2", canaries["listeners"][0].port]],
        "allowed_tcp": [["127.0.0.1", port] for _, port in allowed],
        "abstract_socket": token}))
    stdout, stderr = launch(request.config, backend, "codex", workspace,
                            ["--no-auth", "--ask", "off", "--binary", "/usr/bin/python3", *options],
                            ["-I", "/work/probe.py", "/work/probe.json"], timeout=180)
    return json.loads(stdout.strip().splitlines()[-1])


def assert_contained(report, canaries, allowed_ports=()):
    failed = {name: result["detail"] for name, result in report.items() if not result["ok"]}
    assert not failed, f"escape checks failed: {json.dumps(failed, indent=2)}"
    for listener in canaries["listeners"]:
        if listener.port not in allowed_ports:
            assert not listener.accepted, f"host listener {listener.address}:{listener.port} was reached"
    # The host canary is untouched and nothing new appeared next to it.
    outside = canaries["outside"]
    assert {path: path.stat().st_mtime_ns for path in outside.iterdir()} == canaries["before"]
    assert canaries["secret"].read_text() == canaries["token"]


def test_escape_probe_default_network(request, backend, tmp_path, canaries):
    report = run_probe(request, backend, tmp_path, canaries)
    assert len(report) > 40, "probe ran only partially"
    assert_contained(report, canaries)


def test_escape_probe_open_egress_still_hides_host(request, backend, tmp_path, canaries):
    # Even with all outbound traffic allowed, host loopback and namespaces stay closed.
    report = run_probe(request, backend, tmp_path, canaries, ["--egress-all-allow"])
    for name in [n for n in report if n.startswith(("tcp_internet", "tcp6_internet", "udp_dns"))]:
        report.pop(name)  # Open egress allows these by design.
    loopback = canaries["listeners"][0]
    assert report[f"tcp_host:127.0.0.1:{loopback.port}"]["ok"]
    for name in [n for n in report if n.startswith("tcp_host:") and "127.0.0.1" not in n]:
        report.pop(name)  # LAN addresses of the host are reachable with open egress (documented).
    failed = {name: result["detail"] for name, result in report.items() if not result["ok"]}
    assert not failed, f"escape checks failed: {json.dumps(failed, indent=2)}"
    assert not loopback.accepted


def test_escape_probe_host_port_opens_exactly_one_port(request, tmp_path, canaries):
    if request.config.getoption("--agent-backend") == "bwrap":
        pytest.skip("--allow-host-port is Podman-only")
    allowed = canaries["listeners"][0]
    other = Listener("127.0.0.1")
    canaries["listeners"].append(other)
    report = run_probe(request, "podman", tmp_path, canaries, ["--allow-host-port", str(allowed.port)],
                       allowed=[(allowed.address, allowed.port)])
    assert report[f"allowed_tcp:127.0.0.1:{allowed.port}"]["ok"], report[f"allowed_tcp:127.0.0.1:{allowed.port}"]
    assert allowed.accepted, "forwarded host port was not reached"
    assert report[f"tcp_host:127.0.0.1:{other.port}"]["ok"]
    assert_contained(report, canaries, allowed_ports=[allowed.port])
