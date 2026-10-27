#!/usr/bin/env python3
"""Rootless agent launchers; standard library only. See SANDBOX.md."""
import argparse
import base64
import ctypes
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
PROVIDERS = ["api.openai.com", "chatgpt.com", "auth.openai.com",
             "api.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"]
AUTH_TARGETS = {"codex": ".codex/auth.json", "claude": ".claude/.credentials.json",
                "opencode": ".local/share/opencode/auth.json"}
CAPS = ["SYS_ADMIN", "NET_ADMIN", "SETUID", "SETGID", "SYS_CHROOT",
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETPCAP"]
POD_NETWORK = "pasta:--no-map-gw,-t,none,-u,none,-T,none,-U,none"


def parse_args(argv):
    # The separator belongs to this wrapper; everything afterwards is opaque.
    tail = []
    if "--" in argv:
        pos = argv.index("--")
        argv, tail = argv[:pos], argv[pos + 1:]
    p = argparse.ArgumentParser(description="Agent in rootless Podman/Bubblewrap + verified Firejail.",
                                epilog="Agent arguments follow --. Examples: see SANDBOX.md.")
    p.add_argument("agent", choices=AUTH_TARGETS)
    p.add_argument("--backend", choices=["podman", "bwrap"], default="podman")
    p.add_argument("--workspace", default=os.getcwd(), help="Host directory at /work (default: current directory)")
    p.add_argument("--workspace-ro", action="store_true")
    p.add_argument("--mount", action="append", default=[], metavar="SRC:DEST:ro|rw", help="Repeatable; DEST under /work, /mnt or /opt")
    p.add_argument("--allow", action="append", default=[], metavar="IP_OR_CIDR", help="Allow destination on all IP protocols/ports; repeatable")
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST", help="Pin hostname and allow TCP/443; repeatable")
    p.add_argument("--no-provider-defaults", action="store_true")
    p.add_argument("--local-config", type=Path, help="OpenCode local-only config: copy privately and disable default cloud access")
    p.add_argument("--offline", action="store_true", help="No outside network, including provider defaults")
    p.add_argument("--network", help="Existing Podman network (default: private rootless pasta)")
    p.add_argument("--ip", help="Static IPv4 address in --network")
    p.add_argument("--ip6", help="Static IPv6 address in --network")
    p.add_argument("--image", default="localhost/agent-sandbox:latest", help="Prebuilt Podman image; never pulled implicitly")
    p.add_argument("--podman-url", help="Podman service URL, e.g. unix:///example/podman.sock")
    p.add_argument("--binary", help="Agent executable (host path with bwrap; image path with podman)")
    p.add_argument("--runtime", action="append", default=[], metavar="DIR", help="bwrap: expose runtime directory read-only at same path")
    p.add_argument("--copy-auth", action="store_true", help="Copy this agent's existing JSON credential files")
    p.add_argument("--no-auth", action="store_true", help="Disable automatic copying of host credentials")
    p.add_argument("--auth-file", type=Path, help="Copy this JSON file to agent's standard auth path")
    p.add_argument("--import-codex-auth", type=Path, help="OpenCode only: import a Codex ChatGPT auth.json as a private OAuth copy")
    p.add_argument("--copy", action="append", default=[], metavar="SRC:HOME_RELATIVE_DEST", help="Copy individual config/credential file; repeatable")
    p.add_argument("--state-dir", type=Path, help="Private persistent sandbox home; default: deleted after exit")
    p.add_argument("--env", action="append", default=[], metavar="NAME", help="Pass named host variable to agent only; values never printed")
    p.add_argument("--dry-run", action="store_true", help="Print plan; no DNS, auth reads, copies, pods or processes")
    p.add_argument("--probe", action="store_true", help="Verify both sandbox layers and firewall, then exit without agent")
    a = p.parse_args(argv)
    a.agent_args = tail
    return a


def allowed_hosts(a):
    if a.offline:
        return []
    defaults = PROVIDERS + (["opencode.ai"] if a.agent == "opencode" else [])
    return list(dict.fromkeys(([] if a.no_provider_defaults or a.local_config else defaults) + a.allow_host))


def safe_dest(dest):
    path = PurePosixPath(dest)
    if not path.is_absolute() or ".." in path.parts or str(path) != dest:
        raise ValueError(f"Non-canonical mount destination: {dest}")
    if not any(path == PurePosixPath(base) or PurePosixPath(base) in path.parents
               for base in ("/work", "/mnt", "/opt")):
        raise ValueError("Mount destinations must be under /work, /mnt or /opt")
    return dest


def parse_mount(value):
    try:
        source, dest, mode = value.rsplit(":", 2)
    except ValueError:
        raise ValueError("Mount syntax: SRC:DEST:ro|rw") from None
    try:
        source = Path(source).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Mount source unavailable: {exc}") from None
    if mode not in ("ro", "rw") or not (source.is_dir() or source.is_file()):
        raise ValueError("Mount must be a regular file/directory with ro or rw mode")
    # Socket files are never accepted as individual mounts. Directory contents
    # remain the operator's responsibility (see documentation).
    safe_dest(dest)
    if any(c in str(source) + dest for c in (",", "\n", "\r", "\0")):
        raise ValueError("Mount paths cannot contain commas or control characters")
    return str(source), dest, mode


def validate(a):
    if a.offline and (a.allow or a.allow_host or a.network or a.ip or a.ip6):
        raise ValueError("--offline conflicts with network permissions/options")
    if a.local_config and (a.agent != "opencode" or not a.local_config.expanduser().is_file()):
        raise ValueError("--local-config requires OpenCode and an existing configuration file")
    for value in a.allow:
        ipaddress.ip_network(value, strict=False)
    for host in a.allow_host:
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split(".")):
            raise ValueError(f"Invalid hostname: {host!r}")
    if a.network and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", a.network):
        raise ValueError("--network must name an existing private Podman network")
    if a.network in ("host", "none", "private", "bridge", "default", "slirp4netns", "pasta"):
        raise ValueError("Use a named Podman network, not a special network mode")
    if (a.ip or a.ip6) and not a.network:
        raise ValueError("Static IP requires --network")
    for value, version in [(a.ip, 4), (a.ip6, 6)]:
        if value and ipaddress.ip_address(value).version != version:
            raise ValueError(f"Expected IPv{version} address")
    if a.backend == "bwrap" and (a.network or a.ip or a.ip6 or a.podman_url):
        raise ValueError("--network/--ip/--ip6/--podman-url apply only to Podman")
    if a.backend == "podman" and a.runtime:
        raise ValueError("--runtime applies only to bwrap; install runtimes in the image")
    if sum(bool(value) for value in (a.copy_auth, a.auth_file, a.import_codex_auth, a.no_auth)) > 1:
        raise ValueError("Choose one of --copy-auth, --auth-file, --import-codex-auth or --no-auth")
    if a.import_codex_auth and a.agent != "opencode":
        raise ValueError("--import-codex-auth is supported only for OpenCode")
    for name in a.env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("--env expects a variable NAME, without a value")
        if name not in os.environ:
            raise ValueError(f"Host variable is not set: {name}")
        if name in ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
    for mount in a.mount:
        parse_mount(mount)
    source = Path(a.workspace).expanduser().absolute()
    if any(path.is_symlink() and not path.exists() for path in (source, *source.parents)):
        raise ValueError("Workspace path contains a dangling symlink")
    workspace = source.resolve(strict=False)
    if (workspace.exists() and not workspace.is_dir()) or any(c in str(workspace) for c in (",", "\n", "\r")):
        raise ValueError("Workspace must be a directory without commas/control characters")
    a.workspace = str(workspace)
    if any(parse_mount(m)[1] == "/work" for m in a.mount):
        raise ValueError("Use --workspace to mount /work")
    bindings = [(workspace, PurePosixPath("/work"))]
    for value in a.mount:
        src, dest, _ = parse_mount(value)
        dest = PurePosixPath(dest)
        # A later parent bind hides earlier child mounts. Inspect the tree
        # visible at this step, rather than the longest historic destination.
        for host, mounted in reversed(bindings):
            if mounted == dest or mounted in dest.parents:
                current = host
                for part in dest.relative_to(mounted).parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError(f"Mount destination traverses a symlink: {dest}")
                break
        bindings.append((Path(src), dest))


def prepare_workspace(path):
    workspace = Path(path)
    try:
        workspace.mkdir(parents=True)
    except FileExistsError:
        if not workspace.is_dir():
            raise ValueError(f"Workspace path exists but is not a directory: {workspace}") from None
        print(f"agent-sandbox: Warnung: Workspace existiert bereits; verwende vorhandenes Verzeichnis: {workspace}", file=sys.stderr)


def resolve_hosts(hosts):
    result = {}
    for host in hosts:
        try:
            addresses = sorted({str(ipaddress.ip_address(row[4][0])) for row in
                                socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot resolve allowed host {host}: {exc}") from None
        if not addresses:
            raise ValueError(f"No addresses for allowed host: {host}")
        result[host] = addresses
    return result


def hosts_file(hosts):
    return "127.0.0.1 localhost\n::1 localhost\n" + "".join(
        f"{addr} {name}\n" for name, addresses in hosts.items() for addr in addresses)


def podman_command(a, *args):
    return ["podman", *(["--remote", "--url", a.podman_url] if a.podman_url else []), *args]


def resolve_pod_hosts(a, hosts):
    if not hosts:
        return {}
    # DNS belongs to the selected engine's network, which can differ from the
    # launcher's namespace. No credentials or workspace enter this resolver.
    script = "import json,socket,sys; print(json.dumps({h:sorted({r[4][0] for r in socket.getaddrinfo(h,443,type=socket.SOCK_STREAM)}) for h in json.loads(sys.argv[1])}))"
    cmd = podman_command(a, "run", "--rm", "--pull=never", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--network",
                         a.network or POD_NETWORK,
                         "--entrypoint=/usr/bin/python3", a.image, "-I", "-c", script, json.dumps(hosts))
    result = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=45)
    answer = json.loads(result.stdout)
    if not isinstance(answer, dict) or set(answer) != set(hosts):
        raise ValueError("Podman DNS returned an incomplete host list")
    for host in hosts:
        if not isinstance(answer[host], list) or not answer[host]:
            raise ValueError(f"Podman DNS returned no addresses for {host}")
        answer[host] = sorted({str(ipaddress.ip_address(addr)) for addr in answer[host]})
    return answer


def firewall_rules(networks, addresses):
    lines = ["table inet agent_sandbox {",
             " chain input { type filter hook input priority 0; policy drop;",
             '  iifname "lo" accept', "  ct state established,related accept",
             "  ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept", " }",
             " chain forward { type filter hook forward priority 0; policy drop; }",
             " chain output { type filter hook output priority 0; policy drop;",
             '  oifname "lo" accept', "  ct state established,related accept",
             "  ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept"]
    for raw in networks:
        net = ipaddress.ip_network(raw, strict=False)
        lines.append(f"  {'ip' if net.version == 4 else 'ip6'} daddr {net} accept")
    for raw in sorted(set(addresses)):
        addr = ipaddress.ip_address(raw)
        lines.append(f"  {'ip' if addr.version == 4 else 'ip6'} daddr {addr} tcp dport 443 accept")
    return "\n".join(lines + [" }", "}", ""])


def copy_private(source, home, relative):
    source = Path(source).expanduser().resolve(strict=True)
    relative = PurePosixPath(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Copy destination must be a relative path inside sandbox home")
    target = home.joinpath(*relative.parts)
    for parent in [target, *target.parents]:
        if parent == home:
            break
        if parent.is_symlink():
            raise ValueError("Copy destination contains a symlink")
    if target.exists():
        raise ValueError(f"Refusing to overwrite existing sandbox file: {relative}; omit --copy-auth/--copy when reusing state")
    if not source.is_file():
        raise ValueError("Copy source must be a regular file")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent
    while parent != home:
        parent.chmod(0o700)
        parent = parent.parent
    with source.open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        shutil.copyfileobj(src, dst)


def auth_sources(agent):
    home = Path.home()
    if agent == "codex":
        return [(Path(os.environ.get("CODEX_HOME", home / ".codex")) / "auth.json", AUTH_TARGETS[agent])]
    if agent == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        # A custom config directory can contain .claude.json; the default
        # HOME-based installation stores it next to ~/.claude instead.
        metadata = config / ".claude.json"
        if not metadata.is_file():
            metadata = home / ".claude.json"
        return [(config / ".credentials.json", AUTH_TARGETS[agent]),
                (metadata, ".claude.json")]
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    return [(data / "opencode/auth.json", AUTH_TARGETS[agent])]


def auth_mode(a):
    if a.no_auth:
        return "none"
    if a.copy_auth:
        return "copy"
    if a.auth_file:
        return "file"
    if a.import_codex_auth:
        return "codex_import"
    targets = {AUTH_TARGETS[a.agent]}
    if a.agent == "claude":
        targets.add(".claude.json")
    if any(":" in value and str(PurePosixPath(value.rsplit(":", 1)[1])) in targets for value in a.copy):
        return "manual"
    return "auto"


def prepare_home(a, home):
    copies = []
    if a.copy_auth:
        sources = auth_sources(a.agent)
        if not sources[0][0].is_file():
            raise ValueError(f"Auth JSON missing: {sources[0][0]}; use --auth-file or file-based login first")
        copies.extend((src, dest) for src, dest in sources if src.is_file())
    if a.auth_file:
        copies.append((a.auth_file, AUTH_TARGETS[a.agent]))
    for value in a.copy:
        if ":" not in value:
            raise ValueError("Copy syntax: SRC:HOME_RELATIVE_DEST")
        src, dest = value.rsplit(":", 1)
        copies.append((Path(src), dest))
    for source, dest in copies:
        copy_private(source, home, dest)
    if a.local_config:
        source = a.local_config.expanduser()
        dest = ".config/opencode/opencode.jsonc"
        target = home / dest
        if not target.exists() or target.read_bytes() != source.read_bytes():
            copy_private(source, home, dest)
    if a.import_codex_auth:
        imported = opencode_auth_from_codex(a.import_codex_auth)
        with tempfile.TemporaryDirectory(prefix="agent-auth-import-") as directory:
            source = Path(directory) / "auth.json"
            source.write_text(json.dumps(imported))
            source.chmod(0o600)
            copy_private(source, home, AUTH_TARGETS["opencode"])
    if auth_mode(a) == "auto":
        for source, dest in auth_sources(a.agent):
            target = home / dest
            if target.is_symlink():
                raise ValueError(f"Auth destination is a symlink: {dest}")
            if source.is_file() and not target.exists():
                copy_private(source, home, dest)
    if a.agent == "opencode":
        catalog = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "opencode/models.json"
        target = home / ".cache/opencode/models.json"
        if catalog.is_file() and not target.exists():
            copy_private(catalog, home, ".cache/opencode/models.json")
    for name in [".codex", ".claude", ".config", ".local/share", ".cache"]:
        (home / name).mkdir(mode=0o700, parents=True, exist_ok=True)


def opencode_auth_from_codex(source):
    try:
        tokens = json.loads(Path(source).expanduser().read_text())["tokens"]
        access, refresh, account = (tokens[key] for key in ("access_token", "refresh_token", "account_id"))
        if not all(isinstance(value, str) and value for value in (access, refresh, account)):
            raise ValueError()
        payload = access.split(".")[1]
        expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
            raise ValueError()
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ValueError("Codex auth must contain ChatGPT access/refresh tokens, account_id and a JWT expiry") from None
    return {"openai": {"type": "oauth", "access": access, "refresh": refresh,
                       "expires": expires * 1000, "accountId": account}}


def namespace_state():
    state = {name: os.readlink(f"/proc/self/ns/{name}") for name in ["mnt", "pid"]}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb", "NoNewPrivs", "Seccomp"):
            state[key] = value.strip()
    return state


def verify_inner(before, current):
    if any(before[key] == current[key] for key in ("mnt", "pid")):
        raise RuntimeError("Firejail did not create both mount and PID namespaces; refusing unconfined agent")
    if any(int(current[key], 16) for key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")):
        masks = ", ".join(f"{key}={value}" for key, value in current.items() if key.startswith("Cap"))
        raise RuntimeError(f"Firejail left Linux capabilities enabled: {masks}")
    if current["NoNewPrivs"] != "1" or current["Seccomp"] != "2":
        raise RuntimeError("Firejail did not enable no-new-privileges and seccomp")


def wait_child(child):
    previous = {}
    def forward(sig, _frame):
        if child.poll() is None:
            child.send_signal(sig)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, forward)
    try:
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def inner_bootstrap():
    # Firejail 0.9.80's check_kernel_procs() skips confinement when none of
    # /proc/{1..10}/comm begins with a kernel-worker name. In a fresh PID
    # namespace our PID-1 supervisor deliberately supplies this name. This only
    # bypasses that heuristic: the guard below verifies real confinement.
    if os.getpid() != 1:
        raise RuntimeError("Bootstrap must be PID 1 in a fresh outer PID namespace")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(15, ctypes.c_char_p(b"kthreadd"), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot name namespace supervisor")
    subprocess.run(["nft", "-f", "/sandbox/session/firewall.nft"], check=True)
    # Installation failure aborts before Firejail or the agent starts.
    subprocess.run(["nft", "list", "table", "inet", "agent_sandbox"], check=True, stdout=subprocess.DEVNULL)
    before = namespace_state()
    cmd = firejail_command(before)
    return wait_child(subprocess.Popen(cmd, env=base_env(), close_fds=True))


def firejail_command(before):
    help_text = subprocess.run(["firejail", "--help"], check=True, text=True, capture_output=True).stdout
    command = ["firejail", "--noprofile", "--quiet", "--caps.drop=all", "--nonewprivs", "--seccomp"]
    # New releases always exec directly and reject the former shell option.
    if "--shell=" in help_text:
        command.append("--shell=none")
    return ["setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--", *command,
            "/usr/bin/python3", "-I", "/sandbox/agent_sandbox.py", "--internal-guard", json.dumps(before)]


def base_env():
    return {"HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash",
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
            "TERM": os.environ.get("TERM", "xterm-256color"), "LANG": "C.UTF-8",
            "CODEX_HOME": "/root/.codex",
            "XDG_CONFIG_HOME": "/root/.config", "XDG_DATA_HOME": "/root/.local/share",
            "XDG_CACHE_HOME": "/root/.cache"}


def agent_environment(a):
    env = {key: os.environ[key] for key in a.env}
    if a.local_config:
        env.update({"OPENCODE_CONFIG": "/root/.config/opencode/opencode.jsonc",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1"})
    return env


def agent_command(a, binary):
    args = list(a.agent_args)
    # Codex's native sandbox cannot create its UID map inside the verified
    # outer namespaces. Use the external boundary unless explicitly overridden.
    if a.agent == "codex" and a.binary is None and not a.probe:
        options = args[:args.index("--")] if "--" in args else args
        explicit = any(value in ("--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox")
                       or value.startswith("--sandbox=") or value.startswith("-s") and len(value) > 2
                       for value in options)
        if not explicit:
            args = ["--sandbox", "danger-full-access", *args]
    return [binary, *args]


def inner_guard(before):
    current = namespace_state()
    verify_inner(before, current)
    cfg = json.loads(Path("/sandbox/session/config.json").read_text())
    if cfg["probe"]:
        print(json.dumps({"verified": True, "firejail": current, "outer": before}), flush=True)
        return 0
    env = base_env()
    env.update(cfg["env"])
    cmd = cfg["command"]
    os.chdir("/work")
    os.execvpe(cmd[0], cmd, env)


def mount_option(src, dest, mode):
    if any(c in str(src) + dest for c in (",", "\n", "\r")):
        raise ValueError("Podman bind paths cannot contain commas or newlines")
    return ["--mount", f"type=bind,src={src},dst={dest},{'ro' if mode == 'ro' else 'rw'}"]


def pod_commands(a, name, session, home):
    create = podman_command(a, "pod", "create", "--name", name,
              "--infra-image", a.image, "--infra-command", "/usr/bin/sleep infinity",
              "--userns=keep-id:uid=0,gid=0", "--share=net,ipc,uts",
              "--network", "none" if a.offline else (a.network or POD_NETWORK))
    if a.ip:
        create += ["--ip", a.ip]
    if a.ip6:
        create += ["--ip6", a.ip6]
    run = podman_command(a, "run", "--rm", "--pull=never", "--pod", name, "--user=0:0",
           "--read-only", "--cap-drop=ALL", "--security-opt=seccomp=unconfined",
           "--security-opt=apparmor=unconfined", "--security-opt=label=disable",
           "--tmpfs=/run:rw,nosuid,nodev", "--tmpfs=/tmp:rw,nosuid,nodev",
           "--tmpfs=/var/tmp:rw,nosuid,nodev", "--workdir=/work", "--interactive")
    for cap in CAPS:
        run += ["--cap-add", cap]
    if sys.stdin.isatty() and sys.stdout.isatty():
        run += ["--tty"]
    bindings = [(HERE / "agent_sandbox.py", "/sandbox/agent_sandbox.py", "ro"),
                (session, "/sandbox/session", "ro"), (home, "/root", "rw"),
                (Path(a.workspace).expanduser().resolve(), "/work", "ro" if a.workspace_ro else "rw"),
                (session / "hosts", "/etc/hosts", "ro"),
                (session / "resolv.conf", "/etc/resolv.conf", "ro"),
                (session / "nsswitch.conf", "/etc/nsswitch.conf", "ro")]
    bindings += [parse_mount(value) for value in a.mount]
    for src, dest, mode in bindings:
        run += mount_option(src, dest, mode)
    for key, value in base_env().items():
        run += ["--env", f"{key}={value}"]
    run += ["--entrypoint=/usr/bin/python3", a.image, "-I", "/sandbox/agent_sandbox.py", "--internal-bootstrap"]
    return create, run


def run_pod(a, session, home):
    name = "agent-" + a.agent + "-" + uuid.uuid4().hex[:12]
    create, run = pod_commands(a, name, session, home)
    # Check local image first, before creating an owned pod. Never mutate a
    # pre-existing pod, use host networking, or implicitly fetch images.
    subprocess.run(podman_command(a, "image", "exists", a.image), check=True)
    subprocess.run(create, check=True, stdout=subprocess.DEVNULL)
    try:
        return wait_child(subprocess.Popen(run))
    finally:
        subprocess.run(podman_command(a, "pod", "rm", "--force", name), check=False, stdout=subprocess.DEVNULL)


def bwrap_command(a, session, home):
    cmd = ["bwrap", "--unshare-all", "--unshare-user", "--uid", "0", "--gid", "0", "--cap-add", "ALL",
           "--as-pid-1", "--die-with-parent", "--clearenv", "--hostname", "agent-sandbox",
           "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        p = Path(path)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), path]
        elif p.exists():
            cmd += ["--ro-bind", path, path]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--dir", "/var/tmp", "--dir", "/etc"]
    for path in ("/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/firejail", "/etc/localtime", "/etc/ld.so.cache", "/etc/login.defs"):
        if Path(path).exists():
            cmd += ["--ro-bind", path, path]
    for name in ("hosts", "resolv.conf", "nsswitch.conf", "passwd", "group"):
        cmd += ["--ro-bind", str(session / name), "/etc/" + name]
    cmd += ["--ro-bind", str(HERE / "agent_sandbox.py"), "/sandbox/agent_sandbox.py",
            "--ro-bind", str(session), "/sandbox/session", "--bind", str(home), "/root",
            "--ro-bind" if a.workspace_ro else "--bind", str(Path(a.workspace).expanduser().resolve()), "/work"]
    for value in a.mount:
        src, dest, mode = parse_mount(value)
        cmd += ["--ro-bind" if mode == "ro" else "--bind", src, dest]
    for directory in a.runtime:
        path = Path(directory).expanduser().resolve(strict=True)
        if not path.is_dir() or not any(base in path.parents for base in
                                        map(Path, ("/usr", "/opt", "/home", "/mnt", "/srv"))):
            raise ValueError("Invalid --runtime directory")
        cmd += ["--ro-bind", str(path), str(path)]
    for key, value in base_env().items():
        cmd += ["--setenv", key, value]
    return cmd + ["--chdir", "/work"]


def read_ready(fd, process, timeout=15):
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.1)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError(f"Sandbox setup exited early (status {process.poll()})")
            data += chunk
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                if data == b"1":
                    return True
        if process.poll() is not None:
            raise RuntimeError(f"Sandbox setup failed with status {process.returncode}")
    raise RuntimeError("Timed out waiting for namespace/network setup")


def stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def subordinate_range(kind):
    username = pwd.getpwuid(os.getuid()).pw_name
    for line in Path("/etc/sub" + kind).read_text().splitlines():
        fields = line.split(":")
        if len(fields) != 3 or fields[0] not in (username, str(os.getuid())):
            continue
        start, count = int(fields[1]), int(fields[2])
        if start > 0 and count >= 65536:
            return start, count
    raise ValueError(f"Bubblewrap + Firejail needs a delegated range of at least 65536 IDs in /etc/sub{kind}")


def configure_user_maps(pid):
    # Unlike bwrap's single-ID self-mapping (which permanently denies
    # setgroups), newgidmap permits setgroups when a delegated subgid range
    # is included. Firejail's drop_privs() requires this even as namespace root.
    for kind, real in [("uid", os.getuid()), ("gid", os.getgid())]:
        start, count = subordinate_range(kind)
        subprocess.run(["new" + kind + "map", str(pid), "0", str(real), "1",
                        "1", str(start), str(count)], check=True)
    if Path(f"/proc/{pid}/setgroups").read_text().strip() != "allow":
        raise RuntimeError("User namespace denies setgroups, which Firejail requires")


def run_bwrap(a, session, home):
    cmd = bwrap_command(a, session, home)
    info_r = info_w = maps_r = maps_w = -1
    gate_r = gate_w = ready_r = ready_w = -1
    child = slirp = None
    try:
        # Allocation itself can fail. Keep every successfully allocated pipe
        # inside the cleanup scope, even before the first process exists.
        info_r, info_w = os.pipe()
        maps_r, maps_w = os.pipe()
        gate_r, gate_w = os.pipe()
        ready_r, ready_w = os.pipe()
        child = subprocess.Popen(cmd + ["--info-fd", str(info_w), "--userns-block-fd", str(maps_r), "--block-fd", str(gate_r),
                                       "/usr/bin/python3", "-I", "/sandbox/agent_sandbox.py", "--internal-bootstrap"],
                                 pass_fds=(info_w, maps_r, gate_r))
        os.close(info_w)
        info_w = -1
        os.close(gate_r)
        gate_r = -1
        os.close(maps_r)
        maps_r = -1
        pid = int(read_ready(info_r, child)["child-pid"])
        configure_user_maps(pid)
        os.write(maps_w, b"1")
        os.close(maps_w)
        maps_w = -1
        if not a.offline:
            slirp = subprocess.Popen(["slirp4netns", "--configure", "--disable-host-loopback",
                                      "--enable-sandbox", "--enable-seccomp", "--enable-ipv6",
                                      "--ready-fd", str(ready_w), str(pid), "tap0"], pass_fds=(ready_w,),
                                     stdout=subprocess.DEVNULL)
            os.close(ready_w)
            ready_w = -1
            read_ready(ready_r, slirp)
        os.write(gate_w, b"1")
        os.close(gate_w)
        gate_w = -1
        return wait_child(child)
    finally:
        stop_process(child)
        stop_process(slirp)
        for fd in (info_r, info_w, maps_r, maps_w, gate_r, gate_w, ready_r, ready_w):
            if fd >= 0:
                os.close(fd)


def main(argv):
    a = parse_args(argv)
    validate(a)
    if a.dry_run:
        print(json.dumps({"agent": a.agent, "backend": a.backend, "workspace": str(Path(a.workspace).resolve()),
                          "workspace_mode": "ro" if a.workspace_ro else "rw", "mounts": a.mount,
                          "allow": a.allow, "https_hosts": allowed_hosts(a), "offline": a.offline,
                          "network": a.network, "ip": a.ip, "ip6": a.ip6, "image": a.image,
                          "podman_url": a.podman_url,
                          "local_config": str(a.local_config) if a.local_config else None,
                          "copy_auth": a.copy_auth, "auth_file": str(a.auth_file) if a.auth_file else None,
                          "auth_mode": auth_mode(a),
                          "import_codex_auth": str(a.import_codex_auth) if a.import_codex_auth else None,
                          "copies": a.copy, "env_names": a.env, "agent_args": a.agent_args,
                          "firejail_required": True}, indent=2))
        return 0
    if os.geteuid() == 0:
        raise ValueError("Run as an ordinary host user; both backends use a rootless user namespace")
    required = ["podman"] if a.backend == "podman" else ["bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap"] + ([] if a.offline else ["slirp4netns"])
    for tool in required:
        if not shutil.which(tool):
            raise ValueError(f"Required tool missing: {tool}")
    binary = a.binary or a.agent
    if a.backend == "bwrap" and not a.probe:
        found = shutil.which(binary)
        if not found:
            raise ValueError(f"Agent executable missing: {binary}")
        binary = str(Path(found).resolve(strict=True))
        if not binary.startswith("/usr/"):
            a.runtime.append(str(Path(binary).parent))
    prepare_workspace(a.workspace)
    hosts = resolve_pod_hosts(a, allowed_hosts(a)) if a.backend == "podman" else resolve_hosts(allowed_hosts(a))
    with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as tmp:
        session = Path(tmp)
        home = a.state_dir.expanduser().absolute() if a.state_dir else session / "home"
        if home.is_symlink():
            raise ValueError("--state-dir must not be a symlink")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if home.stat().st_uid != os.getuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
            raise ValueError("--state-dir must be owned by you and have mode 0700")
        prepare_home(a, home)
        cfg = {"command": agent_command(a, binary), "env": agent_environment(a), "probe": a.probe}
        files = {"config.json": json.dumps(cfg), "hosts": hosts_file(hosts), "resolv.conf": "# DNS is pinned in /etc/hosts; no DNS egress.\n",
                 "nsswitch.conf": "passwd: files\ngroup: files\nhosts: files\n",
                 "passwd": "root:x:0:0:Sandbox:/root:/bin/bash\n", "group": "root:x:0:\n",
                 "firewall.nft": firewall_rules(a.allow, [addr for values in hosts.values() for addr in values])}
        for name, content in files.items():
            path = session / name
            path.write_text(content)
            path.chmod(0o600 if name == "config.json" else 0o644)
        return run_pod(a, session, home) if a.backend == "podman" else run_bwrap(a, session, home)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--internal-bootstrap":
            result = inner_bootstrap()
        elif len(sys.argv) > 2 and sys.argv[1] == "--internal-guard":
            result = inner_guard(json.loads(sys.argv[2]))
        else:
            result = main(sys.argv[1:])
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
