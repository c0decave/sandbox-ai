"""What a real start of a catalog MCP server needs on the user's host (the user, 2026-11-23: "Binwalk und Radare2 sind
in dieser Sitzung registriert. Beide MCP-Server beenden sich aber, bevor sie den Handshake beantworten").

Every connection of an image server is a container of its own (`podman run ... IMAGE`, started by the server's bridge
on this host). Its run line asked for --memory and --pids-limit always (--cpus only where the cpu controller is
delegated), while the agent's own pod, the services and the Bubblewrap servers leave out what this host cannot
enforce (agent_sandbox.enforced_limits, services.enforceable, mcp_rootfs.limits_prefix): without a systemd user
session rootless Podman runs on cgroupfs and sets no limits at all, and a controller systemd does not delegate cannot be
set either. There Podman refuses the container ("crun: the requested cgroup controller `pids` is not available",
"cannot set limits without cgroups"): the agent's session ran, its MCP servers were registered, and each connection
ended before the server could answer initialize. Now the run line asks only for what can be enforced here - the
start says which limits are not - and a connection that ends before its answer says why (Podman's own line, its exit
status, where the log is) instead of only "ended before it answered".
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "c" * 64


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_mcp_live_start_test", "agent_sandbox.py")
m = load("sandbox_mcp_live_start_test", "mcp.py")
rt = load("runtime_mcp_live_start_test", "runtime.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for variable, folder in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(variable, str(tmp_path / folder))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


def limits_of(command):
    return {flag.split("=", 1)[0] for flag in command if flag.startswith(("--memory=", "--pids-limit=", "--cpus="))}


DEFINITION = {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-radare2@{DIGEST}", "network": "none", "memory": "1g",
              "pids": 256, "cpus": 1}


def test_without_a_systemd_user_session_the_container_asks_for_no_limit(monkeypatch):
    """Rootless Podman on cgroupfs sets no limits: a run line with one is refused before the server runs."""
    monkeypatch.setattr(m.launcher(), "user_manager_running", lambda: False)
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: {"cpu", "memory", "pids"})
    monkeypatch.setattr(m.sys, "platform", "linux")
    command = m.image_command(m.server_network("r2", DEFINITION))
    assert limits_of(command) == set(), command
    assert "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,notmpcopyup" in command, "its /tmp stays bounded"
    assert m.unenforced_limits()[0] == ["memory", "cpus", "pids"]
    assert "systemd user session" in m.unenforced_limits()[1]


def test_a_controller_that_is_not_delegated_is_left_out_alone(monkeypatch):
    monkeypatch.setattr(m.launcher(), "user_manager_running", lambda: True)
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: {"cpu", "memory"})
    command = m.image_command(m.server_network("r2", DEFINITION))
    assert limits_of(command) == {"--memory", "--cpus"}, command
    assert "--memory=1g" in command and "--cpus=1" in command
    assert m.unenforced_limits()[0] == ["pids"] and "pids" in m.unenforced_limits()[1]
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: {"cpu", "memory", "pids"})
    assert limits_of(m.image_command(m.server_network("r2", DEFINITION))) == {"--memory", "--cpus", "--pids-limit"}
    assert m.unenforced_limits() == ([], None)
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: None)  # not known: asked for, as before
    assert limits_of(m.image_command(m.server_network("r2", DEFINITION))) == {"--memory", "--cpus", "--pids-limit"}


def test_on_a_mac_the_vm_s_podman_sets_them(monkeypatch):
    """podman machine's VM has its own systemd: nothing of this host's is asked there."""
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setattr(m.launcher(), "user_manager_running", lambda: False)
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: set())
    assert limits_of(m.image_command(m.server_network("r2", DEFINITION))) == {"--memory", "--cpus", "--pids-limit"}


def test_the_start_says_which_limits_are_not_enforced(monkeypatch, capsys):
    config = m.config_file()
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"r2": DEFINITION}))
    monkeypatch.setattr(m.launcher(), "user_manager_running", lambda: False)
    monkeypatch.setattr(m.sys, "platform", "linux")
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    monkeypatch.setattr(m, "verified_or_stopped", lambda *a, **kw: None)
    assert m.ensure_running("r2", s.parse_args(["codex"])) == "started"
    said = " ".join(capsys.readouterr().err.split())
    assert "MCP server r2: its limits --memory, --cpus and --pids are not enforced here" in said, said
    assert "systemd user session" in said
    assert limits_of(started[-1]) == set()


# --- a connection that ends before its answer says why ------------------------------------------------------------

def test_a_server_that_ends_before_its_answer_says_why():
    """The relay's error answer names Podman's last line and the exit status (never only "ended before it
    answered"); what it says is bounded and redacted."""
    secret = "ghp_" + "A" * 36
    script = ("import sys; sys.stderr.write('Trying to pull...\\nError: OCI runtime error: crun: the requested cgroup "
              f"controller `pids` is not available {secret}\\n'); sys.exit(126)")
    process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    import socket
    ours, theirs = socket.socketpair()
    gate = rt.McpGateway(None, open(os.devnull, "w"))
    tail = rt.StderrTail()
    import threading
    thread = threading.Thread(target=rt.relay_server_stderr, args=(process.stderr, open(os.devnull, "w"), tail))
    thread.start()
    session = rt.McpConnection(gate, ours, process, ended=lambda: tail.why(process, thread))
    session.client_line(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n')
    session.pump()
    theirs.settimeout(10)
    received = b""
    while not received.endswith(b"\n"):
        received += theirs.recv(65536)
    answer = json.loads(received.decode().strip().splitlines()[-1])
    message = answer["error"]["message"]
    assert message.startswith("sandbox-ai: the MCP server ended before it answered"), message
    assert "exit status 126" in message and "the requested cgroup controller `pids` is not available" in message
    assert secret not in message and len(message) < 700


def test_a_clean_end_without_a_word_says_only_that():
    process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    tail = rt.StderrTail()
    import threading
    thread = threading.Thread(target=rt.relay_server_stderr, args=(process.stderr, open(os.devnull, "w"), tail))
    thread.start()
    assert tail.why(process, thread) == "exit status 0, nothing on its error output"


# --- end to end: ./sandbox-ai, a start with the server, a connection through the real bridge ------------------------

# Podman as rootless Podman is on a host without a systemd user session (cgroupfs): a container that asks for a limit
# is refused as crun refuses it; one that asks for none runs a small MCP server (initialize, tools/list). Everything
# else goes to tests/live_fakes.py's Podman.
STAND_IN = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STAND_IN_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
image = next((a for a in args if a.startswith("ghcr.io/")), None)
if args[:1] == ["run"] and image and "--pod" not in args:
    if any(a.startswith(("--memory=", "--pids-limit=", "--cpus=")) for a in args) and \
            not os.path.exists(f"/run/user/{os.getuid()}/bus"):
        print("Error: OCI runtime error: crun: cannot set limits without cgroups", file=sys.stderr)
        sys.exit(126)
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "r2"}}
        elif message.get("method") == "tools/list":
            result = {"tools": [{"name": "open_file", "inputSchema": {"type": "object"}}]}
        elif "id" in message:
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    sys.exit(0)
os.execv(os.environ["STAND_IN_REAL"], [os.environ["STAND_IN_REAL"], *args])
'''
IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-radare2@{DIGEST}"
ENTRY = {"id": "mcp:radare2", "name": "radare2", "title": "radare2", "kind": "oci",
         "image": "ghcr.io/c0decave/sandbox-ai-mcp-radare2", "digest": DIGEST, "network": "none",
         "mounts": [{"target": "/work", "required": True,
                     "description": "The folder with the binaries to analyze (mount it read-only: --mount /work=DIR:ro)"}]}


@pytest.fixture()
def host(tmp_path):
    found = Host(tmp_path / "h")
    real = found.bin / "podman-real"
    (found.bin / "podman").rename(real)
    (found.bin / "podman").write_text(STAND_IN.replace("PYTHON", sys.executable, 1))
    (found.bin / "podman").chmod(0o755)
    found.env.update(STAND_IN_LOG=str(found.directory / "stand-in.log"), STAND_IN_REAL=str(real),
                     FAKE_PODMAN_RUN="0:1")
    found.add_image(IMAGE)
    found.add_image("localhost/codex-sandbox:latest", labels={"sandbox-ai.role": "agent"})
    yield found
    state = found.home / ".local/state/sandbox-ai/mcp"
    for directory in state.iterdir() if state.is_dir() else ():
        found.run("mcp", "stop", directory.name, timeout=60)


def connect(host, instance, requests):
    """One connection through the real bridge (runtime.py --internal-mcp-bridge), as an agent's relay makes it."""
    sock = host.home / ".local/state/sandbox-ai/mcp" / instance / "sock" / "server.sock"
    return subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-bridge", str(sock)],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
                          timeout=60, env=host.env)


def test_e2e_a_start_s_server_answers_the_handshake_where_podman_sets_no_limits(host, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [ENTRY]}))
    added = host.run("mcp", "add", "radare2", "--catalog", str(catalog), "--no-pull", "--unsigned", timeout=120)
    assert added.returncode == 0, added.stderr
    (host.workspace / "a.bin").write_bytes(b"\x7fELF")
    started = host.run("codex", "--backend", "podman", "--workspace", str(host.workspace), "--mcp", "radare2",
                       "--no-auth", "--", "true", timeout=600)
    assert started.returncode == 0, started.stderr[-3000:]
    instances = [p.name for p in (host.home / ".local/state/sandbox-ai/mcp").iterdir() if p.name.startswith("radare2.")]
    assert len(instances) == 1, instances
    done = connect(host, instances[0], [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert [a.get("id") for a in answers] == [1, 2], (done.stdout, done.stderr)
    assert "error" not in answers[0], answers[0]
    assert answers[1]["result"]["tools"][0]["name"] == "open_file"
    runs = [json.loads(line) for line in (host.directory / "stand-in.log").read_text().splitlines()
            if json.loads(line)[:1] == ["run"] and IMAGE in json.loads(line)]
    assert runs and all(f"--volume={host.workspace.resolve()}:/work:ro" in run for run in runs), runs
    if not Path(f"/run/user/{os.getuid()}/bus").exists():
        assert "are not enforced here" in started.stderr, started.stderr[-2000:]
