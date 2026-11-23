#!/usr/bin/env python3
"""sandbox-ai presets: ready setups to start an agent with.

  sandbox-ai presets list [--json]                      the built-in presets and your own, by category
  sandbox-ai presets categories [--json]                the categories (titles and descriptions in English and German)
  sandbox-ai presets show NAME [--json]                 what NAME sets, needs and asks for
  sandbox-ai presets check NAME [--value KEY=VALUE]... [--agent AGENT]
                                                        what is missing before NAME can start (values, MCP
                                                        servers, VPN profiles, LLM servers the agent can use)
  sandbox-ai presets dirs [list] [--json]               the extra preset directories (defaults.json)
  sandbox-ai presets dirs add|remove DIR                add or remove one
  sandbox-ai AGENT --preset NAME [--preset-value KEY=VALUE]... [options]   start with it

A preset is launcher options (with {placeholders} for what only you know: a folder, a port, a device, an LLM
server), MCP servers by name, an optional ruleset file and an optional first task for the agent. The built-in
presets ship with sandbox-ai (sandbox/presets/*.json); your own, private ones live in
~/.config/sandbox-ai/presets/*.json, outside every workspace (an agent must not write what it starts with), and
win over a built-in one of the same id. Further directories of presets (a private collection, a team's checkout)
are listed in ~/.config/sandbox-ai/defaults.json as "preset_dirs" (`sandbox-ai presets dirs add DIR`): yours win
over them, the first listed over the later ones, and they over the built-in presets. A directory inside a folder
sandboxes can or could write (a workspace, a rw mount, a kept state) is refused and skipped, and so is a preset
file or ruleset there that is a symlink. Such a preset in a built-in one's place, and each secret of yours one hands
on and where to, is said at the start, by `presets show` and on the app's board (preset_warnings). The command line wins over the preset, the preset over nothing else:
a project's .sandbox-ai.json, your defaults, the security mode and the administrator's policy apply as always.

A preset never holds a secret: options only name them (--secret-env NAME, --git-token-secret URL=NAME), and
options that copy a login in are refused. Format (JSON):

  {"format": "sandbox-ai preset", "version": 1, "id": "software-analysis", "title": "...", "description": "...",
   "category": "forensics", "agents": ["claude", "codex", "opencode"], "options": ["--offline", "--mount",
   "{samples}:/mnt/samples:ro"], "mcp": ["hub:capa"], "rules": "software-analysis.rules.json",
   "values": {"samples": {"label": "Folder with the samples", "kind": "folder"}},
   "prompt": "...", "notes": ["..."], "translations": {"de": {"title": "...", "description": "..."}}}

A preset may propose a session bridge between its start and another running session (`sandbox-ai bridge add`):
  "values": {"coder": {"label": "The session to review", "kind": "session", "optional": true}},
  "bridge": {"peer": "{coder}", "allow": ["ask", "files:ro:src"], "direction": "out", "both": false, "ask": "first",
             "observer_actions": ["alert"], "limits": {"rate": 30}, "until": "8h"}
The peer is a value of kind session you pick (NAME, or HOST:NAME of a host of yours), never fixed in the file;
paths are relative to the peer's workspace. Nothing is made by the preset itself: once its start runs, on a terminal,
you are asked (with bridge add's warnings) and your yes runs bridge add; without a terminal the command is said.

The categories (CATEGORY_INFO) are listed by `sandbox-ai presets categories`; "translations" gives the title and
description in German for the app. A server a preset gives a folder of its own for the start (--mcp-mount
NAME:TARGET={folder}:rw, e.g. Engram's brain) is named by `presets check` with the exact `sandbox-ai mcp add` it needs.

Standard library only.
"""
import argparse
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
BUILTIN = HERE / "presets"
# The live check's results per built-in preset and backend (scripts/import_live_report.py from a report of
# scripts/live_check.sh --only presets-live): what a preset's live light is shown from; none: no light (no data yet).
LIVE_EVIDENCE = HERE / "live_evidence.json"
LIVE_SYMBOLS = {"green": "✓", "red": "✗"}
FORMAT = "sandbox-ai preset"
LIMIT = 256 << 10
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
VALUE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
KINDS = ("text", "folder", "file", "port", "device", "host", "secret", "llm", "vpn", "number", "session")
# A preset's "bridge" (optional): a session bridge it PROPOSES between its start and another running session, the
# value of kind session the human picks (never fixed in the file). Made only once the preset's own start runs, after
# the human confirmed it on a terminal with what `bridge add --dry-run` warns about (propose_bridge), by bridge add's
# own code; without a terminal the exact command is said. Its parts are bridge add's: --allow, --both, --ask,
# --observer-actions, --limit, --until, and which side reaches which ("out": this start reaches the peer).
BRIDGE_KEYS = ("peer", "direction", "allow", "both", "ask", "observer_actions", "limits", "until")
BRIDGE_CAPS = ("message", "ask", "watch")  # and files:ro:PATH, files:rw:PATH (PATH relative to the peer's workspace)
BRIDGE_ACTIONS = ("alert", "hint", "net-cut", "pause")  # bridge.OBSERVER_ACTIONS
BRIDGE_ASK = ("each", "first", "never")  # bridge.ASK_POLICIES; never only in a preset of yours
BRIDGE_LIMITS = {"rate": (1, 600), "size": (256, 256 << 10), "ask-timeout": (5, 1800),
                 "daily": (64 << 10, 1 << 34)}  # bridge.LIMIT_RANGES
BRIDGE_DIRECTIONS = ("out", "in")
# --until in a preset: a duration (at least a minute) or HH:MM - never a date, which a preset passed on would outlive.
BRIDGE_UNTIL = re.compile(r"([0-9]{1,6})([smhd])|(?:[01]?[0-9]|2[0-3]):[0-5][0-9]")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# A session as `bridge add` takes it: NAME of this machine or HOST:NAME of a host of `sandbox-ai hosts`.
SESSION = re.compile(r"(?:[a-z0-9][a-z0-9_-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# Agents that take an MCP server while they run (bridge.py's warning; the others read theirs only when they start).
LIVE_MCP_AGENTS = ("claude",)
# The categories of presets, in the order they are listed (sandbox-ai presets list and categories, the app's gallery):
# the one place they are defined. Each has a title and a short description in English and German; a preset's
# "category" is one of these ids, or one of LEGACY_CATEGORIES (presets written before, yours and those of an extra
# directory: they are listed where it points).
CATEGORY_INFO = {
    "development": {"title": {"en": "Development", "de": "Entwicklung"},
                    "description": {"en": "Coding on your projects: languages and their package registries, databases, "
                                          "browsers, memory for the agent.",
                                    "de": "Programmieren an Ihren Projekten: Sprachen und ihre Paket-Registries, "
                                          "Datenbanken, Browser, ein Gedächtnis für den Agenten."}},
    "agents": {"title": {"en": "Agents", "de": "Agenten"},
               "description": {"en": "Each coding agent on its own: with its maker's model, PyPI, npm and GitHub, or "
                                     "with a model on your own server and nothing else.",
                               "de": "Jeder Coding-Agent für sich: mit dem Modell seines Herstellers, PyPI, npm und "
                                     "GitHub, oder mit einem Modell auf Ihrem eigenen Server und sonst nichts."}},
    "research": {"title": {"en": "Research & web", "de": "Recherche & Web"},
                 "description": {"en": "Searching and reading the web, with a memory of the case; what comes from the "
                                       "web is untrusted.",
                                 "de": "Im Web suchen und lesen, mit einem Gedächtnis des Falls; was aus dem Web kommt, "
                                       "gilt als nicht vertrauenswürdig."}},
    "llm": {"title": {"en": "Local LLMs", "de": "Lokale LLMs"},
            "description": {"en": "The agent with a model on your own server (Ollama, LM Studio, llama.cpp, vLLM) "
                                  "instead of the cloud.",
                            "de": "Der Agent mit einem Modell auf Ihrem eigenen Server (Ollama, LM Studio, llama.cpp, "
                                  "vLLM) statt in der Cloud."}},
    "security": {"title": {"en": "Security analysis", "de": "Sicherheitsanalyse"},
                 "description": {"en": "Reviewing code for vulnerabilities and testing targets you own, with the "
                                       "network kept to them.",
                                 "de": "Code auf Schwachstellen prüfen und eigene Ziele testen, das Netz auf sie "
                                       "beschränkt."}},
    "forensics": {"title": {"en": "Forensics & reverse engineering", "de": "Forensik & Reverse Engineering"},
                  "description": {"en": "Evidence, samples and binaries read-only, the analysis tools as MCP servers "
                                        "without network.",
                                  "de": "Beweismittel, Samples und Binärdateien nur lesend, die Analysewerkzeuge als "
                                        "MCP-Server ohne Netz."}},
    "hardware": {"title": {"en": "Hardware & devices", "de": "Hardware & Geräte"},
                 "description": {"en": "Serial ports, debug probes, firmware and phones: a device or a service on the "
                                       "host, nothing more.",
                                 "de": "Serielle Schnittstellen, Debug-Probes, Firmware und Telefone: ein Gerät oder "
                                       "ein Dienst auf dem Host, nicht mehr."}},
    "network": {"title": {"en": "Network", "de": "Netzwerk"},
                "description": {"en": "Captures and traffic analysed offline.",
                                "de": "Mitschnitte und Verkehr offline analysiert."}},
    "privacy": {"title": {"en": "Privacy & anonymity", "de": "Datenschutz & Anonymität"},
                "description": {"en": "Everything through Tor, or as little as possible: the strictest settings.",
                                "de": "Alles über Tor, oder so wenig wie möglich: die strengsten Einstellungen."}},
    "writing": {"title": {"en": "Writing & docs", "de": "Schreiben & Dokumentation"},
                "description": {"en": "Documentation written, built and checked against the code.",
                                "de": "Dokumentation geschrieben, gebaut und gegen den Code geprüft."}},
    "fun": {"title": {"en": "Learning, games & demos", "de": "Lernen, Spiele & Demos"},
            "description": {"en": "Small tasks that show what a sandbox does: races, games, a CTF, git archaeology.",
                            "de": "Kleine Aufgaben, die zeigen, was eine Sandbox tut: Rennen, Spiele, ein CTF, "
                                  "Git-Archäologie."}},
    "testing": {"title": {"en": "Testing sandbox-ai", "de": "sandbox-ai testen"},
                "description": {"en": "Trying sandbox-ai's own limits: canaries, the privacy check, resource limits, "
                                      "networks, its test suites.",
                                "de": "sandbox-ai selbst auf die Probe stellen: Köder, Datenschutzprüfung, "
                                      "Ressourcengrenzen, Netze, seine Testsuiten."}},
    "other": {"title": {"en": "Other", "de": "Weitere"},
              "description": {"en": "Presets that name no category of their own.",
                              "de": "Vorlagen ohne eigene Kategorie."}},
}
CATEGORIES = tuple(CATEGORY_INFO)
DEFAULT_CATEGORY = "other"
# The categories of earlier versions, still taken (your presets, an extra directory's) and listed where they point.
LEGACY_CATEGORIES = {"simple": "other", "analysis": "security", "media": "fun"}
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
KEYS = {"format", "version", "id", "title", "description", "category", "agents", "options", "mcp", "rules",
        "values", "prompt", "notes", "needs", "translations", "bridge"}
# The languages a preset may give its title and description in besides English ("translations": {"de": {"title":
# ..., "description": ...}}): the app shows them in its language. Options, values and notes stay English.
TRANSLATED = ("de",)
TRANSLATED_KEYS = ("title", "description")
# What a preset may not set: what copies a login in (a preset is meant to be passed on), what only a start decides,
# and what is chosen another way (the security mode: --mode on the command line or the project's "mode").
REFUSED = ("--copy-auth", "--auth-file", "--auth-from-session", "--import-codex-auth", "--git-credentials", "--git-token-file",
           "--workspace", "--config", "--no-config", "--session", "--dry-run", "--tmux-socket-dir", "--pod-name",
           "--mcp-socket-dir", "--mcp-approvals-dir", "--helper-of", "--start-id", "--name", "--rules", "--preset",
           # (what a start lacks is fixed unasked only where the human said so: never by a preset passed on)
           "--fix", "--fix-only", "--fix-plan",
           # (the launcher's own word to an MCP server's sandbox: the run whose tunnel it borrows - vpn.validate_lent)
           "--vpn-lent-by",
           "--preset-value", "--")
# Your own presets (~/.config/sandbox-ai/presets, outside every workspace) are yours alone and never passed on: in them
# you may also name the folders a start gets. Built-in presets and those of an extra directory may not.
OWN_ALLOWED = ("--workspace",)
# Values that only you choose (as in a project's file): the cloud provider's whole conversations recorded after TLS
# inspection. A preset of yours may set them; one passed on (built-in, an extra directory's - a team's checkout) may
# not. (The agents' telemetry is always off: an old --telemetry on in any preset is ignored by the launcher.)
OWN_VALUES = {"--llm-traffic": "all"}


def own_values_in(options):
    """["--llm-traffic all", ...]: the OWN_VALUES among OPTIONS (long forms; "--flag value" or "--flag=value")."""
    found = []
    for index, token in enumerate(options):
        name, equals, value = str(token).partition("=")
        if name in OWN_VALUES:
            given = value if equals else (options[index + 1] if index + 1 < len(options) else None)
            if given == OWN_VALUES[name]:
                found.append(f"{name} {given}")
    return found


def own_values_problem(found):
    return (f"{', '.join(dict.fromkeys(found))} is not allowed in a preset that is not yours: only your command line, "
            "~/.config/sandbox-ai/defaults.json or a preset of your own (~/.config/sandbox-ai/presets) chooses it")


def category_of(preset):
    """The category PRESET is listed under: its own (an older one where it points now), else DEFAULT_CATEGORY."""
    category = preset.get("category", DEFAULT_CATEGORY) if isinstance(preset, dict) else DEFAULT_CATEGORY
    category = LEGACY_CATEGORIES.get(category, category)
    return category if category in CATEGORY_INFO else DEFAULT_CATEGORY


def category_text(category, key="title", language="en"):
    """The title (KEY "description": the description) of CATEGORY in LANGUAGE (en, de; English otherwise)."""
    texts = CATEGORY_INFO.get(LEGACY_CATEGORIES.get(category, category), CATEGORY_INFO[DEFAULT_CATEGORY])[key]
    return texts.get(language) or texts["en"]


def categories():
    """[{id, title: {en, de}, description: {en, de}}] in the order they are listed (presets categories --json)."""
    return [{"id": key, **value} for key, value in CATEGORY_INFO.items()]


def refused_flags(origin=None):
    """REFUSED for a preset from ORIGIN ("built-in", "extra", "yours"): yours may also set OWN_ALLOWED."""
    return tuple(flag for flag in REFUSED if origin != "yours" or flag not in OWN_ALLOWED)
# A value of these kinds is checked before it goes into an option.
CHECKS = {"port": re.compile(r"[1-9][0-9]{0,4}"), "number": re.compile(r"[0-9]+(?:\.[0-9]+)?"),
          "host": re.compile(r"[A-Za-z0-9.:\[\]_-]{1,255}"), "secret": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
          # An LLM server as --llm takes it: NAME, NAME:MODEL or NAME:MODEL,MODEL... (llm.py's NAME and MODEL_NAME;
          # qwen2.5-coder:7b, org/Model-GGUF).
          "llm": re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}(?::[\w.:/@+-]{1,200}(?:,[\w.:/@+-]{1,200}){0,49})?",
                            re.ASCII),
          "vpn": re.compile(r"[A-Za-z0-9_.-]{1,64}"), "session": SESSION}


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def user_dir():
    return config_home() / "sandbox-ai" / "presets"


DIRS_KEY = "preset_dirs"  # defaults.json: the extra preset directories, in order
DIRS_LIMIT = 32
# The launcher sets this to its exposed_before() (the trees sandboxes could write in earlier runs) when it loads
# this module; elsewhere net.py's launcher gives it, together with the workspaces of running sandboxes.
EXPOSED_BEFORE = None


def defaults_path():
    return config_home() / "sandbox-ai" / "defaults.json"


def check_dirs(value, where=DIRS_KEY):
    """VALUE as defaults.json's preset_dirs: a list of distinct absolute paths; ValueError otherwise."""
    if not isinstance(value, list) or len(value) > DIRS_LIMIT or not all(
            isinstance(v, str) and os.path.isabs(v) and "\0" not in v and len(v) < 4096
            and not any(ord(c) < 32 for c in v) for v in value):
        raise ValueError(f"{where} is a list of at most {DIRS_LIMIT} absolute paths of directories")
    normal = [os.path.normpath(v) for v in value]
    if len(set(normal)) != len(normal):
        raise ValueError(f"{where} names a directory twice")
    return normal


def listed_dirs():
    """The extra preset directories of defaults.json, in order ([] without the file or the key); a damaged file
    or value is a ValueError."""
    try:
        document = json.loads(defaults_path().read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{defaults_path()}: {exc}") from None
    if not isinstance(document, dict) or DIRS_KEY not in document:
        return []
    return [Path(p) for p in check_dirs(document[DIRS_KEY], f"{defaults_path()}: {DIRS_KEY}")]


def _module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_presets", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def writable_trees():
    """[(tree, why)]: the folders sandboxes can or could write (workspaces, rw mounts, kept states of earlier runs,
    the workspaces of running sandboxes). An error when they cannot be known: a directory is not trusted then."""
    if EXPOSED_BEFORE is not None:
        return [(Path(t), "a folder sandboxes could write in an earlier run") for t in EXPOSED_BEFORE()]
    net = _module("net")
    trees = [(Path(t), "a folder sandboxes could write in an earlier run") for t in net.launcher().exposed_before()]
    trees += [(Path(record["workspace"]), f"the workspace of the running sandbox {name}")
              for name, record in net.running().items() if isinstance(record.get("workspace"), str)]
    return trees


def unsafe_dir(path, trees):
    """Why the preset directory PATH must not be used (None when it may): it lies in a folder sandboxes can write
    (an agent must not write what it is started with), or it is sandbox-ai's own or your presets' folder."""
    literal = Path(os.path.normpath(os.path.abspath(path)))
    real = Path(os.path.realpath(path))
    for candidate in dict.fromkeys([literal, real]):
        for tree, why in trees:
            for form in dict.fromkeys([Path(os.path.normpath(tree)), Path(os.path.realpath(tree))]):
                if candidate == form or form in candidate.parents:
                    return f"it lies in {form}, {why}"
    if real == Path(os.path.realpath(BUILTIN)):
        return "it is the folder of sandbox-ai's built-in presets"
    if real == Path(os.path.realpath(user_dir())):
        return "it is the folder of your own presets, read anyway"
    return None


def extra_dirs(out_problems=None):
    """The extra preset directories that may be used, in order; the others are left out (and said in
    OUT_PROBLEMS)."""
    try:
        listed = listed_dirs()
    except ValueError as exc:
        if out_problems is not None:
            out_problems.append(f"extra preset directories: {exc}")
        return []
    if not listed:
        return []
    try:
        trees = writable_trees()
    except Exception as exc:  # noqa: BLE001 - not knowing where sandboxes write means not trusting the directories
        if out_problems is not None:
            out_problems.append(f"extra preset directories skipped: cannot tell where sandboxes write ({exc})")
        return []
    usable = []
    for directory in listed:
        why = unsafe_dir(directory, trees)
        if why:
            if out_problems is not None:
                out_problems.append(f"preset directory {directory} skipped: {why}")
            continue
        usable.append(directory)
    return usable


def directories(out_problems=None):
    """[(directory, origin)] from the lowest precedence to the highest: the built-in presets, the extra directories
    (the first listed wins over the later ones), yours (they win over all)."""
    extra = [(directory, "extra") for directory in reversed(extra_dirs(out_problems))]
    return [(BUILTIN, "built-in"), *extra, (user_dir(), "yours")]


def problems(document, where="preset", origin=None):
    """Why DOCUMENT is not a usable preset ([] when it is). Shape only; the launcher checks the options themselves."""
    found = []
    if not isinstance(document, dict):
        return [f"{where}: not a JSON object"]
    if document.get("format") != FORMAT or document.get("version") != 1:
        found.append(f'{where}: "format" must be "{FORMAT}" and "version" 1')
    unknown = set(document) - KEYS
    if unknown:
        found.append(f"{where}: unknown keys {sorted(unknown)}")
    if not isinstance(document.get("id"), str) or not ID.fullmatch(document.get("id", "")):
        found.append(f"{where}: id is lower-case letters, digits and -, at most 64")
    for key in ("title", "description"):
        if not isinstance(document.get(key), str) or not document.get(key, "").strip():
            found.append(f"{where}: {key} is a non-empty text")
    category = document.get("category", DEFAULT_CATEGORY)
    if not isinstance(category, str) or (category not in CATEGORIES and category not in LEGACY_CATEGORIES):
        found.append(f"{where}: category is one of {', '.join(CATEGORIES)}")
    agents = document.get("agents", list(AGENTS))
    # Compared one by one: an object or a list among them is no agent (a set of them would fail on it).
    if not isinstance(agents, list) or not agents or not all(isinstance(agent, str) and agent in AGENTS
                                                             for agent in agents):
        found.append(f"{where}: agents is a list of {', '.join(AGENTS)}")
    for key in ("options", "mcp", "notes", "needs"):
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            found.append(f"{where}: {key} is a list of texts")
    values = document.get("values", {})
    if not isinstance(values, dict):
        found.append(f"{where}: values is an object")
        values = {}
    for name, spec in values.items():
        if not VALUE_NAME.fullmatch(name) or not isinstance(spec, dict) or spec.get("kind", "text") not in KINDS \
                or not isinstance(spec.get("label", ""), str) or set(spec) - {"label", "kind", "default", "optional",
                                                                             "hint", "vpn_type"}:
            found.append(f"{where}: value {name!r} needs a label, a kind ({', '.join(KINDS)}) and may have a "
                         "default, optional, hint and (kind vpn) vpn_type")
        elif "vpn_type" in spec and (spec.get("kind") != "vpn" or not isinstance(spec["vpn_type"], str)
                                     or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", spec["vpn_type"])):
            found.append(f"{where}: value {name!r}: vpn_type (the type its VPN profile must have, e.g. tor) is for a "
                         "value of kind vpn")
    for option in document.get("options", []) if isinstance(document.get("options"), list) else []:
        if not isinstance(option, str):
            continue
        flag = option.split("=", 1)[0]
        if flag in refused_flags(origin):
            found.append(f"{where}: {flag} is not allowed in a preset"
                         + (" (only in your own: ~/.config/sandbox-ai/presets)" if flag in OWN_ALLOWED else ""))
        for name in PLACEHOLDER.findall(option):
            if name not in values:
                found.append(f"{where}: {{{name}}} is used but not declared in values")
    options = document.get("options") if isinstance(document.get("options"), list) else []
    widened = own_values_in(options) if origin != "yours" else []
    if widened:
        found.append(f"{where}: {own_values_problem(widened)}")
    rules = document.get("rules")
    if rules is not None and (not isinstance(rules, str) or "/" in rules or not rules.endswith(".json")):
        found.append(f"{where}: rules names a JSON file beside the preset (no path)")
    if document.get("prompt") is not None and not isinstance(document.get("prompt"), str):
        found.append(f"{where}: prompt is a text")
    found += bridge_problems(document, where, origin)
    translations = document.get("translations", {})
    if not isinstance(translations, dict) or set(translations) - set(TRANSLATED) or not all(
            isinstance(texts, dict) and texts and not set(texts) - set(TRANSLATED_KEYS)
            and all(isinstance(text, str) and text.strip() for text in texts.values())
            for texts in translations.values()):
        found.append(f"{where}: translations is {{LANGUAGE: {{title, description}}}} (languages: "
                     f"{', '.join(TRANSLATED)}), each a non-empty text")
    return found


def session_values(document):
    """The names of DOCUMENT's values of kind session (the bridge's peer)."""
    values = document.get("values") if isinstance(document.get("values"), dict) else {}
    return [name for name, spec in values.items() if isinstance(spec, dict) and spec.get("kind") == "session"]


def bridge_path_problem(path):
    """Why PATH (of files:MODE:PATH) is not a path of the peer's workspace that a bridge takes; None when it is."""
    if not isinstance(path, str) or len(path) > 4096 or any(c < " " or "\x7f" <= c <= "\x9f" for c in path):
        return "a path has no control characters and at most 4096 characters"
    if path.startswith(("/", "~")):
        return f"{path}: a path is relative to the peer's workspace (. for all of it)"
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        return f"{path}: '..' is never followed"
    if ".git" in parts:
        return f"{path}: .git is never reached through a bridge"
    return None


def limit_number(key, value):
    """The number a bridge's --limit KEY=VALUE names (bytes for size and daily: 16k, 64m), None when it is none."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip() if isinstance(value, str) else ""
    if key in ("rate", "ask-timeout"):
        return int(text) if re.fullmatch(r"[0-9]{1,7}", text) else None
    match = re.fullmatch(r"([0-9]{1,12})([kKmMgG]?)", text)
    return int(match.group(1)) << {"": 0, "k": 10, "m": 20, "g": 30}[match.group(2).lower()] if match else None


def until_ok(text):
    """TEXT is a --until a preset may give: a duration of at least a minute (30m, 2h, 1d) or HH:MM."""
    match = BRIDGE_UNTIL.fullmatch(text) if isinstance(text, str) else None
    return bool(match) and (match.group(1) is None or int(match.group(1)) * UNIT_SECONDS[match.group(2)] >= 60)


def bridge_problems(document, where="preset", origin=None):
    """Why DOCUMENT's "bridge" (and its values of kind session) is not usable ([] when it is, or when it has none)."""
    found = []
    values = document.get("values") if isinstance(document.get("values"), dict) else {}
    sessions = session_values(document)
    bridge = document.get("bridge")
    caps = bridge.get("allow") if isinstance(bridge, dict) and isinstance(bridge.get("allow"), list) else []
    texts = [*(document.get("options") if isinstance(document.get("options"), list) else []),
             *(document.get("mcp") if isinstance(document.get("mcp"), list) else []), document.get("prompt"), *caps]
    used = {name for text in texts if isinstance(text, str) for name in PLACEHOLDER.findall(text)}
    for name in sessions:
        if name in used or bridge is None:
            found.append(f"{where}: value {name!r}: a value of kind session is the bridge's peer only (\"bridge\": "
                         f"{{\"peer\": \"{{{name}}}\", ...}}), never an option, a server, a path or the task's text")
        if "default" in values[name]:
            found.append(f"{where}: value {name!r}: a session never has a default (you pick it at the start)")
    if bridge is None:
        return found
    if not isinstance(bridge, dict):
        return found + [f"{where}: bridge is an object ({', '.join(BRIDGE_KEYS)})"]
    unknown = set(bridge) - set(BRIDGE_KEYS)
    if unknown:
        found.append(f"{where}: bridge: unknown keys {sorted(unknown)}")
    peer = bridge.get("peer")
    match = PLACEHOLDER.fullmatch(peer) if isinstance(peer, str) else None
    if not match or match.group(1) not in sessions:
        found.append(f"{where}: bridge: peer is a value of kind session, as {{NAME}} (you pick the session at the "
                     "start; it is never fixed in the file)")
    if bridge.get("direction", "out") not in BRIDGE_DIRECTIONS:
        found.append(f"{where}: bridge: direction is out (this start reaches the peer) or in (the peer reaches it)")
    allow = bridge.get("allow")
    if not isinstance(allow, list) or not allow or not all(isinstance(cap, str) for cap in allow):
        found.append(f"{where}: bridge: allow is a non-empty list of message, ask, watch, files:ro:PATH, "
                     "files:rw:PATH")
        allow = []
    for cap in allow:
        if cap in BRIDGE_CAPS:
            continue
        files = re.fullmatch(r"files:(ro|rw):([^,]+)", cap, re.S)
        if not files:
            found.append(f"{where}: bridge: no capability {cap!r} (message, ask, watch, files:ro:PATH, "
                         "files:rw:PATH)")
            continue
        for name in PLACEHOLDER.findall(files.group(2)):
            if name not in values:
                found.append(f"{where}: bridge: {{{name}}} is used but not declared in values")
            elif name not in sessions and values[name].get("kind", "text") != "text":
                found.append(f"{where}: bridge: {{{name}}} in a path is a value of kind text (a folder relative to "
                             "the peer's workspace)")
        why = bridge_path_problem(PLACEHOLDER.sub("x", files.group(2)))
        if why:
            found.append(f"{where}: bridge: {why}")
    if not isinstance(bridge.get("both", False), bool):
        found.append(f"{where}: bridge: both is true or false (the peer gets the same over this start)")
    ask = bridge.get("ask", "first")
    if ask not in BRIDGE_ASK:
        found.append(f"{where}: bridge: ask is each, first or never (what is asked to you about the calls that move "
                     "content across)")
    elif ask == "never" and origin != "yours":
        found.append(f"{where}: bridge: ask never is not allowed in a preset that is not yours: only you choose to "
                     "be asked nothing about what crosses a bridge")
    actions = bridge.get("observer_actions")
    if actions is not None and (not isinstance(actions, list) or not all(action in BRIDGE_ACTIONS
                                                                          for action in actions)):
        found.append(f"{where}: bridge: observer_actions is a list of {', '.join(BRIDGE_ACTIONS)}")
    elif actions and "watch" not in allow:
        found.append(f"{where}: bridge: observer_actions go with watch (an observer's actions)")
    limits = bridge.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - set(BRIDGE_LIMITS):
        found.append(f"{where}: bridge: limits are {{{', '.join(BRIDGE_LIMITS)}}} (bridge add --limit KEY=VALUE)")
        limits = {}
    for key, value in limits.items():
        low, high = BRIDGE_LIMITS[key]
        number = limit_number(key, value)
        if number is None or not low <= number <= high:
            found.append(f"{where}: bridge: limits: {key}: from {low} to {high}")
    until = bridge.get("until")
    if until is not None and not until_ok(until):
        found.append(f"{where}: bridge: until is a duration of at least a minute (30m, 2h, 1d) or HH:MM (never a "
                     "date)")
    return found


def bridge_proposal(preset, given=None):
    """The bridge PRESET proposes with the values GIVEN (--preset-value; its defaults filled in): {preset, peer (None:
    picked at the start), direction, allow, both, ask, observer_actions, limits [[KEY, VALUE]], until, agent_sees
    (whether this start's agent gets a tool of it: it reaches the peer, or is sent messages, questions or hints)};
    None without a bridge. ValueError for a path a value fills in that a bridge would refuse."""
    bridge = preset.get("bridge")
    if not isinstance(bridge, dict):
        return None
    values = checked_values(preset, given or {})[0]
    allow = []
    for cap in bridge["allow"]:
        filled = PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), cap)
        if filled.startswith("files:"):
            mode, path = filled[len("files:"):].split(":", 1)
            path = path.strip() or "."
            why = bridge_path_problem(path) or ("a comma cannot be in a bridge's path" if "," in path else None)
            if why:
                raise ValueError(f"preset {preset['id']}: bridge: {why}")
            filled = f"files:{mode}:{path}"
        allow.append(filled)
    direction = bridge.get("direction", "out")
    actions = [action for action in BRIDGE_ACTIONS if action in (bridge.get("observer_actions") or [])] \
        if "watch" in allow and bridge.get("observer_actions") else None
    sees = direction == "out" or bool({"message", "ask"} & set(allow) or "hint" in (actions or [])
                                      or bridge.get("both"))
    return {"preset": preset["id"], "peer": values.get(PLACEHOLDER.fullmatch(bridge["peer"]).group(1)) or None,
            "direction": direction, "allow": allow, "both": bool(bridge.get("both")),
            "ask": bridge.get("ask", "first"), "observer_actions": actions,
            "limits": [[key, str(value)] for key, value in (bridge.get("limits") or {}).items()],
            "until": bridge.get("until") or None, "agent_sees": sees}


def bridge_argv(proposal, start, peer=None):
    """`sandbox-ai bridge add` (its arguments after "bridge") for PROPOSAL between the start START and PEER (else the
    proposal's own peer, else PEER - to fill in)."""
    peer = peer or proposal.get("peer") or "PEER"
    a, b = (start, peer) if proposal.get("direction", "out") == "out" else (peer, start)
    argv = ["add", a, b, "--allow", ",".join(proposal["allow"]), *(["--both"] if proposal.get("both") else []),
            "--ask", proposal.get("ask") or "first"]
    if proposal.get("observer_actions"):
        argv += ["--observer-actions", ",".join(proposal["observer_actions"])]
    for key, value in proposal.get("limits") or []:
        argv += ["--limit", f"{key}={value}"]
    if proposal.get("until"):
        argv += ["--until", proposal["until"]]
    return argv


def bridge_command(proposal, start, peer=None):
    return "sandbox-ai bridge " + shlex.join(bridge_argv(proposal, start, peer))


def bridge_what(proposal):
    """What PROPOSAL allows, in words: its capabilities, both ways, the observer's actions."""
    text = ",".join(proposal["allow"])
    if proposal.get("observer_actions"):
        text += f" (observer actions: {','.join(proposal['observer_actions'])})"
    return text + (", both ways" if proposal.get("both") else "")


def bridge_agent_note(proposal, agent):
    """What a start of AGENT says about PROPOSAL when its agent reads its MCP servers only when it starts and its side
    gets a tool of the bridge (bridge add warns so too; the Board's bridge_problems): None otherwise."""
    if not proposal or not agent or agent in LIVE_MCP_AGENTS or not proposal.get("agent_sees"):
        return None
    return (f"{agent} reads its MCP servers only when it starts, so it does not see the bridge the preset "
            f"{proposal['preset']} proposes, made once it runs (Claude Code does)")


def running_sessions(starts, start):
    """[(name, agent)] of the agent sessions of this machine that run, but START (what a bridge may join)."""
    found = []
    for entry in starts.start_entries(prune=False).values():
        name = str(entry.get("name") or "")
        if entry.get("state") == "running" and entry.get("kind") in ("session", "run") and name != start \
                and SESSION.fullmatch(name):
            found.append((name, str(entry.get("agent") or "?")))
    return sorted(found)


def _reply(answer, question):
    try:
        return str(answer(question)).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def propose_bridge(proposal, start, out=None, tty=None, answer=None, bridge=None, starts=None):
    """A preset's bridge, once its start START runs: on a terminal (TTY) the peer is picked (from the running sessions,
    unless a value named it), `bridge add --dry-run` says its warnings, and the human's yes runs `bridge add` itself
    (bridge.main, the very code of the command); anything else makes nothing and says the command. Without a terminal
    only the command is said. True when the bridge was made. ANSWER(question): input; BRIDGE, STARTS: the modules."""
    say = out or (lambda line: print(f"agent-sandbox: {printable(line)}", file=sys.stderr))
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if tty is None else tty
    ident = proposal["preset"]
    if not tty:
        say(f"note: the preset {ident} proposes a bridge, made only once you confirm it: "
            f"{bridge_command(proposal, start)}")
        return False
    answer = answer or input
    bridge = bridge or _module("bridge")
    sides = f"this start ({start}) -> {proposal.get('peer') or 'a session you pick'}" \
        if proposal["direction"] == "out" else f"{proposal.get('peer') or 'a session you pick'} -> this start ({start})"
    say(f"the preset {ident} proposes a bridge, made only when you confirm it: {sides}: {bridge_what(proposal)}")
    peer = proposal.get("peer")
    if not peer:
        found = running_sessions(starts or _module("starts"), start)
        say("running sessions to bridge with: " + "  ".join(f"{number}) {name} ({agent})" for number, (name, agent)
                                                             in enumerate(found, 1)) if found else
            "no other session runs on this machine (one of your hosts' is HOST:NAME)")
        reply = _reply(answer, "agent-sandbox: bridge this start to which session? (its number or name, HOST:NAME for "
                               "one of your hosts; empty: none) ")
        if not reply:
            say(f"no bridge made; to make it later: {bridge_command(proposal, start)}")
            return False
        if reply.isdigit() and 1 <= int(reply) <= len(found):
            peer = found[int(reply) - 1][0]
        elif SESSION.fullmatch(reply):
            peer = reply
        else:
            say(f"{reply!r} is not a session (NAME, or HOST:NAME); no bridge made; to make it later: "
                f"{bridge_command(proposal, start)}")
            return False
    argv = bridge_argv(proposal, start, peer)
    if bridge.main([*argv, "--dry-run"], out=say) != 0:
        say(f"not bridged; once it can be: {bridge_command(proposal, start, peer)}")
        return False
    question = f"bridge this start ({start}) to {peer}" if proposal["direction"] == "out" else \
        f"bridge {peer} to this start ({start})"
    reply = _reply(answer, f"agent-sandbox: {question} with {bridge_what(proposal)}? [y/N] ")
    if (reply or "").lower() not in ("y", "yes", "j", "ja"):
        say(f"no bridge made; to make it later: {bridge_command(proposal, start, peer)}")
        return False
    if bridge.main(argv, out=say) != 0:
        say(f"not bridged; once it can be: {bridge_command(proposal, start, peer)}")
        return False
    return True


def translated(preset, key, language="en"):
    """PRESET's KEY (title, description) in LANGUAGE where it has it, else its own (English)."""
    texts = (preset.get("translations") or {}).get(language) if isinstance(preset.get("translations"), dict) else None
    text = texts.get(key) if isinstance(texts, dict) else None
    return text if isinstance(text, str) and text.strip() else preset.get(key, "")


def read_bytes(path, follow=True):
    """The bytes of PATH; FOLLOW=False refuses a symlink (and anything but a regular file)."""
    if follow:
        return Path(path).read_bytes()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path}: a symlink, not followed in an extra preset directory") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: not a regular file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > LIMIT:
                break
            chunks.append(chunk)
        return b"".join(chunks) + (b" " if size > LIMIT else b"")
    finally:
        os.close(fd)


def read(path, follow=True, origin=None):
    data = read_bytes(path, follow)
    if len(data) > LIMIT:
        raise ValueError(f"{path}: larger than {LIMIT >> 10} KiB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError(f"{path}: not valid JSON") from None
    found = problems(document, str(path), origin)
    if found:
        raise ValueError("; ".join(found))
    return document


def catalog(out_problems=None):
    """{id: preset}, each with "_path" and "_origin" (built-in, extra or yours; "_dir" for an extra one); damaged
    files are skipped (and reported to OUT_PROBLEMS). In an extra directory no symlink is followed: a preset or
    ruleset that is one is skipped."""
    found = {}
    for directory, origin in directories(out_problems):
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if path.name.endswith(".rules.json"):
                continue
            try:
                document = read(path, follow=origin != "extra", origin=origin)
                if origin == "extra" and document.get("rules") and (path.parent / document["rules"]).is_symlink():
                    raise ValueError(f"{path}: its ruleset {document['rules']} is a symlink, not followed in an "
                                     "extra preset directory")
            except (OSError, ValueError) as exc:
                if out_problems is not None:
                    out_problems.append(str(exc))
                continue
            before = found.get(document["id"])
            # An extra directory's preset in a built-in one's place (the same id): said wherever it is shown or
            # started (preset_warnings) - --preset ID gets it, not the preset that ships with sandbox-ai.
            shadows = before.get("_shadows") or ("built-in" if before.get("_origin") == "built-in" else None) \
                if before and origin == "extra" else None
            found[document["id"]] = {**document, "_path": str(path), "_origin": origin,
                                     **({"_dir": str(directory)} if origin == "extra" else {}),
                                     **({"_shadows": shadows} if shadows else {})}
    return found


# --- what a preset passed on hands on: your secrets, and where to ---------------------------------------------------
#
# A preset of an extra directory (a team's checkout, someone's collection) names secrets by name only, but it says
# where they go: --api-key HOST=NAME[=SECRET] sends one to a host it chose, --git-token-secret makes one the Git login
# for a server, --secret-env gives one to the agent, --mcp-env NAME:VAR@SECRET and --service ...,secret=... to a server.
# Nothing of it is refused (the secret is yours to give, and a preset is meant for that), but each one is said - at
# the start, by `presets show` and on the app's board - as `board import` says it of a bundle.

SECRET_FLAGS = ("--api-key", "--secret-env", "--git-token-secret", "--mcp-env", "--service")
def flag_values(options, flags):
    """[(flag, value)] of FLAGS among OPTIONS (long forms: "--flag value" or "--flag=value")."""
    found, options = [], [str(token) for token in options]
    for index, token in enumerate(options):
        name, equals, value = token.partition("=")
        if name in flags:
            given = value if equals else (options[index + 1] if index + 1 < len(options) else None)
            if given is not None:
                found.append((name, given))
    return found


def secret_destinations(options):
    """[{"secret", "option", "target", "as"}] of the secrets OPTIONS (canonical) hand on: SECRET goes by OPTION to
    TARGET (a host, a URL, a server, a service; for --secret-env the variable the agent reads), AS the variable a
    server gets it in (--mcp-env), else None."""
    found = []

    def add(secret, option, target, variable=None):
        entry = {"secret": secret, "option": option, "target": target, "as": variable}
        if secret and entry not in found:
            found.append(entry)
    for name, value in flag_values(options, SECRET_FLAGS):
        if name == "--api-key":
            parts = value.split("=")
            if len(parts) >= 2:
                add(parts[2] if len(parts) > 2 else parts[1], name, parts[0])
        elif name == "--secret-env":
            variable, _, secret = value.partition("=")
            add(secret or variable, name, variable)
        elif name == "--git-token-secret":
            url, _, secret = value.rpartition("=")
            add(secret, name, url)
        elif name == "--mcp-env":
            server, _, spec = value.partition(":")
            variable, at, secret = spec.partition("@")
            if at:
                add(secret, name, server, variable)
        elif name == "--service":
            service, _, rest = value.partition(":")
            for part in rest.split(","):
                key, equals, given = part.partition("=")
                if key == "secret" and equals:
                    add(given.partition("=")[2] or given.partition("=")[0], name, service)
    return found


def short(text, limit=200):
    return " ".join(str(text).split())[:limit]


def destination_text(entry, show=short):
    """Where ENTRY (secret_destinations) sends its secret, in words: "goes to HOST (--api-key)"; SHOW(text, limit)
    makes each value printable."""
    option, target = entry["option"], show(entry["target"], 200)
    if option == "--api-key":
        return f"goes to {target} (--api-key)"
    if option == "--secret-env":
        return f"goes into the sandbox as {show(entry['target'], 80)}: the agent reads it (--secret-env)"
    if option == "--git-token-secret":
        return f"is the Git login for {target} (--git-token-secret)"
    if option == "--mcp-env":
        return f"goes to the MCP server {show(entry['target'], 60)} as {show(entry['as'], 80)} (--mcp-env)"
    return f"goes to the service {show(entry['target'], 60)} (--service)"


def secret_text(secret):
    """SECRET as a warning names it: a {placeholder} is the secret you give for it (--preset-value)."""
    return f"the secret you give for {secret}" if PLACEHOLDER.fullmatch(secret) else f"your secret {short(secret, 80)}"


def preset_warnings(preset, options=None):
    """What a preset of an extra directory does that you should know before you start it: [text]. It takes the
    place of a built-in preset (the same id), and each secret its OPTIONS (default: its own, placeholders unfilled;
    at a start: as they run, your values filled in) hand on, and where to. Nothing for yours and the built-in ones."""
    if preset.get("_origin") != "extra":
        return []
    where = f"the preset {preset['id']} from {preset.get('_dir')}"
    found = []
    if preset.get("_shadows"):
        found.append(f"{where} takes the place of the built-in preset {preset['id']}: --preset {preset['id']} starts "
                     "this one")
    for entry in secret_destinations(preset.get("options", []) if options is None else options):
        found.append(f"{secret_text(entry['secret'])} {destination_text(entry)}: {where} says so, and whoever wrote "
                     "it chose where")
    return found


LOADED = None  # the file of the preset load() gave last (the launcher checks it lies where no agent writes)


def load(name):
    global LOADED
    skipped = []
    presets = catalog(skipped)
    if name not in presets:
        # A file of that name that was skipped (refused in an extra directory, damaged): its reason, not only "no
        # preset" - `presets list` does not show it either (hotfix round 1).
        why = [problem for problem in skipped if f"/{name}.json:" in problem or problem.startswith(f"{name}.json:")]
        if why:
            raise ValueError(f"no preset {name!r} to start with: its file was skipped: "
                             + "; ".join(dict.fromkeys(why)))
        raise ValueError(f"no preset {name!r}; see: sandbox-ai presets list")
    LOADED = presets[name]
    return presets[name]


def parse_values(pairs):
    values = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not VALUE_NAME.fullmatch(key):
            raise ValueError(f"--preset-value expects KEY=VALUE: {pair!r}")
        values[key] = value
    return values


def absent_default(spec, value):
    """True when VALUE (expanded) is the default path of a folder, file or device value SPEC and nothing is there:
    a preset's guess about your computer that does not hold here (given in the app's form or not)."""
    default = spec.get("default")
    if spec.get("kind") not in ("folder", "file", "device") or default in (None, ""):
        return False
    return value == os.path.expanduser(str(default)) and not os.path.lexists(value)


def checked_values(preset, given, notes=None):
    """({name: value} for every placeholder: given, else its default; [what is missing]); ValueError for a value
    that is wrong (an unknown name, a malformed port or path). A folder, file or device that is the value's default
    and does not exist on this computer is left out when the value is optional (said in NOTES), and is missing when
    it is not: a start would otherwise create an empty workspace in its place, or fail on a mount."""
    declared = preset.get("values", {})
    unknown = set(given) - set(declared)
    if unknown:
        raise ValueError(f"preset {preset['id']} has no value {', '.join(sorted(unknown))}; it takes: "
                         f"{', '.join(sorted(declared)) or 'none'}")
    values, unset, absent = {}, [], []
    for name, spec in declared.items():
        value = given.get(name, spec.get("default"))
        if value is None or value == "":
            if not spec.get("optional") and spec.get("kind") != "session":  # (a session: picked once the start runs)
                unset.append(name)
            continue
        value = str(value)
        kind = spec.get("kind", "text")
        if kind in CHECKS and not CHECKS[kind].fullmatch(value):
            raise ValueError(f"preset {preset['id']}: {name} must be a {kind}: {value!r}")
        if kind == "port" and not 1 <= int(value) <= 65535:
            raise ValueError(f"preset {preset['id']}: {name} is a port from 1 to 65535: {value!r}")
        if kind in ("folder", "file", "device"):
            value = os.path.expanduser(value)
            if not os.path.isabs(value):
                raise ValueError(f"preset {preset['id']}: {name} needs an absolute path (or ~/...): {value!r}")
            if absent_default(spec, value):
                if not spec.get("optional"):
                    absent.append(name)
                elif notes is not None:
                    notes.append(f"preset {preset['id']}: {name}: {spec['default']} does not exist here, left out "
                                 f"(another one: --preset-value {name}=PATH)")
                continue
        if any(ord(c) < 32 for c in value):
            raise ValueError(f"preset {preset['id']}: {name} contains control characters")
        values[name] = value
    found = []
    if unset:
        found.append(f"preset {preset['id']} needs: " + ", ".join(
            f"--preset-value {name}=... ({declared[name].get('label', name)}"
            + (f"; {declared[name]['hint']}" if declared[name].get("hint") else "") + ")" for name in unset))
    found += [f"preset {preset['id']}: {name} ({declared[name].get('label', name)}): its default "
              f"{declared[name]['default']} does not exist here; create it, or give another: --preset-value {name}=PATH"
              for name in absent]
    return values, found


def resolved_values(preset, given, notes=None):
    """{name: value} for every placeholder (checked_values); ValueError naming what is missing or wrong."""
    values, found = checked_values(preset, given, notes)
    if found:
        raise ValueError("; ".join(found))
    return values


def expand(preset, given=None, extras=True):
    """The launcher options of PRESET with its values filled in. An option whose placeholders are all optional and
    empty is left out, with the flag before it (["--mount", "{extra}"] goes when extra is not given)."""
    values = resolved_values(preset, given or {})
    options, result = list(preset.get("options", [])), []
    index = 0
    while index < len(options):
        option = options[index]
        names = PLACEHOLDER.findall(option)
        if names and any(name not in values for name in names):
            # an optional value left empty: drop this token, and the flag before it when it takes it as its value
            if result and result[-1].startswith("--") and not option.startswith("--"):
                result.pop()
            index += 1
            continue
        result.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], option))
        index += 1
    if not extras:
        return result
    return result + extras_of(preset, values)


def extras_of(preset, values):
    """--mcp and --rules for PRESET's servers and ruleset file (added by sandbox-ai, not options of the preset)."""
    result = []
    for server in preset.get("mcp", []):
        # A catalog entry (hub:capa) is started by the name `sandbox-ai mcp add` gave its definition (capa).
        result += ["--mcp", PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), server).split(":", 1)[-1]]
    if preset.get("rules"):
        result += ["--rules", str(Path(preset["_path"]).parent / preset["rules"])]
    return result


def catalog_choices(preset, values=None):
    """{server: catalog prefix} of PRESET's catalog servers named with their catalog (mcp:binwalk -> {"binwalk":
    "mcp"}), its values filled in: the entry a start that lacks the server adds (an MCP server's name may be an entry
    of more than one catalog)."""
    found = {}
    for server in preset.get("mcp", []):
        prefix, _, name = PLACEHOLDER.sub(lambda m: (values or {}).get(m.group(1), m.group(0)), server).rpartition(":")
        if prefix and name and not PLACEHOLDER.search(name):
            found[name] = prefix
    return found


def prompt_args(preset, agent, values=None):
    """The agent's arguments that give it the preset's first task (none without one), its values filled in."""
    text = preset.get("prompt")
    if not text:
        return []
    text = PLACEHOLDER.sub(lambda m: (values or {}).get(m.group(1), m.group(0)), text)
    if agent == "hermes":
        # Hermes' chat seeds its interactive session with the query on a terminal; the text is the option's own
        # value (--query=...), so one that starts with "-" stays the task.
        return ["chat", "--query=" + text]
    if agent == "qwen":
        # Qwen Code runs the task and stays interactive; the text is the option's own value.
        return ["--prompt-interactive=" + text]
    if agent == "goose":
        # Goose runs the task and stays interactive (goose run --interactive); the text is the option's own value.
        return ["run", "--interactive", "--text=" + text]
    if agent == "openhands":
        # The Canvas has no prompt option: the task runs without the UI (one task, its answer in the window).
        return ["task", "--text=" + text]
    if agent == "nanobot":
        # nanobot's terminal client takes no first task: the task runs as one message (nanobot agent --message=...,
        # its answer in the window), in nanobot's own workspace, so the text says where the project is (as
        # agent_mcp.NANOBOT_PROJECT_NOTE).
        return ["agent", "--message=(sandbox-ai: the project is /work; work there - your tools' relative paths start "
                "in your own workspace.)\n\n" + text]
    if agent == "letta":
        # Letta Code's terminal UI takes no first task: it runs headless (one task, its answer in the window; a
        # restart goes on interactively in the same conversation); after "--" a text that starts with "-" stays it.
        return ["-p", "--", text]
    if agent == "pi":
        # Pi's terminal UI starts with the task as its first message and stays interactive; after "--" a text that
        # starts with "-" stays it, and one that starts with "@" (Pi reads @PATH as a file) gets a space first.
        return ["--", " " + text if text.startswith("@") else text]
    if agent == "dsh":
        # DSH has no prompt for its web UI: the task runs headless (one task, its answer in the window); a text that
        # starts with "-" gets a space first, so DSH never reads it as an option.
        return ["headless", " " + text if text.startswith("-") else text]
    return ["--prompt", text] if agent in ("opencode", "opencode2") else [text]


def option_values(preset, values, flag):
    """[(value, [names of the preset's values in it])] of each FLAG VALUE (or FLAG=VALUE) among PRESET's options,
    its placeholders filled in from VALUES; one whose optional values are left empty is not there."""
    options, found = list(preset.get("options", [])), []
    for index, token in enumerate(options):
        if token == flag and index + 1 < len(options):
            raw = options[index + 1]
        elif token.startswith(flag + "="):
            raw = token.split("=", 1)[1]
        else:
            continue
        names = PLACEHOLDER.findall(raw)
        if any(name not in values for name in names):
            continue
        found.append((PLACEHOLDER.sub(lambda m: values[m.group(1)], raw), names))
    return found


def server_mounts(preset, values, name):
    """["TARGET=HOST:MODE"] of the folders PRESET gives the MCP server NAME (its --mcp-mount NAME:TARGET=HOST:MODE,
    VALUES filled in): what `sandbox-ai mcp add` needs once, for a server that must have a folder (engram, memory)."""
    found = []
    for value, _names in option_values(preset, values, "--mcp-mount"):
        server, colon, rest = value.partition(":")
        if colon and server == name and "=" in rest:
            found.append(shlex.quote(rest))
    return found


def missing(preset, given=None, agent=None, mcp_names=None, notes=None):
    """What keeps PRESET from starting: [text]; empty when nothing does. What a start leaves out or should know
    goes to NOTES."""
    found, given = [], given or {}
    if agent and agent not in preset.get("agents", AGENTS):
        found.append(f"preset {preset['id']} is for {', '.join(preset.get('agents', AGENTS))}, not {agent}")
    try:
        values, problems_found = checked_values(preset, given, notes)
        found += problems_found
    except ValueError as exc:
        found.append(str(exc))
        values = None
    found += registered_values(preset, given, agent)
    known = mcp_names if mcp_names is not None else registered_mcp()
    for server in preset.get("mcp", []):
        if PLACEHOLDER.search(server):
            continue
        name = server.split(":", 1)[-1]
        if name not in known and server not in known:
            found.append(f"MCP server {server} is not added yet: sandbox-ai mcp add {server}"
                         + "".join(f" --mount {mount}" for mount in server_mounts(preset, values or {}, name)))
    if values is None:
        return found
    try:
        proposal = bridge_proposal(preset, given)
    except ValueError as exc:
        found.append(str(exc))
        proposal = None
    if proposal and notes is not None:
        notes.append(f"it proposes a bridge, made once the start runs and only when you confirm it: "
                     f"{bridge_command(proposal, 'START')}")
        why = bridge_agent_note(proposal, agent)
        if why:
            notes.append(why)
    # The image variant it starts (--image-variant): made once from its recipe (android-development: android-dev).
    for variant, _ in option_values(preset, values, "--image-variant"):
        found += variant_missing(variant, agent)
    # The toolchains it brings (--toolchain): one not in the store yet is added when the start offers it.
    for name in missing_toolchains([value for value, _ in option_values(preset, values, "--toolchain")]):
        found.append(f"toolchain {name} is not in the store yet (the start offers to add it): sandbox-ai toolchain add "
                     f"{name}")
    # What the options name by a value of yours: an MCP server (--mcp), a publishing login (--publish).
    def leave_out(names):
        optional = names and all(preset["values"][n].get("optional") for n in names)
        return "; or leave it out: " + " ".join(f"--preset-value {n}=" for n in names) if optional else ""
    for name, names in option_values(preset, values, "--mcp"):
        if name not in known:
            found.append(f"MCP server {name} is not added yet (sandbox-ai mcp list shows yours): sandbox-ai mcp add "
                         f"ENTRY --as {name} (sandbox-ai mcp catalog lists the entries){leave_out(names)}")
    identities = None
    for identity, names in option_values(preset, values, "--identity"):
        try:
            module = _module("identities")
            identities = module.load() if identities is None else identities
        except ValueError as exc:
            found.append(f"--identity {identity}: {exc}")
            break
        if identity != module.DEFAULT and identity not in identities:
            found.append(f"--identity {identity}: no such identity yet; add it once (an identity without commit name "
                         f"and logins carries none of your accounts): sandbox-ai identities add {identity} "
                         f"--description \"no accounts of yours\"{leave_out(names)}")
    logins = None
    for kinds, names in option_values(preset, values, "--publish"):
        try:
            logins = _module("publish_logins").load() if logins is None else logins
        except ValueError as exc:
            found.append(f"--publish {kinds}: {exc}")
            break
        for kind in [k.strip().lower() for k in kinds.split(",") if k.strip()]:
            if kind not in {entry["kind"] for entry in logins}:
                found.append(f"--publish {kind}: no login yet: sandbox-ai secrets set NAME, then sandbox-ai "
                             f"publish-logins add {kind} --secret NAME{leave_out(names)}")
    # A folder, file or device you named that is not there: a start creates a workspace, and fails on anything else.
    if notes is not None:
        servers = {name for _, names in option_values(preset, values, "--mcp-mount") for name in names}
        for name, spec in preset.get("values", {}).items():
            value = values.get(name)
            if spec.get("kind") in ("folder", "file", "device") and value and name in given \
                    and not os.path.lexists(value):
                notes.append(f"{name}: {value} does not exist; the start creates it for its MCP server" if name in
                             servers else f"{name}: {value} does not exist (as a workspace a start creates it empty; "
                             "a mount or device is refused)")
    return found


def variant_missing(variant, agent=None):
    """[what to run] when no agent has the image variant VARIANT yet ([] when one has: a start of another agent takes
    it over); whether its image is built only Podman knows (the start says so)."""
    try:
        images = _module("images")
        if any(name == variant for name, _ in images.variants()):
            return []
    except (ValueError, OSError):
        return []
    who = agent or "AGENT"
    if variant in images.RECIPES:
        return [f"the image variant {variant} is not made yet: sandbox-ai images recipe {variant} --from {who} && "
                f"sandbox-ai build {variant}"]
    return [f"the image variant {variant} is not made yet: sandbox-ai images new {variant} --from {who}, then "
            f"sandbox-ai images extend {variant} ... and sandbox-ai build {variant}"]


def check_vpn_types(preset, values):
    """ValueError when a value of PRESET with a vpn_type (the tor preset's: tor) names a VPN profile of another type:
    "Everything through Tor" must never start behind any VPN. A profile that does not exist is left to the start's
    own check of --vpn (it names the profiles there are)."""
    for name, spec in preset.get("values", {}).items():
        wanted, value = spec.get("vpn_type"), values.get(name)
        if spec.get("kind") != "vpn" or not wanted or not value:
            continue
        try:
            profile = _module("vpn").load_profile(value)
        except ValueError:
            continue
        if profile.get("type") != wanted:
            raise ValueError(f"preset {preset['id']}: {name} must name a {wanted} VPN profile; {value} is of type "
                             f"{profile.get('type')} (sandbox-ai vpn list)")


def last_value(options, flag):
    """The value of the last FLAG VALUE (or FLAG=VALUE) among OPTIONS (long forms), None without one."""
    found = None
    for index, token in enumerate(options):
        if token == flag and index + 1 < len(options):
            found = options[index + 1]
        elif token.startswith(flag + "="):
            found = token.split("=", 1)[1]
    return found


def check_effective_vpn(preset, values, options):
    """The preset's promise holds for the start as it will run: the command line wins over a preset's options, so
    a --vpn given there must be of the type the preset's VPN value needs (the tor preset's: tor), and a preset that
    promises Tor is not started with --vpn-check warn (a failed check, a leak found during the run, must stop it).
    OPTIONS: the start's options (long forms), the preset's and the command line's."""
    for name, spec in preset.get("values", {}).items():
        wanted = spec.get("vpn_type")
        if spec.get("kind") != "vpn" or not wanted or not values.get(name):
            continue
        effective = last_value(options, "--vpn")
        if effective and effective != values[name]:
            try:
                kind = _module("vpn").load_profile(effective).get("type")
            except ValueError:
                kind = None  # a profile that does not exist: the start's own check of --vpn names the ones there are
            if kind is not None and kind != wanted:
                raise ValueError(f"preset {preset['id']}: --vpn {effective} on the command line is of type {kind}; "
                                 f"this preset needs a {wanted} profile ({name})")
        if wanted == "tor" and last_value(options, "--vpn-check") == "warn":
            raise ValueError(f"preset {preset['id']} sends everything through Tor: a failed Tor check must stop it, "
                             "drop --vpn-check warn")


def registered_values(preset, given, agent=None):
    """What keeps the VPN profiles and LLM servers PRESET's values name from a start: [text] (a profile or server
    that is not registered, a server AGENT cannot use); empty when nothing does."""
    found = []
    try:
        values = resolved_values(preset, given)
    except ValueError:
        return found
    for name, spec in preset.get("values", {}).items():
        kind, value = spec.get("kind"), values.get(name)
        if kind not in ("vpn", "llm") or not value:
            continue
        hint = f"; for this preset: {spec['hint']}" if spec.get("hint") else ""
        try:
            if kind == "vpn":
                profile = _module("vpn").load_profile(value)
                wanted = spec.get("vpn_type")
                if wanted and profile.get("type") != wanted:
                    # A per-value constraint: the tor preset's value must be a Tor profile, never any VPN.
                    found.append(f"{name}: the VPN profile {value} is of type {profile.get('type')}, this preset "
                                 f"needs a {wanted} profile{hint}")
            else:
                llm = _module("llm")
                endpoint, _ = llm.selection(SimpleNamespace(llm=value))  # the server, and a model it can name
                problem = llm.agent_problem(endpoint, agent) if agent else None
                if problem:
                    found.append(problem)
        except ValueError as exc:
            found.append(f"{name}: {exc}{hint}")
        except Exception as exc:  # noqa: BLE001 - a damaged store: the start says it in detail
            found.append(f"{name}: {value} cannot be checked ({exc})")
    return found


def registered_mcp():
    """The names of the MCP servers sandbox-ai knows (built-in, added with sandbox-ai mcp add, team servers)."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_for_presets", HERE / "mcp.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return set(module.definitions())
    except Exception:  # noqa: BLE001 - a damaged mcp.json: check reports every server as missing
        return set()


def missing_toolchains(values):
    """The --toolchain VALUES (and what they require) that are not in the store (toolchains.missing), each as
    `toolchain add` takes it; [] when the store cannot be read (the start says what is wrong)."""
    if not values:
        return []
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_toolchains_for_presets", HERE / "toolchains.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.missing(values)
    except Exception:  # noqa: BLE001 - only for what check says
        return []


def refused_in(options, origin=None):
    """The refused flags among OPTIONS (long forms, as the launcher's canonical_options writes them) for a preset
    from ORIGIN (refused_flags)."""
    refused = refused_flags(origin)
    return [token.split("=", 1)[0] for token in options if token.split("=", 1)[0] in refused]


def split_mode(options):
    """(OPTIONS without --mode, the mode it named or None): a preset's security mode is a floor, not a choice."""
    kept, mode, index = [], None, 0
    while index < len(options):
        token = options[index]
        if token == "--mode" and index + 1 < len(options):
            mode, index = options[index + 1], index + 2
            continue
        if token.startswith("--mode="):
            mode, index = token.split("=", 1)[1], index + 1
            continue
        kept.append(token)
        index += 1
    return kept, mode


def copied_logins(options, credential):
    """The --copy values among OPTIONS (canonical) that bring a login in (CREDENTIAL: the launcher's
    copies_credential): a preset may not, as it may not --copy-auth."""
    found = []
    for index, token in enumerate(options):
        name, equals, value = token.partition("=")
        if name != "--copy":
            continue
        value = value if equals else (options[index + 1] if index + 1 < len(options) else "")
        if credential(value):
            found.append(value)
    return found


def host_sources(options):
    """[(flag, host path)] of the --copy SRC:DEST and --mount SRC:DEST[:MODE] values among OPTIONS (canonical)."""
    found = []
    for index, token in enumerate(options):
        name, equals, value = token.partition("=")
        if name not in ("--copy", "--mount"):
            continue
        value = value if equals else (options[index + 1] if index + 1 < len(options) else "")
        parts = 2 if name == "--mount" and value.count(":") >= 2 else 1
        if ":" in value:
            found.append((name, value.rsplit(":", parts)[0]))
    return found


def vpn_bypasses(options, given=()):
    """["--vpn-bypass-host-ports", "--vpn-bypass-tcp ADDRESS:PORT"]: what among OPTIONS (canonical) lets a run past
    its VPN, but a --vpn-bypass-tcp whose value is one the user GIVE (--preset-value)."""
    given, found = {str(value).strip() for value in given if value}, []
    for index, token in enumerate(options):
        name, equals, value = token.partition("=")
        if name == "--vpn-bypass-host-ports":
            found.append(name)
        elif name == "--vpn-bypass-tcp":
            value = value if equals else (options[index + 1] if index + 1 < len(options) else "")
            if value.strip() not in given:
                found.append(f"{name} {value}")
    return found


def apply(argv, canonical=None, with_mode=False, notes=None, credential=None, private=None, code_env=None,
          bridges=None, warnings=None, catalogs=None):
    """ARGV (the launcher's: AGENT options... [-- agent args]) with --preset NAME and --preset-value KEY=VALUE
    replaced by the preset's options, placed right after the agent so that the command line wins; and its first
    task added as the agent's arguments unless the command line gives some. ARGV unchanged without --preset. What
    the preset leaves out (a default folder that is not there) goes to NOTES. PRIVATE(flag, path): why a host path
    a --copy or --mount names is private (the launcher's private_source), else None. CODE_ENV(options, given): the
    --mcp-env values among OPTIONS that change what code runs in an MCP server's sandbox (mcp.code_env_in). BRIDGES
    (a list): the bridge the preset proposes is added to it (bridge_proposal), made only once the start runs, after
    the human confirmed it (propose_bridge). WARNINGS (a list): preset_warnings of the start, said before it runs (a
    preset of an extra directory in a built-in one's place, the secrets of yours it hands on and where to). CATALOGS
    (a dict): {server: catalog prefix} of the catalog servers the preset names by their catalog (mcp:binwalk), so that
    a start that lacks one adds the entry the preset chose where several catalogs have that name (catalog_choices)."""
    if not argv or not any(token == "--preset" or token.startswith("--preset=") for token in argv):
        return (argv, None) if with_mode else argv
    head, tail = list(argv), []
    if "--" in head:
        position = head.index("--")
        head, tail = head[:position], head[position + 1:]
    agent, rest, name, pairs = head[0], [], None, []
    index = 1
    while index < len(head):
        token = head[index]
        for flag in ("--preset", "--preset-value"):
            if token == flag or token.startswith(flag + "="):
                value = token.split("=", 1)[1] if "=" in token else (head[index + 1] if index + 1 < len(head) else None)
                if value is None:
                    raise ValueError(f"{flag} needs a value")
                if flag == "--preset":
                    if name is not None and name != value:
                        raise ValueError("only one --preset per start")
                    name = value
                else:
                    pairs.append(value)
                index += 1 if "=" in token else 2
                break
        else:
            rest.append(token)
            index += 1
    preset = load(name)
    if agent not in preset.get("agents", AGENTS):
        raise ValueError(f"preset {name} is for {', '.join(preset.get('agents', AGENTS))}, not {agent}")
    given = parse_values(pairs)
    resolved_values(preset, given, notes)  # what it leaves out is said once, here
    options = expand(preset, given, extras=False)
    # Checked as the launcher reads them: short forms (-ca for --copy-auth) and values that turned into flags too.
    if canonical:
        options = canonical(options)
    found = refused_in(options, preset.get("_origin"))
    if found:
        raise ValueError(f"preset {name}: {', '.join(dict.fromkeys(found))} is not allowed in a preset")
    # As the preset gives them (a value of its own filled in too): what only you choose.
    widened = own_values_in(options) if preset.get("_origin") != "yours" else []
    if widened:
        raise ValueError(f"preset {name}: {own_values_problem(widened)}")
    # CREDENTIAL (the launcher's copies_credential): a --copy of a login file is a login copied in as well.
    copied = copied_logins(options, credential) if credential else []
    if copied:
        raise ValueError(f"preset {name}: --copy {copied[0]} copies a login in, which is not allowed in a preset "
                         "(as --copy-auth)")
    # PRIVATE (the launcher's private_source): keys and credentials are logins too. A preset passed on may not give
    # the agent a private location of its own choosing; a folder you give it (--preset-value) is your choice.
    if private and preset.get("_origin") != "yours":
        chosen = {os.path.normpath(os.path.expanduser(value)) for value in given.values() if value}
        for flag, source in host_sources(options):
            if os.path.normpath(os.path.expanduser(source)) in chosen:
                continue
            why = private(flag, source)
            if why:
                raise ValueError(f"preset {name}: {flag} {source} is not allowed in a preset that is not yours: {why}. "
                                 "Give it yourself if you mean it (on the command line, or as the folder a "
                                 "--preset-value asks for)")
    # What lets the run past its VPN or Tor (the host's ports, an address of your network) reaches the internet from
    # your own address: only you choose that, by a value you give (--preset-value), never by the preset's own text.
    if preset.get("_origin") != "yours":
        past = vpn_bypasses(options, given.values())
        if past:
            raise ValueError(f"preset {name}: {', '.join(past)} is not allowed in a preset that is not yours: it lets "
                             "the run past its VPN or Tor, from your own address; give it yourself if you mean it")
    # A variable that changes what code runs in an MCP server's sandbox (LD_PRELOAD, NODE_OPTIONS, PYTHONSTARTUP):
    # the human's own is set with a warning; one from someone else's text is not (a value you give is yours).
    if code_env is not None and preset.get("_origin") != "yours":
        changing = code_env(options, given.values())
        if changing:
            raise ValueError(f"preset {name}: {', '.join(dict.fromkeys(changing))} is not allowed in a preset that is "
                             "not yours: it changes what code runs in the MCP server's sandbox; give it yourself if "
                             "you mean it (on the command line, with a warning)")
    # The preset's --mode is taken out: the launcher applies it only where it is stricter than what the command
    # line, the project file and your defaults choose (it never lowers a stricter one).
    options, mode = split_mode(options)
    options += extras_of(preset, resolved_values(preset, given))
    if catalogs is not None:
        catalogs.update(catalog_choices(preset, resolved_values(preset, given)))
    if warnings is not None:
        warnings += preset_warnings(preset, options)
    check_vpn_types(preset, resolved_values(preset, given))
    check_effective_vpn(preset, resolved_values(preset, given), canonical([*options, *rest]) if canonical
                        else [*options, *rest])
    proposal = bridge_proposal(preset, given)
    if proposal and bridges is not None:
        bridges.append(proposal)
    agent_args = tail if tail else prompt_args(preset, agent, resolved_values(preset, given))
    result = [agent, *options, *rest, *(["--", *agent_args] if agent_args or "--" in argv else [])]
    return (result, mode) if with_mode else result


def printable(text):
    """TEXT safe for a terminal (the launcher's printable): control, bidi and other non-printable characters escaped.
    A preset passed on is someone else's text: its title, notes and labels must not drive the user's terminal (an
    escape sequence that clears it, sets its title or - OSC 52 - writes the clipboard the user pastes from next)."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def origin_text(preset):
    return f"from {preset['_dir']}" if preset["_origin"] == "extra" else preset["_origin"]


def describe(preset, out=print):
    say = out
    out = lambda line: say(printable(line))  # noqa: E731 - every line of it holds the preset's own texts
    out(f"{preset['id']}: {preset['title']}  ({category_text(category_of(preset))}, {origin_text(preset)})")
    out(f"  {preset['description']}")
    out(f"  agents: {', '.join(preset.get('agents', AGENTS))}")
    if preset.get("options"):
        out(f"  options: {shlex.join(preset['options'])}")
    if preset.get("mcp"):
        out(f"  MCP servers: {', '.join(preset['mcp'])}")
    if preset.get("rules"):
        out(f"  ruleset: {preset['rules']}")
    for name, spec in preset.get("values", {}).items():
        default = f" (default {spec['default']})" if spec.get("default") not in (None, "") else ""
        optional = " (optional)" if spec.get("optional") else ""
        hint = f"; {spec['hint']}" if spec.get("hint") else ""
        out(f"  value {name}: {spec.get('label', name)} [{spec.get('kind', 'text')}]{default}{optional}{hint}")
    bridge = preset.get("bridge")
    if isinstance(bridge, dict):
        peer = bridge.get("peer")
        sides = f"this start -> {peer}" if bridge.get("direction", "out") == "out" else f"{peer} -> this start"
        parts = [",".join(bridge.get("allow") or [])] + (["both ways"] if bridge.get("both") else [])
        parts += [f"observer actions {','.join(bridge['observer_actions'])}"] if bridge.get("observer_actions") else []
        parts.append(f"--ask {bridge.get('ask', 'first')}")
        if bridge.get("limits"):
            parts.append("limits " + ",".join(f"{key}={value}" for key, value in bridge["limits"].items()))
        if bridge.get("until"):
            parts.append(f"until {bridge['until']}")
        out(f"  bridge: proposed once the start runs, made only when you confirm it: {sides} ({'; '.join(parts)})")
    for need in preset.get("needs", []):
        out(f"  needs: {need}")
    for warning in preset_warnings(preset):
        out(f"  warning: {warning}")
    live = live_light(preset)
    if live:
        out(f"  live: {live_text(live)}")
        for run in live["backends"]:
            out(f"    {run['backend']}: {run['result']} on {run['date']} (sandbox-ai {run['version']}, {run['report']})"
                + (f": {run['detail']}" if run["detail"] else ""))
    for note in preset.get("notes", []):
        out(f"  note: {note}")
    cairn = cairn_workers_line(preset)
    if cairn:
        out(f"  {cairn}")
    if preset.get("prompt"):
        out(f"  first task: {preset['prompt']}")


def cairn_workers_line(preset):
    """A preset with Cairn's board (--cairn): its workers' traffic lights (cairn_workers.py), or None. The workers
    are those of its --cairn-workers, else its --with agents and the lead (the agent it is started with)."""
    options = [str(word) for word in preset.get("options", [])]
    if "--cairn" not in options:
        return None
    try:
        table = _module("cairn_workers")
        given = options[options.index("--cairn-workers") + 1] if "--cairn-workers" in options[:-1] else None
        named = [item.split("=", 1)[0].strip() for item in given.split(",")] if given else \
            [agent for index, word in enumerate(options[:-1]) if word == "--with"
             for agent in options[index + 1].split(",")]
        rows = table.rows([agent for agent in dict.fromkeys(named) if agent in table.relay().HARNESSES])
    except (OSError, ValueError):
        return None
    shown = ", ".join(f"{row['symbol']} {row['agent']} {row['words']}" for row in rows)
    lead = "" if given else (", and the lead's" if shown else "the lead's")
    return f"Cairn's workers' support (sandbox-ai cairn workers): {shown}{lead}"


def public(preset):
    passed_on = preset.get("_origin") == "extra"
    return ({key: value for key, value in preset.items() if not key.startswith("_")} | {"origin": preset["_origin"]}
            | ({"directory": preset["_dir"]} if preset.get("_dir") else {})
            | ({"shadows": preset["_shadows"]} if preset.get("_shadows") else {})
            | ({"secrets_handed_on": secret_destinations(preset.get("options", [])),
                "warnings": preset_warnings(preset)} if passed_on else {})
            | {"live": live_light(preset)})


_live = {}


def live_entries(path=None):
    """{preset id: [entry]} of LIVE_EVIDENCE (the live check's imported results, kind preset); {} when the file is
    missing or not as scripts/import_live_report.py writes it (then no light is shown, nothing is refused)."""
    path = Path(path or LIVE_EVIDENCE)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _live.get("key") == (str(path), stamp):
        return _live["found"]
    found = {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        for entry in document.get("entries") or []:
            if isinstance(entry, dict) and entry.get("kind") == "preset" and isinstance(entry.get("name"), str) \
                    and entry.get("result") in ("passed", "failed"):
                found.setdefault(entry["name"], []).append(
                    {key: str(entry.get(key) or "") for key in ("backend", "agent", "result", "date", "version",
                                                                "report", "detail")})
    except (OSError, ValueError, RecursionError, AttributeError):
        found = {}
    _live.update(key=(str(path), stamp), found=found)
    return found


def live_light(preset, path=None):
    """The live light of a built-in PRESET from the live check's evidence: {light (green: every backend it ran with
    passed; red: one failed), symbol, date and version (the newest run's), backends [{backend, result, date,
    version, report, detail}], why (the failed run's reason)}, or None - no data yet: nothing shown. Your own and
    an extra directory's presets have none (the live check runs the built-in ones)."""
    if preset.get("_origin", "built-in") != "built-in":
        return None
    runs = sorted(live_entries(path).get(preset.get("id"), []), key=lambda e: (e["date"], e["backend"]))
    if not runs:
        return None
    failed = [run for run in runs if run["result"] == "failed"]
    newest = runs[-1]
    colour = "red" if failed else "green"
    return {"light": colour, "symbol": LIVE_SYMBOLS[colour], "date": newest["date"], "version": newest["version"],
            "backends": runs, "why": f"{failed[-1]['backend']}: {failed[-1]['detail']}" if failed else ""}


def live_text(found):
    """A preset's live light (live_light) in words: "✓ live check passed (podman, bwrap) on 2026-11-23, sandbox-ai
    0.1.212", "✗ live check failed on 2026-11-23 (bwrap: REASON)"; "" for none."""
    if not found:
        return ""
    if found["light"] == "green":
        backends = ", ".join(run["backend"] for run in found["backends"])
        return f"{found['symbol']} live check passed ({backends}) on {found['date']}, sandbox-ai {found['version']}"
    return f"{found['symbol']} live check failed on {found['date']}, sandbox-ai {found['version']} ({found['why']})"


# -- the extra preset directories (defaults.json's preset_dirs) --

def dirs_report():
    """[{path, state, why, presets}] of every listed directory: usable (with how many presets it holds) or why not."""
    listed = listed_dirs()
    try:
        trees, problem = writable_trees(), None
    except Exception as exc:  # noqa: BLE001
        trees, problem = [], f"cannot tell where sandboxes write ({exc})"
    report = []
    for directory in listed:
        why = problem or unsafe_dir(directory, trees)
        if why is None and not directory.is_dir():
            why = "it does not exist (or is not a directory)"
        count = 0
        if why is None:
            try:
                count = sum(1 for p in directory.glob("*.json") if not p.name.endswith(".rules.json"))
            except OSError as exc:
                why = exc.strerror
        report.append({"path": str(directory), "state": "skipped" if why else "used", "why": why, "presets": count})
    return report


def change_dirs(change, out):
    """Apply CHANGE(list of paths) -> new list to defaults.json through sandbox-ai defaults (checked, atomic)."""
    defaults = _module("defaults_cmd")
    with defaults.locked():  # from reading defaults.json to writing it back, as `sandbox-ai defaults set`
        document, path = defaults.read()
        current = check_dirs(document.get(DIRS_KEY, []), f"{path}: {DIRS_KEY}")
        wanted = change(list(current))
        if wanted:
            document[DIRS_KEY] = wanted
        else:
            document.pop(DIRS_KEY, None)
        defaults.check(document, path)
        defaults.write(path, document)
    return path


def dirs_add(value, out):
    path = os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    if not os.path.isdir(path):
        raise ValueError(f"{value}: not a directory")
    if os.path.islink(path):
        out(f"note: {path} is a symlink to {os.path.realpath(path)}; both are checked, now and at every use")
    why = unsafe_dir(path, writable_trees())
    if why:
        raise ValueError(f"{path} cannot hold presets: {why} (an agent must not write what it is started with)")

    def add(current):
        if path in current:
            raise ValueError(f"{path} is listed already")
        return current + [path]
    where = change_dirs(add, out)
    count = sum(1 for p in Path(path).glob("*.json") if not p.name.endswith(".rules.json"))
    out(f"preset directory {path} added ({count} preset file(s)) in {where}; your own presets still win over it, "
        "it over the built-in ones")
    return 0


def dirs_remove(value, out):
    path = os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    def remove(current):
        if path not in current:
            raise ValueError(f"{path} is not listed; see: sandbox-ai presets dirs list")
        return [p for p in current if p != path]
    where = change_dirs(remove, out)
    out(f"preset directory {path} removed from {where}")
    return 0


def dirs_main(args, out):
    if args.dirs_command == "add":
        return dirs_add(args.directory, out)
    if args.dirs_command == "remove":
        return dirs_remove(args.directory, out)
    report = dirs_report()
    if args.json:
        out(json.dumps({"key": DIRS_KEY, "file": str(defaults_path()), "builtin": str(BUILTIN), "yours": str(user_dir()),
                        "dirs": report}, indent=2))
        return 0
    out(f"yours (win over all): {user_dir()}")
    for number, entry in enumerate(report, 1):
        state = f"{entry['presets']} preset(s)" if entry["state"] == "used" else f"skipped: {entry['why']}"
        out(f"{number}. {entry['path']}  ({state})")
    if not report:
        out("no extra preset directories; add one with: sandbox-ai presets dirs add DIR")
    out(f"built-in: {BUILTIN}")
    return 0


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai presets", description=__doc__.split("\n\nStandard library")[0],
                                formatter_class=help_style, allow_abbrev=False)
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the built-in presets and your own, by category")
    listing.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    kinds = commands.add_parser("categories", help="the categories presets are listed under (English and German)")
    kinds.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    shown = commands.add_parser("show", help="what a preset sets, needs and asks for")
    shown.add_argument("name")
    shown.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    checked = commands.add_parser("check", help="what is missing before a preset can start")
    checked.add_argument("name")
    checked.add_argument("--value", action="append", default=[], metavar="KEY=VALUE", help="a value for a setting of the preset: KEY=VALUE (repeatable)")
    checked.add_argument("--agent", choices=AGENTS, help="check for this agent (default: the preset's own)")
    dirs = commands.add_parser("dirs", help="the extra preset directories (defaults.json: preset_dirs)",
                               formatter_class=help_style, description="The extra preset directories "
                                                                       "(defaults.json: preset_dirs); alone: the list.")
    dirs_commands = dirs.add_subparsers(dest="dirs_command")
    dirs_list = dirs_commands.add_parser("list", help="the directories, in order, and whether each is used")
    for listing_parser in (dirs, dirs_list):
        listing_parser.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    dirs_commands.add_parser("add", help="add a directory (after the others)").add_argument("directory")
    dirs_commands.add_parser("remove", help="remove a directory").add_argument("directory")
    args = p.parse_args(argv)
    try:
        if args.command == "dirs":
            return dirs_main(args, out)
        if args.command == "categories":
            if args.json:
                out(json.dumps(categories(), indent=2, ensure_ascii=False))
            else:
                for entry in categories():
                    out(f"{entry['id']:<12} {entry['title']['en']}: {entry['description']['en']}")
            return 0
        if args.command == "list":
            damaged = []
            presets = catalog(damaged)
            if args.json:
                out(json.dumps([public(preset) for preset in presets.values()], indent=2))
            else:
                width = max([22, *(len(preset["id"]) for preset in presets.values())])  # (a long id: still a space)
                for category in CATEGORIES:
                    group = [preset for preset in presets.values() if category_of(preset) == category]
                    if group:
                        out(f"{category_text(category)} ({category}):")
                        for preset in sorted(group, key=lambda x: x["id"]):
                            mine = ("  (yours)" if preset["_origin"] == "yours" else
                                    f"  (from {preset['_dir']}"
                                    + (", in place of the built-in one" if preset.get("_shadows") else "") + ")"
                                    if preset["_origin"] == "extra" else "")
                            live = live_light(preset)  # the live check's result: nothing without one
                            shown = f"  {live['symbol']} live {live['date']}" if live else ""
                            out(printable(f"  {preset['id']:<{width}} {preset['title']}{mine}{shown}"))
            for problem in damaged:
                print(f"sandbox-ai presets: skipped {printable(problem)}", file=sys.stderr)
            return 0
        preset = load(args.name)
        if args.command == "show":
            if args.json:
                out(json.dumps(public(preset), indent=2))
            else:
                describe(preset, out)
            return 0
        notes = []
        found = missing(preset, parse_values(args.value), args.agent, notes=notes)
        for line in notes:
            out(printable(f"note: {line}"))
        if found:
            for line in found:
                out(printable(f"missing: {line}"))
            return 1
        out(f"{preset['id']}: ready; start with: sandbox-ai {args.agent or preset.get('agents', AGENTS)[0]} --preset "
            f"{preset['id']}" + "".join(f" --preset-value {shlex.quote(v)}" for v in args.value))
        return 0
    except ValueError as exc:
        print(f"sandbox-ai presets: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
