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
            "env": ["VARIABLE"], "memory": "512m", "pids": 128, "description": "...", "source": "...",
            "vpn": "PROFILE"}}

Such a server runs per connection as `podman run --rm -i` with no capabilities, a read-only root
and only the network and folders the definition names; agents reach it the same way. With "vpn" (network
full only) its containers join the network namespace of a VPN container (sandbox-ai vpn), whose kill switch
lets nothing out but through the tunnel; a server with a command takes --vpn PROFILE in its options.

The relay in front of every server is a gateway: the MCP rules of the starting agent's rulesets allow,
ask or deny each tool call (runtime.py, McpGateway). Its records and questions reach the server's state
directory through the log reader on the host (`mcp.py --internal-log DIR`): mcp-calls.jsonl and
approvals/, which `sandbox-ai approvals` lists as the run mcp-NAME.

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
MCP_NAME_PATTERN = r"[A-Za-z0-9_-]{1,32}"
PROC = Path("/proc")
READY_TIMEOUT = 120
KEYS = {"command", "options", "agent", "workspace", "description"}
IMAGE_KEYS = {"image", "network", "mounts", "env", "memory", "pids", "description", "source", "vpn", "signed_by",
              "rootfs"}
IMAGE_REF = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+"
                       r"@sha256:[0-9a-f]{64}")
MOUNT_TARGET = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
# The run line of catalog servers: the same as axross's, so images are tested for exactly this.
RUN_FLAGS = ("--rm", "-i", "--init", "--pull=never", "--log-driver=none", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--read-only",
             "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,notmpcopyup")
# A catalog that sandbox-ai's own tree carries (projects/NAME/catalog.json: a checkout, or what install.sh
# copied) is read before its published URL: the projects are not all published yet, and the local copy is
# the one that belongs to this version.
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


def launcher():
    global _launcher
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
    return launcher().CONFIG_FORBIDDEN + ("--mcp", "--mcp-socket-dir", "--tmux", "--copy-auth", "--auth-file",
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
    if "vpn" in definition:
        if not isinstance(definition["vpn"], str) or not re.fullmatch(launcher().VPN_PROFILE_NAME, definition["vpn"]):
            raise ValueError(f"{where}: vpn must name a VPN profile (sandbox-ai vpn list)")
        if definition.get("network", "none") != "full":
            raise ValueError(f"{where}: a VPN needs network full (without network nothing would use it)")
    return definition


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
    if definition.get("network", "none") == "full" and \
            (policy.get("mcp_network", "full") == "none" or policy.get("allow_within") is not None):
        raise ValueError(f"MCP server {name}: network full is not allowed by the administrator's policy "
                         f"({l.POLICY_FILE})")
    required, vpn = policy.get("require_vpn", False), definition.get("vpn")
    if definition.get("network", "none") == "full" and required and \
            (not vpn or (isinstance(required, list) and vpn not in required)):
        raise ValueError(f"MCP server {name}: the administrator's policy requires a VPN for network full"
                         + (f" ({', '.join(required)})" if isinstance(required, list) else "")
                         + f" ({l.POLICY_FILE}); add one with: sandbox-ai mcp add ... --vpn PROFILE")
    if vpn and any(entry.split("=", 1)[0] == "--vpn" for entry in policy.get("forbid", [])):
        raise ValueError(f"MCP server {name}: --vpn is not allowed by the administrator's policy ({l.POLICY_FILE})")


def check_image_mounts(name, definition):
    """The host folders of an image server pass the launcher's host-path policy, read-only or not, as a --mount
    does (agent_sandbox.check_given_path): no symlink a sandbox may have planted on the way, no folder of the user's
    sockets or tree with a live socket, no file system of the kernel's own, none of sandbox-ai's own folders, a
    private location with a warning, and not inside a tree an agent can write (it could swap the folder for a link
    before the next connection mounts it). Their rw folders join the registry."""
    l = launcher()
    before = l.exposed_before()
    writable = []
    vpn = l.load_vpn_module()
    for mount in definition.get("mounts", []):
        host = os.path.expanduser(mount["host"])
        option = f"MCP server {name}: mount {mount['target']}"
        vpn.check_paths([host], option)
        l.check_planted(host, option, before)
        resolved = Path(host).resolve()
        for tree in before:
            if tree in resolved.parents:
                raise ValueError(f"{option}: {resolved} lies inside {tree}, which a sandbox can write; it could "
                                 "replace the folder with a link before a connection mounts it. Use a folder of its own.")
        # As a --mount gets, whatever its mode (the container runs as root, you): no folder of sockets, no file
        # system of the kernel's own, no tree with a live socket, never sandbox-ai's own folders; a private
        # location with a warning.
        l.check_given_path(resolved, option, mount.get("mode", "ro") == "rw")
        if mount.get("mode", "ro") == "rw":
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


TOR_USERS = {"950", "debian-tor", "tor", "_tor", "toranon"}


def check_tor_image_user(name, image, inspect=None):
    """An image server behind a tor profile must not run as tor's own user: the kill switch lets that user's
    packets out past Tor (it shares the VPN container's network namespace). The container cannot become it
    (--cap-drop=ALL, no-new-privileges), so only the image's configured user counts: refused when it is tor's
    uid or one of the usual names for it. A user name the image maps to that uid by its own /etc/passwd is not
    seen here (README: the limits of a tor profile)."""
    inspect = inspect or (lambda: subprocess.run(["podman", "image", "inspect", "--format", "{{.Config.User}}", image],
                                                 capture_output=True, text=True, timeout=30))
    try:
        result = inspect()
    except (OSError, subprocess.SubprocessError):
        return
    user = (result.stdout or "").strip().split(":", 1)[0] if result.returncode == 0 else ""
    if user in TOR_USERS:
        raise ValueError(f"MCP server {name}: its image runs as {user!r}, the user tor's kill switch lets out past "
                         "Tor; it cannot go behind a tor profile (use an image with another user)")


def image_command(definition, engine="podman", vpn_container=None):
    """The podman command that runs one instance of an image server (one per connection)."""
    command = [engine, "run", *RUN_FLAGS, f"--pids-limit={definition.get('pids', 128)}",
               f"--memory={definition.get('memory', '512m')}"]
    # full: pasta without the host's loopback or forwarded ports, as the agents' own sandboxes; with a VPN the
    # namespace of its container, where the kill switch lets nothing out but through the tunnel.
    command.append("--network=none" if definition.get("network", "none") == "none" else
                   f"--network=container:{vpn_container}" if vpn_container else f"--network={launcher().POD_NETWORK}")
    for name in definition.get("env", []):
        command.append(f"--env={name}")  # the value comes from the environment, never the command line
    if definition.get("mounts"):
        command.append("--user=0:0")  # the mounted files belong to you, who are root in rootless podman
        for mount in definition["mounts"]:
            host = Path(os.path.expanduser(mount["host"])).resolve()
            command.append(f"--volume={host}:{mount['target']}:{mount.get('mode', 'ro')}")
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
    except ValueError:
        raise ValueError(f"{source}: not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != "axross-mcp-catalog" or \
            document.get("version") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError(f"{source}: not an MCP catalog (schema axross-mcp-catalog, version 1)")
    entries = [entry for entry in document["entries"] if isinstance(entry, dict)]
    return (entries, catalog_signature(source, data, fetch)) if signature else entries


def read_rootfs(source, signature, fetch=None):
    """({name: rootfs entry}, problem or None) of rootfs.json beside catalog SOURCE: the Bubblewrap form of the
    servers (archive URL, SHA-256, entrypoint, user, workdir, env). A file of its own, since axross reads the catalog
    and takes no keys it does not know; signed like it (rootfs.json.sig). Used only when its signature is as good as
    the catalog's: a signed catalog takes only a signed rootfs.json."""
    if not source.endswith("catalog.json"):
        return {}, None
    path = source[:-len("catalog.json")] + "rootfs.json"
    try:
        data = read_source(path, fetch)
    except Exception:  # noqa: BLE001 - no rootfs.json: the catalog's servers are for Podman only
        return {}, None
    own = catalog_signature(path, data, fetch)
    if own["state"] == "bad" or signature["state"] == "good" and own["state"] != "good":
        return {}, f"{path}: not used ({own.get('reason', 'not signed like its catalog')})"
    try:
        document = json.loads(data)
    except ValueError:
        return {}, f"{path}: not JSON"
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("rootfs"), dict):
        return {}, f"{path}: expected {{\"version\": 1, \"rootfs\": {{NAME: ...}}}}"
    return {name: entry for name, entry in document["rootfs"].items() if isinstance(entry, dict)}, None


def default_catalogs(root=None):
    """CATALOGS, each replaced by the copy in sandbox-ai's own tree when there is one."""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    chosen = []
    for prefix, url in CATALOGS:
        local = root / "projects" / LOCAL_CATALOGS.get(prefix, "") / "catalog.json"
        chosen.append((prefix, str(local) if prefix in LOCAL_CATALOGS and local.is_file() else url))
    return chosen


def catalog_entries(sources=None, fetch=None):
    """[(prefix, entry)] of all catalogs; unreachable ones are reported, not fatal."""
    found, problems = [], []
    for prefix, source in sources or default_catalogs():
        try:
            entries, signature = read_catalog(source, fetch, signature=True)
            rootfs, problem = read_rootfs(source, signature, fetch)
            if problem:
                problems.append(problem)
            found += [(prefix, {**entry, "_signature": signature,
                                **({"rootfs": rootfs[entry.get("name")]} if entry.get("name") in rootfs else {})})
                      for entry in entries]
        except OSError as exc:
            if getattr(exc, "code", None) == 404:  # urllib's HTTPError
                problems.append(f"{source}: not published there (yet); pass --catalog FILE_OR_URL")
            else:
                problems.append(f"{source}: {exc}")
        except ValueError as exc:
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


def definition_from_entry(entry, mounts=(), env=(), network=None, memory=None, pids=None, vpn=None):
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
    if vpn:
        definition["vpn"] = vpn
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
    out(f"sandbox-mcp: {entry.get('name')}: added unsigned ({signature['reason']})")
    return {"signed_by": None}


def add(name, local=None, mounts=(), env=(), network=None, memory=None, pids=None, catalogs=None,
        pull=True, fetch=None, out=print, vpn=None, unsigned=False, backend=None):
    """sandbox-ai mcp add: write the definition of catalog entry NAME into mcp.json, once its catalog's signature
    verified (or UNSIGNED): for Podman its image pulled by digest, for Bubblewrap its rootfs fetched, checked and
    unpacked (BACKEND podman, bwrap or both; default: Podman when it is there or the entry has no rootfs)."""
    entries, problems = catalog_entries(catalogs, fetch)
    for problem in problems:
        out(f"sandbox-mcp: catalog not read: {problem}")
    entry = find_entry(entries, name)
    unsigned_note = []
    signed = check_signature(entry, unsigned, unsigned_note.append)
    backend = backend or ("podman" if shutil.which("podman") or not entry.get("rootfs") else "bwrap")
    if backend not in ("podman", "bwrap", "both"):
        raise ValueError("--backend is podman, bwrap or both")
    definition = {**definition_from_entry(entry, mounts, env, network, memory, pids, vpn), **signed}
    if backend in ("bwrap", "both"):
        definition = validated_image({**definition, "rootfs": entry_rootfs(entry, fetch)}, entry["name"])
    if backend == "bwrap":
        definition.pop("image")
        pull = False
    local = local or entry["name"]
    if not re.fullmatch(MCP_NAME_PATTERN, local) or local in BUILTIN:
        raise ValueError(f"invalid or reserved server name: {local}")
    if entry.get("network") == "allowlist":
        out(f"sandbox-mcp: {local} gets the full outside network; it needs only {', '.join(entry.get('hosts', []))}")
    elif definition.get("network") == "full":
        out(f"sandbox-mcp: {local} gets the full outside network ("
            + ("--network full" if network == "full" else "its catalog entry asks for it") + ")")
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
    for line in unsigned_note:
        out(line)
    form = " and ".join(([definition["image"]] if "image" in definition else []) +
                        ([f"rootfs {definition['rootfs']['sha256'][:12]} (Bubblewrap)"] if "rootfs" in definition else []))
    out(f"sandbox-mcp: {local} added ({form}); use it with --mcp {local}")
    return definition


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
    state = load_state(name)
    cmdline = launcher().process_cmdline(state.get("pid"), state.get("pid_start"), PROC)
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


def ensure_running(name, a, vpn=None):
    """Start the server sandbox for NAME unless it already runs; returns 'running' or 'started'. VPN (sandbox-ai
    mcp start --vpn) puts that VPN profile in front for this start, whatever the definition says."""
    with server_lock(name):
        return ensure_running_locked(name, a, vpn)


def gateway_arguments(gateway):
    """--gateway CONFIG for the relay (runtime.py --internal-mcp-serve), or nothing without rules."""
    if gateway is None:
        return []
    encoded = base64.urlsafe_b64encode(json.dumps(gateway, separators=(",", ":")).encode()).decode()
    if len(encoded) > GATEWAY_LIMIT:
        raise ValueError(f"The MCP rules for {gateway['server']} are larger than {GATEWAY_LIMIT >> 10} KiB")
    return ["--gateway", encoded]


def helper_command(name, definition, a, directory, sock, gateway, vpn=None):
    """The launcher's command for helper NAME (agent_sandbox.add_helpers): the agent in a sandbox of its own, with
    its login, its provider's hosts, kits, rules and hooks (unlike other servers), serving agent_mcp.py, which runs
    the agent for each task the lead gives it."""
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
    return [sys.executable, "-I", str(HERE / "agent_sandbox.py"), agent,
            "--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else []),
            "--no-config", "--helper-of", name, *[value for path in rules for value in ("--rules", path)],
            # What narrows the lead narrows its helper: the security mode, a read-only workspace, no login.
            "--mode", definition.get("mode") or "medium", "--telemetry", definition.get("telemetry") or "off",
            *(["--workspace-ro"] if definition.get("workspace_ro") else []),
            *(["--no-auth"] if definition.get("no_auth") else []),
            *(["--dangerously-skip-permissions"] if definition.get("skip_permissions") else []),
            "--state-dir", str(directory / "home"), "--mcp-socket-dir", str(sock),
            *(["--pod-name", pod] if a.backend == "podman" else []),
            *(["--mcp-approvals-dir", str(directory / "approvals")] if gateway and gateway.get("approvals") else []),
            *(["--vpn", vpn] if vpn else []),
            "--mount", f"{AGENT_MCP}:/opt/sandbox-ai/{AGENT_MCP.name}:ro",
            "--workspace", definition["workspace"], "--binary", "python3",
            "--", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", f"{launcher().MCP_SERVE_DIR}/server.sock",
            *gateway_arguments(gateway), "--", *serve]


def ensure_running_locked(name, a, vpn=None):
    servers = {**definitions(getattr(a, "project_document", None)), **(getattr(a, "helper_definitions", None) or {})}
    if name not in servers:
        raise ValueError(f"Unknown MCP server: {name}; defined: {', '.join(sorted(servers))}")
    definition, directory = servers[name], state_dir(name)
    # The relay is the gateway: its rules come from this run's rulesets and are part of what the server is.
    gateway = launcher().mcp_gateway(a, name, directory / "approvals" if "image" in definition or "rootfs" in definition
                                     else launcher().MCP_APPROVALS_DIR)
    digest = definition_digest({**definition, "gateway": gateway["rules"]} if gateway else definition,
                               a.backend, a.podman_url)
    if vpn is not None:
        launcher().load_vpn_module().check_name(vpn)
    if status(name) == "running":
        if load_state(name).get("definition") != digest:
            # Another project's definition of this name: its workspace must not reach this agent. Other rules
            # for it: they must not hold for this agent instead of its own.
            raise ValueError(f"MCP server {name} is running with a different definition or other rules; "
                             f"stop it first: sandbox-ai mcp stop {name}")
        if vpn is not None and load_state(name).get("vpn") != vpn:
            raise ValueError(f"MCP server {name} is running without the VPN {vpn}; stop it first: sandbox-ai mcp stop "
                             f"{name}")
        if (launcher().mcp_socket_dir(name) / "server.sock").exists():
            return "running"
        stop_locked(name)  # alive but its socket is gone: replace it rather than start a second one
    sock = launcher().mcp_socket_dir(name)
    if vpn is not None and "image" in definition:
        definition = validated_image({**definition, "vpn": vpn}, f"MCP server {name}")
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
        engine = "bwrap" if "rootfs" in definition and (a.backend == "bwrap" or "image" not in definition) else "podman"
        bridge = [sys.executable, "-I", str(HERE / "runtime.py"), "--internal-mcp-serve", str(sock / "server.sock"),
                  *gateway_arguments(gateway), "--"]
        if engine == "bwrap":
            if not Path(definition["rootfs"]["path"]).is_dir():
                raise ValueError(f"MCP server {name}: its rootfs is gone ({definition['rootfs']['path']}); add it "
                                 "again: sandbox-ai mcp add ... --backend bwrap")
            checked = directory / "rootfs-definition.json"
            write_private(checked, definition)
            cmd = [*bridge, *rootfs_module().bwrap_command(checked, definition, launcher())]
        else:
            cmd = [*bridge, *image_command(definition)]
        if definition.get("vpn") and engine == "podman":
            # The VPN's container first (checked; a failure stops the server), then the bridge, whose containers
            # join its network namespace. The wrapper is the supervisor: stopping it takes the tunnel down too.
            vpn_module = launcher().load_vpn_module()
            try:
                kind = vpn_module.load_profile(definition["vpn"]).get("type")
            except (OSError, ValueError):
                kind = None  # a missing or damaged profile: the VPN's own start says so
            if kind == "tor" and not definition.get("mounts"):
                check_tor_image_user(name, definition["image"])  # with mounts it runs as 0:0 anyway
            container = vpn_module.mcp_container(name)
            cmd = [sys.executable, "-I", str(HERE / "vpn.py"), "--internal-mcp-vpn", definition["vpn"], container,
                   str(sock.resolve()), "--", *bridge, *image_command(definition, vpn_container=container)]
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
        process = start_logged(cmd, directory, cwd=str(directory), env=environment)
        save_state(name, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                          "image": definition.get("image"), "backend": engine, "definition": digest,
                          **({"rootfs": definition["rootfs"]["sha256"]} if engine == "bwrap" else {}),
                          "vpn": definition.get("vpn"), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        wait_for_socket(sock / "server.sock", log, process)
        form = (f"Bubblewrap, rootfs {definition['rootfs']['sha256'][:12]}" if engine == "bwrap"
                else f"container {definition['image']}")
        print(f"agent-sandbox: MCP server {name} started ({form})", file=sys.stderr)
        return "started"
    pod = pod_name(name)
    if a.backend == "podman" and launcher().remove_stale_pod(a, pod, sock.resolve()):
        print(f"agent-sandbox: removed pod {pod} left behind by an earlier MCP server {name}", file=sys.stderr)
    if "helper" in definition:
        cmd = helper_command(name, definition, a, directory, sock, gateway, vpn)
        process = start_logged(cmd, directory)
        save_state(name, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                          "command": cmd[cmd.index("--", cmd.index("--internal-mcp-serve")) + 1:],
                          "helper": definition["helper"], "backend": a.backend, "podman_url": a.podman_url,
                          "definition": digest, "vpn": vpn,
                          "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        wait_for_socket(sock / "server.sock", directory / "supervisor.log", process)
        print(f"agent-sandbox: helper {definition['helper']} ({name}) started in its own sandbox", file=sys.stderr)
        return "started"
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), definition.get("agent", "codex"),
           "--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else []),
           "--no-provider-defaults", *definition.get("options", []), "--no-auth", "--no-config",
           "--state-dir", str(directory / "home"), "--mcp-socket-dir", str(sock),
           *(["--pod-name", pod] if a.backend == "podman" else []),
           *(["--mcp-approvals-dir", str(directory / "approvals")] if gateway and gateway.get("approvals") else []),
           *(["--vpn", vpn] if vpn else []),
           "--workspace", definition.get("workspace") or str(directory / "work"), "--binary", "python3",
           "--", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", f"{launcher().MCP_SERVE_DIR}/server.sock",
           *gateway_arguments(gateway), "--", *definition["command"]]
    log = directory / "supervisor.log"
    process = start_logged(cmd, directory)
    used_vpn = vpn or option_value(launcher().canonical_options(definition.get("options", [])), "--vpn")
    save_state(name, {"pid": process.pid, "pid_start": launcher().process_start(process.pid, PROC),
                      "command": definition["command"], "backend": a.backend,
                      "podman_url": a.podman_url, "definition": digest, "vpn": used_vpn,
                      "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    wait_for_socket(sock / "server.sock", log, process)
    print(f"agent-sandbox: MCP server {name} started in its own sandbox", file=sys.stderr)
    return "started"


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
    with open(os.open(directory / "supervisor.log", flags, 0o600), "ab") as log, \
            open(os.open(directory / "mcp-calls.jsonl", flags, 0o600), "ab") as calls:
        for line in iter(lambda: stream.readline(1 << 20), b""):
            kind, _, rest = line.partition(b" ")
            record = None
            if kind in (b"sandbox-ai-mcp-call", b"sandbox-ai-mcp-ask"):
                try:
                    record = json.loads(rest)
                except (ValueError, RecursionError):
                    pass
            if not isinstance(record, dict):
                log.write(line)
                log.flush()
                continue
            record.setdefault("server", directory.name)
            if kind == b"sandbox-ai-mcp-ask":
                ident = str(record.get("id", ""))
                if re.fullmatch(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}", ident) and approvals.is_dir():
                    write_private(approvals / f"{ident}.json", record)
                continue
            calls.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
            calls.flush()
            ident = str(record.get("question", ""))
            if re.fullmatch(r"[0-9]{1,12}-[0-9]{1,10}-[0-9]{1,10}", ident) and (approvals / f"{ident}.json").exists():
                write_private(approvals / f"{ident}.done", {**(record.get("approval") or {}), "id": ident})


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
        argv, _ = l.load_presets_module().apply(l.canonical_options([data["agent"], *data["options"], *extra]),
                                                canonical=l.canonical_options, with_mode=True)
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
    result = {"server": name, "start": entry["name"], "state": entry["state"], "server_started": False}
    named = name in starts.mcp_names(data["options"])
    # A run of a version that did not record its servers (no "mcp_running"): its options are taken for it.
    if named and (not running or name in entry.get("mcp_running", [name])):
        result.update(attached="already", message=f"{shown} uses the MCP server {name} already")
        out(result["message"])
        return result
    if start_server:
        result["server_started"] = ensure_running(name, a, vpn) == "started"
    if running and not restart and ask is not None:
        restart = ask(f"{shown} runs; {LIVE}. Restart it now with {name}? The agent continues its conversation.")
    if running and restart:
        out(f"{shown}: stopping it to start it again with the MCP server {name}")
        sessions.stop(key)
        with sessions.session_lock(key):
            sessions.resume_locked(key, mcp=[name])
        result.update(attached="restarted", state="running",
                      message=f"{shown} runs again with the MCP server {name}; the agent continues its conversation")
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


def record_server(record, name, add=True):
    """NAME in (or out of) run.json's mcp_servers: sandbox-ai list, mcp list and the front ends show whom it serves."""
    path = Path(record["directory"]) / "run.json"
    current = starts_module().read_json(path) or {}
    servers = [n for n in current.get("mcp_servers") or [] if isinstance(n, str) and n != name]
    starts_module().rewrite_json(path, {"mcp_servers": servers + ([name] if add else [])})


def live_state(record):
    try:
        data = json.loads((Path(record["directory"]) / LIVE_STATE).read_text())
    except (OSError, ValueError):
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
    with contextlib.suppress(OSError, ValueError):
        with claude_config_lock(Path(home), wait=2.0):
            l.update_json_defaults(Path(home), ".claude.json", apply, l.CLAUDE_STATE_LIMIT, keep_mode=True)
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
    agent = record.get("agent") if record.get("agent") in LIVE_AGENTS else "codex"
    result = {"server": name, "start": entry["name"], "run": record.get("run"), "state": "running",
              "server_started": False, "agent": agent, "agent_config": None,
              "agent_restart": LIVE_AGENTS[agent]["restart"], "relay": f"{launcher().MCP_AGENT_DIR}/{name}/server.sock"}
    result["server_started"] = ensure_running(name, a) == "started"  # its own checks, as at a start
    with run_lock(record):
        # Read under the run's lock: another attach or detach may have changed it since the look above.
        listed = net.ask(record["directory"], {"op": "mcp-list"})
        if name in listed.get("servers", []):
            result["attached"] = "already"
        else:
            directory = launcher().mcp_socket_dir(name)
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
        record_server(record, name)
        try:
            # Relayed already: a server of the start is on the agent's command line, one attached live was written
            # then; never twice.
            if result["attached"] == "live":
                result["agent_config"] = agent_config(record, name)
        except (OSError, ValueError) as exc:
            out(f"  the agent's configuration was not changed: {printable(str(exc))}")
    try:
        keep_server(entry["id"], name, helper)  # False: named there already
        result["kept"] = True
    except (OSError, ValueError) as exc:
        result["kept"] = False
        out(f"  not kept for the next start: {printable(str(exc))}")
    how = LIVE_AGENTS[agent]["attach"].format(name=name, start=shown)
    result["message"] = (f"{shown} reaches the MCP server {name} now"
                         f"{' (it was attached already)' if result['attached'] == 'already' else ''}"
                         f"{', kept for its later starts' if result['kept'] else ''}. {how}")
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
              "agent_restart": LIVE_AGENTS.get(agent, LIVE_AGENTS["codex"])["restart"] if live else None}
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
        how = LIVE_AGENTS.get(agent, LIVE_AGENTS["codex"])["detach"].format(name=name, start=shown)
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
    out(result["message"])
    return result


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-mcp", description="Manage local MCP servers that run in their "
                                     "own sandboxes. Agents use them with --mcp NAME.")
    parser.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Defined servers, their status and the starts they serve")
    listing.add_argument("-j", "--json", action="store_true")
    start = commands.add_parser("start", help="Start a server sandbox now")
    start.add_argument("name")
    start.add_argument("-b", "--backend", choices=["podman", "bwrap"], default="podman")
    start.add_argument("-v", "--vpn", metavar="PROFILE", help="Put this VPN profile in front (sandbox-ai vpn list)")
    start.add_argument("-f", "--for", dest="for_start", metavar="START",
                       help="for this agent start (sandbox-ai list): started as it starts it (its backend and rules) "
                            "and attached to it (as mcp attach)")
    start.add_argument("-r", "--restart", action="store_true",
                       help="with --for: a running start is restarted with it now (the agent continues its "
                            "conversation)")
    start.add_argument("-j", "--json", action="store_true", help="with --for: the result as JSON")
    attaching = commands.add_parser("attach", help="Connect a server to an agent start (kept for its later starts)",
                                    usage="sandbox-mcp attach NAME START [--live | --restart] [--json]\n"
                                          "       sandbox-mcp attach --helper AGENT START --live [--json]")
    attaching.add_argument("name", metavar="NAME", help="the server (sandbox-ai mcp list); with --helper: START")
    attaching.add_argument("start", metavar="START", nargs="?", help="the agent start (sandbox-ai list)")
    attaching.add_argument("-H", "--helper", metavar="AGENT", choices=["claude", "codex", "opencode"],
                           help="the agent AGENT as a helper (an MCP server of its own sandbox), as --helper at a start")
    attaching.add_argument("-r", "--restart", action="store_true",
                           help="a running start is restarted with it now (the agent continues its conversation); "
                                "without it (and without --live) a running start reaches the server from its next "
                                "start")
    attaching.add_argument("-l", "--live", action="store_true",
                           help="into the running sandbox now, without a restart: its relay is opened and the agent's "
                                "MCP configuration updated (Claude Code: /mcp; Codex and OpenCode: after a restart "
                                "of the agent)")
    attaching.add_argument("-j", "--json", action="store_true")
    detaching = commands.add_parser("detach", help="Take a server from an agent start (and with --live from its "
                                                   "running sandbox now)",
                                    usage="sandbox-mcp detach NAME START [--live] [--json]\n"
                                          "       sandbox-mcp detach --helper AGENT START [--live] [--json]")
    detaching.add_argument("name", metavar="NAME", help="the server; with --helper: START")
    detaching.add_argument("start", metavar="START", nargs="?", help="the agent start (sandbox-ai list)")
    detaching.add_argument("-H", "--helper", metavar="AGENT", choices=["claude", "codex", "opencode"])
    detaching.add_argument("-l", "--live", action="store_true",
                           help="close its relay in the running sandbox now (the agent's connections to it end)")
    detaching.add_argument("-j", "--json", action="store_true")
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
    added.add_argument("-b", "--backend", choices=["podman", "bwrap", "both"],
                       help="Podman (the image), Bubblewrap (the image's rootfs, checked and unpacked) or both; "
                            "default: Podman when it is there")
    added.add_argument("-U", "--unsigned", action="store_true",
                       help="Take an entry whose catalog is not signed by a trusted key (sandbox/mcp-signers); a "
                            "signature that does not verify is refused anyway")
    added.add_argument("-v", "--vpn", metavar="PROFILE",
                       help="Its network only through this VPN profile, with a kill switch (needs --network full)")
    a = parser.parse_args(argv)
    if a.command in ("catalog", "add"):
        sources = [(f"c{index}", value) for index, value in enumerate(a.catalog)] or None
    if a.command == "catalog":
        entries, problems = catalog_entries(sources)
        for problem in problems:
            print(f"sandbox-mcp: catalog not read: {problem}", file=sys.stderr)
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
    if a.command == "add":
        add(a.name, a.local, a.mount, a.env, a.network, a.memory, a.pids, sources, pull=not a.no_pull, vpn=a.vpn,
            unsigned=a.unsigned, backend=a.backend)
        return 0
    if a.command == "list":
        try:
            served = serving()
        except (OSError, ValueError) as exc:  # the starts could not be read: the servers are listed anyway
            print(f"sandbox-mcp: the starts they serve are unknown: {exc}", file=sys.stderr)
            served = {}
        found = [{"name": name, "status": status(name),
                  "command": (d["image"] if "image" in d else f"rootfs {d['rootfs']['sha256'][:12]}" if "rootfs" in d
                              else " ".join(d["command"])) + (f"  (vpn {d['vpn']})" if d.get("vpn") else ""),
                  "serves": served.get(name, [])} for name, d in sorted(definitions().items())]
        if a.json:
            print(json.dumps(found, indent=2))
            return 0
        printable = launcher().printable
        rows = [["NAME", "STATUS", "SERVES", "COMMAND"]] + [
            [e["name"], e["status"], printable(", ".join(e["serves"]) or "-"), printable(e["command"])] for e in found]
        widths = [max(len(row[i]) for row in rows) for i in range(3)]
        for row in rows:
            print(f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  {row[2].ljust(widths[2])}  {row[3]}")
        return 0
    if a.command == "start" and a.for_start is None:
        if a.restart or a.json:
            raise ValueError("--restart and --json go with --for START")
        ensure_running(a.name, launcher().parse_args(["codex", "--backend", a.backend]), a.vpn)
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
            raise ValueError("--helper AGENT goes with --live here (a running start: sandbox-ai mcp attach --helper "
                             f"{a.helper} {target} --live), or give it at a start (--helper {a.helper})")
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


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-log":
        sys.exit(log_main(sys.argv[2]))
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"sandbox-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
