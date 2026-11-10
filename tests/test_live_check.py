"""Live checks of what the unit tests can only fake (opt-in: --agent-live; scripts/live_check.py runs them with the
other live suites and writes a report): a workspace added and removed while a sandbox runs, a ruleset refusing a
request on the network, a canary read seen, a session's whole life (start, list, pause, resume, stop, rebirth,
delete), and with --live-vpn-profile a VPN start whose names allowed during the run go through the VPN.

Every start here is `sandbox-ai codex --no-auth --no-provider-defaults --binary ...`: no login, no model, only the
sandbox around a small script. Each test keeps its runs apart (XDG_RUNTIME_DIR, XDG_STATE_HOME of its own)."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from test_agent_sandbox_live import EXCHANGES, backend  # noqa: E402,F401 - fixture and helpers

pytestmark = pytest.mark.agent_live
SAI = str(ROOT / "sandbox-ai")


def private_env(tmp_path):
    env = {**os.environ}
    for name, sub in (("XDG_RUNTIME_DIR", "runtime"), ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir(mode=0o700, exist_ok=True)
        env[name] = str(tmp_path / sub)
    return env


def engine_options(request, backend):
    options = ["--backend", backend]
    if backend == "podman" and request.config.getoption("--agent-image"):
        options += ["--image", request.config.getoption("--agent-image")]
    if backend == "podman" and request.config.getoption("--agent-podman-url"):
        options += ["--podman-url", request.config.getoption("--agent-podman-url")]
    return options


def sai(env, *args, timeout=120):
    return subprocess.run([SAI, *args], capture_output=True, text=True, env=env, timeout=timeout,
                          stdin=subprocess.DEVNULL)


class Running:
    """A sandbox running SCRIPT (python3 -I -c) in the background; WORK is its /work."""

    def __init__(self, request, backend, tmp_path, script, options=(), env=None):
        self.work = tmp_path / "work"
        self.work.mkdir(exist_ok=True)
        self.env = env or private_env(tmp_path)
        command = [SAI, "codex", *engine_options(request, backend), "--workspace", str(self.work), "--no-auth",
                   "--no-provider-defaults", "--no-keep", *options, "--binary", "/usr/bin/python3", "--",
                   "-I", "-c", script]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env=self.env, stdin=subprocess.DEVNULL, start_new_session=True)

    def wait_for(self, name, seconds=180):
        deadline = time.monotonic() + seconds
        while not (self.work / name).exists():
            if self.process.poll() is not None:
                pytest.fail("the sandbox ended early:\n" + "\n".join(self.process.communicate()))
            assert time.monotonic() < deadline, f"/work/{name} did not appear within {seconds} s"
            time.sleep(0.2)

    def finish(self, seconds=180):
        try:
            return self.process.communicate(timeout=seconds)
        finally:
            self.close()

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)


WAIT = '''
import pathlib, time
def wait(name, seconds=300):
    for _ in range(int(seconds * 5)):
        if pathlib.Path("/work/" + name).exists():
            return True
        time.sleep(0.2)
    return False
'''


def test_live_workspace_added_and_removed_while_running(request, backend, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "hello.txt").write_text("from the host")
    script = WAIT + '''
pathlib.Path("/work/ready").write_text("1")
wait("added")
seen = pathlib.Path("/workspaces/extra/hello.txt").read_text()
try:
    pathlib.Path("/workspaces/extra/written").write_text("x")
    readonly = False
except OSError:
    readonly = True
pathlib.Path("/work/checked").write_text("1")
wait("removed")
print("SEEN=%s READONLY=%s GONE=%s" % (seen, readonly, not pathlib.Path("/workspaces/extra/hello.txt").exists()))
'''
    run = Running(request, backend, tmp_path, script)
    try:
        run.wait_for("ready")
        added = sai(run.env, "workspace", "add", str(extra), "--ro")
        assert added.returncode == 0, added.stderr
        listed = sai(run.env, "workspace", "list", "--json")
        assert listed.returncode == 0 and "extra" in listed.stdout, listed.stdout + listed.stderr
        (run.work / "added").write_text("1")
        run.wait_for("checked")
        removed = sai(run.env, "workspace", "remove", "extra")
        assert removed.returncode == 0, removed.stderr
        (run.work / "removed").write_text("1")
        stdout, stderr = run.finish()
    finally:
        run.close()
    assert "SEEN=from the host READONLY=True GONE=True" in stdout, stdout + stderr
    assert not (extra / "written").exists()


def test_live_a_ruleset_refuses_a_request_on_the_network(request, backend, tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "http": [
        {"id": "no-blocked-path", "host": "example.org", "path": "/blocked/**", "decision": "deny"}]}))
    rules.chmod(0o600)
    script = '''
import urllib.request, urllib.error
def status(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError as exc:
        return "error " + str(exc)
print("ALLOWED=%s BLOCKED=%s" % (status("https://example.org/"), status("https://example.org/blocked/x")))
'''
    run = Running(request, backend, tmp_path, script,
                  ["--allow-host", "example.org", "--rules", str(rules), "--log-dir", str(tmp_path / "logs")])
    stdout, stderr = run.finish()
    assert "ALLOWED=200" in stdout and "BLOCKED=200" not in stdout, stdout + stderr
    logs = "".join(path.read_text() for path in (tmp_path / "logs").glob("connections-*.jsonl"))
    assert "no-blocked-path" in logs, "the refusal names its rule in the connection log"


def test_live_a_canary_read_is_seen(request, backend, tmp_path):
    script = '''
import os
text = open(os.path.expanduser("~/.aws/credentials")).read()
print("READ=%d" % len(text))
'''
    run = Running(request, backend, tmp_path, script, ["--canaries", "credentials", "--log-dir", str(tmp_path / "logs")])
    stdout, stderr = run.finish()
    assert "READ=" in stdout, stdout + stderr
    records = [json.loads(line) for path in (tmp_path / "logs").rglob("canaries.jsonl")
               for line in path.read_text().splitlines() if line.strip()]
    assert any("read" in json.dumps(record) for record in records), records or stderr[-3000:]


def state_of(env, name):
    listed = sai(env, "list", "--json", "--all")
    assert listed.returncode == 0, listed.stderr
    for entry in json.loads(listed.stdout):
        if entry.get("name") == name:
            return entry.get("state")
    return None


def wait_state(env, name, wanted, seconds=180):
    deadline = time.monotonic() + seconds
    while True:
        state = state_of(env, name)
        if state in wanted:
            return state
        assert time.monotonic() < deadline, f"{name} is {state}, not {wanted}"
        time.sleep(1)


def test_live_a_sessions_whole_life(request, backend, tmp_path):
    if not shutil.which("script"):
        pytest.skip("needs script (util-linux) for a terminal")
    env = private_env(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    name = f"live-{os.getpid()}"
    if backend == "bwrap" and not shutil.which("tmux"):
        pytest.skip("a session under Bubblewrap runs the host's tmux")
    # --tmux: a start with --binary is a foreground run otherwise, which ends with its terminal.
    start = [SAI, "codex", *engine_options(request, backend), "--workspace", str(work), "--no-auth",
             "--no-provider-defaults", "--name", name, "--tmux", "--binary", "/bin/sh", "--", "-c", "sleep 900"]
    # An interactive start is a session: in a terminal (script gives it one); it detaches when that ends.
    terminal = subprocess.Popen(["script", "-qfec", " ".join(map(repr, start)), "/dev/null"], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    try:
        assert wait_state(env, name, {"running"}) == "running"
        os.killpg(terminal.pid, signal.SIGHUP)  # closing the terminal detaches; the session goes on
        time.sleep(3)
        assert state_of(env, name) == "running"
        for command, wanted in ((("pause", name), {"paused"}), (("resume", name), {"running"}),
                                (("stop", name), {"stopped", "ended"})):
            done = sai(env, *command)
            assert done.returncode == 0, f"{command}: {done.stderr}"
            wait_state(env, name, wanted)
        history = sai(env, "list", "--history", name, "--json")
        assert history.returncode == 0, history.stderr
    finally:
        sai(env, "stop", name)
        if terminal.poll() is None:
            os.killpg(terminal.pid, signal.SIGKILL)
        sai(env, "delete", name, "--yes")


@pytest.fixture
def vpn_profile(request):
    profile = request.config.getoption("--live-vpn-profile")
    if not profile:
        pytest.skip("give --live-vpn-profile NAME (sandbox-ai vpn add ...) to test a VPN")
    return profile


def test_live_vpn_start_and_names_allowed_during_the_run(request, backend, vpn_profile, tmp_path):
    if backend != "podman":
        pytest.skip("--vpn needs Podman")
    script = EXCHANGES + WAIT + '''
before = exchanges("example.org", 443)
pathlib.Path("/work/ready").write_text("1")
wait("go")
print("BEFORE=%s AFTER=%s" % (before, exchanges("example.org", 443, timeout=20)))
'''
    run = Running(request, backend, tmp_path, script, ["--vpn", vpn_profile, "--log-dir", str(tmp_path / "logs")])
    try:
        run.wait_for("ready", seconds=400)
        allowed = sai(run.env, "net", "allow", "--allow-host", "example.org")
        assert allowed.returncode == 0, allowed.stderr
        (run.work / "go").write_text("1")
        stdout, stderr = run.finish()
    finally:
        run.close()
    assert "BEFORE=False AFTER=True" in stdout, stdout + stderr
    records = [json.loads(line) for line in (tmp_path / "logs" / "vpn.jsonl").read_text().splitlines()]
    assert records and not records[0].get("failures"), records
