#!/usr/bin/env python3
"""ARTEX (RuoJi6/ARTEX v0.3.15, AGPL-3.0) in sandbox-ai: an AI penetration-testing platform as a part of its own
beside the agent (--artex), built by sandbox-ai from the reviewed source with sandbox-ai's security patches
(docs/ARTEX.md: the review, the patches, what each closes).

  --artex                       ARTEX in its own container beside the agent; its console on this computer's loopback
                                (its image once: sandbox-ai build artex)
  --artex-data fresh|session    its data (PostgreSQL, its stores, its skills): fresh (the default: new at every start,
                                in memory) or kept with the session (a Podman volume, removed with the session)
  --artex-port PORT             the port of this computer's loopback the console is forwarded to (default: 8787, or the
                                next free one of the 20 after it)

  sandbox-ai artex console [RUN] [--json]   the console's address, user and password of a running ARTEX

The container (POD-service-artex) of localhost/sandbox-ai-artex: ARTEX at the reviewed commit, its self-update
removed at the build, on a Debian base pinned by digest with PostgreSQL, Chromium, nmap, Node and Playwright's MCP and
CLI pinned. It is started like a service (created, initialised, proven to be in the pod's user namespace and, with
--vpn, behind the VPN's kill switch; after the supervisor's firewall and proxy are up), shares the pod's network (the
agent's firewall, allow-list, connection proxy, approvals, kill switch, VPN or Tor), runs as the image's user artex
(1000) without capabilities on a read-only root, and holds no file of the project, no login and no key: the only
folders of this computer in it are read-only files (its runtime, its configuration, the sandbox's resolver and hosts
file, the run's CA certificate). Its data is a tmpfs, or a Podman volume with --artex-data session.

ARTEX listens on the pod's loopback only (127.0.0.1:8787, its recording proxy 127.0.0.1:8788; sandbox-ai's build
refuses any other address). Its admin password is chosen by sandbox-ai at every start and set before ARTEX listens
(the build has no first-caller setup); the console is forwarded to this computer's 127.0.0.1 only, through `podman exec`
of artex_runtime.py connect per connection: no port of the pod is published, nothing listens on another interface.
Its LLM: with --llm, the server sandbox-ai has (its key added by the connection proxy on the wire; ARTEX sees a
placeholder); else whatever you configure in its console, reached only through the allow-list. Its targets: only
what the allow-list opens (--allow-tcp, --allow-host ...: the scope).

Standard library only.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

REPOSITORY = "https://github.com/RuoJi6/ARTEX"
# Tag v0.3.15 (2026-10-09; "fix(release): retain upstream version 0.3.15"): the tree reviewed line by line and by
# whole-tree scans (docs/ARTEX.md), the tree failsafe-security/artex imported. Its git tree is checked at the build.
COMMIT = "a91b27feb7168c1486f827559bc48a589a920012"
TREE = "22083e0bb9db66b76b94814f368d0d1dc5c3adbb"
COMMIT_DATE = "2026-10-09"
VERSION = "0.3.15"
NORMA = "github.com/Autumn-27/norma v0.4.3 (320ae32989da, its h1 hash in ARTEX's go.sum)"
LICENSE = "AGPL-3.0"
IMAGE = "localhost/sandbox-ai-artex:latest"
ROLE = "artex"  # the image's label sandbox-ai.role (build_image.sh)
BUILD = "sandbox-ai build artex"
NAME = "artex"  # the container: POD-service-artex (services.container_name)
USER = "1000:1000"  # the image's user artex: PostgreSQL refuses root, and ARTEX's agents get no root
PORT = 8787  # the console and its API, on the pod's loopback
PROXY_PORT = 8788  # ARTEX's recording proxy, on the pod's loopback
SERVER = f"http://127.0.0.1:{PORT}"
HOST_PORT = 8787  # the console on this computer's loopback (--artex-port; else this or one of the next HOST_TRIES)
HOST_TRIES = 20
LIMITS = {"memory": "4g", "cpus": "2", "pids": 2048}
DATA_SIZE = "2g"  # its tmpfs (--artex-data fresh)
READY_TIMEOUT = 240  # PostgreSQL's first cluster, then ARTEX's own start
MOUNT = "/sandbox-artex"  # in its container: the runtime, its configuration, the run's CA
DATA = "/var/lib/artex"
CONFIG_FILE = "artex.json"  # in the run's session folder, mounted at MOUNT/config.json
CA_FOLDER = "artex-ca"  # in the run's session folder: the run's CA certificate, mounted at MOUNT/ca
CA_FILE = "browser-ca.pem"  # what the supervisor puts into the run's directory (runtime.share_browser_ca)
PASSWORD_ENV = "SANDBOX_AI_ARTEX_PASSWORD"  # artex_runtime.PASSWORD_ENV
CONSOLE_USER = "ARTEX"  # ARTEX's one user name (server/auth.go)
CONSOLE_FILE = "console.json"  # in the run's ARTEX folder (console_dir): the console's address and password
PRESET = "artex-pentest"
OPTIONS = {"--artex-data": "artex_data", "--artex-port": "artex_port"}
DATAS = ("fresh", "session")
TELEMETRY_OFF = ("NORMA_RIPGREP_NO_INSTALL", "NEXT_TELEMETRY_DISABLED", "DO_NOT_TRACK")  # artex_runtime.FIXED
HERE = Path(__file__).resolve().parent

_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_artex", HERE / "agent_sandbox.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _launcher = loaded
    return _launcher


def given(a):
    return [option for option, dest in OPTIONS.items() if getattr(a, dest, None) not in (None, False)]


def parse_port(value):
    """--artex-port's PORT (1024-65535), or ValueError."""
    text = str(value).strip()
    if not text.isdigit() or not 1024 <= int(text) <= 65535:
        raise ValueError(f"--artex-port is a TCP port from 1024 to 65535: {value!r}")
    return int(text)


def check(a, l=None):
    """--artex and its options, checked; a.artex_container gets ARTEX's container (a member of services.members).
    ValueError for whatever the launcher refuses."""
    a.artex_container = None
    if not getattr(a, "artex", False):
        if given(a):
            raise ValueError(f"{given(a)[0]} sets an option of ARTEX; add --artex")
        return None
    l = l or launcher()
    if a.backend != "podman":
        raise ValueError("--artex needs the Podman backend: ARTEX is a container of its own in the sandbox's Podman "
                         "pod, beside the agent; Bubblewrap has no pod and no container engine")
    if a.podman_url:
        raise ValueError("--artex with --podman-url: its console would be forwarded from that machine's engine; start "
                         "it there (sandbox-ai hosts run HOST -- ...)")
    if getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None):
        raise ValueError("--artex is for an agent's start; an MCP server's or helper's sandbox has none")
    data = getattr(a, "artex_data", None) or "fresh"
    if data not in DATAS:
        raise ValueError(f"--artex-data is fresh or session: {data!r}")
    services = l.load_services_module()
    if data == "session" and services.volume_key(a) is None:
        raise ValueError("--artex-data session keeps ARTEX's data with the session, but this start keeps none (its "
                         "HOME is discarded): use fresh, or start a session that is kept")
    a.artex_port = parse_port(a.artex_port) if getattr(a, "artex_port", None) not in (None, "") else None
    for port in (PORT, PROXY_PORT):
        if port in l.host_ports(a):
            raise ValueError(f"--artex: ARTEX listens on port {port} of the sandbox's loopback, which is a port of the "
                             f"host here (--allow-host-port {port})")
    for service in getattr(a, "services", None) or []:
        if service["name"] == NAME:
            raise ValueError(f"--service {NAME}: that name is ARTEX's (--artex); name it otherwise")
        for port in (PORT, PROXY_PORT):
            if port in service.get("ports", []):
                raise ValueError(f"--service {service['name']}: port {port} is ARTEX's (--artex)")
    a.artex_container = {
        "name": NAME, "artex": True, "image": ROLE, "user": USER, "ports": [PORT, PROXY_PORT], "ready": PORT,
        "ready_timeout": READY_TIMEOUT, "env": [], "secrets": [], "data": [DATA], "keep": data == "session",
        "read_only": True, "board": data, **LIMITS, "record": {"image": IMAGE, "id": None, "kind": "sandbox-ai"}}
    return a.artex_container


def llm_environment(a, l=None):
    """ARTEX's LLM from --llm (its environment: ARTEX's FromEnv; a profile saved in its console wins): the server's
    OpenAI-compatible API at the address the sandbox reaches it, its model, and the placeholder the connection proxy
    swaps for the key. Nothing secret; {} without --llm."""
    if not getattr(a, "llm", None):
        return {}
    llm = (l or launcher()).load_llm_module()
    found, model = llm.selection(a)
    return {"ARTEX_LLM_PROVIDER": "openai", "ARTEX_LLM_BASE_URL": llm.agent_url(found),
            "ARTEX_LLM_MODEL": model or "", "OPENAI_API_KEY": llm.PLACEHOLDER}


def config(a, l=None):
    """MOUNT/config.json of ARTEX's runtime (artex_runtime.py)."""
    return {"port": PORT, "proxy_port": PROXY_PORT, "data": DATA, "llm": llm_environment(a, l)}


def notes(a):
    """What a start with ARTEX says about it before it runs."""
    if not getattr(a, "artex_container", None):
        return []
    found = [f"artex: ARTEX in its own container beside the agent, on the agent's network (firewall, allow-list, "
             f"proxy, approvals, kill switch): its targets are only what this start's allow-list opens",
             f"artex: its console is forwarded to this computer's 127.0.0.1:{a.artex_port or HOST_PORT} (or the next "
             f"free port) once it runs; user {CONSOLE_USER}, its password: sandbox-ai artex console",
             "artex: ARTEX's agents run shell commands, scanners and a browser on their own, without asking (its "
             "design): this sandbox's firewall, proxy and approvals hold, and nothing of your project is in its "
             "container",
             f"artex: {LICENSE}, built by {BUILD} from {REPOSITORY} at {COMMIT[:10]} with sandbox-ai's security "
             "patches (docs/ARTEX.md)"]
    if getattr(a, "llm", None):
        found.append("artex: its LLM is the --llm server (the connection proxy adds its key; ARTEX sees a placeholder)")
    else:
        found.append("artex: no --llm: configure ARTEX's model in its console; its provider is reached only if the "
                     "allow-list opens it, and a key you type there is stored in ARTEX's database")
    if (getattr(a, "artex_data", None) or "fresh") == "session":
        found.append("artex: its data is kept with the session (a Podman volume, removed with the session)")
    return found


def console_dir(a, l=None):
    """The run's ARTEX folder on this computer (beside the run's directory, only yours): console.json."""
    if getattr(a, "control_dir", None):
        return Path(a.control_dir).with_name(Path(a.control_dir).name + ".artex")
    return (l or launcher()).runs_dir() / f"{a.run_id}.artex"


def prepare_folders(a, l=None, token=None):
    """At a real start: the run's ARTEX folder (0700; removed with the run by the caller) and the admin password of
    this start (a.artex_password: never on a command line, never in a file of the sandbox's)."""
    folder = console_dir(a, l)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    a.artex_password = (token or secrets.token_urlsafe)(24)
    return folder


def write_console(folder, url, password, container, port):
    """FOLDER/console.json (0600): what `sandbox-ai artex console` shows."""
    path = Path(folder) / CONSOLE_FILE
    temporary = path.with_name(f".{CONSOLE_FILE}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"url": url, "user": CONSOLE_USER, "password": password, "container": container, "port": port},
                  handle)
    os.replace(temporary, path)
    return path


def image_id(a, l=None, run=None):
    """The ID of sandbox-ai's ARTEX image in this machine's Podman, or None (not there, or not sandbox-ai's: its
    label sandbox-ai.role must be artex)."""
    l = l or launcher()
    run = run or subprocess.run
    try:
        done = run(l.podman_command(a, "image", "inspect", "--format",
                                    '{{.Id}} {{index .Labels "sandbox-ai.role"}}', IMAGE),
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ValueError("Required tool missing: podman") from None
    words = (done.stdout or "").split()
    if done.returncode != 0 or len(words) != 2 or words[1] != ROLE or \
            not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", words[0]):
        return None
    return words[0][len("sha256:"):] if words[0].startswith("sha256:") else words[0]


def prepare(a, l=None, run=None):
    """At a real start: ARTEX's image is there; its ID is what the container is made of. ValueError naming the build
    otherwise."""
    container = getattr(a, "artex_container", None)
    if not container:
        return None
    found = image_id(a, l, run)
    if found is None:
        raise ValueError(f"--artex: sandbox-ai's ARTEX image {IMAGE} is not there (nothing is pulled at a start); "
                         f"build it once: {BUILD}")
    container["record"]["id"] = found
    return found


def read_ca(control_dir):
    """The run's CA certificate the supervisor put into the run's directory (browser.read_ca's checks), or None."""
    spec = importlib.util.spec_from_file_location("browser_for_artex", HERE / "browser.py")
    browser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(browser)
    return browser.read_ca(control_dir)


def mounts(a, session, ca=None):
    """[(host path, container path, mode)] of ARTEX's container: read-only files only (its data apart)."""
    session = Path(session)
    found = [(session / "resolv.conf", "/etc/resolv.conf", "ro"), (session / "hosts", "/etc/hosts", "ro"),
             (HERE / "artex_runtime.py", f"{MOUNT}/artex_runtime.py", "ro"),
             (session / CONFIG_FILE, f"{MOUNT}/config.json", "ro")]
    if ca is not None:
        found.append((ca, f"{MOUNT}/ca", "ro"))
    return found


def data_option(a, service, l=None):
    """Its data folder: the session's Podman volume (--artex-data session; made by services.launch) or a tmpfs; both
    owned by its user (U), executables allowed (a tester's tools), no setuid, no devices."""
    services = (l or launcher()).load_services_module()
    (path, kind, volume), = services.data_mounts(a, service)
    if kind == "volume":
        return ["--volume", f"{volume}:{path}:rw,nosuid,nodev,U"]
    return ["--tmpfs", f"{path}:rw,exec,nosuid,nodev,size={DATA_SIZE},mode=0700,U"]


def create_command(a, pod, service, session, l=None, limits=None):
    """(podman create command, its environment) of ARTEX's container in POD: created only (services.launch proves its
    namespaces before it starts it). The admin password goes in podman's environment (--env NAME), never on the
    command line. The run's CA, when the proxy made one, is copied into SESSION/artex-ca and mounted read-only."""
    l = l or launcher()
    services = l.load_services_module()
    limits = services.enforceable(a, l) if limits is None else limits
    image = (service.get("record") or {}).get("id")
    if not image:
        raise ValueError(f"ARTEX's image was not checked ({BUILD})")
    password = getattr(a, "artex_password", None)
    if not password:
        raise ValueError("ARTEX's admin password was not made (artex.prepare_folders)")
    ca = None
    data = read_ca(a.control_dir) if getattr(a, "control_dir", None) else None
    if data is not None:
        ca = Path(session) / CA_FOLDER
        ca.mkdir(mode=0o755, exist_ok=True)
        (ca / "ca.pem").write_bytes(data)
        (ca / "ca.pem").chmod(0o644)
    argv = l.podman_command(a, "create", "--pull=never", "--pod", pod, "--name", services.container_name(pod, service),
                            "--label", f"{l.RUN_LABEL}={a.run_id}", "--label", f"{services.SERVICE_LABEL}={NAME}",
                            "--user", USER, "--cap-drop=ALL", "--security-opt=no-new-privileges", "--init",
                            "--read-only", "--log-driver=k8s-file", "--log-opt", "max-size=10mb",
                            "--image-volume=tmpfs",
                            # PostgreSQL's socket, Chromium's and the tools' temporary files.
                            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g,mode=1777",
                            # A /dev/shm of its own: the pod's (its IPC namespace's) is the agent's too.
                            "--tmpfs", "/dev/shm:rw,nosuid,nodev,noexec,size=512m",
                            "--env", "HOME=/tmp/home", "--env", PASSWORD_ENV)
    argv += data_option(a, service, l)
    if "memory" in limits:
        argv += ["--memory", service["memory"]]
    if "cpus" in limits:
        argv += ["--cpus", str(service["cpus"])]
    if "pids" in limits:
        argv += ["--pids-limit", str(service["pids"])]
    for source, target, mode in mounts(a, session, ca):
        argv += l.mount_option(source, target, mode)
    return argv + ["--entrypoint", "/usr/bin/python3", image, "-I", f"{MOUNT}/artex_runtime.py", "serve"], \
        {PASSWORD_ENV: password}


def plan(a, l=None):
    """The dry run's account of ARTEX, or None without it."""
    if not getattr(a, "artex_container", None):
        return None
    l = l or launcher()
    pod = l.pod_name(a, getattr(a, "run_id", None))
    data = getattr(a, "artex_data", None) or "fresh"
    services = l.load_services_module()
    (_, kind, volume), = services.data_mounts(a, a.artex_container)
    return {"container": f"{pod}-service-{NAME}", "image": IMAGE, "build": BUILD, "source": REPOSITORY,
            "commit": COMMIT, "tree": TREE, "version": VERSION, "licence": LICENSE, "user": USER,
            "listens": [f"127.0.0.1:{PORT} (console and API)", f"127.0.0.1:{PROXY_PORT} (its recording proxy)"],
            "console": {"host": f"http://127.0.0.1:{a.artex_port or HOST_PORT}",
                        "port": a.artex_port or f"{HOST_PORT}, or the next free of the {HOST_TRIES} after it",
                        "user": CONSOLE_USER, "password": "chosen at every start, set before ARTEX listens: "
                                                          "sandbox-ai artex console",
                        "how": "this computer's 127.0.0.1 only, a podman exec of artex_runtime.py connect per "
                               "connection; no port of the pod is published"},
            "network": "the pod's own, which is the agent's: its firewall, allow-list (the scope), connection proxy, "
                       "approvals, kill switch and VPN or Tor",
            "llm": {key: value for key, value in llm_environment(a, l).items()} or
            "none from sandbox-ai: configure it in ARTEX's console (only what the allow-list opens is reached)",
            "capabilities": "none (--cap-drop=ALL, no-new-privileges)", "read_only": True, "limits": dict(LIMITS),
            "data": {"kind": data, "where": f"the Podman volume {volume}, removed with the session"
                     if kind == "volume" else "a tmpfs in its container: new at every start, gone with the run"},
            "host_files": [target for _, target, _ in mounts(a, Path("SESSION"),
                                                             Path("SESSION") / CA_FOLDER
                                                             if l.proxy_enabled(a) else None)],
            "self_update": "removed at the build (the selfupdate package, its routes, its start-time install)"}


def record(a, pod):
    """ARTEX in run.json: sandbox-ai list and the app show it."""
    if not getattr(a, "artex_container", None):
        return None
    return {"server": SERVER, "container": f"{pod}-service-{NAME}", "console": "sandbox-ai artex console",
            "data": getattr(a, "artex_data", None) or "fresh"}


def agent_environment(a):
    """What the agent is told of ARTEX: its API's address in the pod."""
    return {"SANDBOX_AI_ARTEX": SERVER} if getattr(a, "artex_container", None) else {}


# -- the console's forward to this computer's loopback --

def bind(port=None, tries=HOST_TRIES, make=socket.socket):
    """A listening socket on 127.0.0.1 (never another address): PORT, or HOST_PORT or one of the TRIES after it."""
    candidates = [port] if port else list(range(HOST_PORT, HOST_PORT + tries + 1))
    last = None
    for candidate in candidates:
        listener = make(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", candidate))
            listener.listen(32)
            return listener
        except OSError as exc:
            last = exc
            listener.close()
    raise OSError(f"no free port of 127.0.0.1 among {candidates[0]}"
                  f"{'-' + str(candidates[-1]) if len(candidates) > 1 else ''} ({last})")


def exec_command(a, container, l=None):
    """The podman exec line of one console connection: ARTEX's runtime pipes it to the console in the pod."""
    return (l or launcher()).podman_command(a, "exec", "-i", container, "/usr/bin/python3", "-I",
                                            f"{MOUNT}/artex_runtime.py", "connect")


def pump(source, sink):
    """Bytes from SOURCE (a read(n) callable) to SINK (a write(data) callable) until either ends."""
    try:
        while True:
            data = source(65536)
            if not data:
                break
            sink(data)
    except (OSError, ValueError):
        pass


def relay(connection, argv, popen=subprocess.Popen):
    """One browser connection to the console through its own podman exec, until either side closes."""
    try:
        child = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except OSError:
        connection.close()
        return

    def down():
        pump(child.stdout.read, connection.sendall)
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
    reader = threading.Thread(target=down, daemon=True)
    reader.start()

    def write(data):
        child.stdin.write(data)
        child.stdin.flush()
    pump(connection.recv, write)
    with contextlib.suppress(OSError, ValueError):
        child.stdin.close()
    reader.join(timeout=30)
    with contextlib.suppress(OSError):
        connection.close()
    if child.poll() is None:
        child.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=5)


def started(control_dir, l):
    """The services' answer (SERVICES_STARTED in the run's directory): True when ARTEX started, False when the start
    failed, None while there is none."""
    try:
        answer = json.loads((Path(control_dir) / l.RUNTIME.SERVICES_STARTED).read_text())
    except (OSError, ValueError, RecursionError):  # (a deeply nested damaged file: as a damaged one)
        return None
    return bool(answer.get("ok")) and NAME in (answer.get("services") or [])


def forward(a, pod, alive, l=None, popen=subprocess.Popen, sleep=time.sleep, out=None, make=socket.socket):
    """The console's forward, for as long as the run lives (ALIVE()): once ARTEX started, this computer's
    127.0.0.1:PORT, each connection to ARTEX's console through its own podman exec. Writes console.json; says where.
    Returns the port, or None (the run ended first, ARTEX did not start, or no port was free)."""
    l = l or launcher()
    out = out or (lambda text: print(f"agent-sandbox: {text}", file=sys.stderr))
    while True:
        state = started(a.control_dir, l)
        if state is not None:
            break
        if not alive():
            return None
        sleep(0.2)
    if not state:
        return None
    try:
        listener = bind(getattr(a, "artex_port", None), make=make)
    except OSError as exc:
        out(f"artex: the console is not forwarded: {exc}; start again with --artex-port PORT")
        return None
    port = listener.getsockname()[1]
    container = l.load_services_module().container_name(pod, a.artex_container)
    url = f"http://127.0.0.1:{port}"
    write_console(console_dir(a, l), url, a.artex_password, container, port)
    out(f"artex: the console is at {url} (user {CONSOLE_USER}; the password: sandbox-ai artex console)")
    argv = exec_command(a, container, l)
    listener.settimeout(0.5)
    try:
        while alive():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            connection.settimeout(None)
            threading.Thread(target=relay, args=(connection, argv, popen), daemon=True).start()
    finally:
        listener.close()
    return port


# -- sandbox-ai artex console --

def consoles(runs=None):
    """[{"run", "name", "url", "user", "password", "container"}] of the running starts with ARTEX's console
    forwarded (their run.json and their ARTEX folder's console.json)."""
    runs = Path(runs) if runs is not None else launcher().runs_dir()
    found = []
    for record_path in sorted(runs.glob("*/run.json")):
        try:
            record = json.loads(record_path.read_text())
            entry = json.loads((record_path.parent.with_name(record_path.parent.name + ".artex") /
                                CONSOLE_FILE).read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or not record.get("artex") or not isinstance(entry, dict):
            continue
        found.append({"run": record_path.parent.name, "name": record.get("name") or record_path.parent.name,
                      **{key: entry.get(key) for key in ("url", "user", "password", "container")}})
    return found


def console_command(argv, out=print, runs=None):
    import argparse
    parser = argparse.ArgumentParser(prog="sandbox-ai artex", description="ARTEX beside an agent (--artex): its "
                                     "console's address, user and password")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("console", help="The console of a running ARTEX: its address, user and password")
    show.add_argument("run", nargs="?", metavar="RUN", help="the start (its name or run); default: the only one")
    show.add_argument("--json", action="store_true", help="as JSON")
    args = parser.parse_args(argv)
    found = consoles(runs)
    if args.run:
        found = [entry for entry in found if args.run in (entry["run"], entry["name"])]
    if not found:
        print("sandbox-ai artex: no running start with ARTEX's console" + (f" named {args.run}" if args.run else "")
              + " (start one with --artex)", file=sys.stderr)
        return 1
    if len(found) > 1 and not args.run and not args.json:
        print("sandbox-ai artex: several starts run ARTEX; name one: " + ", ".join(e["name"] for e in found),
              file=sys.stderr)
        return 2
    if args.json:
        out(json.dumps(found if len(found) > 1 or not args.run else found[0], indent=2))
        return 0
    entry = found[0]
    out(f"{entry['name']}: {entry['url']}\n  user:     {entry['user']}\n  password: {entry['password']}")
    return 0


def main(argv=None):
    return console_command(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
