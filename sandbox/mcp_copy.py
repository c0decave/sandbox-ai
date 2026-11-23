"""sandbox-ai mcp export / import / copy: an MCP server of one host taken over by another (sandbox-ai hosts).

    sandbox-ai mcp export NAME --json                 what NAME is, as another host needs to know it (no secret value)
    sandbox-ai mcp import FILE|- [options] [--dry-run] [--json]    such a document taken in here, checked as add takes it
    sandbox-ai mcp copy NAME... --from HOST|local --to HOST|local [options] [--attach RUN] [--dry-run] [--json]

How a server travels depends on what it is:

  catalog   a server added from a (signed) catalog is never copied raw: the target adds it from its own read of the
            catalog (`mcp add` there), the same entry pinned to the same digest, so the signature check holds there.
            The source's choices (folders, variables, network, VPN, limits) are given to that add as options. A
            catalog there that pins another digest is refused unless --current-pin takes the catalog's.
  image     a definition of your own with an image pinned by digest: imported through mcp.json's validation
  command   (validated / validated_image), never written as it came.

What the target cannot take as it is, is shown and must be resolved - never carried silently:

  secrets   only their names travel (never a value); the ones the target has no secret of are named, with
            `secrets set` there to keep one (--secrets-later: copied anyway, the server needs them before it starts).
  paths     a host folder of the source is never mounted on the target unasked: each one is given a folder there
            (--path OLD=NEW; NEW=OLD keeps it) or dropped (--drop-path OLD: OLD is a path of the source that
            means nothing there, so the server gets its default folder there, as `mcp add` without --mount gives
            it - /work the start's workspace, any other a folder of its own made there, new and empty; a command
            server's option naming it is left out); the folders you give are checked and warned about as `mcp add
            --mount` does.
  images    a catalog's is pulled there by digest; one of your own that is not there is pulled by its digest
            (--pull-image) or sent from the source (--copy-image: podman save | podman load, its size said) and used
            only if it still answers to its pinned digest there.
  network   network, VPN and a command server's egress options are copied as defined and shown; a VPN profile the
            target lacks is refused unless --no-vpn.
  names     a server of that name on the target is refused unless --as NEWNAME, or --replace (asked first).
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
SCHEMA = "sandbox-ai-mcp-export"
VERSION = 1
COPY_SINCE = (0, 1, 98)  # the first sandbox-ai with mcp export, import and copy
DOCUMENT_LIMIT = 1 << 20
EXPORT_TIMEOUT = 120
IMPORT_TIMEOUT = 900  # a catalog server's image is pulled there
IMAGE_TIMEOUT = 3600
# A command server's launcher options that say where it may connect: copied as defined, and shown.
NETWORK_OPTIONS = ("--offline", "--vpn", "--nameserver", "--no-inspect", "--vpn-bypass-tcp", "--network")
NETWORK_PREFIXES = ("--allow", "--egress")
SECRET_OPTIONS = ("--secret-env", "--api-key")  # NAME[=SECRET], HOST=NAME[=SECRET]: a secret of sandbox-ai secrets
LOADING = threading.RLock()


class Names:
    """mcp.py's names, read as they are now (a test's monkeypatch of the module is seen here too)."""

    def __init__(self, names):
        self.__dict__["_names"] = names

    def __getattr__(self, name):
        try:
            return self._names[name]
        except KeyError:
            raise AttributeError(name) from None


m = None  # mcp.py (bind)
_hosts = None


def bind(names):
    """Use mcp.py's NAMES (its globals())."""
    global m
    m = Names(names)


def hosts_module():
    """hosts.py: how a command reaches another host (its SSH settings, sandbox-ai there)."""
    global _hosts
    if _hosts is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_hosts_for_mcp_copy", HERE / "hosts.py")
        with LOADING:
            if _hosts is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _hosts = loaded
    return _hosts


def shown(text, limit=300):
    return m.launcher().printable(" ".join(str(text).split()))[:limit]


# --- export: what a server is, as another host needs to know it ---------------------------------------------------

def credential_in(texts):
    """Why one of TEXTS looks like a credential (the privacy check's patterns), else None."""
    for text in texts:
        for _key, regex, _severity, reason in m.privacy_module().BUILTIN:
            if re.search(regex, str(text)):
                return reason
    return None


def option_values(options):
    """[(index of the option, name, value, tokens it takes)] of OPTIONS (canonical): --name=value and --name value
    for the options that take one (the launcher's)."""
    actions = m.launcher().option_actions()
    found, index = [], 0
    while index < len(options):
        token = options[index]
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            found.append((index, name, value, 1))
            index += 1
            continue
        action = actions.get(token)
        takes = action is not None and action.nargs != 0 or token in m.launcher().PATH_OPTIONS
        if token.startswith("--") and takes and index + 1 < len(options):
            found.append((index, token, options[index + 1], 2))
            index += 2
            continue
        found.append((index, token, None, 1))
        index += 1
    return found


def command_paths(definition):
    """[{"kind": "option"|"workspace", "option", "value", "path"}] of a command server's host paths."""
    found = []
    path_options = m.launcher().PATH_OPTIONS
    for _index, name, value, _count in option_values(definition.get("options", [])):
        if value is not None and name in path_options:
            path = path_options[name](value)
            if path:
                found.append({"kind": "option", "option": name, "value": value, "path": path})
    if definition.get("workspace"):
        found.append({"kind": "workspace", "path": definition["workspace"]})
    return found


def command_secrets(definition):
    """[{"variable", "secret", "required", "from"}] a command server's options name (--secret-env, --api-key, --env)."""
    found = []
    for _index, name, value, _count in option_values(definition.get("options", [])):
        if value is None:
            continue
        if name == "--secret-env":
            variable, _, secret = value.partition("=")
            found.append({"variable": variable, "secret": secret or variable, "required": True, "from": "secret"})
        elif name == "--api-key":
            parts = value.split("=")
            if len(parts) >= 2:
                found.append({"variable": parts[1], "secret": parts[2] if len(parts) > 2 else parts[1],
                              "required": True, "from": "secret"})
        elif name == "--env":
            found.append({"variable": value, "secret": value, "required": True, "from": "env"})
    return found


def image_secrets(definition):
    """[{"variable", "secret", "required", "from"}] an image server's variables name: env (your environment there,
    else the secret of its name), env_optional (the same, where there is one), env_secrets (that secret)."""
    found = [{"variable": name, "secret": name, "required": True, "from": "env"} for name in definition.get("env", [])]
    found += [{"variable": name, "secret": name, "required": False, "from": "env"}
              for name in definition.get("env_optional", [])]
    found += [{"variable": name, "secret": secret, "required": True, "from": "secret"}
              for name, secret in (definition.get("env_secrets") or {}).items()]
    return found


def image_facts(ref, run=subprocess.run):
    """{"ref", "present", "size"} of the image REF in this host's Podman (size in bytes; None: not known)."""
    present = m.image_present(ref)
    size = None
    if present and shutil.which("podman"):
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
            done = run(["podman", "image", "inspect", "--format", "{{.Size}}", ref], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=30)
            size = int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None
    return {"ref": ref, "present": present, "size": size}


def export(name, run=subprocess.run):
    """`mcp export NAME --json`: the document another host imports server NAME from (schema SCHEMA). Only a server of
    your mcp.json (a built-in one is on every host; a team's is its board's). The values of secrets never: their
    names. ValueError when NAME cannot be exported."""
    _path, document = m.read_config()
    if name not in document:
        if name in m.BUILTIN:
            raise ValueError(f"MCP server {name} is built in: every sandbox-ai has it, nothing to copy")
        if name.startswith("team-"):
            raise ValueError(f"MCP server {name} is a team's board (sandbox-ai team): copy the team, not its server")
        raise ValueError(f"MCP server {name} is not one of yours (not in {m.config_file()}): sandbox-ai mcp list")
    definition = m.validated({name: document[name]}, m.config_file())[name]
    if "image" in definition or "rootfs" in definition:
        # A catalog server: the entry it was added from ("source"; its catalog since 0.1.78) - re-added from its
        # catalog there, never taken as it is.
        kind = "catalog" if isinstance(definition.get("source"), str) and "built" not in definition else "image"
        # (one built here, mcp add --build: its catalog has no image of it to add there - its own image, copied)
        if kind == "image" and "rootfs" in definition:
            raise ValueError(f"MCP server {name}: a Bubblewrap rootfs of your own is this host's unpacked folder; add "
                             "it from its catalog on the other host instead")
        secrets = image_secrets(definition)
        paths = [{"kind": "mount", "target": mount["target"], "path": mount["host"], "mode": mount.get("mode", "ro")}
                 for mount in definition.get("mounts", [])]
    else:
        kind = "command"
        reason = credential_in([*definition["command"], *definition.get("options", []),
                                definition.get("workspace", "")])
        if reason:
            raise ValueError(f"MCP server {name}: its definition holds what looks like a credential ({reason}); a "
                             "secret's value is never copied: keep it with sandbox-ai secrets set NAME, give it with "
                             "--secret-env NAME in its options, then copy it")
        secrets = command_secrets(definition)
        paths = command_paths(definition)
    result = {"schema": SCHEMA, "version": VERSION, "name": name, "kind": kind,
              "sandbox_ai": getattr(m.launcher(), "__version__", None), "definition": definition,
              "secrets": secrets, "paths": paths, "network": network_of(definition)}
    if kind == "catalog":
        result["catalog"] = {"source": definition["source"], "catalog": definition.get("catalog"),
                             "signed": None if "signed_by" not in definition else bool(definition["signed_by"]),
                             "image": definition.get("image"),
                             "rootfs": (definition.get("rootfs") or {}).get("sha256")}
    if "image" in definition:
        result["image"] = image_facts(definition["image"], run)
    return result


def network_of(definition):
    """{"network", "vpn", "options"}: where the server may connect, as its definition says (a command server: its
    egress options)."""
    if "image" in definition or "rootfs" in definition:
        # (the parts of your network it may reach too: as mcp add takes them)
        return {"network": definition.get("network", "none"), "vpn": definition.get("vpn"),
                "options": [*(f"--allow-private {value}" for value in definition.get("allow_private", [])),
                            *(f"--allow-tcp {value}" for value in definition.get("allow_tcp", []))]}
    options = [f"{name}={value}" if value is not None else name
               for _i, name, value, _c in option_values(definition.get("options", []))
               if name.startswith(NETWORK_PREFIXES) or name in NETWORK_OPTIONS]
    return {"network": "the start's rules", "vpn": None, "options": options}


# A network as people read it (the definition's word stays in the documents: export --json, the plan's "network").
NETWORK_WORDS = {"full": "internet (no LAN)", "allowlist": "only its catalog entry's hosts", "lan": "internet + LAN",
                 "none": "none", "the start's rules": "its command's own options (the sandbox's rules)"}


def network_text(network):
    """NETWORK (network_of's) in words: "network: internet (no LAN), VPN x, --allow-tcp ..."."""
    word = network.get("network")
    return (f"network: {NETWORK_WORDS.get(word, word)}" + (f", VPN {network['vpn']}" if network.get("vpn") else "")
            + (f", {' '.join(network['options'])}" if network.get("options") else ""))


def export_text(document):
    """`mcp export NAME` without --json: what it is, for people."""
    lines = [f"MCP server {document['name']} ({document['kind']})"]
    if document.get("catalog"):
        lines.append(f"  from the catalog: {document['catalog']['source']} ({document['catalog'].get('catalog')})")
    if document.get("image"):
        image = document["image"]
        lines.append(f"  image: {image['ref']}" + (f" ({size_text(image['size'])})" if image.get("size") else ""))
    for secret in document["secrets"]:
        lines.append(f"  variable {secret['variable']}: " + (f"the secret {secret['secret']}" if secret["from"] ==
                                                            "secret" else "the environment, else the secret of its name"))
    for path in document["paths"]:
        lines.append(f"  folder {path_label(path)}")
    lines.append("  " + network_text(document["network"]))
    lines.append("  (sandbox-ai mcp export --json: the document; sandbox-ai mcp copy: to another host)")
    return "\n".join(lines)


def size_text(size):
    if not isinstance(size, int) or size < 0:
        return "size not known"
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            return f"{size / (1 << shift):.1f} {unit}"
    return f"{size} bytes"


def path_label(path):
    if path.get("kind") == "mount":
        source = path["path"]
        source = "the start's workspace" if source == m.WORKSPACE_HOST else \
            f"its own folder {source}" if default_folder(source) == m.DEFAULT_HOST else source
        return f"{path.get('target')} in the container <- {source} ({path.get('mode') or 'ro'})"
    if path.get("kind") == "workspace":
        return f"workspace {path['path']}"
    return f"{path.get('option')} {path.get('value')}"


# --- import: such a document taken in here, checked as add and mcp.json take a server ---------------------------

def read_document(text):
    """The export document in TEXT, its shape checked (it comes from another host: data, never trusted)."""
    if len(text) > DOCUMENT_LIMIT:
        raise ValueError(f"the export is larger than {DOCUMENT_LIMIT >> 20} MiB")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("not an MCP server's export (sandbox-ai mcp export NAME --json): not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("version") != VERSION:
        raise ValueError(f"not an MCP server's export (schema {SCHEMA}, version {VERSION})")
    if not isinstance(document.get("name"), str) or not re.fullmatch(m.launcher().MCP_NAME, document["name"]):
        raise ValueError("the export names no valid server")
    if document.get("kind") not in ("catalog", "image", "command") or not isinstance(document.get("definition"), dict):
        raise ValueError("the export says no kind of server it knows (catalog, image, command)")
    for key in ("secrets", "paths"):
        if not isinstance(document.get(key, []), list) or not all(isinstance(i, dict) for i in document.get(key, [])):
            raise ValueError(f"the export's {key} are not a list")
    if document["kind"] == "catalog" and not isinstance(document.get("catalog"), dict):
        raise ValueError("the export of a catalog server names no catalog entry")
    return document


def described(kind, definition):
    """(secrets, paths) of an export's DEFINITION of KIND, read from the definition itself as export() reads them.
    The document's own "secrets" and "paths" lists are the other host's word: one that left a folder or a secret out
    of them would have it taken here unasked and unseen. ValueError when the definition's parts are not readable."""
    def texts(value, what):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"the export's definition: {what} must be a list of texts")
        return value

    if kind == "command":
        checked = {**definition, "options": texts(definition.get("options", []), "options")}
        if not isinstance(checked.get("workspace", ""), str):
            raise ValueError("the export's definition: workspace must be a folder's path")
        return command_secrets(checked), command_paths(checked)
    mounts = definition.get("mounts", [])
    if not isinstance(mounts, list) or not all(
            isinstance(mount, dict) and isinstance(mount.get("target"), str) and isinstance(mount.get("host"), str)
            and isinstance(mount.get("mode", "ro"), str) for mount in mounts):
        raise ValueError("the export's definition: mounts must be a list of {target, host, mode}")
    env_secrets = definition.get("env_secrets") or {}
    if not isinstance(env_secrets, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                    for key, value in env_secrets.items()):
        raise ValueError("the export's definition: env_secrets must name a secret for each variable")
    checked = {"env": texts(definition.get("env", []), "env"),
               "env_optional": texts(definition.get("env_optional", []), "env_optional"), "env_secrets": env_secrets}
    return image_secrets(checked), [{"kind": "mount", "target": mount["target"], "path": mount["host"],
                                     "mode": mount.get("mode", "ro")} for mount in mounts]


def parse_paths(values):
    """{OLD: NEW} of --path OLD=NEW values (absolute paths, or ~/...; @workspace: the workspace of the start that
    uses the server - as the source names it, or as the folder it becomes here)."""
    found = {}
    for value in values or ():
        old, sep, new = str(value).partition("=")
        if not sep or not old or not new:
            raise ValueError(f"--path OLD=NEW: OLD the folder the source names, NEW one of the target (NEW=OLD "
                             f"keeps it); not {shown(value, 200)}")
        for path in (old, new):
            if path == m.WORKSPACE_HOST:
                continue
            if not os.path.isabs(os.path.expanduser(path)) or any(c in path for c in ("\n", "\r", "\0")):
                raise ValueError(f"--path {shown(value, 200)}: give absolute paths (or ~/...)")
        if old in found:
            raise ValueError(f"--path {shown(old, 200)}: given twice")
        found[old] = new
    return found


def secret_names():
    """The names of this host's secrets (sandbox-ai secrets list), or None when they cannot be read."""
    try:
        store = m.secret_store_module()
        backend = store.store()
        names = set(backend.names())
        if backend.name != "file":
            names |= set(store.FileStore().names())
        return names
    except Exception:  # noqa: BLE001 - no keyring, a locked one: not known (said so)
        return None


def problem(plan, kind, text, fix=""):
    plan["problems"].append({"kind": kind, "text": text, "fix": fix})


def existing_names():
    """The servers defined here by name: {name: "yours" | "built in" | "a team's"}."""
    found = {name: "built in" for name in m.BUILTIN}
    with contextlib.suppress(OSError, ValueError):
        found.update({name: "yours" for name in m.read_config()[1]})
    with contextlib.suppress(OSError, ValueError, TypeError):
        found.update({name: "a team's" for name in m.team_definitions()})
    return found


def plan_import(document, as_name=None, replace=False, paths=None, drop_paths=(), unsigned=False,
                secrets_later=False, pull_image=False, no_vpn=False, current_pin=False, catalogs=None, fetch=None,
                secrets=None, here="this host"):
    """What `mcp import` would do with the export DOCUMENT here (nothing is written, pulled or created): {"server",
    "from_name", "kind", "how", "exists", "replace", "definition", "changes", "copied", "secrets", "paths", "image",
    "network", "warnings", "notes", "problems", "ok"}. Every value of the document is checked here, as `mcp add` and
    mcp.json take a server: a catalog server from this host's own read of its catalog, never the definition that
    came. SECRETS: this host's secret names (default: read)."""
    paths = dict(paths or {})
    drop_paths = list(drop_paths or ())
    name = as_name or document["name"]
    plan = {"server": name, "from_name": document["name"], "kind": document["kind"],
            "how": "catalog" if document["kind"] == "catalog" else "definition", "exists": False, "replace": False,
            "definition": None, "changes": [], "copied": [], "secrets": {}, "paths": [], "image": None,
            "network": document.get("network") if isinstance(document.get("network"), dict) else {},
            "warnings": [], "notes": [], "problems": [], "ok": False, "here": here, "capped": []}
    if not isinstance(name, str) or not re.fullmatch(m.launcher().MCP_NAME, name) or name in m.BUILTIN:
        problem(plan, "name", f"{shown(name)} is not a name a server of yours can have (letters, digits, - and _, at "
                              "most 32; not a built-in one's)", "--as NEWNAME")
        return plan
    taken = existing_names().get(name)
    if taken:
        plan["exists"] = True
        if taken != "yours":
            problem(plan, "name", f"{here} has a server {name} already ({taken}): it cannot be replaced",
                    "--as NEWNAME")
        elif not replace:
            problem(plan, "name", f"{here} has a server {name} already", f"--as NEWNAME (another name), or --replace "
                                                                          f"(its definition replaced, asked first)")
        else:
            plan["replace"] = True
            plan["notes"].append(f"replaces the server {name} of {here}")
    source = document["definition"]
    # Its kind is its definition's (a command, or an image or rootfs): the document's "kind" is the other host's word,
    # and a "command" export with an image server's definition would have its folders looked for among a command's
    # options (none) and be written as the image server it is, mounts and variables with it.
    shapes = [shape for shape, keys in (("command", ("command",)), ("image", ("image", "rootfs")))
              if any(key in source for key in keys)]
    if shapes != ["command" if document["kind"] == "command" else "image"]:
        found = " and ".join(f"{'a' if shape == 'command' else 'an'} {shape} server's" for shape in shapes) or "neither"
        problem(plan, "definition", f"the export says its kind is {document['kind']}, but its definition is {found}: "
                                    "not an export of sandbox-ai mcp export")
        return plan
    # What it mounts and which secrets it reads: from its definition, read here - never the lists that came with it.
    # A command server's options first as mcp.json takes them (validated: every short form its long option, -m is
    # --mount), so that the folders and secrets are looked for in what will be written.
    try:
        if document["kind"] == "command":
            source = m.validated({document["name"]: source}, "the export")[document["name"]]
            document = {**document, "definition": source}
        named_secrets, named_paths = described(document["kind"], source)
    except ValueError as exc:
        problem(plan, "definition", str(exc))
        return plan
    # Folders: each one given a folder here or left out - never the source's taken unasked.
    known = named_paths
    unknown_given = [old for old in [*paths, *drop_paths] if old not in [p["path"] for p in known]]
    if unknown_given:
        problem(plan, "path", f"the source names no folder {', '.join(shown(p, 200) for p in unknown_given)}",
                "--path and --drop-path take the folders the export names")
    unresolved = []
    for path in known:
        entry = {**{k: path.get(k) for k in ("kind", "target", "option", "value", "path", "mode") if k in path},
                 "to": None, "dropped": False}
        if path["path"] in drop_paths:
            # A folder of the source is a path of that machine, which means nothing here: dropped, the server gets
            # its default folder here, as `mcp add` without --mount gives it (the start's workspace for /work, else
            # a folder of its own made here, new and empty). A command server's option has no default: left out.
            entry["dropped"] = True
            if path.get("kind") == "mount":
                entry["to"] = m.WORKSPACE_HOST if path.get("target") == m.WORKSPACE_TARGET else m.DEFAULT_HOST
        elif path["path"] in paths:
            entry["to"] = paths[path["path"]]
        elif path.get("kind") == "mount" and default_folder(path["path"]) and (
                default_folder(path["path"]) != m.WORKSPACE_HOST
                or document["kind"] == "catalog" and path.get("target") == m.WORKSPACE_TARGET):
            # A folder default of the source (the start's workspace, a folder of the server's own there): the same
            # default here - the workspace of the start that uses it here, a folder of its own made here (empty: the
            # source's files stay there). Nothing of the source's folders is mounted. The workspace only where it is
            # the default here too: a catalog server's /work (its mode as the catalog declares: plan_catalog) - at
            # another target, or for an image of your own, the source's word would bind it unasked (asked below).
            entry["to"] = default_folder(path["path"])
            plan["notes"].append(f"{path['target']}: " + (
                "the workspace of the start that uses it, as on the source" if entry["to"] == m.WORKSPACE_HOST else
                f"a folder of its own here, new and empty (the source's {shown(path['path'], 200)} stays there)"))
        else:
            unresolved.append(path)
        plan["paths"].append(entry)
    for path in unresolved:
        if path["path"] == m.WORKSPACE_HOST:
            problem(plan, "path", f"the folder {path_label(path)} is the workspace of the start, where the source puts "
                                  "it: it is not mounted so here unless you say so",
                    f"--path {m.WORKSPACE_HOST}={m.WORKSPACE_HOST} (the workspace of the start that uses it on {here}"
                    + (", as writable as its catalog entry declares" if document["kind"] == "catalog" else
                       f", {path.get('mode') or 'ro'} as the source has it")
                    + f"), --path {m.WORKSPACE_HOST}=FOLDER (a folder of {here}) or --drop-path {m.WORKSPACE_HOST} "
                      f"(the server's default folder on {here})")
            continue
        problem(plan, "path", f"the folder {path_label(path)} is the source's: it is not mounted here unless you say "
                              "which folder of this host it is", f"--path {path['path']}=FOLDER (a folder of {here}; "
                                                                 f"{path['path']}={path['path']} keeps it) or "
                                                                 f"--drop-path {path['path']}"
                                                                 + (f" (the server's default folder on {here})"
                                                                    if path.get("kind") == "mount" else
                                                                    " (left out)"))
    # Secrets: names only; those this host has no secret of are named.
    names = secret_names() if secrets is None else set(secrets)
    unnamed = sorted({shown(s["secret"], 80) for s in named_secrets if not re.fullmatch(m.ENV_NAME.pattern, s["secret"])})
    if unnamed:
        problem(plan, "definition", f"the export's definition names a secret that is no secret's name: "
                                    f"{', '.join(unnamed)}")
        return plan
    needed = named_secrets
    missing = sorted({s["secret"] for s in needed if names is not None and s["secret"] not in names
                      and (s.get("required") or s.get("from") == "secret")})
    plan["secrets"] = {"needed": needed, "missing": missing, "known": names is not None}
    if names is None and needed:
        plan["notes"].append(f"the secrets of {here} cannot be read: whether it has "
                             f"{', '.join(sorted({s['secret'] for s in needed}))} is not known")
    for secret in missing:
        variables = [s["variable"] for s in needed if s["secret"] == secret]
        environment = all(s.get("from") == "env" for s in needed if s["secret"] == secret)
        text = (f"{here} has no secret {secret} (for {', '.join(variables)}"
                + ("; the environment there is read first" if environment else "") + ")")
        if secrets_later:
            plan["notes"].append(text + ": copied anyway (--secrets-later); keep it before the server starts")
        else:
            problem(plan, "secret", text, f"sandbox-ai secrets set {secret} there (its value is typed there, never "
                                          "copied), or --secrets-later")
    optional = sorted({s["secret"] for s in needed if names is not None and s["secret"] not in names
                       and not s.get("required") and s.get("from") != "secret"})
    for secret in optional:
        plan["notes"].append(f"{here} has no secret {secret}: the server goes without it (optional)")
    try:
        if document["kind"] == "catalog":
            plan_catalog(plan, document, unsigned, no_vpn, current_pin, catalogs, fetch)
        elif document["kind"] == "image":
            plan_image(plan, document, pull_image, no_vpn)
        else:
            plan_command(plan, document)
    except ValueError as exc:
        problem(plan, "definition", str(exc))
    definition = plan["definition"]
    if definition is not None and ("image" in definition or "rootfs" in definition):
        plan_checks(plan, name, definition)
    if definition is not None:
        changed = sorted(key for key in set(source) | set(definition) if source.get(key) != definition.get(key)
                         and key not in ("signed_by",) and not (key == "mounts" and mounts_said(plan, definition)))
        plan["changes"] = [f"{key}: {shown(json.dumps(source.get(key)), 200)} -> "
                           f"{shown(json.dumps(definition.get(key)), 200)}" for key in changed]
        network = network_of(definition)
        plan["network"] = network
        plan["copied"].append(network_text(network))
    plan["ok"] = not plan["problems"]
    return plan


def mounts_said(plan, definition):
    """Whether the folder lines of PLAN say every folder of DEFINITION already (each target with its mode, a default
    as a default): then its "changed: mounts" line would only repeat them as JSON (a server's own folder is another
    path on every host)."""
    paths = {p.get("target"): p for p in plan["paths"] if p.get("kind") == "mount"}
    mounts = definition.get("mounts")
    if not isinstance(mounts, list) or len(mounts) != len(paths):
        return False
    for mount in mounts:
        path = paths.get(mount.get("target")) if isinstance(mount, dict) else None
        if path is None or path.get("to") not in (m.WORKSPACE_HOST, m.DEFAULT_HOST) or \
                (path.get("mode") or "ro") != (mount.get("mode") or "ro"):
            return False
        if path["to"] == m.WORKSPACE_HOST and mount.get("host") != m.WORKSPACE_HOST:
            return False
    return True


def default_folder(path):
    """m.WORKSPACE_HOST or m.DEFAULT_HOST for a source's folder PATH that is a folder default there (the start's
    workspace, a folder of a server's own: .../sandbox-ai/mcp-folders/NAME/TARGET); None for any other folder."""
    if path == m.WORKSPACE_HOST:
        return m.WORKSPACE_HOST
    return m.DEFAULT_HOST if re.search(r"/sandbox-ai/mcp-folders/[A-Za-z0-9_-]{1,32}/[A-Za-z0-9._-]+$", str(path)) \
        else None


def mount_values(plan):
    """The --mount TARGET=HOST[:mode] values of a catalog server's folders as resolved (the dropped ones not: the
    entry's defaults give them their default folder, its mode the catalog's), each no more writable than the entry
    declares (cap_modes)."""
    return [f"{p['target']}={p['to']}:{p.get('mode_here') or p.get('mode') or 'ro'}" for p in plan["paths"]
            if p.get("kind") == "mount" and p["to"] and not p["dropped"]]


def env_specs(definition):
    """The --env values that give a catalog entry the variables DEFINITION has (names; values that are no secret)."""
    return [*definition.get("env", []), *definition.get("env_optional", []),
            *[f"{k}={v}" for k, v in (definition.get("env_values") or {}).items()],
            *[f"{k}@{v}" for k, v in (definition.get("env_secrets") or {}).items()]]


def catalog_sources(location, catalogs=None):
    """The catalogs to read for an entry added from LOCATION (a URL or a path of the source host): CATALOGS when
    given; the published ones for one of theirs; this host's own copy for a copy in a sandbox-ai tree; another https
    URL as it is. ValueError for a file of the source host."""
    if catalogs:
        return catalogs
    defaults = m.default_catalogs()
    if location is None:  # added before 0.1.78, which did not record its catalog: the published ones
        return defaults
    published = dict((url, prefix) for prefix, url in m.CATALOGS)
    if location in published:
        return [entry for entry in defaults if entry[0] == published[location]]
    path = Path(str(location or ""))
    if path.name == "catalog.json" and path.parent.name in m.LOCAL_CATALOGS.values():
        prefix = next(p for p, folder in m.LOCAL_CATALOGS.items() if folder == path.parent.name)
        return [entry for entry in defaults if entry[0] == prefix]
    if str(location or "").startswith("https://"):
        return [("c0", location)]
    raise ValueError(f"it was added from the catalog file {shown(location, 200)} of its host, which this host does "
                     "not read: give --catalog FILE_OR_URL (that catalog as this host reads it)")


def plan_catalog(plan, document, unsigned, no_vpn, current_pin, catalogs, fetch):
    """A catalog server: its entry as this host reads its catalog (signature checked here), the source's choices
    given to it as `mcp add` options - the definition that came is never taken."""
    source = document["definition"]
    info = document["catalog"]
    entry_id = info.get("source")
    if not isinstance(entry_id, str):
        raise ValueError("the export names no catalog entry")
    try:
        sources = catalog_sources(info.get("catalog"), catalogs)
    except ValueError as exc:
        problem(plan, "catalog", str(exc), "--catalog FILE_OR_URL")
        return
    entries, problems = m.catalog_entries(sources, fetch)
    bare = entry_id.rpartition(":")[2]
    matches = [entry for _prefix, entry in entries if entry.get("id") == entry_id or entry.get("name") == bare]
    if not matches:
        problem(plan, "catalog", f"the catalog entry {shown(entry_id)} cannot be read here"
                + (f" ({'; '.join(shown(p) for p in problems)})" if problems else ""),
                "check the network there (sandbox-ai doctor), or --catalog FILE_OR_URL")
        return
    entry = matches[0]
    plan["notes"].append(f"added from the catalog as {plan['here']} reads it ({shown(m.catalog_label(entry['_catalog']))}"
                         f"): the definition that came is not taken")
    try:
        signed = m.check_signature(entry, unsigned=unsigned, out=plan["notes"].append)
    except ValueError as exc:
        fix = "--unsigned (the source took it unsigned too)" if info.get("signed") is False else \
            "the catalog there must verify with a trusted key (sandbox-ai doctor)"
        problem(plan, "signature", str(exc), fix)
        return
    wanted = source.get("image", "").rpartition("@")[2] if source.get("image") else None
    if wanted and entry.get("digest") != wanted:
        if current_pin:
            plan["notes"].append(f"the catalog here pins {shown(entry.get('digest'))}, the source runs {wanted}: "
                                 "the catalog's taken (--current-pin)")
        else:
            problem(plan, "pin", f"the catalog here pins {shown(entry.get('digest'))} for {entry['name']}, the source "
                                 f"runs {wanted} (one of the two read an older catalog)",
                    "--current-pin (the catalog's pin here), or update the source (sandbox-ai mcp add again there)")
            return
    wanted_rootfs = (source.get("rootfs") or {}).get("sha256")
    if wanted_rootfs and (entry.get("rootfs") or {}).get("sha256") != wanted_rootfs and not current_pin:
        problem(plan, "pin", f"the catalog here has another rootfs for {entry['name']} than the source runs",
                "--current-pin, or update the source")
        return
    vpn = None if no_vpn else source.get("vpn")
    if vpn and not vpn_known(vpn):
        problem(plan, "vpn", f"{plan['here']} has no VPN profile {vpn}",
                f"make it there (sandbox-ai vpn add {vpn} ...), or --no-vpn (the server goes without it)")
        vpn = None
    elif source.get("vpn") and no_vpn:
        plan["notes"].append(f"without the VPN {source['vpn']} (--no-vpn)")
    cap_modes(plan, entry)
    limits = {key: source.get(key) for key in ("memory", "cpus", "pids", "tmp")}
    definition = m.definition_from_entry(entry, mount_values(plan), env_specs(source), source.get("network", "none"),
                                         limits["memory"], limits["pids"], vpn, local=plan["server"],
                                         cpus=str(limits["cpus"]) if limits["cpus"] is not None else None,
                                         tmp=limits["tmp"], allow_private=source.get("allow_private", ()),
                                         allow_tcp=source.get("allow_tcp", ()))
    opened = [*source.get("allow_private", []), *source.get("allow_tcp", [])]
    if source.get("network") == "lan" or opened:
        # The source's local network is not the target's: what it may reach there is said, as you chose it.
        plan["notes"].append(f"it reaches {'your local network' if source.get('network') == 'lan' else ', '.join(opened)}"
                             f" on {plan['here']} too, as on the source (sandbox-ai mcp set {plan['server']}"
                             " --network full --no-allow there takes it away)")
    definition = {**definition, **signed,
                  **({"catalog": entry["_catalog"]} if isinstance(entry.get("_catalog"), str) else {})}
    if "tool_timeout" in source:
        definition["tool_timeout"] = m.checked_tool_timeout(source["tool_timeout"])
    backend = "both" if "image" in source and "rootfs" in source else "bwrap" if "rootfs" in source else "podman"
    if backend != "podman":
        if not isinstance(entry.get("rootfs"), dict):
            problem(plan, "rootfs", f"the catalog here has no Bubblewrap rootfs for {entry['name']}",
                    "it is added for Podman only: copy it again after adding it with mcp add there")
            return
        plan["notes"].append("its rootfs is fetched, checked against the catalog's SHA-256 and unpacked there")
    if backend == "bwrap":
        definition.pop("image", None)
    plan["backend"] = backend
    plan["entry"] = entry
    plan["definition"] = definition
    if "image" in definition:
        plan["image"] = {"ref": definition["image"], "present": m.image_present(definition["image"]),
                         "action": "pull"}
        plan["notes"].append(f"its image is pulled there by digest: {definition['image']}")
    plan["copied"].append("folders, variables, network, VPN and limits as the source chose them (checked as mcp add "
                          "checks them)")


def capped_text(plan, path, why, folder):
    return (f"{path['target']}: read-only, {why} (the source has it writable; sandbox-ai mcp set {plan['server']} "
            f"--mount {path['target']}={folder}:rw there makes it writable, if you mean it)")


def cap_modes(plan, entry):
    """Each folder of a catalog server's PLAN no more writable than its catalog ENTRY declares it (mcp.declared_mode):
    the source's mode is the other host's word - a folder the catalog has read-only stays so here, said in
    plan["capped"]. A folder you make writable yourself is `mcp set --mount ...:rw` there, as with `mcp add`."""
    known = {mount.get("target"): mount for mount in entry.get("mounts", []) if isinstance(mount, dict)}
    for path in plan["paths"]:
        if path.get("kind") != "mount" or path.get("target") not in known or (path.get("mode") or "ro") != "rw":
            continue
        if m.declared_mode(entry, known[path["target"]]) == "ro":
            path["mode_here"] = "ro"  # ("mode" stays the source's: what its folder is there)
            folder = path["to"] if path["to"] and not path["dropped"] else \
                m.WORKSPACE_HOST if path["target"] == m.WORKSPACE_TARGET else m.DEFAULT_HOST
            plan["capped"].append(capped_text(plan, path, "as its catalog entry declares", folder))


def vpn_known(name):
    try:
        m.launcher().load_vpn_module().load_profile(name)
        return True
    except (OSError, ValueError):
        return False


def plan_image(plan, document, pull_image, no_vpn):
    """An image server of your own: its definition with the folders resolved, validated as mcp.json is."""
    definition = dict(document["definition"])
    for key in ("source", "catalog", "signed_by", "rootfs", "built"):  # (an image of your own: no catalog's word on it;
        # one built on the other host is an image of your own here, never signed)
        definition.pop(key, None)
    mounts = []
    for path in plan["paths"]:
        if path.get("kind") != "mount" or not path["to"]:  # (a dropped one: its default, the to set by plan_import)
            continue
        mode = path.get("mode") or "ro"
        if path["to"] in (m.WORKSPACE_HOST, m.DEFAULT_HOST):  # (its default here: the start's, its own folder)
            host = m.WORKSPACE_HOST if path["to"] == m.WORKSPACE_HOST or path["target"] == m.WORKSPACE_TARGET else \
                str(m.own_folder(plan["server"], path["target"]))
            if path["dropped"] and host == m.WORKSPACE_HOST and mode == "rw":
                # /work's default (--drop-path) is the workspace: read-only, as `mcp add` gives it - the source's "rw"
                # is the other host's word (--path @workspace=@workspace takes it as the source has it).
                mode = "ro"
                plan["capped"].append(capped_text(plan, path, "as the workspace is by default", m.WORKSPACE_HOST))
            mounts.append({"target": path["target"], "host": host, "mode": mode})
            continue
        m.mount_host(f"{path['target']}={path['to']}:{mode}", path["to"], mode, create=False)
        mounts.append({"target": path["target"], "host": path["to"], "mode": mode})
    definition["mounts"] = mounts
    if not mounts:
        definition.pop("mounts", None)
    if definition.get("vpn"):
        if no_vpn:
            plan["notes"].append(f"without the VPN {definition.pop('vpn')} (--no-vpn)")
        elif not vpn_known(definition["vpn"]):
            problem(plan, "vpn", f"{plan['here']} has no VPN profile {definition['vpn']}",
                    f"make it there (sandbox-ai vpn add {definition['vpn']} ...), or --no-vpn")
    definition = m.validated_image(definition, plan["server"])
    plan["definition"] = definition
    # What the source's definition opens of THIS host's network (lan, --allow-private, --allow-tcp): `mcp add` warns
    # of it when you choose it; here it came with the definition, so it is a warning of the plan, not a line among
    # what was copied.
    opened = m.egress_note(plan["server"], definition)
    if opened and (definition.get("network") == "lan" or definition.get("allow_private") or
                   definition.get("allow_tcp")):
        plan["warnings"].append(f"{opened} - on {plan['here']}, as its definition on the source says (sandbox-ai mcp "
                                f"set {plan['server']} --network full --no-allow takes it away)")
    image = document.get("image") if isinstance(document.get("image"), dict) else {}
    present = m.image_present(definition["image"])
    size = image.get("size") if isinstance(image.get("size"), int) else None
    plan["image"] = {"ref": definition["image"], "present": present, "size": size,
                     "action": "none" if present else "pull" if pull_image else "missing"}
    if present is False and not pull_image:
        problem(plan, "image", f"{plan['here']} does not have its image {definition['image']}",
                "--pull-image (Podman pulls it there by its digest), or --copy-image (sent from the source: podman "
                f"save | podman load, {size_text(size)})")
    elif present is False:
        plan["notes"].append(f"its image is pulled there by digest: {definition['image']}")
    plan["copied"].append("its definition, checked as mcp.json is")


def plan_command(plan, document):
    """A command server of your own: its options with the folders resolved, validated as mcp.json is."""
    definition = dict(document["definition"])
    options = list(definition.get("options", []))
    replaced, dropped = {}, set()
    for path in plan["paths"]:
        if path.get("kind") == "workspace":
            if path["dropped"]:
                definition.pop("workspace", None)
            elif path["to"]:
                definition["workspace"] = path["to"]
            continue
        if path["dropped"]:
            dropped.add((path.get("option"), path.get("value")))
        elif path["to"]:
            value = path.get("value") or ""
            replaced[(path.get("option"), value)] = path["to"] + value[len(path["path"]):] \
                if value.startswith(path["path"]) else value.replace(path["path"], path["to"], 1)
    result = []
    for index, name, value, count in option_values(options):
        tokens = options[index:index + count]
        if (name, value) in dropped:
            continue
        if (name, value) in replaced:
            tokens = [f"{name}={replaced[(name, value)]}"] if count == 1 else [name, replaced[(name, value)]]
        result += tokens
    if "options" in definition:
        definition["options"] = result
    plan["definition"] = m.validated({plan["server"]: definition}, "the export")[plan["server"]]
    plan["notes"].append(f"it runs in the {definition.get('agent', 'codex')} image there (built at its first start "
                         "if that host has none)")
    # Shown as written: an option may give the server more of this host than folders, secrets and network (its Git
    # logins, a device, a published port) - what the human copies, they see.
    written = plan["definition"]
    plan["copied"].append("its command and options, checked as mcp.json is: "
                          + shown(shlex.join(written["command"]), 600)
                          + (f"; options: {shown(shlex.join(written['options']), 1200)}" if written.get("options")
                             else "; no options"))


def plan_checks(plan, name, definition):
    """The checks of an image server here: the administrator's policy, the folders you chose (warned about as
    `mcp add --mount` warns), a Tor profile in front."""
    try:
        m.check_image_policy(name, definition)
    except ValueError as exc:
        problem(plan, "policy", str(exc))
    if definition.get("mounts"):
        try:
            m.check_image_mounts(name, definition, remember=False, warn=plan["warnings"].append)
        except ValueError as exc:
            problem(plan, "path", str(exc))
    if definition.get("vpn"):
        try:
            m.checked_behind_tor(name, definition, definition["vpn"], plan["notes"].append)
        except ValueError as exc:
            problem(plan, "vpn", str(exc))


def apply_import(plan, out=print, fetch=None):
    """The import PLAN (plan_import, without problems) carried out: a catalog server's image pulled by digest (its
    rootfs fetched and checked), an image of your own pulled where asked, a writable folder created; then its
    definition written into mcp.json."""
    if plan["problems"]:
        raise ValueError(problems_text(plan))
    definition = dict(plan["definition"])
    name = plan["server"]
    if plan["kind"] == "catalog":
        if plan.get("backend") in ("bwrap", "both"):
            definition = m.validated_image({**definition, "rootfs": m.entry_rootfs(plan["entry"], fetch)}, name)
        if "image" in definition:
            m.pull_image(definition["image"], name, out)
    elif plan["kind"] == "image" and plan["image"] and plan["image"]["action"] == "pull":
        m.pull_image(definition["image"], name, out)
    if "image" in definition or "rootfs" in definition:
        m.create_mounts(definition, out, name)
    m.write_definition(name, definition)
    m.LOG.info("mcp", f"{name} imported ({plan['kind']}, from {plan['from_name']}"
               + (", replacing the one there" if plan["replace"] else "") + ")")
    return definition


def problems_text(plan, host=None):
    """The problems of PLAN, each with its fix, as lines."""
    where = f" on {host}" if host else ""
    lines = [f"MCP server {plan['from_name']}{where}" + (f" as {plan['server']}" if plan["server"] != plan["from_name"]
                                                         else "") + ": not copied:"]
    for item in plan["problems"]:
        lines.append(f"  - {item['text']}" + (f"; resolve: {item['fix']}" if item.get("fix") else ""))
    return "\n".join(lines)


def folder_state(path, host):
    """What becomes of the source's folder PATH (a plan's) on HOST, for people."""
    if path.get("dropped") and path.get("kind") == "mount" and path.get("to"):
        return f"the default folder on {host} (" + (
            "the workspace of the start that uses it there" if path["to"] == m.WORKSPACE_HOST else
            "a folder of its own there, new and empty, made by sandbox-ai there") + ")"
    if path.get("dropped"):
        return "left out"
    if path.get("kind") == "mount" and path.get("to") == m.WORKSPACE_HOST:
        return f"the workspace of the start that uses it on {host}"
    if path.get("kind") == "mount" and path.get("to") == m.DEFAULT_HOST:
        return f"a folder of its own on {host}, new and empty"
    return f"-> {path['to']}" if path.get("to") else "not resolved"


def plan_text(plan, host=None):
    """What PLAN does, for people."""
    where = f" on {host}" if host else ""
    head = f"MCP server {plan['from_name']}" + (f" as {plan['server']}" if plan["server"] != plan["from_name"] else "")
    how = ("added from its catalog there" if plan["how"] == "catalog" else "its definition imported")
    if plan["problems"]:  # (not copied: said below - never "imported" above it)
        how = ("would be added from its catalog there" if plan["how"] == "catalog" else
               "its definition would be imported")
    lines = [f"{head}{where}: {how}" + (" (replaces the one there)" if plan["replace"] else "")]
    for text in plan["copied"]:
        lines.append(f"  copied: {text}")
    for path in plan["paths"]:
        lines.append(f"  folder {path_label(path)}: " + folder_state(path, host or plan.get("here") or "this host"))
    secrets = plan.get("secrets") or {}
    if secrets.get("needed"):
        lines.append("  secrets (names only, never a value): " + ", ".join(
            sorted({s["secret"] for s in secrets["needed"]}))
            + (f"; missing there: {', '.join(secrets['missing'])}" if secrets.get("missing") else ""))
    image = plan.get("image")
    if image:
        lines.append(f"  image {image['ref']}: " + {"pull": "pulled there by digest", "none": "there already",
                                                   "missing": "not there", "copied": "sent from the source"}.get(
            image.get("action"), str(image.get("action"))))
    for text in plan["changes"]:
        lines.append(f"  changed: {text}")
    for text in plan.get("capped") or ():
        lines.append(f"  capped: {text}")
    for text in plan["notes"]:
        lines.append(f"  note: {text}")
    for text in plan["warnings"]:
        lines.append(f"  warning: {text}")
    if plan["problems"]:
        lines.append(problems_text(plan, host))
    return "\n".join(lines)


def import_main(a, out=print, stdin=None):
    """`sandbox-ai mcp import FILE|-`: the plan (--dry-run), else the server written. Exit status 1 with the problems
    when there are any."""
    if a.file == "-":
        text = (stdin or sys.stdin).read(DOCUMENT_LIMIT + 1)
    else:
        text = Path(os.path.expanduser(a.file)).read_text()[:DOCUMENT_LIMIT + 1]
    document = read_document(text)
    catalogs = [(f"c{index}", value) for index, value in enumerate(a.catalog)] or None
    plan = plan_import(document, a.as_name, a.replace, parse_paths(a.path), a.drop_path, a.unsigned,
                       a.secrets_later, a.pull_image, a.no_vpn, a.current_pin, catalogs)
    say = (lambda text: print(text, file=sys.stderr)) if a.json else out
    if plan["problems"] or a.dry_run:
        if a.json:
            print(json.dumps({**plan, "entry": None, "written": False, "dry_run": a.dry_run}, indent=2))
        else:
            out(plan_text(plan))
        return 1 if plan["problems"] else 0
    if plan["replace"] and not a.yes:
        if not confirm(f"Replace the MCP server {plan['server']} here with {plan['from_name']} of the export?"):
            raise ValueError(f"{plan['server']} is not replaced (--replace asks first; --yes when nothing can ask)")
    definition = apply_import(plan, say)
    say(f"sandbox-ai mcp: {plan['server']} imported; use it with --mcp {plan['server']}")
    if a.json:
        print(json.dumps({**plan, "entry": None, "definition": definition, "written": True, "dry_run": False},
                         indent=2))
    return 0


def confirm(question):
    """QUESTION answered yes at a terminal; False when nothing can ask."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    answer = input(f"{m.launcher().printable(question)} [y/N] ")
    return answer.strip().lower() in ("y", "yes", "j", "ja")


# --- copy: from one host to another, through this machine -----------------------------------------------------------

class TooOld(ValueError):
    """The sandbox-ai on HOST does not know `sandbox-ai mcp COMMAND` yet."""

    def __init__(self, host, command):
        self.host, self.command = host, command
        version = getattr(m.launcher(), "__version__", "?") if m else "?"
        super().__init__(f"the sandbox-ai on {host} does not know `sandbox-ai mcp {command}` yet: it needs "
                         f"{'.'.join(map(str, COPY_SINCE))} or later (this machine has {version}); update it from "
                         f"here: sandbox-ai hosts update {host} --from-here, then copy again")


def too_old(status, err):
    """Whether an answer (STATUS, stderr ERR) is an older sandbox-ai's that does not know the command or its
    options (argparse's usage error)."""
    return status == 2 and bool(re.search(r"invalid choice: '(?:export|import|copy|image-load)'|unrecognized "
                                          r"arguments", err or ""))


def host_label(host):
    return "this machine" if host == hosts_module().LOCAL else host


def run_on(host, args, input=None, timeout=EXPORT_TIMEOUT):
    """(status, out, err) of `sandbox-ai ARGS` on HOST (local: this machine's sandbox-ai, as a command too)."""
    return hosts_module().run(host, list(args), input=input, timeout=timeout)


def check_version(host):
    """TooOld when the last status of HOST found a sandbox-ai older than COPY_SINCE (not asked here)."""
    if host == hosts_module().LOCAL:
        return
    if hosts_module().older_than(host, COPY_SINCE):
        raise TooOld(host, "copy")


def export_from(host, name):
    """The export document of server NAME of HOST."""
    status, out, err = run_on(host, ["mcp", "export", name, "--json"])
    if too_old(status, err):
        raise TooOld(host, "export")
    if status != 0:
        raise ValueError(f"{host_label(host)}: sandbox-ai mcp export {name}: "
                         f"{shown((err or out).strip()[-600:] or f'exit status {status}', 600)}")
    return read_document(out)


def import_args(document, a, dry_run, replace_yes=False):
    """The words of `sandbox-ai mcp import -` on the target for DOCUMENT with the options of `mcp copy` A."""
    args = ["mcp", "import", "-", "--json"]
    if a.as_name:
        args.append(f"--as={a.as_name}")  # (NAME=VALUE: a value is never read as an option there)
    if a.replace:
        args += ["--replace"] + (["--yes"] if replace_yes else [])
    args += [f"--path={value}" for value in a.path or ()]
    args += [f"--drop-path={value}" for value in a.drop_path or ()]
    for flag, on in (("--unsigned", a.unsigned), ("--secrets-later", a.secrets_later), ("--pull-image", a.pull_image),
                     ("--no-vpn", a.no_vpn), ("--current-pin", a.current_pin)):
        if on:
            args.append(flag)
    args += [f"--catalog={value}" for value in a.catalog or ()]
    if dry_run:
        args.append("--dry-run")
    return args


PLAN_TEXT = 2000   # characters of one text of a plan that came from another host
PLAN_ITEMS = 200   # entries of one of its lists or objects


def received(value, depth=0):
    """VALUE of a plan another host sent, as data: every text printable (no escape sequence, no bidi control reaches
    this terminal) and bounded, lists and objects bounded, nothing nested deeper than a plan is."""
    if isinstance(value, str):
        return m.launcher().printable(value)[:PLAN_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 8:
        return None
    if isinstance(value, list):
        return [received(item, depth + 1) for item in value[:PLAN_ITEMS]]
    if isinstance(value, dict):
        return {m.launcher().printable(str(key))[:200]: received(item, depth + 1)
                for key, item in list(value.items())[:PLAN_ITEMS]}
    return None


def received_plan(plan):
    """The import plan the target host answered with (plan_import there), made printable (received) and its shape
    checked - what copy() and plan_text() read of it -, or None: that host's sandbox-ai did not answer with a plan."""
    def texts(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def text_or_none(value):
        return value is None or isinstance(value, str)
    if not isinstance(plan, dict):
        return None
    plan = received(plan)
    secrets, image = plan.get("secrets"), plan.get("image")
    well_formed = (
        all(isinstance(plan.get(key), str) for key in ("server", "from_name"))
        and isinstance(plan.get("replace"), bool) and text_or_none(plan.get("how"))
        and all(texts(plan.get(key)) for key in ("copied", "notes", "warnings", "changes"))
        and texts(plan.get("capped", []))
        and isinstance(plan.get("problems"), list)
        and all(isinstance(item, dict) and isinstance(item.get("text"), str)
                and all(text_or_none(item.get(key)) for key in ("kind", "fix")) for item in plan["problems"])
        and isinstance(plan.get("paths"), list)
        and all(isinstance(item, dict) and isinstance(item.get("path"), str)
                and isinstance(item.get("dropped", False), bool)
                and all(text_or_none(item.get(key)) for key in ("kind", "target", "option", "value", "mode", "to"))
                for item in plan["paths"])
        and isinstance(secrets, dict) and texts(secrets.get("missing", []))
        and isinstance(secrets.get("needed", []), list)
        and all(isinstance(item, dict) and isinstance(item.get("secret"), str) for item in secrets.get("needed", []))
        and (image is None or isinstance(image, dict) and isinstance(image.get("ref"), str)
             and text_or_none(image.get("action"))))
    return plan if well_formed else None


def import_on(host, document, a, dry_run, replace_yes=False):
    """The plan (dry run) or result of the import of DOCUMENT on HOST: (plan dict, exit status)."""
    status, out, err = run_on(host, import_args(document, a, dry_run, replace_yes), input=json.dumps(document),
                              timeout=EXPORT_TIMEOUT if dry_run else IMPORT_TIMEOUT)
    if too_old(status, err):
        raise TooOld(host, "import")
    try:
        plan = received_plan(json.loads(out))
    except (ValueError, RecursionError):
        plan = None
    if plan is None:
        raise ValueError(f"{host_label(host)}: sandbox-ai mcp import: "
                         f"{shown((err or out).strip()[-600:] or f'exit status {status}', 600)}")
    plan["said"] = [shown(line, 600) for line in (err or "").splitlines() if line.strip()][-20:]
    return plan, status


# What an image reference of an export must look like before it becomes an argument for the target (which checks it
# in full: mcp.IMAGE_REF): a name without anything an option or a shell reads, pinned by digest.
PINNED_REF = re.compile(r"[a-z0-9][A-Za-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64}")


def copy_image(source, target, document, out=print, popen=subprocess.Popen):
    """The image of DOCUMENT's server sent from SOURCE to TARGET: `sandbox-ai mcp export NAME --image` there piped
    into `sandbox-ai mcp image-load REF` here, through this machine; ValueError when it did not arrive under its
    pinned digest."""
    hosts = hosts_module()
    image = document.get("image") if isinstance(document.get("image"), dict) else {}
    ref = image.get("ref")
    if not isinstance(ref, str) or not PINNED_REF.fullmatch(ref):  # (the export is the source's word)
        raise ValueError(f"{host_label(source)}: its export of {shown(document['name'], 60)} names no image pinned by "
                         "digest: nothing to send")
    out(f"sandbox-ai mcp: sending the image {ref} ({size_text(image.get('size'))}) from "
        f"{host_label(source)} to {host_label(target)}")
    # The sender's standard error into a file of its own: a pipe nobody reads while the image flows (64 KiB of
    # podman save's progress and ssh's warnings) would stop the sender half-way, and the copy wait IMAGE_TIMEOUT.
    with tempfile.TemporaryFile() as sender_said:
        sender = popen(hosts.argv(source, ["mcp", "export", document["name"], "--image"]), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=sender_said)
        try:
            receiver = popen(hosts.argv(target, ["mcp", "image-load", ref]), stdin=sender.stdout,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except BaseException:
            sender.kill()
            sender.wait()
            raise
        sender.stdout.close()
        try:
            said, problem_text = receiver.communicate(timeout=IMAGE_TIMEOUT)
            sender.wait(timeout=60)
        except subprocess.TimeoutExpired:
            for process in (sender, receiver):
                process.kill()
                process.wait()
            raise ValueError(f"the image did not arrive within {IMAGE_TIMEOUT} s") from None
        sender_said.seek(max(0, sender_said.seek(0, os.SEEK_END) - (64 << 10)))  # (its end says why)
        sender_err = sender_said.read()
    if sender.returncode != 0:
        if too_old(sender.returncode, sender_err.decode(errors="replace")):
            raise TooOld(source, "export --image")
        raise ValueError(f"{host_label(source)}: the image could not be saved: "
                         f"{shown(sender_err.decode(errors='replace').strip()[-400:], 400)}")
    if receiver.returncode != 0:
        if too_old(receiver.returncode, problem_text.decode(errors="replace")):
            raise TooOld(target, "image-load")
        raise ValueError(f"{host_label(target)}: {shown(problem_text.decode(errors='replace').strip()[-600:], 600)}")
    return said


def copy(a, out=print):
    """`sandbox-ai mcp copy NAME... --from HOST --to HOST`: each server exported from the source, planned on the
    target (every problem of every server said, nothing written while there is one), then imported there - and with
    --attach RUN attached to that running start there. {"from", "to", "servers": [plan], "attached": [...],
    "written", "dry_run"}."""
    hosts = hosts_module()
    if len(a.names) > 1 and a.as_name:
        raise ValueError("--as names one server: copy one NAME with it")
    for host in (a.source, a.target):
        if host != hosts.LOCAL and host not in hosts.load():
            raise ValueError(f"no host {shown(host, 60)}; see: sandbox-ai hosts list (local: this machine)")
    if a.copy_image and a.pull_image:
        raise ValueError("--copy-image sends the image from the source, --pull-image pulls it there: choose one")
    check_version(a.source)
    check_version(a.target)
    result = {"from": a.source, "to": a.target, "servers": [], "attached": [], "written": False,
              "dry_run": bool(a.dry_run)}
    documents = [export_from(a.source, name) for name in dict.fromkeys(a.names)]
    plans = []
    for document in documents:
        plan, _status = import_on(a.target, document, a, dry_run=True)
        if a.copy_image and plan.get("image") and plan["image"].get("action") == "missing":
            plan["problems"] = [p for p in plan["problems"] if p.get("kind") != "image"]
            plan["image"]["action"] = "copy"
            plan["notes"].append(f"its image is sent from {host_label(a.source)} (--copy-image, "
                                 f"{size_text((document.get('image') or {}).get('size'))})")
            plan["ok"] = not plan["problems"]
        plans.append(plan)
    result["servers"] = plans
    blocked = [plan for plan in plans if plan["problems"]]
    if blocked or a.dry_run:
        return result
    if any(plan["replace"] for plan in plans) and not a.yes:
        names = ", ".join(plan["server"] for plan in plans if plan["replace"])
        if not confirm(f"Replace {names} on {host_label(a.target)}?"):
            raise ValueError(f"{names} not replaced (--replace asks first; --yes when nothing can ask)")
    for document, plan in zip(documents, plans):
        if plan.get("image") and plan["image"].get("action") == "copy":
            copy_image(a.source, a.target, document, out)
        done, status = import_on(a.target, document, a, dry_run=False, replace_yes=True)
        if status != 0 or done["problems"]:
            raise ValueError(problems_text(done, host_label(a.target)) if done["problems"] else
                             f"{host_label(a.target)}: sandbox-ai mcp import {plan['server']}: exit status {status}: "
                             + " ".join(done.get("said") or []))
        plan.update({"written": True, "definition": done.get("definition"), "said": done.get("said", [])})
        result["written"] = True
    if a.attach:
        for plan in plans:
            status, said, err = run_on(a.target, ["mcp", "attach", plan["server"], a.attach, "--live", "--json"],
                                       timeout=630)
            try:
                attached = json.loads(said) if status == 0 else None
            except (ValueError, RecursionError):
                attached = None
            if not isinstance(attached, dict):
                result["attached"].append({"server": plan["server"], "start": a.attach, "error": shown(
                    (err or said).strip()[-600:] or f"exit status {status}", 600)})
                continue
            result["attached"].append({**received(attached), "server": plan["server"], "start": a.attach})
    return result


def copy_text(result):
    """The result of copy() for people."""
    target = host_label(result["to"])
    lines = [plan_text(plan, target) for plan in result["servers"]]
    blocked = [plan for plan in result["servers"] if plan["problems"]]
    if blocked:
        lines.append("Nothing was copied: resolve what is said above, then copy again (--dry-run shows it first).")
    elif result["dry_run"]:
        lines.append("Dry run: nothing was copied.")
    else:
        lines.append(f"Copied to {target}: {', '.join(plan['server'] for plan in result['servers'])}.")
    for attached in result["attached"]:
        if attached.get("error"):
            lines.append(f"{attached['server']} was copied but not attached to {attached['start']}: "
                         f"{attached['error']}")
        else:
            lines.append(attached.get("message") or f"{attached['server']} attached to {attached['start']}")
    return "\n".join(lines)


def copy_main(a, out=print):
    result = copy(a, out=(lambda text: print(text, file=sys.stderr)) if a.json else out)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        out(copy_text(result))
    blocked = any(plan["problems"] for plan in result["servers"])
    failed = any(entry.get("error") for entry in result["attached"])
    return 1 if blocked or failed else 0


# --- the image of a server of your own, from one Podman to another -------------------------------------------------

def save_image(name, stdout=None, run=subprocess.run):
    """`mcp export NAME --image`: the image of server NAME (yours, pinned by digest) as `podman save` writes it, on
    STDOUT. Only that image: nothing else of this host's."""
    _path, document = m.read_config()
    if name not in document:
        raise ValueError(f"MCP server {name} is not one of yours: sandbox-ai mcp list")
    definition = m.validated({name: document[name]}, m.config_file())[name]
    if "image" not in definition:
        raise ValueError(f"MCP server {name} has no Podman image")
    if not m.image_present(definition["image"]):
        raise ValueError(f"this host does not have the image {definition['image']}")
    stdout = stdout if stdout is not None else sys.stdout.buffer
    stdout.flush()
    done = run(["podman", "save", "--format", "oci-archive", definition["image"]], stdout=stdout,
               stdin=subprocess.DEVNULL, check=False)
    return done.returncode


ARCHIVE_INDEX_LIMIT = 4 << 20  # bytes of an archive's index.json, manifest.json or repositories


def archive_names(path):
    """The image names the archive at PATH would give this host's Podman (`podman load` names each image as the
    archive says): an OCI archive's index.json (the annotation org.opencontainers.image.ref.name of each manifest),
    a Docker archive's manifest.json (RepoTags) and its older `repositories`. Only the tar's own index is read,
    nothing unpacked. ValueError when it is no image archive this reads (then nothing is loaded)."""
    import posixpath
    import tarfile
    names, indexes = set(), 0
    refused = ValueError("what arrived is not an image archive (podman save --format oci-archive): nothing was loaded")
    try:
        with tarfile.open(path, mode="r:*") as tar:
            for member in tar:
                name = posixpath.normpath(member.name)
                if name not in ("index.json", "manifest.json", "repositories"):
                    continue
                if not member.isfile() or member.size > ARCHIVE_INDEX_LIMIT:
                    raise refused
                value = json.loads(tar.extractfile(member).read(ARCHIVE_INDEX_LIMIT + 1))
                indexes += 1
                if name == "index.json":
                    for manifest in value["manifests"]:
                        named = (manifest.get("annotations") or {}).get("org.opencontainers.image.ref.name")
                        if named is not None:
                            names.add(str(named))
                elif name == "manifest.json":
                    for image in value:
                        names.update(str(tag) for tag in image.get("RepoTags") or [])
                else:
                    for repository, tags in value.items():
                        names.update(f"{repository}:{tag}" for tag in tags)
    except (tarfile.TarError, OSError, EOFError, ValueError, RecursionError, KeyError, TypeError, AttributeError):
        raise refused from None
    if not indexes:
        raise refused
    return sorted(names)


# The names of sandbox-ai's own images in Podman (the agents' localhost/AGENT-sandbox[-VARIANT], the team images, the
# browser's, cairn's, the VPN containers' and your images' localhost/sandbox-ai-...): the image a later start runs.
OWN_IMAGE = re.compile(r"localhost/(?:[a-z0-9]+(?:-[a-z0-9]+)*-sandbox(?:-[A-Za-z0-9_.-]+)?|sandbox-ai-[A-Za-z0-9_.-]+)"
                       r"(?=$|[:@])")


def own_image_name(name):
    """Whether NAME (an image reference, a name an archive gives) is one of sandbox-ai's own images' names: an
    archive from elsewhere never gets to give its image such a name (`podman load` would - and every later start of
    that agent would run it)."""
    return bool(OWN_IMAGE.match(str(name)))


def spool_folder():
    """Where an arriving image waits to be read before Podman gets it: /var/tmp (a disk; an image does not fit every
    /tmp), else the system's temporary folder."""
    return "/var/tmp" if os.path.isdir("/var/tmp") and os.access("/var/tmp", os.W_OK | os.X_OK) else None


def load_image(ref, stdin=None, run=subprocess.run, out=print):
    """`mcp image-load REF`: an image archive from STDIN loaded into this host's Podman; it counts only when Podman
    then has REF (its pinned digest). The archive comes from another host and is read before Podman sees it: one that
    names an image outside REF's repository is refused - `podman load` would give that name to the archive's image,
    taking it from the image of this host that has it (localhost/sandbox-ai-claude:latest: every later start).
    ValueError otherwise: nothing of it is used."""
    import tempfile
    if not isinstance(ref, str) or not m.IMAGE_REF.fullmatch(ref):
        raise ValueError("REF must be REGISTRY/PATH@sha256:<64 hex>, the pinned image of the server")
    stdin = stdin if stdin is not None else sys.stdin.buffer
    repository = ref.partition("@")[0]
    if own_image_name(ref):
        raise ValueError(f"{repository} is the name of one of sandbox-ai's own images: no archive is loaded as it "
                         "(sandbox-ai build makes those here)")
    with tempfile.NamedTemporaryFile(prefix="sandbox-ai-image-", suffix=".tar", dir=spool_folder()) as spooled:
        shutil.copyfileobj(stdin, spooled, 1 << 20)
        spooled.flush()
        others = [name for name in archive_names(spooled.name)
                  if name != repository and not name.startswith((repository + ":", repository + "@"))]
        others += [name for name in archive_names(spooled.name) if own_image_name(name) and name not in others]
        if others:
            raise ValueError(f"the archive that arrived names other images than the server's ({repository}): "
                             f"{shown(', '.join(others[:5]), 400)}. Loading it would give these names to its "
                             "images here: nothing was loaded")
        done = run(["podman", "load", "--input", spooled.name], stdin=subprocess.DEVNULL, capture_output=True,
                   check=False)
    if done.returncode != 0:
        raise ValueError(f"podman load failed: {shown(done.stderr.decode(errors='replace').strip()[-400:], 400)}")
    if not m.image_present(ref):
        raise ValueError(f"the image arrived, but Podman here does not have it as {ref} (Podman keeps a registry's "
                         "digest only for an image it pulled): pull it there by its digest instead (mcp copy "
                         "--pull-image), or push it to a registry that host reaches")
    out(f"sandbox-ai mcp: the image {ref} is there")
    return 0
