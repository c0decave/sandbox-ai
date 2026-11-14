"""Stability round 2 (0.1.95): what a long or busy run met - each test failed before its fix."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEEP = "[" * 100000 + "]" * 100000  # nested deeper than Python's json reads: RecursionError, no ValueError


def load(name):
    spec = importlib.util.spec_from_file_location(f"stability2_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(tmp_path, *args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "XDG_DATA_HOME": str(tmp_path / "data"),
           "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_NO_UPDATE_CHECK": "1", "LANG": "C.UTF-8"}
    return subprocess.run([sys.executable, str(ROOT / "sandbox" / "main.py"), *args], env=env, capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("name, args", [
    ("defaults.json", ("defaults", "show")), ("git-logins.json", ("git-logins", "list")),
    ("publish-logins.json", ("publish-logins", "list")), ("hosts.json", ("hosts", "list")),
    ("identities.json", ("identities", "list")), ("web/users.json", ("web", "user", "list"))])
def test_a_config_file_nested_too_deep_is_said_to_be_broken(tmp_path, name, args):
    """A configuration file damaged into thousands of brackets (a half-written or garbled write, a script's
    mistake): these commands ended with a RecursionError traceback - every other reader of the file said it was
    broken and named it."""
    for folder in ("home", "config", "state", "runtime", "data"):
        (tmp_path / folder).mkdir(mode=0o700)
    path = tmp_path / "config" / "sandbox-ai" / name
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(DEEP)
    path.chmod(0o600)
    done = cli(tmp_path, *args)
    assert "Traceback" not in done.stderr + done.stdout, done.stderr[-2000:]
    assert done.returncode != 0 and name.split("/")[-1] in done.stderr, done.stderr[-2000:]


class Lines:
    """A stand-in for the VPN container's output (Tunnel.process): LINES, then its end."""

    def __init__(self, lines):
        self.read, self.write = os.pipe()
        data = b"".join(line + b"\n" for line in lines)

        def write():  # (more than a pipe holds: written while it is read)
            with os.fdopen(self.write, "wb") as stream:
                stream.write(data)
        threading.Thread(target=write, daemon=True).start()
        self.stdout = os.fdopen(self.read, "rb", buffering=0)

    def poll(self):
        return None


def test_the_vpn_containers_reports_are_followed_past_a_damaged_line(tmp_path):
    """Tunnel.follow - how a run (and an MCP server behind a VPN) hears that its tunnel went down or that the check
    during the run found a leak: one line nested too deep, or a report the callback cannot read, ended the reading
    thread with a traceback. What came after - "down", the container's end - was never heard: the MCP server behind
    the VPN was not stopped, no message said why the network was gone."""
    vpn = load("vpn")
    tunnel = vpn.Tunnel.__new__(vpn.Tunnel)
    tunnel.buffer = b""
    tunnel.process = Lines([DEEP.encode(), b'{"event": "recheck", "checks": 5}', b'{"event": "down", "message": "x"}'])
    seen, ended = [], threading.Event()

    def on_event(event):
        if event.get("event") == "recheck":
            event["checks"].get("ipv4")  # a report it cannot read (AttributeError)
        seen.append(event.get("event"))
    tunnel.follow(on_event, on_end=ended.set)
    assert ended.wait(10)
    assert "down" in seen


@pytest.mark.parametrize("hangs", ["inspect", "nft", "pod"])
def test_a_hanging_podman_fails_the_vpn_check_closed(hangs):
    """check_vpn_container / check_pod_vpn - proving that an MCP server or a service runs behind its VPN with the
    kill switch: a podman that did not answer within 60 s raised subprocess.TimeoutExpired, which no caller expects
    (they stop the server on ValueError): mcp.verified_or_stopped left the server running unproven and the command
    ended with a traceback. It is a failed check now, like any other."""
    vpn = load("vpn")
    good = "a" * 64

    def run(argv, **kwargs):
        if (hangs == "inspect" and "inspect" in argv and "container" in argv) or (hangs == "nft" and "nft" in argv) \
                or (hangs == "pod" and "pod" in argv):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if "pod" in argv:
            return subprocess.CompletedProcess(argv, 0, good + "\n", "")
        if "nft" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "no table")
        return subprocess.CompletedProcess(argv, 0, f"{good}|true|now|container:{good}|p\n", "")
    with pytest.raises(ValueError, match="did not answer"):
        if hangs == "pod":
            vpn.check_pod_vpn(["podman"], "pod1", "p", run)
        else:
            vpn.check_vpn_container(["podman"], good, "p", run)
