"""The Tor container as sandbox-ai really starts it (vpn.container_command: rootless Podman, every capability dropped
but the few it adds, a read-only image, the data directory a tmpfs of its own): tor must come up in it, become
debian-tor and own its data directory. Unit tests only read the command line - Podman refusing a --tmpfs option, or
root without CAP_FOWNER failing a chmod, show only here.

Needs Podman and the Tor image (`sandbox-ai build tor`, or SANDBOX_AI_BUILD_TOR=1 builds it here); skipped without.
SANDBOX_AI_LIVE_NETWORK=1 also brings a real Tor profile up through the Tor network (`sandbox-ai vpn check`).
Run: python -m pytest -v tests/test_tor_container_live.py"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v = load("vpn_container_live", ROOT / "sandbox" / "vpn.py")
PODMAN = shutil.which("podman")


def image_ready():
    """None when Podman and the Tor image are there (SANDBOX_AI_BUILD_TOR=1 builds it), else why not."""
    if not PODMAN:
        return "needs Podman"
    if subprocess.run([PODMAN, "image", "exists", v.TOR_IMAGE], capture_output=True).returncode == 0:
        return None
    if os.environ.get("SANDBOX_AI_BUILD_TOR") != "1":
        return "needs the Tor image (sandbox-ai build tor, or SANDBOX_AI_BUILD_TOR=1)"
    built = subprocess.run([str(ROOT / "sandbox-ai"), "build", "tor"], capture_output=True, text=True, timeout=1800)
    if built.returncode != 0:
        tail = " | ".join((built.stdout + built.stderr).strip().splitlines()[-12:])
        return f"sandbox-ai build tor failed ({built.returncode}): {tail}"[:1500]
    return None


MISSING = image_ready()
pytestmark = pytest.mark.skipif(MISSING is not None, reason=MISSING or "")

# Runs in the container in place of `vpn_runtime.py serve`: the runtime's own steps up to tor, with the network off.
PROBE = r"""
import importlib.util, json, os, subprocess, sys, time
spec = importlib.util.spec_from_file_location("rt", "/vpn/vpn_runtime.py")
rt = importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)
uid = rt.tor_uid()
rt.own_data_directory(uid)
rt.RUN.mkdir(mode=0o700, exist_ok=True)
config, defaults = rt.RUN / "torrc", rt.RUN / "torrc-defaults"
rt.write_private(config, rt.tor_config({"socks": False}) + "DisableNetwork 1\n")
rt.write_private(defaults, "")
tor = subprocess.Popen(["tor", "-f", str(config), "--defaults-torrc", str(defaults)], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
lines, deadline = [], time.monotonic() + 40
os.set_blocking(tor.stdout.fileno(), False)
while time.monotonic() < deadline and tor.poll() is None:
    chunk = tor.stdout.read()
    if chunk:
        lines += chunk.splitlines()
    if any("DisableNetwork is set" in line or "Bootstrapped 0%" in line for line in lines):
        time.sleep(1)
        break
    time.sleep(0.2)
status = {}
if tor.poll() is None:
    for line in open(f"/proc/{tor.pid}/status"):
        if line.startswith("Uid:"):
            status["uid"] = int(line.split()[1])
data = os.stat("/var/lib/tor")
report = {"running": tor.poll() is None, "uid": status.get("uid"), "tor_uid": uid, "data_owner": data.st_uid,
          "data_mode": oct(data.st_mode & 0o777), "files": sorted(os.listdir("/var/lib/tor")), "log": lines[-30:]}
tor.terminate()
print(json.dumps(report))
"""


def test_tor_comes_up_in_the_container_as_sandbox_ai_starts_it():
    name = f"sandbox-ai-tor-live-{uuid.uuid4().hex[:8]}"
    profile = {"version": 1, "name": "live", "type": "tor", "provider": "tor",
               "tor": {"bridges": 0, "bridges_secret": None, "bridges_secret_owned": False, "exit_country": [],
                       "socks": False}}
    command = v.container_command([PODMAN], name, profile)
    tail = [v.TOR_IMAGE, "-I", "/vpn/vpn_runtime.py", "serve"]
    assert command[-len(tail):] == tail
    command = command[:-3] + ["-I", "-c", PROBE]
    command[command.index("-i")] = "--interactive=false"
    done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, f"the container failed:\n{done.stderr}\n{done.stdout}"
    report = json.loads(done.stdout.strip().splitlines()[-1])
    log = "\n".join(report["log"])
    assert report["running"], f"tor ended:\n{log}"
    assert report["uid"] == report["tor_uid"] == v.runtime().TOR_UID, "tor became debian-tor"
    assert report["data_owner"] == report["tor_uid"] and report["data_mode"] == "0o700"
    assert "[err]" not in log and "Permission denied" not in log, log


@pytest.mark.skipif(os.environ.get("SANDBOX_AI_LIVE_NETWORK") != "1", reason="SANDBOX_AI_LIVE_NETWORK=1: through Tor")
def test_a_tor_profile_comes_up_through_the_tor_network(tmp_path):
    environment = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
                   "XDG_STATE_HOME": str(tmp_path / "state")}
    launcher = [sys.executable, str(ROOT / "sandbox-ai")]
    added = subprocess.run([*launcher, "vpn", "add", "live", "--tor"], env=environment, capture_output=True, text=True)
    assert added.returncode == 0, added.stderr
    checked = subprocess.run([*launcher, "vpn", "check", "live"], env=environment, capture_output=True, text=True,
                             timeout=600)
    assert checked.returncode == 0, f"{checked.stdout}\n{checked.stderr}"
