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
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

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
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
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
        _rootfs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_rootfs)
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
    """The host folders of an image server pass the launcher's host-path policy: no symlink a sandbox may have
    planted on the way, no private location for rw, and not inside a tree an agent can write (it could swap
    the folder for a link before the next connection mounts it). Their rw folders join the registry."""
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
        _keys = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_keys)
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
    unsigned one anyway (a catalog of your own, say), never a signature that does not verify."""
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
    signed = check_signature(entry, unsigned, out)
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
            container = launcher().load_vpn_module().mcp_container(name)
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


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-mcp", description="Manage local MCP servers that run in their "
                                     "own sandboxes. Agents use them with --mcp NAME.")
    parser.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Defined servers and their status")
    start = commands.add_parser("start", help="Start a server sandbox now")
    start.add_argument("name")
    start.add_argument("-b", "--backend", choices=["podman", "bwrap"], default="podman")
    start.add_argument("-v", "--vpn", metavar="PROFILE", help="Put this VPN profile in front (sandbox-ai vpn list)")
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
            print(f"{prefix}:{entry.get('name')}  [{network}]  {entry.get('title', '')}{state}")
        return 0
    if a.command == "add":
        add(a.name, a.local, a.mount, a.env, a.network, a.memory, a.pids, sources, pull=not a.no_pull, vpn=a.vpn,
            unsigned=a.unsigned, backend=a.backend)
        return 0
    if a.command == "list":
        rows = [["NAME", "STATUS", "COMMAND"]] + [[name, status(name), (d["image"] if "image" in d else
                                                                        f"rootfs {d['rootfs']['sha256'][:12]}" if "rootfs" in d else
                                                                        " ".join(d["command"]))
                                                  + (f"  (vpn {d['vpn']})" if d.get("vpn") else "")]
                                                  for name, d in sorted(definitions().items())]
        widths = [max(len(row[i]) for row in rows) for i in range(2)]
        for row in rows:
            print(f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  {row[2]}")
        return 0
    if a.command == "start":
        ensure_running(a.name, launcher().parse_args(["codex", "--backend", a.backend]), a.vpn)
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
