"""--nested-containers in a real sandbox: Podman inside the agent's sandbox runs a container, unshare -rn works, and the
containers' connections meet the sandbox's own firewall and connection proxy.

A destination that answers whenever it is reachable - tests/devtools/fake_internet.py on this host's own address, one
port per attempt - must stay unreached for the agent itself, a container on the agent's network, a container as user
1 (the proxy's ID outside), a container behind slirp4netns and one behind pasta; each attempt's port must appear in the
proxy's connection log (the connection went to the proxy, not past it) and no request may arrive at its server. This
test reaches each server itself first: they do answer. slirp4netns must work (the dev image has it); pasta only where
the Podman inside supports it as root (4.4 and later: Debian bookworm's 4.3 has no pasta mode), else the test fails.
The agent's loopback is the containers' (they share its network), and none of the reserved IDs (the proxy's,
PulseAudio's, tor's) is mapped.

Needs Podman, /dev/net/tun and the dev image variant (sandbox-ai build claude; sandbox-ai images recipe dev --from
claude; sandbox-ai build dev), or SANDBOX_AI_NESTED_IMAGE naming an image with Podman and busybox-static; skipped
without. Run: python -m pytest -v --agent-live tests/test_nested_containers_live.py (CI: the job "nested")."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
PODMAN = shutil.which("podman")
IMAGE = os.environ.get("SANDBOX_AI_NESTED_IMAGE", "localhost/claude-sandbox-dev:latest")
RESERVED = (1, 2, 950)  # the proxy's, PulseAudio's and tor's IDs
ATTEMPTS = ("agent", "shared", "user1", "slirp4netns", "pasta")


def image_ready():
    if not PODMAN or not os.path.exists("/dev/net/tun"):
        return False
    return subprocess.run([PODMAN, "image", "exists", IMAGE], capture_output=True).returncode == 0


pytestmark = [pytest.mark.agent_live,
              pytest.mark.skipif(not image_ready(), reason=f"needs Podman, /dev/net/tun and the image {IMAGE} "
                                                           "(sandbox-ai images recipe dev; sandbox-ai build dev)")]


def load_fake_internet():
    spec = importlib.util.spec_from_file_location("fake_internet_live", ROOT / "tests" / "devtools" / "fake_internet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host_address():
    """This host's own address on its default route: what the sandbox would reach it by, past the firewall."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # no packet is sent: only the route is looked up
        return probe.getsockname()[0]


# Runs as the agent (sh): every step reports "RESULT name value"; nothing stops the script early. HOST and the PORT_*
# placeholders are filled in by the test.
SCRIPT = r"""
result() { echo "RESULT $1 $2"; }
attempt() {  # NAME, then the command that fetches the page: reached, or refused
  name=$1; shift
  if "$@" >/tmp/$name.out 2>/tmp/$name.err && grep -q '"method": "GET"' /tmp/$name.out; then
    result "$name" reached
  else
    result "$name" refused
  fi
}
python3 -I -c 'import sys; print("RESULT uid_map", sys.argv[1].replace("\n", ";"))' "$(cat /proc/self/uid_map)"
python3 -I -c 'import sys; print("RESULT gid_map", sys.argv[1].replace("\n", ";"))' "$(cat /proc/self/gid_map)"
result podman_version "$(podman version --format '{{.Client.Version}}' 2>/dev/null || echo unknown)"
# What the Podman inside thinks it is (rootful in the agent's namespace), and where it keeps what; an error in full.
result podman_info "$(podman info --format '{{.Host.Security.Rootless}} {{.Store.GraphDriverName}} {{.Store.GraphRoot}} {{.Host.NetworkBackend}}' 2>&1 | tr '\n' ' ' | head -c 600)"
result caps "$(grep -E '^Cap(Eff|Bnd)' /proc/self/status | tr '\n' ' ')"
result sys "$(ls /sys/fs 2>&1 | tr '\n' ' ') $(stat -f -c %T /sys/fs/cgroup 2>&1)"
# Podman's own account of its start, its last 30 lines (they name a missing file, a refused mount).
result podman_debug "$(podman --log-level=debug info 2>&1 | tail -n 30 | tr '\n' '|' | cut -c1-6000)"
unshare -rn true && result unshare ok || result unshare failed
# A small local image: busybox-static alone, imported from a file (nothing is pulled, no pipe to break).
mkdir -p /tmp/rootfs/bin /tmp/rootfs/tmp && cp /bin/busybox /tmp/rootfs/bin/busybox \
  && tar -C /tmp/rootfs -cf /tmp/rootfs.tar . 2>/tmp/import.err \
  && podman import -q /tmp/rootfs.tar localhost/nested-probe:latest >/dev/null 2>>/tmp/import.err \
  && result import ok || result import "failed:$(tail -c 1500 /tmp/import.err | tr '\n' ' ')"
IMG=localhost/nested-probe:latest
podman run --rm $IMG /bin/busybox echo hello >/tmp/run.out 2>/tmp/run.err \
  && result run "$(cat /tmp/run.out)" || result run "failed:$(tail -c 400 /tmp/run.err | tr '\n' ' ')"
# The agent's loopback is the containers' (they share its network namespace).
python3 -I -m http.server 8765 --bind 127.0.0.1 --directory /tmp/rootfs >/dev/null 2>&1 &
sleep 2
podman run --rm $IMG /bin/busybox wget -q -T 10 -O /dev/null http://127.0.0.1:8765/bin/ \
  && result loopback ok || result loopback failed
# A destination that answers when it is reached, one port per attempt: refused for every one of them.
WGET="/bin/busybox wget -q -T 10 -O -"
attempt agent $WGET http://HOST:PORT_agent/agent
attempt shared podman run --rm $IMG $WGET http://HOST:PORT_shared/shared
attempt user1 podman run --rm --user 1:1 $IMG $WGET http://HOST:PORT_user1/user1
for mode in slirp4netns pasta; do
  if podman run --rm --network $mode $IMG /bin/busybox true 2>/tmp/$mode-start.err; then
    case $mode in slirp4netns) port=PORT_slirp4netns ;; pasta) port=PORT_pasta ;; esac
    attempt $mode podman run --rm --network $mode $IMG $WGET http://HOST:$port/$mode
  else
    result $mode "unavailable:$(tail -c 300 /tmp/$mode-start.err | tr '\n' ' ')"
  fi
done
kill %1 2>/dev/null
exit 0
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


def covered_outside(text):
    """IDs of the parent namespace a uid_map/gid_map (lines joined by ;) covers."""
    found = set()
    for line in text.split(";"):
        if line.split():
            inside, outside, count = (int(v) for v in line.split())
            found.update(range(outside, outside + count))
    return found


def pasta_supported(version):
    """Whether the Podman inside has a pasta network mode as root (4.4 and later)."""
    match = re.match(r"(\d+)\.(\d+)", version or "")
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (4, 4)


def test_podman_inside_the_sandbox_stays_behind_its_firewall_and_proxy(tmp_path):
    fi = load_fake_internet()
    address = host_address()
    assert not address.startswith("127."), "this host has no address of its own to test with"
    servers, logs_of = {}, {}
    for name in ATTEMPTS:
        logs_of[name] = tmp_path / f"server-{name}.jsonl"
        servers[name] = fi.start(bind="0.0.0.0", http=0, log=str(logs_of[name]))
    try:
        for name, started in servers.items():  # they answer whoever reaches them: this test does
            with urllib.request.urlopen(f"http://{address}:{started['http']}/control", timeout=10) as answer:
                assert answer.status == 200 and json.loads(answer.read())["path"] == "/control"
            logs_of[name].unlink()
        script = SCRIPT.replace("HOST", address)
        for name, started in servers.items():
            script = script.replace(f"PORT_{name}", str(started["http"]))
        work, logs = tmp_path / "work", tmp_path / "logs"
        work.mkdir()
        command = [str(ROOT / "sandbox-ai"), "claude", "--image", IMAGE, "--workspace", str(work), "--no-config",
                   "--no-auth", "--no-tmux", "--ask", "off", "--log-dir", str(logs), "--nested-containers",
                   "--binary", "/bin/sh", "--", "-c", script]
        environment = {**os.environ, "SANDBOX_AI_EXPERIMENTAL": "nested-containers"}  # experimental until it passes
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=environment)
        try:
            stdout, stderr = process.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=60)
            pytest.fail(f"the nested sandbox timed out\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
    finally:
        for started in servers.values():
            started["stop"]()
    found = results(stdout)
    # Everything, in full, with every failure: CI shows only the failure's lines (they become an annotation).
    (tmp_path / "stdout.txt").write_text(stdout)
    (tmp_path / "stderr.txt").write_text(stderr)
    report = (f"exit {process.returncode}\nRESULTS:\n{json.dumps(found, indent=1)}\nSTDOUT:\n{stdout}\n"
              f"STDERR:\n{stderr}")
    if process.returncode != 0:
        pytest.fail(f"the nested sandbox ended with {process.returncode}\n{report}")
    # The agent's own user namespace: root is the sandbox's root, the reserved IDs are mapped nowhere.
    for key in ("uid_map", "gid_map"):
        outside = covered_outside(found.get(key, ""))
        if not (0 in outside and not outside & set(RESERVED)):
            pytest.fail(f"{key}: root not mapped or a reserved ID mapped\n{report}")
    for name, wanted in (("unshare", "ok"), ("import", "ok"), ("run", "hello"), ("loopback", "ok")):
        if found.get(name) != wanted:
            pytest.fail(f"{name}: {found.get(name)!r}, not {wanted!r}\n{report}")
    required = ["agent", "shared", "user1", "slirp4netns"]
    if pasta_supported(found.get("podman_version")):
        required.append("pasta")
    elif not found.get("pasta", "").startswith("unavailable"):
        required.append("pasta")  # it ran after all: then it must be refused like the others
    for name in required:
        if found.get(name) != "refused":  # "unavailable" fails here too
            pytest.fail(f"{name}: {found.get(name)!r}, not refused\n{report}")
    logged = "\n".join(p.read_text(errors="replace") for p in logs.rglob("connections-*.jsonl"))
    for name in required:
        port = servers[name]["http"]
        if f"{address}:{port}" not in logged:
            pytest.fail(f"{name}: port {port} is not in the proxy's log\n{logged[-3000:]}\n{report}")
        if logs_of[name].exists() and logs_of[name].read_text().strip():
            pytest.fail(f"{name}: a request reached the server past the firewall: {logs_of[name].read_text()}")
