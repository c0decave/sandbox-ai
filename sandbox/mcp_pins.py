#!/usr/bin/env python3
"""The MCP tool definitions pinned per project: what each server's tools/list told the model when it was trusted.

  sandbox-ai mcp tools [NAME] [--project DIR] [--json]     the pinned servers of the project (DIR, default: here),
                                                         or NAME's pinned tools with their definitions
  sandbox-ai mcp tools --all [--json]                      the pins of every project, each with its folder
  sandbox-ai mcp tools NAME --forget [--project DIR]       drop NAME's pin: its next list is pinned anew
  sandbox-ai mcp tools NAME --repin [--project DIR]        pin the list NAME's relay saw last instead (refused
                                                         when the hidden-text checker finds something in it)

A run's MCP relay (runtime.McpGateway.vet_tools) compares every tools/list answer with the pin: a new or changed tool
(name, title, description, input or output schema, annotations) is asked about as an approval of kind "mcp-tools",
with the definitions before and after - a "rug pull" guard: a server that changes what its tools tell the model after
you trusted it. Allowed "always", the new list becomes the pin; "for the run", it holds until the run ends; denied (or
not answered), the new and changed tools are taken out of the list and their calls refused. A description in which the
hidden-text checker (lint.py) finds something is asked about too, also on the first list. A server without a pin has
its first list pinned after the run (trust on first use). After every run the list each server gave last is kept
beside its pin (NAME.seen.json: what `mcp tools` compares with the pin, and what --repin pins).

The pins live in sandbox-ai's state ($XDG_STATE_HOME/sandbox-ai/mcp-tools/PROJECT-HASH/NAME.json), never in a
sandbox. Standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

VERSION = 1
SERVER = re.compile(r"[A-Za-z0-9_-]{1,32}")
TOOLS_LIMIT = 500          # tools one pin keeps
DEFINITION_LIMIT = 4 << 10  # characters of one definition kept (runtime.McpToolPins.SHOWN)
FILE_LIMIT = 8 << 20
RECORDS_FILE = "mcp-tools.jsonl"  # in the run's control directory: what the run's relay saw first (McpToolPins.note)
DIGEST = re.compile(r"[0-9a-f]{64}")


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"


def project_key(project):
    """The store's folder name of PROJECT (its main workspace): NAME-HASH, as its log folder."""
    project = Path(project)
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", project.name or "root")[:60].lstrip(".") or "root"
    return f"{name}-{digest}"


def project_dir(project):
    return state_root() / "mcp-tools" / project_key(Path(project).expanduser().resolve())


def check_server(name):
    if not isinstance(name, str) or not SERVER.fullmatch(name):
        raise ValueError(f"not an MCP server name: {str(name)[:40]!r} (1-32 of A-Z a-z 0-9 _ -)")
    return name


def pin_path(project, server):
    return project_dir(project) / f"{check_server(server)}.json"


def read_json(path):
    """PATH's JSON object (never through a link, at most FILE_LIMIT bytes), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, FILE_LIMIT + 1)
    finally:
        os.close(fd)
    if len(data) > FILE_LIMIT:
        return None
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def clean_tools(tools):
    """{name: {"digest", "definition"}} of TOOLS as far as they are well formed."""
    found = {}
    for name, entry in (tools.items() if isinstance(tools, dict) else []):
        if len(found) >= TOOLS_LIMIT:
            break
        if not isinstance(name, str) or not 0 < len(name) <= 200 or not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            continue
        definition = entry.get("definition") if isinstance(entry.get("definition"), str) else ""
        found[name] = {"digest": digest, "definition": definition[:DEFINITION_LIMIT]}
    return found


def load(project, server):
    """The pin of SERVER for PROJECT ({"server", "pinned", "how", "tools": {name: {"digest", "definition"}}}), or
    None when there is none (or it cannot be read: then the next list is asked about as a first one)."""
    document = read_json(pin_path(project, server))
    if not document or document.get("version") != VERSION:
        return None
    return {"server": server, "pinned": str(document.get("pinned", "")), "how": str(document.get("how", "")),
            "tools": clean_tools(document.get("tools"))}


def relay_pin(project, server, lint=None):
    """What the run's relay of SERVER gets ("tools_pin" of its configuration): the pinned digests (None: no pin yet),
    the pinned definitions (what a question shows as "before") and LINT, the hidden-text checker's patterns as data
    (lint.pattern_data: the relay's runtime.HiddenText)."""
    pinned = load(project, server) if project else None
    return {"pins": {name: entry["digest"] for name, entry in pinned["tools"].items()} if pinned else None,
            "definitions": {name: entry["definition"] for name, entry in pinned["tools"].items()} if pinned else {},
            **({"lint": lint} if lint else {})}


def save(project, server, tools, how):
    """Pin TOOLS ({name: {"digest", "definition"}}) for SERVER of PROJECT; HOW: why ("first use", "allowed always
    (question ID)")."""
    directory = project_dir(project)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{directory}: not a folder of yours (a link?) - remove it")
    path = pin_path(project, server)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with open(fd, "w") as stream:
        json.dump({"version": VERSION, "server": server, "pinned": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "how": str(how)[:200], "project": str(Path(project).expanduser().resolve()),
                   "tools": clean_tools(tools)}, stream)
    os.replace(temporary, path)
    return path


def pin_answer(project, server, digests, definitions, question=""):
    """An "Allow always" of an mcp-tools question: the list it showed (DIGESTS {name: digest}) pinned, each with its
    definition from the question (DEFINITIONS) or, unchanged, from the pin before."""
    check_server(server)
    before = (load(project, server) or {}).get("tools", {})
    tools = {}
    for name, digest in (digests.items() if isinstance(digests, dict) else []):
        if not isinstance(digest, str):
            continue
        definition = definitions.get(name) if isinstance(definitions, dict) else None
        if not isinstance(definition, str):
            old = before.get(name)
            definition = old["definition"] if old and old["digest"] == digest else ""
        tools[name] = {"digest": digest, "definition": definition}
    return save(project, server, tools, f"allowed always (question {str(question)[:60]})")


def pin_after_run(project, control_dir):
    """After a run: each server whose first list the run's relay saw (control_dir/mcp-tools.jsonl, state "first")
    and that has no pin yet, pinned. Lines about what was pinned."""
    notes, path = [], Path(control_dir) / RECORDS_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return notes
    with open(fd, "rb") as stream:
        data = stream.read(FILE_LIMIT)
    done, last = set(), {}
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        server = entry.get("server") if isinstance(entry, dict) else None
        if not isinstance(server, str) or not SERVER.fullmatch(server):
            continue
        if entry.get("state") == "seen":
            last[server] = entry
            continue
        if entry.get("state") != "first" or server in done:
            continue
        done.add(server)
        if load(project, server) is not None:
            continue  # pinned meanwhile (an "always" answer, another run): that one holds
        tools = clean_tools(entry.get("tools"))
        if tools:
            save(project, server, tools, "first use")
            notes.append(f"MCP server {server}: its {len(tools)} tool definition(s) pinned for this project; a later "
                         f"change is asked about (sandbox-ai mcp tools {server})")
    for server, entry in last.items():  # what each server listed last: compared with the pin, --repin pins it
        tools = clean_tools(entry.get("tools"))
        if tools:
            save_seen(project, server, tools, entry.get("time") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return notes


def seen_path(project, server):
    return project_dir(project) / f"{check_server(server)}.seen.json"


def load_seen(project, server):
    """The list SERVER's relay saw last for PROJECT ({"server", "seen", "tools"}), or None."""
    document = read_json(seen_path(project, server))
    if not document or document.get("version") != VERSION:
        return None
    return {"server": server, "seen": str(document.get("seen", "")), "tools": clean_tools(document.get("tools"))}


def save_seen(project, server, tools, when):
    directory = project_dir(project)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{directory}: not a folder of yours (a link?) - remove it")
    path = seen_path(project, server)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with open(fd, "w") as stream:
        json.dump({"version": VERSION, "server": server, "seen": str(when)[:40], "tools": clean_tools(tools)}, stream)
    os.replace(temporary, path)
    return path


def changes(pinned, seen):
    """[{"tool", "change": new|changed|gone}] of the list SEEN against the pin PINNED (both {name: {"digest"}})."""
    found = [{"tool": name, "change": "new" if name not in pinned else "changed"}
             for name in sorted(seen) if (pinned.get(name) or {}).get("digest") != seen[name]["digest"]]
    return found + [{"tool": name, "change": "gone"} for name in sorted(pinned) if name not in seen]


def lint_findings(tools):
    """The hidden-text checker's lines about the definitions of TOOLS ({name: {"definition"}}): what the model
    would read in them (their JSON strings decoded)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_lint_for_pins", Path(__file__).resolve().parent / "lint.py")
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)

    def strings(value, found):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                found.append(str(key))
                strings(item, found)
        elif isinstance(value, list):
            for item in value:
                strings(item, found)
        return found
    lines = []
    for name, entry in sorted(tools.items()):
        try:
            texts = strings(json.loads(entry["definition"]), [])
        except (ValueError, RecursionError):
            texts = [entry["definition"]]
        for text in texts:
            lines += lint.lines(lint.check_text(text, f"tool {name[:100]}"), limit=5)
    return lines


def repin(project, server):
    """The list SERVER's relay saw last pinned for PROJECT (the human's choice: sandbox-ai mcp tools NAME --repin, the
    app's and the web's Re-pin). ValueError: none was seen, or the hidden-text checker finds something in it."""
    seen = load_seen(project, server)
    if seen is None or not seen["tools"]:
        raise ValueError(f"MCP server {server}: no list was seen for {project} yet (a run of it keeps the last one)")
    findings = lint_findings(seen["tools"])
    if findings:
        raise ValueError(f"MCP server {server}: the list seen last holds text that may hide instructions - not "
                         "pinned; allow it in its question (Allow always) if you trust it:\n  "
                         + "\n  ".join(printable(line) for line in findings[:20]))
    return save(project, server, seen["tools"], f"re-pinned by you (the list seen {seen['seen'][:40]})")


def forget(project, server):
    try:
        pin_path(project, server).unlink()
        return True
    except FileNotFoundError:
        return False


def listing(project):
    """[{"server", "pinned", "how", "tools"}] of PROJECT's pins."""
    directory = project_dir(project)
    found = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json") or not SERVER.fullmatch(name[:-5]):
            continue
        pinned = load(project, name[:-5])
        if pinned is not None:
            seen = load_seen(project, name[:-5])
            found.append({"server": pinned["server"], "pinned": pinned["pinned"], "how": pinned["how"],
                          "tools": len(pinned["tools"]), "seen": seen["seen"] if seen else None,
                          "differs": len(changes(pinned["tools"], seen["tools"])) if seen else None})
    return found


def every_listing():
    """[{"project", "server", ...}] of the pins of every project (listing's, with the project's folder as each pin
    names it; None for a pin made before sandbox-ai 0.1.133, which named none)."""
    found = []
    try:
        folders = sorted(path for path in (state_root() / "mcp-tools").iterdir() if path.is_dir())
    except OSError:
        return found
    for folder in folders:
        projects = set()
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json") and not name.endswith(".seen.json") and SERVER.fullmatch(name[:-5]):
                document = read_json(folder / name) or {}
                if isinstance(document.get("project"), str) and project_dir(document["project"]) == folder:
                    projects.add(document["project"])
        for project in sorted(projects):
            found += [{"project": project, **entry} for entry in listing(project)]
        if not projects:  # older pins: listed without their folder (forget or re-pin need --project DIR)
            for name in sorted(os.listdir(folder)):
                if name.endswith(".json") and not name.endswith(".seen.json") and SERVER.fullmatch(name[:-5]):
                    document = read_json(folder / name) or {}
                    if document.get("version") == VERSION:
                        found.append({"project": None, "server": name[:-5], "pinned": str(document.get("pinned", "")),
                                      "how": str(document.get("how", "")),
                                      "tools": len(clean_tools(document.get("tools"))), "seen": None,
                                      "differs": None})
    return found


def printable(text):
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}"
                   for c in str(text))


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai mcp tools", allow_abbrev=False,
                                description="The MCP tool definitions pinned for a project: a later change is asked "
                                            "about (approval kind mcp-tools).")
    p.add_argument("name", nargs="?", metavar="NAME", help="one server: its pinned tools with their definitions")
    p.add_argument("--project", metavar="DIR", default=".", help="the project (its main workspace; default: here)")
    p.add_argument("--forget", action="store_true", help="drop NAME's pin: its next list is pinned anew")
    p.add_argument("--repin", action="store_true", help="pin the list NAME's relay saw last instead (refused when "
                                                        "the hidden-text checker finds something in it)")
    p.add_argument("--all", action="store_true", help="the pins of every project, each with its folder")
    p.add_argument("--json", action="store_true", help="as JSON (for programs)")
    a = p.parse_args(argv)
    try:
        project = Path(a.project).expanduser().resolve()
        if a.forget:
            if not a.name:
                raise ValueError("--forget needs NAME")
            done = forget(project, a.name)
            out(json.dumps({"forgotten": done}) if a.json else
                f"MCP server {a.name}: pin dropped; its next list is pinned anew" if done else
                f"MCP server {a.name} has no pin for {project}")
            return 0
        if a.repin:
            if not a.name or a.forget:
                raise ValueError("--repin needs NAME (and no --forget)")
            repin(project, a.name)
            pinned = load(project, a.name)
            out(json.dumps({"pinned": True, "tools": len(pinned["tools"])}) if a.json else
                f"MCP server {a.name}: the list seen last ({len(pinned['tools'])} tool(s)) pinned for {project}")
            return 0
        if a.name:
            pinned = load(project, a.name)
            seen = load_seen(project, a.name)
            if a.json:
                out(json.dumps(None if pinned is None and seen is None else {
                    **(pinned or {"server": a.name, "pinned": None, "how": None, "tools": {}}), "seen": seen,
                    "changes": changes((pinned or {}).get("tools", {}), seen["tools"]) if seen else []}))
                return 0
            if pinned is None:
                out(f"MCP server {a.name}: nothing pinned for {project} (its next list is pinned after the run)")
                return 0
            out(f"MCP server {a.name}: {len(pinned['tools'])} tool(s) pinned {pinned['pinned']} "
                f"({printable(pinned['how'])})")
            for name, entry in sorted(pinned["tools"].items()):
                out(f"  {printable(name)}  sha256 {entry['digest'][:16]}")
                if entry["definition"]:
                    out(f"    {printable(entry['definition'][:400])}")
            differ = changes(pinned["tools"], seen["tools"]) if seen else []
            if differ:
                out(f"The list seen last ({seen['seen']}) differs: "
                    + ", ".join(f"{printable(c['tool'])} {c['change']}" for c in differ[:20])
                    + f" (pin it: sandbox-ai mcp tools {a.name} --repin)")
            return 0
        if a.all:
            found = every_listing()
            if a.json:
                out(json.dumps(found))
                return 0
            if not found:
                out("No MCP tool definitions pinned for any project yet.")
            for entry in found:
                out(f"{printable(entry['project'] or '(a project before 0.1.133)')}: {entry['server']}: "
                    f"{entry['tools']} tool(s), pinned {entry['pinned']} ({printable(entry['how'])})")
            return 0
        found = listing(project)
        if a.json:
            out(json.dumps(found))
            return 0
        if not found:
            out(f"No MCP tool definitions pinned for {project} yet (a server's first list is pinned after its run).")
        for entry in found:
            seen = (f"; last seen {entry['seen']}: {entry['differs']} differ" if entry["differs"] else
                    f"; last seen {entry['seen']}: as pinned") if entry["seen"] else ""
            out(f"{entry['server']}: {entry['tools']} tool(s), pinned {entry['pinned']} ({printable(entry['how'])})"
                + seen)
        return 0
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai mcp tools: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
