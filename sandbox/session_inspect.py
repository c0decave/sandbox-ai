#!/usr/bin/env python3
"""A start's agent memory, the agent's own session files, and the start's folder as one archive - read only.

  sandbox-ai session memory NAME [--file F | --all] [--from home|store] [--raw] [--show-secret] [--hide-secrets]
                                 [--json] [--host HOST]
  sandbox-ai session files NAME [--file F [--full]] [--raw] [--show-secret] [--hide-secrets] [--json] [--host HOST]
  sandbox-ai session files NAME --search PATTERN [--text TEXT] [--case] [--json] [--host HOST]
  sandbox-ai session export NAME OUT.tar.gz [--include-secrets [--yes]] [--json] [--host HOST]

NAME is a start `sandbox-ai list` shows: running, paused, or stopped with its HOME kept (a run that keeps no HOME
only while it runs).

Memory: the agent's memory file (kits.MEMORY_FILE: CLAUDE.md, AGENTS.md ...) and its auto memory (kits.MEMORY_DIRS:
Claude's MEMORY.md index and each memory file). While the start runs (or is paused), the live copy in its HOME; once
it stopped, what went back to the project's memory store (kits.memory_store: ~/.local/state/sandbox-ai/memory/
PROJECT-HASH/AGENT). The output says which one it is; --from chooses.

Session files: the agents' own transcripts in the start's HOME (SESSION_PLACES: each agent's folder, and whether the
place is verified against sandbox-ai's own readers of it). One is shown as a conversation - user, assistant, tool
call, tool result, with their times; long tool input and output shortened unless --full - or, with --raw, as it is.

Search (--search PATTERN, --text TEXT): the session files whose name - with a / in the pattern, whose path in the
HOME - matches PATTERN, as the desktop app's file manager searches (file_search.py): * any characters but /, ? one,
[abc] [a-z] [!abc] one of them or none, ** any number of folders (.claude/**/*.jsonl); a pattern without a wildcard
is a part of the name; letter case ignored unless --case; names compared in NFC. With --text, only those whose text
holds TEXT (a NUL byte makes a file binary, not looked into), each with the line it was found in (a part of it
around the text). Bounded: SEARCH_RESULTS results, SEARCH_FILE bytes read of one file (a larger one: only its start,
said), SEARCH_TOTAL bytes in all, SEARCH_SECONDS; every limit reached is said. A file marked as a login or key is
found by its name, never looked into. Over a host (--host) the search runs there: nothing but the results comes back.

Logins and keys: a file that looks like one - by its name (secret_name: home_review.SECRET, an agent's login file,
gh's, Docker's and Kubernetes' logins, Letta's provider keys) or, in the memory whose text is read anyway, by its
content (a private key, a placeholder of a secret the launcher gave) - is listed like every other one (name, size,
date) and marked ("secret": why). Its text is shown only when asked (--show-secret; the app and the web interface ask
first: show-secret-file). --hide-secrets, or "hide_secrets": true in your defaults (Settings: Hide logins and keys in
file views), leaves them out of the listing as before (the listing says how many).

Export: session.json, exit.json, supervisor.log and the kept HOME of a start, as a tar.gz written where you say (never
over a file that is there). An archive is made to be passed on: logins, keys and the files that hold a placeholder of
a secret the launcher gave are left out by default and listed as left out by name (in the output and in the
archive's export.json); --include-secrets takes them in (asked first: --yes where nothing can ask).

Everything in a HOME is written by the agent: untrusted. It is read through directory descriptors, every component
opened with O_NOFOLLOW (kits.open_dir, activity.open_file and regular_files), regular files only, within limits per
file, in total and in number; every text shown goes through printable() (no terminal escapes; the app and the web
show it as plain text). A file is named by its name in the listing, never by a free path. Standard library only.
"""
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import threading
import time

HERE = Path(__file__).resolve().parent

FILE_LIMIT = 16 << 20        # bytes of one memory file or transcript read to be shown (kits.MEMORY_BYTES)
TOTAL_LIMIT = 64 << 20       # bytes of memory text read for --all
LIST_LIMIT = 5000            # session files listed per place (activity.FILES_LIMIT)
LIST_TOTAL = 20000           # session files listed in all
MESSAGE_LIMIT = 50000        # messages of one conversation shown
SHORT = 2000                 # characters of a tool call's input or a tool's output shown unless --full
DEPTH_LIMIT = 8              # folders below a place looked into
EXPORT_FILE_LIMIT = 4 << 30  # one file of a HOME exported (larger ones are left out, and listed)
EXPORT_TOTAL = 64 << 30      # bytes exported in all (more: the export is refused)
EXPORT_FILES = 500_000       # files and folders exported in all (more: refused)
EXPORT_DEPTH = 64            # folders below the HOME exported
SCAN_BYTES = 1 << 20         # bytes of each file looked at for a private key or a placeholder of a secret
# The parts of a start's folder an export takes (sessions.py's layout); the rest (tmux sockets, the paused mark, a
# browser profile, the HOME's review records) is the launcher's own and stays.
EXPORTED = ("session.json", "exit.json", "supervisor.log")
SEARCH_RESULTS = 1000         # session files found by a search, at most
SEARCH_FILE = FILE_LIMIT      # bytes of one file looked into for --text
SEARCH_TOTAL = 256 << 20      # bytes looked into in one search
SEARCH_SECONDS = 60           # seconds a search may take
SEARCH_SHOWN = 200            # characters of the line a text was found in, around it
WILDCARDS = "*?["
# What the launcher puts in a sandbox in place of a secret (agent_sandbox.API_KEY_PLACEHOLDER, llm.PLACEHOLDER,
# hermes.OPENROUTER_PLACEHOLDER): a file holding one is left out of an export.
PLACEHOLDERS = (b"sandbox-ai-api-key-placeholder-", b"sandbox-ai-llm-key-placeholder", b"sk-or-v1-sandbox-ai-placeholder")
PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
# Folders below the HOME whose files are keys whatever their names (Letta's provider keys of its local backend).
SECRET_FOLDERS = ((".letta/lc-local-backend/providers/", "an agent's provider keys"),)
# What a secret file's mark says when its name gives it away (the app and the web interface translate the marks).
BY_NAME = "a login, token or key (by its name)"
HIDE_KEY = "hide_secrets"  # your defaults (defaults.json): true leaves logins and keys out of the file views

# Where each agent keeps its own sessions in its HOME: (agents, folder below the HOME, the files there (a regular
# expression on the path below that folder), format, verified). VERIFIED: sandbox-ai reads that place itself
# (activity.py's readers: Claude Code, Codex, OpenCode) or its review of the agent names it with its format (Pi:
# pi.STORAGE, JSONL). Not verified - the folder is the one the agent's review names, the files in it are taken as they
# look (JSON, JSONL, SQLite), their format is not known here, and they are shown as generic JSON: DeepSeek Harness
# (~/.dsh/sessions), nanobot (~/.nanobot/sessions: "the session database"), Letta Code (its local backend
# ~/.letta/lc-local-backend: "agents, memory and conversations"; its provider keys listed as keys), OpenHands (~/.openhands:
# "settings, conversations"), Hermes (~/.hermes), Qwen Code (~/.qwen) and Goose (~/.local/share/goose). The listing
# says so (verified: false).
SESSION_PLACES = (
    (("claude",), ".claude/projects", r"[^/]+/[^/]+\.jsonl|[^/]+/[^/]+/subagents/agent-[^/]+\.jsonl", "claude", True),
    (("codex",), ".codex/sessions", r"\d{4}/\d\d/\d\d/rollout-[^/]+\.jsonl", "codex", True),
    (("codex",), ".codex/archived_sessions", r"rollout-[^/]+\.jsonl", "codex", True),
    (("opencode", "opencode2"), ".local/share/opencode",
     r"opencode(?:-[A-Za-z0-9_.-]{1,40})?\.db|storage/session/[^/]+/[^/]+\.json", "opencode", True),
    (("pi",), ".pi/agent/sessions", r"(?:[^/]+/)?[^/]+\.jsonl", "jsonl", True),
    (("dsh",), ".dsh/sessions", r".+\.(?:jsonl?|db|sqlite3?|md|txt)", "auto", False),
    (("nanobot",), ".nanobot/sessions", r".+\.(?:jsonl?|db|sqlite3?)", "auto", False),
    (("letta",), ".letta/lc-local-backend", r"(?!memfs/).+\.(?:jsonl?|db|sqlite3?)", "auto", False),
    (("openhands",), ".openhands", r"(?:conversations|sessions|file_store)/.+\.(?:jsonl?|db)", "auto", False),
    (("hermes",), ".hermes", r"(?:sessions/.+\.(?:jsonl?|db)|[^/]+\.db|[^/]+\.jsonl)", "auto", False),
    (("qwen",), ".qwen", r"(?:tmp|projects)/[^/]+/(?:chats/)?.+\.jsonl?", "auto", False),
    (("goose",), ".local/share/goose", r"sessions/.+\.(?:jsonl?|db)", "auto", False),
)
ROLES = ("user", "assistant", "system", "reasoning", "tool-call", "tool-result", "event")

_modules = {}
_LOADING = threading.RLock()


def module(name):
    """sandbox-ai's NAME.py (starts, kits, activity, home_review, hosts, net, homepack, storage), loaded once by
    path."""
    with _LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_inspect", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped; lines and tabs kept."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def one_line(text):
    return printable(text).replace("\n", "\\n").replace("\t", " ")


def human(size):
    size = int(size or 0)
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if size >= 1 << shift:
            return f"{size / (1 << shift):.1f} {unit}"
    return f"{size} B"


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds)) if seconds else None


def shown_time(value):
    return str(value or "").replace("T", " ").replace("Z", "")[:19]


# --- the start --------------------------------------------------------------------------------------------------------

def locate(name, found=None):
    """What NAME is and where its HOME is: {"name", "id", "agent", "state", "kind", "home" (a folder of this machine,
    or None with "home_why"), "directory" (its start folder or None), "workspace", "memory" (its project memory is
    used), "store" (that memory's folder)}. FOUND: starts.entries() when read already (a listing of every start).
    ValueError: no such start, or one that has no agent's HOME."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError(f"not a start's name: {one_line(name)[:80]}")
    entry = module("starts").find(name, found)
    shown = one_line(entry.get("name") or name)
    if entry.get("kind") == "mcp":
        raise ValueError(f"{shown} is an MCP server's sandbox: it has no agent's memory or session files")
    if entry.get("state") == "damaged" or entry.get("agent") not in module("kits").MEMORY_FILE:
        raise ValueError(f"the record of {shown} is damaged: see sandbox-ai doctor")
    directory = Path(entry["directory"]) if entry.get("directory") else None
    data = {}
    if directory is not None:
        with contextlib.suppress(OSError, ValueError, RecursionError):
            loaded = json.loads((directory / "session.json").read_text())
            data = loaded if isinstance(loaded, dict) else {}
    home, why = None, None
    if entry.get("state") in ("running", "paused", "orphaned") and entry.get("run"):
        orphans = {}
        with contextlib.suppress(Exception):  # noqa: BLE001 - a record that cannot be read: its start folder's HOME
            records = module("net").running(orphans)
            record = records.get(entry["run"]) or orphans.get(entry["run"]) or {}
            if isinstance(record.get("agent_home"), str) and os.path.isabs(record["agent_home"]):
                home = record["agent_home"]
    if home is None and directory is not None and entry.get("keep"):
        home = str(directory / "home")
    if home is None:
        why = (f"{shown} ended without keeping its HOME" if entry.get("state") == "stopped" else
               f"{shown} keeps no HOME this machine can read")
    elif os.path.islink(home) or not os.path.isdir(home):
        packed = False
        with contextlib.suppress(Exception):  # noqa: BLE001 - only to say why
            packed = directory is not None and bool(module("homepack").packed(directory))
        why = (f"the HOME of {shown} is packed (sandbox-ai gc --pack); its next resume unpacks it" if packed else
               f"the HOME of {shown} is not on this machine" if entry.get("backend") not in (None, "", "podman") else
               f"the HOME of {shown} is gone")
        home = None
    options = data.get("options") if isinstance(data.get("options"), list) else []
    workspace = entry.get("workspace") if isinstance(entry.get("workspace"), str) else data.get("workspace")
    memory = "--no-memory" not in options and isinstance(workspace, str) and os.path.isabs(workspace)
    store = str(module("kits").memory_store(workspace, entry["agent"])) if memory else None
    return {"name": entry.get("name") or name, "id": entry.get("id"), "agent": entry["agent"],
            "state": entry.get("state"), "kind": entry.get("kind"), "home": home, "home_why": why,
            "directory": str(directory) if directory is not None else None, "workspace": workspace,
            "memory": memory, "store": store}


def header(found):
    return {key: found.get(key) for key in ("name", "id", "agent", "state")}


# --- memory -----------------------------------------------------------------------------------------------------------

def memory_source(found, choice="auto"):
    """("home" or "store", its folder) of the memory to show of the start FOUND (locate): CHOICE "auto" takes the live
    copy in the HOME while the start runs or is paused, else the project's memory store (the HOME's copy when the
    start uses none). ValueError: the one chosen is not there."""
    shown = one_line(found["name"])
    store = found["store"] if found["memory"] and found["store"] and os.path.isdir(found["store"]) else None
    if choice == "home" or (choice == "auto" and (found["state"] in ("running", "paused", "orphaned") or not store)):
        if found["home"] is None:
            if choice == "auto" and store:
                return "store", store
            raise ValueError(found["home_why"] or f"{shown} has no HOME here")
        return "home", found["home"]
    if choice in ("store", "auto"):
        if not found["memory"]:
            raise ValueError(f"{shown} runs without project memory (--no-memory): there is no memory store of it; "
                             "see its HOME's copy with --from home")
        if not store:
            raise ValueError(f"no memory store of {shown}'s project yet ({one_line(found['store'])}): it is made "
                             "when a start of the project ends")
        return "store", store
    raise ValueError(f"--from takes home or store, not {one_line(choice)}")


def memory_files(root, agent, with_text=False):
    """({name: {"name", "size", "modified"(, "data")}}, notes, complete) of AGENT's memory below ROOT (a HOME or a
    store), as kits.collect reads it: through directory descriptors, no link followed, at most kits.MEMORY_FILES
    files and MEMORY_BYTES in all."""
    kits = module("kits")
    found, notes, complete = kits.collect(root, agent)
    files = {}
    for relative, data in sorted(found.items()):
        parts = PurePosixPath(relative).parts
        modified = None
        with contextlib.suppress(OSError, ValueError):
            fd = kits.open_dir(root, parts[:-1])
            if fd is not None:
                try:
                    modified = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mtime
                finally:
                    os.close(fd)
        files[relative] = {"name": relative, "size": len(data), "modified": iso(modified),
                           "secret": secret_name(relative) or secret_content(data[:SCAN_BYTES]),
                           **({"data": data} if with_text else {})}
    return files, notes, complete


def text_of(data):
    """(text, binary) of the bytes DATA: UTF-8 (wrong bytes replaced); a NUL byte makes it binary (not shown)."""
    if b"\0" in data[:65536]:
        return None, True
    return data.decode("utf-8", "replace"), False


def memory(name, file=None, every=False, choice="auto", show_secret=False, hide_secrets=None):
    """The memory of start NAME: {"name", "id", "agent", "state", "source": "home"|"store", "path", "files": [{"name",
    "size", "modified", "secret"}], "notes", "complete", "hidden", "hide_secrets"}; with FILE (a name of its listing)
    or EVERY, each file's "text" too (None and "binary": true for a binary one). A file marked as a login or key
    ("secret": why) is listed; its text only with SHOW_SECRET (FILE: refused without; EVERY: left out, said in the
    notes). HIDE_SECRETS (None: your defaults, hide_secrets) leaves such files out ("hidden": how many). ValueError: no
    such start or file."""
    found = locate(name)
    source, root = memory_source(found, choice)
    files, notes, complete = memory_files(root, found["agent"], with_text=True)
    hide = hiding(hide_secrets)
    hidden = sum(1 for entry in files.values() if entry["secret"]) if hide else 0
    if hide:
        files = {key: entry for key, entry in files.items() if not entry["secret"]}
    if file is not None and file not in files:
        raise ValueError(f"not among the memory files of {one_line(found['name'])}: {one_line(file)[:200]} (see: "
                         f"sandbox-ai session memory {one_line(found['name'])})")
    if file is not None and files[file]["secret"] and not show_secret:
        refuse_secret(files[file], "memory")
    shown, total, withheld = [], 0, 0
    for key, entry in files.items():
        item = {k: v for k, v in entry.items() if k != "data"}
        if (every or key == file) and entry["secret"] and not show_secret:
            item["text"], item["binary"] = None, False
            withheld += 1
        elif (every or key == file) and total + entry["size"] <= TOTAL_LIMIT:
            item["text"], binary = text_of(entry["data"])
            item["binary"] = binary
            total += entry["size"]
        elif every or key == file:
            item["text"], item["binary"] = None, False
            notes.append(f"{key}: not shown, more than {human(TOTAL_LIMIT)} of memory in all")
        if file is None or key == file:
            shown.append(item)
    if withheld:
        notes.append(f"{withheld} file(s) marked as a login or key: not shown (add --show-secret to show them)")
    return {**header(found), "source": source, "path": root, "memory_file": module("kits").MEMORY_FILE[found["agent"]][1],
            "files": shown, "notes": notes, "complete": complete, "hidden": hidden, "hide_secrets": hide}


def memory_lines(result, file=None, every=False, raw=False):
    """The output of `session memory` (RESULT: memory()), as lines for a terminal."""
    if raw:
        return [printable(item.get("text") or "") for item in result["files"] if item.get("text") is not None]
    where = ("its memory as the agent has it now, in its HOME" if result["source"] == "home" else
             "what went back to the project's memory store")
    lines = [f"{one_line(result['name'])} ({one_line(result['agent'])}, {one_line(result['state'])}): {where}",
             f"  {one_line(result['path'])}"]
    if not result["files"]:
        lines.append(f"No memory files yet (its memory file is {one_line(result['memory_file'])}).")
    elif file is None and not every:
        width = max(len("NAME"), *(len(one_line(item["name"])) for item in result["files"]))
        lines.append(f"{'NAME'.ljust(width)}  {'SIZE':>9}  {'MODIFIED':<19}  LOGIN OR KEY")
        for item in result["files"]:
            lines.append(f"{one_line(item['name']).ljust(width)}  {human(item['size']):>9}  "
                         f"{shown_time(item['modified']):<19}  {one_line(item.get('secret') or '')}".rstrip())
    else:
        for item in result["files"]:
            lines += ["", f"== {one_line(item['name'])} ({human(item['size'])}, {shown_time(item['modified'])})"]
            if item.get("secret") and item.get("text") is not None:
                lines.append(f"warning: it holds a login or key ({one_line(item['secret'])}): do not pass this on")
            lines.append("(binary: not shown)" if item.get("binary") else
                         printable(item["text"]) if item.get("text") is not None else "(not shown)")
    lines += hidden_lines(result)
    lines += [f"note: {one_line(note)}" for note in result["notes"]]
    if not result["complete"]:
        lines.append("note: the memory is larger than what is read: not every file is listed")
    return lines


def hidden_lines(result):
    """The note of a listing that left logins and keys out (RESULT's "hidden")."""
    hidden = result.get("hidden") or 0
    if not hidden:
        return []
    return [f"note: {hidden} files that hold a login or key are not listed (--hide-secrets, or hide_secrets in your "
            "defaults: sandbox-ai defaults unset hide_secrets lists them)"]


# --- session files ----------------------------------------------------------------------------------------------------

def secret_path(relative):
    """Why the HOME path RELATIVE is a login or key (a name that looks like one: home_review.SECRET on the whole path,
    so a folder named so counts too; an agent's login file: agent_sandbox.AUTH_TARGETS, read from its source without
    loading the launcher; SECRET_FOLDERS), else None. What an export leaves out unless --include-secrets."""
    if relative in auth_targets():
        return "an agent's login"
    for folder, why in SECRET_FOLDERS:
        if relative.startswith(folder):
            return why
    if relative in LOGIN_FILES or module("home_review").SECRET.search(relative):
        return BY_NAME
    return None


def secret_name(relative):
    """Why the listed file RELATIVE (below a HOME or a memory store) is a login or key by its name, else None: as
    secret_path, but home_review.SECRET only on the file's own name - a project folder such as
    .claude/projects/-home-me-secret-app marks none of its transcripts."""
    if relative in auth_targets():
        return "an agent's login"
    for folder, why in SECRET_FOLDERS:
        if relative.startswith(folder):
            return why
    if relative in LOGIN_FILES or module("home_review").SECRET.search(relative.rsplit("/", 1)[-1]):
        return BY_NAME
    return None


def secret_content(head):
    """Why the bytes HEAD (a file's first SCAN_BYTES) hold a secret, else None: a private key, a placeholder of a
    secret the launcher gave."""
    if PRIVATE_KEY.search(head):
        return "holds a private key"
    if any(marker in head for marker in PLACEHOLDERS):
        return "holds a placeholder of a secret the launcher gave"
    return None


def hide_secrets_default():
    """"hide_secrets" of your defaults file (~/.config/sandbox-ai/defaults.json): true leaves logins and keys out of
    the file views. Anything else - no file, a broken one, another value - is false: they are listed."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(data, dict) and data.get(HIDE_KEY) is True


def hiding(choice):
    """True: logins and keys are left out of a listing - CHOICE True (--hide-secrets), or None and your defaults say
    so (hide_secrets_default)."""
    return bool(choice) or (choice is None and hide_secrets_default())


def refuse_secret(item, command):
    """ValueError for showing the text of ITEM (a listed file marked as a login or key) without --show-secret."""
    raise ValueError(f"{one_line(item['name'])[:200]} holds a login or key ({one_line(item['secret'])}): its text is "
                     f"shown only when asked - add --show-secret (sandbox-ai session {command} ... --show-secret)")


# Logins of tools that home_review.SECRET does not name by their path: GitHub's CLI, Docker's and Kubernetes' clients.
LOGIN_FILES = (".config/gh/hosts.yml", ".docker/config.json", ".kube/config")
_auth = []


def auth_targets():
    """The HOME paths of the agents' login files (agent_sandbox.AUTH_TARGETS), read from the launcher's source (it is
    large to load for a listing): every quoted ".../...json|yaml" value of that table."""
    if not _auth:
        found = set()
        with contextlib.suppress(OSError, ValueError):
            source = (HERE / "agent_sandbox.py").read_text()
            start = source.index("AUTH_TARGETS = {")
            table = source[start:source.index("\n# ", start)]
            found = set(re.findall(r'"\s*:\s*"(\.[^"]+)"', table))
        _auth.append(found or {".claude/.credentials.json", ".codex/auth.json"})
    return _auth[0]


def place_agent(agents, agent):
    return agent if agent in agents else agents[0]


def file_format(form, relative):
    if form != "auto":
        return "opencode-db" if form == "opencode" and relative.endswith(".db") else \
            "opencode-json" if form == "opencode" else form
    suffix = relative.rsplit(".", 1)[-1].lower() if "." in relative else ""
    return {"jsonl": "jsonl", "json": "json", "db": "database", "sqlite": "database", "sqlite3": "database"}.get(
        suffix, "text")


def session_listing(home, agent, hide=False):
    """([{"name": its path in the HOME, "agent", "size", "modified", "format", "verified", "secret"}], hidden) of the
    agents' session files in HOME, newest first; no link followed. A login or key by its name (secret_name) is listed
    and marked ("secret": why); with HIDE it is left out and counted (hidden). AGENT: the start's agent (OpenCode's
    place is named after it when it is OpenCode 2)."""
    activity = module("activity")
    found, hidden = [], 0
    for agents, folder, pattern, form, verified in SESSION_PLACES:
        if len(found) >= LIST_TOTAL:
            break
        accept = re.compile(pattern)
        parts = folder.split("/")
        # (a transcript sandbox-ai gc --compact-transcripts compressed in place, FILE.jsonl.gz, is read as FILE.jsonl)
        files = activity.regular_files(home, parts, lambda relative, a=accept: bool(
            a.fullmatch(relative) or (relative.endswith(".jsonl.gz") and a.fullmatch(relative[:-3]))),
            depth=DEPTH_LIMIT, limit=LIST_LIMIT)
        for relative, info in files.items():
            name = f"{folder}/{relative}"
            why = secret_name(name)
            if why and hide:
                hidden += 1
                continue
            packed = relative.endswith(".jsonl.gz")
            found.append({"name": name, "agent": place_agent(agents, agent), "size": info.st_size,
                          "modified": iso(info.st_mtime), "format": file_format(form, relative[:-3] if packed
                                                                                else relative),
                          "verified": verified, "secret": why, "compacted": packed})
    found.sort(key=lambda item: (item["modified"] or "", item["name"]), reverse=True)
    return found[:LIST_TOTAL], hidden


def read_head(home, relative, limit=None):
    """(bytes up to LIMIT (FILE_LIMIT), size) of the regular file HOME/RELATIVE, opened without following a link."""
    activity = module("activity")
    limit = FILE_LIMIT if limit is None else limit
    fd, info = activity.open_file(home, relative)
    try:
        if relative.endswith(".jsonl.gz"):  # compacted (storage.py): read as it was, its size the uncompressed one
            return module("storage").read_fd(fd, info, limit, relative)
        return activity.read_at(fd, 0, min(info.st_size, limit)), info.st_size
    finally:
        os.close(fd)


def short(text, full, limit=SHORT):
    """(TEXT, shortened) - cut to LIMIT characters unless FULL."""
    text = str(text)
    if full or len(text) <= limit:
        return text, False
    return text[:limit] + f"\n… ({len(text) - limit} more characters; --full shows all)", True


def compact(value):
    """VALUE as text: a string as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=1)
    except (TypeError, ValueError, RecursionError):
        return str(value)


def message(role, text, at=None, tool=None, error=False, full=False):
    shortened = False
    if role in ("tool-call", "tool-result", "event"):
        text, shortened = short(text, full)
    return {"role": role, "time": at if isinstance(at, str) else str(at) if isinstance(at, (int, float)) else None,
            "text": str(text), "tool": tool if isinstance(tool, str) else None, "error": bool(error),
            "shortened": shortened}


def block_text(content):
    """The text of CONTENT: a string, or a list of blocks with "text" (Claude's, Codex's)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text") for block in content[:500]
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def blocks(role, content, at, full):
    """The messages of one turn's CONTENT (a string or a list of blocks): text, thinking, tool calls and results, as
    Claude Code (text, thinking, tool_use, tool_result) and Pi-like formats (toolCall, toolResult) write them."""
    if isinstance(content, str):
        return [message(role, content, at, full=full)] if content.strip() else []
    found = []
    for block in content[:500] if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
            if block["text"].strip():
                found.append(message(role, block["text"], at, full=full))
        elif kind in ("thinking", "reasoning") and isinstance(block.get("thinking") or block.get("text"), str):
            found.append(message("reasoning", block.get("thinking") or block.get("text"), at, full=full))
        elif kind in ("tool_use", "toolCall", "tool_call", "function_call", "server_tool_use"):
            arguments = block.get("input", block.get("arguments", block.get("args")))
            found.append(message("tool-call", compact(arguments), at, tool=block.get("name"), full=full))
        elif kind in ("tool_result", "toolResult", "function_call_output", "web_search_tool_result"):
            output = block.get("content", block.get("output"))
            found.append(message("tool-result", block_text(output) or compact(output), at,
                                 error=block.get("is_error") or block.get("isError"), full=full))
        elif kind == "image":
            found.append(message(role, "[an image]", at, full=full))
    return found


def claude_messages(data, full):
    """One line of a Claude Code transcript: {"type": "user"|"assistant"|"system"|"summary"|..., "timestamp",
    "message": {"role", "content"}} (activity.ClaudeParser)."""
    kind, at = data.get("type"), data.get("timestamp")
    if kind in ("user", "assistant"):
        content = (data.get("message") or {}).get("content") if isinstance(data.get("message"), dict) else None
        return blocks(kind, content, at, full)
    if kind == "system" and isinstance(data.get("content"), str):
        return [message("system", data["content"], at, full=full)]
    if kind == "summary" and isinstance(data.get("summary"), str):
        return [message("event", f"summary: {data['summary']}", at, full=full)]
    return []


def codex_messages(data, full):
    """One line of a Codex rollout: {"timestamp", "type": "session_meta"|"response_item"|..., "payload"}
    (activity.CodexParser); the response items carry the conversation, the event messages repeat it."""
    kind, payload, at = data.get("type"), data.get("payload"), data.get("timestamp")
    if not isinstance(payload, dict):
        kind, payload = "response_item", data  # (rollouts from before the envelope)
    if kind == "session_meta":
        return [message("event", f"conversation in {payload.get('cwd') or '?'}"
                                 + (f" (codex {payload['cli_version']})" if isinstance(payload.get("cli_version"), str)
                                    else ""), at, full=full)]
    if kind != "response_item":
        return []
    item = payload.get("type")
    if item == "message":
        role = payload.get("role") if payload.get("role") in ("user", "assistant") else "system"
        text = block_text(payload.get("content"))
        return [message(role, text, at, full=full)] if text.strip() else []
    if item == "reasoning":
        text = block_text(payload.get("summary"))
        return [message("reasoning", text, at, full=full)] if text.strip() else []
    if item in ("function_call", "custom_tool_call"):
        return [message("tool-call", compact(payload.get("arguments", payload.get("input"))), at,
                        tool=payload.get("name"), full=full)]
    if item == "local_shell_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        command = action.get("command")
        return [message("tool-call", " ".join(map(str, command)) if isinstance(command, list) else compact(action),
                        at, tool="shell", full=full)]
    if item in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        if isinstance(output, dict):
            output = output.get("content", output)
        return [message("tool-result", block_text(output) or compact(output), at, full=full)]
    if item == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        return [message("tool-call", compact(action.get("query") or action), at, tool="web_search", full=full)]
    return []


TIME_KEYS = ("timestamp", "time", "created_at", "createdAt", "ts", "date")


def generic_messages(data, full):
    """One JSON object of an agent whose format is not known here: its "message" (or itself) with a role and a content
    (a string or blocks); anything else is shown as JSON."""
    at = next((data.get(key) for key in TIME_KEYS if isinstance(data.get(key), (str, int, float))), None)
    inner = data.get("message") if isinstance(data.get("message"), dict) else data
    role = inner.get("role") if isinstance(inner.get("role"), str) else data.get("role") or data.get("type")
    content = inner.get("content", inner.get("text", inner.get("parts")))
    if isinstance(role, str) and content is not None:
        role = role.lower()
        role = "tool-result" if role in ("tool", "toolresult", "tool_result", "function") else \
            role if role in ("user", "assistant", "system") else "assistant" if role in ("model", "ai", "agent") \
            else None
        if role is not None:
            found = blocks(role, content, at, full)
            if role == "tool-result":
                found = [dict(m, role="tool-result") for m in found] or \
                    [message("tool-result", compact(content), at, full=full)]
            if found:
                return found
    return [message("event", compact(data), at, full=full)]


def json_values(text):
    """The objects of a JSON file: its "messages" (or "history", "items", "events") when it has them, the elements of
    a list, else the object itself."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(value, dict):
        for key in ("messages", "history", "items", "events", "conversation"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return None


def opencode_messages(home, relative, full):
    """OpenCode's sessions as sandbox-ai's activity reader takes them (activity.OpenCodeSource: its database copied
    and read with SQLite's defensive settings, or its JSON storage), as messages; a storage session file: only that
    session's. Their texts are activity's (at most activity.TEXT_LIMIT characters each)."""
    activity = module("activity")
    source = activity.opencode_source(home, from_start=True)
    try:
        source.snapshot()
        events = source.poll([activity.POLL_BUDGET], time.time(), final=True)
    finally:
        source.close()
    wanted = PurePosixPath(relative).stem if "/storage/session/" in f"/{relative}" else None
    found = []
    roles = {"prompt": "user", "reply": "assistant", "tool": "tool-call", "command": "tool-call", "mcp": "tool-call",
             "file": "tool-call", "approval": "event", "session": "event", "tokens": "event"}
    for event in sorted((e for e in events if isinstance(e, dict)), key=lambda e: str(e.get("time") or "")):
        if wanted is not None and event.get("conversation") != wanted:
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        role = roles.get(event.get("kind"), "event")
        text = details.get("text") if role in ("user", "assistant") and isinstance(details.get("text"), str) else \
            event.get("summary")
        found.append(message(role, text or "", activity.stamp(event["time"]) if isinstance(event.get("time"), (int, float))
                             else event.get("time"), tool=details.get("tool"),
                             error=details.get("status") in ("error", "denied"), full=full))
    return found


def conversation(home, item, full=False):
    """(messages, notes) of the session file ITEM (session_listing's) in HOME."""
    form, relative = item["format"], item["name"]
    if form in ("opencode-db", "opencode-json"):
        return opencode_messages(home, relative, full), \
            ["OpenCode's sessions as sandbox-ai's activity reads them: texts at most 2000 characters, tool results as "
             "their status"]
    if form == "database":
        return [], [f"{relative} is a database: not shown as a conversation (it is part of the export)"]
    data, size = read_head(home, relative)
    notes = [f"only its first {human(FILE_LIMIT)} of {human(size)} are shown (--raw shows all)"] if size > len(data) \
        else []
    if form == "text":
        return [message("event", data.decode("utf-8", "replace"), full=True)], notes
    parse = {"claude": claude_messages, "codex": codex_messages}.get(form, generic_messages)
    if form == "json":
        values = json_values(data.decode("utf-8", "replace"))
        if values is None:
            return [message("event", data.decode("utf-8", "replace"), full=full)], \
                notes + ["not JSON as a whole: shown as text"]
    else:
        values, broken = [], 0
        if size > len(data):
            data = data[:data.rfind(b"\n") + 1]  # (the line cut at the limit is not one to parse)
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            value = module("activity").json_object(line)
            if value is None:
                broken += 1
            else:
                values.append(value)
        if broken:
            notes.append(f"{broken} line(s) are no JSON object (damaged, cut, or nested too deep): left out (--raw "
                         "shows them)")
    found = []
    for value in values:
        try:
            found += parse(value, full)
        except (AttributeError, TypeError, ValueError, RecursionError):
            found.append(message("event", "(a record of another shape: left out)", full=full))
        if len(found) >= MESSAGE_LIMIT:
            notes.append(f"only its first {MESSAGE_LIMIT} messages are shown")
            break
    return found[:MESSAGE_LIMIT], notes


def session_home(name):
    found = locate(name)
    if found["home"] is None:
        raise ValueError(found["home_why"])
    return found


def pick(listing, file, shown):
    for item in listing:
        if item["name"] == file:
            return item
    raise ValueError(f"not among the session files of {one_line(shown)}: {one_line(file)[:200]} (see: sandbox-ai "
                     f"session files {one_line(shown)})")


def session_files(name, file=None, full=False, show_secret=False, hide_secrets=None):
    """The session files of start NAME: {"name", "id", "agent", "state", "home", "files": [...], "hidden",
    "hide_secrets"}; with FILE (a name of that list) "file": the item and "messages" (conversation()) and "notes". A
    file marked as a login or key is shown only with SHOW_SECRET; HIDE_SECRETS (None: your defaults) leaves such files
    out of the list."""
    found = session_home(name)
    hide = hiding(hide_secrets)
    listing, hidden = session_listing(found["home"], found["agent"], hide)
    result = {**header(found), "home": found["home"], "files": listing, "hidden": hidden, "hide_secrets": hide,
              "unverified": sorted({folder for agents, folder, _, _, verified in SESSION_PLACES if not verified
                                    and any(item["name"].startswith(folder + "/") for item in listing)})}
    if file is not None:
        item = pick(listing, file, found["name"])
        if item["secret"] and not show_secret:
            refuse_secret(item, "files")
        messages, notes = conversation(found["home"], item, full)
        result.update(file=item, messages=messages, notes=notes, files=[item])
    return result


# --- search: the app's file manager's patterns (file_search.Matcher), over the session files -----------------------

def glob_regex(pattern):
    """The regular expression of the glob PATTERN: * and ? never cross a /, ** does ("**/" also none) - the desktop
    app's file_search.translate, so that a pattern finds the same in both."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i >= 2:
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    j += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
            i = j
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:  # (no ]: a [ as it is)
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j]
                negate = body[:1] in ("!", "^")
                body = body[1:] if negate else body
                body = body.replace("\\", "\\\\").replace("[", "\\[").replace("&&", "&\\&").replace("~~", "~\\~") \
                    .replace("||", "|\\|")
                out.append(f"[^/{body}]" if negate else f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class Matcher:
    """PATTERN as the app's file manager takes it (file_search.Matcher, for files): a part of the name without a
    wildcard, a glob of the name (or with a /, of the path); empty or ** every file; CASE: letter case counts."""

    def __init__(self, pattern, case=False):
        import unicodedata
        self.normal = lambda text: unicodedata.normalize("NFC", text)
        text = self.normal(str(pattern or "").strip())
        self.case, self.part, self.regex = case, None, None
        self.folders_only = text.endswith("/") and text.strip("/") != ""
        text = text.strip("/") if "/" in text else text
        self.path = "/" in text
        if not text or text == "**":
            return
        if not self.path and not any(c in text for c in WILDCARDS):
            self.part = text if case else text.casefold()
            return
        try:
            self.regex = re.compile(glob_regex(text), 0 if case else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"not a pattern: {one_line(pattern)} ({exc})") from None

    def match(self, relative):
        if self.folders_only:
            return False  # (session files are files)
        name = relative.rsplit("/", 1)[-1]
        if self.part is not None:
            name = self.normal(name)
            return self.part in (name if self.case else name.casefold())
        if self.regex is None:
            return True
        return self.regex.fullmatch(self.normal(relative if self.path else name)) is not None


def excerpt(line, at, width=SEARCH_SHOWN):
    """A part of LINE around the column AT, at most WIDTH characters, printable; "…" where it was cut."""
    start = max(0, at - width // 3)
    shown = line[start:start + width]
    return printable(("…" if start else "") + shown.strip() + ("…" if start + width < len(line) else ""))


def search(name, pattern="", text=None, case=False, hide_secrets=None, clock=time.monotonic):
    """The session files of start NAME that match PATTERN (Matcher) and, with TEXT, hold it: session_files()'s
    header with "files": the files found, each with "line" and "excerpt" where TEXT was found, and "search": {"pattern",
    "text", "case", "looked_into", "binary", "partly" (larger than SEARCH_FILE: only their start looked into),
    "secret" (logins and keys: never looked into), "unreadable": [name: why], "limits": the limits reached, "seconds"}."""
    found = session_home(name)
    hide = hiding(hide_secrets)
    listing, hidden = session_listing(found["home"], found["agent"], hide)
    matcher = Matcher(pattern, case)
    needle = None if text in (None, "") else matcher.normal(str(text) if case else str(text).casefold())
    began = clock()
    report = {"pattern": str(pattern or ""), "text": text, "case": bool(case), "looked_into": 0, "binary": 0,
              "partly": 0, "secret": 0, "unreadable": [], "limits": []}
    hits, total = [], 0
    for item in listing:
        if not matcher.match(item["name"]):
            continue
        if len(hits) >= SEARCH_RESULTS:
            report["limits"].append(f"only the first {SEARCH_RESULTS} files found are listed")
            break
        if needle is None:
            hits.append(item)
            continue
        if item["secret"]:
            report["secret"] += 1  # (found by its name only: its text is a login's or a key's)
            continue
        if clock() - began > SEARCH_SECONDS:
            report["limits"].append(f"stopped after {SEARCH_SECONDS} s: the files after it were not looked into")
            break
        if total >= SEARCH_TOTAL:
            report["limits"].append(f"stopped after {human(SEARCH_TOTAL)} read: the files after it were not looked "
                                    "into")
            break
        try:
            data, size = read_head(found["home"], item["name"], min(SEARCH_FILE, SEARCH_TOTAL - total))
        except (OSError, ValueError) as exc:
            report["unreadable"].append(f"{one_line(item['name'])}: {one_line(getattr(exc, 'strerror', None) or exc)}")
            continue
        total += len(data)
        report["looked_into"] += 1
        if size > len(data):
            report["partly"] += 1
        if b"\0" in data:
            report["binary"] += 1
            continue
        hay_text = data.decode("utf-8", "replace")
        hay = matcher.normal(hay_text if case else hay_text.casefold())
        at = hay.find(needle)
        if at < 0:
            continue
        number = hay.count("\n", 0, at) + 1  # (case folding keeps the lines: a line's text is the file's own)
        line = hay_text.split("\n", number)[number - 1]
        column = matcher.normal(line if case else line.casefold()).find(needle)
        hits.append({**item, "line": number, "excerpt": excerpt(line, max(column, 0))})
    report["seconds"] = round(clock() - began, 2)
    return {**header(found), "home": found["home"], "files": hits, "hidden": hidden, "hide_secrets": hide,
            "search": report}


def search_lines(result):
    """`session files --search` (RESULT: search()) as lines for a terminal."""
    report = result["search"]
    what = f"named {one_line(report['pattern'])!r}" if report["pattern"] else "of every name"
    if report.get("text"):
        what += f" that hold {one_line(report['text'])!r}"
    lines = [f"{one_line(result['name'])}: the session files {what}: {len(result['files'])} found"]
    for item in result["files"]:
        lines.append(f"  {one_line(item['name'])}" + (f":{item['line']}: {item['excerpt']}" if "line" in item else
                                                      f"  ({human(item['size'])}, {shown_time(item['modified'])})"))
    if report.get("text"):
        lines.append(f"looked into {report['looked_into']} file(s)"
                     + (f"; {report['binary']} binary, not looked into" if report["binary"] else "")
                     + (f"; {report['partly']} larger than {human(SEARCH_FILE)}: only their start" if report["partly"]
                        else "")
                     + (f"; {report['secret']} marked as a login or key, found by name only" if report["secret"]
                        else ""))
    lines += [f"not read: {entry}" for entry in report["unreadable"]]
    lines += [f"note: {entry}" for entry in report["limits"]]
    return lines + hidden_lines(result)


def raw_file(name, file, out=None, show_secret=False, hide_secrets=None):
    """--raw: the session file FILE of start NAME as it is, to OUT (a binary stream; standard output). On a terminal
    every non-printable character is escaped; into a pipe or file, the bytes as they are. A login or key only with
    SHOW_SECRET (and not when HIDE_SECRETS - None: your defaults - leaves it out of the list)."""
    found = session_home(name)
    item = pick(session_listing(found["home"], found["agent"], hiding(hide_secrets))[0], file, found["name"])
    if item["secret"] and not show_secret:
        refuse_secret(item, "files")
    activity = module("activity")
    stream = out or sys.stdout.buffer
    terminal = out is None and sys.stdout.isatty()
    fd, info = activity.open_file(found["home"], file)
    try:
        if file.endswith(".jsonl.gz"):  # compacted (storage.py): as it was
            module("storage").copy_fd(fd, file, lambda chunk: stream.write(
                printable(chunk.decode("utf-8", "replace")).encode() if terminal else chunk))
            stream.flush()
            return 0
        offset = 0
        while offset < info.st_size:
            chunk = activity.read_at(fd, offset, min(1 << 20, info.st_size - offset))
            if not chunk:
                break
            offset += len(chunk)
            stream.write(printable(chunk.decode("utf-8", "replace")).encode() if terminal else chunk)
    finally:
        os.close(fd)
    stream.flush()
    return 0


ROLE_TITLES = {"user": "user", "assistant": "assistant", "system": "system", "reasoning": "reasoning",
               "tool-call": "tool call", "tool-result": "tool result", "event": "event"}


def files_lines(result):
    """The output of `session files` (RESULT: session_files()), as lines for a terminal."""
    shown = one_line(result["name"])
    if "messages" not in result:
        lines = [f"{shown} ({one_line(result['agent'])}, {one_line(result['state'])}): the agents' session files in "
                 f"its HOME", f"  {one_line(result['home'])}"]
        if not result["files"]:
            return lines + ["No session files yet.", *hidden_lines(result)]
        width = max(len("NAME"), *(len(one_line(item["name"])) for item in result["files"]))
        lines.append(f"{'NAME'.ljust(width)}  {'AGENT':<9}  {'SIZE':>9}  {'MODIFIED':<19}  LOGIN OR KEY")
        for item in result["files"]:
            lines.append(f"{one_line(item['name']).ljust(width)}  {one_line(item['agent']):<9}  "
                         f"{human(item['size']):>9}  {shown_time(item['modified']):<19}  "
                         f"{one_line(item.get('secret') or '')}".rstrip())
        lines += hidden_lines(result)
        for folder in result.get("unverified") or []:
            lines.append(f"note: {one_line(folder)}: where this agent keeps its sessions is not verified against its "
                         "source; its files are listed as they look")
        return lines
    item = result["file"]
    lines = [f"{shown}: {one_line(item['name'])} ({one_line(item['agent'])}, {human(item['size'])}, "
             f"{shown_time(item['modified'])})"]
    if item.get("secret"):
        lines.append(f"warning: it holds a login or key ({one_line(item['secret'])}): do not pass this on")
    for entry in result["messages"]:
        tool = f": {one_line(entry['tool'])}" if entry.get("tool") else ""
        error = " (error)" if entry.get("error") else ""
        lines += ["", f"== {shown_time(entry.get('time')) or '-'}  {ROLE_TITLES.get(entry['role'], entry['role'])}"
                      f"{tool}{error}", printable(entry["text"])]
    if not result["messages"]:
        lines.append("(nothing to show as a conversation)")
    lines += [f"note: {one_line(note)}" for note in result.get("notes") or []]
    return lines


# --- export -----------------------------------------------------------------------------------------------------------

class ExportRefused(ValueError):
    pass


def check_out(out, found):
    """OUT as an absolute path for the archive: not there yet, its folder there, never inside the start's folder or
    its HOME (the archive would hold itself)."""
    if not isinstance(out, str) or not out or "\0" in out or "\n" in out:
        raise ValueError("name the archive to write: a path such as ~/export.tar.gz")
    path = Path(os.path.abspath(os.path.expanduser(out)))
    if os.path.lexists(path):
        raise ValueError(f"{one_line(path)} is there already: name a new file (nothing is overwritten)")
    folder = path.parent
    if not folder.is_dir():
        raise ValueError(f"no folder {one_line(folder)} to write {one_line(path.name)} into")
    real = Path(os.path.realpath(folder))
    for inside in (found.get("directory"), found.get("home")):
        if inside and (real == Path(os.path.realpath(inside)) or Path(os.path.realpath(inside)) in real.parents):
            raise ValueError(f"{one_line(path)} lies in the folder that is exported: write it elsewhere")
    return path


def tar_info(name, info, kind=tarfile.REGTYPE):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.mode = stat.S_IMODE(info.st_mode) & 0o777
    member.mtime = int(info.st_mtime)
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.size = info.st_size if kind == tarfile.REGTYPE else 0
    return member


CANARY = "a decoy (canary) of this machine: never exported, the sandbox there gets its own"


class Exporter:
    """One export: the members written into TAR, what was left out and why, the counts against the limits. A login,
    key or a file holding a placeholder of a secret is left out ("secret": true) unless INCLUDE_SECRETS."""

    def __init__(self, tar, prefix, include_secrets=False, never=()):
        self.tar, self.prefix, self.include_secrets = tar, prefix, include_secrets
        self.left_out, self.files, self.bytes, self.entries = [], 0, 0, 0
        self.members = {}  # {member name: [size, sha256]} of every file written: the archive's manifest.json
        self.never = set(never)  # HOME paths left out whatever is asked (the canaries planted there: decoys of here)
        self.where = ""  # what is exported now ("" the HOME, "workspace/", ...): the paths of left_out are named so

    def leave(self, relative, why, secret=False):
        if len(self.left_out) < 5000:
            self.left_out.append({"path": self.where + relative, "why": why, **({"secret": True} if secret else {})})
        elif len(self.left_out) == 5000:
            self.left_out.append({"path": "…", "why": "further entries left out without a note"})

    def secret(self, relative):
        """Why RELATIVE is left out as a login or key by its path (secret_path), unless secrets are taken in."""
        return None if self.include_secrets else secret_path(relative)

    def canary(self, relative):
        """A canary planted in the HOME (NEVER): left out always (a decoy of this machine), said as such."""
        if not self.where and relative in self.never:
            self.leave(relative, CANARY)
            return True
        return False

    def add_bytes(self, member_name, data, mode=0o600):
        """DATA (bytes made here: a record of the export) as the file MEMBER_NAME."""
        self.count(len(data))
        member = tarfile.TarInfo(member_name)
        member.size, member.mtime, member.mode = len(data), int(time.time()), mode
        self.tar.addfile(member, io.BytesIO(data))
        self.members[member_name] = [len(data), hashlib.sha256(data).hexdigest()]

    def count(self, size=0):
        self.entries += 1
        self.bytes += size
        if self.entries > EXPORT_FILES:
            raise ExportRefused(f"more than {EXPORT_FILES} files and folders: too many to export")
        if self.bytes > EXPORT_TOTAL:
            raise ExportRefused(f"more than {human(EXPORT_TOTAL)}: too large to export")

    def secret_content(self, fd, size):
        os.lseek(fd, 0, os.SEEK_SET)
        head = os.read(fd, min(size, SCAN_BYTES))
        os.lseek(fd, 0, os.SEEK_SET)
        return secret_content(head)

    def add_file(self, directory_fd, name, member_name, relative, checks=True):
        """Add the regular file NAME of DIRECTORY_FD; anything else is left out with why."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
        except OSError as exc:
            self.leave(relative, "a link: not followed" if exc.errno == errno.ELOOP else
                       f"not readable ({exc.strerror or exc})")
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self.leave(relative, "not a regular file")
                return
            if info.st_size > EXPORT_FILE_LIMIT:
                self.leave(relative, f"larger than {human(EXPORT_FILE_LIMIT)}")
                return
            os.set_blocking(fd, True)
            why = self.secret_content(fd, info.st_size) if checks and not self.include_secrets else None
            if why:
                self.leave(relative, why, secret=True)
                return
            self.count(info.st_size)
            member = tar_info(member_name, info)
            with os.fdopen(os.dup(fd), "rb") as stream:
                bounded = Bounded(stream, info.st_size)
                self.tar.addfile(member, bounded)
            self.members[member_name] = [info.st_size, bounded.digest.hexdigest()]
            self.files += 1
        finally:
            os.close(fd)

    def add_tree(self, directory_fd, member_name, relative, depth=0):
        info = os.fstat(directory_fd)
        self.count()
        self.tar.addfile(tar_info(member_name, info, tarfile.DIRTYPE))
        try:
            with os.scandir(directory_fd) as listed:
                children = sorted(listed, key=lambda entry: entry.name)
        except OSError as exc:
            self.leave(relative or ".", f"not readable ({exc.strerror or exc})")
            return
        for entry in children:
            child = f"{relative}/{entry.name}" if relative else entry.name
            member = f"{member_name}/{entry.name}"
            try:
                kind = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                continue
            if self.canary(child):
                continue
            if stat.S_ISLNK(kind):
                self.leave(child, "a link: not followed")
            elif stat.S_ISDIR(kind):
                if self.secret(child):
                    self.leave(child, self.secret(child), secret=True)
                elif depth >= EXPORT_DEPTH:
                    self.leave(child, "nested too deep")
                else:
                    try:
                        fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=directory_fd)
                    except OSError as exc:
                        self.leave(child, f"not readable ({exc.strerror or exc})")
                        continue
                    try:
                        self.add_tree(fd, member, child, depth + 1)
                    finally:
                        os.close(fd)
            elif stat.S_ISREG(kind):
                why = self.secret(child)
                if why:
                    self.leave(child, why, secret=True)
                else:
                    self.add_file(directory_fd, entry.name, member, child)
            else:
                self.leave(child, "not a regular file (a socket, FIFO or device)")


class Bounded(io.RawIOBase):
    """At most SIZE bytes of STREAM, padded with zero bytes when it shrank meanwhile (tarfile wants exactly the size
    it was told)."""

    def __init__(self, stream, size):
        super().__init__()
        self.stream, self.left = stream, size
        self.digest = hashlib.sha256()  # of the bytes written into the archive (the manifest's checksum)

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.left <= 0:
            return 0
        wanted = min(len(buffer), self.left)
        data = self.stream.read(wanted) or b"\0" * wanted
        buffer[:len(data)] = data
        self.digest.update(data)
        self.left -= len(data)
        return len(data)


# The archive's own record of what it holds (written last): its schema and version, and every file with its size and
# SHA-256, which `sandbox-ai session import` checks (session_state.py). Version 1: the export.json of 0.1.190 and before,
# without a manifest.
ARCHIVE_SCHEMA = "sandbox-ai-session-archive"
ARCHIVE_VERSION = 2
MANIFEST = "manifest.json"


def canaries_of(home):
    """The HOME paths of the canaries (decoy logins) planted in HOME (canary.py's record of it): never exported - the
    sandbox at the other end gets its own."""
    with contextlib.suppress(Exception):  # noqa: BLE001 - no record, a damaged one: nothing to leave out
        record = json.loads(module("canary").home_state(home).read_text())
        return {str(item["path"]) for item in record.get("canaries") or [] if isinstance(item, dict)
                and isinstance(item.get("path"), str)}
    return set()


def export(name, out, include_secrets=False, extra=None):
    """Write session.json, exit.json, supervisor.log and the HOME of the kept start NAME into the new tar.gz OUT:
    {"name", "id", "agent", "state", "path", "files", "bytes", "left_out": [{"path", "why"(, "secret")}],
    "secrets_left_out": [path], "include_secrets", "parts"}. Members are plain relative names below the start's id;
    links, sockets and FIFOs are left out, and so are logins, keys and files holding a placeholder of a secret unless
    INCLUDE_SECRETS (the human's choice, asked first by every caller), and the canaries planted in the HOME always.
    EXTRA(exporter, prefix, found): adds the further parts of a session's state (session_state.py: its memory,
    checkpoints, workspace, network approvals, start plan) and returns what the manifest says of them ("parts").
    The last member is manifest.json: ARCHIVE_SCHEMA, ARCHIVE_VERSION and every file's size and SHA-256. Written
    beside OUT first, then put in place without replacing anything."""
    found = locate(name)
    shown = one_line(found["name"])
    if found["directory"] is None:
        raise ValueError(f"{shown} keeps no start folder to export: only a kept start (sandbox-ai AGENT --keep, a "
                         "session) has one")
    path = check_out(out, found)
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", str(found["id"]))[:48] or "start"
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    exporter, parts = None, {}
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            exporter = Exporter(tar, prefix, include_secrets, never=canaries_of(found["home"]) if found["home"] else ())
            directory = os.open(found["directory"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                exporter.count()
                tar.addfile(tar_info(prefix, os.fstat(directory), tarfile.DIRTYPE))
                for part in EXPORTED:
                    if os.path.lexists(os.path.join(found["directory"], part)):
                        exporter.add_file(directory, part, f"{prefix}/{part}", part, checks=False)
                if found["home"] is None:
                    exporter.leave("home", found["home_why"] or "no HOME")
                else:
                    try:
                        home = os.open(found["home"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    except OSError as exc:
                        exporter.leave("home", f"not readable ({exc.strerror or exc})")
                    else:
                        try:
                            exporter.add_tree(home, f"{prefix}/home", "")
                        finally:
                            os.close(home)
            finally:
                os.close(directory)
            if extra is not None:
                parts = extra(exporter, prefix, found) or {}
                exporter.where = ""
            report = {**header(found), "exported": iso(time.time()), "files": exporter.files,
                      "bytes": exporter.bytes, "left_out": exporter.left_out, "include_secrets": bool(include_secrets)}
            exporter.add_bytes(f"{prefix}/export.json", json.dumps(report, indent=2).encode())
            manifest = {"schema": ARCHIVE_SCHEMA, "version": ARCHIVE_VERSION, "sandbox_ai": launcher_version(),
                        "created": iso(time.time()), "prefix": prefix, **header(found),
                        "workspace": found.get("workspace"), "include_secrets": bool(include_secrets),
                        "parts": parts, "members": exporter.members}
            data = json.dumps(manifest, indent=1).encode()
            member = tarfile.TarInfo(f"{prefix}/{MANIFEST}")
            member.size, member.mtime, member.mode = len(data), int(time.time()), 0o600
            tar.addfile(member, io.BytesIO(data))
        os.link(temporary, path)  # (never over a file that came meanwhile)
    except FileExistsError:
        raise ValueError(f"{one_line(path)} came meanwhile: nothing is overwritten") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    return {**header(found), "path": str(path), "files": exporter.files, "bytes": exporter.bytes,
            "left_out": exporter.left_out, "include_secrets": bool(include_secrets),
            "secrets_left_out": [item["path"] for item in exporter.left_out if item.get("secret")], "parts": parts}


def launcher_version():
    """sandbox-ai's version, read from the launcher's source (not loaded for it)."""
    with contextlib.suppress(OSError):
        found = re.search(r'^__version__ = "([^"]+)"', (HERE / "agent_sandbox.py").read_text(), re.M)
        if found:
            return found.group(1)
    return None


def export_lines(result):
    lines = [f"{one_line(result['name'])}: {result['files']} files ({human(result['bytes'])}) written to "
             f"{one_line(result['path'])}"]
    secrets = [item for item in result["left_out"] if item.get("secret")]
    others = [item for item in result["left_out"] if not item.get("secret")]
    if secrets:
        lines.append(f"logins, keys and secrets left out ({len(secrets)}) - the archive is made to be passed on:")
        lines += [f"  {one_line(item['path'])}: {one_line(item['why'])}" for item in secrets[:200]]
        if len(secrets) > 200:
            lines.append(f"  … and {len(secrets) - 200} more (the archive's export.json lists them)")
        lines.append("  to take them in (they can log in as you wherever the archive goes): sandbox-ai session export "
                     f"{one_line(result['name'])} NEW.tar.gz --include-secrets")
    elif result.get("include_secrets"):
        lines.append("warning: logins, keys and secrets are in it (--include-secrets): whoever has the archive can "
                     "log in as you - pass it on to no one you would not give them to")
    if others:
        lines.append(f"left out ({len(others)}):")
        lines += [f"  {one_line(item['path'])}: {one_line(item['why'])}" for item in others[:200]]
        if len(others) > 200:
            lines.append(f"  … and {len(others) - 200} more (the archive's export.json lists them)")
    return lines


# --- the command line (sessions.py's `session memory|files|export`) ---------------------------------------------------

def remote(host, args, timeout=300):
    """The JSON `sandbox-ai session ARGS --json` prints on HOST (over SSH); ValueError with what it said."""
    status, out, err = module("hosts").run(host, ["session", args[0], "--json", *args[1:]], timeout=timeout)
    if status != 0:
        text = printable((err or out).strip()[-600:])
        if "invalid choice" in text:
            text = (f"the sandbox-ai on {host} is older and has no `session {args[0]}`: update it (sandbox-ai hosts "
                    f"update {host})")
        elif "unrecognized arguments" in text:
            text = (f"the sandbox-ai on {host} is older and does not know {text.rpartition(':')[2].strip()} of "
                    f"`session {args[0]}`: update it (sandbox-ai hosts update {host})")
        raise ValueError(f"{one_line(host)}: {text.removeprefix('sandbox-ai session: ') or f'exit status {status}'}")
    try:
        data = json.loads(out)
    except (ValueError, RecursionError):
        raise ValueError(f"{one_line(host)}: sandbox-ai session {args[0]} printed no JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"{one_line(host)}: not what sandbox-ai session {args[0]} says")
    return data


def local_host(host):
    return host in (None, "", "local")


def remote_view(host, args, hide):
    """remote() of a file view, with --hide-secrets when HIDE (yours: --hide-secrets or your defaults here; that host's
    own defaults may hide them too). An older sandbox-ai there knows no --hide-secrets - it never lists logins and
    keys at all -: asked again without it."""
    if not hide:
        return remote(host, args)
    try:
        return remote(host, [*args, "--hide-secrets"])
    except ValueError as exc:
        if "--hide-secrets" not in str(exc):
            raise
    return remote(host, args)


def cli_memory(a, out=print):
    if a.file is not None and a.all:
        raise ValueError("--file and --all: one of them")
    if a.raw and a.file is None and not a.all:
        raise ValueError("--raw shows a file: add --file NAME (or --all)")
    show, hide = getattr(a, "show_secret", False), getattr(a, "hide_secrets", False) or None
    if local_host(a.host):
        result = memory(a.name, a.file, a.all, a.source, show_secret=show, hide_secrets=hide)
    else:
        result = remote_view(a.host, ["memory", a.name, f"--from={a.source}", *([f"--file={a.file}"] if a.file else []),
                                      *(["--all"] if a.all else []), *(["--show-secret"] if show else [])],
                             hiding(hide))
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in memory_lines(result, a.file, a.all, a.raw):
            out(line)
    return 0


def cli_files(a, out=print):
    if a.full and a.file is None:
        raise ValueError("--full shows a file in full: add --file NAME")
    if a.raw and a.file is None:
        raise ValueError("--raw shows a file as it is: add --file NAME")
    show, hide = getattr(a, "show_secret", False), getattr(a, "hide_secrets", False) or None
    if show and a.file is None:
        raise ValueError("--show-secret shows a file marked as a login or key: add --file NAME")
    searched = getattr(a, "search", None) is not None or getattr(a, "text", None) is not None
    if searched and (a.file is not None or a.raw or a.full or show):
        raise ValueError("--search and --text list the files found: not with --file, --full, --raw or --show-secret")
    if getattr(a, "case", False) and not searched:
        raise ValueError("--case goes with --search or --text")
    if searched:
        pattern = a.search or ""
        Matcher(pattern, a.case)  # (not a pattern: said here, before a host is asked)
        if local_host(a.host):
            result = search(a.name, pattern, a.text, a.case, hide_secrets=hide)
        else:
            result = remote_view(a.host, ["files", a.name, f"--search={pattern}",
                                          *([f"--text={a.text}"] if a.text is not None else []),
                                          *(["--case"] if a.case else [])], hiding(hide))
            if not isinstance(result.get("search"), dict):
                raise ValueError(f"{one_line(a.host)}: the sandbox-ai there does not search session files yet: "
                                 f"update it (sandbox-ai hosts update {one_line(a.host)})")
        if a.json:
            out(json.dumps(result, indent=2))
        else:
            for line in search_lines(result):
                out(line)
        return 0
    if not local_host(a.host):
        if a.raw:
            raise ValueError("--raw is for this machine: with --host the file comes as a conversation (or use "
                             "sandbox-ai hosts run HOST session files ... --raw)")
        result = remote_view(a.host, ["files", a.name, *([f"--file={a.file}"] if a.file else []),
                                      *(["--full"] if a.full else []), *(["--show-secret"] if show else [])],
                             hiding(hide))
    elif a.raw:
        return raw_file(a.name, a.file, show_secret=show, hide_secrets=hide)
    else:
        result = session_files(a.name, a.file, a.full, show_secret=show, hide_secrets=hide)
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in files_lines(result):
            out(line)
    return 0


def confirm_secrets(a, ask=None):
    """--include-secrets is the human's decision, asked first: --yes, or a y at the terminal; nothing that can ask
    (no terminal) and no --yes: refused."""
    if a.yes:
        return
    question = ("Take the logins, keys and secrets of its HOME into the archive? Whoever has the archive can log in "
                "as you with them. [y/N] ")
    if ask is None:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ValueError("--include-secrets takes logins and keys into the archive: confirm it with --yes "
                             "(nothing here can ask)")
        ask = lambda text: (sys.stderr.write(text), sys.stderr.flush(), sys.stdin.readline())[2]  # noqa: E731
    if str(ask(question) or "").strip().lower() not in ("y", "yes"):
        raise ValueError("not exported: --include-secrets was not confirmed")


def cli_export(a, out=print, ask=None):
    include = getattr(a, "include_secrets", False)
    if include:
        confirm_secrets(a, ask)
    if local_host(a.host):
        result = export(a.name, a.out, include_secrets=include)
    else:
        if not a.out.startswith("/"):
            raise ValueError("with --host OUT is a path of that host: give it in full (/...)")
        result = remote(a.host, ["export", a.name, *(["--include-secrets", "--yes"] if include else []), "--", a.out],
                        timeout=3600)
    if a.json:
        out(json.dumps(result, indent=2))
    else:
        for line in export_lines(result):
            out(line)
    return 0


def add_parsers(commands):
    """The subcommands memory, files and export of `sandbox-ai session` (COMMANDS: its subparsers)."""
    shown = commands.add_parser("memory", help="The agent's memory of a start: its memory file and auto memory",
                                description="The agent's memory of a start: its memory file (CLAUDE.md, AGENTS.md, "
                                            "...) and its auto memory (Claude's MEMORY.md and memory files). While it "
                                            "runs or is paused, the live copy in its HOME; once stopped, what went "
                                            "back to the project's memory store. Read only.")
    shown.add_argument("name", help="a start (sandbox-ai list)")
    shown.add_argument("--file", metavar="NAME", help="show this file of the list (its name as listed)")
    shown.add_argument("--all", action="store_true", help="show every file")
    shown.add_argument("--from", dest="source", choices=("auto", "home", "store"), default="auto",
                       help="home: the copy in its HOME; store: the project's memory store (default: auto - the HOME "
                            "while it runs, else the store)")
    shown.add_argument("--raw", action="store_true", help="only the file's text, no header")
    shown.add_argument("--show-secret", action="store_true",
                       help="show the text of a file marked as a login or key too (it is listed, its text held back "
                            "unless asked)")
    shown.add_argument("--hide-secrets", action="store_true",
                       help="leave files that look like logins or keys out of the list (default: listed and marked; "
                            "hide_secrets in your defaults hides them always)")
    shown.add_argument("--json", action="store_true", help="as JSON (with --file or --all: with the texts)")
    shown.add_argument("--host", metavar="HOST", help="a start of this host of sandbox-ai hosts (over SSH)")
    files = commands.add_parser("files", help="The agents' session files of a start: listed, or one shown",
                                description="The agents' own session files (transcripts) in a start's HOME: listed, "
                                            "or one shown as a conversation (user, assistant, tool calls and results, "
                                            "with their times; long tool output shortened unless --full), or as it "
                                            "is (--raw). Read only.")
    files.add_argument("name", help="a start (sandbox-ai list)")
    files.add_argument("--file", metavar="NAME", help="show this file of the list (its name as listed)")
    files.add_argument("--full", action="store_true", help="tool input and output in full")
    files.add_argument("--raw", action="store_true", help="the file as it is (escaped on a terminal)")
    files.add_argument("--show-secret", action="store_true",
                       help="show a file marked as a login or key (it is listed, shown only when asked)")
    files.add_argument("--hide-secrets", action="store_true",
                       help="leave files that look like logins or keys out of the list (default: listed and marked; "
                            "hide_secrets in your defaults hides them always)")
    files.add_argument("--search", metavar="PATTERN",
                       help="list the files whose name (with a /: path) matches PATTERN: * ? [abc] and ** for any "
                            "folders; without a wildcard a part of the name")
    files.add_argument("--text", metavar="TEXT", help="only the files whose text holds TEXT, each with its line "
                                                      "(bounded; logins and keys are not looked into)")
    files.add_argument("--case", action="store_true", help="with --search or --text: letter case counts")
    files.add_argument("--json", action="store_true", help="as JSON")
    files.add_argument("--host", metavar="HOST", help="a start of this host of sandbox-ai hosts (over SSH)")
    exported = commands.add_parser("export", help="A kept start with its state as one tar.gz (session import "
                                                  "takes it)",
                                   description="Write a kept start with its state into a new tar.gz (never over a "
                                               "file that is there): session.json, exit.json, supervisor.log, its "
                                               "HOME with the agent's session files, and for a session its "
                                               "configuration, the project's agent memory, its checkpoints "
                                               "(--no-checkpoints: not), its network approvals and with "
                                               "--with-workspace its workspace's files; manifest.json lists every "
                                               "file with its SHA-256 (sandbox-ai session import checks it). A "
                                               "running session is paused for the moment. Links, sockets and FIFOs "
                                               "are left out; so are logins, keys and files with a placeholder of a "
                                               "secret unless --include-secrets, and the HOME's canaries always. What "
                                               "is left out is listed by name (also in the archive's export.json).")
    exported.add_argument("name", help="a kept start (sandbox-ai list)")
    exported.add_argument("out", metavar="OUT", help="the archive to write (a new file; with --host, a path there)")
    exported.add_argument("--include-secrets", action="store_true",
                          help="take logins, keys and files with a placeholder of a secret into the archive too "
                               "(asked first; whoever has the archive can log in as you)")
    exported.add_argument("--yes", action="store_true", help="--include-secrets without asking (for scripts)")
    exported.add_argument("--with-workspace", action="store_true",
                          help="take the workspace's files into the archive too (default: the import maps it)")
    exported.add_argument("--no-checkpoints", action="store_true", help="leave the workspace's checkpoints out")
    exported.add_argument("--json", action="store_true", help="what was written and left out, as JSON")
    exported.add_argument("--host", metavar="HOST", help="a start of this host of sandbox-ai hosts; OUT is written "
                                                         "there")


def run(a, out=print):
    return {"memory": cli_memory, "files": cli_files, "export": cli_export}[a.command](a, out)
