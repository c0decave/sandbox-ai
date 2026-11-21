#!/usr/bin/env python3
"""What the agents on this machine have set up themselves - their skills, commands, prompts, subagents, memory files,
auto memory, hooks, plugins, marketplaces and MCP servers - read only, for `sandbox-ai kits host-list`, `host-show`,
`host-export` and `host-import` (kitctl.py writes; this module only reads and converts).

Where each agent keeps them (its own folder; CLAUDE_CONFIG_DIR, CODEX_HOME, HERMES_HOME, DSH_HOME, XDG_CONFIG_HOME
as the agents take them):

  Claude Code  ~/.claude: skills/, commands/ (subfolders too), agents/, output-styles/, CLAUDE.md, settings.json and
               settings.local.json (hooks), plugins/ (installed plugins, known marketplaces), projects/*/memory/
               (auto memory); ~/.claude.json (MCP servers)
  Codex        ~/.codex: skills/, prompts/, AGENTS.md, AGENTS.override.md, config.toml (MCP servers)
  shared       ~/.agents/skills (Codex, Goose, OpenHands, Pi, DeepSeek Harness, Hermes read it)
  OpenCode     ~/.config/opencode (OpenCode 1 and 2): skills/ (skill/), agents/ (agent/), commands/ (command/),
               plugins/ (plugin/: code), AGENTS.md, opencode.json (MCP servers)
  Qwen Code    ~/.qwen: skills/, commands/ (TOML), agents/, QWEN.md, settings.json (hooks, MCP servers)
  Gemini CLI   ~/.gemini: skills/, commands/ (TOML), agents/, GEMINI.md, settings.json (MCP servers) - no
               sandbox-ai agent: its parts go into the kits of the agents chosen
  Hermes       ~/.hermes: skills/ (in category folders), memories/MEMORY.md, memories/USER.md
  DeepSeek H.  ~/.dsh: skills/, AGENTS.md
  Goose        ~/.config/goose: skills/, AGENTS.md, .goosehints
  OpenHands    ~/.openhands: skills/ (AGENTS.md there is its memory file)
  nanobot      ~/.nanobot/workspace: skills/, memory/MEMORY.md, AGENTS.md, USER.md, SOUL.md
  Letta Code   ~/.letta: skills/, AGENTS.md
  Pi           ~/.pi/agent: skills/, prompts/, extensions/ (code), AGENTS.md

Nothing is followed out of an agent's folder: an entry that is a symlink counts only when it leads to a place inside
the same folder (a skill kept in a subfolder, say); one that leads elsewhere is listed as refused, never opened. Below
an entry nothing is followed at all (a symlink inside a skill is left out, with a note). Files are opened through
directory descriptors without following links, special files are skipped, and every part has the kits' limits
(kits.KIT_FILES, KIT_BYTES, KIT_DEPTH; one file of a single-file part: TEXT_LIMIT).

A part's id is AGENT:KIND:NAME (claude:skill:pdf, claude:memory:-home-me-proj/feedback.md). What runs code by itself
(hooks, an MCP server's command, a plugin) is shown, never imported; a plugin module (OpenCode, Pi) is imported only
into a kit's hooks, which run only once approved (sandbox-ai kits trust-hooks). Lines that look like credentials (the
privacy check's built-in rules and KEY = value lines with a secret-sounding key) are flagged and masked wherever text
is shown; memory and instructions files are marked private (notes about you and your projects).

Standard library only.
"""
import base64
import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

HERE = Path(__file__).resolve().parent
BUNDLE_VERSION = 1
LIST_VERSION = 1
TEXT_LIMIT = 1 << 20          # one file of a single-file part (a command, an agent, a memory file)
SHOWN_LIMIT = 64 << 10        # the text host-show prints of a file
SCAN_LIMIT = 1 << 20          # bytes of one file a listing scans for secret-looking lines (host-show, an import: all)
LIST_SCAN_BUDGET = 64 << 20   # bytes scanned by one host-list in all (beyond: "secrets": null, host-show checks)
ENTRY_LIMIT = 500             # entries of one folder looked at
NAMES_SHOWN = 20
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1", ".php", ".lua")
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_hostkits", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)  # (published once it ran: a failed load is never half a module)
        _modules[name] = loaded
    return _modules[name]


def kits():
    return module("kits")


TOML_MISSING = "reading TOML needs Python 3.11 or later, or the tomli package (pip install tomli)"


def toml_loads(text):
    """TEXT as TOML: tomllib (Python 3.11 and later) or the tomli package (macOS's python3 is 3.9); ImportError naming
    what is missing without either, ValueError for TOML that is not valid."""
    for name in ("tomllib", "tomli"):
        try:
            loader = importlib.import_module(name)
        except ImportError:
            continue
        return loader.loads(text)
    raise ImportError(TOML_MISSING)


def printable(text):
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


# --- the agents' folders -------------------------------------------------------------------------------------------

AGENT_TITLES = {"claude": "Claude Code", "codex": "Codex", "agents": "Shared skills folder", "opencode": "OpenCode",
                "qwen": "Qwen Code", "gemini": "Gemini CLI", "hermes": "Hermes", "dsh": "DeepSeek Harness",
                "goose": "Goose", "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code", "pi": "Pi"}
KIND_TITLES = {"skill": "skills", "command": "commands", "prompt": "prompts", "agent": "subagents",
               "output-style": "output styles", "memory-file": "memory and instructions files",
               "memory": "memory", "hooks": "hooks", "plugin": "installed plugins", "marketplace": "marketplaces",
               "mcp": "MCP servers", "plugin-module": "plugin modules (code)", "extension": "extensions (code)"}
KIND_ORDER = tuple(KIND_TITLES)
# The kinds a kit or a project's memory can take; the others are shown only (SHOWN_ONLY says why and what to do).
IMPORTABLE = ("skill", "command", "prompt", "agent", "memory-file", "memory", "plugin-module")
MEMORY_KINDS = ("memory-file", "memory")
SHOWN_ONLY = {
    "output-style": "kits carry no output styles: copy the file into a sandbox's ~/.claude/output-styles yourself",
    "hooks": "hooks run commands of this machine by themselves: never imported; a hook a kit should have is added "
             "with sandbox-ai kits add-hook KIT FILE and runs once approved (sandbox-ai kits trust-hooks)",
    "plugin": "a plugin comes from its marketplace: add it with sandbox-ai kits add-plugin KIT NAME@MARKETPLACE (or "
              "the Marketplaces tab), then sandbox-ai plugins claude installs it",
    "marketplace": "declare it in a kit with sandbox-ai kits import-marketplace KIT NAME (or the Marketplaces tab)",
    "mcp": "an MCP server runs a program of this machine; sandbox-ai runs MCP servers itself (sandbox-ai mcp add)",
    "extension": "Pi's extensions are code that runs in Pi: import one only as a kit hook (a .js or .mjs module), "
                 "which runs once approved",
}
PRIVATE_NOTE = ("memory and instructions files may hold private notes about you, your work and your projects: a kit's "
                "copy reaches every sandbox that uses the kit, a project's every later start of that project")

# (kind, place in the agent's folder, how it is read, format)
SOURCES = {
    "claude": [("skill", "skills", "skills", "skill"), ("command", "commands", "md-tree", "claude"),
               ("agent", "agents", "md", "claude"), ("output-style", "output-styles", "md", "claude"),
               ("memory-file", "CLAUDE.md", "file", "text"), ("memory", "projects", "claude-memory", "text"),
               ("hooks", "settings.json", "json-hooks", None), ("hooks", "settings.local.json", "json-hooks", None),
               ("plugin", "plugins/installed_plugins.json", "claude-plugins", None),
               ("marketplace", "plugins/known_marketplaces.json", "claude-marketplaces", None)],
    "codex": [("skill", "skills", "skills", "skill"), ("prompt", "prompts", "md", "codex"),
              ("memory-file", "AGENTS.md", "file", "text"), ("memory-file", "AGENTS.override.md", "file", "text"),
              ("mcp", "config.toml", "toml-mcp", None)],
    "agents": [("skill", "skills", "skills", "skill")],
    "opencode": [("skill", "skills", "skills", "skill"), ("skill", "skill", "skills", "skill"),
                 ("agent", "agents", "md", "opencode"), ("agent", "agent", "md", "opencode"),
                 ("command", "commands", "md-tree", "opencode"), ("command", "command", "md-tree", "opencode"),
                 ("plugin-module", "plugins", "modules", "opencode"), ("plugin-module", "plugin", "modules", "opencode"),
                 ("memory-file", "AGENTS.md", "file", "text"), ("mcp", "opencode.json", "json-mcp:mcp", None)],
    "qwen": [("skill", "skills", "skills", "skill"), ("command", "commands", "toml-tree", "toml"),
             ("agent", "agents", "md", "qwen"), ("memory-file", "QWEN.md", "file", "text"),
             ("hooks", "settings.json", "json-hooks", None), ("mcp", "settings.json", "json-mcp:mcpServers", None)],
    "gemini": [("skill", "skills", "skills", "skill"), ("command", "commands", "toml-tree", "toml"),
               ("agent", "agents", "md", "qwen"), ("memory-file", "GEMINI.md", "file", "text"),
               ("hooks", "settings.json", "json-hooks", None), ("mcp", "settings.json", "json-mcp:mcpServers", None)],
    "hermes": [("skill", "skills", "skills", "skill"), ("memory-file", "memories/MEMORY.md", "file", "text"),
               ("memory", "memories/USER.md", "file", "text")],
    "dsh": [("skill", "skills", "skills", "skill"), ("memory-file", "AGENTS.md", "file", "text")],
    "goose": [("skill", "skills", "skills", "skill"), ("memory-file", "AGENTS.md", "file", "text"),
              ("memory", ".goosehints", "file", "text")],
    "openhands": [("skill", "skills", "skills", "skill"), ("memory-file", "skills/AGENTS.md", "file", "text")],
    "nanobot": [("skill", "workspace/skills", "skills", "skill"),
                ("memory-file", "workspace/memory/MEMORY.md", "file", "text"),
                ("memory", "workspace/AGENTS.md", "file", "text"), ("memory", "workspace/USER.md", "file", "text"),
                ("memory", "workspace/SOUL.md", "file", "text")],
    "letta": [("skill", "skills", "skills", "skill"), ("memory-file", "AGENTS.md", "file", "text")],
    "pi": [("skill", "skills", "skills", "skill"), ("prompt", "prompts", "md", "pi"),
           ("plugin-module", "extensions", "modules", "pi"), ("memory-file", "AGENTS.md", "file", "text")],
}
# Where a kit takes a command or a prompt, and a subagent, per agent (kits.LAYOUT).
COMMAND_TARGETS = {"claude": "commands", "codex": "prompts", "opencode": "commands", "opencode2": "commands"}
AGENT_TARGETS = {"claude": "agents", "opencode": "agents", "opencode2": "agents"}
MODULE_TARGETS = {"opencode": ("opencode", "opencode2"), "pi": ("pi",)}


def agent_folders(env=None):
    """{source: folder} of every agent's own folder, as the agents themselves find it."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return {"claude": Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
            "codex": Path(env.get("CODEX_HOME") or home / ".codex"), "agents": home / ".agents",
            "opencode": config / "opencode", "qwen": home / ".qwen", "gemini": home / ".gemini",
            "hermes": Path(env.get("HERMES_HOME") or home / ".hermes"), "dsh": Path(env.get("DSH_HOME") or home / ".dsh"),
            "goose": config / "goose", "openhands": home / ".openhands", "nanobot": home / ".nanobot",
            "letta": home / ".letta", "pi": home / ".pi/agent"}


def claude_state_file(env=None):
    """~/.claude.json (Claude Code's MCP servers of the user scope): beside ~/.claude, or in CLAUDE_CONFIG_DIR."""
    env = os.environ if env is None else env
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return Path(env.get("HOME") or Path.home()) / ".claude.json"


def tilde(path):
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


# --- reading without following ------------------------------------------------------------------------------------

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class Refused(ValueError):
    """A part that cannot be read or imported (its reason is the message)."""


def real_root(folder):
    """The agent's folder as the file system has it (a folder that is itself a link counts where it leads: that is
    where the agent reads it from), or None when it is missing."""
    try:
        real = Path(os.path.realpath(folder))
        return real if real.is_dir() else None
    except OSError:
        return None


def inside(root, relative):
    """(real relative parts) of ROOT/RELATIVE when it, followed, stays inside ROOT; Refused otherwise (a link that
    leads out of the agent's folder is never followed)."""
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not os.path.lexists(path):
        raise Refused(f"{tilde(path)} does not exist")
    real = Path(os.path.realpath(path))
    if real != root and root not in real.parents:
        target = os.readlink(path) if os.path.islink(path) else str(real)
        raise Refused(f"{tilde(path)} is a symlink to {printable(target)}, outside {tilde(root)}: not followed")
    return real.relative_to(root).parts


def open_inside(root, parts):
    """Directory fd of ROOT/PARTS, no component followed (the real path from inside())."""
    try:
        fd = kits().open_dir(root, tuple(parts))
    except (ValueError, OSError):
        raise Refused(f"{tilde(root.joinpath(*parts))} changed while it was read") from None
    if fd is None:
        raise Refused(f"{tilde(root.joinpath(*parts))} does not exist")
    return fd


def read_file_at(directory_fd, name, limit):
    """(bytes, mode) of the regular file NAME (not followed), else Refused."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise Refused(f"{printable(name)}: {'a symlink' if exc.errno == errno.ELOOP else exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{printable(name)} is not a regular file")
        if info.st_size > limit:
            raise Refused(f"{printable(name)} is larger than {limit >> 10} KiB")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise Refused(f"{printable(name)} is larger than {limit >> 10} KiB")
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def read_tree(directory_fd, label):
    """([(relative path, bytes, mode)], notes) of the folder DIRECTORY_FD: links and special files left out (with a
    note), .git too; the kits' limits are errors (Refused)."""
    k = kits()
    files, notes, state = [], [], {"bytes": 0, "entries": 0}

    def note(text):
        if len(notes) < NAMES_SHOWN:
            notes.append(text)

    def walk(fd, relative, depth):
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            state["entries"] += 1
            if state["entries"] > 4 * k.KIT_FILES:
                raise Refused(f"{label} holds more than {4 * k.KIT_FILES} entries")
            shown = f"{relative}/{name}" if relative else name
            if k.bad_name(name):
                note(f"{printable(shown)} left out: {k.bad_name(name)}")
                continue
            if name == ".git":
                note(f"{shown} left out: a Git repository's own data")
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                note(f"{printable(shown)} left out: a symlink (never followed)")
            elif stat.S_ISDIR(info.st_mode):
                if depth + 1 >= k.KIT_DEPTH:
                    raise Refused(f"{label}: {printable(shown)} is nested deeper than {k.KIT_DEPTH} folders")
                try:
                    child = os.open(name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    note(f"{printable(shown)} left out: changed while it was read")
                    continue
                try:
                    walk(child, shown, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(files) >= k.KIT_FILES:
                    raise Refused(f"{label} holds more than {k.KIT_FILES} files")
                data, mode = read_file_at(fd, name, k.KIT_BYTES)
                state["bytes"] += len(data)
                if state["bytes"] > k.KIT_BYTES:
                    raise Refused(f"{label} holds more than {k.KIT_BYTES >> 20} MiB")
                files.append((shown, data, mode))
            else:
                note(f"{printable(shown)} left out: not a regular file or folder (FIFO, socket or device)")
    walk(directory_fd, "", 0)
    return files, notes


# --- secrets --------------------------------------------------------------------------------------------------------

# KEY = value with a secret-sounding KEY and a literal value: quoted (8+ characters), or a bare token-like word (16+
# characters of a token's alphabet). Code that only names a secret (api_key = os.environ["API_KEY"], token: $TOKEN,
# password: <yours>) is no finding.
GENERIC_SECRET = re.compile(r"""(?i)\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)"""
                            r"""[\w.-]*["']?\s*[:=]\s*(?:"[^"\s$<{]{8,}"|'[^'\s$<{]{8,}'|[A-Za-z0-9_+/=-]{16,}(?![\w.(\[]))""")


def secret_rules():
    """[(id, compiled pattern)]: the privacy check's built-in credential shapes, then KEY = value lines."""
    if "rules" not in _modules:
        rules = []
        try:
            rules = [(pattern.id, pattern.regex) for pattern in module("privacy").builtin_rules().patterns
                     if pattern.secret]
        except Exception:  # noqa: BLE001 - its own copy below is the fallback, never no check
            rules = []
        _modules["rules"] = rules + [("secret-assignment", GENERIC_SECRET)]
    return _modules["rules"]


def mask(line):
    for rule, pattern in secret_rules():
        line = pattern.sub(f"[{rule}]", line)
    return line


def is_text(data):
    return b"\0" not in data[:8192]


def scan(files, limit=None):
    """[{"file", "line", "rule", "text" (masked)}] of the secret-looking lines in FILES ([(path, bytes, mode)]):
    every byte of them (what an import copies and host-show shows), or the first LIMIT bytes of each (a listing:
    measure())."""
    found = []
    for path, data, _ in files:
        if not is_text(data):
            continue
        for number, line in enumerate(data[:limit].decode("utf-8", "replace").split("\n"), 1):
            for rule, pattern in secret_rules():
                if pattern.search(line):
                    shown = "".join(c if c.isprintable() else " " for c in mask(line).strip())
                    found.append({"file": path, "line": number, "rule": rule, "text": shown[:160]})
                    break
    return found


def scripts_of(files):
    """The files of a part an agent may run: executable, a script's suffix, or a #! line."""
    return [path for path, data, mode in files
            if mode & 0o111 or path.endswith(SCRIPT_SUFFIXES) or data.startswith(b"#!")]


# --- front matter ---------------------------------------------------------------------------------------------------

def front_matter(text):
    """([(key, [raw lines])], body) of Markdown TEXT with YAML front matter (---); ([], TEXT) without. Only top-level
    keys are told apart (an indented line belongs to the key before it): enough to keep, drop or rename keys."""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 3)
    if end < 0:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return [], text
    keys = []
    for line in text[4:end + 1].splitlines():
        match = re.match(r"([A-Za-z_][\w-]*)\s*:", line)
        if match and not line[:1].isspace():
            keys.append((match.group(1), [line]))
        elif keys:
            keys[-1][1].append(line)
        elif line.strip():
            keys.append(("", [line]))
    return keys, text[end + 5:]


def value_of(keys, name):
    for key, lines in keys:
        if key == name:
            value = lines[0].split(":", 1)[1].strip()
            return value.strip("\"'") if value not in ("|", ">", "|-", ">-") else " ".join(
                line.strip() for line in lines[1:])
    return None


def with_front_matter(keys, body):
    if not keys:
        return body
    return "---\n" + "".join(line + "\n" for _, lines in keys for line in lines) + "---\n" + body


def description_of(data):
    try:
        text = data[:SCAN_LIMIT].decode("utf-8")
    except UnicodeDecodeError:
        return None
    keys, body = front_matter(text)
    found = value_of(keys, "description")
    if not found:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                found = line
                break
    return " ".join(mask(found).split())[:300] if found else None


# --- discovery ------------------------------------------------------------------------------------------------------

def new_part(agent, kind, name, path, fmt, root):
    return {"id": f"{agent}:{kind}:{name}", "agent": agent, "agent_title": AGENT_TITLES[agent], "kind": kind,
            "name": name, "path": tilde(path), "format": fmt, "description": None, "files": 0, "bytes": 0,
            "scripts": [], "secrets": None, "findings": None, "private": kind in MEMORY_KINDS, "importable": kind in IMPORTABLE,
            "why": None if kind in IMPORTABLE else SHOWN_ONLY.get(kind), "detail": [], "notes": [], "hooks": False,
            "_root": root, "_relative": None}


def refuse(part, why):
    part["importable"], part["why"] = False, why
    return part


def listed(fd):
    with os.scandir(fd) as entries:
        found = sorted(entries, key=lambda e: e.name)
    return [e for e in found if not e.name.startswith(".") and not kits().bad_name(e.name)][:ENTRY_LIMIT]


def folder_fd(root, relative):
    """fd of the folder ROOT/RELATIVE (its real place inside ROOT), None when it is missing, Refused when it leads
    out of ROOT."""
    if not os.path.lexists(root.joinpath(*PurePosixPath(relative).parts)):
        return None, None
    parts = inside(root, relative)
    if not root.joinpath(*parts).is_dir():
        return None, None
    return open_inside(root, parts), parts


def discover(source, folder, budget, measured=True):
    """The parts of the agent SOURCE in its FOLDER: [part] and notes (MEASURED: their files read for sizes, scripts,
    descriptions and secret-looking lines)."""
    parts, notes = [], []
    root = real_root(folder)
    if root is None:
        return parts, notes
    seen = set()
    for kind, relative, how, fmt in SOURCES[source]:
        try:
            parts += [p for p in discover_one(source, root, kind, relative, how, fmt) if p["id"] not in seen]
        except Refused as exc:
            notes.append(str(exc))
        except OSError as exc:
            notes.append(f"{tilde(root / relative)}: {exc.strerror}")
        seen.update(p["id"] for p in parts)
    for part in parts:
        if measured and part["importable"] and part["_relative"] is not None:
            measure(part, budget)
    return parts, notes


def discover_one(source, root, kind, relative, how, fmt):
    if how == "file":
        return [file_part(source, root, kind, relative, Path(relative).name, fmt)] \
            if os.path.lexists(root / relative) else []
    if how in ("json-hooks", "claude-plugins", "claude-marketplaces", "toml-mcp") or how.startswith("json-mcp"):
        return config_parts(source, root, kind, relative, how)
    if how == "claude-memory":
        return claude_memory(source, root)
    fd, parts = folder_fd(root, relative)
    if fd is None:
        return []
    try:
        found = []
        if how == "skills":
            found = skill_parts(source, root, parts, fd, depth=0)
        elif how in ("md", "md-tree", "toml-tree", "modules"):
            suffixes = {"md": (".md",), "md-tree": (".md",), "toml-tree": (".toml",),
                        "modules": (".js", ".mjs", ".ts", ".cjs")}[how]
            found = file_parts(source, root, kind, parts, fd, suffixes, fmt, tree=how.endswith("-tree"),
                               keep_suffix=how == "modules")
        return found
    finally:
        os.close(fd)


def file_part(source, root, kind, relative, name, fmt):
    part = new_part(source, kind, name, root / relative, fmt, root)
    try:
        real = inside(root, relative)
    except Refused as exc:
        return refuse(part, str(exc))
    part["_relative"] = (real, False)
    return part


def skill_parts(source, root, parts, fd, depth, prefix=""):
    """Skill folders (with SKILL.md) in the folder FD (ROOT/PARTS); Hermes and others keep skills in category folders:
    a folder without SKILL.md is looked into once."""
    found = []
    for entry in listed(fd):
        name = prefix + entry.name
        relative = "/".join((*parts, entry.name))
        if entry.is_symlink():
            part = new_part(source, "skill", name, root / relative, "skill", root)
            try:
                real = inside(root, relative)
            except Refused as exc:
                found.append(refuse(part, str(exc)))
                continue
            if not root.joinpath(*real).is_dir():
                continue
            part["_relative"] = (real, True)
            part["notes"].append(f"a symlink to {tilde(root.joinpath(*real))} (inside {tilde(root)})")
            found.append(part)
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            child = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
        except OSError:
            continue
        try:
            info = os.stat("SKILL.md", dir_fd=child, follow_symlinks=False) if os.path.lexists(
                root / relative / "SKILL.md") else None
            if info is not None:
                part = new_part(source, "skill", name, root / relative, "skill", root)
                part["_relative"] = ((*parts, entry.name), True)
                if not stat.S_ISREG(info.st_mode):
                    refuse(part, "its SKILL.md is not a regular file (a symlink?)")
                found.append(part)
            elif depth == 0:
                found += skill_parts(source, root, (*parts, entry.name), child, 1, prefix=f"{entry.name}/")
        finally:
            os.close(child)
    return found


def file_parts(source, root, kind, parts, fd, suffixes, fmt, tree=False, keep_suffix=False, prefix="", depth=0):
    found = []
    for entry in listed(fd):
        relative = "/".join((*parts, entry.name))
        if entry.is_dir(follow_symlinks=False):
            if tree and depth < 3:
                try:
                    child = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue
                try:
                    found += file_parts(source, root, kind, (*parts, entry.name), child, suffixes, fmt, tree,
                                        keep_suffix, f"{prefix}{entry.name}/", depth + 1)
                finally:
                    os.close(child)
            continue
        if not entry.name.endswith(suffixes):
            continue
        stem = entry.name if keep_suffix else entry.name.rsplit(".", 1)[0]
        part = new_part(source, kind, prefix + stem, root / relative, fmt, root)
        try:
            real = inside(root, relative)
        except Refused as exc:
            found.append(refuse(part, str(exc)))
            continue
        part["_relative"] = (real, False)
        if kind == "plugin-module" and source == "pi" and not entry.name.endswith((".js", ".mjs")):
            refuse(part, "Pi takes a kit's extension modules as .js or .mjs only")
        found.append(part)
    return found


def claude_memory(source, root):
    """Claude's auto memory: projects/PROJECT/memory/*.md (MEMORY.md is the index an import keeps up to date)."""
    fd, parts = folder_fd(root, "projects")
    if fd is None:
        return []
    found = []
    try:
        for project in listed(fd):
            if project.is_symlink() or not project.is_dir(follow_symlinks=False):
                continue
            relative = f"projects/{project.name}/memory"
            if not os.path.isdir(root / relative) or os.path.islink(root / relative):
                continue
            try:
                memory = open_inside(root, ("projects", project.name, "memory"))
            except Refused:
                continue
            try:
                index = None
                with contextlib.suppress(Refused):
                    index = read_file_at(memory, "MEMORY.md", TEXT_LIMIT)[0].decode("utf-8", "replace")
                for entry in listed(memory):
                    if entry.name == "MEMORY.md" or not entry.name.endswith(".md"):
                        continue
                    part = new_part(source, "memory", f"{project.name}/{entry.name}", root / relative / entry.name,
                                    "text", root)
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        found.append(refuse(part, "a symlink or not a regular file: not followed"))
                        continue
                    part["_relative"] = (("projects", project.name, "memory", entry.name), False)
                    part["index_line"] = next((line for line in (index or "").splitlines()
                                               if f"]({entry.name})" in line), None)
                    found.append(part)
            finally:
                os.close(memory)
    finally:
        os.close(fd)
    return found


def read_config(root, relative):
    """The text of the configuration file ROOT/RELATIVE (inside the agent's folder), or None when it is missing."""
    if not os.path.lexists(root / relative):
        return None
    parts = inside(root, relative)
    fd = open_inside(root, parts[:-1])
    try:
        return read_file_at(fd, parts[-1], 4 << 20)[0].decode("utf-8", "replace")
    finally:
        os.close(fd)


def command_line(entry):
    if not isinstance(entry, dict):
        return "?"
    if isinstance(entry.get("url"), str):
        return f"{entry.get('type') or 'remote'} {entry['url']}"
    command = entry.get("command")
    words = command if isinstance(command, list) else [command, *(entry.get("args") or [])] \
        if isinstance(entry.get("args"), list) else [command]
    text = " ".join(str(w) for w in words if w is not None)
    env = entry.get("env") or entry.get("environment")
    if isinstance(env, dict) and env:
        text += f" (environment: {', '.join(sorted(str(key) for key in env))}; values not shown)"
    return text


def config_parts(source, root, kind, relative, how):
    """Hooks, installed plugins, known marketplaces and MCP servers of a configuration file: shown only."""
    path = root / relative
    try:
        text = read_config(root, relative)
    except Refused as exc:
        return [refuse(new_part(source, kind, Path(relative).name, path, None, root), str(exc))]
    if text is None:
        return []
    try:
        if how == "toml-mcp":
            document = toml_loads(text)
        else:
            document = json.loads(text)
    except ImportError:
        return [refuse(new_part(source, kind, Path(relative).name, path, None, root),
                       f"{tilde(path)} could not be read: {TOML_MISSING}")]
    except (ValueError, RecursionError):
        return [refuse(new_part(source, kind, Path(relative).name, path, None, root),
                       f"{tilde(path)} could not be read (not valid {'TOML' if how == 'toml-mcp' else 'JSON'})")]
    if not isinstance(document, dict):
        return []
    found = []
    if how == "json-hooks":
        hooks = document.get("hooks")
        if isinstance(hooks, dict) and hooks:
            part = new_part(source, "hooks", Path(relative).name, path, None, root)
            for event, groups in hooks.items():
                for group in groups if isinstance(groups, list) else []:
                    for handler in (group.get("hooks") or []) if isinstance(group, dict) else []:
                        if isinstance(handler, dict):
                            part["detail"].append(mask(f"{event} {group.get('matcher') or '*'}: "
                                                       f"{handler.get('command') or handler.get('type')}"))
            found.append(part)
    elif how == "claude-plugins":
        for name in sorted((document.get("plugins") or {}) if isinstance(document.get("plugins"), dict) else {}):
            found.append(new_part(source, "plugin", name, path, None, root))
    elif how == "claude-marketplaces":
        for name, entry in sorted(document.items()):
            part = new_part(source, "marketplace", name, path, None, root)
            source_ = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source_, dict):
                where = source_.get("repo") or source_.get("url") or source_.get("path") or source_.get("source")
                part["detail"].append(mask(f"source: {where}"))
            found.append(part)
    else:
        key = "mcp_servers" if how == "toml-mcp" else how.split(":", 1)[1]
        servers = document.get(key)
        for name, entry in sorted(servers.items() if isinstance(servers, dict) else []):
            part = new_part(source, "mcp", name, path, None, root)
            part["detail"].append(mask(command_line(entry)))
            found.append(part)
    return found


def claude_mcp(env=None):
    """Claude Code's MCP servers of the user scope (~/.claude.json mcpServers): shown only."""
    path = claude_state_file(env)
    parent = real_root(path.parent)
    if parent is None or not os.path.lexists(path):
        return []
    try:
        fd = open_inside(parent, ())
        try:
            data, _ = read_file_at(fd, path.name, 64 << 20)
        finally:
            os.close(fd)
        document = json.loads(data.decode("utf-8", "replace"))
    except (Refused, ValueError, RecursionError):
        return []
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    found = []
    for name, entry in sorted(servers.items() if isinstance(servers, dict) else []):
        part = new_part("claude", "mcp", name, path, None, parent)
        part["detail"].append(mask(command_line(entry)))
        found.append(part)
    return found


def measure(part, budget):
    """Files, bytes, scripts, description and secret-looking lines of PART (read now; a part that cannot be read is
    refused with the reason). BUDGET: bytes scanned left for this listing."""
    try:
        files, notes = read_part_files(part)
    except Refused as exc:
        refuse(part, str(exc))
        return
    part["files"], part["bytes"] = len(files), sum(len(data) for _, data, _ in files)
    part["scripts"] = scripts_of(files)[:NAMES_SHOWN] if part["kind"] == "skill" else []
    part["notes"] += notes
    main = main_file(part, files)
    if main is not None and part["kind"] != "plugin-module":
        part["description"] = toml_description(main) if part["format"] == "toml" else description_of(main)
        part["hooks"] = part["format"] != "toml" and own_hooks(main)
    if budget[0] >= part["bytes"]:
        budget[0] -= part["bytes"]
        found = len(scan(files, SCAN_LIMIT))
        # A file longer than a listing reads: nothing found in its start says nothing about the rest (None, as
        # beyond the budget: host-show and an import check all of it).
        part["secrets"] = found if found or all(len(data) <= SCAN_LIMIT or not is_text(data)
                                                for _, data, _ in files) else None
        # The hidden-text checker (lint.py): invisible characters, injection phrasing - refused on import too.
        part["findings"] = len(module("lint").check_files(files))


def toml_description(data):
    try:
        value = toml_loads(data[:SCAN_LIMIT].decode("utf-8")).get("description")
    except (ImportError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return " ".join(mask(value).split())[:300] if isinstance(value, str) else None


def own_hooks(data):
    """True when the Markdown DATA (a SKILL.md, a command, a subagent) has hooks of its own in its front matter."""
    try:
        keys, _ = front_matter(data[:SCAN_LIMIT].decode("utf-8"))
    except UnicodeDecodeError:
        return False
    return any(key == "hooks" for key, _ in keys)


def main_file(part, files):
    wanted = "SKILL.md" if part["kind"] == "skill" else None
    for path, data, _ in files:
        if wanted is None or path == wanted:
            return data
    return None


def read_part_files(part):
    """([(relative path, bytes, mode)], notes) of PART: a skill's folder, else its one file."""
    relative, folder = part["_relative"]
    root = part["_root"]
    if folder:
        fd = open_inside(root, relative)
        try:
            return read_tree(fd, part["path"])
        finally:
            os.close(fd)
    fd = open_inside(root, relative[:-1])
    try:
        data, mode = read_file_at(fd, relative[-1], TEXT_LIMIT)
    finally:
        os.close(fd)
    return [(relative[-1], data, mode)], []


def inventory(env=None, measured=True):
    """Everything host-list prints: {"version", "home", "agents": [{"agent", "title", "path", "found", "notes"}],
    "parts": [...]} (the parts' internal keys dropped). MEASURED false: no file of a part is read (the ids only, for
    the shell's completion)."""
    budget = [LIST_SCAN_BUDGET]
    agents, parts = [], []
    for source, folder in agent_folders(env).items():
        found, notes = discover(source, folder, budget, measured)
        if source == "claude":
            found += claude_mcp(env)
        agents.append({"agent": source, "title": AGENT_TITLES[source], "path": tilde(folder),
                       "found": real_root(folder) is not None, "notes": notes})
        parts += found
    return {"version": LIST_VERSION, "home": str(Path((env or os.environ).get("HOME") or Path.home())),
            "agents": agents, "parts": [public(p) for p in parts]}


def public(part):
    return {key: value for key, value in part.items() if not key.startswith("_")}


def find(ids, env=None):
    """{id: part (with its internal keys)} of the parts IDS; Refused for an id no agent has."""
    wanted = list(dict.fromkeys(ids))
    for id_ in wanted:
        check_id(id_)
    by_source = {}
    for id_ in wanted:
        by_source.setdefault(id_.split(":", 1)[0], []).append(id_)
    found = {}
    folders = agent_folders(env)
    for source, names in by_source.items():
        parts, _ = discover(source, folders[source], [0], measured=False)
        if source == "claude":
            parts += claude_mcp(env)
        known = {p["id"]: p for p in parts}
        for id_ in names:
            if id_ not in known:
                raise Refused(f"no {id_} on this machine (see: sandbox-ai kits host-list)")
            found[id_] = known[id_]
    return {id_: found[id_] for id_ in wanted}


def check_id(id_):
    source, _, rest = id_.partition(":")
    kind, _, name = rest.partition(":")
    if source not in SOURCES or kind not in KIND_TITLES or not name or "\0" in id_ or len(id_) > 600:
        raise Refused(f"{printable(id_)} is no part's id: AGENT:KIND:NAME as host-list shows it "
                      "(e.g. claude:skill:pdf)")
    return source, kind, name


# --- what an import of a part makes: the bundle ---------------------------------------------------------------------

def export(ids, env=None):
    """The bundle of the importable parts IDS: {"version", "parts": [{"id", "agent", "kind", "name", "format", "path",
    "index_line", "notes", "files": [{"path", "mode", "data" (base64)}]}]}; Refused for a part that cannot be
    imported (said with its reason, nothing exported)."""
    entries, total = [], 0
    for id_, part in find(ids, env).items():
        if not part["importable"]:
            raise Refused(f"{id_}: {part['why'] or 'cannot be imported'}"
                          + (" (shown only)" if part["kind"] not in IMPORTABLE else ""))
        files, notes = read_part_files(part)
        total += sum(len(data) for _, data, _ in files)
        if total > kits().KIT_BYTES:
            raise Refused(f"the parts chosen hold more than {kits().KIT_BYTES >> 20} MiB together")
        entries.append({"id": id_, "agent": part["agent"], "kind": part["kind"], "name": part["name"],
                        "format": part["format"], "path": part["path"], "index_line": part.get("index_line"),
                        "notes": part["notes"] + notes,
                        "files": [{"path": path, "mode": mode, "data": base64.b64encode(data).decode()}
                                  for path, data, mode in files]})
    return {"version": BUNDLE_VERSION, "parts": entries}


def read_bundle(document, ids):
    """The checked parts of a bundle (export(), from this machine or a host): exactly the parts IDS, each file a
    relative path inside its part (no .., no absolute path, no control characters), within the kits' limits. A host's
    bundle is data: anything else is a ValueError."""
    k = kits()
    if not isinstance(document, dict) or document.get("version") != BUNDLE_VERSION \
            or not isinstance(document.get("parts"), list):
        raise ValueError("the parts sent are not a bundle of sandbox-ai kits host-export (an older sandox-ai there?)")
    parts, total = [], 0
    for entry in document["parts"]:
        if not isinstance(entry, dict):
            raise ValueError("a part sent is not an object")
        source, kind, name = check_id(str(entry.get("id")))
        if (entry.get("agent"), entry.get("kind"), entry.get("name")) != (source, kind, name) or kind not in IMPORTABLE:
            raise ValueError(f"{printable(entry.get('id'))}: the part sent does not match its id")
        files = []
        for item in entry.get("files") if isinstance(entry.get("files"), list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError(f"{entry['id']}: a file sent has no path")
            path = PurePosixPath(item["path"])
            if path.is_absolute() or not path.parts or any(p in (".", "..") or k.bad_name(p) for p in path.parts) \
                    or len(path.parts) > k.KIT_DEPTH or str(path) != item["path"]:
                raise ValueError(f"{entry['id']}: {printable(item['path'])} is no path inside the part")
            try:
                data = base64.b64decode(item.get("data") or "", validate=True)
            except (ValueError, TypeError):
                raise ValueError(f"{entry['id']}: {printable(item['path'])} is not base64") from None
            mode = item.get("mode") if isinstance(item.get("mode"), int) else 0o644
            total += len(data)
            files.append((str(path), data, mode & 0o777))
        if not files or len(files) > k.KIT_FILES or total > k.KIT_BYTES:
            raise ValueError(f"{entry['id']}: no files, or more than the kits' limits")
        if len({path for path, _, _ in files}) != len(files):
            raise ValueError(f"{entry['id']}: a file sent twice")
        if kind == "skill" and "SKILL.md" not in {path for path, _, _ in files}:
            raise ValueError(f"{entry['id']}: a skill without SKILL.md")
        if kind != "skill" and len(files) != 1:
            raise ValueError(f"{entry['id']}: a single-file part with several files")
        line = entry.get("index_line")
        parts.append({"id": entry["id"], "agent": source, "kind": kind, "name": name,
                      "format": entry.get("format") if isinstance(entry.get("format"), str) else None,
                      "path": str(entry.get("path") or ""), "files": files,
                      "index_line": line if isinstance(line, str) and "\n" not in line and len(line) < 1000 else None,
                      "notes": [str(n)[:300] for n in entry.get("notes") or [] if isinstance(n, str)][:NAMES_SHOWN]})
    if sorted(p["id"] for p in parts) != sorted(dict.fromkeys(ids)):
        raise ValueError("the parts sent are not the parts asked for")
    order = {id_: index for index, id_ in enumerate(dict.fromkeys(ids))}
    return sorted(parts, key=lambda p: order[p["id"]])


# --- conversion ------------------------------------------------------------------------------------------------------

SHELL_INJECTION = re.compile(r"!`[^`]+`")
TOML_SHELL, TOML_FILE = "!{", "@{"
FILE_REFERENCE = re.compile(r"(?:^|\s)@[\w./~-]+")


def text_of(data, id_):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{id_}: not UTF-8 text") from None


def strip_hooks(keys):
    """(KEYS without "hooks", the text of the hooks taken out or None): a skill's, command's or subagent's own hooks
    would run in the sandbox without any approval."""
    kept = [(key, lines) for key, lines in keys if key != "hooks"]
    taken = [line for key, lines in keys if key == "hooks" for line in lines]
    return kept, ("\n".join(taken) + "\n" if taken else None)


def target_name(part):
    """The name of PART in a kit: a skill's folder, a file's stem (a nested command's last part)."""
    return part["name"].rsplit("/", 1)[-1]


def command_parts(part):
    """(front matter keys, body) of a command or prompt in any of the formats, as Markdown."""
    text = text_of(part["files"][0][1], part["id"])
    if part["format"] != "toml":
        return front_matter(text)
    try:
        document = toml_loads(text)
    except ImportError as exc:
        raise Refused(f"{part['id']}: {exc}") from None
    except (ValueError, RecursionError) as exc:
        raise Refused(f"{part['id']}: not a TOML command ({exc})") from None
    prompt = document.get("prompt")
    if not isinstance(prompt, str):
        raise Refused(f"{part['id']}: a TOML command without a prompt")
    keys = [("description", [f"description: {json.dumps(document['description'], ensure_ascii=False)}"
                             if re.search(r"[:#\"'\n]", str(document["description"])) else
                             f"description: {document['description']}"])] \
        if isinstance(document.get("description"), str) else []
    return keys, prompt if prompt.endswith("\n") else prompt + "\n"


def convert_command(part, agent):
    """(file name, bytes, notes) of the command or prompt PART for AGENT's kit, or Refused where the formats do not
    map (what it does would change)."""
    fmt, name = part["format"], target_name(part)
    keys, body = command_parts(part)
    keys, hooks = strip_hooks(keys)
    notes = [f"its own hooks are taken out (they would run without approval): {hooks.strip()[:200]}"] if hooks else []
    if fmt == "toml":
        if TOML_SHELL in body:
            raise Refused(f"{part['id']}: runs shell commands (!{{...}}), which only Gemini CLI and Qwen Code "
                          "commands do: Gemini/Qwen-only")
        if TOML_FILE in body:
            raise Refused(f"{part['id']}: injects files (@{{...}}), which only Gemini CLI and Qwen Code commands do: "
                          "Gemini/Qwen-only")
        body = body.replace("{{args}}", "$ARGUMENTS")
    if fmt == "pi":
        body = body.replace("$@", "$ARGUMENTS")
    if agent == fmt or (agent == "opencode2" and fmt == "opencode"):
        return f"{name}.md", with_front_matter(keys, body).encode(), notes
    keep = {"claude": ("description", "argument-hint", "allowed-tools", "model", "disable-model-invocation"),
            "codex": ("description", "argument-hint"), "opencode": ("description",),
            "opencode2": ("description",)}[agent]
    if fmt != "claude":
        keep = tuple(key for key in keep if key not in ("allowed-tools", "model", "disable-model-invocation"))
    dropped = [key for key, _ in keys if key and key not in keep]
    kept = [(key, lines) for key, lines in keys if key in keep]
    if agent == "codex" and SHELL_INJECTION.search(body):
        raise Refused(f"{part['id']}: runs shell commands (!`...`), which Codex prompts cannot: "
                      f"{AGENT_TITLES.get(part['agent'], part['agent'])}-only there")
    if agent == "codex" and FILE_REFERENCE.search(body):
        notes.append("file references (@path) are not expanded by Codex: the agent reads them as text")
    if dropped:
        notes.append(f"{', '.join(dropped)} left out ({agent} has no such setting for a "
                     f"{'prompt' if agent == 'codex' else 'command'})")
    return f"{name}.md", with_front_matter(kept, body).encode(), notes


def convert_agent(part, agent):
    """(file name, bytes, notes) of the subagent PART for AGENT's kit, or Refused."""
    fmt, name = part["format"], target_name(part)
    keys, body = front_matter(text_of(part["files"][0][1], part["id"]))
    keys, hooks = strip_hooks(keys)
    notes = [f"its own hooks are taken out (they would run without approval): {hooks.strip()[:200]}"] if hooks else []
    if agent == fmt or (agent == "opencode2" and fmt == "opencode"):
        return f"{name}.md", with_front_matter(keys, body).encode(), notes
    present = {key for key, _ in keys}
    restricting = sorted(present & {"tools", "permission", "disallowedTools", "allowed-tools"})
    if restricting:
        raise Refused(f"{part['id']}: limits its tools ({', '.join(restricting)}), and the agents name their tools "
                      f"differently: the limit would be lost - {AGENT_TITLES.get(part['agent'], part['agent'])}-only")
    if value_of(keys, "mode") == "primary":
        raise Refused(f"{part['id']}: an OpenCode primary agent, no subagent: OpenCode-only")
    description = value_of(keys, "description")
    if not description:
        raise Refused(f"{part['id']}: has no description, which a subagent needs")
    lines = next(lines for key, lines in keys if key == "description")  # (as written: quoted, folded, ...)
    if agent == "claude":
        kept = [("name", [f"name: {name}"]), ("description", lines)]
    else:
        kept = [("description", lines), ("mode", ["mode: subagent"])]
    dropped = sorted(present - {"name", "description", "mode", ""})
    if dropped:
        notes.append(f"{', '.join(dropped)} left out (no such setting there, or another format)")
    return f"{name}.md", with_front_matter(kept, body).encode(), notes


def default_agents(part, scope, kit_agents=()):
    """The agents a part goes to when none is chosen: its own (OpenCode: 1 and 2, those the kit has), the kit's for
    the shared skills folder; [] when it has to be chosen."""
    source, kind = part["agent"], part["kind"]
    own = {"opencode": ["opencode", "opencode2"]}.get(source, [source])
    possible = targets(part, scope)
    if source in ("agents",) and kind == "skill":
        return [a for a in kit_agents if a in possible] if scope == "kit" else []
    chosen = [a for a in own if a in possible]
    if len(chosen) > 1 and kit_agents:
        chosen = [a for a in chosen if a in kit_agents] or chosen
    return chosen


def targets(part, scope):
    """The agents a part can go to (in a kit: SCOPE "kit"; into a project's memory: "project")."""
    agents = kits().AGENTS
    kind = part["kind"]
    if kind in MEMORY_KINDS:
        return list(agents)
    if scope != "kit":
        return []
    if kind == "skill":
        return list(agents)
    if kind in ("command", "prompt"):
        return list(COMMAND_TARGETS)
    if kind == "agent":
        return list(AGENT_TARGETS)
    if kind == "plugin-module":
        return list(MODULE_TARGETS.get(part["agent"], ()))
    return []


def conversion(part, agent):
    """{"agent", "ok", "where" (in the kit), "why", "notes"} of PART for AGENT's kit."""
    result = {"agent": agent, "ok": False, "where": None, "why": None, "notes": []}
    try:
        if agent not in targets(part, "kit"):
            raise Refused(f"a kit for {kits().AGENT_NAMES.get(agent, agent)} takes no {KIND_TITLES[part['kind']]}")
        kind = part["kind"]
        if kind == "skill":
            result["where"] = f"{agent}/skills/{target_name(part)}/"
        elif kind in ("command", "prompt"):
            name, _, notes = convert_command(part, agent)
            result["where"], result["notes"] = f"{agent}/{COMMAND_TARGETS[agent]}/{name}", notes
        elif kind == "agent":
            name, _, notes = convert_agent(part, agent)
            result["where"], result["notes"] = f"{agent}/agents/{name}", notes
        elif kind == "plugin-module":
            name = part["files"][0][0]
            if not name.endswith(kits().MODULE_SUFFIXES[agent]):
                raise Refused(f"{kits().AGENT_NAMES[agent]} takes plugin modules "
                              f"({', '.join(kits().MODULE_SUFFIXES[agent])}) only")
            result["where"] = f"{agent}/hooks/{name} (runs once approved: sandbox-ai kits trust-hooks)"
        else:
            result["where"] = f"{agent}/{kits().MEMORY_FILE[agent][0]} (appended)"
        result["ok"] = True
    except Refused as exc:
        result["why"] = str(exc)
    return result


def skill_files(part):
    """The files of the skill PART as a kit takes them: its SKILL.md's own hooks taken out (kept beside it in
    hooks.disabled.yaml, which nothing runs); (files, notes)."""
    files, notes = [], []
    for path, data, mode in part["files"]:
        if path == "SKILL.md":
            try:
                keys, body = front_matter(data.decode("utf-8"))
            except UnicodeDecodeError:
                keys, body = [], None
            kept, hooks = strip_hooks(keys)
            if hooks:
                data = with_front_matter(kept, body).encode()
                files.append(("hooks.disabled.yaml", ("# Taken out of SKILL.md by sandbox-ai kits host-import: a "
                                                      "skill's own hooks would run without approval. Add a hook "
                                                      "with: sandbox-ai kits add-hook\n" + hooks).encode(), 0o644))
                notes.append("its own hooks are taken out of SKILL.md (they would run without approval): kept in "
                             "hooks.disabled.yaml, which nothing runs; add one with sandbox-ai kits add-hook")
        files.append((path, data, mode))
    return files, notes


def preview(part, files=None, notes=()):
    """What host-show prints of PART (with its files read): the files, the scripts, the secret-looking lines, the text
    of its main file (masked, shortened) and what it becomes in each agent's kit."""
    shown = public(part)
    if files is None and part["importable"]:
        try:
            files, more = read_part_files(part)
            notes = [*notes, *more]
        except Refused as exc:
            refuse(shown, str(exc))
            files = None
    shown["notes"] = list(dict.fromkeys([*shown.get("notes", []), *notes]))
    shown["text"], shown["secrets"], shown["file_list"], shown["conversions"] = None, [], [], []
    shown["findings"] = []
    if part.get("detail"):
        shown["text"] = "\n".join(part["detail"]) + "\n"
    if not files:
        return shown
    shown["file_list"] = [{"path": path, "bytes": len(data), "script": path in scripts_of([(path, data, mode)])}
                          for path, data, mode in files]
    shown["scripts"] = scripts_of(files)
    shown["secrets"] = scan(files)
    shown["findings"] = module("lint").check_files(files)
    main = main_file(part, files)
    shown["files"], shown["bytes"] = len(files), sum(len(data) for _, data, _ in files)
    if main is not None and part["kind"] != "plugin-module":
        shown["description"] = toml_description(main) if part["format"] == "toml" else description_of(main)
        shown["hooks"] = part["format"] != "toml" and own_hooks(main)
    if main is not None and is_text(main):
        text = main[:SHOWN_LIMIT].decode("utf-8", "replace")
        shown["text"] = "\n".join(mask(line) for line in text.split("\n")) + (
            "\n[… shortened]" if len(main) > SHOWN_LIMIT else "")
    bundle = dict(part, files=files)
    if part["kind"] not in MEMORY_KINDS:
        shown["conversions"] = [conversion(bundle, agent) for agent in targets(part, "kit")]
    if part["kind"] == "skill":
        shown["notes"] += skill_files(bundle)[1]
    if part["private"]:
        shown["notes"].append(PRIVATE_NOTE)
    return shown


# --- text output -----------------------------------------------------------------------------------------------------

def size_text(count):
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KiB" if count < 1 << 20 else \
        f"{count / (1 << 20):.1f} MiB"


def flags(part):
    words = []
    if part["kind"] == "skill" and part["importable"]:
        words.append(f"{part['files']} file(s), {size_text(part['bytes'])}")
    if part["scripts"]:
        words.append(f"scripts: {', '.join(part['scripts'][:3])}{', …' if len(part['scripts']) > 3 else ''}")
    if part["secrets"]:
        words.append(f"{part['secrets']} secret-looking line(s)")
    if part.get("findings"):
        words.append(f"{part['findings']} hidden-text finding(s)")
    if part["private"]:
        words.append("private")
    if part.get("hooks"):
        words.append("own hooks: taken out on import")
    if not part["importable"] and part["kind"] in IMPORTABLE:
        words.append("refused")
    return words


def render(data, out):
    where = f" on {data['host']}" if data.get("host") not in (None, "local") else ""
    by_agent = {}
    for part in data["parts"]:
        by_agent.setdefault(part["agent"], []).append(part)
    out(f"What the agents{where} have set up themselves (read only; import with: sandbox-ai kits host-import "
        "--kit KIT ID...):")
    for agent in data["agents"]:
        parts = by_agent.get(agent["agent"], [])
        if not parts and not agent["notes"]:
            continue
        out("")
        out(f"{agent['title']} ({printable(agent['path'])})")
        for kind in KIND_ORDER:
            chosen = [p for p in parts if p["kind"] == kind]
            if not chosen:
                continue
            out(f"  {KIND_TITLES[kind]}:" + (f" (shown only: {SHOWN_ONLY[kind]})" if kind in SHOWN_ONLY else ""))
            for part in chosen:
                words = flags(part)
                out(f"    {printable(part['id'])}" + (f"  ({'; '.join(words)})" if words else ""))
                if part["description"]:
                    out(f"      {printable(part['description'])}")
                for line in part["detail"][:5]:
                    out(f"      {printable(line)}")
                if not part["importable"] and part["why"] and kind not in SHOWN_ONLY:
                    out(f"      {printable(part['why'])}")
        for note in agent["notes"]:
            out(f"  note: {printable(note)}")
    if not data["parts"]:
        out("No agent on this machine has skills, commands, subagents, memory or other parts of its own.")
    elif any(p["private"] for p in data["parts"]):
        out("")
        out(f"note: {PRIVATE_NOTE}")


def render_part(shown, out):
    out(f"{printable(shown['id'])}: {shown['agent_title']} {KIND_TITLES[shown['kind']]} ({printable(shown['path'])})")
    if shown.get("description"):
        out(f"  {printable(shown['description'])}")
    if not shown["importable"]:
        out(f"  {'shown only' if shown['kind'] not in IMPORTABLE else 'refused'}: {printable(shown['why'] or '')}")
    for item in shown["file_list"]:
        out(f"  file {printable(item['path'])} ({size_text(item['bytes'])}){'  script: an agent may run it' if item['script'] else ''}")
    for found in shown["secrets"]:
        out(f"  secret-looking line: {printable(found['file'])}:{found['line']} ({found['rule']}): "
            f"{printable(found['text'])}")
    for line in module("lint").lines(shown.get("findings") or []):
        out(f"  hidden-text finding: {printable(line)}")
    conversions = shown["conversions"]
    if shown["kind"] == "skill" and conversions and all(item["ok"] for item in conversions):
        out(f"  into a kit: AGENT/skills/{printable(target_name(shown))}/ for any agent "
            f"({', '.join(item['agent'] for item in conversions)})")
        conversions = []
    for item in conversions:
        out(f"  into a kit for {item['agent']}: " + (f"{printable(item['where'])}" if item["ok"] else
                                                      f"no - {printable(item['why'])}"))
        for note in item["notes"]:
            out(f"    note: {printable(note)}")
    for note in shown["notes"]:
        out(f"  note: {printable(note)}")
    if shown["text"]:
        out("")
        for line in shown["text"].split("\n"):
            out(f"  | {printable(line)}")
