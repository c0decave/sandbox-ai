"""End-to-end tests of 0.1.20 and later against real sandboxes (opt-in: --agent-live).

The connection proxy behind the real nftables redirect, the push check on the network with a real
Git server on the host, SSH refusal, network changes while a sandbox runs, devices, MCP catalog
containers, trusted workspaces and the built-in skill. CI runs them (workflow e2e.yml) on
ubuntu-24.04 with rootless Podman and Bubblewrap."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from test_agent_sandbox_live import EXCHANGES, backend, launch  # noqa: E402,F401 - fixture and helpers

pytestmark = pytest.mark.agent_live
TOKEN_SHAPE = "'ghp_' + 'A1b2C3d4' * 4 + 'E5f6'"  # assembled inside the sandbox, never written here


def logs_of(directory):
    return [json.loads(line) for path in sorted(Path(directory).glob("connections-*.jsonl"))
            for line in path.read_text().splitlines() if line.strip()]


def test_live_proxy_opens_tls_logs_and_tools_trust_it(request, backend, tmp_path):
    logs, work = tmp_path / "logs", tmp_path / "work"
    work.mkdir()
    script = '''
set -e
python3 -I -c "import urllib.request; print(urllib.request.urlopen('https://api.github.com/zen', timeout=30).status)"
curl -sS -o /dev/null -w 'curl %{http_code}\\n' https://api.github.com/
git ls-remote https://github.com/c0decave/sandbox-ai HEAD | cut -c1-7
test -s /run/sandbox-ca/ca.pem && echo CA_PRESENT
echo PROXY_TOOLS_OK
'''
    stdout, _ = launch(request.config, backend, "codex", work, ["--no-auth", "--allow-github", "--log-dir", str(logs),
                                                                   "--binary", "/bin/sh"], ["-c", script], timeout=180)
    assert "PROXY_TOOLS_OK" in stdout and "curl 200" in stdout and "CA_PRESENT" in stdout, stdout
    records = logs_of(logs)
    urls = [r.get("url", "") for r in records if r.get("event") == "http"]
    assert any(u.startswith("https://api.github.com/zen") for u in urls), records
    assert any("/info/refs?service=git-upload-pack" in u for u in urls), urls
    assert all(r.get("event") != "tls-refused" for r in records), records


def git_server(tmp_path):
    """git http-backend on the host's loopback, receive-pack enabled; (port, bare repository, server)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import test_proxy
    root = tmp_path / "server"
    (root / "repo.git").mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(root / "repo.git")], check=True)
    subprocess.run(["git", "-C", str(root / "repo.git"), "config", "http.receivepack", "true"], check=True)
    server = test_proxy.serve(type("H", (test_proxy.GitHandler,), {"root": root}))
    return server.server_address[1], root / "repo.git", server


def test_live_push_is_checked_on_the_network(request, backend, tmp_path):
    if backend != "podman":
        pytest.skip("the host's loopback is reachable only through Podman's --allow-host-port")
    port, bare, server = git_server(tmp_path)
    work = tmp_path / "work"  # the server's repository stays outside the workspace
    work.mkdir()
    script = f'''
set -u
cd /work && git clone -q http://127.0.0.1:{port}/repo.git r 2>/dev/null || git init -q -b main r
cd r && git config user.name Tester && git config user.email tester@example.org
echo clean > clean.txt && git add . && git commit -q -m clean
if git push -q http://127.0.0.1:{port}/repo.git HEAD:refs/heads/clean; then echo CLEAN_PUSHED; else echo CLEAN_FAILED; fi
python3 -c "print('TOKEN = ' + repr({TOKEN_SHAPE}))" > config.py && git add . && git commit -q -m secret
if git -c core.hooksPath=/dev/null push --no-verify http://127.0.0.1:{port}/repo.git HEAD:refs/heads/main 2>err.txt
then echo SECRET_PUSHED; else grep -q "sandbox-ai" err.txt && echo SECRET_REFUSED; fi
'''
    try:
        # --privacy-check always: the host's loopback is no public remote, but the check must run.
        stdout, stderr = launch(request.config, backend, "codex", work,
                           ["--no-auth", "--no-provider-defaults", "--allow-host-port", str(port), "--ask", "off",
                            "--privacy-check", "always", "--binary", "/bin/sh"], ["-c", script], timeout=240)
    finally:
        server.shutdown()
    assert "CLEAN_PUSHED" in stdout and "SECRET_REFUSED" in stdout and "SECRET_PUSHED" not in stdout, stdout + stderr
    heads = subprocess.run(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True).stdout
    assert "refs/heads/clean" in heads and "refs/heads/main" not in heads


@pytest.mark.parametrize("privacy", ["public", "off"])
def test_live_ssh_to_a_git_host_is_refused(request, backend, tmp_path, privacy):
    script = '''
import socket, sys
try:
    with socket.create_connection(("github.com", 22), timeout=15) as connection:
        connection.settimeout(15)
        banner = connection.recv(64)
except OSError as exc:
    banner = b""
print("BANNER=" + repr(banner[:4]))
'''
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--egress-all-allow", "--privacy-check", privacy, "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script], timeout=120)
    if privacy == "off":
        assert "BANNER=b'SSH-'" in stdout, stdout  # the control: without the check SSH passes
    else:
        assert "BANNER=b''" in stdout, stdout


def test_live_network_changes_while_running(request, backend, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "XDG_STATE_HOME": str(tmp_path / "state")}
    (tmp_path / "runtime").mkdir()
    script = EXCHANGES + '''
import pathlib, time
before = exchanges("example.org", 443)
pathlib.Path("/work/ready").write_text("1")
for _ in range(600):
    if pathlib.Path("/work/go").exists():
        break
    time.sleep(0.2)
after = exchanges("example.org", 443, timeout=15)
print("BEFORE=%s AFTER=%s" % (before, after))
'''
    command = [str(ROOT / "codex-sandbox"), "--backend", backend, "--workspace", str(work), "--no-auth",
               "--no-provider-defaults", "--binary", "/usr/bin/python3", "--", "-I", "-c", script]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                               stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while not (work / "ready").exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the sandbox did not start"
            time.sleep(0.2)
        allowed = subprocess.run([str(ROOT / "sandbox-ai"), "net", "allow", "--allow-host", "example.org"],
                                 capture_output=True, text=True, env=env, timeout=60)
        assert allowed.returncode == 0, allowed.stderr
        (work / "go").write_text("1")
        stdout, stderr = process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
    assert "BEFORE=False AFTER=True" in stdout, stdout + stderr


def test_live_device_reaches_the_sandbox(request, backend, tmp_path):
    if not os.path.exists("/dev/kvm") or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("no usable /dev/kvm on this host")
    script = "import os; os.close(os.open('/dev/kvm', os.O_RDWR)); print('KVM_OK')"
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--offline", "--device", "/dev/kvm", "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script])
    assert "KVM_OK" in stdout


def test_live_workspace_trusted_and_builtin_skill(request, backend, tmp_path):
    if backend != "podman":
        pytest.skip("checks the sandbox HOME the Claude image gets")
    script = ("python3 -I -c 'import json; print(json.dumps(json.load(open(\"/root/.claude.json\"))))'; "
              "ls /root/.claude/skills")
    stdout, _ = launch(request.config, backend, "claude", tmp_path, ["--no-auth", "-o", "--binary", "/bin/sh"],
                       ["-c", script])
    state = json.loads(stdout.split("\n", 1)[0])
    assert state["projects"]["/work"]["hasTrustDialogAccepted"] is True
    assert "sandbox-ai" in stdout.split("\n", 1)[1]


def test_live_mcp_server_from_the_catalog(request, backend, tmp_path):
    if backend != "podman" or not shutil.which("podman"):
        pytest.skip("catalog servers are containers")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("hello yara\n")
    added = subprocess.run([str(ROOT / "sandbox-mcp"), "add", "hub:yara", "--mount", f"/work={samples}"],
                           capture_output=True, text=True, env=env, timeout=600)
    assert added.returncode == 0, added.stdout + added.stderr
    started = subprocess.run([str(ROOT / "sandbox-mcp"), "start", "yara"], capture_output=True, text=True, env=env,
                             timeout=300)
    try:
        assert started.returncode == 0, started.stderr
        sock = tmp_path / "state/sandbox-ai/mcp/yara/sock/server.sock"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(120)
            directory = os.open(sock.parent, os.O_RDONLY | os.O_DIRECTORY)  # pytest's paths are long
            try:
                client.connect(f"/proc/self/fd/{directory}/{sock.name}")
            finally:
                os.close(directory)
            stream = client.makefile("rw")
            for message in ({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "e2e", "version": "1"}}},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}):
                stream.write(json.dumps(message) + "\n")
                stream.flush()
            answers = {}
            while 1 not in answers:
                line = stream.readline()
                assert line, "the server closed the connection"
                answer = json.loads(line)
                answers[answer.get("id")] = answer
        assert answers[1]["result"]["tools"], answers
    finally:
        subprocess.run([str(ROOT / "sandbox-mcp"), "stop", "yara"], env=env, timeout=120)


def test_live_resource_limits_reach_the_container(request, backend, tmp_path):
    """--pids and --memory are enforced: forks beyond the limit fail, an allocation beyond it is killed."""
    script = """
import subprocess
started = []
try:
    for _ in range(200):
        started.append(subprocess.Popen(["sleep", "20"]))
except OSError as exc:
    print("FORK_REFUSED", exc.errno)
print("STARTED", len(started), flush=True)
for p in started:
    p.kill()
"""
    process, _ = running_sandbox(request, backend, tmp_path, script, ["--offline", "--pids", "64"])
    stdout, stderr = finished(process, 120)
    if "not enforced" in stderr:
        pytest.skip("this host gives the user no systemd session: " + stderr.strip().splitlines()[-1])
    assert "FORK_REFUSED" in stdout and int(stdout.split("STARTED")[1].split()[0]) < 64, stdout + stderr
    hog = "data = bytearray(400 << 20)\nfor i in range(0, len(data), 4096): data[i] = 1\nprint('SURVIVED')"
    process, _ = running_sandbox(request, backend, tmp_path, hog, ["--offline", "--memory", "128m"])
    stdout, stderr = finished(process, 120)
    assert "SURVIVED" not in stdout and process.returncode != 0, stdout + stderr[-3000:]


def running_sandbox(request, backend, tmp_path, script, options=(), extra_env=None):
    """Start a sandbox in the background; (process, environment) with private state, config and data dirs."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    # XDG_DATA_HOME stays: rootless Podman keeps its images there.
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "SANDBOX_AI_SECRETS": "file", **(extra_env or {})}
    command = [str(ROOT / "codex-sandbox"), "--backend", backend, "--workspace", str(work), "--no-auth",
               *options, "--binary", "/usr/bin/python3", "--", "-I", "-c", script]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            stdin=subprocess.DEVNULL, start_new_session=True), env


def finished(process, timeout):
    try:
        return process.communicate(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def answer_when_asked(env, kind_text, decision, scope="run", timeout=120):
    """Wait for an open question whose description contains KIND_TEXT; answer it with sandbox-ai approvals."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = subprocess.run([str(ROOT / "sandbox-ai"), "approvals", "list"], capture_output=True, text=True,
                                env=env, timeout=30).stdout
        for line in listed.splitlines():
            if kind_text in line:
                ident = line.split()[0]
                args = [decision, ident] + (["--scope", scope] if decision == "allow" else [])
                done = subprocess.run([str(ROOT / "sandbox-ai"), "approvals", *args], capture_output=True, text=True,
                                      env=env, timeout=60)
                assert done.returncode == 0, done.stderr
                return ident
        time.sleep(0.5)
    raise AssertionError(f"no question about {kind_text!r}")


def test_live_approvals_connection_lookup_and_browser(request, backend, tmp_path):
    script = EXCHANGES + '''
import socket, subprocess, time
direct = exchanges("1.1.1.1", 443, timeout=90)            # held until the human answers
try:
    socket.getaddrinfo("example.org", 443)
    before = True
except OSError:
    before = False                                          # refused at once, asked in the background
after = False
for _ in range(120):
    try:
        socket.getaddrinfo("example.org", 443)
        after = exchanges("example.org", 443, timeout=15)
        break
    except OSError:
        time.sleep(1)
opened = subprocess.run(["xdg-open", "https://example.org/login"], capture_output=True, text=True)
print("DIRECT=%s BEFORE=%s AFTER=%s OPEN=%s" % (direct, before, after, opened.returncode), flush=True)
import pathlib
for _ in range(600):  # a browser request is not waited for; the run must last until it is answered
    if pathlib.Path("/work/answered").exists():
        break
    time.sleep(0.2)
'''
    process, env = running_sandbox(request, backend, tmp_path, script)
    try:
        answer_when_asked(env, "connect to 1.1.1.1:443", "allow", "once")
        answer_when_asked(env, "look up example.org", "allow", "run")
        answer_when_asked(env, "open https://example.org/login", "deny")
        (tmp_path / "work" / "answered").write_text("1")
    except AssertionError as exc:
        process.kill()
        stdout, stderr = finished(process, 30)
        raise AssertionError(f"{exc}\n{stdout[-3000:]}\n{stderr[-5000:]}") from None
    stdout, stderr = finished(process, 240)
    assert "DIRECT=True BEFORE=False AFTER=True OPEN=0" in stdout, stdout + stderr[-4000:]


def test_live_secret_env_and_process_log(request, backend, tmp_path):
    name = f"E2E_DEMO_{os.getpid()}"
    data = {"SANDBOX_AI_SECRETS": "file"}
    secrets = [str(ROOT / "sandbox-ai"), "secrets"]
    stored = subprocess.run(secrets + ["set", name], input="demo-secret-value\n",
                            capture_output=True, text=True, env={**os.environ, **data}, timeout=30)
    assert stored.returncode == 0, stored.stderr
    logs = tmp_path / "logs"
    # The process log polls twice a second: the marker process lives long enough to be seen.
    script = ("import os, subprocess; subprocess.run(['sleep', '2.5'], check=True); "
              f"print('KEY=%s' % (os.environ.get('DEMO_KEY') == 'demo-secret-value'), flush=True)")
    try:
        process, _ = running_sandbox(request, backend, tmp_path, script,
                                     ["--offline", "--secret-env", f"DEMO_KEY={name}", "--log-dir", str(logs)], data)
        stdout, stderr = finished(process, 180)
    finally:
        subprocess.run(secrets + ["delete", name], capture_output=True, env={**os.environ, **data}, timeout=30)
    assert process.returncode == 0 and "KEY=True" in stdout, stdout + stderr[-4000:]
    assert "demo-secret-value" not in stderr
    commands = [json.loads(line).get("command", "") for path in logs.rglob("processes.jsonl")
                for line in path.read_text().splitlines() if line.strip()]
    assert any("sleep 2.5" in c for c in commands), commands
