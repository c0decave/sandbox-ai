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

A server can also be a container image from an MCP catalog (sandbox-ai-mcp, axross-mcp-hub):
`sandbox-ai mcp add NAME` checks the entry, pulls the image by digest and writes

  {"NAME": {"image": "ghcr.io/...@sha256:...", "network": "none" | "full",
            "mounts": [{"target": "/work", "host": "/abs/path", "mode": "ro"}],
            "env": ["VARIABLE"], "memory": "512m", "pids": 128, "description": "...", "source": "..."}}

Such a server runs per connection as `podman run --rm -i` with no capabilities, a read-only root
and only the network and folders the definition names; agents reach it the same way.
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
MCP_NAME_PATTERN = r"[A-Za-z0-9_-]{1,32}"
PROC = Path("/proc")
READY_TIMEOUT = 120
KEYS = {"command", "options", "agent", "workspace", "description"}
IMAGE_KEYS = {"image", "network", "mounts", "env", "memory", "pids", "description", "source"}
IMAGE_REF = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+"
                       r"@sha256:[0-9a-f]{64}")
MOUNT_TARGET = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
# The run line of catalog servers: the same as axross's, so images are tested for exactly this.
RUN_FLAGS = ("--rm", "-i", "--init", "--pull=never", "--log-driver=none", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--read-only",
             "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,notmpcopyup")
CATALOGS = (("mcp", "https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/main/catalog.json"),
            ("hub", "https://raw.githubusercontent.com/c0decave/axross-mcp-hub/main/catalog.json"))
CATALOG_LIMIT = 2 << 20
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
                                          "--git-credentials", "--git-token-secret", "--git-identity",
                                          "--kit", "--kit-dir",
                                          "--backend", "--podman-url",  # always the agent's own
                                          "--dangerously-skip-permissions")


def validated(servers, source):
    if not isinstance(servers, dict):
        raise ValueError(f"{source}: MCP servers must be an object of NAME: definition")
    result = {}
    for name, definition in servers.items():
        if not re.fullmatch(launcher().MCP_NAME, name):
            raise ValueError(f"{source}: invalid MCP server name {name!r}")
        if isinstance(definition, dict) and "image" in definition:
            result[name] = validated_image(definition, f"{source}: {name}")
            continue
        if not isinstance(definition, dict) or set(definition) - KEYS:
            raise ValueError(f"{source}: {name}: allowed keys are {sorted(KEYS)}")
        command, options = definition.get("command"), definition.get("options", [])
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            raise ValueError(f"{source}: {name}: command must be a non-empty list of strings")
        if not isinstance(options, list) or not all(isinstance(v, str) for v in options):
            raise ValueError(f"{source}: {name}: options must be a list of strings")
        options = launcher().canonical_options(options)  # -S/dir is --state-dir /dir
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
        result[name] = {**definition, "options": options} if "options" in definition else definition
    return result


def validated_image(definition, where):
    """A container definition with every value checked (it becomes a podman command line)."""
    if set(definition) - IMAGE_KEYS:
        raise ValueError(f"{where}: allowed keys of an image server are {sorted(IMAGE_KEYS)}")
    image = definition.get("image")
    if not isinstance(image, str) or not IMAGE_REF.fullmatch(image):
        raise ValueError(f"{where}: image must be REGISTRY/PATH@sha256:<64 hex>, pinned by digest")
    if definition.get("network", "none") not in ("none", "full"):
        raise ValueError(f"{where}: network must be none or full")
    mounts = definition.get("mounts", [])
    if not isinstance(mounts, list) or len(mounts) > 8:
        raise ValueError(f"{where}: mounts must be a list of at most 8 folders")
    targets = set()
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) - {"target", "host", "mode"}:
            raise ValueError(f"{where}: a mount is {{target, host, mode}}")
        target, host, mode = mount.get("target"), mount.get("host"), mount.get("mode", "ro")
        if not isinstance(target, str) or not MOUNT_TARGET.fullmatch(target) or ".." in target.split("/") \
                or target in targets:
            raise ValueError(f"{where}: mount target {target!r} is not a distinct absolute path")
        if not isinstance(host, str) or not os.path.isabs(os.path.expanduser(host)) or \
                any(c in host for c in (",", "\n", "\r", "\0", ":")):
            raise ValueError(f"{where}: mount host {host!r} must be an absolute path without , or :")
        if mode not in ("ro", "rw"):
            raise ValueError(f"{where}: mount mode must be ro or rw")
        targets.add(target)
    env = definition.get("env", [])
    if not isinstance(env, list) or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", v)
                                            for v in env):
        raise ValueError(f"{where}: env must be a list of variable names")
    memory = definition.get("memory", "512m")
    if not isinstance(memory, str) or not re.fullmatch(r"[1-9][0-9]{0,5}[mg]", memory):
        raise ValueError(f"{where}: memory must be like 512m or 4g")
    pids = definition.get("pids", 128)
    if isinstance(pids, bool) or not isinstance(pids, int) or not 16 <= pids <= 4096:
        raise ValueError(f"{where}: pids must be 16-4096")
    for key in ("description", "source"):
        if key in definition and not isinstance(definition[key], str):
            raise ValueError(f"{where}: {key} must be text")
    return definition


def check_image_policy(name, definition):
    """An image server runs podman itself, outside the launcher: the administrator's policy is applied here. A
    server with network acts for the agent past its firewall and proxy, so mcp_network none (or a network
    ceiling, allow_within) refuses it."""
    l = launcher()
    policy = l.load_policy()
    if definition.get("network", "none") == "full" and \
            (policy.get("mcp_network", "full") == "none" or policy.get("allow_within") is not None):
        raise ValueError(f"MCP server {name}: network full is not allowed by the administrator's policy "
                         f"({l.POLICY_FILE})")


def check_image_mounts(name, definition):
    """The host folders of an image server pass the launcher's host-path policy: no symlink a sandbox may have
    planted on the way, no private location for rw, and not inside a tree an agent can write (it could swap
    the folder for a link before the next connection mounts it). Their rw folders join the registry."""
    l = launcher()
    before = l.exposed_before()
    writable = []
    for mount in definition.get("mounts", []):
        host = os.path.expanduser(mount["host"])
        option = f"MCP server {name}: mount {mount['target']}"
        l.check_planted(host, option, before)
        resolved = Path(host).resolve()
        for tree in before:
            if tree in resolved.parents:
                raise ValueError(f"{option}: {resolved} lies inside {tree}, which a sandbox can write; it could "
                                 "replace the folder with a link before a connection mounts it. Use a folder of its own.")
        if mount.get("mode", "ro") == "rw":
            l.check_private_location(resolved, option)
            writable.append(resolved)
    if writable:
        l.remember_exposed(None, writable)


def mount_records(definition):
    """[{"path", "dev", "ino"}] of an image server's host folders, as image_command mounts them."""
    records = []
    for mount in definition.get("mounts", []):
        path = Path(os.path.expanduser(mount["host"])).resolve()
        info = os.stat(path)
        records.append({"path": str(path), "dev": info.st_dev, "ino": info.st_ino})
    return records


def image_command(definition, engine="podman"):
    """The podman command that runs one instance of an image server (one per connection)."""
    command = [engine, "run", *RUN_FLAGS, f"--pids-limit={definition.get('pids', 128)}",
               f"--memory={definition.get('memory', '512m')}"]
    # full: pasta without the host's loopback or forwarded ports, as the agents' own sandboxes.
    command.append("--network=none" if definition.get("network", "none") == "none"
                   else f"--network={launcher().POD_NETWORK}")
    for name in definition.get("env", []):
        command.append(f"--env={name}")  # the value comes from the environment, never the command line
    if definition.get("mounts"):
        command.append("--user=0:0")  # the mounted files belong to you, who are root in rootless podman
        for mount in definition["mounts"]:
            host = Path(os.path.expanduser(mount["host"])).resolve()
            command.append(f"--volume={host}:{mount['target']}:{mount.get('mode', 'ro')}")
    return [*command, definition["image"]]


def read_catalog(source, fetch=None):
    """Entries of an MCP catalog (axross-mcp-catalog schema, version 1) from a file or https URL."""
    if source.startswith("https://"):
        import urllib.request
        fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=30).read(CATALOG_LIMIT + 1))
        data = fetch(source)
    else:
        data = Path(os.path.expanduser(source)).read_bytes()[:CATALOG_LIMIT + 1]
    if len(data) > CATALOG_LIMIT:
        raise ValueError(f"{source}: larger than {CATALOG_LIMIT >> 20} MiB")
    try:
        document = json.loads(data)
    except ValueError:
        raise ValueError(f"{source}: not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != "axross-mcp-catalog" or \
            document.get("version") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError(f"{source}: not an MCP catalog (schema axross-mcp-catalog, version 1)")
    return [entry for entry in document["entries"] if isinstance(entry, dict)]


def catalog_entries(sources=None, fetch=None):
    """[(prefix, entry)] of all catalogs; unreachable ones are reported, not fatal."""
    found, problems = [], []
    for prefix, source in sources or CATALOGS:
        try:
            found += [(prefix, entry) for entry in read_catalog(source, fetch)]
        except (OSError, ValueError) as exc:
            problems.append(f"{source}: {exc}")
    return found, problems


def find_entry(entries, name):
    """The entry NAME (or PREFIX:NAME) names; ambiguity is an error."""
    prefix, _, bare = name.rpartition(":")
    matches = [(p, e) for p, e in entries if e.get("name") == bare and (not prefix or p == prefix
                                                                        or e.get("id") == name)]
    if not matches:
        raise ValueError(f"no catalog entry {name}; see sandbox-ai mcp catalog")
    if len(matches) > 1:
        raise ValueError(f"{name} is in several catalogs: {', '.join(p + ':' + bare for p, _ in matches)}")
    return matches[0][1]


def definition_from_entry(entry, mounts=(), env=(), network=None, memory=None, pids=None):
    """A definition for ENTRY with the folders, variables and network given on the command line."""
    digest, image = entry.get("digest"), entry.get("image")
    if entry.get("kind") != "oci" or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"{entry.get('name')}: the catalog has no digest for it yet (not published)")
    wanted = entry.get("network", "none")
    if network is None:
        if wanted == "allowlist":
            raise ValueError(f"{entry['name']} needs {', '.join(entry.get('hosts', []))}; sandbox-ai cannot limit a "
                             "server to these hosts yet: add --network full to grant it the outside network")
        network = wanted
    if network not in ("none", "full"):
        raise ValueError("--network must be none or full")
    known = {m.get("target"): m for m in entry.get("mounts", []) if isinstance(m, dict)}
    given = {}
    for value in mounts:
        target, sep, rest = value.partition("=")
        host, _, mode = rest.rpartition(":") if rest.endswith((":ro", ":rw")) else (rest, "", "ro")
        if not sep or target not in known:
            raise ValueError(f"--mount TARGET=HOST[:ro|rw]; {entry['name']} takes "
                             + (", ".join(known) or "no folders"))
        path = Path(os.path.expanduser(host)).resolve()
        if not path.is_dir():
            raise ValueError(f"--mount {value}: {path} is not a directory")
        given[target] = {"target": target, "host": str(path), "mode": mode or "ro"}
    missing = [t for t, m in known.items() if m.get("required") and t not in given]
    if missing:
        raise ValueError(f"{entry['name']} needs --mount {missing[0]}=HOST")
    required = [v.get("name") for v in entry.get("env", []) if isinstance(v, dict) and v.get("required")]
    unknown = [n for n in env if n not in [v.get("name") for v in entry.get("env", []) if isinstance(v, dict)]]
    if unknown:
        raise ValueError(f"{entry['name']} does not use {', '.join(unknown)}")
    if [n for n in required if n not in env]:
        raise ValueError(f"{entry['name']} needs --env {[n for n in required if n not in env][0]} (the variable "
                         "is read from your environment when the server starts)")
    definition = {"image": f"{image}@{digest}", "network": network, "mounts": list(given.values()),
                  "env": list(env), "description": str(entry.get("title") or entry.get("name")),
                  "source": str(entry.get("id") or entry.get("name"))}
    if memory:
        definition["memory"] = memory
    if pids:
        definition["pids"] = pids
    return validated_image(definition, entry["name"])


def add(name, local=None, mounts=(), env=(), network=None, memory=None, pids=None, catalogs=None,
        pull=True, fetch=None, out=print):
    """sandbox-ai mcp add: write the definition of catalog entry NAME into mcp.json, image pulled by digest."""
    entries, problems = catalog_entries(catalogs, fetch)
    for problem in problems:
        out(f"sandbox-mcp: catalog not read: {problem}")
    entry = find_entry(entries, name)
    definition = definition_from_entry(entry, mounts, env, network, memory, pids)
    local = local or entry["name"]
    if not re.fullmatch(MCP_NAME_PATTERN, local) or local in BUILTIN:
        raise ValueError(f"invalid or reserved server name: {local}")
    if entry.get("network") == "allowlist":
        out(f"sandbox-mcp: {local} gets the full outside network; it needs only {', '.join(entry.get('hosts', []))}")
    if pull:
        subprocess.run(["podman", "pull", "--quiet", definition["image"]], check=True, stdout=subprocess.DEVNULL)
    path = config_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        document = json.loads(path.read_text()) if path.is_file() else {}
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from None
    document[local] = definition
    fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2)
    os.replace(temporary, path)
    out(f"sandbox-mcp: {local} added ({definition['image']}); use it with --mcp {local}")
    return definition


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
    directory = os.fsencode(str(launcher().mcp_socket_dir(name)))
    return "running" if any(arg == directory or arg == directory + b"/server.sock" for arg in cmdline) else "stopped"


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
    if "image" in definition:
        check_image_mounts(name, definition)
        check_image_policy(name, definition)
        # A container per connection, started by a small bridge on this host: the image brings its own
        # isolation (see image_command); the bridge only connects the socket with podman's stdio.
        cmd = [sys.executable, "-I", str(HERE / "runtime.py"), "--internal-mcp-serve", str(sock / "server.sock"),
               "--", *image_command(definition)]
        environment = dict(os.environ)
        for variable in definition.get("env", []):
            if variable not in environment:
                # Not in your environment: the secret of that name (sandbox-ai secrets), if there is one.
                spec = importlib.util.spec_from_file_location("secret_store_for_mcp", HERE / "secret_store.py")
                store = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(store)
                value = store.get(variable)
                if value is None:
                    raise ValueError(f"MCP server {name} needs the variable {variable}: set it, or keep it with "
                                     f"sandbox-ai secrets set {variable}")
                environment[variable] = value
        # The bridge compares every folder with what was checked here before each connection's container.
        environment["SANDBOX_AI_MCP_MOUNTS"] = json.dumps(mount_records(definition))
        log = directory / "supervisor.log"
        with open(log, "ab") as stream:
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                       start_new_session=True, cwd=str(directory), env=environment)
        save_state(name, {"pid": process.pid, "image": definition["image"], "backend": "podman",
                          "definition": digest, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        wait_for_socket(sock / "server.sock", log, process)
        print(f"agent-sandbox: MCP server {name} started (container {definition['image']})", file=sys.stderr)
        return "started"
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
    parser.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Defined servers and their status")
    start = commands.add_parser("start", help="Start a server sandbox now")
    start.add_argument("name")
    start.add_argument("-b", "--backend", choices=["podman", "bwrap"], default="podman")
    commands.add_parser("stop", help="Stop a server sandbox").add_argument("name")
    catalog = commands.add_parser("catalog", help="Servers of the MCP catalogs (sandbox-ai-mcp, axross-mcp-hub)")
    catalog.add_argument("-c", "--catalog", action="append", default=[], metavar="FILE_OR_URL",
                         help="Read this catalog instead of the default ones; repeatable")
    added = commands.add_parser("add", help="Add a catalog server: pull its image by digest, write its definition")
    added.add_argument("name", help="Catalog entry, e.g. duckduckgo or hub:binwalk")
    added.add_argument("-a", "--as", dest="local", metavar="NAME", help="Name for --mcp (default: the entry's)")
    added.add_argument("-m", "--mount", action="append", default=[], metavar="TARGET=HOST[:ro|rw]")
    added.add_argument("-e", "--env", action="append", default=[], metavar="VARIABLE",
                       help="Pass this variable from your environment (a secret the entry names)")
    added.add_argument("-n", "--network", choices=["none", "full"],
                       help="Override the entry's network; an allowlist entry needs full")
    added.add_argument("-M", "--memory", metavar="SIZE", help="Memory limit, e.g. 2g (default 512m)")
    added.add_argument("-p", "--pids", type=int, metavar="N", help="Process limit (default 128)")
    added.add_argument("-c", "--catalog", action="append", default=[], metavar="FILE_OR_URL")
    added.add_argument("-N", "--no-pull", action="store_true", help="Do not pull the image (it is there already)")
    a = parser.parse_args(argv)
    if a.command in ("catalog", "add"):
        sources = [(f"c{index}", value) for index, value in enumerate(a.catalog)] or None
    if a.command == "catalog":
        entries, problems = catalog_entries(sources)
        for problem in problems:
            print(f"sandbox-mcp: catalog not read: {problem}", file=sys.stderr)
        for prefix, entry in sorted(entries, key=lambda pe: (pe[0], str(pe[1].get("name")))):
            state = "" if entry.get("digest") else "  (not published yet)"
            network = entry.get("network", "none") + (f" {','.join(entry.get('hosts', []))}" if entry.get("hosts") else "")
            print(f"{prefix}:{entry.get('name')}  [{network}]  {entry.get('title', '')}{state}")
        return 0
    if a.command == "add":
        add(a.name, a.local, a.mount, a.env, a.network, a.memory, a.pids, sources, pull=not a.no_pull)
        return 0
    if a.command == "list":
        rows = [["NAME", "STATUS", "COMMAND"]] + [[name, status(name), d["image"] if "image" in d else " ".join(d["command"])]
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
