#!/usr/bin/env python3
"""The agent's intro: what its sandbox has, in one short file the agent is told to read.

A start writes /sandbox/session/INTRO.md (the run's session directory, read-only in the sandbox): each folder,
toolchain, MCP server, service and device of the run, where it is in the sandbox and how to use it. Every harness
gets one line that names the file where it reads its instructions from its start (agent_sandbox.start_note,
NOTE_PLACES) - never a file of your repository. On by default; --no-intro, "agent_intro": false in your defaults
or SANDBOX_AI_INTRO=0 in the environment start without both (--intro turns it on for one start).

The file is rewritten when something is attached or removed while the sandbox runs (`sandbox-ai toolchain
attach|detach`, `mcp attach|detach --live`, `devices add|remove`, `workspace add|remove`): refresh(record) reads what
the start had (intro.json in the run's directory, on the host only) and what the run's own registries hold now.

It names nothing the agent does not see anyway: paths of the sandbox (never a host path), names, commands, host names
of registries. No secret, no variable's value, no token.

Standard library only.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = "INTRO.md"                      # in the run's session directory
PATH = "/sandbox/session/" + FILE      # where the agent reads it
NOTE_FILE = "agent-note.md"            # what a harness that takes a file reads at its start (the notes joined)
NOTE_PATH = "/sandbox/session/" + NOTE_FILE
STATE = "intro.json"                   # in the run's directory (the host's): what the file was made from
DEFAULTS_KEY = "agent_intro"           # your defaults: false starts without the intro (--intro turns it on again)
# The line every harness reads at its start (short: it is part of every conversation).
LINE = ("sandbox-ai: what this sandbox gives you - folders, toolchains, MCP servers, services, devices - is listed in "
        f"{PATH}, each with where it is and how to use it. Read it before you start; it is rewritten when something "
        "is added or removed while you run.")
KINDS = {"project": "the project (you start here)", "workspace": "a further project folder",
         "mount": "an extra folder or file", "added": "added while you run"}
MODES = {"ro": "read-only", "rw": "read-write"}


def module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_intro", HERE / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def plain(value, limit=200):
    """VALUE as one line of the file: printable characters only, no Markdown that would start a block."""
    text = "".join(char if char.isprintable() else "?" for char in str(value))[:limit]
    return text.replace("`", "'")


def registries(entry, plan=None, opened=None):
    """(opened, closed) of the registries the toolchain manifest ENTRY names: its hosts and the launcher's presets
    (shown as "the preset NAME"). PLAN (a start's toolchains.start_plan) or OPENED (a run's registry: what an attach
    opened) says which are open."""
    wanted = [*(entry.get("hosts") or []), *(f"--allow-{preset}" for preset in entry.get("allow") or [])]
    if plan is not None:
        open_now = {*plan.get("hosts", []), *(f"--allow-{preset}" for preset in plan.get("presets", []))}
    else:
        open_now = set(opened or [])
    return [item for item in wanted if item in open_now], [item for item in wanted if item not in open_now]


def toolchain_item(entry, opened, closed, why=None):
    return {"name": entry["name"], "version": entry.get("version"), "sandbox": f"/opt/toolchains/{entry['name']}",
            "commands": sorted(entry.get("commands") or {}), "opened": list(opened), "closed": list(closed),
            "why_closed": why if closed else None}


def resources(a, launcher):
    """What the start A gives its agent, as the file lists it (LAUNCHER: agent_sandbox). Sandbox paths only."""
    mode = "ro" if getattr(a, "workspace_ro", False) else "rw"
    folders = [{"sandbox": dest, "mode": mode, "kind": "project" if index == 0 else "workspace"}
               for index, (_host, dest) in enumerate(launcher.workspace_targets(a))]
    for value in getattr(a, "mount", None) or []:
        with contextlib.suppress(ValueError):
            _source, dest, mount_mode = launcher.parse_mount(value)
            folders.append({"sandbox": dest, "mode": mount_mode, "kind": "mount"})
    plan = getattr(a, "toolchain_plan", None)
    toolchains = []
    if plan:
        if launcher.strict_mode(a):
            why = f"the security mode {getattr(a, 'mode', None)}"
        elif getattr(a, "offline", False):
            why = "the sandbox is offline"
        elif getattr(a, "toolchain_offline", False):
            why = "started with --toolchain-offline"
        else:
            why = "the administrator's policy"
        for entry in plan["entries"]:
            opened, closed = registries(entry, plan=plan)
            toolchains.append(toolchain_item(entry, opened, closed, why))
    servers = [] if getattr(a, "mcp_socket_dir", None) else list(dict.fromkeys(getattr(a, "mcp", None) or []))
    services = [{"name": service["name"], "reachable_at": [f"localhost:{port}" for port in service.get("ports") or []]}
                for service in getattr(a, "services", None) or []]
    try:
        devices = [{"path": path, "kind": kind} for path, kind in launcher.devices(a)]
    except (OSError, ValueError):
        devices = []
    return {"version": 1, "agent": a.agent, "folders": folders, "toolchains": toolchains,
            "mcp": [{"name": name} for name in servers], "services": services, "devices": devices,
            "gpu": getattr(a, "gpu", None) or None, "offline": bool(getattr(a, "offline", False))}


def shown_registry(value):
    return f"the preset {value[len('--allow-'):]}" if value.startswith("--allow-") else value


def text(state):
    """INTRO.md for STATE (resources(), or what refresh() made of it)."""
    lines = ["# What this sandbox gives you (sandbox-ai)", "",
             "Written by sandbox-ai, read-only. Rewritten when something is added or removed while you run: read it "
             "again when a folder, command or tool you expect is missing.", ""]
    folders = state.get("folders") or []
    if folders:
        lines += ["## Folders", ""]
        for item in folders:
            lines.append(f"- `{plain(item['sandbox'])}` - {KINDS.get(item.get('kind'), 'a folder')}, "
                         f"{MODES.get(item.get('mode'), 'read-write')}")
        lines.append("")
    toolchains = state.get("toolchains") or []
    if toolchains:
        lines += ["## Toolchains", "",
                  "Compilers and SDKs mounted read-only below `/opt/toolchains`; their commands are on your PATH. A "
                  "shell opened before one was added reads them with `. /opt/toolchains/env.sh`.", ""]
        for item in toolchains:
            line = f"- {plain(item['name'])} {plain(item.get('version') or '')}".rstrip() + \
                f" at `{plain(item['sandbox'])}`: {', '.join(plain(c) for c in item['commands']) or 'no commands'}."
            if item.get("opened"):
                line += f" Registries open: {', '.join(shown_registry(plain(v)) for v in item['opened'])}."
            if item.get("closed"):
                line += (f" Registries closed ({plain(item.get('why_closed') or 'not opened')}): "
                         f"{', '.join(shown_registry(plain(v)) for v in item['closed'])} - a download from there is "
                         "asked about or refused; work with what is here.")
            lines.append(line)
        lines.append("")
    servers = state.get("mcp") or []
    if servers:
        lines += ["## MCP servers", "",
                  "Each is in your MCP configuration under its name: call its tools as any MCP tool. One added while "
                  "you run is listed once your MCP configuration is read again (a restart of you, done by the human, "
                  "where your harness needs one).", ""]
        lines += [f"- {plain(item['name'])}" for item in servers] + [""]
    services = state.get("services") or []
    if services:
        lines += ["## Services", "",
                  "Containers that run beside you and share your network: reach each at its address. "
                  "`$SANDBOX_AI_SERVICES` lists them too.", ""]
        for item in services:
            where = ", ".join(plain(address) for address in item.get("reachable_at") or []) or "no port"
            lines.append(f"- {plain(item['name'])}: {where}")
        lines.append("")
    devices = state.get("devices") or []
    if devices or state.get("gpu"):
        lines += ["## Devices", "", "Devices of the host handed to you; open them as on any Linux machine.", ""]
        lines += [f"- `{plain(item['path'])}`" + (f" ({plain(item['kind'])})" if item.get("kind") not in (None, "node")
                                                 else "") for item in devices]
        if state.get("gpu"):
            lines.append(f"- GPU: {plain(state['gpu'])}")
        lines.append("")
    lines += ["## Network", "",
              "No network at all (offline)." if state.get("offline") else
              "Only what sandbox-ai allows is reachable; anything else is asked about or refused. A refused "
              "connection is the sandbox's rule, not an error of yours: say what you need instead of working around "
              "it. More: the sandbox-ai skill.", ""]
    return "\n".join(lines)


def write_private(path, content, mode):
    """CONTENT as PATH, replaced at once (a reader sees the old file or the new one, never half of it)."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.new")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode),
              "w", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def write_state(directory, state):
    """What the file was made from, in the run's DIRECTORY (the host's, only yours): refresh() starts from it."""
    write_private(Path(directory) / STATE, json.dumps(state), 0o600)


def load_state(directory):
    try:
        data = json.loads((Path(directory) / STATE).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("folders"), list) else None


def current(record, state, loaders=None):
    """STATE (the start's) with what the run's registries hold now: the folders, toolchains and devices added while
    it runs, and the MCP servers of run.json (LOADERS: {"workspaces", "toolchains", "devices"} for tests)."""
    loaders = loaders or {}
    now = dict(state)
    directory = Path(record["directory"])
    with contextlib.suppress(Exception):  # the folders added while it runs (workspaces.json)
        rows = (loaders.get("workspaces") or module("workspaces").present_workspaces)(record)
        start = [item for item in state.get("folders") or [] if item.get("kind") != "added"]
        taken = {item["sandbox"] for item in start}
        now["folders"] = start + [{"sandbox": row["sandbox"], "mode": row.get("mode") or "rw", "kind": "added"}
                                  for row in rows if isinstance(row.get("sandbox"), str)
                                  and row.get("state", "attached") == "attached" and row["sandbox"] not in taken]
    with contextlib.suppress(Exception):  # the toolchains attached while it runs (toolchains.json)
        entries = (loaders.get("toolchains") or module("toolchains").load)(record)
        attached = [entry for entry in entries if entry.get("state") == "attached"]
        names = {entry["name"] for entry in attached}
        start = [item for item in state.get("toolchains") or [] if not item.get("added")
                 and (not entries or item["name"] in names)]
        taken = {item["name"] for item in start}
        added = []
        for entry in attached:
            if entry["name"] in taken or entry.get("source") != "added":
                continue
            opened, closed = registries(entry.get("manifest") or {}, opened=entry.get("opened") or [])
            added.append({**toolchain_item({**(entry.get("manifest") or {}), "name": entry["name"],
                                            "version": entry.get("version")}, opened, closed,
                                           "not opened when it was attached"), "added": True})
        now["toolchains"] = start + added
    with contextlib.suppress(Exception):  # the devices attached now (devices.json: the start's and those added)
        if (directory / "devices.json").exists() or loaders.get("devices"):
            grants = (loaders.get("devices") or module("devices").load)(record)
            paths = list(dict.fromkeys(path for grant in grants if grant.get("state") == "attached"
                                       for path in (grant.get("nodes") or {})))
            kinds = {item["path"]: item.get("kind") for item in state.get("devices") or []}
            now["devices"] = [{"path": path, "kind": kinds.get(path)} for path in paths]
    with contextlib.suppress(Exception):  # the MCP servers it reaches now (run.json: the start's and those attached)
        run = json.loads((directory / "run.json").read_text())
        if isinstance(run.get("mcp_servers"), list):
            now["mcp"] = [{"name": name} for name in run["mcp_servers"] if isinstance(name, str)]
    return now


def refresh(record, loaders=None):
    """The intro of the running sandbox RECORD written again from what it has now. True when the file changed; False
    when the run has none (--no-intro, an older sandbox-ai) or nothing changed. Never raises: an attach that worked is
    never failed by its note."""
    try:
        state = load_state(record["directory"])
        session = record.get("session_dir")
        if state is None or not isinstance(session, str) or not Path(session).is_dir():
            return False
        target = Path(session) / FILE
        content = text(current(record, state, loaders))
        try:
            if target.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeDecodeError):
            pass
        write_private(target, content, 0o644)
        return True
    except Exception:  # noqa: BLE001 - see above
        return False


def refreshed(record, out=print):
    """refresh(RECORD), said in one line when the file changed (the live commands' last step)."""
    if refresh(record):
        out(f"  the agent's intro lists what the sandbox has now: {PATH}")
        return True
    return False


def plan(a, launcher, place=None):
    """The dry run's account: whether the agent is told, the file, its text and where the agent reads the line."""
    return {"file": PATH, "line": LINE, "agent_reads_it": place, "text": text(resources(a, launcher))}
