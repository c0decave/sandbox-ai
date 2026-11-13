"""Your images in real sandboxes: a service (--service) beside the agent, and a variant on a base image of yours.

The service: redis from Docker Hub, added with `sandbox-ai images pull` (pinned by its digest, recorded unchecked)
and started with --service. The agent must reach it at localhost:6379 (PING -> PONG); the service's container must run
as the numeric user given, with no capabilities and no new privileges, in the sandbox's pod; and what it sends must
meet the sandbox's firewall and connection proxy: a destination that answers whenever it is reachable
(tests/devtools/fake_internet.py on this host's own address) stays unreached from inside the service's container
(podman exec), and its port appears in the proxy's connection log. After the run the service's container is gone.

The base: python:3.12-slim-bookworm as the base of a variant of claude (sandbox-ai images new pybase --from claude --base
...; sandbox-ai build pybase): the variant's sandbox has that image's Python and the agent, and its firewall holds.

Needs Podman, the claude image (sandbox-ai build claude) and the network for Docker Hub; the image and the references
can be given: SANDBOX_AI_LIVE_IMAGE, SANDBOX_AI_SERVICE_REF (default docker.io/library/redis:7-alpine),
SANDBOX_AI_BASE_REF (default docker.io/library/python:3.12-slim-bookworm; SANDBOX_AI_SKIP_BASE_BUILD=1 leaves the
variant out). Run: python -m pytest -v --agent-live tests/test_services_live.py (CI: the job "services")."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
PODMAN = shutil.which("podman")
IMAGE = os.environ.get("SANDBOX_AI_LIVE_IMAGE", "localhost/claude-sandbox:latest")
SERVICE_REF = os.environ.get("SANDBOX_AI_SERVICE_REF", "docker.io/library/redis:7-alpine")
BASE_REF = os.environ.get("SANDBOX_AI_BASE_REF", "docker.io/library/python:3.12-slim-bookworm")


def image_ready():
    return bool(PODMAN) and subprocess.run([PODMAN, "image", "exists", IMAGE], capture_output=True).returncode == 0


pytestmark = [pytest.mark.agent_live,
              pytest.mark.skipif(not image_ready(), reason=f"needs Podman and the image {IMAGE} (sandbox-ai build claude)")]


def load_fake_internet():
    spec = importlib.util.spec_from_file_location("fake_internet_services", ROOT / "tests" / "devtools" /
                                                  "fake_internet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # no packet is sent: only the route is looked up
        return probe.getsockname()[0]


@pytest.fixture
def own_config(tmp_path):
    """Your images' records in a configuration directory of the test's own (Podman's store stays the real one)."""
    config = tmp_path / "config"
    config.mkdir()
    return {**os.environ, "XDG_CONFIG_HOME": str(config)}


def sandbox_ai(env, *args, timeout=1800):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], env=env, capture_output=True, text=True, timeout=timeout)


# The agent: PING the service at localhost:6379, then wait while the test looks into the service's container.
AGENT = r"""
python3 -I - <<'EOF'
import socket, time
for attempt in range(30):
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=5) as connection:
            connection.sendall(b"PING\r\n")
            print("RESULT ping", connection.recv(64).decode().strip())
            break
    except OSError as exc:
        error = exc
        time.sleep(1)
else:
    print("RESULT ping failed:", error)
EOF
echo "RESULT ps $(cat /proc/[0-9]*/comm 2>/dev/null | tr '\n' ' ')"
sleep 60
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


def wait_for_container(label, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = subprocess.run([PODMAN, "ps", "--filter", f"label={label}", "--format", "{{.Names}}"],
                               capture_output=True, text=True).stdout.split()
        if names:
            return names[0]
        time.sleep(1)
    return None


def test_a_service_is_reached_by_the_agent_and_stays_behind_the_firewall(tmp_path, own_config):
    pulled = sandbox_ai(own_config, "images", "pull", SERVICE_REF, "--as", "redis", "--json")
    assert pulled.returncode == 0, pulled.stderr
    entry = json.loads(pulled.stdout)
    assert "@sha256:" in entry["image"] and entry["reviewed"] is False and "UNCHECKED" in pulled.stderr
    fi = load_fake_internet()
    address = host_address()
    assert not address.startswith("127."), "this host has no address of its own to test with"
    server_log = tmp_path / "server.jsonl"
    server = fi.start(bind="0.0.0.0", http=0, log=str(server_log))
    try:
        with urllib.request.urlopen(f"http://{address}:{server['http']}/control", timeout=10) as answer:
            assert answer.status == 200  # it answers whoever reaches it: this test does
        server_log.unlink()
        work, logs = tmp_path / "work", tmp_path / "logs"
        work.mkdir()
        run_label = f"services-live-{os.getpid()}"
        command = [str(ROOT / "sandbox-ai"), "claude", "--image", IMAGE, "--workspace", str(work), "--no-config",
                   "--no-auth", "--no-tmux", "--no-keep", "--ask", "off", "--log-dir", str(logs), "--name", run_label,
                   "--service", "cache:image=redis,port=6379,user=999:1000,data=/data",
                   "--binary", "/bin/sh", "--", "-c", AGENT]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=own_config)
        try:
            container = wait_for_container("sandbox-ai.service=cache")
            assert container, "the service's container never ran"
            inspect = json.loads(subprocess.run([PODMAN, "inspect", container], capture_output=True,
                                                text=True).stdout)[0]
            looks = {}
            for name, argv in (
                    ("status", ["cat", "/proc/self/status"]),
                    ("outside", ["wget", "-q", "-T", "10", "-O", "-", f"http://{address}:{server['http']}/service"]),
                    ("write_root", ["sh", "-c", "touch /probe && echo written || echo refused"]),
                    ("write_data", ["sh", "-c", "touch /data/probe && echo written || echo refused"])):
                done = subprocess.run([PODMAN, "exec", container, *argv], capture_output=True, text=True, timeout=60)
                looks[name] = (done.returncode, done.stdout, done.stderr)
            stdout, stderr = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=60)
            pytest.fail(f"the sandbox timed out\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=60)
    finally:
        server["stop"]()
    report = f"exit {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}\nLOOKS:\n{json.dumps(looks, indent=1)}"
    found = results(stdout)
    assert process.returncode == 0, report
    assert found.get("ping") == "+PONG", report
    assert "redis-server" not in found.get("ps", ""), "the agent sees the service's processes\n" + report
    status = looks["status"][1]
    caps = {line.split(":")[0]: line.split()[1] for line in status.splitlines() if line.startswith("Cap")}
    assert caps.get("CapEff") == "0000000000000000" and caps.get("CapPrm") == "0000000000000000", report
    assert "NoNewPrivs:\t1" in status, report
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    assert uid_line.split()[1:3] == ["999", "999"], report
    assert inspect["HostConfig"]["ReadonlyRootfs"] is True, report
    assert looks["write_root"][1].strip() == "refused" and looks["write_data"][1].strip() == "written", report
    assert looks["outside"][0] != 0, "the service reached a destination outside the allow-list\n" + report
    assert not (server_log.exists() and server_log.read_text().strip()), "a request reached the server"
    logged = "\n".join(p.read_text(errors="replace") for p in logs.rglob("connections-*.jsonl"))
    assert f"{address}:{server['http']}" in logged, "the service's connection is not in the proxy's log\n" + logged[-3000:]
    assert any(p.name.startswith("service-cache-") for p in logs.iterdir()), "the service's output was not kept"
    left = subprocess.run([PODMAN, "ps", "--all", "--filter", "label=sandbox-ai.service=cache", "--format",
                           "{{.Names}}"], capture_output=True, text=True).stdout.split()
    assert container not in left, "the service's container outlived the run"


@pytest.mark.skipif(bool(os.environ.get("SANDBOX_AI_SKIP_BASE_BUILD")), reason="SANDBOX_AI_SKIP_BASE_BUILD")
def test_a_variant_on_a_base_image_of_yours(tmp_path, own_config):
    pulled = sandbox_ai(own_config, "images", "pull", BASE_REF, "--as", "pybase-image")
    assert pulled.returncode == 0, pulled.stderr
    made = sandbox_ai(own_config, "images", "new", "pybase", "--from", "claude", "--base", "pybase-image")
    assert made.returncode == 0 and "UNCHECKED" in made.stdout, made.stdout + made.stderr
    built = sandbox_ai(own_config, "build", "pybase", timeout=3600)
    assert built.returncode == 0, built.stdout[-4000:] + built.stderr[-4000:]
    work = tmp_path / "work"
    work.mkdir()
    script = ("echo RESULT python $(python3.12 --version 2>&1); echo RESULT claude $(claude --version 2>&1 | head -1); "
              "if curl -s -m 10 -o /dev/null https://example.org; then echo RESULT outside reached; "
              "else echo RESULT outside refused; fi")
    done = subprocess.run([str(ROOT / "sandbox-ai"), "claude", "--image-variant", "pybase", "--workspace", str(work),
                           "--no-config", "--no-auth", "--no-tmux", "--no-keep", "--ask", "off", "--binary", "/bin/sh",
                           "--", "-c",
                           script], capture_output=True, text=True, timeout=600, env=own_config)
    found = results(done.stdout)
    report = f"exit {done.returncode}\nSTDOUT:\n{done.stdout}\nSTDERR:\n{done.stderr}"
    assert done.returncode == 0, report
    assert found.get("python", "").startswith("Python 3.12"), report
    assert found.get("claude") and "not found" not in found["claude"], report
    assert found.get("outside") == "refused", report
    assert "UNCHECKED" in done.stderr, report
    removed = sandbox_ai(own_config, "images", "remove", "pybase")
    assert removed.returncode == 0, removed.stderr
