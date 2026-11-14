"""The sandbox's own browser (--browser) in real sandboxes: sandbox-ai's browser image built (sandbox-ai build browser),
a start with it beside the agent, and what each control mode gives the agent.

human (the default): the browser's container runs in the sandbox's pod (no capabilities, no new privileges, a read-only
root, no published port), its VNC server answers RFB on the Unix socket RUN.browser-vnc/vnc.sock on this host, and the
agent reaches nothing of it - no DevTools port on 127.0.0.1:9222, no abstract X socket in the shared network namespace.
agent: the agent reaches Chromium's DevTools at 127.0.0.1:9222, and a page it opens there outside the allow-list goes
to the connection proxy (it appears in the proxy's log), not to the internet. A profile kept in a folder of yours
(--browser-profile DIR) is written there, stays yours and is used again by the next start.

Needs Podman, the claude image (sandbox-ai build claude) and the network for Debian's packages (the browser image is
built once). Run: python -m pytest -v --agent-live tests/test_browser_live.py (CI: the job "browser")."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PODMAN = shutil.which("podman")
IMAGE = os.environ.get("SANDBOX_AI_LIVE_IMAGE", "localhost/claude-sandbox:latest")
BROWSER_IMAGE = "localhost/sandbox-ai-browser:latest"


def image_ready():
    return bool(PODMAN) and subprocess.run([PODMAN, "image", "exists", IMAGE], capture_output=True).returncode == 0


pytestmark = [pytest.mark.agent_live,
              pytest.mark.skipif(not image_ready(), reason=f"needs Podman and the image {IMAGE} (sandbox-ai build claude)")]


@pytest.fixture(scope="module")
def browser_image():
    if subprocess.run([PODMAN, "image", "exists", BROWSER_IMAGE], capture_output=True).returncode != 0:
        built = subprocess.run([str(ROOT / "sandbox-ai"), "build", "browser"], capture_output=True, text=True,
                               timeout=3600)
        assert built.returncode == 0, built.stdout[-4000:] + built.stderr[-4000:]
    labels = json.loads(subprocess.run([PODMAN, "image", "inspect", BROWSER_IMAGE], capture_output=True,
                                       text=True).stdout)[0]["Config"]["Labels"]
    assert labels["sandbox-ai.role"] == "browser" and "@sha256:" in labels["sandbox-ai.base"], labels
    return BROWSER_IMAGE


AGENT = r"""
python3 -I - <<'EOF'
import json, socket, time, urllib.request
time.sleep(3)
try:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=10) as answer:
        print("RESULT cdp", json.loads(answer.read()).get("Browser", "?").replace(" ", "_"))
    request = urllib.request.Request("http://127.0.0.1:9222/json/new?http://198.51.100.7:8080/outside", method="PUT")
    with urllib.request.urlopen(request, timeout=10) as answer:
        print("RESULT opened", answer.status)
except OSError as exc:
    print("RESULT cdp none", type(exc).__name__)
abstract = [line.split()[-1] for line in open("/proc/net/unix") if line.split()[-1].startswith("@/tmp/.X11-unix/X42")]
print("RESULT abstract", len(abstract))
EOF
sleep 40
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


def wait_for_container(label, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = subprocess.run([PODMAN, "ps", "--filter", f"label={label}", "--format", "{{.Names}}"],
                               capture_output=True, text=True).stdout.split()
        if names:
            return names[0]
        time.sleep(1)
    return None


def run_dir_socket(name, timeout=120):
    """The browser's VNC socket of the run named NAME (sandbox-ai list --json "browser" "vnc")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = subprocess.run([str(ROOT / "sandbox-ai"), "list", "--json"], capture_output=True, text=True)
        try:
            entries = json.loads(listed.stdout or "[]")
        except ValueError:
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            browser = entry.get("browser") or {}
            if entry.get("name") == name and browser.get("vnc") and Path(browser["vnc"]).exists():
                return Path(browser["vnc"])
        time.sleep(1)
    return None


def start(tmp_path, name, control, profile=None, agent=AGENT):
    work, logs = tmp_path / "work", tmp_path / "logs"
    work.mkdir(exist_ok=True)
    command = [str(ROOT / "sandbox-ai"), "claude", "--image", IMAGE, "--workspace", str(work), "--no-config",
               "--no-auth", "--no-tmux", "--no-keep", "--ask", "off", "--log-dir", str(logs), "--name", name,
               "--browser", "--browser-control", control, *(["--browser-profile", str(profile)] if profile else []),
               "--binary", "/bin/sh", "--", "-c", agent]
    return subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True), logs


def finish(process):
    try:
        return process.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=60)
        pytest.fail(f"the sandbox timed out\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=60)


def test_a_browser_for_the_human_only(tmp_path, browser_image):
    name = f"browser-live-human-{os.getpid()}"
    process, _ = start(tmp_path, name, "human")
    greeting, inspect, status = b"", {}, ""
    try:
        container = wait_for_container("sandbox-ai.service=browser")
        assert container, "the browser's container never ran"
        inspect = json.loads(subprocess.run([PODMAN, "inspect", container], capture_output=True, text=True).stdout)[0]
        status = subprocess.run([PODMAN, "exec", container, "cat", "/proc/1/status"], capture_output=True,
                                text=True).stdout
        path = run_dir_socket(name)
        assert path is not None, "no VNC socket listed for the run"
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(30)
            client.connect(str(path))
            greeting = client.recv(12)
    finally:
        stdout, stderr = finish(process)
    report = f"exit {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    found = results(stdout)
    assert greeting.startswith(b"RFB 003."), report
    assert found.get("cdp", "").startswith("none"), "the agent reached DevTools of a browser for the human\n" + report
    assert found.get("abstract") == "0", "an abstract X socket in the pod's network namespace\n" + report
    assert inspect["HostConfig"]["ReadonlyRootfs"] is True and not inspect["HostConfig"].get("PortBindings"), report
    caps = {line.split(":")[0]: line.split()[1] for line in status.splitlines() if line.startswith("Cap")}
    assert caps.get("CapEff") == "0000000000000000" and "NoNewPrivs:\t1" in status, report


def test_a_browser_the_agent_drives_goes_through_the_proxy(tmp_path, browser_image):
    name = f"browser-live-agent-{os.getpid()}"
    process, logs = start(tmp_path, name, "agent")
    stdout, stderr = finish(process)
    report = f"exit {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    found = results(stdout)
    assert process.returncode == 0, report
    assert "Chrom" in found.get("cdp", ""), report
    logged = "\n".join(p.read_text(errors="replace") for p in logs.rglob("connections-*.jsonl"))
    assert "198.51.100.7" in logged, "the browser's page did not pass the connection proxy\n" + logged[-3000:]
    assert any(p.name.startswith("service-browser-") for p in logs.iterdir()), "the browser's output was not kept"


KEPT = r"""
python3 -I - <<'EOF'
import json, time, urllib.request
for _ in range(60):
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as answer:
            print("RESULT cdp", json.loads(answer.read()).get("Browser", "?").replace(" ", "_"), flush=True)
            break
    except OSError:
        time.sleep(1)
else:
    print("RESULT cdp none", flush=True)
EOF
sleep 15
"""


def test_a_profile_folder_of_yours_is_kept_between_starts(tmp_path, browser_image):
    """--browser-profile DIR (the session browser's kept profile, in a folder you name): the browser writes its profile
    there, it stays yours (0700, your files), and a second start uses the same folder - what the first start left
    there is still there."""
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    for turn in (1, 2):
        process, _ = start(tmp_path, f"browser-live-kept-{turn}-{os.getpid()}", "agent", profile, KEPT)
        stdout, stderr = finish(process)
        report = f"exit {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        assert process.returncode == 0 and "Chrom" in results(stdout).get("cdp", ""), report
        written = [path for path in profile.rglob("*") if path.is_file() and not path.is_symlink()]
        assert written, f"the browser wrote nothing into {profile}\n" + report
        assert all(path.stat().st_uid == os.getuid() for path in written), "the profile's files are not yours"
        assert oct(profile.stat().st_mode & 0o777) == "0o700", report
        if turn == 1:
            (profile / "sandbox-ai-live-marker").write_text("from the first start")
    assert (profile / "sandbox-ai-live-marker").read_text() == "from the first start"
