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

  {"NAME": {"image": "ghcr.io/...@sha256:...", "network": "none" | "full" | "allowlist" | "lan",
            "hosts": ["NAME:PORT"], "allow_private": ["CIDR"], "allow_tcp": ["ADDRESS:PORT"],
            "mounts": [{"target": "/work", "host": "/abs/path", "mode": "ro"}],
            "env": ["VARIABLE"], "memory": "512m", "cpus": 2, "pids": 128, "description": "...",
            "source": "...", "vpn": "PROFILE",
            "env_optional": ["VARIABLE"], "env_values": {"FLAG": "1"}, "env_secrets": {"VARIABLE": "SECRET"}}}

Its variables (see "variables" below): "env" required (your environment's, else the secret of that name),
"env_optional" passed where there is one, "env_values" fixed values that are no secret, "env_secrets" from the
secret named; `mcp add/set --env NAME`, `--env NAME=VALUE`, `--env NAME@SECRET`, a start's own: --mcp-env.

Such a server runs per connection as `podman run --rm -i` with no capabilities, a read-only root
and only the network and folders the definition names; agents reach it the same way. A network is the internet, never
your local network ("full"), only the hosts of its catalog entry ("allowlist"), or your network too ("lan", your
choice): nftables rules in its network namespace, loaded before it runs (mcp_egress.py). With "vpn" (a server with
network only) its containers join the network namespace of a VPN container (sandbox-ai vpn), whose kill switch
lets nothing out but through the tunnel; a server with a command takes --vpn PROFILE in its options. Every
server's network is decided in server_network and proven after its start (verify_network; each connection of an
image server by vpn.py's guard): one that would reach the network outside the VPN meant for it does not run.
Behind its start's profile that runs in one sandbox at a time (a WireGuard key, Mullvad's too, a persistent
Tailscale node) a server brings up no second tunnel of the key: its VPN container borrows the start's tunnel
(LentVpn, vpn.lends), as an instance of that run alone, stopped when the run ends (stop_lent).

The relay in front of every server is a gateway: the MCP rules of the starting agent's rulesets allow,
ask or deny each tool call (runtime.py, McpGateway). Its records and questions reach the server's state
directory through the log reader on the host (`mcp.py --internal-log DIR`): mcp-calls.jsonl and
approvals/, which `sandbox-ai approvals` lists as the run mcp-NAME.

Limits: the definition's memory, cpus and pids hold for every start (an image server's: 512m, half this host's CPUs and
128 unless it says otherwise, for each connection's container; a command server's: its options' --memory, --cpus and
--pids, else the launcher's defaults). `sandbox-ai mcp add` takes the limits the catalog entry recommends ("limits", in
the entry or in rootfs.json beside it), `sandbox-ai mcp set NAME --memory 2g` changes them. One start may ask for other
limits: --mcp-limits NAME:memory=2g,cpus=2,pids=512 (the board writes it for an MCP node whose limits were changed). A
running server has one set of limits, so a start whose limits differ from the defaults gets an instance of its own,
NAME.HASH (HASH: of its limits) with its own state directory, socket, pod and supervisor; the starts with the same
limits share it, as the starts with the defaults share NAME. `mcp list` shows the instances and their limits, `mcp stop
NAME` stops all of them (NAME.HASH: that one). A start's own folders (--mcp-mount) and variables (--mcp-env) give
instances the same way, and so does a server of the start's name that runs with another definition (another
project's, one changed by mcp set, other MCP rules): the start gets an instance of its own (start_instance), the one
that runs is left as it is. Everything a start's servers need is checked before anything is made (start_problems).

`mcp attach NAME START` (and `mcp start NAME --for START`, which starts the server as that start starts it)
connects a server to an agent start that exists: --mcp NAME joins its kept options, checked as its start checks
it. A running start is restarted with it (--restart), gets it at its next start, or takes it while it runs
(--live: the run's supervisor opens a relay for it over its control socket; see attach_live and LIVE_AGENTS for what
each agent makes of it). `mcp detach NAME START [--live]` takes it out again (with --live: its relay closes now);
--helper AGENT in place of NAME does the same for a helper. `mcp list` names the starts each server serves.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
MCP_NAME_PATTERN = r"[A-Za-z0-9_-]{1,32}"
PROC = Path("/proc")
READY_TIMEOUT = 120
KEYS = {"command", "options", "agent", "workspace", "description", "tool_timeout"}
IMAGE_KEYS = {"image", "network", "mounts", "env", "memory", "cpus", "pids", "tmp", "description", "source", "vpn",
              "signed_by", "rootfs", "catalog", "env_optional", "env_values", "env_secrets", "tool_timeout", "pid1",
              "hosts", "allow_private", "allow_tcp", "built", "tmp_exec"}
# "built": an image built on this host from a catalog entry's reviewed source (mcp add --build: mcp_build.py), never
# signed - {"commit", "repository", "path", "archive_sha256", "at"}; its "signed_by" is null.
BUILT_KEYS = ("commit", "repository", "path", "archive_sha256", "at")
# An image server's network (mcp_egress.py: what each one reaches, enforced in its network namespace):
#   none       nothing
#   full       the internet, never your local network nor this host (private, carrier-grade NAT, loopback,
#              link-local with the clouds' metadata service, IPv6 unique-local and link-local, multicast)
#   allowlist  only "hosts" (NAME:PORT, its catalog entry's), pinned when each connection begins; no DNS of its own
#   lan        the internet and your local network (a choice of yours, said with a warning); never the metadata service
# "allow_private" (networks) and "allow_tcp" (ADDRESS:PORT) open a part of your network to a full or allowlist server.
NETWORKS = ("none", "full", "allowlist", "lan")
NETWORK_LABELS = {"none": "none", "full": "internet (no LAN)", "allowlist": "only its hosts", "lan": "internet + LAN"}
EGRESS_LIST_LIMIT = 32  # networks or addresses a server's allow_private or allow_tcp names
HOSTS_LIMIT = 64
# "pid1": "server" - the server's own process is PID 1 of its container (Podman without --init) or Bubblewrap sandbox
# (bwrap --as-pid-1) and reaps its children itself. By default catatonit (Podman's --init) or bwrap's reaper is PID 1
# and holds the MCP connection's stdin and stdout, which every process of the server can open through /proc/1/fd: a
# server that runs commands (exec) asks for this, so that none of them can write MCP messages to the client.
PID1_VALUES = ("server",)
# "tmp_exec": true - the server's /tmp may run programs (Podman: its tmpfs without noexec; Bubblewrap's /tmp always
# could). Only for a server whose own tools must run a helper they unpack or copy there (smali: apktool runs its aapt2
# only after chmod, which the read-only image refuses); from its signed catalog entry, part of its identity. Every
# other server's /tmp stays noexec; nosuid and nodev stay for all.
# How long an agent waits for one call of the server (seconds; its "tool_timeout", from its catalog entry or `mcp set
# --tool-timeout`): a server whose tools run long (exec: commands up to 600 s) says so. The agent's setting, not the
# server's: nothing of what the server can reach (definition_digest leaves it out).
TOOL_TIMEOUT_RANGE = (10, 3600)
LIMIT_KEYS = ("memory", "cpus", "pids", "tmp")
# An image server's, unless its definition says otherwise. tmp: the size of its /tmp (a tmpfs: memory, counted against
# the memory limit). A browser keeps its profile, cache, shared memory (--disable-dev-shm-usage) and screenshots
# there: 64m crashed Chromium on the first heavier page.
IMAGE_DEFAULTS = {"memory": "512m", "pids": 128, "tmp": "64m"}
LIMIT_OPTIONS = {"memory": "--memory", "cpus": "--cpus", "pids": "--pids", "tmp": "--tmp-size"}  # a command server's
COMMAND_DEFAULTS = {"memory": "8g", "pids": 4096}  # the launcher's --memory and --pids defaults (a test keeps both)
# A server's instance with limits of its own (--mcp-limits): NAME.HASH. A dot is in no server's name, so an instance
# never takes a defined server's name; 8 hex digits keep the socket's path short (a Unix socket's takes 107 bytes).
INSTANCE_PATTERN = MCP_NAME_PATTERN + r"(?:\.[0-9a-f]{8})?"
IMAGE_REF = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+"
                       r"@sha256:[0-9a-f]{64}")
MOUNT_TARGET = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
# The run line of catalog servers: the same as axross's, so images are tested for exactly this.
RUN_FLAGS = ("--rm", "-i", "--init", "--pull=never", "--log-driver=none", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--read-only",
             "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,notmpcopyup")
# The published, signed catalogs are read by default; a copy in sandbox-ai's own tree (projects/NAME/catalog.json, a
# development checkout's) only with SANDBOX_AI_LOCAL_CATALOGS=1 (default_catalogs).
LOCAL_CATALOGS = {"mcp": "sandbox-ai-mcp", "hub": "axross-mcp-hub"}
CATALOGS = (("mcp", "https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/main/catalog.json"),
            ("hub", "https://raw.githubusercontent.com/c0decave/axross-mcp-hub/main/catalog.json"))
CATALOG_LIMIT = 2 << 20
GATEWAY_LIMIT = 96 << 10  # the relay's configuration travels as one argument (Linux takes 128 KiB)
AGENT_MCP = HERE / "agent_mcp.py"  # a helper's server: the agent, run for each task (--helper)
BUILTIN = {"demo": {"command": ["python3", "-I", "/opt/sandbox-ai/mcp_demo.py"],
                    "options": ["--offline", "--mount", f"{HERE / 'mcp_demo.py'}:/opt/sandbox-ai/mcp_demo.py:ro"],
                    "description": "Test server: list_directory and version"}}
_launcher = None
_launcher_source = None  # starts.py's launcher() when starts.py loaded this module: one launcher for all of its modules


def launcher():
    global _launcher
    if _launcher is None and _launcher_source is not None:
        _launcher = _launcher_source()
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_mcp", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def forbidden():
    # Server sandboxes never get credentials, sessions, tmux or nested MCP.
    return launcher().CONFIG_FORBIDDEN + ("--mcp", "--mcp-limits", "--mcp-mount", "--mcp-env", "--mcp-socket-dir",
                                          "--tmux",
                                          "--copy-auth", "--auth-file", "--auth-from-session",
                                          "--import-codex-auth", "--copy", "--probe", "--binary", "--state-dir",
                                          "--gui-wayland", "--git-token-env", "--git-token-file",
                                          "--git-credentials", "--git-token-secret", "--git-identity",
                                          "--kit", "--kit-dir", "--helper",
                                          "--backend", "--podman-url",  # always the agent's own
                                          "--dangerously-skip-permissions")


def validated(servers, source):
    if not isinstance(servers, dict):
        raise ValueError(f"{source}: MCP servers must be an object of NAME: definition")
    result = {}
    for name, definition in servers.items():
        if not re.fullmatch(launcher().MCP_NAME, name):
            raise ValueError(f"{source}: invalid MCP server name {name!r}")
        if isinstance(definition, dict) and ("image" in definition or "rootfs" in definition):
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
        if "tool_timeout" in definition:
            checked_tool_timeout(definition["tool_timeout"], f"{source}: {name}")
        result[name] = {**definition, "options": options} if "options" in definition else definition
    return result


def validated_image(definition, where):
    """A container definition with every value checked (it becomes a podman command line)."""
    if set(definition) - IMAGE_KEYS:
        raise ValueError(f"{where}: allowed keys of an image server are {sorted(IMAGE_KEYS)}")
    if "image" not in definition and "rootfs" not in definition:
        raise ValueError(f"{where}: an image server needs its image (Podman) or its rootfs (Bubblewrap)")
    image = definition.get("image")
    if "image" in definition and (not isinstance(image, str) or not IMAGE_REF.fullmatch(image)):
        raise ValueError(f"{where}: image must be REGISTRY/PATH@sha256:<64 hex>, pinned by digest")
    if "rootfs" in definition:
        rootfs_module().validated(definition["rootfs"], where)
        if definition.get("vpn"):
            raise ValueError(f"{where}: a VPN is for the Podman form of a server (drop rootfs or vpn)")
    check_egress(definition, where)
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
    check_variables(definition, where)
    checked_memory(definition.get("memory", "512m"), where)
    if "cpus" in definition:
        checked_cpus(definition["cpus"], where)
    if "tmp" in definition:
        checked_tmp(definition["tmp"], where)
    check_tmp_fits({"memory": str(definition.get("memory", "512m")).lower(),
                    "tmp": str(definition.get("tmp", IMAGE_DEFAULTS["tmp"])).lower()}, where)
    pids = definition.get("pids", 128)
    if isinstance(pids, bool) or not isinstance(pids, int):  # a number in the file, not text
        raise ValueError(f"{where}: pids must be 16-4096")
    checked_pids(pids, where)
    for key in ("description", "source", "catalog"):
        if key in definition and not isinstance(definition[key], str):
            raise ValueError(f"{where}: {key} must be text")
    if "tool_timeout" in definition:
        checked_tool_timeout(definition["tool_timeout"], where)
    if "built" in definition:
        checked_built(definition, where)
    if "pid1" in definition:
        checked_pid1(definition["pid1"], where)
    if "tmp_exec" in definition and definition["tmp_exec"] is not True:
        raise ValueError(f"{where}: tmp_exec must be true (its /tmp may run programs) or left out")
    if "vpn" in definition:
        if not isinstance(definition["vpn"], str) or not re.fullmatch(launcher().VPN_PROFILE_NAME, definition["vpn"]):
            raise ValueError(f"{where}: vpn must name a VPN profile (sandbox-ai vpn list)")
        if definition.get("network", "none") == "none":
            raise ValueError(f"{where}: a VPN needs network full (without network nothing would use it)")
    return definition


def checked_built(definition, where):
    """DEFINITION's "built" (an image built here: mcp add --build) checked: its commit, repository, path and the source
    archive's hash, and never signed ("signed_by" null): nothing built here is presented as signed."""
    built = definition["built"]
    if not isinstance(built, dict) or set(built) - set(BUILT_KEYS) or \
            not all(isinstance(built.get(key), str) and built[key].isprintable() and len(built[key]) <= 1024
                    for key in BUILT_KEYS) or not re.fullmatch(r"[0-9a-f]{40}", built["commit"]) or \
            not re.fullmatch(r"[0-9a-f]{64}", built["archive_sha256"]):
        raise ValueError(f"{where}: built is {{commit (40 hex), repository, path, archive_sha256 (64 hex), at}}")
    if definition.get("signed_by") is not None:
        raise ValueError(f"{where}: an image built here is never signed (signed_by must be null)")
    if "image" not in definition or "rootfs" in definition:
        raise ValueError(f"{where}: a server built here is its image, for Podman")


_egress = None


def egress_module():
    """mcp_egress.py: each connection's egress guard, and vpn_runtime.py's rules through it."""
    global _egress
    if _egress is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_egress", HERE / "mcp_egress.py")
        with LOADING:
            if _egress is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _egress = loaded
    return _egress


def checked_hosts(hosts, where):
    """HOSTS (a catalog entry's ["NAME:PORT"]) as an allowlist server keeps them; ValueError otherwise."""
    if not isinstance(hosts, list) or not hosts or len(hosts) > HOSTS_LIMIT:
        raise ValueError(f"{where}: an allowlist server needs its hosts (NAME:PORT, its catalog entry's; at most "
                         f"{HOSTS_LIMIT})")
    for host in hosts:
        if not isinstance(host, str):
            raise ValueError(f"{where}: hosts: each is NAME:PORT")
        try:
            egress_module().runtime().egress_host(host)
        except ValueError as exc:
            raise ValueError(f"{where}: hosts: {exc}") from None
    return list(hosts)


def checked_allowed(key, values, where):
    """VALUES of allow_private (networks of yours, not all addresses) or allow_tcp (ADDRESS:PORT) as a definition
    keeps them (their text, as given); ValueError otherwise."""
    if not isinstance(values, list) or len(values) > EGRESS_LIST_LIMIT or \
            not all(isinstance(value, str) for value in values):
        raise ValueError(f"{where}: {key} must be a list of at most {EGRESS_LIST_LIMIT} values")
    rt = egress_module().runtime()
    for value in values:
        try:
            if key == "allow_tcp":
                rt.egress_endpoint(value)
            elif ipaddress.ip_network(value, strict=False).prefixlen == 0:
                raise ValueError(f"{value} is every address: give --network lan instead")
        except ValueError as exc:
            raise ValueError(f"{where}: {key}: {exc}") from None
    return list(values)


def check_egress(definition, where):
    """The network of an image server DEFINITION (NETWORKS) with its hosts and the parts of your network it may
    reach, checked; ValueError otherwise."""
    network = definition.get("network", "none")
    if network not in NETWORKS:
        raise ValueError(f"{where}: network must be none, full, allowlist or lan")
    if network == "allowlist":
        checked_hosts(definition.get("hosts"), where)
    elif "hosts" in definition:
        raise ValueError(f"{where}: hosts: only an allowlist server has hosts of its own")
    for key in ("allow_private", "allow_tcp"):
        if key in definition:
            if network == "none":
                raise ValueError(f"{where}: {key}: a server without network reaches nothing")
            checked_allowed(key, definition[key], where)


class ListAppend(argparse._AppendAction):
    """--allow-private and --allow-tcp of mcp add|set: one or more in a value, separated by commas or spaces (as the
    launcher's allow-type options take them); a value without any is refused. Each is checked where it is kept."""

    def __call__(self, parser, namespace, values, option_string=None):
        items = list(dict.fromkeys(item for item in re.split(r"[\s,]+", str(values)) if item))
        if not items:
            raise argparse.ArgumentError(self, "no value (one or more, separated by commas or spaces)")
        setattr(namespace, self.dest, [*(getattr(namespace, self.dest, None) or []), *items])


def network_label(definition):
    """An image server's network for people: "internet (no LAN)", "only its hosts (A:443, ...)", "internet + LAN",
    "none", and the parts of your network opened to it."""
    network = definition.get("network", "none")
    label = NETWORK_LABELS.get(network, str(network))
    if network == "allowlist":
        label += f" ({', '.join(definition.get('hosts') or [])})"
    opened = [*definition.get("allow_private", []), *definition.get("allow_tcp", [])]
    return label + (f" + {', '.join(opened)}" if opened and network != "none" else "")


def egress_note(name, definition, entry=None):
    """What `mcp list`, `mcp add` and doctor say of the network of the image server NAME (DEFINITION; ENTRY: its
    catalog entry, where known), or None: one that reaches your network (lan, allow_private, allow_tcp), and a full
    server whose catalog entry needs only its hosts (switch to them)."""
    network = definition.get("network", "none")
    opened = [*definition.get("allow_private", []), *definition.get("allow_tcp", [])]
    if network == "lan":
        return (f"MCP server {name} reaches your local network and this host (--network lan); only the clouds' metadata "
                f"service is refused. Back to the internet alone: sandbox-ai mcp set {name} --network full")
    if network != "none" and opened:
        return (f"MCP server {name} reaches {', '.join(opened)} of your network (--allow-private/--allow-tcp); the "
                "rest of it stays closed")
    if network in ("full", "allowlist") and not opened and str((definition.get("env_values") or {}).get(
            "ALLOW_PRIVATE_TARGETS", "")).strip().lower() not in ("", "0", "false", "no"):
        # (the way to reach your own network before 0.1.98: such a server reaches none of it now)
        return (f"MCP server {name} has ALLOW_PRIVATE_TARGETS=1 for targets in your local network, but its network is "
                "the internet, not your local network (since 0.1.98), so it reaches none of them: open the part it "
                f"needs with sandbox-ai mcp set {name} --allow-private 192.168.1.0/24 (or --allow-tcp IP:PORT; all of "
                "your network: --network lan)")
    if network == "full" and isinstance(entry, dict) and entry.get("network") == "allowlist" and entry.get("hosts"):
        return (f"MCP server {name} reaches the whole internet, while its catalog entry needs only "
                f"{', '.join(map(str, entry['hosts']))}: sandbox-ai mcp set {name} --network allowlist keeps it to them")
    return None


def checked_tool_timeout(value, where="tool_timeout"):
    """VALUE as a server's tool_timeout: whole seconds within TOOL_TIMEOUT_RANGE (a number in the file, not text);
    ValueError otherwise."""
    low, high = TOOL_TIMEOUT_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{where}: tool_timeout must be whole seconds, {low}-{high} (how long an agent waits for one "
                         f"call of the server), not {shown_value(value)}")
    return value


def checked_pid1(value, where="pid1"):
    """VALUE as a server's pid1: "server" (it runs as PID 1 itself, see PID1_VALUES); ValueError for anything else,
    never read as the default or as on."""
    if not isinstance(value, str) or value not in PID1_VALUES:
        raise ValueError(f'{where}: pid1 must be "server" (the server runs as PID 1 and reaps its children) or left '
                         f"out, not {shown_value(value)}")
    return value


def server_is_pid1(definition):
    """The image or rootfs server DEFINITION runs as PID 1 itself ("pid1": "server"; validated_image checked it)."""
    return definition.get("pid1") == "server"


def entry_tool_timeout(entry):
    """The tool_timeout catalog ENTRY declares, checked; None when it declares none or one that is not valid (the
    agents' default then, as limits of an entry that are not valid are left out)."""
    try:
        return checked_tool_timeout(entry["tool_timeout"]) if "tool_timeout" in entry else None
    except ValueError:
        return None


def parse_tool_timeout(value):
    """`mcp set --tool-timeout SECONDS|default`: the seconds (checked), or None for default."""
    text = str(value).strip().lower()
    if text == "default":
        return None
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise ValueError(f"--tool-timeout: whole seconds, {TOOL_TIMEOUT_RANGE[0]}-{TOOL_TIMEOUT_RANGE[1]}, or default")
    return checked_tool_timeout(int(text), "--tool-timeout")


# --- limits: the definition's for every start, a start's own (--mcp-limits) in an instance of its own ---------------

CPUS_LIMIT = 1024


def shown_value(value):
    """VALUE in an error message: short, on one line (it may come from a file or a field someone typed in)."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def checked_memory(value, where="memory"):
    """VALUE as a memory limit in lower case ("512m", "4g": a whole number of MiB or GiB); ValueError otherwise."""
    text = value.strip().lower() if isinstance(value, str) else None
    if text is None or not re.fullmatch(r"[1-9][0-9]{0,5}[mg]", text):
        raise ValueError(f"{where}: memory must be like 512m or 4g (a whole number with m or g), not "
                         f"{shown_value(value)}")
    return text


def checked_cpus(value, where="cpus"):
    """VALUE as a CPU limit, as text ("2", "1.5"): a number of CPUs from 0.01 to 1024 with at most two decimals (a
    number of a file, or text someone typed in); ValueError otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        number = None
    elif isinstance(value, str):
        number = float(value.strip()) if re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,2})?", value.strip()) else None
    else:
        number = float(value) if value == value and round(float(value), 2) == float(value) else None
    if number is None or not 0.01 <= number <= CPUS_LIMIT:
        raise ValueError(f"{where}: cpus must be a number of CPUs from 0.01 to {CPUS_LIMIT} (2, 1.5), not "
                         f"{shown_value(value)}")
    return f"{number:.2f}".rstrip("0").rstrip(".")


def checked_pids(value, where="pids"):
    """VALUE as a process limit: a whole number from 16 to 4096 (a number, or text of one); ValueError otherwise."""
    number = value if isinstance(value, int) and not isinstance(value, bool) else \
        int(value.strip()) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value.strip()) else None
    if number is None or not 16 <= number <= 4096:
        raise ValueError(f"{where}: pids must be 16-4096 (processes and threads), not {shown_value(value)}")
    return number


def checked_tmp(value, where="tmp"):
    """VALUE as the size of a server's /tmp in lower case ("64m", "1g"); ValueError otherwise."""
    text = value.strip().lower() if isinstance(value, str) else None
    if text is None or not re.fullmatch(r"[1-9][0-9]{0,5}[mg]", text):
        raise ValueError(f"{where}: tmp must be like 64m or 1g (the size of the server's /tmp: a whole number with m "
                         f"or g), not {shown_value(value)}")
    return text


def size_mib(text):
    """MiB of a size "512m" / "2g" (None: none, or not one)."""
    match = re.fullmatch(r"([0-9]+)([mg])", str(text).strip().lower())
    return int(match[1]) * (1024 if match[2] == "g" else 1) if match else None


def check_tmp_fits(limits, where="limits"):
    """ValueError when LIMITS' tmp is more than its memory: /tmp is a tmpfs, its files are memory counted against the
    memory limit - a /tmp as large as the memory leaves the server's processes nothing and they are killed."""
    tmp, memory = size_mib(limits.get("tmp")), size_mib(limits.get("memory"))
    if tmp is not None and memory is not None and tmp > memory:
        raise ValueError(f"{where}: tmp {limits['tmp']} is more than the memory {limits['memory']}: /tmp is memory (a "
                         "tmpfs, counted against the memory limit); give it at most the memory, better half of it")


def checked_limits(limits, where="limits"):
    """{memory, cpus, pids, tmp} (each optional) checked and in their usual form: memory "2g", cpus "1.5", pids 512,
    tmp "1g"; tmp never more than memory where both are given. ValueError names what is wrong (another key, a value
    out of range)."""
    if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS):
        raise ValueError(f"{where}: limits are memory, cpus, pids and tmp")
    found = {}
    for key, check in (("memory", checked_memory), ("cpus", checked_cpus), ("pids", checked_pids),
                       ("tmp", checked_tmp)):
        if key in limits:
            found[key] = check(limits[key], where)
    check_tmp_fits(found, where)
    return found


def host_cpus_default():
    """Half this host's CPUs, at least one: the CPU limit of a server that names none (as an agent's --cpus)."""
    return str(max(1, (os.cpu_count() or 1) // 2))


def default_limits(definition):
    """{memory, cpus, pids, tmp} server DEFINITION runs with when a start asks for nothing else: an image server's
    (each connection's container) from its definition, else 512m, half this host's CPUs, 128, a /tmp of 64m; a command
    server's (its sandbox) from its options, else the launcher's 8g, half the CPUs, 4096 and a /tmp of half the memory
    (--tmp-size; "none": no limit). None for a helper."""
    if "helper" in definition:
        return None
    if "image" in definition or "rootfs" in definition:
        return {"memory": str(definition.get("memory", IMAGE_DEFAULTS["memory"])).lower(),
                "cpus": checked_cpus(definition["cpus"]) if "cpus" in definition else host_cpus_default(),
                "pids": definition.get("pids", IMAGE_DEFAULTS["pids"]),
                "tmp": str(definition.get("tmp", IMAGE_DEFAULTS["tmp"])).lower()}
    options = definition.get("options", [])
    found = {}
    for key, default in (("memory", COMMAND_DEFAULTS["memory"]), ("cpus", None), ("pids", COMMAND_DEFAULTS["pids"]),
                         ("tmp", None)):
        value = option_value(options, LIMIT_OPTIONS[key])  # (validated() made the short forms long)
        value = str(value).strip().lower() if value is not None else default
        if key == "cpus" and value is None:
            value = host_cpus_default()
        if key == "tmp" and value is None:  # the launcher's: half the memory limit
            memory = size_mib(found["memory"])
            value = f"{memory // 2}m" if memory else "none"
        if value != "none":
            with contextlib.suppress(ValueError):  # the launcher's own check says what is wrong with it
                value = {"memory": lambda v: str(v).lower(), "cpus": checked_cpus,
                         "pids": lambda v: int(v), "tmp": lambda v: str(v).lower()}[key](value)
        found[key] = value
    return found


def parse_start_limits(value):
    """(NAME, {memory, cpus, pids}) of one --mcp-limits NAME:KEY=VALUE[,KEY=VALUE...]; ValueError says what is wrong."""
    name, colon, rest = str(value).partition(":")
    if not colon or not re.fullmatch(MCP_NAME_PATTERN, name):
        raise ValueError(f"--mcp-limits NAME:memory=SIZE,cpus=N,pids=N,tmp=SIZE (NAME: an MCP server), not "
                         f"{shown_value(value)}")
    given = {}
    for part in rest.split(","):
        key, equals, number = part.strip().partition("=")
        if not equals or key not in LIMIT_KEYS or key in given:
            raise ValueError(f"--mcp-limits {name}: give memory=SIZE, cpus=N, pids=N and tmp=SIZE, each at most once, not "
                             f"{shown_value(part.strip())}")
        given[key] = number.strip()
    return name, checked_limits(given, f"--mcp-limits {name}")


def start_limits(a):
    """{server: {memory, cpus, pids}} a start asks for (its --mcp-limits, later ones over earlier ones key by key)."""
    found = {}
    for value in getattr(a, "mcp_limits", None) or []:
        name, limits = parse_start_limits(value)
        found[name] = {**found.get(name, {}), **limits}
    return found


def with_limits(name, definition, limits):
    """DEFINITION with LIMITS (checked: checked_limits) in place of its own: an image server's keys, a command server's
    options (--memory, --cpus, --pids after its own: the last one counts). A helper or a team's board takes none."""
    if not limits:
        return definition
    if "helper" in definition or name.startswith("team-"):
        raise ValueError(f"MCP server {name}: a helper's or a team's server takes no limits of its own "
                         "(--mcp-limits); it runs with its lead's start")
    if "image" in definition or "rootfs" in definition:
        return {**definition, **limits}
    return {**definition, "options": [*definition.get("options", []),
                                      *[token for key in LIMIT_KEYS if key in limits
                                        for token in (LIMIT_OPTIONS[key], str(limits[key]))]]}


def instance_for(name, definition, limits, mounts=None, env=None):
    """(instance name, definition with the limits, folders and variables) of server NAME for a start asking for LIMITS
    ({}: none), MOUNTS ({target: {host, mode}} of its --mcp-mount; None: none) and ENV ({VAR: (kind, detail)} of its
    --mcp-env; None: none): NAME itself where they are its own (shared by every such start), else NAME.HASH, one per
    set of limits, folders and variables."""
    mounted = with_mounts(name, definition, mounts) if mounts else definition
    if env:
        # The start's own variables: an instance of its own unless they are what the definition says already.
        own = with_start_env(name, mounted, env)
        if own != mounted:
            found, changed = instance_for(name, definition, limits, mounts)
            changed = with_start_env(name, changed, env)
            variables = {key: changed[key] for key in ENV_KEYS if key in changed}
            key = {"base": found, "env": variables}
            return f"{name}.{hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]}", changed
    if not limits and mounted == definition:
        return name, definition
    defaults = default_limits(definition) or {}
    # What it runs with then (a command server's /tmp follows its memory unless it names one: half of it).
    wanted = {**defaults, **(default_limits(with_limits(name, definition, limits)) or {}), **limits} if limits \
        else defaults
    check_tmp_fits(wanted, f"MCP server {name}")  # its own memory with the server's /tmp, or the other way round
    if wanted == defaults and mounted == definition:
        return name, definition
    # Limits alone: the hash of the limits, as before; with folders of the start's own, of both.
    key = wanted if mounted == definition else {"limits": wanted, "mounts": mounted.get("mounts", [])}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:8]
    # All three, the defaults too: two starts with the same limits run the same definition (one names cpus, one not)
    return f"{name}.{digest}", with_limits(name, mounted, wanted) if wanted != defaults else mounted


# --- folders: the definition's for every start, a start's own (--mcp-mount) in an instance of its own --------------

def parse_start_mount(value):
    """(NAME, TARGET, HOST, MODE) of one --mcp-mount NAME:TARGET=HOST[:ro|rw] (HOST absolute or ~/..., expanded;
    MODE None where it is not given: the definition's). ValueError says what is wrong."""
    text = str(value)
    usage = (f"--mcp-mount NAME:TARGET=HOST[:ro|rw] (NAME: an MCP server, TARGET: one of the folders its definition "
             f"mounts, HOST: a folder of this machine), not {shown_value(value)}")
    name, colon, rest = text.partition(":")
    target, equals, host = rest.partition("=")
    if not colon or not equals or not re.fullmatch(MCP_NAME_PATTERN, name) or not MOUNT_TARGET.fullmatch(target) \
            or ".." in target.split("/"):
        raise ValueError(usage)
    mode = None
    if host.endswith((":ro", ":rw")):
        host, mode = host[:-3], host[-2:]
    expanded = os.path.expanduser(host)
    if not host or not os.path.isabs(expanded) or any(c in host for c in (",", ":")) \
            or any(ord(c) < 32 for c in host):
        raise ValueError(f"--mcp-mount {name}: HOST must be an absolute folder of this machine (or ~/...) without "
                         f", or :, not {shown_value(host)}")
    return name, target, os.path.normpath(expanded), mode


def start_mounts(a):
    """{server: {target: {"host", "mode"}}} a start asks for (its --mcp-mount, a later one for a target over an
    earlier one)."""
    found = {}
    for value in getattr(a, "mcp_mount", None) or []:
        name, target, host, mode = parse_start_mount(value)
        found.setdefault(name, {})[target] = {"host": host, "mode": mode}
    return found


def with_mounts(name, definition, mounts):
    """DEFINITION with the host folders MOUNTS ({target: {host, mode}}) in place of its own for those targets. Only an
    image server's folders, only the targets its definition mounts already (a start never adds one), and never a
    read-only one made writable (a mode left out: the definition's). A helper, a team's board or a command server
    takes none (a command server's folders are its options' --mount)."""
    if not mounts:
        return definition
    if "helper" in definition or name.startswith("team-"):
        raise ValueError(f"MCP server {name}: a helper's or a team's server takes no folders of its own "
                         "(--mcp-mount); it works on its lead's workspace")
    if "image" not in definition and "rootfs" not in definition:
        raise ValueError(f"MCP server {name} is defined with a command: its folders are the --mount options of its "
                         "definition (sandbox-ai mcp list), not --mcp-mount")
    own = {m["target"]: m for m in definition.get("mounts", [])}
    for target, given in mounts.items():
        if target not in own:
            raise ValueError(f"--mcp-mount {name}:{target}: the server mounts "
                             + (", ".join(sorted(own)) if own else "no folder") + f", not {target} (a start only "
                             "changes the host folder of a target its definition has)")
        if given.get("mode") == "rw" and own[target].get("mode", "ro") != "rw":
            raise ValueError(f"--mcp-mount {name}:{target}: its definition mounts it read-only; a start does not make "
                             f"it writable (sandbox-ai mcp set {name} --mount {target}=HOST:rw, if you mean it)")
    changed = [{**mount, **({"host": mounts[mount["target"]]["host"],
                            "mode": mounts[mount["target"]].get("mode") or mount.get("mode", "ro")}
                           if mount["target"] in mounts else {})}
               for mount in definition.get("mounts", [])]
    return validated_image({**definition, "mounts": changed}, f"--mcp-mount {name}")


def start_change(a, name):
    """(limits, mounts) the start A asks for server NAME (--mcp-limits, --mcp-mount): None for what it does not."""
    return start_limits(a).get(name), start_mounts(a).get(name)


def start_mount_hosts(a):
    """[Path] of the host folders the start A gives its MCP servers (--mcp-mount), resolved: no workspace of it may
    hold one (the agent could swap the folder for a link before the next connection mounts it)."""
    hosts = []
    for mounts in start_mounts(a).values():
        for given in mounts.values():
            with contextlib.suppress(OSError, RuntimeError):
                hosts.append(Path(given["host"]).resolve())
    return hosts


def check_start_mounts(a, servers, create=False, out=None):
    """The --mcp-mount folders of the start A for the servers it uses (SERVERS: {name: definition}), checked as `mcp
    add --mount` checks them (mount_host: a folder that is there; a writable one that is not is created with CREATE,
    else only checked to be creatable) and for what with_mounts allows. ValueError says what is wrong."""
    for name, mounts in start_mounts(a).items():
        if name not in servers:
            continue
        mounted = with_mounts(name, servers[name], mounts)
        for mount in mounted.get("mounts", []):
            if mount["target"] in mounts:
                mount_host(f"{name}:{mount['target']}={mount['host']}:{mount.get('mode', 'ro')}", mount["host"],
                           mount.get("mode", "ro"), create, out, option="--mcp-mount")


# --- variables: required ones, optional ones, plain values and secrets of another name ------------------------------
#
# A definition names the variables its server's containers get, each in one of four ways:
#   "env": [NAME]                  required: NAME of your environment, else the secret NAME (sandbox-ai secrets) - the
#                                  only form before 0.1.95, so every older definition stays as it was
#   "env_optional": [NAME]         the same where there is one; left out where there is none (no refusal)
#   "env_values": {NAME: VALUE}    a fixed value that is no secret (ALLOW_PRIVATE_TARGETS=1); checked: one line, at most
#                                  ENV_VALUE_LIMIT characters, nothing of a secret's shape or name
#   "env_secrets": {NAME: SECRET}  required: the secret SECRET (sandbox-ai secrets); its value is never in the file
# `mcp add` and `mcp set` take --env NAME, --env NAME=VALUE and --env NAME@SECRET; a start's own values (the board's MCP
# part) are --mcp-env NAME:VAR=VALUE and NAME:VAR@SECRET, in an instance of its own as with --mcp-limits.

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
ENV_VALUE_LIMIT = 1024
ENV_LIMIT = 64  # variables of one server
ENV_KEYS = ("env", "env_optional", "env_values", "env_secrets")
# Words of a variable's name that say it holds a secret (split at "_": MAX_TOKENS is none, GITHUB_TOKEN is one).
SECRET_WORDS = frozenset({"TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "APIKEY", "CREDENTIAL", "CREDENTIALS",
                          "COOKIE", "PW", "PWD", "PASS", "PASSCODE"})
# Words that end in one of these are a secret's too (AUTHTOKEN, CLIENTSECRET, ROOTPASSWORD; TOKENS and TOKENIZER not).
SECRET_ENDINGS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "APIKEY")
SECRET_PAIRS = frozenset({("API", "KEY"), ("PRIVATE", "KEY"), ("ACCESS", "KEY"), ("AUTH", "KEY"), ("SIGNING", "KEY")})
# What the server's run sets itself: never a definition's or a start's.
RESERVED_ENV = frozenset({"SANDBOX_AI_MCP_MOUNTS"})
ENV_HELP = ("--env NAME (NAME of your environment, else the secret NAME of sandbox-ai secrets), --env NAME=VALUE (a "
            "value that is no secret, kept in the definition) or --env NAME@SECRET (the secret SECRET)")
_privacy = None


def privacy_module():
    """privacy.py: the shapes of credentials its check knows (BUILTIN), so that none is kept as a plain value."""
    global _privacy
    if _privacy is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_privacy_for_mcp", HERE / "privacy.py")
        with LOADING:
            if _privacy is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _privacy = loaded
    return _privacy


def secret_looking(name, value):
    """Why NAME=VALUE looks like a secret, or None: a name that says so (GITHUB_TOKEN, BRAVE_API_KEY, DB_PASSWORD -
    split at "_", so MAX_TOKENS is none), or a value of a credential's shape (the privacy check's patterns)."""
    parts = [part for part in str(name).upper().split("_") if part]
    if SECRET_WORDS & set(parts) or any(pair in SECRET_PAIRS for pair in zip(parts, parts[1:])) or \
            any(part.endswith(SECRET_ENDINGS) for part in parts):
        return f"{name} is the name of a secret"
    for _key, regex, _severity, reason in privacy_module().BUILTIN:
        if re.search(regex, str(value)):
            return f"the value has the shape of a credential ({reason})"
    return None


def checked_env_value(name, value, where="--env"):
    """VALUE as the plain value of variable NAME: text of one line (no control character), at most ENV_VALUE_LIMIT
    characters, nothing that looks like a secret (secret_looking). ValueError says what is wrong and how a secret is
    kept instead."""
    if not isinstance(value, str):
        raise ValueError(f"{where} {name}: its value must be text")
    if len(value) > ENV_VALUE_LIMIT:
        raise ValueError(f"{where} {name}: its value is longer than {ENV_VALUE_LIMIT} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{where} {name}: its value must be one line, without control characters")
    reason = secret_looking(name, value)
    if reason:
        if where.startswith("--mcp-env "):  # a start's own variable (--mcp-env NAME:): its own option says how
            raise ValueError(f"{where} {name}: {reason}, and a secret is never kept as a plain value: keep it with "
                             f"sandbox-ai secrets set {name} and give {where}{name}@{name} (--mcp-env "
                             f"{where[len('--mcp-env '):]}{name}@SECRET for a secret of another name)")
        raise ValueError(f"{where} {name}: {reason}, and a secret is never kept as a plain value: keep it with "
                         f"sandbox-ai secrets set {name} and give --env {name} (or --env {name}@SECRET for a secret "
                         "of another name)")
    return value


def parse_env_spec(value, where="--env", check=True):
    """(NAME, kind, detail) of one --env value: NAME ("pass": your environment's NAME, else the secret NAME), NAME=VALUE
    ("value", the value checked: checked_env_value, unless not CHECK - the caller checks it) or NAME@SECRET ("secret",
    the secret's name). ValueError otherwise."""
    text = value if isinstance(value, str) else ""
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]{0,63})(?:=(.*)|@([A-Za-z_][A-Za-z0-9_]{0,63}))?", text, re.S)
    if not match:
        raise ValueError(f"{where}: {ENV_HELP}; not {shown_value(value)}")
    name = match[1]
    if name in RESERVED_ENV:
        raise ValueError(f"{where} {name}: the server's start sets this variable itself")
    if match[3] is not None:
        return name, "secret", match[3]
    if match[2] is not None:
        return name, "value", checked_env_value(name, match[2], where) if check else match[2]
    return name, "pass", None


def check_variables(definition, where):
    """The variables of an image server's DEFINITION (env, env_optional, env_values, env_secrets) checked: names of
    variables (secrets' names too), plain values as checked_env_value takes them, each variable in one of them only,
    at most ENV_LIMIT, none the start sets itself. ValueError otherwise."""
    for key in ("env", "env_optional"):
        names = definition.get(key, [])
        if not isinstance(names, list) or not all(isinstance(v, str) and ENV_NAME.fullmatch(v) for v in names):
            raise ValueError(f"{where}: {key} must be a list of variable names")
    values, secrets = definition.get("env_values", {}), definition.get("env_secrets", {})
    if not isinstance(values, dict) or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) for k in values):
        raise ValueError(f"{where}: env_values must be an object of NAME: VALUE (values that are no secret)")
    for name, value in values.items():
        checked_env_value(name, value, f"{where}: env_values")
    if not isinstance(secrets, dict) or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str)
                                                and ENV_NAME.fullmatch(v) for k, v in secrets.items()):
        raise ValueError(f"{where}: env_secrets must be an object of NAME: SECRET (the names of sandbox-ai secrets)")
    names = [*definition.get("env", []), *definition.get("env_optional", []), *values, *secrets]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f"{where}: each variable once (env, env_optional, env_values or env_secrets), not "
                         f"{', '.join(twice)} twice")
    if len(names) > ENV_LIMIT:
        raise ValueError(f"{where}: at most {ENV_LIMIT} variables")
    reserved = sorted(RESERVED_ENV & set(names))
    if reserved:
        raise ValueError(f"{where}: the server's start sets {', '.join(reserved)} itself")


def variables_of(definition):
    """[{"name", "source": "env" | "optional" | "value" | "secret", "value" (a plain one), "secret" (its secret's
    name)}] of DEFINITION's variables: what `mcp list --json` and the app show."""
    found = [{"name": name, "source": "env"} for name in definition.get("env", [])]
    found += [{"name": name, "source": "optional"} for name in definition.get("env_optional", [])]
    found += [{"name": name, "source": "value", "value": value}
              for name, value in (definition.get("env_values") or {}).items()]
    found += [{"name": name, "source": "secret", "secret": secret}
              for name, secret in (definition.get("env_secrets") or {}).items()]
    return found


def with_variables(definition, given):
    """DEFINITION with the variables GIVEN ({NAME: (kind, detail)}, kind "pass" (required), "optional", "value" or
    "secret") in place of what it says of them. env stays (an older reader finds it); the other keys only when they
    hold something."""
    changed = {key: value for key, value in definition.items() if key not in ENV_KEYS}
    env = [n for n in definition.get("env", []) if n not in given]
    optional = [n for n in definition.get("env_optional", []) if n not in given]
    values = {n: v for n, v in (definition.get("env_values") or {}).items() if n not in given}
    secrets = {n: v for n, v in (definition.get("env_secrets") or {}).items() if n not in given}
    for name, (kind, detail) in given.items():
        if kind == "value":
            values[name] = detail
        elif kind == "secret":
            secrets[name] = detail
        elif kind == "optional":
            optional.append(name)
        else:
            env.append(name)
    changed["env"] = env
    for key, value in (("env_optional", optional), ("env_values", values), ("env_secrets", secrets)):
        if value:
            changed[key] = value
    return changed


def entry_variables(entry, specs, where="--env"):
    """{env, env_optional, env_values, env_secrets} of the --env values SPECS (parse_env_spec) for catalog ENTRY,
    whose "env" declares each variable it uses as {name, description, secret, required}: only those, each once, every
    required one. NAME of a required variable is required (env), of another one optional (env_optional: passed where
    there is one); NAME=VALUE is never taken for a secret one (its value comes from sandbox-ai secrets, never the
    file); NAME@SECRET names the secret it comes from."""
    declared = {v.get("name"): v for v in entry.get("env", []) if isinstance(v, dict) and isinstance(v.get("name"), str)}
    given, unknown = {}, []
    for spec in specs:
        name, kind, detail = parse_env_spec(spec, where, check=False)
        if name not in declared:
            unknown.append(name)
            continue
        if name in given:
            raise ValueError(f"{where} {name}: given twice")
        if kind == "value" and declared[name].get("secret"):
            raise ValueError(f"{where} {name}=...: {entry['name']} takes {name} as a secret, so its value is never "
                             f"kept in the definition: keep it with sandbox-ai secrets set {name} and give --env "
                             f"{name} (or --env {name}@SECRET)")
        if kind == "value":
            detail = checked_env_value(name, detail, where)
        if kind == "pass" and not declared[name].get("required"):
            kind = "optional"
        given[name] = (kind, detail)
    if unknown:
        raise ValueError(f"{entry['name']} does not use {', '.join(unknown)}"
                         + (f" (it takes {', '.join(declared)})" if declared else ""))
    missing = [n for n, v in declared.items() if v.get("required") and n not in given]
    if missing:
        raise ValueError(f"{entry['name']} needs --env {missing[0]} (the variable is read from your environment, else "
                         f"from the secret {missing[0]} of sandbox-ai secrets, when the server starts; --env "
                         f"{missing[0]}@SECRET names another secret)")
    return with_variables({}, given)


def entry_env(entry, env):
    """The names of the variables ENV (--env values) for catalog ENTRY, checked as entry_variables checks them (the
    form before 0.1.95; add and set use entry_variables)."""
    found = entry_variables(entry, env)
    return [*found["env"], *found.get("env_optional", []), *found.get("env_values", {}), *found.get("env_secrets", {})]


_secrets = None


def secret_store_module():
    """secret_store.py (sandbox-ai secrets): where a variable's secret comes from."""
    global _secrets
    if _secrets is None:
        spec = importlib.util.spec_from_file_location("secret_store_for_mcp", HERE / "secret_store.py")
        with LOADING:
            if _secrets is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _secrets = loaded
    return _secrets


def secret_value(name):
    """The value of the secret NAME (sandbox-ai secrets), or None (none, or no store to read)."""
    try:
        return secret_store_module().get(name)
    except (OSError, ValueError):
        return None


def run_variables(name, definition, environ=None, secret=None, notes=None):
    """({variable: value} the containers of server NAME get, [problem]) from DEFINITION: env from ENVIRON (your
    environment) or the secret of its name, env_optional the same where there is one, env_values as they are,
    env_secrets from their secrets (SECRET: name -> value or None). A problem (a required variable without a value)
    says its fix; no value is ever said. A required one without a value that its catalog declares optional
    (catalog_optional: a definition of before 0.1.95) is left out, said in NOTES (a list) instead."""
    environ = os.environ if environ is None else environ
    secret = secret or secret_value
    found, problems = {}, []
    for variable in definition.get("env", []):
        value = environ.get(variable)
        value = value if value is not None else secret(variable)
        if value is None and catalog_optional(definition, variable):
            if notes is not None:
                notes.append(f"MCP server {name}: {variable} not set: left out (optional in the catalog); set it with "
                             f"sandbox-ai mcp set {name} --env {variable}=VALUE (with its other variables), or make it "
                             f"optional in the definition: sandbox-ai mcp set {name} --env {variable}")
            continue
        if value is None:
            problems.append(f"MCP server {name} needs the variable {variable}: set it, or keep it with sandbox-ai "
                            f"secrets set {variable}; a value that is no secret goes into its definition: sandbox-ai "
                            f"mcp set {name} --env {variable}=VALUE (with its other variables)"
                            + (f"; if its catalog declares it optional, sandbox-ai mcp set {name} --env {variable} "
                               "(with its other variables) makes it optional"
                               if isinstance(definition.get("catalog"), str) else ""))
            continue
        found[variable] = value
    for variable in definition.get("env_optional", []):
        value = environ.get(variable)
        value = value if value is not None else secret(variable)
        if value is not None:
            found[variable] = value
    found.update(definition.get("env_values") or {})
    for variable, source in (definition.get("env_secrets") or {}).items():
        value = secret(source)
        if value is None:
            problems.append(f"MCP server {name} takes {variable} from the secret {source}, which is not there: "
                            f"sandbox-ai secrets set {source}")
            continue
        found[variable] = value
    return found, problems


def run_definition(definition, passed):
    """DEFINITION as its containers run it: "env" the variables PASSED (each --env=NAME, the values in the bridge's
    environment, never on a command line), the other forms gone."""
    return {**{key: value for key, value in definition.items() if key not in ENV_KEYS}, "env": list(passed)}


# --- a start's own variables (--mcp-env), in an instance of its own --------------------------------------------------

def parse_start_env(value):
    """(NAME, VAR, kind, detail) of one --mcp-env NAME:VAR=VALUE or NAME:VAR@SECRET (parse_env_spec); ValueError says
    what is wrong."""
    name, colon, rest = str(value).partition(":")
    if not colon or not re.fullmatch(MCP_NAME_PATTERN, name) or not rest:
        raise ValueError(f"--mcp-env NAME:VAR=VALUE or NAME:VAR@SECRET (NAME: an MCP server), not {shown_value(value)}")
    variable, kind, detail = parse_env_spec(rest, f"--mcp-env {name}:")
    if kind == "pass":
        raise ValueError(f"--mcp-env {name}:{variable}: give its value (VAR=VALUE) or the secret it comes from "
                         "(VAR@SECRET)")
    return name, variable, kind, detail


def start_env(a):
    """{server: {VAR: (kind, detail)}} a start asks for (its --mcp-env, a later one for a variable over an earlier
    one)."""
    found = {}
    for value in getattr(a, "mcp_env", None) or []:
        name, variable, kind, detail = parse_start_env(value)
        found.setdefault(name, {})[variable] = (kind, detail)
    return found


# Variables that change what code a program runs: the loader's libraries (LD_*, macOS' DYLD_*), an interpreter's
# startup, options and module path, a shell's startup, the programs found on PATH. A value of one makes the server's
# sandbox run other code than its own - with a folder the agent can write mounted there (--mcp-mount), the agent's
# code, with the server's secrets and network. You choose it: set, with a warning; a preset passed on may not.
CODE_ENV = frozenset({"NODE_OPTIONS", "NODE_PATH", "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE",
                      "PYTHONINSPECT", "PERL5OPT", "PERL5LIB", "PERLLIB", "PERL5DB", "RUBYOPT", "RUBYLIB", "BASH_ENV",
                      "ENV", "PROMPT_COMMAND", "PATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
                      "CLASSPATH", "GCONV_PATH", "LUA_INIT", "LUA_PATH", "LUA_CPATH", "GIT_EXEC_PATH",
                      "DENO_DIR", "BUN_OPTIONS", "GODEBUG", "PHPRC", "PHP_INI_SCAN_DIR"})
CODE_ENV_PREFIXES = ("LD_", "DYLD_")


def code_variable(name):
    """Whether the variable NAME changes what code a program runs (CODE_ENV, LD_*, DYLD_*)."""
    return str(name) in CODE_ENV or str(name).startswith(CODE_ENV_PREFIXES)


def code_env_in(options, given=()):
    """["--mcp-env NAME:VAR", ...] of the --mcp-env values among OPTIONS (canonical, as on the command line) whose
    VAR changes what code runs (code_variable) - but a whole value the user GIVES (--preset-value) is theirs."""
    given, found = {str(value).strip() for value in given if value}, []
    for index, token in enumerate(options):
        flag, equals, value = str(token).partition("=")
        if flag != "--mcp-env":
            continue
        value = value if equals else (str(options[index + 1]) if index + 1 < len(options) else "")
        match = re.fullmatch(r"([^:]*):([A-Za-z_][A-Za-z0-9_]{0,63})(?:[=@].*)?", value, re.S)
        if match and code_variable(match[2]) and value.strip() not in given:
            found.append(f"--mcp-env {match[1]}:{match[2]}")
    return found


def code_env_warnings(a):
    """The warnings of the start A's --mcp-env variables that change what code runs in an MCP server's sandbox
    (code_variable): you chose them, so they are set - the warning names the risk. [] for a value that is not valid
    (start_problems says what is wrong with it)."""
    try:
        wanted = start_env(a)
    except ValueError:
        return []
    # (short enough to be relayed whole from a session's supervisor: sessions.RELAYED)
    return [f"--mcp-env {name}:{variable}: {variable} changes what code runs in the MCP server {name}'s sandbox: "
            "code in a folder the agent can write (a --mcp-mount) would run as the server, with its secrets and "
            "network; set as you chose it"
            for name, variables in wanted.items() for variable in variables if code_variable(variable)]


def with_start_env(name, definition, env):
    """DEFINITION with a start's own variables ENV ({VAR: (kind, detail)}) in place of its own: only an image
    server's (a command server's are its options' --env), never a helper's or a team's board."""
    if not env:
        return definition
    if "helper" in definition or name.startswith("team-"):
        raise ValueError(f"MCP server {name}: a helper's or a team's server takes no variables of its own "
                         "(--mcp-env); it runs with its lead's start")
    if "image" not in definition and "rootfs" not in definition:
        raise ValueError(f"MCP server {name} is defined with a command: its variables are the --env options of its "
                         "definition (sandbox-ai mcp list), not --mcp-env")
    # As mcp add -e NAME=VALUE: never a plain value for a variable the server takes from a secret (its definition's
    # env_secrets) - the value would be kept in the instance's definition and a saved board, whatever its name says.
    secret_ones = sorted(variable for variable, (kind, _) in env.items()
                         if kind == "value" and variable in (definition.get("env_secrets") or {}))
    if secret_ones:
        variable = secret_ones[0]
        raise ValueError(f"--mcp-env {name}:{variable}=...: {name} takes {variable} from the secret "
                         f"{definition['env_secrets'][variable]}, so its value is never kept as a plain value: give "
                         f"--mcp-env {name}:{variable}@SECRET (sandbox-ai secrets set SECRET)")
    return validated_image(with_variables(definition, env), f"--mcp-env {name}")


def server_of(instance):
    """The server an instance is of: NAME of NAME.HASH, NAME itself."""
    return instance.split(".", 1)[0]


def instance_of(a, name):
    """The instance of server NAME the start A reaches: the one its check chose (a.mcp_instances: start_instance, an
    instance of its own where NAME runs with another definition), else instance_for its --mcp-limits, --mcp-mount and
    --mcp-env; NAME without them."""
    chosen = (getattr(a, "mcp_instances", None) or {}).get(name)
    if isinstance(chosen, str) and re.fullmatch(INSTANCE_PATTERN, chosen) and server_of(chosen) == name:
        return chosen
    limits, mounts = start_change(a, name)
    env = start_env(a).get(name)
    if not limits and not mounts and not env:
        return name
    servers = {**definitions(getattr(a, "project_document", None)), **(getattr(a, "helper_definitions", None) or {})}
    if name not in servers:
        return name
    return instance_for(name, servers[name], limits, mounts, env)[0]


def instances(name, include_stopped=False):
    """The instances of server NAME with state here (NAME and its NAME.HASH), running ones only unless
    INCLUDE_STOPPED; NAME first."""
    found = [name] if include_stopped or status(name) == "running" else []
    try:
        directories = sorted(path.name for path in state_dir(name).parent.iterdir()
                             if path.name.startswith(name + ".") and path.is_dir() and not path.is_symlink())
    except OSError:
        directories = []
    for instance in directories:
        if re.fullmatch(INSTANCE_PATTERN, instance) and server_of(instance) == name and \
                (include_stopped or status(instance) == "running"):
            found.append(instance)
    return found


def limits_text(limits):
    """'2g, 2 CPUs, 512 processes' of LIMITS ({memory, cpus, pids}; "none": no limit), '-' for none."""
    if not limits:
        return "-"
    parts = []
    for key, unit in (("memory", ""), ("cpus", " CPUs"), ("pids", " processes"), ("tmp", "")):
        if key in limits:
            value = limits[key]
            parts.append(f"no {key} limit" if value == "none" else f"/tmp {value}" if key == "tmp" else
                         f"{value}{unit}")
    return ", ".join(parts) or "-"


_rootfs = None


def rootfs_module():
    global _rootfs
    if _rootfs is None:
        spec = importlib.util.spec_from_file_location("mcp_rootfs_for_mcp", HERE / "mcp_rootfs.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _rootfs is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _rootfs = loaded
    return _rootfs


def entry_rootfs(entry, read=None):
    """The definition's "rootfs" for catalog ENTRY: its archive fetched, checked against the SHA-256 the signed
    catalog gives and unpacked (rootfs_module), with the entry's entrypoint, user, workdir and environment."""
    given = entry.get("rootfs")
    if not isinstance(given, dict) or not isinstance(given.get("url"), str):
        raise ValueError(f"{entry.get('name')}: the catalog has no rootfs for Bubblewrap yet (use Podman)")
    module = rootfs_module()
    rootfs = {"path": str(module.rootfs_dir(str(given.get("sha256")))), "sha256": given["sha256"],
              "entrypoint": given.get("entrypoint"), "user": given.get("user", 65534),
              "workdir": given.get("workdir", "/tmp"), "env": given.get("env", {})}
    module.validated(rootfs, entry.get("name"))
    module.install(given["url"], given["sha256"], read)
    return rootfs


def check_image_policy(name, definition):
    """An image server runs podman itself, outside the launcher: the administrator's policy is applied here. A
    server with network acts for the agent past its firewall and proxy, so mcp_network none (or a network
    ceiling, allow_within) refuses it. require_vpn holds for a server with network as for an agent; a policy
    that forbids --vpn forbids a server's VPN too."""
    l = launcher()
    policy = l.load_policy()
    network = definition.get("network", "none")  # (full, allowlist, lan: each reaches the network)
    if network != "none" and \
            (policy.get("mcp_network", "full") == "none" or policy.get("allow_within") is not None):
        raise ValueError(f"MCP server {name}: network {network} is not allowed by the administrator's policy "
                         f"({l.POLICY_FILE})")
    required, vpn = policy.get("require_vpn", False), definition.get("vpn")
    if network != "none" and required and \
            (not vpn or (isinstance(required, list) and vpn not in required)):
        raise ValueError(f"MCP server {name}: the administrator's policy requires a VPN for network {network}"
                         + (f" ({', '.join(required)})" if isinstance(required, list) else "")
                         + f" ({l.POLICY_FILE}); add one with: sandbox-ai mcp add ... --vpn PROFILE")
    if vpn and any(entry.split("=", 1)[0] == "--vpn" for entry in policy.get("forbid", [])):
        raise ValueError(f"MCP server {name}: --vpn is not allowed by the administrator's policy ({l.POLICY_FILE})")


def check_image_mounts(name, definition, remember=True, warn=None):
    """The host folders of an image server - yours: `mcp add|set --mount`, the project file you trusted - pass the
    launcher's host-path policy, read-only or not, as a --mount does (agent_sandbox.check_given_path): no symlink a
    sandbox may have planted on the way (refused: not your choice); a folder of your sockets or a tree with a live
    socket, a file system of the kernel's own, sandbox-ai's own folders, a private location, a folder of the VPNs'
    keys, one inside a tree an agent can write (it could swap the folder for a link: each connection checks it is
    still the one it was) - given, with a warning (WARN(text), default: the launcher's warner). Their rw folders join
    the registry (with REMEMBER: the server's start; `mcp add|set` and a start's first check only check)."""
    l = launcher()
    before = l.exposed_before()
    writable = []
    vpn = l.load_vpn_module()
    for mount in definition.get("mounts", []):
        host = os.path.expanduser(mount["host"])
        option = f"MCP server {name}: mount {mount['target']}"
        vpn.check_paths([host], option, warn=warn)
        l.check_planted(host, option, before)
        resolved = Path(host).resolve()
        for tree in before:
            if tree in resolved.parents:
                l.warner(warn)(f"{option}: {resolved} lies inside {tree}, which a sandbox can write: an agent there "
                               "can change what the server keeps in it, or replace the folder with a link (a "
                               "connection is refused then); a folder of its own is safer")
                break
        # As a --mount gets, whatever its mode (the container runs as root, you): a folder of sockets, a file system
        # of the kernel's own, a tree with a live socket, sandbox-ai's own folders, a private location - each said.
        l.check_given_path(resolved, option, mount.get("mode", "ro") == "rw", warn=warn)
        if mount.get("mode", "ro") == "rw":
            writable.append(resolved)
    if writable and remember:
        l.remember_exposed(None, writable)


def mount_records(definition):
    """[{"path", "dev", "ino"}] of an image server's host folders, as image_command mounts them."""
    records = []
    for mount in definition.get("mounts", []):
        path = Path(os.path.expanduser(mount["host"])).resolve()
        info = os.stat(path)
        records.append({"path": str(path), "dev": info.st_dev, "ino": info.st_ino})
    return records


TOR_USERS = {"950", "debian-tor", "tor", "_tor", "toranon"}


_services = None


def services_module():
    """services.py, loaded once: its image_file reads a file of an image (image_passwd)."""
    global _services
    if _services is None:
        spec = importlib.util.spec_from_file_location("services_for_mcp", HERE / "services.py")
        with LOADING:
            if _services is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _services = loaded
    return _services


def image_passwd(image, run=None):
    """The text of IMAGE's /etc/passwd, read without running anything of the image - as services.image_file reads
    every file of an image nobody reviewed: a container created (never started) without network, the file copied
    out of it as a tar stream read only up to 1 MiB and a header's slack (services.copy_bounded), only one regular
    file taken, the container removed. None when it cannot be read. RUN(command): podman's runner (tests)."""
    runner = None if run is None else (lambda argv, **_kwargs: run(argv))
    return services_module().image_file(image, "/etc/passwd", ["podman"], run=runner)


def passwd_uid(text, user):
    """The uid of USER in the passwd TEXT as the container engine reads it (Go's user package: each line trimmed,
    the first match), or None."""
    for line in (text or "").splitlines():
        fields = line.strip().split(":")
        if len(fields) >= 3 and fields[0].strip() == user:
            try:
                return int(fields[2].strip())
            except ValueError:
                return None
    return None


def check_tor_image_user(name, image, inspect=None, passwd=None):
    """An image server behind a tor profile must not run as tor's own user: the kill switch lets that user's
    packets out past Tor (it shares the VPN container's network namespace). The container cannot become it
    (--cap-drop=ALL, no-new-privileges), so only the image's configured user counts: refused when it is tor's
    uid, one of the usual names for it, or a name the image's own /etc/passwd (PASSWD: its text, read with
    image_passwd) gives that uid; and refused when the uid of a name cannot be found (it could be tor's)."""
    inspect = inspect or (lambda: subprocess.run(["podman", "image", "inspect", "--format", "{{.Config.User}}", image],
                                                 capture_output=True, text=True, timeout=30))
    # A user that cannot be told is refused (it could be tor's): an inspect that failed or timed out (a busy Podman)
    # never lets the server go behind Tor unchecked.
    try:
        result = inspect()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"MCP server {name}: sandbox-ai cannot tell which user its image runs as (podman image "
                         f"inspect: {exc}); behind a tor profile it must know (tor's own uid would pass the kill "
                         "switch): try again") from None
    if result.returncode != 0:
        said = (result.stderr or "").strip()[-300:] or f"exit status {result.returncode}"
        raise ValueError(f"MCP server {name}: sandbox-ai cannot tell which user its image runs as (podman image "
                         f"inspect {image}: {said}); behind a tor profile it must know. Is the image there? Add it "
                         f"again: sandbox-ai mcp add")
    user = (result.stdout or "").strip().split(":", 1)[0]
    refused = (f"MCP server {name}: its image runs as {user!r}, the user tor's kill switch lets out past Tor; it "
               "cannot go behind a tor profile (use an image with another user)")
    if user in TOR_USERS:
        raise ValueError(refused)
    tor_uid = int(launcher().load_vpn_module().runtime().TOR_UID)
    if re.fullmatch(r"[+-]?[0-9]+", user):  # a number as the engine reads it: "0950" is 950
        if int(user) == tor_uid:
            raise ValueError(refused)
        return
    if not user or user == "root":
        return
    text = passwd() if passwd is not None else image_passwd(image)
    uid = passwd_uid(text, user)
    if uid is None:
        raise ValueError(f"MCP server {name}: its image runs as {user!r}, whose uid its /etc/passwd does not say; "
                         "behind a tor profile the uid must be known (tor's own, 950, would pass the kill switch)")
    if uid == tor_uid:
        raise ValueError(refused.replace(f"{user!r}", f"{user!r} (uid {uid})", 1))


def tor_environment():
    """What a program behind Tor is told (an MCP server's container, a command server's or agent's sandbox): that it
    runs behind Tor, and the network tor maps .onion names into (its VirtualAddrNetworkIPv4); never set otherwise."""
    return launcher().load_vpn_module().tor_environment()


def image_command(plan, engine="podman"):
    """The podman command that runs one instance of an image server (one per connection), from its network PLAN
    (server_network: the definition - an instance's with its limits - and its one network option). The network
    option comes from the plan alone."""
    definition, network = checked_plan(plan, "image")
    limits = default_limits(definition)
    flags = [flag.replace(",size=64m,", f",size={limits['tmp']},") if flag.startswith("--tmpfs=/tmp:") else flag
             for flag in RUN_FLAGS]
    if server_is_pid1(definition):  # no catatonit holding the MCP pipes at /proc/1/fd: the server is PID 1
        flags.remove("--init")
    if definition.get("tmp_exec") is True:  # (its signed entry: helpers it runs from /tmp; nosuid, nodev stay)
        flags = [flag.replace(",noexec,", ",exec,") if flag.startswith("--tmpfs=/tmp:") else flag for flag in flags]
    command = [engine, "run", *flags, f"--pids-limit={limits['pids']}", f"--memory={limits['memory']}"]
    controllers = launcher().delegated_controllers()
    if controllers is None or "cpu" in controllers:  # rootless Podman sets it only where the controller is delegated
        # Never more than this host has: Podman refuses that (a catalog's 2 on a machine with one CPU).
        command.append(f"--cpus={min(float(limits['cpus']), float(os.cpu_count() or 1)):g}")
    command.append(network)
    if plan.get("vpn") and plan.get("vpn_type") == "tor":
        # Behind Tor (and only there): the server is told so, and which addresses tor gives .onion names, so that a
        # fetcher that refuses private targets takes those (an onion service) and says why it refuses others.
        command += [f"--env={key}={value}" for key, value in tor_environment().items()]
    for name in definition.get("env", []):
        command.append(f"--env={name}")  # the value comes from the environment, never the command line
    if definition.get("mounts"):
        command.append("--user=0:0")  # the mounted files belong to you, who are root in rootless podman
        for mount in definition["mounts"]:
            host = Path(os.path.expanduser(mount["host"])).resolve()
            command.append(f"--volume={host}:{mount['target']}:{mount.get('mode', 'ro')}")
    egress = plan.get("egress") or {}
    if egress.get("kind") == "hosts":
        # Its names: only its pinned hosts (the guard, or its VPN container, writes them before each connection).
        command.append(f"--volume={egress['hosts_file']}:/etc/hosts:ro")
    return [*command, definition["image"]]


def read_source(source, fetch=None):
    """The bytes of SOURCE: an https URL or a file (at most CATALOG_LIMIT + 1)."""
    if source.startswith("https://"):
        import urllib.request
        fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=30).read(CATALOG_LIMIT + 1))
        return fetch(source)
    return Path(os.path.expanduser(source)).read_bytes()[:CATALOG_LIMIT + 1]


_keys = None


def release_keys():
    global _keys
    if _keys is None:
        spec = importlib.util.spec_from_file_location("release_keys_for_mcp", HERE / "release_keys.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _keys is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _keys = loaded
    return _keys


def catalog_signature(source, data, fetch=None, keys=None):
    """{"state": "good", "signer": FINGERPRINT} when SOURCE.sig is an SSH signature over DATA by a key of
    sandbox/mcp-signers (namespace sandbox-ai-mcp), {"state": "unsigned"|"bad", "reason"} otherwise. The signature
    says the catalog's servers were built from their pinned sources, audited and checked with sandbox-ai under
    Podman and Bubblewrap; it covers every image digest and rootfs hash in it."""
    k = release_keys()
    try:
        trusted = k.load(ring=k.MCP) if keys is None else keys
    except ValueError as exc:
        return {"state": "bad", "reason": str(exc)}
    try:
        signature = read_source(source + ".sig", fetch)
    except Exception:  # noqa: BLE001 - not there, not reachable: unsigned, which add refuses unless asked
        return {"state": "unsigned", "reason": f"{source}.sig cannot be read"}
    if not trusted:
        return {"state": "unsigned", "reason": f"no MCP signing key is trusted yet ({k.MCP['signers']})"}
    try:
        verdict, detail = k.verify(data, signature, trusted, ring=k.MCP, what="catalog.json")
    except RuntimeError as exc:  # no ssh-keygen
        return {"state": "unsigned", "reason": str(exc)}
    return {"state": "good", "signer": detail} if verdict == "good" else {"state": "bad", "reason": detail}


def read_catalog(source, fetch=None, signature=False):
    """Entries of an MCP catalog (axross-mcp-catalog schema, version 1) from a file or https URL; with SIGNATURE
    (entries, catalog_signature)."""
    data = read_source(source, fetch)
    if len(data) > CATALOG_LIMIT:
        raise ValueError(f"{source}: larger than {CATALOG_LIMIT >> 20} MiB")
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError(f"{source}: not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != "axross-mcp-catalog" or \
            document.get("version") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError(f"{source}: not an MCP catalog (schema axross-mcp-catalog, version 1)")
    entries = [entry for entry in document["entries"] if well_formed(entry)]
    return (entries, catalog_signature(source, data, fetch)) if signature else entries


def well_formed(entry):
    """A catalog ENTRY whose fields have the types the listing and `mcp add` read: a name, texts where texts are
    expected, lists of texts or objects (a list as a name, or a number as its hosts, ended `mcp catalog` and
    `mcp add` with a traceback for every catalog). One that is not is left out."""
    def texts(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def objects(value, key):
        return isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get(key), str)
                                               for item in value)
    return isinstance(entry, dict) and isinstance(entry.get("name"), str) \
        and all(isinstance(entry[key], str) for key in ("id", "title", "kind", "image", "digest", "network")
                if entry.get(key) is not None) \
        and texts(entry.get("hosts", [])) and objects(entry.get("mounts", []), "target") \
        and objects(entry.get("env", []), "name") and isinstance(entry.get("limits", {}), dict)


def read_sidecar(source, signature, fetch=None):
    """({name: rootfs entry}, {name: limits}, problem or None) of rootfs.json beside catalog SOURCE: sandbox-ai's own
    part of the catalog - the Bubblewrap form of the servers (archive URL, SHA-256, entrypoint, user, workdir, env)
    and, optionally, the limits a server needs ("limits": {NAME: {"memory": "2g", "cpus": 2, "pids": 512}}, which
    `mcp add` takes). A file of its own, since axross reads the catalog and takes no keys it does not know; signed like
    it (rootfs.json.sig). Used only when its signature is as good as the catalog's: a signed catalog takes only a
    signed rootfs.json."""
    if not source.endswith("catalog.json"):
        return {}, {}, None
    path = source[:-len("catalog.json")] + "rootfs.json"
    try:
        data = read_source(path, fetch)
    except Exception:  # noqa: BLE001 - no rootfs.json: the catalog's servers are for Podman only
        return {}, {}, None
    own = catalog_signature(path, data, fetch)
    if own["state"] == "bad" or signature["state"] == "good" and own["state"] != "good":
        return {}, {}, f"{path}: not used ({own.get('reason', 'not signed like its catalog')})"
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return {}, {}, f"{path}: not JSON"
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("rootfs"), dict):
        return {}, {}, f"{path}: expected {{\"version\": 1, \"rootfs\": {{NAME: ...}}}}"
    limits = document.get("limits") if isinstance(document.get("limits"), dict) else {}
    return ({name: entry for name, entry in document["rootfs"].items() if isinstance(entry, dict)},
            {name: entry for name, entry in limits.items() if isinstance(entry, dict)}, None)


LOCAL_CATALOGS_VARIABLE = "SANDBOX_AI_LOCAL_CATALOGS"  # =1: the copies in sandbox-ai's own tree (development);
# =FOLDER (absolute): the checkouts FOLDER/sandbox-ai-mcp and FOLDER/axross-mcp-hub (a catalog's own git checkout, which
# mcp add --build builds a pending entry from, at its commit)


def default_catalogs(root=None, environ=None):
    """CATALOGS: the published, signed catalogs. The copy in sandbox-ai's own tree (projects/NAME/catalog.json, a
    development checkout's) only with SANDBOX_AI_LOCAL_CATALOGS=1 (=FOLDER: FOLDER/NAME/catalog.json, the catalogs'
    own checkouts): an installation carried such a copy, old and unsigned, and took it before the published one -
    every entry was "not signed by a trusted key". A copy's signature is checked as the published one's."""
    environ = os.environ if environ is None else environ
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    wanted = environ.get(LOCAL_CATALOGS_VARIABLE) or ""
    local_wanted = wanted == "1" or os.path.isabs(wanted)
    folder = Path(wanted) if os.path.isabs(wanted) else root / "projects"
    chosen = []
    for prefix, url in CATALOGS:
        local = folder / LOCAL_CATALOGS.get(prefix, "") / "catalog.json"
        chosen.append((prefix, str(local) if local_wanted and prefix in LOCAL_CATALOGS and local.is_file() else url))
    return chosen


def downloaded(sources, fetch=None):
    """FETCH (read_source's) for the catalogs SOURCES: what catalog_entries downloads of each - the catalog, its
    signature, its rootfs.json and that one's signature - downloaded at once, each in a thread of its own, and then
    handed out as if downloaded then (the same bytes, or the same exception). One after the other they took eight
    round trips (570 ms here, seconds through Tor or a VPN); a URL not downloaded here is downloaded when asked for."""
    urls = []
    for _, source in sources:
        if source.startswith("https://"):
            urls += [source, source + ".sig"]
            if source.endswith("catalog.json"):
                rootfs = source[:-len("catalog.json")] + "rootfs.json"
                urls += [rootfs, rootfs + ".sig"]
    urls = list(dict.fromkeys(urls))
    results = {}
    if fetch is None and urls:
        # One TLS context for all, as urllib makes one per connection (the system's CAs, the host name checked): made
        # in eight threads at once, OpenSSL's locks spun for 1.4 s of CPU time.
        import ssl
        import urllib.request
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

        def fetch(url):
            with opener.open(url, timeout=30) as response:
                return response.read(CATALOG_LIMIT + 1)

    def download(url):
        try:
            results[url] = (True, read_source(url, fetch))
        except Exception as exc:  # noqa: BLE001 - raised again where the URL is read, as it would have been
            results[url] = (False, exc)
    threads = [threading.Thread(target=download, args=(url,), name="mcp-catalog", daemon=True) for url in urls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    def handed_out(url):
        if url not in results:
            return read_source(url, fetch)
        ok, value = results[url]
        if not ok:
            raise value
        return value
    return handed_out


def os_reason(exc):
    """What went wrong of an OSError EXC in words ("No such file or directory"; a download's reason), without Python's
    "[Errno N]" and the path said again."""
    reason = getattr(exc, "reason", None)
    if reason is not None:
        return str(getattr(reason, "strerror", None) or reason)
    return exc.strerror or str(exc)


def catalog_entries(sources=None, fetch=None):
    """[(prefix, entry)] of all catalogs; unreachable ones are reported, not fatal."""
    found, problems = [], []
    sources = list(sources or default_catalogs())
    fetch = downloaded(sources, fetch)
    for prefix, source in sources:
        try:
            entries, signature = read_catalog(source, fetch, signature=True)
            rootfs, limits, problem = read_sidecar(source, signature, fetch)
            if problem:
                problems.append(problem)
            # The limits an entry recommends: its own "limits" (a catalog axross does not read), else rootfs.json's.
            found += [(prefix, {**entry, "_signature": signature, "_catalog": source,
                                **({"rootfs": rootfs[entry.get("name")]} if entry.get("name") in rootfs else {}),
                                **({"limits": limits[entry.get("name")]} if entry.get("name") in limits
                                   and "limits" not in entry else {})})
                      for entry in entries]
        except OSError as exc:
            if getattr(exc, "code", None) == 404:  # urllib's HTTPError
                problems.append(f"{source}: not published there (yet); pass --catalog FILE_OR_URL")
            else:
                problems.append(f"{source}: {os_reason(exc)}")
        except ValueError as exc:
            problems.append(f"{source}: {exc}")
    remember_recommended(found)
    remember_catalog_env(found)
    return found, problems


def recommended_file():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "mcp-recommended.json"


def catalog_env_file():
    return recommended_file().with_name("mcp-catalog-env.json")


def remember_catalog_env(found):
    """The variables each entry of FOUND (catalog_entries) declares - whether required, whether a secret - and its
    catalog's signature, kept by catalog and entry (catalog_env_file): a start asks whether a variable a definition
    needs is optional in its catalog (catalog_optional) without reading the catalog (no network at a start). An
    entry of a catalog whose signature does not verify is forgotten."""
    try:
        path = catalog_env_file()
        known = json.loads(path.read_text()) if path.is_file() else {}
        known = known if isinstance(known, dict) else {}
    except (OSError, ValueError, RecursionError):
        known = {}
    changed = False
    for _prefix, entry in found:
        catalog, ident = entry.get("_catalog"), entry.get("id") or entry.get("name")
        if not isinstance(catalog, str) or not isinstance(ident, str):
            continue
        state = (entry.get("_signature") or {}).get("state")
        if state not in ("good", "unsigned"):
            changed |= isinstance(known.get(catalog), dict) and known[catalog].pop(ident, None) is not None
            continue
        env = {v["name"]: {"required": v.get("required") is not False, "secret": v.get("secret") is not False}
               for v in entry.get("env", []) if isinstance(v, dict) and isinstance(v.get("name"), str)
               and ENV_NAME.fullmatch(v["name"])}
        kept = {"signature": state, "env": env}
        # Its network and hosts too: `mcp list` and doctor tell a full server of an allowlist entry to switch to
        # its hosts without reading the catalog (cached_entry).
        if entry.get("network") in ("none", "full", "allowlist"):
            kept["network"] = entry["network"]
            kept["hosts"] = []
            for host in [host for host in entry.get("hosts") or [] if isinstance(host, str)][:HOSTS_LIMIT]:
                with contextlib.suppress(ValueError):
                    egress_module().runtime().egress_host(host)
                    kept["hosts"].append(host)
        if not isinstance(known.get(catalog), dict):
            known[catalog] = {}
        if known[catalog].get(ident) != kept:
            known[catalog][ident] = kept
            changed = True
    if changed:
        with contextlib.suppress(OSError):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(path, known)


def cached_entry(definition):
    """{"network", "hosts"} of the catalog entry DEFINITION was added from, as that catalog was last read here
    (catalog_env_file; nothing is downloaded), or None."""
    catalog, source = definition.get("catalog"), definition.get("source")
    if not isinstance(catalog, str) or not isinstance(source, str):
        return None
    try:
        known = json.loads(catalog_env_file().read_text())
    except (OSError, ValueError, RecursionError):
        return None
    entries = known.get(catalog) if isinstance(known, dict) else None
    entry = entries.get(source) if isinstance(entries, dict) else None
    if not isinstance(entry, dict) or entry.get("signature") not in ("good", "unsigned") or "network" not in entry:
        return None
    return {"network": entry["network"], "hosts": list(entry.get("hosts") or [])}


def catalog_optional(definition, variable):
    """True when the catalog entry DEFINITION was added from declares VARIABLE optional and no secret (explicitly:
    "required": false, "secret": false), as that catalog was last read here (catalog_env_file; nothing is
    downloaded) - and, for a definition added from a signed catalog, signed by a trusted key then too. A definition
    added before 0.1.95 made such a variable required (`mcp add --env NAME`): it is left out when it has no value."""
    catalog, source = definition.get("catalog"), definition.get("source")
    if not isinstance(catalog, str) or not isinstance(source, str):
        return False
    try:
        known = json.loads(catalog_env_file().read_text())
    except (OSError, ValueError, RecursionError):
        return False
    entries = known.get(catalog) if isinstance(known, dict) else None
    entry = entries.get(source) if isinstance(entries, dict) else None
    if not isinstance(entry, dict) or entry.get("signature") not in ("good", "unsigned") or \
            (definition.get("signed_by") and entry.get("signature") != "good"):
        return False
    declared = entry.get("env", {}).get(variable) if isinstance(entry.get("env"), dict) else None
    return isinstance(declared, dict) and declared.get("required") is False and declared.get("secret") is False


def remember_recommended(found):
    """The limits the entries of FOUND (catalog_entries) recommend, kept by catalog and entry (recommended_file): `mcp
    list` compares a definition with them without reading the catalog again (it is asked every few seconds)."""
    try:
        path = recommended_file()
        known = json.loads(path.read_text()) if path.is_file() else {}
        known = known if isinstance(known, dict) else {}
    except (OSError, ValueError, RecursionError):
        known = {}
    changed = False
    for _prefix, entry in found:
        catalog, ident = entry.get("_catalog"), entry.get("id") or entry.get("name")
        if not isinstance(catalog, str) or not isinstance(ident, str):
            continue
        limits = recommended_limits(entry)
        if known.get(catalog, {}).get(ident) != limits:
            known.setdefault(catalog, {})[ident] = limits
            changed = True
    if changed:
        with contextlib.suppress(OSError):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(path, known)


def below_recommended(definition):
    """{key: the catalog's value} of the limits DEFINITION has below what its catalog entry recommends (as its catalog
    was last read here: recommended_file), {} when none or not known."""
    catalog, ident = definition.get("catalog"), definition.get("source")
    try:
        known = json.loads(recommended_file().read_text())
        wanted = known.get(catalog, {}).get(ident) if isinstance(known, dict) else None
        wanted = checked_limits(wanted) if isinstance(wanted, dict) else {}
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}
    have = default_limits(definition) or {}
    below = {}
    for key, value in wanted.items():
        mine = have.get(key)
        if mine in (None, "none"):
            continue
        number = size_mib if key in ("memory", "tmp") else float
        try:
            if number(mine) is not None and number(value) is not None and number(mine) < number(value):
                below[key] = value
        except (TypeError, ValueError):
            continue
    return below


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


MOUNT_HELP = ("--mount TARGET=HOST[:ro|rw]: TARGET is the folder inside the server's container, HOST the folder on "
              "this machine (read-only unless :rw; a writable one that is not there yet is created)")
START_MOUNT_HELP = ("--mcp-mount NAME:TARGET=HOST[:ro|rw]: TARGET is the folder inside the server NAME's container, HOST "
                    "the folder on this machine (read-only unless :rw; a writable one that is not there yet is created)")


def this_machine():
    """"this machine (NAME)": where `mcp add` runs is said by its name too - run on another host (sandbox-ai hosts run,
    the app's or the web's Add on a host), "this machine" alone was read as the machine the human sat at (user report
    2026-11-19: "the app tries to create the folder locally, not on the remote host")."""
    import socket
    try:
        name = "".join(c for c in socket.gethostname() if c.isprintable() and not c.isspace())[:64]
    except OSError:
        name = ""
    return f"this machine ({name})" if name else "this machine"


# Where a sandbox sees its folders (agent_sandbox: the workspace at /work, the others at /workspaces/NAME): a HOST
# folder of `mcp add --mount` given as one of these, and not there, is the agent's view of a folder (an agent that
# told the human the command wrote its own /work; user report 2026-11-20: "--mount /work=/work:rw: /work is not there
# and cannot be created (/ is not a folder you can write)").
SANDBOX_PLACES = ("/work", "/workspaces")


def running_workspaces():
    """[(start name, its main workspace on this machine)] of the starts running here (their run.json), at most 5; []
    when none can be read. Plain reads: said in a refusal, no Podman asked."""
    found = []
    try:
        directories = net_module().run_dirs()
    except Exception:  # noqa: BLE001 - only a hint
        return []
    for directory in directories:
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                record = json.loads((entry / "run.json").read_text())
                os.kill(int(record["pid"]), 0)
            except (OSError, ValueError, RecursionError, KeyError, TypeError):
                continue
            workspace, name = record.get("workspace"), record.get("name") or record.get("start") or entry.name
            if isinstance(workspace, str) and os.path.isabs(workspace) and record.get("kind", "agent") == "agent" \
                    and (str(name), workspace) not in found:
                found.append((str(name), workspace))
    return found[:5]


def sandbox_path_hint(value, path):
    """" HOST is a folder of this machine ...": what is said of the --mount VALUE whose HOST folder PATH is not there
    when PATH is where a sandbox sees a folder (SANDBOX_PLACES) - with the workspaces of the starts running here as the
    fix, read-only unless VALUE asked for writable; "" for any other PATH."""
    text = str(path)
    if not any(text == place or text.startswith(place + "/") for place in SANDBOX_PLACES):
        return ""
    printable = launcher().printable
    target = str(value).partition("=")[0]
    starts = running_workspaces()
    # Read-only, as the catalogs' analysis servers want their folder (radare2, binwalk: "read-only is enough"); :rw
    # stays the human's choice.
    fix = ("; ".join(f"{printable(name)}: --mount {printable(target)}={printable(folder)}:ro"
                     for name, folder in starts) if starts else
           f"--mount {printable(target)}=/path/of/your/project:ro on this machine")
    return (f" HOST is a folder of {this_machine()}, not a path inside a sandbox: {printable(text)} is where a sandbox "
            "sees its workspace (an agent that wrote the command gave its own view of the folder). Give the folder on "
            f"this machine that the sandbox shows there - " + ("the workspace of the start running here: " if starts
                                                               else "") + fix
            + (" (read-only is enough to analyze it; :rw only where the server must write)"
               if str(value).endswith(":rw") else ""))


def mount_host(value, host, mode, create=False, out=None, option="--mount"):
    """The folder HOST (of --mount VALUE) on this machine, resolved: a folder that is there; for MODE rw one that is
    not there yet is created with CREATE (0700, its parents as needed; OUT says so), else only checked to be creatable.
    ValueError where it is no folder, a read-only one is missing, or it cannot be created. OPTION: how VALUE was
    given (--mount of mcp add/set, a start's --mcp-mount NAME:TARGET=HOST), named so in what is said."""
    help_text = MOUNT_HELP if option == "--mount" else START_MOUNT_HELP
    if not host or not os.path.isabs(os.path.expanduser(host)):
        raise ValueError(f"{option} {value}: HOST must be an absolute folder of this machine (or ~/...). {help_text}")
    path = Path(os.path.expanduser(host)).resolve()
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"{option} {value}: {path} on {this_machine()} is not a folder. {help_text}")
        return path
    if mode != "rw":
        raise ValueError(f"{option} {value}: the folder {path} is not there on {this_machine()}; a read-only folder "
                         f"must be there (a writable one, :rw, is created)." + (sandbox_path_hint(value, path)
                                                                                or f" {help_text}"))
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f"{option} {value}: {path} is not there on {this_machine()} and cannot be created ({parent} is "
                         "not a folder you can write)." + sandbox_path_hint(value, path))
    if create:
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            raise ValueError(f"{option} {value}: {path} cannot be created: {exc.strerror or exc}") from None
        (out or print)(f"sandbox-ai mcp: created {path}")
    return path


def entry_mounts(entry, mounts, create=False, out=None):
    """[{target, host, mode}] of the --mount TARGET=HOST[:ro|rw] values MOUNTS for catalog ENTRY: only the targets it
    takes, each once, every required one; the host folders checked (mount_host: a writable one created with CREATE)."""
    known = {m.get("target"): m for m in entry.get("mounts", []) if isinstance(m, dict)}
    given = {}
    for value in mounts:
        target, sep, rest = value.partition("=")
        host, _, mode = rest.rpartition(":") if rest.endswith((":ro", ":rw")) else (rest, "", "ro")
        if not sep or target not in known:
            raise ValueError(f"{MOUNT_HELP}. {entry['name']} takes "
                             + (", ".join(t for t in known if t) or "no folders") + f", not {shown_value(value)}")
        if target in given:
            raise ValueError(f"--mount {target}: given twice")
        path = mount_host(value, host, mode or "ro", create, out)
        given[target] = {"target": target, "host": str(path), "mode": mode or "ro"}
    missing = [t for t, m in known.items() if m.get("required") and t not in given]
    if missing:
        raise ValueError(f"{entry['name']} needs --mount {missing[0]}=HOST (HOST: a folder on this machine)")
    return list(given.values())


def recommended_limits(entry):
    """The limits catalog ENTRY recommends (its "limits", or rootfs.json's for it: catalog_entries), checked; {} when
    it names none or ones that are not valid (a server is added with the defaults then)."""
    try:
        return checked_limits(entry.get("limits") or {}, f"{entry.get('name')}: limits")
    except ValueError:
        return {}


def cpus_number(text):
    """A CPU limit (checked_cpus's text) as the number a definition keeps: 2, 1.5."""
    number = float(text)
    return int(number) if number.is_integer() else number


def definition_from_entry(entry, mounts=(), env=(), network=None, memory=None, pids=None, vpn=None, cpus=None,
                          create=False, out=None, tmp=None, allow_private=(), allow_tcp=()):
    """A definition for ENTRY with the folders, variables and network given on the command line; the limits given, else
    those the entry recommends (recommended_limits), else none (the defaults). CREATE: a writable folder that is not
    there yet is created (mount_host). NETWORK: the entry's own unless given (an allowlist entry: only its hosts);
    ALLOW_PRIVATE, ALLOW_TCP: parts of your network opened to it (--allow-private, --allow-tcp)."""
    digest, image = entry.get("digest"), entry.get("image")
    if entry.get("kind") != "oci" or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"{entry.get('name')}: the catalog has no digest for it yet (not published): sandbox-ai mcp "
                         f"add {entry.get('name')} --build builds it here from its reviewed source and adds it "
                         "unsigned (asked first)")
    wanted = entry.get("network", "none")
    if network is None:
        network = wanted
    if network not in NETWORKS:
        raise ValueError("--network must be none, full, allowlist or lan")
    if network == "allowlist" and (entry.get("network") != "allowlist" or not entry.get("hosts")):
        raise ValueError(f"{entry.get('name')}: its catalog entry has no hosts to keep it to (network "
                         f"{wanted}): give --network full (the internet, not your network) or none")
    limits = {**recommended_limits(entry),
              **checked_limits({key: value for key, value in (("memory", memory), ("cpus", cpus), ("pids", pids),
                                                              ("tmp", tmp)) if value}, "--memory/--cpus/--pids/--tmp")}
    # --env NAME (required as the entry says, else optional), NAME=VALUE (never for a secret), NAME@SECRET.
    variables = entry_variables(entry, env)
    definition = {"image": f"{image}@{digest}", "network": network,
                  "mounts": entry_mounts(entry, mounts, create, out), **variables,
                  "description": str(entry.get("title") or entry.get("name")),
                  "source": str(entry.get("id") or entry.get("name"))}
    if "memory" in limits:
        definition["memory"] = limits["memory"]
    if "cpus" in limits:
        definition["cpus"] = cpus_number(limits["cpus"])
    if "pids" in limits:
        definition["pids"] = limits["pids"]
    if "tmp" in limits:
        definition["tmp"] = limits["tmp"]
    if network == "allowlist":
        definition["hosts"] = [str(host) for host in entry.get("hosts") or []]
    if allow_private:
        definition["allow_private"] = list(allow_private)
    if allow_tcp:
        definition["allow_tcp"] = list(allow_tcp)
    if vpn:
        definition["vpn"] = vpn
    if entry_tool_timeout(entry) is not None:
        definition["tool_timeout"] = entry_tool_timeout(entry)
    if "pid1" in entry:  # checked with the rest (validated_image): a value that is not "server" is refused
        definition["pid1"] = entry["pid1"]
    if "tmp_exec" in entry:  # checked with the rest: only true is taken
        definition["tmp_exec"] = entry["tmp_exec"]
    return validated_image(definition, entry["name"])


def check_signature(entry, unsigned=False, out=print):
    """Refuse ENTRY unless its catalog is signed by a trusted MCP key (catalog_signature); UNSIGNED takes an
    unsigned one anyway (a catalog of your own, say), never a signature that does not verify. OUT gets the note on
    an unsigned one; add() passes a list and says it once the server is added (an entry refused afterwards, one
    without a digest say, is not "added unsigned")."""
    signature = entry.get("_signature") or {"state": "unsigned", "reason": "no signature was read"}
    if signature["state"] == "good":
        return {"signed_by": signature["signer"]}
    if signature["state"] == "bad":
        raise ValueError(f"{entry.get('name')}: the catalog's signature does not verify ({signature['reason']}); "
                         "nothing of it is used")
    if not unsigned:
        raise ValueError(f"{entry.get('name')}: its catalog is not signed by a trusted key ({signature['reason']}), "
                         "so nobody vouched that it is built from its pinned sources and works with sandbox-ai; "
                         "--unsigned adds it anyway")
    out(f"sandbox-ai mcp: {entry.get('name')}: added unsigned ({signature['reason']})")
    return {"signed_by": None}


_progress = None


def progress_module():
    """progress.py: podman's pull output, bounded and readable (it took minutes in silence)."""
    global _progress
    if _progress is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_progress_for_mcp", HERE / "progress.py")
        with LOADING:
            if _progress is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _progress = loaded
    return _progress


def pull_image(image, local, out=print, popen=subprocess.Popen):
    """Pull IMAGE for the server LOCAL, what podman does said as it goes (its layers counted, the time it took);
    CalledProcessError when it fails (its last lines said)."""
    progress = progress_module()
    argv = ["podman", "pull", image]
    begun = time.monotonic()
    status = progress.stream(argv, f"pulling the image of {local}", progress.Progress(out=out, prefix="sandbox-ai mcp"),
                             popen=popen)
    LOG.ran("mcp", f"pull the image of {local}", argv, begun, returncode=status)
    if status != 0:
        raise subprocess.CalledProcessError(status, argv)


PULL_RESERVE = 256 << 20        # bytes left free beside a pulled image (the running starts write there too)
PULL_UNKNOWN = 1 << 30          # what an image is taken to need when its catalog names no size
PULL_FACTOR = 3                 # an image takes about this many times its rootfs archive (compressed) unpacked


def podman_storage():
    """Podman's image store (podman info: GraphRoot), else None (no Podman, no answer within 30 s)."""
    try:
        found = subprocess.run(["podman", "info", "--format", "{{.Store.GraphRoot}}"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    root = (found.stdout or "").strip().splitlines()[-1:] if found.returncode == 0 else []
    return root[0] if root and os.path.isabs(root[0]) else None


def check_pull_room(local, entry):
    """Before `mcp add` pulls the image of catalog ENTRY for LOCAL: a ValueError when Podman's image store (and the
    run directory under XDG_RUNTIME_DIR) has less room than the image needs (PULL_FACTOR times the size of its rootfs
    archive in the catalog, else PULL_UNKNOWN) and PULL_RESERVE more - said with the file system, its free bytes and
    inodes and the size needed. A pull that fills the disk fails half-way (Podman: no space left on device) and takes
    the room every running start writes its logs into with it (user report 2026-11-20)."""
    rootfs = entry.get("rootfs") if isinstance(entry.get("rootfs"), dict) else {}
    size = rootfs.get("size") if isinstance(rootfs.get("size"), int) and rootfs["size"] > 0 else None
    need = size * PULL_FACTOR if size else PULL_UNKNOWN
    S = statedirs_module()
    paths = [path for path in (podman_storage(), os.environ.get("XDG_RUNTIME_DIR")) if path]
    short = S.space_short(paths[:1], need + PULL_RESERVE, 1000) + S.space_short(paths[1:], PULL_RESERVE, 1000)
    if not short:
        return
    how = (f"about {S.human(need)} ({PULL_FACTOR} times its {S.human(size)} archive in the catalog)" if size else
           f"about {S.human(need)} (its catalog names no size)")
    raise ValueError(f"{local} is not added: not enough room to pull its image, which needs {how}, and "
                     f"{S.human(PULL_RESERVE)} stay free for the running starts: "
                     + "; ".join(text for _, text in short)
                     + ". Free space - sandbox-ai gc --check shows what can go (the app: Storage) - then add it again"
                     " (--no-pull when its image is there already)")


def add(name, local=None, mounts=(), env=(), network=None, memory=None, pids=None, catalogs=None,
        pull=True, fetch=None, out=print, vpn=None, unsigned=False, backend=None, cpus=None, tmp=None,
        allow_private=(), allow_tcp=(), built=None):
    """sandbox-ai mcp add: write the definition of catalog entry NAME into mcp.json, once its catalog's signature
    verified (or UNSIGNED): for Podman its image pulled by digest, for Bubblewrap its rootfs fetched, checked and
    unpacked (BACKEND podman, bwrap or both; default: Podman when it is there or the entry has no rootfs). The limits
    given, else those the entry recommends; a writable folder that is not there yet is created (once all is checked).
    BUILT ({"image", "digest", "record"}: mcp_build.build_and_add, `mcp add --build`): the entry has no image of its
    own, this one was built here from its source - added unsigned ("signed_by": null, "built": RECORD), whatever its
    catalog's signature says of the entry; nothing is pulled."""
    LOG.info("mcp", f"add asked: {name}" + (f" as {local}" if local else "") + (f", backend {backend}" if backend else ""))
    entries, problems = catalog_entries(catalogs, fetch)
    for problem in problems:
        LOG.warning("mcp", f"catalog not read: {problem}")
        out(f"sandbox-ai mcp: catalog not read: {problem}")
    entry = find_entry(entries, name)
    unsigned_note = []
    signed = check_signature(entry, unsigned, unsigned_note.append)
    if built is not None:
        # Built here (its catalog lists it, unpublished): the image is nobody's signed one, the note says so.
        entry = {**entry, "kind": "oci", "image": built["image"], "digest": built["digest"]}
        entry.pop("rootfs", None)
        signed, backend, pull = {"signed_by": None}, "podman", False
        unsigned_note = [f"sandbox-ai mcp: {entry.get('name')}: built here from {built['record']['repository']} at "
                         f"{built['record']['commit'][:12]}, not signed (no signature covers this image)"]
    backend = backend or ("podman" if shutil.which("podman") or not entry.get("rootfs") else "bwrap")
    if backend not in ("podman", "bwrap", "both"):
        raise ValueError("--backend is podman, bwrap or both")
    definition = {**definition_from_entry(entry, mounts, env, network, memory, pids, vpn, cpus=cpus, tmp=tmp,
                                          allow_private=allow_private, allow_tcp=allow_tcp),
                  **signed,
                  **({"catalog": entry["_catalog"]} if isinstance(entry.get("_catalog"), str) else {}),
                  **({"built": dict(built["record"])} if built is not None else {})}
    if built is not None:
        definition = validated_image(definition, entry["name"])
    if backend in ("bwrap", "both"):
        definition = validated_image({**definition, "rootfs": entry_rootfs(entry, fetch)}, entry["name"])
    if backend == "bwrap":
        definition.pop("image")
        pull = False
    local = local or entry["name"]
    if not re.fullmatch(MCP_NAME_PATTERN, local) or local in BUILTIN:
        raise ValueError(f"invalid or reserved server name: {local}")
    for line in network_said(local, definition, entry, network):
        out(line)
    recommended = {key: value for key, value in recommended_limits(entry).items()
                   if not {"memory": memory, "cpus": cpus, "pids": pids, "tmp": tmp}[key]}
    if recommended:
        out(f"sandbox-ai mcp: {local} gets the limits its catalog entry recommends: {limits_text(recommended)} "
            f"(sandbox-ai mcp set {local} changes them)")
    if "image" in definition or "rootfs" in definition:  # (a folder you chose with a risk: said, then added)
        check_image_mounts(local, definition, remember=False, warn=lambda line: out(f"sandbox-ai mcp: warning: {line}"))
    checked_behind_tor(local, definition, vpn, out, entry)
    if pull:
        check_pull_room(local, entry)
        pull_image(definition["image"], local, out)
    create_mounts(definition, out)
    write_definition(local, definition)
    for line in unsigned_note:
        out(line)
    form = " and ".join(([definition["image"]] if "image" in definition else []) +
                        ([f"rootfs {definition['rootfs']['sha256'][:12]} (Bubblewrap)"] if "rootfs" in definition else []))
    if built is not None:
        form += ", built here, not signed"
    LOG.info("mcp", f"{local} added ({form}; catalog {definition.get('catalog') or 'none'})")
    out(f"sandbox-ai mcp: {local} added ({form}); use it with --mcp {local}")
    return definition


def network_said(local, definition, entry=None, given=None):
    """What `mcp add` (and `mcp set`) say of the network the server LOCAL gets: where it reaches, a warning where it
    reaches your local network."""
    network = definition.get("network", "none")
    lines = []
    if network == "allowlist":
        lines.append(f"sandbox-ai mcp: {local} reaches only {', '.join(definition.get('hosts', []))} (its catalog "
                     "entry's hosts, nothing else - not your local network, no DNS of its own)")
    elif network == "full":
        why = "--network full" if given == "full" else "its catalog entry asks for it"
        lines.append(f"sandbox-ai mcp: {local} reaches the internet, not your local network ({why})")
    note = egress_note(local, definition, entry)
    if note:
        warning = network == "lan" or definition.get("allow_private") or definition.get("allow_tcp")
        lines.append(f"sandbox-ai mcp: {'warning: ' if warning else ''}{note}")
    return lines


def create_mounts(definition, out=print):
    """The writable host folders of DEFINITION that are not there yet, created (mount_host)."""
    for mount in definition.get("mounts", []):
        if mount.get("mode") == "rw" and not Path(mount["host"]).exists():
            mount_host(f"{mount['target']}={mount['host']}:rw", mount["host"], "rw", create=True, out=out)


def read_config():
    """(path, document) of the user's mcp.json ({} when there is none)."""
    path = config_file()
    try:
        document = json.loads(path.read_text()) if path.is_file() else {}
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ValueError(f"{path}: MCP servers must be an object of NAME: definition")
    return path, document


_filelock = None


def config_lock():
    """The lock of a read-modify-write of mcp.json (filelock.py, mcp.json.lock): `mcp add` and `mcp set` run side by
    side (the app's MCP page and a terminal) each read the file and wrote it back whole - one change was lost."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock_for_mcp", HERE / "filelock.py")
        with LOADING:
            if _filelock is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _filelock = loaded
    return _filelock.locked(config_file())


def write_definition(name, definition):
    """DEFINITION as server NAME in the user's mcp.json, replaced in one step (under config_lock: the other servers'
    definitions as they are now)."""
    with config_lock():
        path, document = read_config()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        document[name] = definition
        fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(document, stream, indent=2)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)  # (a full disk: no .mcp-* file left beside it)
            raise


SET_KEYS = ("memory", "cpus", "pids", "tmp", "network", "mounts", "env", "vpn", "recommended", "tool_timeout",
            "allow_private", "allow_tcp")


def set_definition(name, changes, entry=None, fetch=None, catalogs=None):
    """(old definition, new definition) of server NAME of the user's mcp.json with CHANGES ({memory, cpus, pids,
    network, mounts: [TARGET=HOST[:ro|rw]], env: [NAME | NAME=VALUE | NAME@SECRET] (all of its variables:
    entry_variables), vpn: PROFILE or None}; a key left out stays as it is; memory,
    cpus or pids None: back to the default), checked as `mcp add` checks them: the mounts and variables against the
    catalog entry (ENTRY, else read from the catalog it was added from), the limits, network and VPN as add takes them.
    Its image, rootfs, signature and catalog stay as they are. Nothing is written or created here (set() does)."""
    if set(changes) - set(SET_KEYS):
        raise ValueError(f"only {', '.join(SET_KEYS)} can be set")
    _path, document = read_config()
    if name not in document:
        if name not in BUILTIN and not name.startswith("team-"):
            raise ValueError(f"MCP server {name} is defined nowhere (not in {config_file()}, not built in, not a team's "
                             f"board): add it with sandbox-ai mcp add {name} (a server of a catalog: sandbox-ai mcp "
                             f"catalog lists them); one a project file's mcp_servers defines is changed there or on "
                             f"the board")
        where = "built in" if name in BUILTIN else "a team's board"
        raise ValueError(f"MCP server {name} is not in {config_file()} ({where}): only a server of yours can be set")
    old = validated({name: document[name]}, config_file())[name]
    new = json.loads(json.dumps(old))
    changes = dict(changes)
    if "tool_timeout" in changes:  # the agents' wait for one of its calls, for a server of either kind
        new.pop("tool_timeout", None)
        timeout = changes.pop("tool_timeout")
        if timeout is not None:
            new["tool_timeout"] = checked_tool_timeout(timeout, "--tool-timeout")
        if not changes:
            return old, (validated_image(new, f"MCP server {name}") if "image" in old or "rootfs" in old
                         else validated({name: new}, config_file())[name])
    if changes.pop("recommended", False):
        # The limits its catalog entry recommends, for those not given here as well.
        recommended = recommended_limits(entry or catalog_entry_of(name, old, fetch, catalogs))
        if not recommended:
            raise ValueError(f"MCP server {name}: its catalog entry recommends no limits (the defaults are its own)")
        changes = {**recommended, **changes}
    limits = checked_limits({key: changes[key] for key in LIMIT_KEYS if changes.get(key) not in (None, "")},
                            f"MCP server {name}")
    if "image" not in old and "rootfs" not in old:
        if set(changes) - set(LIMIT_KEYS):
            raise ValueError(f"MCP server {name} runs a command: its network, folders and VPN are its options in "
                             f"{config_file()}; only --memory, --cpus, --pids and --tmp can be set here")
        options = list(old.get("options", []))
        for key in LIMIT_KEYS:
            if key in changes:
                options = without_option(options, LIMIT_OPTIONS[key])
                if key in limits:
                    options += [LIMIT_OPTIONS[key], str(limits[key])]
        new["options"] = options
        new = validated({name: new}, config_file())[name]
        check_tmp_fits(default_limits(new), f"MCP server {name}")
        return old, new
    for key in LIMIT_KEYS:
        if key in changes:
            new.pop(key, None)
            if key in limits:
                new[key] = cpus_number(limits[key]) if key == "cpus" else limits[key]
    if "network" in changes:
        if changes["network"] not in NETWORKS:
            raise ValueError("--network must be none, full, allowlist or lan")
        new["network"] = changes["network"]
        new.pop("hosts", None)
        if changes["network"] == "allowlist":
            # Its catalog entry's hosts, as add takes them.
            source = entry or catalog_entry_of(name, old, fetch, catalogs)
            if source.get("network") != "allowlist" or not source.get("hosts"):
                raise ValueError(f"MCP server {name}: its catalog entry has no hosts to keep it to: --network full "
                                 "(the internet, not your network) or none")
            new["hosts"] = [str(host) for host in source["hosts"]]
        if changes["network"] == "none":
            for key in ("allow_private", "allow_tcp"):
                if not changes.get(key):
                    new.pop(key, None)
    for key in ("allow_private", "allow_tcp"):
        if key in changes:
            new.pop(key, None)
            if changes[key]:
                new[key] = list(changes[key])
    if "vpn" in changes:
        new.pop("vpn", None)
        if changes["vpn"]:
            launcher().load_vpn_module().check_name(changes["vpn"])
            new["vpn"] = changes["vpn"]
    if "mounts" in changes or "env" in changes:
        entry = entry or catalog_entry_of(name, old, fetch, catalogs)
        if "mounts" in changes:
            new["mounts"] = entry_mounts(entry, changes["mounts"])
        if "env" in changes:
            # All of its variables (as add takes them): NAME, NAME=VALUE (never for a secret one), NAME@SECRET.
            new = {**{key: value for key, value in new.items() if key not in ENV_KEYS},
                   **entry_variables(entry, changes["env"])}
    return old, validated_image(new, f"MCP server {name}")


def set_changes(a):
    """The CHANGES of set_definition from `mcp set`'s arguments A (a limit given as "default": back to the default)."""
    changes = {}
    for key in LIMIT_KEYS:
        value = getattr(a, key)
        if value is not None:
            changes[key] = None if str(value).strip().lower() == "default" else value
    if a.network:
        changes["network"] = a.network
    allowed = {"allow_private": getattr(a, "allow_private", None), "allow_tcp": getattr(a, "allow_tcp", None)}
    if getattr(a, "no_allow", False):
        if any(values is not None for values in allowed.values()):
            raise ValueError("--no-allow takes your network away again; give it or --allow-private/--allow-tcp, not "
                             "both")
        changes.update(allow_private=[], allow_tcp=[])
    for key, values in allowed.items():
        if values is not None:
            changes[key] = list(values)
    for key, values, none in (("mounts", a.mount, a.no_mounts), ("env", a.env, a.no_env)):
        if values is not None and none:
            raise ValueError(f"--no-{key} takes all of them away; give it or the new ones, not both")
        if values is not None or none:
            changes[key] = list(values or [])
    if a.vpn and a.no_vpn:
        raise ValueError("--vpn PROFILE or --no-vpn, not both")
    if a.vpn or a.no_vpn:
        changes["vpn"] = a.vpn or None
    if getattr(a, "recommended", False):
        changes["recommended"] = True
    if getattr(a, "tool_timeout", None) is not None:
        changes["tool_timeout"] = parse_tool_timeout(a.tool_timeout)
    if not changes:
        raise ValueError("give what to change: --memory, --cpus, --pids, --tmp, --recommended, --network, "
                         "--allow-private, --allow-tcp, --no-allow, --mount, --env, --vpn or --tool-timeout")
    return changes


def without_option(options, option):
    """OPTIONS without OPTION (OPTION VALUE and OPTION=VALUE)."""
    kept, skip = [], False
    for token in options:
        if skip:
            skip = False
        elif token == option:
            skip = True
        elif not token.startswith(option + "="):
            kept.append(token)
    return kept


def catalog_entry_of(name, definition, fetch=None, catalogs=None):
    """The catalog entry server NAME was added from (its definition's "source" in its "catalog", or CATALOGS): which
    folders and variables it takes. ValueError when the catalog cannot be read or no longer has it."""
    source = definition.get("source")
    if not isinstance(source, str):
        raise ValueError(f"MCP server {name}: it does not say which catalog entry it is, so the folders and variables "
                         "it takes are not known: add it again (sandbox-ai mcp add)")
    sources = catalogs or ([("c0", definition["catalog"])] if isinstance(definition.get("catalog"), str) else None)
    entries, problems = catalog_entries(sources, fetch)
    bare = source.rpartition(":")[2]
    matches = [entry for _prefix, entry in entries if entry.get("id") == source or entry.get("name") == bare]
    if not matches:
        raise ValueError(f"MCP server {name}: its catalog entry {source} cannot be read now"
                         + (f" ({'; '.join(problems)})" if problems else "") + ": the folders and variables it takes "
                         "are not known")
    # As `mcp add` checked it: a server added from a signed catalog takes the entry only with a good signature by a
    # trusted key again (the catalog may have changed since); one that does not verify never; one added unsigned may
    # stay unsigned.
    check_signature(matches[0], unsigned=not definition.get("signed_by"), out=lambda line: None)
    return matches[0]


def set_server(name, changes, stop_running=False, out=print, fetch=None, catalogs=None):
    """sandbox-ai mcp set NAME: CHANGES (set_definition) written into mcp.json, a writable folder that is not there yet
    created; its running instances are stopped with STOP_RUNNING (a start that uses it starts it again with these
    settings), else they run on as they are until stopped - a start meanwhile gets an instance of its own with the
    new definition (start_instance). {"server", "changed": [keys], "definition", "running": [instances], "stopped": [...],
    "message"}."""
    old, new = set_definition(name, changes, fetch=fetch, catalogs=catalogs)
    changed = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))
    result = {"server": name, "changed": changed, "definition": new, "running": [], "stopped": []}
    if not changed:
        result["message"] = f"MCP server {name}: nothing changed"
        out(result["message"])
        return result
    if "vpn" in changed:
        checked_behind_tor(name, new, new.get("vpn"), out)
    if {"network", "hosts", "allow_private", "allow_tcp"} & set(changed):
        for line in network_said(name, new, given=new.get("network")):
            out(line)
    if "mounts" in changed and ("image" in new or "rootfs" in new):  # (a folder you chose with a risk: said, set)
        check_image_mounts(name, new, remember=False, warn=lambda line: out(f"sandbox-ai mcp: warning: {line}"))
    create_mounts(new, out)
    write_definition(name, new)
    running = instances(name)
    result["running"] = running
    text = f"MCP server {name}: {', '.join(changed)} set; limits now {limits_text(default_limits(new))}"
    if running and stop_running:
        for instance in running:
            stop(instance)
        result["stopped"] = running
        text += f". Stopped {', '.join(running)}: the next start that uses it starts it with these settings"
    elif running:
        text += (f". It runs ({', '.join(running)}) as it was until it is stopped; a start meanwhile gets an instance "
                 f"of its own with these settings (sandbox-ai mcp stop {name} ends the old one)")
    result["message"] = text
    out(text)
    return result


def catalog_label(source):
    """A catalog's name for people, from its URL or path: REPO (github.com/OWNER) for a catalog on GitHub,
    NAME (sandbox-ai's copy) for the copy in sandbox-ai's own tree, else the address itself."""
    source = str(source)
    github = re.fullmatch(r"https://(?:raw\.githubusercontent\.com|github\.com)/([^/]+)/([^/]+)/.*", source)
    if github:
        return f"{github[2]} (github.com/{github[1]})"
    path = Path(source)
    if not source.startswith("https://") and path.name == "catalog.json" and \
            path.parent.name in LOCAL_CATALOGS.values() and path.parent.parent.name == "projects":
        return f"{path.parent.name} (sandbox-ai's copy)"
    return source.removeprefix("https://")


def origin(name, definition):
    """Where server NAME's DEFINITION comes from, for mcp list: {"catalog": a name for people, "catalog_source":
    the catalog's URL or path (None if not from a catalog or not known), "signed": True (its catalog's signature
    verified), False (added --unsigned, or built here), None (not from a catalog, or not known), and for an image built
    here (mcp add --build) "built": {commit, repository, path, archive_sha256, at}}. `mcp add` records the catalog
    since 0.1.78; an image server added before says so."""
    if name in BUILTIN and definition == BUILTIN[name]:
        return {"catalog": "built-in", "catalog_source": None, "signed": None}
    if "image" not in definition and "rootfs" not in definition:
        return {"catalog": "team" if name.startswith("team-") else "own (mcp.json)", "catalog_source": None,
                "signed": None}
    signed = (definition["signed_by"] is not None) if "signed_by" in definition else None
    # Built here (mcp add --build): never signed, whatever is said of it - and said so wherever it is shown.
    built = {"built": dict(definition["built"]), "signed": False} if isinstance(definition.get("built"), dict) else {}
    if isinstance(definition.get("catalog"), str):
        return {"catalog": catalog_label(definition["catalog"]), "catalog_source": definition["catalog"],
                "signed": signed, **built}
    return {"catalog": "unknown (added before 0.1.78)", "catalog_source": None, "signed": signed, **built}


def config_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "mcp.json"


def team_definitions():
    """{team-NAME: the board of team NAME} of every team (team.py); a team that cannot be read is left out, never
    a reason for another start to fail."""
    try:
        spec = importlib.util.spec_from_file_location("team_for_mcp", HERE / "team.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.definitions()
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def definitions(project=None):
    result = {**BUILTIN, **team_definitions()}
    path = config_file()
    if path.is_file():
        try:
            document = json.loads(path.read_text())
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from None
        result.update(validated(document, path))
    if project:
        result.update(validated(project.get("mcp_servers", {}), "project configuration"))
    return result


def pod_name(name):
    """Fixed pod name of MCP server NAME, so a stale pod can be found and removed."""
    return f"agent-mcp-{name}"


# What a server's state needs of the launcher, without loading it (`sandbox-ai list` and `mcp list`, which the app
# asks every few seconds): through the launcher once that is loaded, else the same here (a test keeps both the same).
_procinfo = None


def socket_dir(name):
    """launcher().mcp_socket_dir(NAME): the host directory holding the Unix socket of MCP server NAME."""
    if _launcher is not None:
        return _launcher.mcp_socket_dir(name)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "mcp" / name / "sock"


def process_cmdline(pid, start, proc):
    """launcher().process_cmdline."""
    global _procinfo
    if _launcher is not None:
        return _launcher.process_cmdline(pid, start, proc)
    if _procinfo is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_procinfo_for_mcp", HERE / "procinfo.py")
        with LOADING:
            if _procinfo is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _procinfo = loaded
    return _procinfo.recorded_cmdline(pid, start, proc)


def state_dir(name):
    return socket_dir(name).parent


def load_state(name):
    """The state of server NAME's sandbox ({} when there is none, or it is damaged: valid JSON of another shape -
    a list, a number - ended `mcp list` for every server, and `mcp stop`, with an AttributeError)."""
    try:
        state = json.loads((state_dir(name) / "mcp.json").read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(name, data):
    directory = state_dir(name)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, directory / "mcp.json")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)  # (a full disk: no .mcp-* file left in the server's state folder)
        raise


def status(name):
    """running: the supervisor of this server is alive (not a reused PID).

    Decided by the supervisor alone: the socket directory is writable by the
    untrusted server, which could otherwise fake being stopped."""
    state = load_state(name)
    cmdline = process_cmdline(state.get("pid"), state.get("pid_start"), PROC)
    directory = os.fsencode(str(socket_dir(name)))
    return "running" if any(arg == directory or arg == directory + b"/server.sock" for arg in cmdline) else "stopped"


# A line of a server's log that says how its own Tor bootstraps (vpn.mcp_wrapper's "sandbox-ai mcp: vpn: ...", a
# command server's own start "agent-sandbox: vpn: ..."): said by the start that waits for it (wait_for_socket).
TOR_PROGRESS = re.compile(r"(?:sandbox-ai mcp|agent-sandbox): (vpn: (?:tor: |Tor bootstrap ).{1,400})")


def wait_for_socket(path, log, process, timeout=READY_TIMEOUT, ready=None, name=None, clock=time.monotonic,
                    sleep=time.sleep, say=None):
    """Until the server's socket PATH is there (READY: macOS, see machine_ready), or the sandbox ended. Past TIMEOUT
    it waits as long as the server's LOG says new lines (progress.StartWatch: a server behind a Tor of its own
    bootstraps for minutes), and what its Tor says of its bootstrap is said here too, as "MCP server NAME: vpn:
    tor: bootstrapped 69% ..." (SAY, default standard error: the start's progress shows it)."""
    progress = progress_module()
    deadline = clock() + timeout
    ready = ready or path.exists
    watch = progress.StartWatch(log, clock, cap=max(progress.START_CAP, timeout))
    position = log.stat().st_size if name and log.exists() else 0
    say = say or (lambda text: print(text, file=sys.stderr, flush=True))

    def relay():
        nonlocal position
        try:
            with open(log, "rb") as stream:
                stream.seek(position)
                chunk = stream.read(1 << 16)
        except OSError:
            return
        complete = chunk[:chunk.rfind(b"\n") + 1]
        position += len(complete)
        for raw in complete.decode(errors="replace").splitlines():
            found = TOR_PROGRESS.fullmatch(raw.strip())
            if found:
                say(f"agent-sandbox: MCP server {progress.shown(name, 80)}: {progress.shown(found[1], 300)}")
    while not ready():
        if name:
            relay()
        if process.poll() is not None:
            tail = launcher().printable(log.read_text(errors="replace")[-3000:]) if log.exists() else ""
            raise RuntimeError(f"MCP server sandbox failed to start (status {process.returncode}):\n{tail}")
        over = watch.over(clock() > deadline)
        if over:
            raise RuntimeError(f"MCP server {name + ' ' if name else ''}did not become ready: {watch.why(over)}; see "
                               f"{log}")
        sleep(0.2)


def machine_ready(name, a, runner=None):
    """macOS: {"ready": probe} for wait_for_socket - the server's socket lies in a Podman volume in podman machine's
    VM (agent_sandbox.mcp_volume), asked for through podman exec into the server's container; {} elsewhere."""
    if not launcher().on_machine(a):
        return {}
    record = {"pod": pod_name(name), "podman_url": a.podman_url}

    def probe():
        command = launcher().machine_exec(record, ["--internal-attach-probe",
                                                   f"{launcher().MCP_SERVE_DIR}/server.sock"], interactive=False)
        try:
            return (runner or subprocess.run)(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                              check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return {"ready": probe}


def definition_digest(definition, backend, podman_url=None):
    """Identity of a server instance: everything that decides what it can reach (its tool_timeout is the agents'
    setting, not the server's: a change of it makes no instance of its own)."""
    definition = {key: value for key, value in definition.items() if key != "tool_timeout"}
    return hashlib.sha256(json.dumps([definition, backend, podman_url], sort_keys=True).encode()).hexdigest()


@contextlib.contextmanager
def server_lock(name, timeout=180):
    """Serialize start and stop of one server: a second agent waits until the first has started it,
    then reuses it, instead of starting another server on the same HOME."""
    if not re.fullmatch(INSTANCE_PATTERN, name):
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


def network_of(definition):
    """(reaches the network, its own VPN profile or None) of the server DEFINITION: an image's "network" and "vpn", a
    command's options (--offline, --vpn), a helper (it reaches its provider; its VPN is the one it is started with)."""
    if "image" in definition or "rootfs" in definition:
        return definition.get("network", "none") != "none", definition.get("vpn")
    if "helper" in definition:
        return True, None
    l = launcher()
    options = l.canonical_options(definition.get("options", []))
    # --offline, or --profile offline (modes.mcp_has_network reads both, as the security modes do).
    return l.MODES.mcp_has_network(definition, l), option_value(options, "--vpn")


class LentVpn(str):
    """The name of the VPN profile a server reaches the network through when it borrows its start's tunnel of it
    (vpn.lends: a WireGuard key, Mullvad's too, or a persistent Tailscale node runs in one sandbox at a time, so the
    start lends its tunnel instead of the server bringing up a second one of the same key). A str: compared, saved
    and shown as the profile's name. LENT_BY: the start's run, None while the start does not run (its checks)."""

    def __new__(cls, profile, lent_by=None):
        value = super().__new__(cls, profile)
        value.lent_by = lent_by
        return value


def lent_of(vpn):
    """(True, the run) for a VPN the server borrows (LentVpn), else (False, None). Told by its kind's name and its
    attribute, not by the class: the launcher and this file are each loaded by path, and another copy of this module
    (the launcher's own) makes its LentVpn - a str with lent_by all the same (a plain str has no such attribute)."""
    if isinstance(vpn, str) and type(vpn).__name__ == "LentVpn" and hasattr(vpn, "lent_by"):
        run = vpn.lent_by
        return True, run if isinstance(run, str) else None
    return False, None


def vpn_conflicts(name, definition, vpn, tor, lent=False):
    """Why the command server NAME cannot be started behind the start's VPN (its own start would refuse it, after the
    agent's start had begun), or None: its own resolvers (--nameserver), with Tor or a lent tunnel (LENT) no
    connection proxy (--no-proxy), with Tor the host's ports without --vpn-bypass-host-ports."""
    if "command" not in definition:
        return None
    options = launcher().canonical_options(definition.get("options", []))
    given = {token.split("=", 1)[0] for token in options}
    if "--nameserver" in given:
        return (f"MCP server {name} names resolvers of its own (--nameserver), which conflicts with the start's "
                f"--vpn {vpn}: drop it from its options (the VPN's resolvers answer)")
    if lent and "--no-proxy" in given:
        return (f"MCP server {name} has --no-proxy, and it reaches the network through the start's tunnel of {vpn}, "
                "lent to it: the connection proxy is what hands its connections to the tunnel's relay. Drop it from "
                "its options")
    if not tor and "--vpn-bypass-tcp" in given:
        return (f"MCP server {name} has --vpn-bypass-tcp, which goes with a Tor profile only, and the start's --vpn "
                f"{vpn} is none: drop it from its options")
    if tor and "--no-proxy" in given:
        return (f"MCP server {name} has --no-proxy, and behind the start's Tor profile {vpn} the connection proxy is "
                "what hands its connections to Tor: drop it from its options")
    if tor and "--vpn-bypass-host-ports" not in given:
        # Host ports the options give outright, or bring along (--llm of this host's loopback, a Git login of a
        # server on localhost): read as its own start reads them.
        ports = []
        try:
            parsed = launcher().parse_args([definition.get("agent", "codex"), *options])
            ports = list(launcher().host_ports(parsed))
        except (SystemExit, ValueError, OSError, KeyError, AttributeError):
            ports = ["?"] if "--allow-host-port" in given else []
        if ports:
            return (f"MCP server {name} reaches the host's ports ({', '.join(map(str, ports))}: --allow-host-port, or "
                    f"what opens one), which lie past the start's Tor profile {vpn}: drop what opens them, or give it "
                    "--vpn-bypass-host-ports if you mean it")
    return None


def server_options(definition, vpn):
    """The options a command server's own start gets: --vpn-bypass-host-ports and --vpn-bypass-tcp only with a VPN
    in front (given to this start, or its own --vpn); without one its start refused them and never came up."""
    options = list(definition.get("options", []))
    if vpn or option_value(launcher().canonical_options(options), "--vpn"):
        return options
    kept, skip = [], False
    for token in options:
        if skip:
            skip = False
            continue
        name = token.split("=", 1)[0]
        if name == "--vpn-bypass-tcp" and "=" not in token:
            skip = True  # its value follows
        if name not in ("--vpn-bypass-host-ports", "--vpn-bypass-tcp"):
            kept.append(token)
    return kept


# --- the one place that gives an MCP server its network --------------------------------------------------------
#
# server_network() decides it for every kind of server and every way one comes up (a start's --mcp, a helper, mcp
# start [--vpn] [--for], mcp attach [--live], a team's member, a restart, a server shared by several starts); every
# command that starts a server takes the plan it returns (image_command, connection_command, the launcher's
# options) and nothing else names a network. tests/test_mcp_vpn.py holds every path, server kind and VPN kind to it
# and checks that no other code in this file builds a network option. After the start verify_network() proves what
# the plan asked for (vpn.check_vpn_container, vpn.check_pod_vpn) and stops a server that does not have it.

PLAN_KINDS = ("image", "command", "helper")

# How the signed catalogs' servers fare behind a Tor profile. Tor is transparent there: every TCP connection goes to
# tor's TransPort, which accepts it at once and only then has an exit try the destination; every lookup on port 53
# goes to tor's DNSPort, which answers A, AAAA and PTR questions only; UDP, ICMP and raw sockets do not get past the
# kill switch. A server that only speaks TCP (HTTPS APIs, browsers, whatweb, WHOIS, MTProto) works; one that needs
# other DNS record types works in part; one that judges a port by whether its connection is accepted (a port scan)
# would only be told lies. Every server of both catalogs is held to this in tests/test_mcp_tor_matrix.py. Keyed by
# the catalog entry's id (a definition's "source"); a catalog entry's own "tor" ({"support": "limited"|"no", "note":
# TEXT}) is taken over this table when it has one. Not listed: works behind Tor (or reaches no network at all).
TOR_LIMITS = {
    "hub:nmap": ("no", "its connect scans would see every port open: tor's TransPort accepts each connection before "
                       "an exit tries the target, so every port state it reported through Tor would be wrong"),
    "hub:nuclei": ("limited", "HTTP templates work; DNS templates of record types other than A, AAAA and PTR get no "
                              "answer (tor's resolver answers only those), and templates of the network protocol see "
                              "every port open (tor's TransPort accepts each connection first)"),
    "hub:dnstwist": ("limited", "only A and AAAA lookups work (tor's resolver answers A, AAAA and PTR only): its MX "
                                "and NS checks get no answer, and a scan of hundreds of permutations through Tor is "
                                "slow"),
    "mcp:osint-mcp": ("limited", "dns_lookup of record types other than A, AAAA and PTR (MX, TXT, NS, SOA, CAA ...), "
                                 "email_security and the DNS parts of recon_domain and compare_domains get no answer: "
                                 "tor's resolver answers A, AAAA and PTR only; RDAP, crt.sh, Wayback, RIPEstat, "
                                 "InternetDB, HTTP headers and TLS work"),
    "mcp:openosint": ("limited", "search_dns gets no answer for MX, NS, TXT, CNAME, SOA and DMARC (tor's resolver "
                                 "answers A, AAAA and PTR only); WHOIS and the web lookups work, though some services "
                                 "refuse Tor exits"),
    "mcp:searxng": ("limited", "SEARXNG_URL must be reachable from a Tor exit: a public instance or an .onion "
                               "address; one in your local network or on this host cannot be reached through Tor"),
}
TOR_SUPPORT = ("yes", "limited", "no")


def tor_support(definition, entry=None):
    """(support, note) of the server DEFINITION (or catalog ENTRY) behind a Tor profile: ("yes", "") when it works
    there; ("limited", NOTE) when some of its tools do not; ("no", WHY) when it cannot work behind Tor at all
    (TOR_LIMITS, or the catalog entry's own "tor")."""
    own = (entry or {}).get("tor") if isinstance(entry, dict) else None
    if isinstance(own, dict) and own.get("support") in TOR_SUPPORT:
        return own["support"], launcher().printable(str(own.get("note") or ""))[:600]
    source = str((entry or {}).get("id") or (definition or {}).get("source") or "")
    return TOR_LIMITS.get(source, ("yes", ""))


def tor_problem(name, definition, profile_name, entry=None):
    """None, or why the server NAME (DEFINITION) cannot work behind the Tor profile PROFILE_NAME (ValueError's
    text); a server that works in part is said by tor_note."""
    support, why = tor_support(definition, entry)
    if support != "no":
        return None
    return (f"MCP server {name} cannot work behind the Tor profile {profile_name}: {why}. Use it without Tor, or "
            f"behind a VPN profile that is no Tor profile")


def tor_note(name, definition, entry=None):
    """What does not work of the server NAME behind Tor ("" when everything does)."""
    support, note = tor_support(definition, entry)
    return f"MCP server {name} behind Tor: {note}" if support == "limited" and note else ""


def checked_behind_tor(name, definition, vpn, out, entry=None):
    """mcp add --vpn and mcp set --vpn: a server that cannot work behind a Tor profile VPN is refused, one that works
    in part is added with what does not work said (OUT)."""
    if not vpn:
        return
    try:
        kind = launcher().load_vpn_module().load_profile(vpn).get("type")
    except (OSError, ValueError):
        return  # (no such profile: the server's start says so)
    if kind != "tor":
        return
    problem = tor_problem(name, definition, vpn, entry)
    if problem:
        LOG.warning("mcp", f"refused: {problem}")
        raise ValueError(problem)
    note = tor_note(name, definition, entry)
    if note:
        LOG.info("mcp", note)
        out(f"sandbox-ai mcp: {note}")


def server_kind(definition):
    return "helper" if "helper" in definition else "image" if "image" in definition or "rootfs" in definition \
        else "command"


def server_network(name, definition, vpn=None, vpn_module=None):
    """The network plan of the MCP server NAME (DEFINITION) started behind VPN (the start's VPN, vpn_for_servers, or
    mcp start --vpn; None: only what the definition says): {"server", "kind" (PLAN_KINDS), "reaches" (the network at
    all), "vpn" (the profile it leaves through, or None), "vpn_type", "own" (that VPN is the definition's own),
    "definition" (as started: with the VPN, an image server in its Podman form), "network" (an image server's
    connections: its one podman --network option), "vpn_container" (theirs), "launcher" (what a command server's or
    helper's launcher gets for it), "lent" (VPN is a LentVpn: the start's tunnel, borrowed - an image server's VPN
    container and a command server's or helper's launcher borrow it from "lent_by", the start's run)}. ValueError for
    a server that would reach the network outside that VPN: a VPN for a server without network, a server under
    Bubblewrap only, a profile that is no way out (vpn.egress_problem), a borrowed tunnel of a profile not lent."""
    l = launcher()
    kind = server_kind(definition)
    reaches, own = network_of(definition)
    chosen = vpn if vpn is not None else own
    lent, lent_by = lent_of(vpn)
    if chosen is not None and vpn_module is None:
        vpn_module = l.load_vpn_module()  # (once; VPN_MODULE where the caller has it already)
    if vpn is not None:
        vpn_module.check_name(vpn)
    plan = {"server": name, "kind": kind, "reaches": bool(reaches), "vpn": chosen, "vpn_type": None,
            "own": chosen is not None and chosen == own and vpn in (None, own) and not lent, "definition": definition,
            "network": None, "vpn_container": None, "launcher": [], "lent": lent, "lent_by": lent_by, "egress": None}
    if lent and not reaches:
        raise ValueError(f"MCP server {name} reaches no network: it borrows no tunnel")
    if lent_by is not None:
        vpn_module.check_run(lent_by)
    if kind == "image":
        if chosen is not None:
            if "image" not in definition:
                raise ValueError(f"MCP server {name} runs under Bubblewrap only, where no VPN can be put in front of "
                                 f"it: with the VPN {chosen} it would leave from your own address. Add its Podman form "
                                 "(sandbox-ai mcp add ... --backend podman)")
            # Its Podman form: the VPN is a container's network namespace (a rootfs beside it is not used).
            podman_form = {key: value for key, value in definition.items() if key != "rootfs"}
            plan["definition"] = validated_image({**podman_form, "vpn": chosen}, f"MCP server {name}")
            plan["vpn_container"] = vpn_module.mcp_container(name)
            plan["network"] = f"--network=container:{plan['vpn_container']}"
        else:
            # full: pasta without the host's loopback or forwarded ports, as the agents' own sandboxes.
            plan["network"] = f"--network={l.POD_NETWORK}" if reaches else "--network=none"
        # What it reaches through that network (mcp_egress.py): the internet, not your network - or only its hosts,
        # or your network too where you chose it. Behind a VPN its VPN container loads the rules.
        plan["egress"] = egress_of(name, plan["definition"], None if chosen is not None else l.POD_NETWORK)
    elif vpn is not None:
        # Behind the start's VPN (or mcp start --vpn): a failed check stops the server, whatever its options say;
        # behind the start's tunnel, lent: borrowed from the start's run (a server's options cannot name that).
        plan["launcher"] = ["--vpn", str(vpn), "--vpn-check", "stop",
                            *(["--vpn-lent-by", lent_by] if lent_by is not None else [])]
    if chosen is not None and reaches:
        try:
            profile = vpn_module.load_profile(chosen)
        except (OSError, ValueError) as exc:
            if lent:
                raise ValueError(f"MCP server {name}: the VPN profile {chosen} of the start's tunnel: {exc}") from None
            LOG.debug("mcp", f"{name}: the VPN profile {chosen} is not read here (its own start refuses it)", exc)
            profile = None  # a missing or damaged profile: the VPN's own start refuses it (nothing starts)
        if profile is not None:
            plan["vpn_type"] = profile.get("type")
            problem = vpn_module.egress_problem(profile, name)
            if problem:
                raise ValueError(problem)
            if lent and not vpn_module.lends(profile):
                raise ValueError(f"MCP server {name}: a start does not lend its tunnel of {chosen} "
                                 f"({profile.get('type')}): the server brings up one of its own")
            if plan["vpn_type"] == "tor":
                # A server that cannot work behind Tor is refused, not started broken (TOR_LIMITS); what does not
                # work of one that works in part is said when it starts (begin_running).
                problem = tor_problem(name, definition, chosen)
                if problem:
                    raise ValueError(problem)
                plan["tor_note"] = tor_note(name, definition)
    return plan


def egress_of(name, definition, network=None):
    """The egress of the image server NAME (DEFINITION) for its plan (mcp_egress.egress_config), or None without
    network; NETWORK: its connections' podman network option (without a VPN)."""
    return egress_module().egress_config(definition, state_dir(name) / "egress-hosts", network)


def checked_plan(plan, kind):
    """(definition, network option) of PLAN, a plan of server_network for a server of KIND; ValueError for anything
    else - a definition handed in without its plan, or a plan whose VPN its network option does not carry."""
    if not isinstance(plan, dict) or plan.get("kind") != kind or "definition" not in plan:
        raise ValueError(f"an MCP server's command is built from its network plan (mcp.server_network), "
                         f"not from {type(plan).__name__}")
    if plan.get("lent") and (not plan.get("vpn") or not isinstance(plan.get("lent_by"), str)):
        # Borrowed from a start that does not run (its checks, a stopped start): nothing to borrow from yet.
        raise ValueError(f"MCP server {plan.get('server')} borrows the tunnel of its start's --vpn {plan.get('vpn')}, "
                         "which lends it only while it runs: the server comes up with that start")
    network = plan.get("network")
    if kind == "image":
        behind = f"--network=container:{plan.get('vpn_container')}"
        if plan.get("vpn") and network != behind or not plan.get("vpn") and network not in (
                "--network=none", f"--network={launcher().POD_NETWORK}") \
                or plan["definition"].get("vpn") != plan.get("vpn"):
            raise ValueError(f"MCP server {plan.get('server')}: its network plan does not hold ({network})")
        wanted = egress_of(plan.get("server"), plan["definition"], None if plan.get("vpn") else
                           launcher().POD_NETWORK) if plan.get("reaches") else None
        if plan.get("egress") != wanted:
            # A server with network reaches it through its egress rules or not at all.
            raise ValueError(f"MCP server {plan.get('server')}: its network plan does not hold (its egress)")
    elif plan.get("vpn") and not plan.get("own") and plan.get("launcher") != [
            "--vpn", plan["vpn"], "--vpn-check", "stop", *(["--vpn-lent-by", plan.get("lent_by")] if plan.get("lent")
                                                           else [])]:
        raise ValueError(f"MCP server {plan.get('server')}: its network plan does not hold (behind {plan['vpn']})")
    return plan["definition"], network


def connection_command(plan, engine="podman"):
    """What the bridge runs for each connection of an image server: its podman run line (image_command), behind a
    VPN through vpn.py's guard, which creates the container in the checked VPN container's namespace (by ID) and
    starts it only then (vpn.mcp_guard); with network but no VPN through mcp_egress.py's guard, which creates it,
    loads its egress rules into its namespace and starts it only then."""
    command = image_command(plan, engine)
    if not plan["vpn"]:
        if not plan.get("egress"):
            return command  # (no network)
        # The rules in its namespace before it runs (mcp_egress.guard): the internet, not your network.
        return [sys.executable, "-I", str(HERE / "mcp_egress.py"), "--internal-egress-guard",
                egress_module().encoded(plan["egress"]), "--", *command]
    return [sys.executable, "-I", str(HERE / "vpn.py"), "--internal-mcp-guard", plan["vpn_container"], "--", *command]


def verify_network(name, plan, a, podman_url=None, run=None):
    """After the start of the server NAME (and before one running is used again): what PLAN asked for, proven
    with Podman - an image server's VPN container runs with its kill switch (each connection is checked again by
    vpn.mcp_guard), a command server's or helper's pod has its VPN container with the kill switch, and the server's
    container shares its network namespace (vpn.check_pod_vpn). ValueError when not. Nothing to prove without a
    VPN (no network, or the network the plan gives it)."""
    if not plan.get("vpn") or not plan.get("reaches"):
        return
    vpn_module = launcher().load_vpn_module()
    if plan["kind"] == "image":
        vpn_module.check_vpn_container(["podman"], plan.get("vpn_container") or vpn_module.mcp_container(name),
                                       plan["vpn"], run)
        return
    podman = launcher().podman_command(SimpleNamespace(podman_url=podman_url if podman_url is not None
                                                       else getattr(a, "podman_url", None)))
    vpn_module.check_pod_vpn(podman, pod_name(name), plan["vpn"], run)


def verified_or_stopped(name, plan, a, podman_url=None):
    """verify_network for the server NAME that was just started: one that does not have its VPN is stopped, and
    the start is refused."""
    try:
        verify_network(name, plan, a, podman_url)
    except ValueError as exc:
        LOG.warning("mcp", f"{name}: stopped again - it does not run behind the VPN {plan['vpn']}", exc)
        stop_locked(name)
        raise ValueError(f"MCP server {name} was stopped: it does not run behind the VPN {plan['vpn']} as it must "
                         f"({exc})") from None


def vpn_for_servers(a, names, profile_type=None):
    """{name: the VPN to start the server NAME behind for this start, or None (none needed: its own, or no network)}
    for a start A with --vpn: the servers reach the network behind the start's VPN too - a server outside it (its
    own sandbox, its own network) reached the internet from your address while the agent went through the VPN
    (a Tor start's browser server showed your own IP). A server with a VPN of its own keeps it; with a Tor profile
    it must be Tor too. ValueError for a server that would leave outside Tor. PROFILE_TYPE: the start's profile type
    (read when not given).

    A profile that runs in one sandbox at a time (vpn.lends: a WireGuard key, Mullvad's too, a persistent Tailscale
    node) is never brought up twice: a server behind the start's, or whose own definition names that same profile,
    borrows the start's tunnel (a LentVpn, the start's run) - one key, one tunnel, the agent and its servers behind
    it. A server whose own definition names another profile keeps a tunnel of its own of that one."""
    if not getattr(a, "vpn", None):
        return {name: None for name in names}
    servers = {**definitions(getattr(a, "project_document", None)), **(getattr(a, "helper_definitions", None) or {})}
    vpn_module = launcher().load_vpn_module()
    profile = getattr(a, "vpn_profile", None) or vpn_module.load_profile(a.vpn)
    profile_type = profile_type or profile.get("type")
    lent = LentVpn(a.vpn, getattr(a, "run_id", None)) if vpn_module.lends(profile) else None
    found = {}
    for name in names:
        definition = servers.get(name)
        if definition is None:
            found[name] = None  # (unknown: ensure_running says so)
            continue
        reaches, own = network_of(definition)
        if not reaches:
            found[name] = None
        elif own:
            if profile_type == "tor" and own != a.vpn:
                try:
                    own_type = vpn_module.load_profile(own).get("type")
                except (OSError, ValueError):
                    own_type = None
                if own_type != "tor":
                    raise ValueError(f"MCP server {name} reaches the network through the VPN {own}, not through Tor: "
                                     f"with --vpn {a.vpn} (Tor) every connection must go through Tor. Give it a Tor "
                                     f"profile (sandbox-ai mcp add {name} ... --vpn {a.vpn}) or start without it")
            # Its own profile is the start's, which runs once at a time: the start's tunnel, borrowed (not the same
            # key twice); another profile of its own: a tunnel of its own of that one.
            found[name] = lent if lent is not None and own == a.vpn else None
            conflict = vpn_conflicts(name, definition, a.vpn, False, lent=True) if found[name] else None
            if conflict:
                raise ValueError(conflict)
        else:
            if "rootfs" in definition and "image" not in definition:
                raise ValueError(f"MCP server {name} reaches the network and runs under Bubblewrap only, where no VPN "
                                 f"can be put in front of it: with --vpn {a.vpn} it would leave from your own address. "
                                 "Add its Podman form, or start without it")
            conflict = vpn_conflicts(name, definition, a.vpn, profile_type == "tor", lent=lent is not None)
            if conflict:
                raise ValueError(conflict)
            found[name] = lent if lent is not None else a.vpn
        if lent_of(found[name])[0] and server_kind(definition) == "image" and getattr(a, "podman_url", None) and \
                not launcher().on_machine(a):  # (on macOS a catalog server is refused anyway: start_problems)
            # (its bridge and VPN container run with this host's Podman; the start's tunnel is lent on the other)
            raise ValueError(f"MCP server {name} is a catalog server, whose containers run with this host's Podman, "
                             f"and the start's tunnel of {a.vpn} is lent on the engine of --podman-url: it cannot "
                             f"borrow it. Give it another profile (sandbox-ai mcp set {name} --vpn OTHER) or start "
                             "without --podman-url")
        if reaches:
            # What its start would refuse (a profile that is no way out, a VPN its form cannot take): refused now,
            # before the agent's start has begun.
            server_network(name, definition, found[name], vpn_module)
    return found


def held_elsewhere(name, plan, vpn_module=None):
    """Why the server NAME cannot bring up the tunnel of its own its PLAN asks for (server_network), or None: its
    profile runs in one sandbox at a time (vpn.sharing "once") and another sandbox holds it now (the lock its own VPN
    container would take: refused there too, but late, inside the server's start). A start's own servers borrow its
    tunnel instead (vpn_for_servers); this names the holder and that way."""
    if not plan.get("vpn") or not plan.get("reaches") or plan.get("lent"):
        return None
    vpn_module = vpn_module or launcher().load_vpn_module()
    try:
        profile = {"name": str(plan["vpn"]), **vpn_module.load_profile(plan["vpn"])}
        if vpn_module.sharing(profile) != "once":
            return None
        holder = vpn_module.profile_holder(profile)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None  # (its own start says why: its VPN container takes the profile's lock, or is refused)
    if holder is None:
        return None
    why, fix = vpn_module.sharing_text(profile)
    return (f"MCP server {name} would bring up a tunnel of its own of the VPN profile {plan['vpn']}, which is in use "
            f"by {holder}: {why}. A start's own MCP servers share its tunnel instead - start it with that start "
            f"(sandbox-ai mcp start {server_of(name)} --for START, or sandbox-ai mcp attach {server_of(name)} START "
            f"--live), or give it another profile (sandbox-ai mcp start {server_of(name)} --vpn OTHER; {fix})")


def server_vpn(a, name, explicit=None):
    """The VPN to start the server NAME behind for the start A (vpn_for_servers), with EXPLICIT (mcp start --vpn)
    in its place where given: with a Tor start that one must be a Tor profile too; the start's own profile where
    it lends its tunnel is that tunnel, borrowed (vpn_for_servers). ValueError where the server would leave outside
    the start's VPN."""
    behind = vpn_for_servers(a, [name]).get(name)
    if explicit is None or explicit == behind:
        return behind
    if getattr(a, "vpn", None) and explicit == a.vpn:
        vpn_module = launcher().load_vpn_module()
        profile = getattr(a, "vpn_profile", None) or vpn_module.load_profile(a.vpn)
        if vpn_module.lends(profile):
            return LentVpn(a.vpn, getattr(a, "run_id", None))
    if getattr(a, "vpn", None):
        vpn_module = launcher().load_vpn_module()
        kinds = {}
        for profile in (a.vpn, explicit):
            try:
                kinds[profile] = vpn_module.load_profile(profile).get("type")
            except (OSError, ValueError):
                kinds[profile] = None
        if kinds[a.vpn] == "tor" and kinds[explicit] != "tor":
            raise ValueError(f"--vpn {explicit} is not a Tor profile, and the start goes through Tor (--vpn {a.vpn}): "
                             f"MCP server {name} would leave outside Tor. Leave --vpn out, or name a Tor profile")
    return explicit


def check_running_vpn(name, vpn):
    """A server NAME (an instance: instance_of) that runs already, but not behind VPN (the one the start puts it
    behind, or the one of its own definition: ensure_running hands that in too), is refused: it would serve the start
    from outside that VPN. Nothing to check for VPN None or a server that does not run. An instance that borrows a
    start's tunnel (its state's "lent_by") serves that start alone: never one behind another tunnel (another run's,
    or one of its own) - and for a VPN borrowed (LentVpn) only that run's instance counts (the start gets its own)."""
    lent, run = lent_of(vpn)
    if vpn is not None and status(name) == "running" and load_state(name).get("lent_by") not in (None, run):
        raise ValueError(f"MCP server {name} is running behind the tunnel of the run {load_state(name)['lent_by']}, "
                         f"lent to it, which is not this one's way out; stop it first: sandbox-ai mcp stop {name}")
    if lent and load_state(name).get("lent_by") is None:
        return  # (not the start's own instance: that one is NAME.HASH of its run - ensure_running)
    if vpn is not None and status(name) == "running" and load_state(name).get("vpn") != vpn:
        raise ValueError(f"MCP server {name} is running without the VPN {vpn}"
                         + (f" (it runs behind {load_state(name)['vpn']})" if load_state(name).get("vpn") else "")
                         + f"; stop it first: sandbox-ai mcp stop {name}")


def vpn_starts_reaching(instance, runs=None):
    """[(run name, VPN profile, VPN type)] of the running agent starts with a VPN that reach the MCP server instance
    INSTANCE (run.json: mcp_servers, mcp_instances; a live attach records itself there too). RUNS: net.running()."""
    runs = net_module().running() if runs is None else runs
    found = []
    for run, record in runs.items():
        if not isinstance(record, dict) or record.get("kind") != "agent" or not record.get("vpn_profile"):
            continue
        instances_ = record.get("mcp_instances") if isinstance(record.get("mcp_instances"), dict) else {}
        reached = {instances_.get(name, name) for name in record.get("mcp_servers") or [] if isinstance(name, str)}
        if instance in reached:
            found.append((str(record.get("name") or run), str(record["vpn_profile"]),
                          record.get("vpn_type") or None))
    return found


def check_vpn_starts(instance, plan, runs=None):
    """Before the server INSTANCE is started (anew: it did not run, or it lost its socket or its VPN): a running start
    behind a VPN that reaches it would reach the new one too - on macOS its socket is in the same Podman volume, and
    the next connection of that start goes to whatever serves there - so it must come up behind a VPN as well, behind
    Tor for a Tor start. A start without one (or mcp start without --vpn) that finds such a server gone is refused:
    it would bring it back outside the running start's VPN, from your own address."""
    if not plan.get("reaches"):
        return
    for run, profile, kind in vpn_starts_reaching(instance, runs):
        if plan.get("vpn") is None or kind == "tor" and plan.get("vpn_type") != "tor":
            server = server_of(instance)
            through = f"through {plan['vpn']} ({plan.get('vpn_type') or 'its type unknown'})" if plan.get("vpn") \
                else "without a VPN"
            raise ValueError(
                f"MCP server {instance} serves the running start {launcher().printable(run)} behind "
                f"{'Tor' if kind == 'tor' else 'the VPN'} {profile}, and would come up {through} now: that start "
                f"would reach it there. Start it behind {profile} (sandbox-ai mcp start {server} --vpn {profile}), "
                f"or end {launcher().printable(run)} first (sandbox-ai stop {launcher().printable(run)})")


def start_servers(a):
    """{name: definition} of every server the start A can name (definitions(), its project's, its helpers')."""
    return {**definitions(getattr(a, "project_document", None)), **(getattr(a, "helper_definitions", None) or {})}


def start_definition(name, a, servers=None):
    """(instance, definition, digest) of server NAME as the start A runs it: instance_for its --mcp-limits,
    --mcp-mount and --mcp-env (NAME, or NAME.HASH), the definition with them, and the digest of what decides what it
    can reach (definition_digest of it with the gateway's rules of A's rulesets, A's backend). ValueError: NAME is not
    defined, or what the start asks for it is not valid."""
    servers = start_servers(a) if servers is None else servers
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    limits, mounts = start_change(a, name)
    instance, definition = instance_for(name, servers[name], limits, mounts, start_env(a).get(name))
    gateway = launcher().mcp_gateway(a, name, None)  # (only its rules count for the digest, not where it asks)
    digest = definition_digest({**definition, "gateway": gateway["rules"]} if gateway else definition,
                               a.backend, a.podman_url)
    return instance, definition, digest


def separable(name, definition):
    """Whether server NAME (DEFINITION) may run in an instance of a start's own: not a helper (one per start, by its
    own name) and not a team's board (one board the team's members share)."""
    return "helper" not in definition and not name.startswith("team-")


def own_instance(name, digest):
    """NAME.HASH of the instance of server NAME for a start whose definition (its DIGEST, start_definition) differs
    from the one NAME runs with."""
    return f"{name}.{hashlib.sha256(str(digest).encode()).hexdigest()[:8]}"


def start_instance(name, a, servers=None, lent_by=None):
    """(instance, definition, digest, note) of server NAME for the start A (start_definition), where an instance of
    the same name runs with another definition or other rules already (another project's server of this name, a
    definition changed by mcp set, another ruleset): an instance of its own for this start, NAME.HASH of what it runs
    - the one running serves its starts on, unchanged, and nothing of it reaches this start. NOTE says so (None: the
    instance is the start's as it is). ValueError for a helper or a team's board (one of each runs) and for an unknown
    server. LENT_BY (the run whose tunnel it borrows, vpn_for_servers): always an instance of that run's own,
    NAME.HASH of the run too - it serves that run alone and goes with it (stop_lent), and no other start, another
    tunnel behind it, ever reaches it (a helper is its start's own already, by its name)."""
    instance, definition, digest = start_definition(name, a, servers)
    if lent_by is not None:
        launcher().load_vpn_module().check_run(lent_by)
        if "helper" in definition:
            return instance, definition, digest, None
        if not separable(name, definition):
            raise ValueError(f"MCP server {name} is a team's board, which reaches no network: it borrows no tunnel")
        return own_instance(name, f"{digest}|lent-by:{lent_by}"), definition, digest, None
    if status(instance) != "running" or load_state(instance).get("definition") == digest:
        return instance, definition, digest, None
    if not separable(name, definition):
        raise ValueError(f"MCP server {instance} is running with a different definition or other rules, and "
                         + ("a helper" if "helper" in definition else "a team's board") + " runs once: stop it "
                         f"first: sandbox-ai mcp stop {instance}")
    own = own_instance(name, digest)
    if status(own) == "running" and load_state(own).get("definition") != digest:
        raise ValueError(f"MCP server {own} is running with a different definition or other rules; stop it first: "
                         f"sandbox-ai mcp stop {own}")
    return own, definition, digest, (
        f"MCP server {instance} runs with another definition or other rules (another project's server of that name, a "
        f"definition changed since it started, or other MCP rules): this start gets an instance of its own, {own}; "
        f"the running one serves its starts on (sandbox-ai mcp stop {instance} ends it)")


def image_present(image, run=subprocess.run):
    """Whether Podman of this host has IMAGE (`podman image exists`: a server's containers run it with --pull=never);
    None when that cannot be said (no Podman, no answer; SANDBOX_AI_MCP_NO_IMAGE_CHECK=1: not asked - the tests' made-up
    images)."""
    if os.environ.get("SANDBOX_AI_MCP_NO_IMAGE_CHECK") == "1" or not shutil.which("podman"):
        return None
    try:
        return LOG.run("mcp", f"is the server image {image} there", ["podman", "image", "exists", image], runner=run,
                       ok=(0, 1), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None  # (its line in launcher.log says why)


def start_problems(a, names, servers=None, present=None):
    """([problem], {name: instance}, [note]) of the --mcp servers NAMES of the start A, each checked before anything
    is made or started, every problem with its fix and all of them at once (a start used to fail on the first one,
    late, after its workspace and session were made - and again on the next): what the start asks for them
    (--mcp-limits, --mcp-mount, --mcp-env), their folders (there, or creatable when writable) and the administrator's
    policy, their variables and secrets (run_variables), their image (there: Podman runs it with --pull=never;
    PRESENT(image) -> True, False or None, default image_present) or rootfs, their VPN (their own profile there, the
    start's VPN in front: vpn_for_servers), macOS (no image server yet), and an instance of the same name running with
    another definition (start_instance: an instance of the start's own, said in a note; a helper's or a team's board
    is a problem). The instances: those that are not NAME. Names not in SERVERS are left to the caller (unknown)."""
    servers = start_servers(a) if servers is None else servers
    present = present or image_present
    problems, found, notes = [], {}, []
    wanted = {}
    for key, parse in (("limits", start_limits), ("mounts", start_mounts), ("env", start_env)):
        try:
            wanted[key] = parse(a)
        except ValueError as exc:
            problems.append(str(exc))
            wanted[key] = {}
    l = launcher()
    vpn_module = None
    for name in dict.fromkeys(names):
        if name not in servers:
            continue
        own_problems = []
        try:
            _instance, definition = instance_for(name, servers[name], wanted["limits"].get(name),
                                                 wanted["mounts"].get(name), wanted["env"].get(name))
            if wanted["mounts"].get(name):
                check_start_mounts(a, {name: servers[name]})
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if "image" in definition or "rootfs" in definition:
            if l.on_machine(a):
                own_problems.append(f"MCP server {name} is a container image of a catalog; on macOS such servers are "
                                    "not supported yet: start without it (a server defined with a command works)")
            for mount in definition.get("mounts", []):
                with contextlib.suppress(KeyError):
                    try:
                        mount_host(f"{mount['target']}={mount['host']}:{mount.get('mode', 'ro')}", mount["host"],
                                   mount.get("mode", "ro"))
                    except ValueError as exc:
                        own_problems.append(f"MCP server {name}: {exc} (sandbox-ai mcp set {name} --mount ...)")
            if not own_problems:  # (the folders there: the host-path policy its start holds them to, read-only too;
                try:                # a risk you chose is said here, before anything is made)
                    check_image_mounts(name, definition, remember=False)
                except ValueError as exc:
                    own_problems.append(str(exc))
            try:
                check_image_policy(name, definition)
            except ValueError as exc:
                own_problems.append(str(exc))
            own_problems += run_variables(name, definition, notes=notes)[1]
            engine = "bwrap" if "rootfs" in definition and (a.backend == "bwrap" or "image" not in definition) \
                else "podman"
            if engine == "podman" and present(definition["image"]) is False:
                own_problems.append(f"MCP server {name}: its image {definition['image']} is not there (its containers "
                                    f"are not pulled at a start): podman pull {definition['image']}, or add it again "
                                    f"(sandbox-ai mcp add)")
            elif engine == "bwrap" and not Path(str(definition["rootfs"].get("path", ""))).is_dir():
                own_problems.append(f"MCP server {name}: its rootfs is gone ({definition['rootfs'].get('path')}); "
                                    "add it again: sandbox-ai mcp add ... --backend bwrap")
        agent = definition.get("agent", "codex")
        if "command" in definition and getattr(a, "backend", None) == "podman" and agent in l.AUTH_TARGETS and \
                not any(str(option).split("=", 1)[0] in ("--image", "--image-variant", "-i", "-iv")
                        for option in definition.get("options") or []) and \
                present(f"localhost/{agent}-sandbox:latest") is False:
            # A command server (demo, a team's board) runs in an agent's image, codex unless it names another:
            # missing, its sandbox failed later with Podman's bare "image exists" status.
            own_problems.append(f"MCP server {name}: it runs in the image of {agent} (localhost/{agent}-sandbox:"
                                f"latest), which is not built here: sandbox-ai build {agent}")
        own_vpn = network_of(definition)[1]
        if own_vpn and not getattr(a, "vpn", None):
            try:
                vpn_module = vpn_module or l.load_vpn_module()
                vpn_module.load_profile(own_vpn)
            except (OSError, ValueError) as exc:
                own_problems.append(f"MCP server {name}: its VPN: {exc}")
        behind = None
        if getattr(a, "vpn", None):
            try:
                # Behind the start's VPN; where that runs once at a time (WireGuard, Mullvad, a persistent Tailscale
                # node) the start's own tunnel, borrowed - its instance is the run's own, chosen when it starts.
                behind = vpn_for_servers(a, [name]).get(name)
            except (OSError, ValueError) as exc:
                own_problems.append(str(exc))
        if not own_problems and lent_of(behind)[0]:
            notes.append(f"MCP server {name} reaches the network through the start's tunnel of {a.vpn}, lent to it "
                         "(one key, one tunnel: no second tunnel of the same profile); it comes up once that tunnel "
                         "is up, and stops with the start")
        elif not own_problems:
            try:
                instance, _definition, _digest, note = start_instance(name, a, servers)
            except ValueError as exc:
                own_problems.append(str(exc))
            else:
                if instance != name:
                    found[name] = instance
                if note:
                    notes.append(note)
        problems += own_problems
    return list(dict.fromkeys(problems)), found, notes


def problems_text(problems, about="the MCP servers of this start"):
    """What a start with PROBLEMS (start_problems) is told: the one problem, else each on a line of its own after how
    many there are (with ABOUT: "this start" when others than its MCP servers' are among them) - none of them is a
    reason to try one at a time."""
    if len(problems) == 1:
        return problems[0]
    return (f"{len(problems)} problems with {about} (nothing was started or made; fix them, then start again):\n"
            + "\n".join(f"  - {problem}" for problem in problems))


def remember_instance(a, name, instance):
    """The instance of server NAME the start A reaches (its socket is mounted, its record names it): a.mcp_instances."""
    found = dict(getattr(a, "mcp_instances", None) or {})
    if instance == name:
        found.pop(name, None)
    else:
        found[name] = instance
    with contextlib.suppress(AttributeError):
        a.mcp_instances = found


def ensure_running(name, a, vpn=None):
    """Start the server sandbox for NAME unless it already runs; returns 'running' or 'started'. VPN (sandbox-ai
    mcp start --vpn) puts that VPN profile in front for this start, whatever the definition says. The instance is
    start_instance's: NAME, NAME.HASH for a start with limits, folders or variables of its own, or one of its own
    where NAME runs with another definition; behind the start's tunnel, borrowed (a LentVpn), the run's own."""
    instance, _definition, _digest, note = start_instance(name, a, lent_by=lent_run(name, vpn))
    if note and (getattr(a, "mcp_instances", None) or {}).get(name) != instance:
        print(f"agent-sandbox: {note}", file=sys.stderr)  # (the start's check said it already otherwise)
    remember_instance(a, name, instance)
    with server_lock(instance):
        return ensure_running_locked(name, a, vpn, instance)


def lent_run(name, vpn):
    """The run whose tunnel the server NAME borrows behind VPN (LentVpn), None for any other VPN; ValueError for a
    tunnel borrowed from a start that does not run (nothing lends it yet)."""
    lent, run = lent_of(vpn)
    if lent and run is None:
        raise ValueError(f"MCP server {name} borrows the tunnel of its start's --vpn {vpn}, which lends it only while "
                         "it runs: the server comes up with that start")
    return run


def begin_running(name, a, vpn=None):
    """ensure_running without its wait: the server for NAME started (unless it runs) and left starting; the function
    returned waits until it is ready (its socket, its network checked, "started" said) and returns 'running' or
    'started'. The server's lock is held until then: a start begins each of its servers, then waits for them, so that
    their sandboxes come up side by side instead of one after another."""
    instance, _definition, _digest, note = start_instance(name, a, lent_by=lent_run(name, vpn))
    if note and (getattr(a, "mcp_instances", None) or {}).get(name) != instance:
        print(f"agent-sandbox: {note}", file=sys.stderr)  # (the start's check said it already otherwise)
    remember_instance(a, name, instance)
    held = contextlib.ExitStack()
    held.enter_context(server_lock(instance))
    try:
        outcome = ensure_running_locked(name, a, vpn, instance, wait=False)
    except BaseException:
        held.close()
        raise

    def ready():
        with held:
            return outcome() if callable(outcome) else outcome
    return ready


def gateway_arguments(gateway):
    """--gateway CONFIG for the relay (runtime.py --internal-mcp-serve), or nothing without rules."""
    if gateway is None:
        return []
    encoded = base64.urlsafe_b64encode(json.dumps(gateway, separators=(",", ":")).encode()).decode()
    if len(encoded) > GATEWAY_LIMIT:
        raise ValueError(f"The MCP rules for {gateway['server']} are larger than {GATEWAY_LIMIT >> 10} KiB")
    return ["--gateway", encoded]


def helper_command(name, plan, a, directory, sock, gateway):
    """The launcher's command for helper NAME (agent_sandbox.add_helpers; its network PLAN, server_network): the
    agent in a sandbox of its own, with its login, its provider's hosts, kits, rules and hooks (unlike other
    servers), serving agent_mcp.py, which runs the agent for each task the lead gives it."""
    definition, _ = checked_plan(plan, "helper")
    agent = definition["helper"]
    rules = list(definition.get("rules") or [])
    if definition.get("project_rules") is not None:
        # The project's rules hold for the helper's own tools too; its sandbox reads no project file.
        path = directory / "project-rules.json"
        write_private(path, definition["project_rules"])
        rules.append(str(path))
    pod = pod_name(name)
    serve = ["python3", "-I", f"/opt/sandbox-ai/{AGENT_MCP.name}", agent,
             *(["--skip-permissions"] if definition.get("skip_permissions") else [])]
    return [sys.executable, "-I", str(launcher().launcher_script()), agent,
            "--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else []),
            "--no-config", "--helper-of", name, *[value for path in rules for value in ("--rules", path)],
            # What narrows the lead narrows its helper: the security mode, a read-only workspace, no login.
            "--mode", definition.get("mode") or "medium", "--telemetry", definition.get("telemetry") or "off",
            *(["--workspace-ro"] if definition.get("workspace_ro") else []),
            *(["--no-auth"] if definition.get("no_auth") else []),
            *(["--dangerously-skip-permissions"] if definition.get("skip_permissions") else []),
            # An agent without a login (Goose, OpenHands, nanobot, Letta Code): the lead's keys, its only provider.
            *[word for key in definition.get("api_keys") or [] if isinstance(key, str)
              for word in ("--api-key", key)],
            "--state-dir", str(directory / "home"), "--mcp-socket-dir", str(sock),
            *(["--pod-name", pod] if a.backend == "podman" else []),
            *(["--mcp-approvals-dir", str(directory / "approvals")] if gateway and gateway.get("approvals") else []),
            *plan["launcher"],
            "--mount", f"{AGENT_MCP}:/opt/sandbox-ai/{AGENT_MCP.name}:ro",
            "--workspace", definition["workspace"], "--binary", "python3",
            "--", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", f"{launcher().MCP_SERVE_DIR}/server.sock",
            *gateway_arguments(gateway), "--", *serve]


def started_for(a):
    """The start an MCP server is started for, recorded in its mcp.json (sandbox-ai list shows the server below it):
    {"start": the start directory's id or None, "run": the run id or None}, from the launcher's arguments A (its
    run_id and start key) or from a.started_for (mcp attach sets it); None for a server started on its own."""
    given = getattr(a, "started_for", None)
    if isinstance(given, dict):
        found = {key: given.get(key) if isinstance(given.get(key), str) else None for key in ("start", "run")}
    else:
        start = getattr(a, "start_key", None) or getattr(a, "start_id", None)
        run = getattr(a, "run_id", None)
        found = {"start": start if isinstance(start, str) else None, "run": run if isinstance(run, str) else None}
    return found if any(found.values()) else None


def ensure_running_locked(name, a, vpn=None, instance=None, wait=True):
    """ensure_running under the lock of INSTANCE (start_instance: NAME, NAME.HASH with the start's own limits, folders
    or variables, or one of its own where NAME runs with another definition). WAIT False: a server it starts is left
    starting, and what it returns then is the function that waits until it is ready (begin_running)."""
    servers = start_servers(a)
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    limits, mounts = start_change(a, name)
    if mounts:  # the start's own folders: a writable one that is not there yet is made now (as mcp add makes it)
        check_start_mounts(a, {name: servers[name]}, create=True, out=lambda line: print(line, file=sys.stderr))
    base, definition = instance_for(name, servers[name], limits, mounts, start_env(a).get(name))
    instance = instance if isinstance(instance, str) and server_of(instance) == name and \
        re.fullmatch(INSTANCE_PATTERN, instance) else base
    directory = state_dir(instance)
    # How it is named in what is said: NAME, or NAME.HASH with its limits (an instance of a start's own limits) and
    # the folders of the start's own.
    label = name if instance == name else f"{instance} ({limits_text(default_limits(definition))}" + "".join(
        f"; {m['target']}={m['host']}:{m.get('mode', 'ro')}" for m in definition.get("mounts", [])
        if m["target"] in (mounts or {})) + ")"
    # The relay is the gateway: its rules come from this run's rulesets and are part of what the server is.
    gateway = launcher().mcp_gateway(a, name, directory / "approvals" if "image" in definition or "rootfs" in definition
                                     else launcher().MCP_APPROVALS_DIR)
    digest = definition_digest({**definition, "gateway": gateway["rules"]} if gateway else definition,
                               a.backend, a.podman_url)
    # Its network, decided in the one place for it (with VPN: behind that VPN, whatever the definition says).
    plan = server_network(instance, definition, vpn)  # (an instance: its own VPN container and pod)
    if plan.get("tor_note"):  # behind Tor, what of it does not work there (TOR_LIMITS), said as it starts
        LOG.info("mcp", plan["tor_note"])
        print(f"agent-sandbox: {plan['tor_note']}", file=sys.stderr)
    if launcher().on_machine(a) and ("image" in definition or "rootfs" in definition):
        # The bridge of a catalog server runs on this host and listens on a Unix socket there, which the sandboxes in
        # podman machine's VM cannot connect to (TODO.md: macOS support, stage 4).
        raise ValueError(f"MCP server {name} is a container image of a catalog; on macOS such servers are not "
                         "supported yet (their bridge would run on the Mac, out of reach of podman machine's VM). "
                         "A server defined with a command (\"command\" in mcp.json) works.")
    if status(instance) == "running":
        if load_state(instance).get("definition") != digest:
            # Another project's definition of this name: its workspace must not reach this agent. Other rules
            # for it: they must not hold for this agent instead of its own.
            LOG.warning("mcp", f"{label}: refused - it runs with another definition or other rules")
            raise ValueError(f"MCP server {label} is running with a different definition or other rules; "
                             f"stop it first: sandbox-ai mcp stop {instance}")
        # Behind the VPN this start puts it behind, or behind its own definition's (vpn None here): a server of
        # that definition started meanwhile with another (mcp start NAME --vpn OTHER) is not this one's way out.
        check_running_vpn(instance, vpn if vpn is not None else plan["vpn"] if plan["reaches"] else None)
        state = load_state(instance)
        try:
            # Shared by several starts: it is used again only while it still has its VPN (the tunnel's container
            # runs, its kill switch is in place, the server is in its namespace).
            verify_network(instance, {**plan, "vpn": state.get("vpn")}, a, state.get("podman_url"))
            usable = machine_ready(instance, a).get("ready",
                                                    (launcher().mcp_socket_dir(instance) / "server.sock").exists)()
        except ValueError as exc:
            print(f"agent-sandbox: MCP server {label} lost its VPN ({exc}); it is started again", file=sys.stderr)
            usable = False
        if usable:
            LOG.info("mcp", f"{label}: runs already (PID {state.get('pid')}); this start uses it")
            return "running"
        LOG.info("mcp", f"{label}: runs, but its socket or its VPN is gone; it is stopped and started again")
        stop_locked(instance)  # alive but its socket (or its VPN) is gone: replace it rather than start a second one
    held = held_elsewhere(instance, plan)  # a tunnel of its own of a profile another sandbox holds: said now
    if held:
        LOG.warning("mcp", f"{label}: refused - {held}")
        raise ValueError(held)
    lender = launcher().load_vpn_module() if plan["lent"] else None
    not_lent = lender.lent_tunnel_problem(plan["lent_by"], lender.lends_by_volume(a)) if lender is not None else None
    if not_lent:
        # (a run that ended, or one whose lender could not start: it went on without it and wrote down why)
        LOG.warning("mcp", f"{label}: refused - the run {plan['lent_by']} does not lend its tunnel: {not_lent}")
        raise ValueError(f"MCP server {label} borrows the tunnel of the run {plan['lent_by']} (--vpn {plan['vpn']}), "
                         f"which does not lend it now: {not_lent}")
    check_vpn_starts(instance, plan)  # never back outside the VPN of a running start that reaches it
    sock = launcher().mcp_socket_dir(instance)
    definition = plan["definition"]
    variables = {}
    if "image" in definition or "rootfs" in definition:
        # Its variables (required ones, optional ones there are, plain values, secrets), every problem at once; its
        # containers get exactly these, each --env=NAME with the value in the bridge's environment.
        variables, problems = run_variables(name, definition)
        if problems:
            raise ValueError("; ".join(problems))
        definition = run_definition(definition, variables)
        plan = {**plan, "definition": definition}
    # The server sandbox can write its socket directory: start from a fresh one. Questions of an earlier
    # server are void: their asker is gone.
    shutil.rmtree(sock, ignore_errors=True)
    shutil.rmtree(directory / "approvals", ignore_errors=True)
    for path in (directory, sock, directory / "home", directory / "work", directory / "approvals"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    if "image" in definition or "rootfs" in definition:
        check_image_mounts(name, definition)
        check_image_policy(name, definition)
        # A container (or a Bubblewrap sandbox of the image's rootfs) per connection, started by a small bridge on
        # this host: it brings its own isolation (image_command, mcp_rootfs.bwrap_args); the bridge only connects
        # the socket with its stdio. Bubblewrap when the run's backend is bwrap and the rootfs is there, or when
        # there is only the rootfs.
        # (With a VPN the plan has the Podman form only: a VPN is a container's network namespace.)
        engine = "bwrap" if "rootfs" in definition and (a.backend == "bwrap" or "image" not in definition) else "podman"
        if plan.get("egress") and not plan["vpn"]:
            # Its egress rules are loaded with this host's nft: without it the server would reach your network.
            problem = egress_module().prerequisite_problem(engine)
            if problem:
                LOG.warning("mcp", f"{label}: refused - {problem}")
                raise ValueError(f"MCP server {label}: {problem}")
        bridge = [sys.executable, "-I", str(HERE / "runtime.py"), "--internal-mcp-serve", str(sock / "server.sock"),
                  *gateway_arguments(gateway), "--"]
        if engine == "bwrap":
            if plan["vpn"]:
                raise ValueError(f"MCP server {name}: no VPN under Bubblewrap")  # (server_network refused it before)
            if not Path(definition["rootfs"]["path"]).is_dir():
                raise ValueError(f"MCP server {name}: its rootfs is gone ({definition['rootfs']['path']}); add it "
                                 "again: sandbox-ai mcp add ... --backend bwrap")
            checked = directory / "rootfs-definition.json"
            # With its egress (mcp_rootfs.run loads the rules into each connection's namespace before it runs).
            write_private(checked, {**definition, "egress": plan.get("egress")})
            # Each connection under the watch (what ends its processes is recorded: watched), Bubblewrap too.
            cmd = [*bridge, *watched(directory, instance, definition,
                                     rootfs_module().bwrap_command(checked, definition, launcher()))]
        elif not plan["vpn"]:
            cmd = [*bridge, *watched(directory, instance, definition, connection_command(plan))]
        else:
            # The VPN's container first (checked; a failure stops the server), then the bridge, whose connections'
            # containers join its network namespace through the guard (connection_command). The wrapper is the
            # supervisor: stopping it takes the tunnel down too, and the tunnel's end stops the server. The watch is
            # outside the guard: it hands the guard's podman run line a --cidfile (podman create takes it), nothing
            # that chooses a network.
            if plan["vpn_type"] == "tor" and not definition.get("mounts"):
                check_tor_image_user(name, definition["image"])  # with mounts it runs as 0:0 anyway
            # Behind the start's tunnel, lent: the VPN container borrows it (no tunnel, no key of its own).
            wrapper = ["--internal-mcp-borrow", str(plan["vpn"]), plan["vpn_container"], str(sock.resolve()),
                       plan["lent_by"]] if plan["lent"] else \
                ["--internal-mcp-vpn", str(plan["vpn"]), plan["vpn_container"], str(sock.resolve())]
            cmd = [sys.executable, "-I", str(HERE / "vpn.py"), *wrapper, "--", *bridge,
                   *watched(directory, instance, definition, connection_command(plan))]
        environment = dict(os.environ)
        environment.update(variables)
        # The bridge compares every folder with what was checked here before each connection's container.
        environment["SANDBOX_AI_MCP_MOUNTS"] = json.dumps(mount_records(definition))
        if plan["vpn"] and plan.get("egress"):
            # Its VPN container loads its egress rules (and pins its hosts through the tunnel); each connection's guard
            # checks them (vpn.mcp_wrapper, vpn.mcp_guard).
            environment[egress_module().EGRESS_ENV] = json.dumps(plan["egress"], sort_keys=True)
        log = directory / "supervisor.log"
        process = start_logged(cmd, directory, cwd=str(directory), env=environment)
        LOG.info("mcp", f"{label}: its bridge starts ({engine}, {'behind the VPN ' + plan['vpn'] if plan['vpn'] else 'no VPN'}"
                        f"), PID {process.pid}; its log: {log}")
        save_state(instance, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                              "server": name, "limits": default_limits(definition),
                              "image": definition.get("image"), "backend": engine, "definition": digest,
                          # The folders it mounts (a start's own, --mcp-mount: shown by mcp list beside its instance).
                          **({"mounts": [{key: m.get(key, "ro") for key in ("target", "host", "mode")}
                                         for m in definition["mounts"]]} if definition.get("mounts") else {}),
                          **({"rootfs": definition["rootfs"]["sha256"]} if engine == "bwrap" else {}),
                          "vpn": plan["vpn"], "lent_by": plan["lent_by"],
                          "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "started_for": started_for(a)})
        def started():
            waited(instance, lambda: wait_for_socket(sock / "server.sock", log, process, name=label), log)
            verified_or_stopped(instance, plan, a)
            form = (f"Bubblewrap, rootfs {definition['rootfs']['sha256'][:12]}" if engine == "bwrap"
                    else f"container {definition['image']}")
            print(f"agent-sandbox: MCP server {label} started ({form})", file=sys.stderr)
            return "started"
        return started() if wait else started
    pod = pod_name(instance)
    if a.backend == "podman" and launcher().remove_stale_pod(a, pod, sock.resolve()):
        print(f"agent-sandbox: removed pod {pod} left behind by an earlier MCP server {instance}", file=sys.stderr)
    if "helper" in definition:
        cmd = helper_command(name, plan, a, directory, sock, gateway)
        process = start_logged(cmd, directory)
        LOG.info("mcp", f"{name}: the helper {definition['helper']} starts in its own sandbox, PID {process.pid}; its "
                        f"log: {directory / 'supervisor.log'}")
        save_state(name, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                          "command": cmd[cmd.index("--", cmd.index("--internal-mcp-serve")) + 1:],
                          "helper": definition["helper"], "backend": a.backend, "podman_url": a.podman_url,
                          "definition": digest, "vpn": plan["vpn"], "lent_by": plan["lent_by"],
                          "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "started_for": started_for(a)})
        def started():
            waited(instance, lambda: wait_for_socket(sock / "server.sock", directory / "supervisor.log", process,
                                                     name=name, **machine_ready(name, a)), directory / "supervisor.log")
            verified_or_stopped(instance, plan, a)
            print(f"agent-sandbox: helper {definition['helper']} ({name}) started in its own sandbox", file=sys.stderr)
            return "started"
        return started() if wait else started
    cmd = server_command(instance, plan, a, directory, sock, gateway)
    log = directory / "supervisor.log"
    process = start_logged(cmd, directory)
    LOG.info("mcp", f"{label}: starts in its own sandbox ({a.backend}"
                    f"{', behind the VPN ' + plan['vpn'] if plan['vpn'] else ''}), PID {process.pid}; its log: {log}")
    save_state(instance, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                          "server": name, "limits": default_limits(definition),
                          "command": definition["command"], "backend": a.backend,
                          "podman_url": a.podman_url, "definition": digest, "vpn": plan["vpn"],
                          "lent_by": plan["lent_by"], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "started_for": started_for(a)})
    def started():
        waited(instance, lambda: wait_for_socket(sock / "server.sock", log, process, name=label,
                                                 **machine_ready(instance, a)), log)
        verified_or_stopped(instance, plan, a)
        print(f"agent-sandbox: MCP server {label} started in its own sandbox", file=sys.stderr)
        return "started"
    return started() if wait else started


def waited(instance, wait, log):
    """WAIT() (a server's socket awaited), with its time and its failure in launcher.log (the server's own log: LOG)."""
    begun = time.monotonic()
    try:
        wait()
    except Exception as exc:
        LOG.warning("mcp", f"{instance}: did not come up after {time.monotonic() - begun:.1f} s (its log: {log})", exc)
        with contextlib.suppress(AttributeError, TypeError):
            exc.launch_logged = True  # (the start's own line names the server without saying all of this again)
        raise
    LOG.info("mcp", f"{instance}: its socket is there after {time.monotonic() - begun:.1f} s")


def server_command(name, plan, a, directory, sock, gateway):
    """The launcher's command for the command server NAME (its network PLAN, server_network): the server in a
    sandbox of its own with no credentials, its own options, and the VPN the plan puts in front."""
    definition, _ = checked_plan(plan, "command")
    return [sys.executable, "-I", str(launcher().launcher_script()), definition.get("agent", "codex"),
            "--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else []),
            "--no-provider-defaults", *server_options(definition, plan["vpn"]), "--no-auth", "--no-config",
            "--state-dir", str(directory / "home"), "--mcp-socket-dir", str(sock),
            *(["--pod-name", pod_name(name)] if a.backend == "podman" else []),
            *(["--mcp-approvals-dir", str(directory / "approvals")] if gateway and gateway.get("approvals") else []),
            *plan["launcher"],
            "--workspace", definition.get("workspace") or str(directory / "work"), "--binary", "python3",
            "--", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", f"{launcher().MCP_SERVE_DIR}/server.sock",
            *gateway_arguments(gateway), "--", *definition["command"]]


def start_logged(cmd, directory, **options):
    """Start a server's supervisor CMD with its output through the log reader (`mcp.py --internal-log DIR`,
    a process of its own: the server outlives this launcher)."""
    read, write = os.pipe()
    try:
        with open(os.devnull, "rb") as null:
            subprocess.Popen([sys.executable, "-I", str(HERE / "mcp.py"), "--internal-log", str(directory)],
                             stdin=read, stdout=null, stderr=null, start_new_session=True)
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=write, stderr=write, start_new_session=True,
                                **options)
    finally:
        os.close(read)
        os.close(write)


def split_log(directory, stream):
    """The supervisor's output from STREAM into DIRECTORY: the relay's tool-call records to mcp-calls.jsonl,
    its questions to approvals/ID.json (and the outcome to ID.done once the call was decided), the rest to
    supervisor.log. The relay writes these lines, never the server: the server's own stderr reaches this
    stream only prefixed, and the relay is not dumpable, so the server cannot write to its stderr."""
    directory = Path(directory)
    approvals = directory / "approvals"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    # A write that fails (a full disk, a quota) loses that line only: the reader goes on reading, else the
    # supervisor's next line found its output closed and the server's relay failed every call from then on, also
    # once there was room again. What was lost is said in the file once a line can be written there again.
    lost = {"supervisor.log": 0, "mcp-calls.jsonl": 0}
    # Each log is rotated at MCP_LOG_PART (statedirs.rotate: renamed, compressed beside it, the oldest parts beyond
    # their limit dropped) - a server that runs for weeks wrote hundreds of MB. The lock, held shared while this runs,
    # tells gc to leave these logs to it.
    lock = None
    with contextlib.suppress(OSError):
        lock = os.open(directory / ".log.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(lock, fcntl.LOCK_SH)
    files, sizes = {}, {}

    def rotated(name):
        with contextlib.suppress(Exception):  # a log that cannot be rotated is written on
            module = statedirs_module()
            if module.rotate(directory / name, module.MCP_ARCHIVE_LIMIT, rotation_time()):
                return True
        return False

    def reopen(name):
        if files.get(name) is not None:
            files.pop(name).close()
        fd = os.open(directory / name, flags, 0o600)
        sizes[name] = os.fstat(fd).st_size
        files[name] = open(fd, "ab", buffering=0)

    def write(name, data):
        if lost[name]:
            why = "not written (the disk was full or not writable)"
            note = json.dumps({"event": "lost", "lines": lost[name], "server": directory.name, "reason": why}) + "\n" \
                if name.endswith(".jsonl") else f"sandbox-ai: {lost[name]} lines of this log were {why}\n"
            data = note.encode() + data
        try:
            if files.get(name) is None:
                reopen(name)
            view = memoryview(data)
            while view:  # (unbuffered: a write cut short by the full disk is finished, or fails, here)
                view = view[files[name].write(view):]
            lost[name] = 0
            sizes[name] += len(data)
            if sizes[name] > MCP_LOG_PART:
                files.pop(name).close()
                if not rotated(name):
                    sizes[name] = -(MCP_LOG_PART // 4)  # tried again after a quarter more
                    files[name] = open(os.open(directory / name, flags, 0o600), "ab", buffering=0)
                else:
                    reopen(name)
        except OSError:
            lost[name] += 1

    def private(path, data):
        with contextlib.suppress(OSError):  # an unwritten question is never answered: its call is refused
            write_private(path, data)

    for name in lost:
        with contextlib.suppress(OSError):
            if os.lstat(directory / name).st_size > MCP_LOG_PART:
                rotated(name)  # (grown before this version, or by a reader of an older one)
        with contextlib.suppress(OSError):
            reopen(name)
    try:
        for line in iter(lambda: stream.readline(1 << 20), b""):
            kind, _, rest = line.partition(b" ")
            record = None
            if kind in (b"sandbox-ai-mcp-call", b"sandbox-ai-mcp-ask"):
                try:
                    record = json.loads(rest)
                except (ValueError, RecursionError):
                    pass
            if not isinstance(record, dict):
                write("supervisor.log", line)
                continue
            record.setdefault("server", directory.name)
            if kind == b"sandbox-ai-mcp-ask":
                ident = str(record.get("id", ""))
                if re.fullmatch(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}", ident) and approvals.is_dir():
                    private(approvals / f"{ident}.json", record)  # (asked again: the same question, open once more)
                    with contextlib.suppress(OSError):
                        (approvals / f"{ident}.state").unlink(missing_ok=True)
                continue
            write("mcp-calls.jsonl", (json.dumps(record, ensure_ascii=False) + "\n").encode())
            ident = str(record.get("question", ""))
            if re.fullmatch(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}", ident) and (approvals / f"{ident}.json").exists():
                approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
                if str(approval.get("reason") or "").startswith(NOT_ANSWERED):
                    # Nobody answered in time: no decision - the question stays open ("timed out"), answerable.
                    private(approvals / f"{ident}.state", {"state": "timeout", "time": record.get("time")})
                else:
                    private(approvals / f"{ident}.done", {**approval, "id": ident})
    finally:
        for stream_ in files.values():
            with contextlib.suppress(OSError):
                stream_.close()
        if lock is not None:
            os.close(lock)


MCP_LOG_PART = int(os.environ.get("SANDBOX_AI_MCP_LOG_PART") or 32 << 20)  # statedirs.MCP_LOG_PART
NOT_ANSWERED = "not answered yet"  # the start of runtime.NOT_ANSWERED: a call refused while its question stays open
_statedirs = None


def rotation_time():
    """The time a rotated log's older part is named by (tests: their own)."""
    return time.time()


def statedirs_module():
    global _statedirs
    if _statedirs is None:
        spec = importlib.util.spec_from_file_location("statedirs_for_mcp", HERE / "statedirs.py")
        with LOADING:
            if _statedirs is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _statedirs = loaded
    return _statedirs


def write_private(path, data):
    """PATH as JSON DATA that only you can read, replaced in one step (readers never see half a file)."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


def log_main(directory):
    """`mcp.py --internal-log DIR`: split the supervisor's output (stdin) until it ends; questions also show up
    as desktop notifications, as an agent's do."""
    stop = threading.Event()
    with contextlib.suppress(Exception):  # notifications are a convenience; the log must be written regardless
        spec = importlib.util.spec_from_file_location("approvals_for_mcp", HERE / "approvals.py")
        approvals = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(approvals)
        threading.Thread(target=approvals.watch, args=(Path(directory) / "approvals", stop), daemon=True).start()
    try:
        split_log(directory, sys.stdin.buffer)
    finally:
        stop.set()
    return 0


def option_value(options, name):
    """The last value of option NAME in OPTIONS (as on a command line), or None."""
    value = None
    for index, token in enumerate(options):
        if token == name and index + 1 < len(options):
            value = options[index + 1]
        elif token.startswith(name + "="):
            value = token.partition("=")[2]
    return value


def stop(name, timeout=30):
    """Stop server NAME: all of its instances (NAME and the NAME.HASH of starts with limits of their own); an instance
    NAME.HASH alone."""
    if "." in name or not re.fullmatch(MCP_NAME_PATTERN, name):
        with server_lock(name):
            return stop_locked(name, timeout)
    others = [instance for instance in instances(name, include_stopped=True)
              if instance != name and status(instance) == "running"]
    with server_lock(name):
        stop_locked(name, timeout, quiet=bool(others))
    for instance in others:
        with server_lock(instance):
            stop_locked(instance, timeout)
    return 0


def stop_locked(name, timeout=30, quiet=False):
    if status(name) != "running":
        if not quiet:  # (QUIET: other instances of it are stopped instead)
            print(f"sandbox-ai mcp: MCP server {name} is not running", file=sys.stderr)
        return 0
    pid = load_state(name)["pid"]
    LOG.info("mcp", f"{name}: stopping its supervisor (PID {pid})")
    with contextlib.suppress(ProcessLookupError):  # it ended right after the check: stopped all the same
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while status(name) == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    if status(name) == "running":
        LOG.warning("mcp", f"{name}: its supervisor (PID {pid}) did not end within {timeout} s of SIGTERM: killed")
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        state = load_state(name)
        if state.get("backend") == "podman":
            engine = SimpleNamespace(podman_url=state.get("podman_url"))
            try:
                # Without its supervisor the pod would keep running the server; only our labelled one goes.
                launcher().remove_stale_pod(engine, pod_name(name), launcher().mcp_socket_dir(name).resolve())
            except (ValueError, subprocess.CalledProcessError) as exc:
                LOG.warning("mcp", f"{name}: its pod could not be removed", exc)
                print(f"agent-sandbox: {exc}", file=sys.stderr)
    (launcher().mcp_socket_dir(name) / "server.sock").unlink(missing_ok=True)
    print(f"sandbox-ai mcp: MCP server {name} stopped", file=sys.stderr)
    return 0


def lent_instances(run):
    """The running instances that borrow the tunnel of the run RUN (their state's "lent_by")."""
    root = state_dir("x").parent
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return []
    return [name for name in names if re.fullmatch(INSTANCE_PATTERN, name) and load_state(name).get("lent_by") == run
            and status(name) == "running"]


def stop_lent(run, timeout=30):
    """When the run RUN ends (agent_sandbox, before its tunnel goes): every server that borrows its tunnel is stopped -
    it served that run alone, and without the tunnel it would reach nothing (its kill switch holds). Returns their
    names; one that cannot be stopped is said and logged, the others are stopped all the same."""
    stopped = []
    for name in lent_instances(run):
        try:
            with server_lock(name):
                stop_locked(name, timeout, quiet=True)
            stopped.append(name)
        except (OSError, ValueError) as exc:
            LOG.warning("mcp", f"{name}: borrowed the tunnel of the run {run}, which ends; it could not be stopped", exc)
            print(f"agent-sandbox: MCP server {name} could not be stopped ({exc}); it reaches nothing without the "
                  f"tunnel of {run} (sandbox-ai mcp stop {name})", file=sys.stderr)
    return stopped


# --- an MCP server for a named start (mcp start NAME --for START, mcp attach NAME START) ---------------------

# Why a running sandbox reaches a new MCP server only after a restart unless it is attached with --live: its agent
# (claude, codex or opencode) reads its MCP servers when it starts (--mcp-config, -c mcp_servers, opencode.json).
LIVE = ("a running sandbox cannot take a new MCP server while it runs unless it is attached with --live: its agent "
        "(claude, codex or opencode) reads its MCP servers when it starts (--mcp-config, -c mcp_servers, "
        "opencode.json); --live opens the server's relay in the running sandbox now (Codex and OpenCode see it after "
        "a restart of the agent all the same)")

_starts = None


def starts_module():
    global _starts
    if _starts is None:
        spec = importlib.util.spec_from_file_location("starts_for_mcp", HERE / "starts.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _starts is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _starts = loaded
    return _starts


def start_arguments(data, extra=()):
    """The launcher's arguments of a kept start (its session.json DATA) with EXTRA options, prepared as its next
    start prepares them (preset, project file, security mode) and checked as it checks them: a ValueError names
    what the administrator's policy or the start's security mode refuses (the same words as at a start)."""
    l = launcher()
    sessions = starts_module().module("sessions")
    with sessions.in_directory(data["cwd"]):
        argv, _ = l.load_presets_module().apply(l.canonical_options([data["agent"], *sessions.run_options(data), *extra]),
                                                canonical=l.canonical_options, with_mode=True,
                                                credential=l.copies_credential, private=l.private_source)
        argv = l.canonical_options(argv)
        try:
            a = l.parse_args(argv)
        except SystemExit:
            raise ValueError("the start's saved options no longer parse; start it anew") from None
        config_path, document = l.load_project_config(a)
        merged = l.merge_config(argv, a, document) if document else argv
        if document:
            a = l.parse_args(merged)
        user_defaults, policy = l.load_user_defaults(a), l.load_policy()
        a = l.MODES.apply(l.launcher_namespace(), merged, a, document, config_path, user_defaults, policy)
        a.project_config, a.project_document = (str(config_path) if config_path else None), document
        l.check_policy(a, policy)
        l.MODES.check(l.launcher_namespace(), a, policy)
    return a


def serving(found=None):
    """{server: [start names]}: the agent starts that reach each MCP server now (their run says so), or will at
    their next start (their saved options name it)."""
    found = starts_module().entries(True, prune=False) if found is None else found
    served = {}
    for entry in found:
        if entry.get("kind") == "mcp":
            continue
        for name in dict.fromkeys((entry.get("mcp") or []) + (entry.get("mcp_running") or [])):
            served.setdefault(name, []).append(entry["name"])
    return served


def attach(name, start, restart=False, start_server=False, vpn=None, ask=None, out=print):
    """Connect the MCP server NAME to the agent start START (by name or id): kept in its options (--mcp NAME, for
    its later starts too), checked as its start checks it (policy, security mode, the server's definition). A
    stopped start reaches it from its next start; a running one only after a restart (LIVE says why), done now
    with RESTART (or when ASK(question) says yes), else named. START_SERVER (mcp start --for): the server is
    started now as that start starts it (its backend and rules). {"server", "start", "state", "attached":
    "already" | "next start" | "restarted" | "after a restart", "server_started": bool, "message"}."""
    printable = launcher().printable
    starts = starts_module()
    entry = starts.find(start)
    shown = printable(entry["name"])
    if entry["kind"] == "mcp":
        raise ValueError(f"{shown} is an MCP server's sandbox: a server is attached to an agent's start")
    if entry["state"] == "damaged":
        raise ValueError(f"{shown} is damaged (see sandbox-ai list); repair or delete it first")
    if not entry.get("directory"):
        raise ValueError(f"{shown} is a run without a kept start, which keeps nothing to start again with, and {LIVE}; "
                         f"end it (sandbox-ai stop {shown}) and start anew with --mcp {name}")
    sessions = starts.module("sessions")
    key = entry["id"]
    data = sessions.load(key)
    a = start_arguments(data, ["--mcp", name])  # refuses what its start would refuse
    servers = definitions(a.project_document)
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    running = entry["state"] in ("running", "paused")
    if running and isinstance(entry.get("run"), str):
        a.run_id = entry["run"]  # (the run that lends its tunnel to the start's own servers while it runs)
    if getattr(a, "vpn", None):
        # A server that would leave outside the start's VPN (Tor: outside Tor), or one running already without it, is
        # refused now: not after a restart has stopped the start.
        check_running_vpn(instance_of(a, name), server_vpn(a, name, vpn))
    result = {"server": name, "start": entry["name"], "state": entry["state"], "server_started": False}
    named = name in starts.mcp_names(data["options"])
    # A run of a version that did not record its servers (no "mcp_running"): its options are taken for it.
    if named and (not running or name in entry.get("mcp_running", [name])):
        result.update(attached="already", message=f"{shown} uses the MCP server {name} already")
        LOG.info("mcp", f"attach {name} to {entry['name']}: {result['attached']}")
        out(result["message"])
        return result
    if start_server:
        a.started_for = {"start": key, "run": entry.get("run")}
        # As the start starts it: behind its VPN where the server reaches the network (with Tor: Tor only); behind
        # its tunnel, borrowed, where the start lends it - only while it runs (else it comes up with its next start).
        if not running and lent_of(server_vpn(a, name, vpn))[0]:
            out(f"MCP server {name} borrows the tunnel of {shown}'s --vpn {a.vpn} (one key, one tunnel); it comes up "
                f"with {shown}'s next start")
        else:
            result["server_started"] = ensure_running(name, a, server_vpn(a, name, vpn)) == "started"
    if running and not restart and ask is not None:
        restart = ask(f"{shown} runs; {LIVE}. Restart it now with {name}? The agent continues its conversation.")
    if running and restart:
        # Its new start checked first (the server's image, folders, rules, room on the disks): one that would fail
        # leaves it running as it was - stopped first, a server that could not be added ended the session.
        sessions.check_restart(key, mcp=[name])
        why = f"to start it again with the MCP server {name}"
        out(f"{shown}: stopping it {why}")
        sessions.stop(key, by=f"sandbox-ai mcp attach {name} {entry['name']} --restart ({why})")
        try:
            with sessions.session_lock(key):
                sessions.resume_locked(key, mcp=[name])
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("mcp", f"attach {name} to {entry['name']}: stopped, and its new start failed", exc)
            raise sessions.restart_failed(key, why, exc) from None
        result.update(attached="restarted", state="running",
                      message=f"{shown} runs again with the MCP server {name}; the agent continues its conversation")
        LOG.info("mcp", f"attach {name} to {entry['name']}: {result['attached']}")
        out(result["message"])
        return result
    sessions.add_mcp(key, [name])
    if running:
        result.update(attached="after a restart",
                      message=f"{shown} reaches the MCP server {name} from its next start on, not now: {LIVE}. "
                              f"Restart it now (the agent continues its conversation): sandbox-ai mcp attach {name} "
                              f"{shown} --restart (or: sandbox-ai resume {shown} --restart), or attach it to the "
                              f"running sandbox: sandbox-ai mcp attach {name} {shown} --live")
    else:
        result.update(attached="next start", message=f"{shown} reaches the MCP server {name} from its next start on: "
                                                     f"sandbox-ai resume {shown}")
    LOG.info("mcp", f"attach {name} to {entry['name']}: {result['attached']}")
    out(result["message"])
    return result


# --- an MCP server for a running sandbox, while it runs (mcp attach|detach NAME START --live) ----------------------
#
# The host starts the server if it does not run (as the start would: ensure_running, with the start's own options,
# policy and security mode), opens the server's socket directory (O_PATH) and hands that descriptor to the run's
# supervisor over its control socket (op mcp-open): the supervisor opens a relay for it at /run/sandbox-mcp/NAME, as
# it does at a start for each --mcp server, with the run's broker (questions), taint and mcp-calls.jsonl, behind the
# same gateway (the server's own rules are in the server's relay, as at a start). No mount is made: the agent never
# reaches the server's directory. Then the run records it (run.json mcp_servers), the start keeps it (--mcp NAME, or
# --helper AGENT, for its later starts) and the agent's MCP configuration gets it where the agent reads one at run
# time (LIVE_AGENTS). `detach --live` closes the relay (the agent's connections to it end at once).

# What each agent makes of a server attached while it runs: where it is written for it, and what the human does.
LIVE_AGENTS = {
    "claude": {"config": "~/.claude.json (mcpServers, user scope)", "restart": "maybe",
               "attach": "Claude Code: the server is in the sandbox's ~/.claude.json (user scope). Open /mcp in Claude "
                         "and look for {name}; Claude Code reads its MCP servers when it starts, and whether a "
                         "running one takes up a server added since is not documented: if /mcp does not list it, "
                         "restart the agent: sandbox-ai resume {start} --restart (the conversation continues).",
               "detach": "Claude Code: its connection to {name} ends now and its calls fail; /mcp may still list it "
                         "until the agent restarts (sandbox-ai resume {start} --restart; the conversation continues)."},
    "codex": {"config": None, "restart": "required",
              "attach": "Codex reads its MCP servers only when it starts (its -c mcp_servers options): restart the "
                        "agent to use {name}: sandbox-ai resume {start} --restart (the conversation continues). Until "
                        "then the relay is open, but Codex does not know it.",
              "detach": "Codex: its connection to {name} ends now and its calls fail; it still names the server until "
                        "the agent restarts (sandbox-ai resume {start} --restart; the conversation continues)."},
    "opencode": {"config": None, "restart": "required",
                 "attach": "OpenCode reads its MCP servers only when it starts (OPENCODE_CONFIG_CONTENT): restart the "
                           "agent to use {name}: sandbox-ai resume {start} --restart (the conversation continues). "
                           "Until then the relay is open, but OpenCode does not know it.",
                 "detach": "OpenCode: its connection to {name} ends now and its calls fail; it still names the server "
                           "until the agent restarts (sandbox-ai resume {start} --restart; the conversation "
                           "continues)."},
}
AGENT_TITLES = {"hermes": "Hermes", "dsh": "DeepSeek Harness", "qwen": "Qwen Code", "goose": "Goose",
                "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code", "opencode2": "OpenCode 2",
                "pi": "Pi"}


def live_agent(agent):
    """LIVE_AGENTS' entry of AGENT; for any other agent (each reads its MCP servers only when it starts) one that names
    it by its own name - they were all told they were Codex."""
    if agent in LIVE_AGENTS:
        return LIVE_AGENTS[agent]
    title = AGENT_TITLES.get(agent) or "The agent"
    return {"config": None, "restart": "required",
            "attach": f"{title} reads its MCP servers only when it starts: restart the agent to use {{name}}: "
                      "sandbox-ai resume {start} --restart (the conversation continues). Until then the relay is "
                      f"open, but {title} does not know it.",
            "detach": f"{title}: its connection to {{name}} ends now and its calls fail; it still names the server "
                      "until the agent restarts (sandbox-ai resume {start} --restart; the conversation continues)."}


LIVE_STATE = "mcp-live.json"  # in the run's directory: what attach --live wrote into the agent's configuration
_net = None


def net_module():
    global _net
    if _net is None:
        spec = importlib.util.spec_from_file_location("net_for_mcp", HERE / "net.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _net is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _net = loaded
    return _net


def live_target(start):
    """(entry, record, data): the running agent start START (sandbox-ai list), its run (run.json, with "directory")
    and its kept start (session.json); ValueError when it cannot take a server while it runs."""
    printable = launcher().printable
    starts = starts_module()
    entry = starts.find(start)
    shown = printable(entry["name"])
    if entry["kind"] == "mcp":
        raise ValueError(f"{shown} is an MCP server's sandbox: a server is attached to an agent's start")
    if entry["state"] == "paused":
        raise ValueError(f"{shown} is paused: resume it first (sandbox-ai resume {shown}), then attach")
    if entry["state"] != "running":
        raise ValueError(f"{shown} does not run ({entry['state']}): --live is for a running sandbox; without it the "
                         "server is attached for its next start")
    if not entry.get("directory"):
        raise ValueError(f"{shown} is a run without a kept start, which keeps nothing to check the server against; "
                         f"end it (sandbox-ai stop {shown}) and start anew with --mcp NAME")
    record = net_module().running().get(entry.get("run") or "")
    if record is None:
        raise ValueError(f"{shown}: its run is not found (has it ended?)")
    if not record.get("mcp_live") or record.get("kind") != "agent":
        raise ValueError(f"{shown} was started by a version of sandbox-ai that cannot take an MCP server while it "
                         f"runs: restart it with the server (sandbox-ai mcp attach NAME {shown} --restart)")
    if record.get("podman_url"):
        raise ValueError(f"{shown} runs on another machine (--podman-url): an MCP server's socket on this host does "
                         f"not reach it while it runs; restart it with the server (--restart)")
    return entry, record, starts.module("sessions").load(entry["id"])


def live_arguments(data, record, name=None, helper=None):
    """(a, server name): the start's arguments with --mcp NAME (or --helper AGENT), checked as its start checks
    them (start_arguments: the administrator's policy, the security mode, the project file), and against the
    security mode the run has (run.json) if that is another one now."""
    l = launcher()
    a = start_arguments(data, ["--helper", helper] if helper else ["--mcp", name])
    if helper:
        # The helper's name hashes the main workspace as the start resolved it: the same server as its start's.
        a.workspaces = [record.get("workspace") or a.workspaces[0], *a.workspaces[1:]]
        l.add_helpers(a)
        name = l.helper_name(helper, a.workspaces[0])
    mode = (record.get("mode") or {}).get("name") if isinstance(record.get("mode"), dict) else None
    if mode in l.MODES.NAMES and mode != a.mode:
        found = l.MODES.widening(l.launcher_namespace(), a, mode)
        if found:
            raise ValueError(f"{found[0][0]} widens the security mode {mode} of this run: {found[0][1]}")
    return a, name


@contextlib.contextmanager
def run_lock(record):
    fd = os.open(Path(record["directory"]) / "mcp.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def record_server(record, name, add=True, instance=None):
    """NAME in (or out of) run.json's mcp_servers: sandbox-ai list, mcp list and the front ends show whom it serves;
    INSTANCE, where it is not NAME (the start's own limits), in its mcp_instances (the kill switch finds its sandbox)."""
    path = Path(record["directory"]) / "run.json"
    current = starts_module().read_json(path) or {}
    servers = [n for n in current.get("mcp_servers") or [] if isinstance(n, str) and n != name]
    instances_ = {key: value for key, value in (current.get("mcp_instances") or {}).items()
                  if isinstance(key, str) and isinstance(value, str) and key != name} \
        if isinstance(current.get("mcp_instances"), dict) else {}
    if add and instance and instance != name:
        instances_[name] = instance
    starts_module().rewrite_json(path, {"mcp_servers": servers + ([name] if add else []),
                                        **({"mcp_instances": instances_} if instances_ or "mcp_instances" in current
                                           else {})})


def live_state(record):
    try:
        data = json.loads((Path(record["directory"]) / LIVE_STATE).read_text())
    except (OSError, ValueError, RecursionError):
        return {"claude": [], "created": False}
    return data if isinstance(data, dict) else {"claude": [], "created": False}


CLAUDE_LOCK_STALE = 10.0  # seconds: proper-lockfile's default; Claude refreshes its lock's mtime meanwhile
CLAUDE_LOCK_WAIT = 15.0  # seconds to wait for Claude to give the lock back


@contextlib.contextmanager
def claude_config_lock(home, wait=CLAUDE_LOCK_WAIT, stale=CLAUDE_LOCK_STALE, clock=time.monotonic, sleep=time.sleep):
    """Hold Claude Code's lock on HOME/.claude.json as Claude takes it (proper-lockfile: the directory
    .claude.json.lock made with mkdir, its mtime refreshed while held, stale once older than STALE seconds, then
    taken over). ValueError when it is not given back within WAIT seconds (the agent may hold it: the file is left
    as it is). Made and removed through HOME's descriptor, never through a link the agent put there."""
    base = os.open(home, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    name = ".claude.json.lock"
    deadline = clock() + wait
    try:
        while True:
            try:
                os.mkdir(name, 0o700, dir_fd=base)
                break
            except FileExistsError:
                try:
                    info = os.stat(name, dir_fd=base, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and time.time() - info.st_mtime > stale:
                        os.rmdir(name, dir_fd=base)  # stale: its holder is gone (as proper-lockfile takes it over)
                        continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            if clock() >= deadline:
                raise ValueError("Claude Code holds ~/.claude.json's lock (~/.claude.json.lock) for longer than "
                                 f"{int(wait)} s; left as it is")
            sleep(0.1)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=base)
    finally:
        os.close(base)


def forget_live_entries(home):
    """At a start: take out of HOME's ~/.claude.json the MCP servers an attach --live of an earlier run wrote there
    (a bridge to the run's relay, exactly as agent_config writes it) and a launcher killed on the way left behind; a
    start names its servers with --mcp-config. The names taken out ([] when nothing was)."""
    l = launcher()
    path = Path(home) / ".claude.json"
    if not path.is_file() or path.is_symlink():
        return []
    removed = []

    def apply(document):
        servers = document.get("mcpServers")
        if not isinstance(servers, dict):
            return False
        for name, value in list(servers.items()):
            if isinstance(name, str) and re.fullmatch(l.MCP_NAME, name) and value == {
                    "type": "stdio", "command": "python3", "args": l.mcp_bridge_args(name), "env": {}}:
                del servers[name]
                removed.append(name)
        return bool(removed)
    try:
        with claude_config_lock(Path(home), wait=2.0):
            l.update_json_defaults(Path(home), ".claude.json", apply, l.CLAUDE_STATE_LIMIT, keep_mode=True)
    except (OSError, ValueError) as exc:
        LOG.warning("mcp", f"the live MCP servers were not taken out of {Path(home) / '.claude.json'}", exc)
    return removed


def agent_config(record, name, add=True):
    """Claude: mcpServers[NAME] (the bridge to the run's relay, as --mcp-config names it at a start) added to, or
    taken out of, the sandbox HOME's ~/.claude.json (user scope); a server of that name of its own stays as it is.
    What was written is kept in the run's directory (LIVE_STATE), so that the end of the run takes it out again
    (forget_live_config). Codex and OpenCode read none at run time: None. Returns a short description of what was
    done, or None."""
    l = launcher()
    if record.get("agent") != "claude" or not record.get("agent_home"):
        return None
    home = Path(record["agent_home"])
    wanted = {"type": "stdio", "command": "python3", "args": l.mcp_bridge_args(name), "env": {}}
    state = live_state(record)
    created = []

    def apply(document):
        servers = document.get("mcpServers")
        if add:
            if servers is None:
                servers = document["mcpServers"] = {}
                created.append(True)
            if not isinstance(servers, dict):
                raise ValueError("~/.claude.json in the sandbox: mcpServers is no object; left as it is")
            if servers.get(name) == wanted:
                return False
            if name in servers:
                raise ValueError(f"~/.claude.json in the sandbox names an MCP server {name} of its own; left as it is")
            servers[name] = wanted
            return True
        if not isinstance(servers, dict) or servers.get(name) != wanted:
            return False
        del servers[name]
        if not servers and state.get("created"):
            del document["mcpServers"]
        return True
    if not add and not (home / ".claude.json").exists():
        return None
    # Claude Code saves ~/.claude.json under a lock of its own (proper-lockfile: the folder ~/.claude.json.lock):
    # taken here too, so that neither overwrites what the other just wrote; read, changed and replaced (atomically,
    # its mode kept) within it. A Claude that writes without the lock (older versions) can still lose a change
    # made in the moment between the read and the replace.
    with claude_config_lock(home):
        problem = l.update_json_defaults(home, ".claude.json", apply, l.CLAUDE_STATE_LIMIT, keep_mode=True)
    if problem:
        raise ValueError(f"~/.claude.json in the sandbox was left as it is ({problem})")
    names = [n for n in state.get("claude", []) if n != name] + ([name] if add else [])
    state = {"claude": names, "created": bool(state.get("created") or created) and (add or bool(names))}
    write_private(Path(record["directory"]) / LIVE_STATE, state)
    return "~/.claude.json (mcpServers)"


def forget_live_config(directory):
    """At the end of a run (the launcher's cleanup): take out of the agent's configuration what attach --live put
    there (the next start names the servers it keeps with --mcp-config, as always); never raises."""
    with contextlib.suppress(Exception):
        record = {**json.loads((Path(directory) / "run.json").read_text()), "directory": str(directory)}
        for name in live_state(record).get("claude", []):
            with contextlib.suppress(Exception):
                agent_config(record, name, add=False)


def keep_server(key, name=None, helper=None, add=True):
    """--mcp NAME (or --helper AGENT) into (or out of) the saved options of the start KEY; True if they changed."""
    sessions = starts_module().module("sessions")
    with sessions.session_lock(key):
        data = sessions.load(key)
        options = list(data["options"])
        option, value = ("--helper", helper) if helper else ("--mcp", name)
        short = launcher().SHORT_OPTIONS.get(option)
        given = [i for i, token in enumerate(options) if token in (option, short) and i + 1 < len(options)
                 and options[i + 1] == value]
        given += [i for i, token in enumerate(options) if token == f"{option}={value}"]
        if add and given:
            return False
        if add:
            data["options"] = sessions.added_options(data, mcp=[name]) if not helper else [*options, option, value]
        else:
            if not given:
                return False
            for index in sorted(given, reverse=True):
                del options[index:index + (1 if "=" in options[index] else 2)]
            data["options"] = options
        sessions.save(key, data)
    return True


def socket_dir_handle(record, directory, identity):
    """The descriptor handed to the supervisor of the running sandbox RECORD for the server's socket DIRECTORY (still
    IDENTITY: device, inode): a detached read-only clone of that folder alone (workspaces.detached_copy), through
    which nothing else of the host is reached. ValueError when it cannot be made."""
    spec = importlib.util.spec_from_file_location("sandbox_workspaces_for_mcp", HERE / "workspaces.py")
    workspaces = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workspaces)
    try:
        return workspaces.detached_copy(record, directory, identity)
    except (OSError, ValueError) as exc:
        raise ValueError(f"the server's socket folder could not be handed over on its own ({exc}): restart the start "
                         "with the server instead (sandbox-ai mcp attach NAME START --restart)") from None


def attach_live(name, start, helper=None, out=print):
    """`sandbox-ai mcp attach NAME START --live` (HELPER: --helper AGENT instead of NAME): the server into the
    running sandbox of START now, without a restart. {"server", "start", "run", "state", "attached": "live" |
    "already", "server_started", "kept", "agent", "agent_config", "agent_restart", "relay", "message"}."""
    printable = launcher().printable
    entry, record, data = live_target(start)
    a, name = live_arguments(data, record, name, helper)
    servers = {**definitions(a.project_document), **(getattr(a, "helper_definitions", None) or {})}
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    shown = printable(entry["name"])
    net = net_module()
    listed = net.ask(record["directory"], {"op": "mcp-list"})
    if not listed.get("live"):
        raise ValueError(f"{shown} made no place for MCP servers attached while it runs (started by an older version): "
                         f"restart it with the server (sandbox-ai mcp attach {name} {shown} --restart)")
    agent = record.get("agent") if isinstance(record.get("agent"), str) else "codex"
    result = {"server": name, "start": entry["name"], "run": record.get("run"), "state": "running",
              "server_started": False, "agent": agent, "agent_config": None,
              "agent_restart": live_agent(agent)["restart"], "relay": f"{launcher().MCP_AGENT_DIR}/{name}/server.sock"}
    a.started_for = {"start": entry.get("id") if entry.get("directory") else None, "run": record.get("run")}
    if record.get("vpn_profile"):
        # The VPN the run has (its --llm server's, say), whatever its kept options say now; the run that lends its
        # tunnel (vpn.lends) to the servers it reaches.
        a.vpn, a.vpn_profile = record["vpn_profile"], None
        if isinstance(record.get("run"), str):
            a.run_id = record["run"]
    # Its own checks, as at a start; behind the run's VPN where it reaches the network (with Tor: Tor only). A server
    # that runs already without it is refused (ensure_running).
    result["server_started"] = ensure_running(name, a, server_vpn(a, name)) == "started"
    with run_lock(record):
        # Read under the run's lock: another attach or detach may have changed it since the look above.
        listed = net.ask(record["directory"], {"op": "mcp-list"})
        if name in listed.get("servers", []):
            result["attached"] = "already"
        else:
            instance = instance_of(a, name)  # the one this start reaches: its own limits (--mcp-limits), if any
            directory = launcher().mcp_socket_dir(instance)
            fd = os.open(directory, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            tree = None
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid():
                    raise ValueError(f"{directory} is not yours: not handed to the sandbox")
                # Never the host directory itself: '..' from its descriptor would reach the host's file system. A
                # detached clone of the folder alone (socket_dir_handle) is all the supervisor gets.
                tree = socket_dir_handle(record, directory, (info.st_dev, info.st_ino))
                answer = net.ask(record["directory"], {"op": "mcp-open", "name": name,
                                                       "relay": launcher().mcp_relay_config(a, name)}, fds=[tree])
            finally:
                os.close(fd)
                if tree is not None:
                    os.close(tree)
            result["attached"] = "live" if answer.get("opened") else "already"
        record_server(record, name, instance=instance_of(a, name))
        try:
            # Relayed already: a server of the start is on the agent's command line, one attached live was written
            # then; never twice.
            if result["attached"] == "live":
                result["agent_config"] = agent_config(record, name)
        except (OSError, ValueError) as exc:
            out(f"  the agent's configuration was not changed: {printable(str(exc))}")
    # The run's kill switch (sandbox-ai net cut) holds for the server it reaches now as well.
    runs = net.running()
    for run, error in net.propagate_cut(runs.get(record.get("run"), record), runs):
        out(f"  {printable(run)}: " + (f"the kill switch of {shown} could not be set there: {printable(error)}"
                                       if error else f"cut as {shown} is (sandbox-ai net cut)"))
    try:
        keep_server(entry["id"], name, helper)  # False: named there already
        result["kept"] = True
    except (OSError, ValueError) as exc:
        result["kept"] = False
        out(f"  not kept for the next start: {printable(str(exc))}")
    how = live_agent(agent)["attach"].format(name=name, start=shown)
    result["message"] = (f"{shown} reaches the MCP server {name} now"
                         f"{' (it was attached already)' if result['attached'] == 'already' else ''}"
                         f"{', kept for its later starts' if result['kept'] else ''}. {how}")
    LOG.info("mcp", f"attach {helper and 'the helper ' + helper or name} to {entry['name']} --live: "
                    f"{result['attached']}" + ("" if result["kept"] else "; not kept for its later starts"))
    out(result["message"])
    return result


def detach(name, start, helper=None, live=False, out=print):
    """`sandbox-ai mcp detach NAME START [--live]`: the server out of the start's options (its later starts); with
    LIVE (a running start) its relay in the sandbox closed now. The server's own sandbox keeps running (other starts
    may use it; sandbox-ai mcp stop NAME ends it). {"server", "start", "run", "state", "detached": "live" |
    "next start" | "not attached", "relay_closed", "removed_from_options" (out of the start's options now), "agent",
    "agent_config", "agent_restart" (as attach_live says it: whether the agent must restart to drop the server from
    its list; None without --live), "message"}."""
    printable = launcher().printable
    starts = starts_module()
    if live:
        entry, record, data = live_target(start)
    else:
        entry = starts.find(start)
        record, data = None, None
        if entry["kind"] == "mcp":
            raise ValueError(f"{printable(entry['name'])} is an MCP server's sandbox: a server is attached to an "
                             "agent's start")
        if not entry.get("directory"):
            raise ValueError(f"{printable(entry['name'])} is a run without a kept start: it keeps no options to "
                             "take the server out of; --live closes its relay while it runs")
    shown = printable(entry["name"])
    if helper:
        workspace = (record or {}).get("workspace") or entry.get("workspace")
        if not workspace:
            raise ValueError(f"{shown}: its main workspace is not known, so its helper's name is not either")
        name = launcher().helper_name(helper, workspace)
    if not re.fullmatch(launcher().MCP_NAME, name or ""):
        raise ValueError(f"Invalid MCP server name: {name!r}")
    agent = (record or {}).get("agent") if (record or {}).get("agent") in LIVE_AGENTS else entry.get("agent")
    result = {"server": name, "start": entry["name"], "run": (record or {}).get("run") or entry.get("run"),
              "state": entry["state"], "relay_closed": False, "agent": agent, "agent_config": None,
              "agent_restart": live_agent(agent)["restart"] if live else None}
    if live:
        with run_lock(record):
            answer = net_module().ask(record["directory"], {"op": "mcp-close", "name": name})
            result["relay_closed"] = bool(answer.get("closed"))
            record_server(record, name, add=False)
            try:
                result["agent_config"] = agent_config(record, name, add=False)
            except (OSError, ValueError) as exc:
                out(f"  the agent's configuration was not changed: {printable(str(exc))}")
    try:
        result["removed_from_options"] = keep_server(entry["id"], name, helper, add=False)
    except (OSError, ValueError) as exc:
        result["removed_from_options"] = False
        out(f"  still in its options: {printable(str(exc))}")
    removed = result["removed_from_options"]
    later = ", and its later starts go without it" if removed else ""
    if result["relay_closed"]:
        result["detached"] = "live"
        how = live_agent(agent)["detach"].format(name=name, start=shown)
        result["message"] = f"{shown} no longer reaches the MCP server {name}: its relay is closed{later}. {how}"
    elif removed:
        result["detached"] = "next start"
        result["message"] = (f"{shown} goes without the MCP server {name} from its next start on" +
                             (f"; it still reaches it now (sandbox-ai mcp detach {name} {shown} --live closes it now)"
                              if entry["state"] in ("running", "paused") and not live else ""))
    else:
        result["detached"] = "not attached"
        result["message"] = f"{shown} does not use the MCP server {name}"
    result["message"] += ". The server's own sandbox keeps running (sandbox-ai mcp stop NAME ends it)" \
        if result["detached"] != "not attached" else ""
    LOG.info("mcp", f"detach {helper and 'the helper ' + helper or name} from {entry['name']}"
                    f"{' --live' if live else ''}: {result['detached']}")
    out(result["message"])
    return result


class LauncherVersion(argparse._VersionAction):
    """-V/--version: sandbox-ai's version, from the launcher, which is loaded only when it is asked for (a listing
    loads none)."""

    @property
    def version(self):
        return f"sandbox-ai {launcher().__version__}"

    @version.setter
    def version(self, value):
        pass


def server_list(served=None):
    """`mcp list --json`: [{"name", "status" (running: one of its instances runs), "command", "serves", "limits" (its
    defaults, for every start; None for a helper), "instances": [{"instance", "status", "limits", "started_for"}] (the
    running ones: NAME, and NAME.HASH of starts with limits of their own), "kind" (image, rootfs, command, helper),
    "own" (in your mcp.json: `mcp set` changes it), "definition", "builtin_defaults" (what an empty limit of `mcp set`
    gives), "last_problem" ({time, what, why, fix}: what ended or failed its processes last; None), "below_recommended"
    ({key: value} of the limits below what its catalog entry recommends: `mcp set NAME --recommended`), "catalog",
    "catalog_source", "signed"}]."""
    served = {} if served is None else served
    found = []
    try:
        own = set(read_config()[1])
    except (OSError, ValueError):
        own = set()
    for name, d in sorted(definitions().items()):
        defaults = default_limits(d)
        running = []
        for instance in instances(name):
            state = load_state(instance)
            limits = state.get("limits") if isinstance(state.get("limits"), dict) else \
                defaults if instance == name else None
            running.append({"instance": instance, "status": "running", "limits": limits,
                            "started_for": state.get("started_for"), "last_problem": last_problem(instance),
                            # The VPN it really runs behind (its start's: own, the start's, mcp start --vpn), and since.
                            "vpn": state.get("vpn"), "started": state.get("started"),
                            # The folders an instance of a start's own mounts (--mcp-mount).
                            **({"mounts": state["mounts"]} if isinstance(state.get("mounts"), list)
                               and state["mounts"] and instance != name else {})})
        # What ended or failed its processes last (mcp.py's watch), of any of its instances, running or not.
        problems = [p for p in (last_problem(i) for i in instances(name, include_stopped=True)) if p]
        problem = max(problems, key=lambda p: str(p.get("time") or "")) if problems else None
        found.append({"name": name, "status": "running" if running else "stopped",
                      "command": (d["image"] if "image" in d else f"rootfs {d['rootfs']['sha256'][:12]}" if "rootfs" in d
                                  else " ".join(d["command"])) + (f"  (vpn {d['vpn']})" if d.get("vpn") else ""),
                      "serves": served.get(name, []), "limits": defaults, "instances": running,
                      "kind": "image" if "image" in d else "rootfs" if "rootfs" in d else
                      "helper" if "helper" in d else "command", "own": name in own,
                      "definition": {key: value for key, value in d.items() if key != "rootfs"},
                      # Its variables, each with where its value comes from (variables_of; a secret's value never).
                      "variables": variables_of(d) if "image" in d or "rootfs" in d else [],
                      "builtin_defaults": default_limits({"image": ""} if "image" in d or "rootfs" in d
                                                         else {"command": []}) if "helper" not in d else None,
                      "last_problem": problem,
                      "below_recommended": below_recommended(d) if "image" in d or "rootfs" in d else {},
                      # Where it reaches (an image server: network_label; a command server: its sandbox's own
                      # rules), and what to say of it (egress_note).
                      "network": network_label(d) if "image" in d or "rootfs" in d else None,
                      "egress_note": egress_note(name, d, cached_entry(d)) if "image" in d or "rootfs" in d
                      else None,
                      **origin(name, d)})
    return found


def catalog_json(prefix, entry):
    """Catalog ENTRY (of the catalog PREFIX) as `mcp catalog --json` prints it: what the listing says (entry, title,
    network and hosts, published, signature good|bad|none, the catalog's name) and the variables it declares
    ([{name, description, secret, required}]: the web page's add form shows them), its tool_timeout. A catalog may be
    unsigned: every text one printable line, bounded; a variable without a proper name is left out."""
    def shown(text, limit=300):
        return launcher().printable(" ".join(str(text).split()))[:limit]
    signature = (entry.get("_signature") or {}).get("state")
    variables = [{"name": item["name"], "description": shown(item.get("description") or "", 500),
                  "secret": bool(item.get("secret")), "required": bool(item.get("required"))}
                 for item in entry.get("env", []) if isinstance(item, dict) and isinstance(item.get("name"), str)
                 and ENV_NAME.fullmatch(item["name"])]
    # The folders it takes ([{target, required, description}]): one it requires is asked for before `mcp add` (the
    # app's Board), not left for add to refuse with "needs --mount TARGET=HOST".
    mounts = [{"target": item["target"], "required": bool(item.get("required")),
               "description": shown(item.get("description") or "", 300)}
              for item in entry.get("mounts", []) if isinstance(item, dict) and isinstance(item.get("target"), str)
              and item["target"].startswith("/") and item["target"].isprintable() and len(item["target"]) <= 200]
    return {"entry": f"{shown(prefix)}:{shown(entry.get('name'))}", "prefix": shown(prefix),
            "name": shown(entry.get("name")), "title": shown(entry.get("title", "")),
            "network": shown(entry.get("network", "none")), "hosts": [shown(host) for host in entry.get("hosts", [])],
            "published": bool(entry.get("digest")), "signature": {"good": "good", "bad": "bad"}.get(signature, "none"),
            "catalog": shown(catalog_label(entry["_catalog"])) if isinstance(entry.get("_catalog"), str) else None,
            "env": variables, "mounts": mounts, "tool_timeout": entry_tool_timeout(entry),
            # (0.1.98) what Add… on another host shows as this machine's does: its description and the limits it
            # recommends (recommended_limits: checked, {} when it names none)
            "description": shown(entry.get("description") or "", 1000), "limits": recommended_limits(entry)}


_copy = None


def copy_module():
    """mcp_copy.py: mcp export, import and copy (a server of one host taken over by another), with this module's
    names."""
    global _copy
    if _copy is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_copy", HERE / "mcp_copy.py")
        with LOADING:
            if _copy is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded.bind(globals())
                _copy = loaded
    return _copy


_build = None


def build_module():
    """mcp_build.py: mcp add --build (a catalog entry without a signed image, built here from its source), with this
    module's names."""
    global _build
    if _build is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_build", HERE / "mcp_build.py")
        with LOADING:
            if _build is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded.bind(globals())
                _build = loaded
    return _build


def build_question(text, yes=False):
    """The human's answer to TEXT (mcp add --build: what is built, from which commit, unsigned): yes with YES
    (--yes: asked already, by the app or the web interface), else asked on a terminal; without one, refused."""
    if yes:
        return True
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("nothing is built without a yes: run it on a terminal, or look at what would be built "
                         "(--build-plan) and pass --yes with its --commit and --source-sha256")
    print(f"sandbox-ai mcp: {launcher().printable(text)}", file=sys.stderr)
    try:
        return input("sandbox-ai mcp: build it? [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
    except EOFError:
        return False


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


PINS = {}


def pins_module():
    """mcp_pins.py: the tool definitions pinned per project (sandbox-ai mcp tools)."""
    with LOADING:
        if "mcp_pins" not in PINS:
            spec = importlib.util.spec_from_file_location("sandbox_mcp_pins", HERE / "mcp_pins.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            PINS["mcp_pins"] = loaded
        return PINS["mcp_pins"]


def build_cli(a, sources):
    """mcp add NAME --build | --build-plan (mcp_build.py)."""
    if a.commit is not None and not re.fullmatch(r"[0-9a-f]{40}", a.commit):
        raise ValueError("--commit takes the full commit (40 hex digits)")
    if a.source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", a.source_sha256):
        raise ValueError("--source-sha256 takes 64 hex digits")
    if a.no_pull:
        raise ValueError("--no-pull is for an image that is pulled; --build builds one")
    builder = build_module()
    options = {"local": a.local, "mounts": a.mount, "env": a.env, "network": a.network, "memory": a.memory,
               "pids": a.pids, "vpn": a.vpn, "backend": a.backend, "cpus": a.cpus, "tmp": a.tmp,
               "allow_private": a.allow_private, "allow_tcp": a.allow_tcp}
    if a.build_plan:
        found = builder.build_and_add(a.name, options, sources, unsigned=a.unsigned, commit=a.commit,
                                      source_sha256=a.source_sha256, plan_only=True,
                                      out=lambda text: print(text, file=sys.stderr))
        print(json.dumps(found, indent=2))
        return 0
    try:
        builder.build_and_add(a.name, options, sources, unsigned=a.unsigned, commit=a.commit,
                              source_sha256=a.source_sha256, confirm=lambda text: build_question(text, a.yes))
    except builder.BuildFailed as exc:
        # The log's end first, then what failed and where the whole log is: the last line (the app shows it).
        if exc.tail:
            print("sandbox-ai mcp: the log's end:\n" + launcher().printable(exc.tail), file=sys.stderr)
        print(f"sandbox-ai mcp: {exc.summary}" + (f"; the whole log: {exc.log}" if exc.log else ""), file=sys.stderr)
        return 1
    return 0


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai mcp", formatter_class=help_style,
                                     description="Manage local MCP servers that run in their own sandboxes. Agents use "
                                                 "them with --mcp NAME.")
    parser.add_argument("-V", "--version", action=LauncherVersion)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Defined servers, their status and the starts they serve")
    pinned = commands.add_parser("tools", help="The tool definitions pinned for a project (a change is asked)",
                                 description="The MCP tool definitions pinned for a project: a later change of a "
                                             "server's tools is asked about (approval kind mcp-tools).")
    pinned.add_argument("name", nargs="?", metavar="NAME", help="one server: its pinned tools with their definitions")
    pinned.add_argument("--project", metavar="DIR", default=".", help="the project (its main workspace; default: here)")
    pinned.add_argument("--forget", action="store_true", help="drop NAME's pin: its next list is pinned anew")
    pinned.add_argument("--repin", action="store_true", help="pin the list NAME's relay saw last instead (refused "
                                                             "when the hidden-text checker finds something in it)")
    pinned.add_argument("--all", action="store_true", help="the pins of every project, each with its folder")
    pinned.add_argument("--json", action="store_true", help="as JSON (for programs)")
    listing.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    start = commands.add_parser("start", help="Start a server sandbox now")
    start.add_argument("name")
    start.add_argument("-b", "--backend", choices=["podman", "bwrap"], default="podman", help="the sandbox backend of the server: podman or bwrap")
    start.add_argument("-v", "--vpn", metavar="PROFILE", help="Put this VPN profile in front (sandbox-ai vpn list)")
    start.add_argument("-f", "--for", dest="for_start", metavar="START",
                       help="for this agent start (sandbox-ai list): started as it starts it (its backend and rules) "
                            "and attached to it (as mcp attach)")
    start.add_argument("-r", "--restart", action="store_true",
                       help="with --for: a running start is restarted with it now (the agent continues its "
                            "conversation)")
    start.add_argument("-j", "--json", action="store_true", help="with --for: the result as JSON")
    attaching = commands.add_parser("attach", help="Connect a server to an agent start (kept for its later starts)",
                                    usage="sandbox-ai mcp attach NAME START [--live | --restart] [--json]\n"
                                          "       sandbox-ai mcp attach --helper AGENT START --live [--json]")
    attaching.add_argument("name", metavar="NAME", help="the server (sandbox-ai mcp list); with --helper: START")
    attaching.add_argument("start", metavar="START", nargs="?", help="the agent start (sandbox-ai list)")
    attaching.add_argument("-H", "--helper", metavar="AGENT", choices=["claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"],
                           help="the agent AGENT as a helper (an MCP server of its own sandbox), as --helper at a start")
    attaching.add_argument("-r", "--restart", action="store_true",
                           help="a running start is restarted with it now (the agent continues its conversation); "
                                "without it (and without --live) a running start reaches the server from its next "
                                "start")
    attaching.add_argument("-l", "--live", action="store_true",
                           help="into the running sandbox now, without a restart: its relay is opened and the agent's "
                                "MCP configuration updated (Claude Code: /mcp; Codex and OpenCode: after a restart "
                                "of the agent)")
    attaching.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    detaching = commands.add_parser("detach", help="take a server from an agent start",
                                              description="Take a server from an agent start (and with --live from "
                                                          "its running sandbox now).",
                                    usage="sandbox-ai mcp detach NAME START [--live] [--json]\n"
                                          "       sandbox-ai mcp detach --helper AGENT START [--live] [--json]")
    detaching.add_argument("name", metavar="NAME", help="the server; with --helper: START")
    detaching.add_argument("start", metavar="START", nargs="?", help="the agent start (sandbox-ai list)")
    detaching.add_argument("-H", "--helper", metavar="AGENT", choices=["claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"], help="detach from this agent's helper start instead of NAME's start")
    detaching.add_argument("-l", "--live", action="store_true",
                           help="close its relay in the running sandbox now (the agent's connections to it end)")
    detaching.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    commands.add_parser("stop", help="Stop a server sandbox: all of its instances (NAME.HASH: that one)").add_argument(
        "name")
    setting = commands.add_parser("set", help="change a server of yours (mcp.json)",
                                         description="Change a server of yours (mcp.json): its limits for every "
                                                     "start, its network, folders, variables, VPN; checked as add "
                                                     "checks them.")
    setting.add_argument("name")
    setting.add_argument("-M", "--memory", metavar="SIZE", help="Memory limit, e.g. 2g; default: back to the default")
    setting.add_argument("-C", "--cpus", metavar="N", help="CPU limit, e.g. 2 or 1.5; default: back to the default")
    setting.add_argument("-p", "--pids", metavar="N", help="Process limit, 16-4096; default: back to the default")
    setting.add_argument("-t", "--tmp", metavar="SIZE",
                         help="Size of its /tmp, e.g. 1g (memory: at most the memory limit); default: back to 64m")
    setting.add_argument("-R", "--recommended", action="store_true",
                         help="The limits its catalog entry recommends (those given here win)")
    setting.add_argument("-n", "--network", choices=list(NETWORKS),
                         help="the network of the server: full (the internet, not your local network), allowlist (only the "
                              "hosts its catalog entry declares), lan (the internet and your local network: a warning) "
                              "or none")
    setting.add_argument("-ap", "--allow-private", action=ListAppend, metavar="CIDR",
                         help="A network of yours a full or allowlist server may reach too (192.168.1.0/24; a "
                              "warning), all of them; one or more, separated by commas or spaces; repeatable")
    setting.add_argument("-at", "--allow-tcp", action=ListAppend, metavar="IP:PORT",
                         help="An address and TCP port of your network it may reach too (192.168.1.20:8080, "
                              "[fd00::7]:8080; a warning), all of them; one or more, separated by commas or spaces; "
                              "repeatable")
    setting.add_argument("--no-allow", action="store_true",
                         help="None of your network again (takes --allow-private and --allow-tcp away)")
    setting.add_argument("-m", "--mount", action="append", metavar="TARGET=HOST[:ro|rw]",
                         help="The folders, all of them (TARGET: in the server's container, of those its catalog entry "
                              "takes; HOST: a folder on this machine, a writable one is created; held to the "
                              "host-path policy of its start); repeatable")
    setting.add_argument("--no-mounts", action="store_true", help="No folders")
    setting.add_argument("-e", "--env", action="append", metavar="NAME[=VALUE|@SECRET]",
                         help="The variables, all of them: NAME (your environment's, else the secret NAME), "
                              "NAME=VALUE (a value that is no secret, kept in the definition) or NAME@SECRET (that "
                              "secret of sandbox-ai secrets); repeatable")
    setting.add_argument("--no-env", action="store_true", help="No variables")
    setting.add_argument("-v", "--vpn", metavar="PROFILE", help="Its network only through this VPN profile")
    setting.add_argument("--no-vpn", action="store_true", help="No VPN of its own")
    setting.add_argument("-T", "--tool-timeout", metavar="SECONDS",
                         help="How long an agent waits for one call of it, 10-3600 seconds (a server whose commands "
                              "run long); default: back to sandbox-ai's (660)")
    setting.add_argument("-s", "--stop", action="store_true",
                         help="Stop its running instances now: the next start that uses it starts it with the change")
    setting.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    catalog = commands.add_parser("catalog", help="Servers of the MCP catalogs (sandbox-ai-mcp, axross-mcp-hub)")
    catalog.add_argument("-c", "--catalog", action="append", default=[], metavar="FILE_OR_URL",
                         help="Read this catalog instead of the default ones; repeatable")
    catalog.add_argument("-j", "--json", action="store_true",
                         help="{entries: [...], problems: [...]}: each entry with the variables its catalog declares")
    added = commands.add_parser("add", help="Add a catalog server: pull its image by digest, write its definition")
    added.add_argument("name", help="Catalog entry, e.g. duckduckgo or hub:binwalk")
    added.add_argument("-a", "--as", dest="local", metavar="NAME", help="Name for --mcp (default: the entry's)")
    added.add_argument("-m", "--mount", action="append", default=[], metavar="TARGET=HOST[:ro|rw]",
                       help="A folder of the server (TARGET: in its container, one its catalog entry takes - "
                            "exiftool's and binwalk's /work, engram's and memory's data; HOST: a folder on this "
                            "machine, a writable one is created); held to the host-path policy of its start before "
                            "anything is pulled (not HOME itself, nothing of sandbox-ai's own); repeatable")
    added.add_argument("-e", "--env", action="append", default=[], metavar="NAME[=VALUE|@SECRET]",
                       help="A variable of the entry: NAME passes your environment's NAME, else the secret NAME "
                            "(required where the entry requires it, else only where there is one); NAME=VALUE keeps a "
                            "value that is no secret in the definition (ALLOW_PRIVATE_TARGETS=1; never a secret's); "
                            "NAME@SECRET takes the secret SECRET of sandbox-ai secrets; repeatable")
    added.add_argument("-n", "--network", choices=list(NETWORKS),
                       help="Override the entry's network: full (the internet, not your local network), allowlist "
                            "(only the hosts its entry declares: the default of such an entry), lan (the internet "
                            "and your local network: a warning) or none")
    added.add_argument("-ap", "--allow-private", action=ListAppend, default=[], metavar="CIDR",
                       help="A network of yours it may reach too, e.g. 192.168.1.0/24 (a warning); one or more, "
                            "separated by commas or spaces; repeatable")
    added.add_argument("-at", "--allow-tcp", action=ListAppend, default=[], metavar="IP:PORT",
                       help="An address and TCP port of your network it may reach too, e.g. 192.168.1.20:8080 "
                            "(SearXNG on your LAN; a warning); one or more, separated by commas or spaces; "
                            "repeatable")
    added.add_argument("-M", "--memory", metavar="SIZE", help="Memory limit, e.g. 2g (default 512m)")
    added.add_argument("-C", "--cpus", metavar="N",
                       help="CPU limit of each connection's container, e.g. 2 or 1.5 (default: half this host's CPUs)")
    added.add_argument("-p", "--pids", type=int, metavar="N", help="Process limit (default 128)")
    added.add_argument("-t", "--tmp", metavar="SIZE",
                       help="Size of its /tmp, e.g. 1g (default 64m; memory, so at most the memory limit)")
    added.add_argument("-c", "--catalog", action="append", default=[], metavar="FILE_OR_URL", help="also read this catalog (a file or an https URL; repeatable)")
    added.add_argument("-N", "--no-pull", action="store_true", help="Do not pull the image (it is there already)")
    added.add_argument("-b", "--backend", choices=["podman", "bwrap", "both"],
                       help="Podman (the image), Bubblewrap (the image's rootfs, checked and unpacked) or both; "
                            "default: Podman when it is there")
    added.add_argument("-U", "--unsigned", action="store_true",
                       help="Take an entry whose catalog is not signed by a trusted key (sandbox/mcp-signers); a "
                            "signature that does not verify is refused anyway")
    added.add_argument("-v", "--vpn", metavar="PROFILE",
                       help="Its network only through this VPN profile, with a kill switch (a server with network)")
    added.add_argument("-B", "--build", action="store_true",
                       help="An entry with no signed image yet (not published): build it here with Podman from its "
                            "reviewed source - the catalog's repository at the catalog's exact commit, servers/NAME, "
                            "as its CI builds it - check it with its smoke test, and add it UNSIGNED (built here, not "
                            "signed). Asked first, naming the commit and the source archive's SHA-256")
    added.add_argument("--build-plan", action="store_true",
                       help="What --build would build (catalog, repository, commit, the source archive's SHA-256, the "
                            "question) as JSON; nothing is built")
    added.add_argument("--commit", metavar="SHA",
                       help="With --build: only this commit (the one asked about); refused when the catalog is at "
                            "another one now")
    added.add_argument("--source-sha256", metavar="HEX",
                       help="With --build: only a source archive of this SHA-256 (the one asked about)")
    added.add_argument("-y", "--yes", action="store_true",
                       help="With --build: build without the question (asked already: give --commit and "
                            "--source-sha256 of what was asked)")
    exporting = commands.add_parser("export", help="what a server of yours is, for another host (no secret's value)",
                                    description="What the server NAME of your mcp.json is, as another host needs to "
                                                "know it (mcp import, mcp copy): a catalog server's entry and the "
                                                "choices you made, a server of your own's definition; the names of the "
                                                "secrets it uses, never a value; its host folders; its image.")
    exporting.add_argument("name")
    exporting.add_argument("-j", "--json", action="store_true", help="the document (what mcp import reads)")
    exporting.add_argument("--image", action="store_true",
                           help="its Podman image as podman save writes it, on standard output (mcp copy "
                                "--copy-image)")

    def resolving(command):
        command.add_argument("-a", "--as", dest="as_name", metavar="NEWNAME", help="under this name there")
        command.add_argument("--replace", action="store_true",
                             help="replace a server of that name there (asked first; --yes when nothing can ask)")
        command.add_argument("-y", "--yes", action="store_true", help="with --replace: without asking")
        command.add_argument("-P", "--path", action="append", default=[], metavar="OLD=NEW",
                             help="the source's folder OLD is NEW there (NEW=OLD keeps it); a folder of the source "
                                  "is never mounted there unasked; repeatable")
        command.add_argument("-D", "--drop-path", action="append", default=[], metavar="OLD",
                             help="leave the source's folder OLD out; repeatable")
        command.add_argument("--secrets-later", action="store_true",
                             help="copied although a secret it uses is missing there (keep it before it starts)")
        command.add_argument("--pull-image", action="store_true",
                             help="a server of your own whose image is not there: Podman pulls it there by its digest")
        command.add_argument("--no-vpn", action="store_true", help="without the VPN profile it is behind")
        command.add_argument("-U", "--unsigned", action="store_true",
                             help="a catalog server its source took unsigned: take it unsigned there too")
        command.add_argument("--current-pin", action="store_true",
                             help="a catalog server: the image the catalog there pins, where it pins another")
        command.add_argument("-c", "--catalog", action="append", default=[], metavar="FILE_OR_URL",
                             help="the catalog to read there for a catalog server (default: the one it came from)")
        command.add_argument("-n", "--dry-run", action="store_true", help="say what would be done; change nothing")
        command.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    importing = commands.add_parser("import", help="take in a server another host exported (checked as add checks)",
                                    description="Take in the server of an export (sandbox-ai mcp export NAME --json "
                                                "on another host): a catalog server is added from the catalog as "
                                                "this host reads it (its signature checked here), a server of your "
                                                "own's definition is checked as mcp.json is. What this host cannot "
                                                "take as it is - a name it has, a secret it lacks, a folder of the "
                                                "source, an image it lacks, a VPN profile it lacks - is said with "
                                                "how to resolve it, and nothing is written.")
    importing.add_argument("file", metavar="FILE", help="the export (- reads standard input)")
    resolving(importing)
    copying = commands.add_parser("copy", help="copy servers from one host to another (sandbox-ai hosts)",
                                  description="Copy MCP servers from one host to another (sandbox-ai hosts; local: "
                                              "this machine): each exported there (mcp export), checked on the "
                                              "target (mcp import --dry-run: every problem of every server said, "
                                              "nothing written while there is one), then imported there. A catalog "
                                              "server is added from its catalog there (the same pinned image), never "
                                              "copied raw; secrets travel by name only.")
    copying.add_argument("names", nargs="+", metavar="NAME", help="servers of the source host (mcp list there)")
    copying.add_argument("-f", "--from", dest="source", required=True, metavar="HOST",
                         help="the host they are on (local: this machine)")
    copying.add_argument("-t", "--to", dest="target", required=True, metavar="HOST",
                         help="the host to copy them to (local: this machine)")
    copying.add_argument("--attach", metavar="RUN",
                         help="then attach them to this running start of the target (mcp attach --live there)")
    copying.add_argument("--copy-image", action="store_true",
                         help="a server of your own whose image the target lacks: sent from the source (podman save "
                              "| podman load, its size said); used only if it keeps its pinned digest there")
    resolving(copying)
    loading = commands.add_parser("image-load", help="load a server's image from standard input (mcp copy)",
                                  description="Load an image archive from standard input into Podman (what mcp copy "
                                              "--copy-image sends); it counts only when Podman then has REF, the "
                                              "server's pinned image.")
    loading.add_argument("ref", metavar="REF", help="REGISTRY/PATH@sha256:DIGEST")
    a = parser.parse_args(argv)
    if a.command == "tools":  # mcp_pins.py, with the same options
        return pins_module().main(list(argv[1:]))
    if a.command in ("export", "import", "copy", "image-load"):
        copier = copy_module()
        if a.command == "export":
            if a.image:
                return copier.save_image(a.name)
            document = copier.export(a.name)
            print(json.dumps(document, indent=2) if a.json else copier.export_text(document))
            return 0
        if a.command == "import":
            return copier.import_main(a)
        if a.command == "copy":
            return copier.copy_main(a)
        return copier.load_image(a.ref, out=lambda text: print(text, file=sys.stderr))
    if a.command in ("catalog", "add"):
        sources = [(f"c{index}", value) for index, value in enumerate(a.catalog)] or None
    if a.command == "catalog":
        entries, problems = catalog_entries(sources)
        if a.json:
            print(json.dumps({"entries": [catalog_json(prefix, entry) for prefix, entry in
                                          sorted(entries, key=lambda pe: (pe[0], str(pe[1].get("name"))))],
                              "problems": [launcher().printable(str(problem))[:600] for problem in problems]},
                             indent=2))
            return 0
        for problem in problems:
            print(f"sandbox-ai mcp: catalog not read: {problem}", file=sys.stderr)
        for prefix, entry in sorted(entries, key=lambda pe: (pe[0], str(pe[1].get("name")))):
            signature = entry.get("_signature") or {}
            state = ("" if entry.get("digest") else "  (not published yet)") + (
                "  signed" if signature.get("state") == "good" else "  SIGNATURE BAD" if signature.get("state") == "bad"
                else "  unsigned")
            network = entry.get("network", "none") + (f" {','.join(entry.get('hosts', []))}" if entry.get("hosts") else "")
            # One line per entry, whatever a catalog holds: its texts printable (a newline in an unsigned
            # catalog's title would otherwise forge another, "signed" row for the readers of these lines).
            def shown(text):
                return launcher().printable(" ".join(str(text).split()))
            print(f"{shown(prefix)}:{shown(str(entry.get('name')))}  [{shown(network)}]  "
                  f"{shown(str(entry.get('title', '')))}{state}")
        return 0
    if a.command == "add" and (a.build or a.build_plan):
        return build_cli(a, sources)
    if a.command == "add":
        for option, given in (("--commit", a.commit), ("--source-sha256", a.source_sha256), ("--yes", a.yes)):
            if given:
                raise ValueError(f"{option} is for --build")
        try:
            add(a.name, a.local, a.mount, a.env, a.network, a.memory, a.pids, sources, pull=not a.no_pull, vpn=a.vpn,
                unsigned=a.unsigned, backend=a.backend, cpus=a.cpus, tmp=a.tmp, allow_private=a.allow_private,
                allow_tcp=a.allow_tcp)
        except subprocess.CalledProcessError as exc:
            # Its image could not be pulled (its last lines are said above): nothing is added - a message, never a
            # traceback (the app's Preparing tab shows this).
            local = a.local or str(a.name).rpartition(":")[2]
            print(f"sandbox-ai mcp: {launcher().printable(local)} is not added: pulling its image failed (exit status "
                  f"{exc.returncode}); check the network and Podman (sandbox-ai doctor), then add it again",
                  file=sys.stderr)
            return 1
        return 0
    if a.command == "set":
        result = set_server(a.name, set_changes(a), stop_running=a.stop,
                            out=(lambda text: print(text, file=sys.stderr)) if a.json else print)
        if a.json:
            print(json.dumps(result, indent=2))
        return 0
    if a.command == "list":
        try:
            served = serving()
        except (OSError, ValueError) as exc:  # the starts could not be read: the servers are listed anyway
            print(f"sandbox-ai mcp: the starts they serve are unknown: {exc}", file=sys.stderr)
            served = {}
        found = server_list(served)
        if a.json:
            print(json.dumps(found, indent=2))
            return 0
        printable = launcher().printable
        rows = [["NAME", "STATUS", "LIMITS", "SERVES", "CATALOG", "COMMAND"]]
        notes = []
        for e in found:
            rows.append([e["name"], e["status"] + (" (!)" if e["last_problem"] else ""),
                         limits_text(e["limits"]) + (" (below its catalog's)" if e["below_recommended"] else ""),
                         printable(", ".join(e["serves"]) or "-"),
                         printable(e["catalog"] + (", built here, not signed" if e.get("built") else
                                                   {True: ", signed", False: ", unsigned", None: ""}[e["signed"]])),
                         printable(e["command"])])
            if e["below_recommended"]:
                notes.append(f"{e['name']}: below the limits its catalog recommends ({limits_text(e['below_recommended'])})"
                             f": sandbox-ai mcp set {e['name']} --recommended")
            if e.get("egress_note"):
                notes.append(printable(f"{e['name']}: {e['egress_note']}"))
            if e["last_problem"]:
                problem = e["last_problem"]
                notes.append(printable(f"{e['name']}: {problem.get('time')}: {problem.get('what')}: {problem.get('why')}"
                                       f". Fix: {problem.get('fix')}"))
            # Its instances with limits or folders of their own (--mcp-limits, --mcp-mount), below it.
            rows += [[f"  {i['instance']}", i["status"], limits_text(i["limits"]), "-", "",
                      printable(", ".join(f"{m.get('target')}={m.get('host')}:{m.get('mode')}"
                                          for m in i.get("mounts") or [] if isinstance(m, dict)))]
                     for i in e["instances"] if i["instance"] != e["name"]]
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        for row in rows:
            print(("  ".join(cell.ljust(width) for cell, width in zip(row[:5], widths)) + f"  {row[5]}").rstrip())
        if notes:
            print()
            for note in notes:
                print(note)
        return 0
    if a.command == "start" and a.for_start is None:
        if a.restart or a.json:
            raise ValueError("--restart and --json go with --for START")
        given = launcher().parse_args(["codex", "--backend", a.backend])
        if a.name in start_servers(given):
            # What an agent start with --mcp NAME checks first (its image there, its folders, policy, secrets):
            # "started" was said for a server whose every connection would then fail (its image never pulled).
            problems = [problem for problem in start_problems(given, [a.name])[0]  # (--vpn: in front of its own)
                        if not (a.vpn and problem.startswith(f"MCP server {a.name}: its VPN: "))]
            if problems:
                raise ValueError(problems_text(problems, f"the MCP server {a.name}"))
        ensure_running(a.name, given, a.vpn)
        return 0
    if a.command in ("attach", "detach"):
        if (a.start is None) != bool(a.helper):
            raise ValueError(f"give {'--helper AGENT START' if a.helper else 'NAME START'}")
        name, target = (None, a.name) if a.helper else (a.name, a.start)
        say = (lambda text: print(text, file=sys.stderr)) if a.json else print
        if a.command == "detach" or a.live:
            if a.command == "attach" and a.restart:
                raise ValueError("--live attaches without a restart; --restart restarts: choose one")
            result = (detach(name, target, a.helper, a.live, out=say) if a.command == "detach" else
                      attach_live(name, target, a.helper, out=say))
            if a.json:
                print(json.dumps(result, indent=2))
            return 0
        if a.helper:
            # A helper joins a start that runs (--live), or comes with a new start; a stopped start keeps its options.
            raise ValueError(f"--helper AGENT joins a running start: sandbox-ai mcp attach --helper {a.helper} "
                             f"{launcher().printable(target)} --live while it runs; for a start that does not run, "
                             f"give --helper {a.helper} to a new start (its kept options stay as they are)")
        a.name, a.start = name, target
    if a.command in ("start", "attach"):
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not a.json

        def ask(question):
            answer = input(f"{launcher().printable(question)} [y/N] ")
            return answer.strip().lower() in ("y", "yes", "j", "ja")
        say = (lambda text: print(text, file=sys.stderr)) if a.json else print
        result = attach(a.name, a.for_start if a.command == "start" else a.start, restart=a.restart,
                        start_server=a.command == "start", vpn=getattr(a, "vpn", None),
                        ask=ask if interactive else None, out=say)
        if a.json:
            print(json.dumps(result, indent=2))
        return 0
    return stop(a.name)


# --- what ended a server's process: its container's out-of-memory kills, its process limit, a full /tmp -------------
#
# Each connection's container (or Bubblewrap scope) runs under `mcp.py --internal-watch DIR INSTANCE TMP -- COMMAND`:
# the command with its stdin and stdout as they are (the MCP connection), and beside it a look at the container's
# cgroup every WATCH_INTERVAL seconds - memory.events (oom_kill), pids.events (max) and memory.stat (shmem: the files
# of its tmpfs /tmp and /dev/shm). What it sees, and an exit that was not asked for, becomes a safeguards record in the
# instance's state directory (DIR/safeguards.jsonl: `sandbox-ai safeguards --all`, `mcp list`, the app's MCP page),
# with the reason and the fix - instead of the agent seeing only "crashed". A renderer the kernel killed inside a
# browser's container is seen even while the server runs on.

WATCH_INTERVAL = 2.0
WATCH_FILE = "safeguards.jsonl"
WATCH_KIND = "mcp-server"
CGROUP_ROOT = Path("/sys/fs/cgroup")
_safeguards = None


def safeguards_module():
    global _safeguards
    if _safeguards is None:
        spec = importlib.util.spec_from_file_location("safeguards_for_mcp", HERE / "safeguards.py")
        with LOADING:
            if _safeguards is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _safeguards = loaded
    return _safeguards


def process_cgroup(pid, proc=None, root=None):
    """The cgroup (v2) directory of process PID, or None: its container's scope (libpod-ID.scope, or a systemd-run
    scope run-*.scope) when it lies in one, never one of this host's own slices."""
    proc, root = proc or PROC, root or CGROUP_ROOT
    try:
        text = (Path(proc) / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::/"):
            path = Path(root) / line[4:]
            for candidate in (path, *path.parents):
                if candidate.name.endswith(".scope") and candidate.name.startswith(("libpod-", "run-")):
                    return candidate
                if candidate == Path(root):
                    break
    return None


def cgroup_counters(directory):
    """{"oom_kill", "pids_max", "shmem", "peak"} of the cgroup DIRECTORY (what it has; {} when it is gone)."""
    found = {}
    for name, key, field in (("memory.events", "oom_kill", "oom_kill"), ("pids.events", "pids_max", "max"),
                             ("memory.stat", "shmem", "shmem")):
        try:
            for line in (Path(directory) / name).read_text().splitlines():
                word, _, value = line.partition(" ")
                if word == field and value.strip().isdigit():
                    found[key] = int(value)
        except OSError:
            continue
    try:
        found["peak"] = int((Path(directory) / "memory.peak").read_text().strip())
    except (OSError, ValueError):
        pass
    return found


def mib_text(count):
    return f"{count / (1 << 20):.0f} MiB"


class ContainerWatch:
    """What happens to one connection's container (or scope): see the module's notes above (--internal-watch)."""

    def __init__(self, directory, instance, tmp, limits=None, clock=time.monotonic):
        self.directory, self.instance, self.tmp = Path(directory), instance, tmp
        self.limits = limits or {}
        self.server = server_of(instance)
        self.seen, self.cgroup, self.tmp_said, self.records = {}, None, False, []
        self.clock = clock

    def record(self, what, why, fix, details=None):
        S = safeguards_module()
        entry = S.record(WATCH_KIND, what, why, fix, run=f"mcp-{self.instance}", target=self.server,
                         details={"instance": self.instance, **(details or {})})
        self.records.append(entry)
        with contextlib.suppress(OSError):
            S.append(self.directory / WATCH_FILE, [entry])
        return entry

    def fix(self, key, value):
        option = {"memory": "--memory", "pids": "--pids", "tmp": "--tmp"}[key]
        return (f"sandbox-ai mcp set {self.server} {option} {value} (every start; the catalog's: --recommended), or for "
                f"one start --mcp-limits {self.server}:{key}={value} (the board: its MCP node)")

    def look(self, counters):
        """Compare COUNTERS (cgroup_counters) with the last look: new kills, the process limit hit, /tmp nearly full."""
        memory, pids = self.limits.get("memory", "?"), self.limits.get("pids", "?")
        kills = counters.get("oom_kill", 0) - self.seen.get("oom_kill", 0)
        if kills > 0:
            bigger = f"{2 * size_mib(memory) // 1024 or 1}g" if size_mib(memory) else "2g"
            self.record(f"out of memory: the kernel ended {kills} process{'es' if kills > 1 else ''} of MCP server "
                        f"{self.instance}", f"its memory limit {memory} is used up (a browser's renderer is the first "
                        "to go: its page crashes, the server says it crashed)" + (
                            f"; its /tmp holds {mib_text(counters['shmem'])} of it" if counters.get("shmem") else ""),
                        self.fix("memory", bigger), {"oom_kills": counters.get("oom_kill", 0), "memory": memory})
        hits = counters.get("pids_max", 0) - self.seen.get("pids_max", 0)
        if hits > 0:
            self.record(f"process limit: MCP server {self.instance} could not start a process or thread",
                        f"its process limit {pids} is reached ({hits} refusal{'s' if hits > 1 else ''}): a program "
                        "that cannot start a thread fails or crashes", self.fix("pids", 512 if str(pids) != "512" else 1024),
                        {"pids_refused": counters.get("pids_max", 0), "pids": pids})
        tmp = size_mib(self.tmp)
        if tmp and not self.tmp_said and counters.get("shmem", 0) >= tmp * (1 << 20) * 0.9:
            self.tmp_said = True
            self.record(f"/tmp full: MCP server {self.instance}'s /tmp ({self.tmp}) is nearly full",
                        f"its /tmp and shared memory hold {mib_text(counters['shmem'])} of {self.tmp}: a program "
                        "writing there (a browser's profile, cache, shared memory, screenshots) fails or crashes",
                        self.fix("tmp", f"{max(256, tmp * 4)}m"), {"shmem": counters["shmem"], "tmp": self.tmp})
        self.seen = {**self.seen, **{k: v for k, v in counters.items() if k in ("oom_kill", "pids_max")}}

    def log_fix(self):
        """Where the server's own output is, as a path that is there: its supervisor.log, else the newest part of it
        rotated away (supervisor.log.TIME[.gz]); else that it has none and why (never a path that is not there: the
        user's `cat .../mcp/binwalk/supervisor.log` said No such file or directory)."""
        log = self.directory / "supervisor.log"
        if log.is_file():
            return f"read the server's log: {log}"
        parts = sorted(self.directory.glob("supervisor.log.*"))
        if parts:
            return f"read the server's log: {parts[-1]} (the newest part kept)"
        space = statedirs_module().space(self.directory)
        return (f"its log was never written in {self.directory} (" + (statedirs_module().space_text(space) if space
                                                                     else "that folder cannot be read")
                + "); the launcher's own log: "
                + str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"
                      / "launcher.log"))

    def ended(self, code, asked, said=""):
        """The command ended with CODE; ASKED: its end was asked for (the connection closed); SAID: the end of what it
        wrote to its error output (Podman's own error line). An end that was not asked for is recorded with what it
        means (137: SIGKILL, mostly the out-of-memory killer; 125: Podman did not start the container - its own line,
        and "no space left on device" named as a full file system)."""
        if asked or code == 0:
            return None
        killed = any(r["kind"] == WATCH_KIND and "out of memory" in r["what"] for r in self.records)
        lines = [line.strip() for line in str(said or "").splitlines() if line.strip()]
        line = next((x for x in reversed(lines) if x.lower().startswith("error")), lines[-1] if lines else "")
        line = launcher().printable(line)[-400:] if line else ""
        if code == 137:
            why = ("ended by SIGKILL (exit status 137): " + ("the kernel's out-of-memory killer (see above)" if killed
                   else f"most likely the out-of-memory killer, at its memory limit {self.limits.get('memory', '?')}"))
            fix = self.fix("memory", "2g")
        elif code == 125:
            full = "no space left on device" in line.lower() or "disk quota exceeded" in line.lower()
            why = ("podman could not start its container (exit status 125)" + (f": {line}" if line else "")
                   + ("; a file system Podman writes is full - its image store (podman info: GraphRoot) or its run "
                      "directory (RunRoot, under XDG_RUNTIME_DIR: a tmpfs mostly) - or out of inodes" if full else ""))
            fix = (("free space there (df -h and df -i on those folders; sandbox-ai gc --check shows what sandbox-ai "
                    "can free), then use the server again; ") if full else "") + self.log_fix()
        else:
            why = f"it ended with exit status {code}" + (f": {line}" if line else "")
            fix = self.log_fix()
        return self.record(f"MCP server {self.instance}'s process ended while it was used", why, fix,
                           {"exit": code})


def container_pid(cidfile, run=None, wait=30.0, sleep=time.sleep):
    """The host PID of the container whose ID podman wrote into CIDFILE (podman inspect), or None."""
    run = run or (lambda command: subprocess.run(command, capture_output=True, text=True, timeout=30))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            ident = Path(cidfile).read_text().strip()
        except OSError:
            ident = ""
        if re.fullmatch(r"[0-9a-f]{12,64}", ident):
            try:
                result = run(["podman", "inspect", "--format", "{{.State.Pid}}", ident])
            except (OSError, subprocess.SubprocessError):
                return None
            value = (result.stdout or "").strip()
            if result.returncode == 0 and value.isdigit() and int(value) > 0:
                return int(value)
        sleep(0.5)
    return None


def with_cidfile(command, cidfile):
    """(COMMAND with --cidfile=CIDFILE in its podman run line, CIDFILE), or (COMMAND, None) without one. The run line
    is the command itself, or behind a VPN the one vpn.py's guard runs (as podman create; the guard takes a --cidfile
    as it takes every option that chooses no network)."""
    at = next((index for index in range(len(command) - 1)
               if Path(command[index]).name == "podman" and command[index + 1] == "run"), None)
    if at is None:
        return list(command), None
    return [*command[:at + 2], f"--cidfile={cidfile}", *command[at + 2:]], cidfile


def watch_main(argv):
    """mcp.py --internal-watch DIR INSTANCE TMP MEMORY PIDS -- COMMAND: COMMAND (podman run ..., or the Bubblewrap
    scope's systemd-run) with this process's stdio, watched (ContainerWatch). Its exit status is COMMAND's."""
    if len(argv) < 7 or argv[5] != "--":
        print("usage: mcp.py --internal-watch DIR INSTANCE TMP MEMORY PIDS -- COMMAND", file=sys.stderr)
        return 2
    directory, instance, tmp, memory, pids, command = argv[0], argv[1], argv[2], argv[3], argv[4], argv[6:]
    watch = ContainerWatch(directory, instance, tmp, {"memory": memory, "pids": pids})
    folder = Path(directory) / "watch"
    command, cidfile = with_cidfile(command, folder / f"{os.getpid()}-{time.monotonic_ns()}.cid")
    if cidfile is not None:
        with contextlib.suppress(OSError):
            folder.mkdir(mode=0o700, exist_ok=True)
    # Its error output passed on as it comes (the server's log) and its end kept: Podman's own error line goes into
    # the record of an end that was not asked for (ContainerWatch.ended). Standard input and output stay the MCP
    # connection's.
    child = subprocess.Popen(command, stderr=subprocess.PIPE)
    tail = bytearray()

    def passing():
        while True:
            try:
                chunk = os.read(child.stderr.fileno(), 65536)
            except OSError:
                return
            if not chunk:
                return
            with contextlib.suppress(OSError):
                os.write(2, chunk)
            tail.extend(chunk)
            del tail[:-4096]
    passer = threading.Thread(target=passing, daemon=True, name="mcp-watch-stderr")
    passer.start()
    asked = []

    def forward(signum, _frame):
        asked.append(signum)
        with contextlib.suppress(OSError):
            child.send_signal(signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)

    def watching():
        pid = container_pid(cidfile) if cidfile else None
        own = process_cgroup(os.getpid())
        deadline = time.monotonic() + 10
        while child.poll() is None:
            if watch.cgroup is None and (pid or not cidfile):
                found = process_cgroup(pid or child.pid)
                if found is not None and found != own:
                    watch.cgroup = found
                    watch.seen = cgroup_counters(found)  # what was before it is not this connection's
                elif not cidfile and time.monotonic() > deadline:
                    return  # no scope of its own (no systemd): nothing to watch
            elif watch.cgroup is not None:
                counters = cgroup_counters(watch.cgroup)
                if counters:
                    watch.look(counters)
            time.sleep(WATCH_INTERVAL)
    threading.Thread(target=watching, daemon=True, name="mcp-watch").start()
    code = child.wait()
    passer.join(5)  # (what it wrote last; a process it left behind holding the pipe is not waited for)
    if watch.cgroup is not None:  # the last look, when it is still there
        counters = cgroup_counters(watch.cgroup)
        if counters:
            watch.look(counters)
    watch.ended(code if code >= 0 else 128 - code, bool(asked), bytes(tail).decode(errors="replace"))
    if cidfile is not None:
        with contextlib.suppress(OSError):
            cidfile.unlink()
    return code if code >= 0 else 128 - code


def watched(directory, instance, definition, command):
    """COMMAND (one connection's container or Bubblewrap scope) under --internal-watch."""
    limits = default_limits(definition) or {}
    return [sys.executable, "-I", str(HERE / "mcp.py"), "--internal-watch", str(directory), instance,
            str(limits.get("tmp", "64m")), str(limits.get("memory", "?")), str(limits.get("pids", "?")), "--",
            *command]


def full_text(exc):
    """"; the file system of PATH (...): N free, M inodes free ..." for an OSError EXC that is a full file system
    (ENOSPC, EDQUOT) with a file name - which file system, never "the disk" - else ""."""
    import errno
    if not isinstance(exc, OSError) or exc.errno not in (errno.ENOSPC, errno.EDQUOT) or not exc.filename:
        return ""
    with contextlib.suppress(Exception):
        found = statedirs_module().space(os.fsdecode(exc.filename))
        if found:
            return (f"; the file system of {statedirs_module().space_text(found)}: free space (sandbox-ai gc --check "
                    "shows what can go), then try again")
    return ""


def last_problem(instance):
    """The newest safeguards record of an instance (its state directory's), or None."""
    path = state_dir(instance) / WATCH_FILE
    if not path.is_file():
        return None
    records = safeguards_module().read(path, tail=1)
    if not records:
        return None
    entry = records[-1]
    return {key: entry.get(key) for key in ("time", "what", "why", "fix")}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-log":
        sys.exit(log_main(sys.argv[2]))
    if len(sys.argv) > 2 and sys.argv[1] == "--internal-watch":
        sys.exit(watch_main(sys.argv[2:]))
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"sandbox-ai mcp: {exc}" + full_text(exc), file=sys.stderr)
        sys.exit(1)
