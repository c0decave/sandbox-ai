#!/usr/bin/env python3
"""Local MCP servers, each in its own sandbox, shared by agent sandboxes.

A server sandbox is a normal launcher run with no credentials and, unless its
definition allows more, no network. Inside Firejail it listens on a Unix
socket and starts one instance of the stdio MCP server per connection. Agent
sandboxes mount the socket directory read-only and reach it through a stdio
bridge, so no port or firewall opening is involved.

Definitions: built-in `demo`, ~/.config/sandbox-ai/mcp.json and the trusted
project file's "mcp_servers", later ones overriding earlier ones:

  {"NAME": {"command": ["npx", "-y", "@scope/server", "/work"],
            "options": ["--allow-github"],          launcher options for the server sandbox
            "agent": "codex",                       whose image runs it (default codex)
            "workspace": "/path/mounted/at/work",   default: an empty private directory
            "description": "..."}}
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
READY_TIMEOUT = 120
KEYS = {"command", "options", "agent", "workspace", "description"}
BUILTIN = {"demo": {"command": ["python3", "-I", "/opt/sandbox-ai/mcp_demo.py"],
                    "options": ["--offline", "--mount", f"{HERE / 'mcp_demo.py'}:/opt/sandbox-ai/mcp_demo.py:ro"],
                    "description": "Test server: list_directory and version"}}
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_mcp", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def forbidden():
    # Server sandboxes never get credentials, sessions, tmux or nested MCP.
    return launcher().CONFIG_FORBIDDEN + ("--mcp", "--mcp-socket-dir", "--tmux", "--copy-auth", "--auth-file",
                                          "--import-codex-auth", "--copy", "--probe", "--binary", "--state-dir",
                                          "--gui-wayland", "--git-token-env", "--git-token-file",
                                          "--git-credentials", "--kit", "--kit-dir",
                                          "--backend", "--podman-url",  # always the agent's own
                                          "--dangerously-skip-permissions")


def validated(servers, source):
    if not isinstance(servers, dict):
        raise ValueError(f"{source}: MCP servers must be an object of NAME: definition")
    result = {}
    for name, definition in servers.items():
        if not re.fullmatch(launcher().MCP_NAME, name):
            raise ValueError(f"{source}: invalid MCP server name {name!r}")
        if not isinstance(definition, dict) or set(definition) - KEYS:
            raise ValueError(f"{source}: {name}: allowed keys are {sorted(KEYS)}")
        command, options = definition.get("command"), definition.get("options", [])
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            raise ValueError(f"{source}: {name}: command must be a non-empty list of strings")
        if not isinstance(options, list) or not all(isinstance(v, str) for v in options):
            raise ValueError(f"{source}: {name}: options must be a list of strings")
        for value in options:
            if any(value == flag or value.startswith(flag + "=") for flag in forbidden()):
                raise ValueError(f"{source}: {name}: option {value} is not allowed for an MCP server")
        if definition.get("agent", "codex") not in launcher().AUTH_TARGETS:
            raise ValueError(f"{source}: {name}: agent must be codex, claude or opencode")
        if not isinstance(definition.get("workspace", ""), str):
            raise ValueError(f"{source}: {name}: workspace must be a path")
        # A relative path would resolve against each agent's own directory, while the running
        # server is shared by definition: another project's files could reach this agent.
        relative = launcher().relative_paths(options)
        workspace = definition.get("workspace")
        if workspace and not os.path.isabs(os.path.expanduser(workspace)):
            relative.append(f"workspace {workspace}")
        if relative:
            raise ValueError(f"{source}: {name}: use absolute paths (or ~/...), not: {', '.join(relative)}")
        result[name] = definition
    return result


def config_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "mcp.json"


def definitions(project=None):
    result = dict(BUILTIN)
    path = config_file()
    if path.is_file():
        try:
            document = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from None
        result.update(validated(document, path))
    if project:
        result.update(validated(project.get("mcp_servers", {}), "project configuration"))
    return result


def pod_name(name):
    """Fixed pod name of MCP server NAME, so a stale pod can be found and removed."""
    return f"agent-mcp-{name}"


def state_dir(name):
    return launcher().mcp_socket_dir(name).parent


def load_state(name):
    try:
        return json.loads((state_dir(name) / "mcp.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(name, data):
    directory = state_dir(name)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=directory)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, directory / "mcp.json")


def status(name):
    """running: the supervisor of this server is alive (not a reused PID).

    Decided by the supervisor alone: the socket directory is writable by the
    untrusted server, which could otherwise fake being stopped."""
    pid = load_state(name).get("pid")
    try:
        cmdline = (PROC / str(pid) / "cmdline").read_bytes().split(b"\0") if pid else []
    except OSError:
        cmdline = []
    return "running" if os.fsencode(str(launcher().mcp_socket_dir(name))) in cmdline else "stopped"


def wait_for_socket(path, log, process, timeout=READY_TIMEOUT):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            tail = launcher().printable(log.read_text(errors="replace")[-3000:]) if log.exists() else ""
            raise RuntimeError(f"MCP server sandbox failed to start (status {process.returncode}):\n{tail}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"MCP server did not become ready within {timeout} s; see {log}")
        time.sleep(0.2)


def definition_digest(definition, backend, podman_url=None):
    """Identity of a server instance: everything that decides what it can reach."""
    return hashlib.sha256(json.dumps([definition, backend, podman_url], sort_keys=True).encode()).hexdigest()


@contextlib.contextmanager
def server_lock(name, timeout=180):
    """Serialize start and stop of one server: a second agent waits until the first has started it,
    then reuses it, instead of starting another server on the same HOME."""
    if not re.fullmatch(launcher().MCP_NAME, name):
        raise ValueError(f"Invalid MCP server name: {name!r}")
    directory = state_dir(name)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError(f"MCP server {name} is busy (being started or stopped); try again") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def ensure_running(name, a):
    """Start the server sandbox for NAME unless it already runs; returns 'running' or 'started'."""
    with server_lock(name):
        return ensure_running_locked(name, a)


def ensure_running_locked(name, a):
    servers = definitions(getattr(a, "project_document", None))
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    digest = definition_digest(servers[name], a.backend, a.podman_url)
    if status(name) == "running":
        if load_state(name).get("definition") != digest:
            # Another project's definition of this name: its workspace must not reach this agent.
            raise ValueError(f"MCP server {name} is running with a different definition; "
                             f"stop it first: sandbox-mcp stop {name}")
        if (launcher().mcp_socket_dir(name) / "server.sock").exists():
            return "running"
        stop_locked(name)  # alive but its socket is gone: replace it rather than start a second one
    definition, directory, sock = servers[name], state_dir(name), launcher().mcp_socket_dir(name)
    # The server sandbox can write its socket directory: start from a fresh one.
    shutil.rmtree(sock, ignore_errors=True)
    for path in (directory, sock, directory / "home", directory / "work"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    pod = pod_name(name)
    if a.backend == "podman" and launcher().remove_stale_pod(a, pod, sock.resolve()):
        print(f"agent-sandbox: removed pod {pod} left behind by an earlier MCP server {name}", file=sys.stderr)
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), definition.get("agent", "codex"),
           "--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else []),
           "--no-provider-defaults", *definition.get("options", []), "--no-auth", "--no-config",
           "--state-dir", str(directory / "home"), "--mcp-socket-dir", str(sock),
           *(["--pod-name", pod] if a.backend == "podman" else []),
           "--workspace", definition.get("workspace") or str(directory / "work"), "--binary", "python3",
           "--", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", f"{launcher().MCP_SERVE_DIR}/server.sock",
           "--", *definition["command"]]
    log = directory / "supervisor.log"
    with open(log, "ab") as stream:
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True)
    save_state(name, {"pid": process.pid, "command": definition["command"], "backend": a.backend,
                      "podman_url": a.podman_url, "definition": digest,
                      "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    wait_for_socket(sock / "server.sock", log, process)
    print(f"agent-sandbox: MCP server {name} started in its own sandbox", file=sys.stderr)
    return "started"


def stop(name, timeout=30):
    with server_lock(name):
        return stop_locked(name, timeout)


def stop_locked(name, timeout=30):
    if status(name) != "running":
        print(f"agent-sandbox: MCP server {name} is not running", file=sys.stderr)
        return 0
    pid = load_state(name)["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while status(name) == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    if status(name) == "running":
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        state = load_state(name)
        if state.get("backend") == "podman":
            engine = SimpleNamespace(podman_url=state.get("podman_url"))
            try:
                # Without its supervisor the pod would keep running the server; only our labelled one goes.
                launcher().remove_stale_pod(engine, pod_name(name), launcher().mcp_socket_dir(name).resolve())
            except (ValueError, subprocess.CalledProcessError) as exc:
                print(f"agent-sandbox: {exc}", file=sys.stderr)
    (launcher().mcp_socket_dir(name) / "server.sock").unlink(missing_ok=True)
    print(f"agent-sandbox: MCP server {name} stopped", file=sys.stderr)
    return 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-mcp", description="Manage local MCP servers that run in their "
                                     "own sandboxes. Agents use them with --mcp NAME.")
    parser.add_argument("--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Defined servers and their status")
    start = commands.add_parser("start", help="Start a server sandbox now")
    start.add_argument("name")
    start.add_argument("--backend", choices=["podman", "bwrap"], default="podman")
    commands.add_parser("stop", help="Stop a server sandbox").add_argument("name")
    a = parser.parse_args(argv)
    if a.command == "list":
        rows = [["NAME", "STATUS", "COMMAND"]] + [[name, status(name), " ".join(d["command"])]
                                                  for name, d in sorted(definitions().items())]
        widths = [max(len(row[i]) for row in rows) for i in range(2)]
        for row in rows:
            print(f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  {row[2]}")
        return 0
    if a.command == "start":
        ensure_running(a.name, launcher().parse_args(["codex", "--backend", a.backend]))
        return 0
    return stop(a.name)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"sandbox-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
