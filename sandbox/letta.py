#!/usr/bin/env python3
"""Letta Code (letta-ai/letta-code, Apache-2.0, a TypeScript CLI) as a sandbox-ai agent: what upstream does that a
sandbox turns off, the switches that turn each part on again (--letta-enable, the form of Hermes' --hermes-enable),
Letta Code's variables and its start in a sandbox.

Letta Code keeps every setting in files of the user's (~/.letta/settings.json, the project's .letta/); it has no
system layer that wins over them. So everything that must hold is a variable of the agent's process (the agent cannot
change its own environment): the local backend (LETTA_LOCAL_BACKEND_EXPERIMENTAL=1: agents, memory and conversations
in ~/.letta/lc-local-backend of the sandbox's HOME, never Letta Cloud), Letta Cloud's address pointed at a closed
port on the loopback (LETTA_BASE_URL: no call to api.letta.com even where the local backend still asks it), the
telemetry and update opt-outs, no keychain, no nested sandbox of its own. The launcher writes what changes per start
(the --mcp servers, the model a new agent starts with) into the session directory, mounted read-only at /etc/letta;
sandbox-ai's start script there lays the --mcp servers into the agents' own settings and then becomes Letta Code.

The review behind every default here: docs/LETTA_REVIEW.md (Letta Code 0.34.36).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/letta-sandbox (Containerfile.letta): the newest one at least 14 days
# old when it was taken in. PACKAGE@VERSION from npm (its own npm-shrinkwrap.json pins every dependency), locked with
# versions published before LOCK_BEFORE (scripts/agent_npm_lock.py, Containerfile.letta.lock).
PACKAGE = "@letta-ai/letta-code"
VERSION = "0.34.36"
COMMIT = "8f72222dec142dfc2316692d68ada3ab2e439d46"
INTEGRITY = "sha512-zpzMgkFNh99QNuAh9lS423WxOFToLdscUBSnWEt1hTLEwl5vP0L7lT8hdPgCGeD78CqG5lHU7Wn+z6WEvUXfyw=="
LOCK_BEFORE = "2026-10-31T08:00:00Z"
REPOSITORY = "https://github.com/letta-ai/letta-code"
MANAGED_DIR = "/etc/letta"
SESSION_DIR = "letta"  # in the session directory, mounted read-only at MANAGED_DIR
START = MANAGED_DIR + "/sandbox-ai-start.py"  # sandbox-ai's start: the --mcp servers into the agents, then Letta
START_CONFIG = MANAGED_DIR + "/sandbox-ai-start.json"  # what the start script lays in ({"servers": [...], "model"})
EXPERIMENTAL = "letta"  # not live-tested yet: the start says so (a note, never a refusal)
NAME = "Letta Code"
STORAGE = ".letta/lc-local-backend"  # the local backend's agents, memory (MemFS) and conversations in the HOME
MEMORY_NOTES = ".letta/AGENTS.md"  # sandbox-ai's project notes for Letta Code (kits.MEMORY_FILE)
# Letta Cloud's address for a start without the cloud feature: the discard port on the sandbox's loopback (nothing
# listens there), so the calls the local backend still makes to its Letta server (the billing tier at every
# interactive start, the server's version) end at once, inside the sandbox.
CLOSED_URL = "http://127.0.0.1:9"
LLM_PROVIDER = "llama-cpp"  # --llm: Letta Code's OpenAI-compatible provider read from variables (below)
LLM_URL_VARIABLE = "LLAMA_CPP_BASE_URL"
LLM_KEY_VARIABLE = "LLAMA_CPP_API_KEY"
DANGER_ARGS = ["--yolo"]  # -D: no approvals (Letta Code asks before what changes something, upstream's default)

# What --letta-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host) and the
# variables it no longer sets.
FEATURES = {
    "cloud": {
        "hosts": ["api.letta.com", "app.letta.com"],
        "drops": ["LETTA_LOCAL_BACKEND_EXPERIMENTAL", "LETTA_BASE_URL"],
        "help": "Letta Cloud as Letta Code's backend: agents, memory and conversations in your Letta account "
                "(api.letta.com), its sign-in (/login: app.letta.com's device login, kept in the sandbox's HOME) and "
                "its remote computers; without it everything stays in the sandbox's HOME (the local backend). "
                "Telemetry stays off: the proxy refuses its paths on api.letta.com"},
    "updates": {
        "hosts": ["registry.npmjs.org"],
        "drops": ["DISABLE_AUTOUPDATER"],
        "help": "Letta Code's update check at every start (npm's registry); Letta Code itself is updated by a new "
                "review and sandbox-ai build letta: its install is read-only in the sandbox"},
    "skills-install": {
        "hosts": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                  "objects.githubusercontent.com", "clawhub.ai", "registry.npmjs.org"],
        "help": "letta install and letta skills install: skills and mods from GitHub, ClawHub and npm (installed "
                "ones load without it)"},
    # Letta Code 0.34.36 runs the project's hooks before the user's, where sandbox-ai's rule hook is, and the first
    # updatedInput wins (src/hooks): while rules hold, a project with hooks stops the start (START_SCRIPT,
    # project_hooks) unless the human trusts them here.
    "project-hooks": {
        "hosts": [],
        "help": "the project's own hooks (.letta/settings.json, settings.local.json of the workspace) while rules "
                "for Letta Code's own tools hold: Letta Code runs them before sandbox-ai's rule check, and their "
                "rewrite of a call wins; without it a start with rules and such hooks does not start Letta Code"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*")
TRUSTS = ("project-hooks",)  # features that trust something of the workspace and open no host

# Telemetry is never switchable for Letta Code (the user's rule): no feature, no --telemetry level and no option of
# the start turns it on. Its usage and error events (on upstream, also with the local backend: POST
# /v1/metadata/telemetry to its Letta server, api.letta.com) are off by variables (LETTA_CODE_TELEM=0 and
# DO_NOT_TRACK=1, read before anything is queued), the server is a closed port without the cloud feature, the
# connection proxy refuses the telemetry and feedback paths on api.letta.com in every Letta Code run (also with the
# cloud feature) and Scarf's install analytics host (its package's install script never runs: npm ci
# --ignore-scripts) is never opened.
TELEMETRY_HOSTS = ("*.scarf.sh",)
TELEMETRY_PATHS = (("api.letta.com", "/v1/metadata/telemetry/**"), ("api.letta.com", "/v1/metadata/feedback/**"))
TELEMETRY_ENV = {
    "LETTA_CODE_TELEM": "0",  # src/telemetry/index.ts: isTelemetryEnabled
    "DO_NOT_TRACK": "1",  # the same switch there, and Scarf's
    "SCARF_ANALYTICS": "false",  # Scarf's own (its install script never runs here anyway)
}
# Every start's other variables (docs/LETTA_REVIEW.md has each and the source behind it).
BASE_ENV = {
    "LETTA_LOCAL_BACKEND_EXPERIMENTAL": "1",  # the local backend: agents and memory in the HOME, never Letta Cloud
    "LETTA_BASE_URL": CLOSED_URL,
    "DISABLE_AUTOUPDATER": "1",  # no update check (registry.npmjs.org) and no npm install -g at start
    "LETTA_FS_SANDBOX": "0",  # its own bwrap confinement of memory subagents: this sandbox is the boundary
    "LETTA_SKIP_KEYCHAIN_CHECK": "1",  # no Secret Service in a container: secrets stay in the HOME's files
    "LETTA_DISABLE_LSP_DOWNLOAD": "1",  # no language-server download (LETTA_ENABLE_LSP is off anyway)
}

# The agent's arguments that would undo a default: a subcommand or option whose feature is off, Letta's channels
# (their runtimes are npm packages installed at run time) and an App Server off the sandbox's loopback.
SUBCOMMAND_FEATURES = {"update": "updates", "upgrade": "updates", "install": "skills-install",
                       "computers": "cloud", "environments": "cloud", "envs": "cloud", "teleport": "cloud",
                       "sandbox": "cloud", "usage": "cloud"}
OPTION_FEATURES = {"--update": "updates", "--upgrade": "updates", "--computer": "cloud", "--environment": "cloud",
                   "--env": "cloud"}
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def parse(values):
    """The features of --letta-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given. ValueError
    names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not FEATURE_SYNTAX.fullmatch(name) or name not in FEATURES:
                raise ValueError(f"--letta-enable {part.strip()!r}: not a Letta Code feature; one of "
                                 f"{', '.join(FEATURES)}")
            if name not in found:
                found.append(name)
    return found


def features(a):
    """The features this start turns on (none when it is not Letta Code's)."""
    return parse(getattr(a, "letta_enable", None)) if getattr(a, "agent", None) == "letta" else []


def hosts(names):
    """The hosts the features NAMES open, each once."""
    return list(dict.fromkeys(host for name in names for host in FEATURES[name]["hosts"]))


def network_options(a):
    """The features' hosts as the launcher's own --allow-host. Errors are left to check()."""
    try:
        names = features(a)
    except ValueError:
        return
    if getattr(a, "offline", False):
        return  # check() refuses the combination
    for host in hosts(names):
        if host not in a.allow_host:
            a.allow_host.append(host)


def agent_options(args):
    """The options among the agent's arguments ARGS (before a "--" of their own)."""
    return list(args[:args.index("--")]) if "--" in args else list(args)


def option_value(options, index, name):
    """The value of OPTIONS[INDEX] if it is NAME (NAME VALUE or NAME=VALUE), else None."""
    arg = options[index]
    if arg == name:
        return options[index + 1] if index + 1 < len(options) else ""
    return arg.split("=", 1)[1] if arg.startswith(name + "=") else None


def listen_host(url):
    """The host of an App Server's --listen URL (ws://HOST:PORT), or "" for none."""
    rest = str(url).split("://", 1)[-1]
    host = rest.split("/", 1)[0]
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def refused_arg(args, names=()):
    """(argument, why) of the first agent argument that a sandbox refuses with the features NAMES, or None."""
    options = agent_options(args or [])
    first = next((arg for arg in options if not arg.startswith("-")), None)
    if first in SUBCOMMAND_FEATURES and SUBCOMMAND_FEATURES[first] not in names:
        feature = SUBCOMMAND_FEATURES[first]
        return first, f"letta {first} needs --letta-enable {feature} ({FEATURES[feature]['help'].split(':')[0]})"
    for index, arg in enumerate(options):
        name = arg.split("=", 1)[0]
        if name in OPTION_FEATURES and OPTION_FEATURES[name] not in names:
            feature = OPTION_FEATURES[name]
            return arg, f"{name} needs --letta-enable {feature} ({FEATURES[feature]['help'].split(':')[0]})"
        backend = option_value(options, index, "--backend")
        if backend is not None and backend.lower() in ("cloud", "api") and "cloud" not in names:
            return arg, "--backend cloud needs --letta-enable cloud (Letta Cloud as Letta Code's backend)"
        if name in ("--channels", "--install-channel-runtimes"):
            return arg, ("Letta Code's channels install their runtimes from npm when they start (code no review "
                         "pinned); not in a sandbox")
    if first == "server":
        listens = [index for index, arg in enumerate(options) if arg == "--listen" or arg.startswith("--listen=")]
        if not listens and "cloud" not in names:
            return first, ("letta server without --listen connects this sandbox to Letta Cloud as a remote "
                           "computer: it needs --letta-enable cloud")
        for listen in listens:  # every one: the last given is the one it takes
            url = option_value(options, listen, "--listen")
            if url and not url.startswith("-") and listen_host(url) not in LOOPBACK:
                return options[listen], "letta server --listen serves on the sandbox's loopback only (127.0.0.1)"
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what Letta Code cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "letta_enable", None) and a.agent != "letta" and "letta" not in companions:
        raise ValueError(f"--letta-enable is for Letta Code (sandbox-ai letta, or --with letta), not {a.agent}")
    if a.agent != "letta" and "letta" not in companions:
        return
    names = parse(getattr(a, "letta_enable", None)) if a.agent == "letta" or "letta" in companions else []
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "letta" else [], names)
    if refused:
        raise ValueError(f"{refused[0]} in Letta Code's arguments: {refused[1]}")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--letta-enable {','.join(names)} needs the network; drop --offline")


def host_matches_any(host, pattern):
    """HOST and PATTERN (each a name or *.DOMAIN) can name the same server (the launcher's rule)."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
    return host == pattern or bare(host).endswith("." + bare(pattern)) or bare(pattern).endswith("." + bare(host)) \
        or bare(host) == bare(pattern)


def telemetry_hosts_in(values):
    """The TELEMETRY_HOSTS that host names or *.DOMAIN patterns VALUES would reach."""
    return sorted({telemetry for value in values for telemetry in TELEMETRY_HOSTS
                   if host_matches_any(str(value), telemetry)})


def drop_fix(a):
    """The fix of a start that opens a telemetry host: the --allow-domain that opened one (it became --allow-host
    DOMAIN '*.DOMAIN' right after the options were read), else that --allow-host."""
    for value in getattr(a, "allow_domain", None) or []:
        name = str(value).lower()
        name = (name[2:] if name.startswith("*.") else name.lstrip(".")).rstrip(".")
        if name and telemetry_hosts_in([name, "*." + name]):
            return f"drop --allow-domain {name}"
    return "drop that --allow-host"


def check_telemetry(a):
    """Letta Code's telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS
    (--allow-host, a wildcard over it), let a telemetry host or api.letta.com (whose telemetry paths the proxy
    refuses) pass the proxy unseen (--no-inspect), or open everything without the proxy that refuses them
    (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"Letta Code's telemetry is always off: {', '.join(opened)} is never opened for Letta Code; "
                         + drop_fix(a))
    hidden = list(getattr(a, "no_inspect", []) or [])
    unseen = telemetry_hosts_in(hidden) + sorted({host for host, _ in TELEMETRY_PATHS
                                                  if any(host_matches_any(str(value), host) for value in hidden)})
    if unseen:
        raise ValueError(f"Letta Code's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse "
                         "it; drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("Letta Code's telemetry is always off: --egress-all-allow without the connection proxy "
                         "would let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules(a=None):
    """The proxy's rules for every Letta Code run (the ruleset "telemetry"): each of rule_hosts() refused, and the
    telemetry and feedback paths on api.letta.com (the cloud feature opens that host for Letta Cloud itself)."""
    rules = [{"id": f"telemetry-letta-{number}", "host": host, "path": "/**", "decision": "deny",
              "reason": "Letta Code's telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]
    rules += [{"id": f"telemetry-letta-path-{number}", "host": host, "path": path, "decision": "deny",
               "reason": "Letta Code's telemetry is always off"}
              for number, (host, path) in enumerate(TELEMETRY_PATHS, 1)]
    return rules


def experimental_note():
    return "Letta Code: not live-tested yet (its image and a real start have not passed their live test)"


def mcp_servers(names, bridge_args):
    """Letta Code's MCP servers for the --mcp servers NAMES (the agents' "mcpServers" in ~/.letta/settings.json):
    each a stdio server that runs the launcher's bridge (BRIDGE_ARGS(name))."""
    return [{"name": name, "command": "python3", "args": list(bridge_args(name))} for name in dict.fromkeys(names)]


def start_model(a, llm_model=None):
    """The model a new agent of this start gets: --llm's (LLM_MODEL, Letta Code's handle) or --model, else None."""
    return llm_model or getattr(a, "model", None) or None


# The run's hooks in Letta Code: it has no system layer, so sandbox-ai's start lays them into the user's
# ~/.letta/settings.json at every start (and takes out an earlier start's: every hook whose command is sandbox-ai's,
# OWN_COMMANDS), sets hooks.disabled false there (the user's level wins over the project's), and refuses to start
# Letta Code when it cannot. Letta Code 0.34.36 (src/hooks) runs an event's hooks one after the other - the project's
# local file's, the project's, then the user's - each on the call's original input, and the first updatedInput wins:
# so the kits' PreToolUse hooks come first among sandbox-ai's (the wrapper checks a rewrite of theirs), then the rule
# hook, which answers an allowed call with the very input it checked as updatedInput (runtime.hook_client), so a
# user's hook after it cannot rewrite the call. Timeouts in milliseconds.
OWN_COMMANDS = ("/usr/bin/python3 -I /sandbox/runtime.py --internal-", "/sandbox/session/kits/hooks/")


def letta_hooks(groups):
    """Letta Code's "hooks" for the launcher's hook GROUPS ({EVENT: [{"matcher", "hooks": [handler]}]}, the rule hooks
    first): the same groups with timeouts in milliseconds, a PreToolUse rule group after the kits' groups."""
    hooks = {}
    for event, entries in groups.items():
        converted = [{**entry, "hooks": [{**handler, "timeout": handler.get("timeout", 60) * 1000}
                                         for handler in entry["hooks"]]} for entry in entries]
        if event == "PreToolUse":
            rule = [entry for entry in converted
                    if any("--internal-hook letta " in handler["command"] for handler in entry["hooks"])]
            converted = [entry for entry in converted if entry not in rule] + rule
        hooks[event] = converted
    return hooks


def start_config(a, servers=None, llm_model=None, note=None, hooks=None):
    """What the start script lays in: the --mcp servers (SERVERS), the model for an agent it creates, the run's
    network note (NOTE: its file, read by NOTE_MOD at the first turn of each conversation; None: the mod is taken
    out again) and the run's hooks (HOOKS: the launcher's hook groups, letta_hooks; None: an earlier start's are
    taken out)."""
    return {"servers": list(servers or []), "model": start_model(a, llm_model), "cloud": "cloud" in features(a),
            "note": note, "hooks": letta_hooks(hooks) if hooks else None,
            "project_hooks": "project-hooks" in features(a)}


# The network note's place in Letta Code: a mod of sandbox-ai's (Letta Code 0.34.36 loads every .mjs of its global
# mods folder, ~/.letta/mods or LETTA_MODS_DIR, as trusted local code: src/mods/mod-sources.ts). Its turn_start
# handler puts the note file's text as a system reminder before the first user message of each conversation, where
# Letta Code puts its own reminders (src/reminders); Letta Code has no instruction file it reads itself and keeps the
# system prompt per agent. The start script writes the mod for a run with a note and removes it (only its own, by
# NOTE_MARK) for one without.
NOTE_MOD_FILE = "sandbox-ai-network.mjs"
NOTE_MARK = "// sandbox-ai: the run's network note"


# sandbox-ai's start of Letta Code (python3 START LETTA ARGS...): lays the --mcp servers of START_CONFIG into every
# agent of ~/.letta/settings.json (Letta Code keeps MCP servers per agent) - with the local backend it first creates
# the agent a first start would create, so the servers are there from its first turn - takes out the ones an earlier
# start laid in that this start has not, lays the run's hooks into the user's settings (lay_hooks) and then becomes
# Letta Code (exec). It never follows a symlink; whatever goes wrong with the servers or the note, Letta Code starts all
# the same - but not without the hooks the start asked for (the rules for its own tools hold only through them).
START_SCRIPT = r'''#!/usr/bin/env python3
"""sandbox-ai's start of Letta Code: the --mcp servers into the agents' settings, then Letta Code (exec)."""
import json
import os
import subprocess
import sys

CONFIG = "/etc/letta/sandbox-ai-start.json"
NOTE_MOD_FILE = "sandbox-ai-network.mjs"
NOTE_MARK = "// sandbox-ai: the run's network note"
NOTE_MOD = NOTE_MARK + r""" (written by /etc/letta/sandbox-ai-start.py at every start with one, removed by a start
// without): the note file's text as a system reminder before the first user message of each conversation.
import { readFileSync } from "node:fs";

const NOTE = @NOTE@;

function noteText() {
  try {
    return readFileSync(NOTE, "utf8").trim();
  } catch {
    return "";
  }
}

export default function activate(letta) {
  if (!letta?.events?.on || letta.capabilities?.events?.turns === false) return undefined;
  const given = new Set();
  const dispose = letta.events.on("turn_start", (event) => {
    const key = event?.conversationId ?? "";
    if (given.has(key) || !Array.isArray(event?.input)) return;
    const index = event.input.findIndex((item) => item && item.type !== "approval" && item.role === "user");
    const note = index < 0 ? "" : noteText();
    const item = index < 0 ? null : event.input[index];
    if (!note || !(typeof item.content === "string" || Array.isArray(item.content))) return;
    given.add(key);
    const text = `<system-reminder>\n${note}\n</system-reminder>\n\n`;
    const content = typeof item.content === "string" ? text + item.content : [{ type: "text", text }, ...item.content];
    event.input = event.input.map((entry, position) => (position === index ? { ...item, content } : entry));
  });
  return typeof dispose === "function" ? dispose : undefined;
}
"""


def read_json(path):
    try:
        if os.path.islink(path):
            return None
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def write_json(path, document):
    write_text(path, json.dumps(document, indent=2) + "\n")


def write_text(path, text):
    if os.path.islink(path):
        return
    temporary = f"{path}.sandbox-ai.{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


def note_mod(config, home):
    """The network note's mod in Letta Code's global mods folder: written for a run with a note, taken out (only our
    own) for one without."""
    folder = (os.environ.get("LETTA_MODS_DIR") or "").strip() or (os.environ.get("LETTA_EXTENSIONS_DIR") or "").strip() \
        or os.path.join(home, "mods")
    path = os.path.join(folder, NOTE_MOD_FILE)
    note = config.get("note")
    if isinstance(note, str) and note:
        if os.path.islink(folder):
            return
        os.makedirs(folder, mode=0o700, exist_ok=True)
        text = NOTE_MOD.replace("@NOTE@", json.dumps(note))
        try:
            with open(path, encoding="utf-8") as stream:
                if stream.read() == text:
                    return
        except OSError:
            pass
        write_text(path, text)
        return
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as stream:
            ours = stream.read(len(NOTE_MARK)) == NOTE_MARK
    except (OSError, UnicodeDecodeError):
        return
    if ours:
        os.unlink(path)


OWN_COMMANDS = ("/usr/bin/python3 -I /sandbox/runtime.py --internal-", "/sandbox/session/kits/hooks/")


def own_group(group):
    return isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(
        isinstance(hook, dict) and isinstance(hook.get("command"), str) and hook["command"].startswith(OWN_COMMANDS)
        for hook in group["hooks"])


def lay_hooks(config, home):
    """The run's hooks (config "hooks") first in each event's list of the user's settings, hooks on there; an earlier
    start's taken out. True if the settings hold this run's hooks (or none were asked for)."""
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else None
    settings_path = os.path.join(home, "settings.json")
    settings = read_json(settings_path)
    if settings is None:
        if os.path.lexists(settings_path) or not hooks:
            return not hooks  # a symlink or no JSON: never written through
        settings = {}
    if not isinstance(settings, dict):
        return not hooks
    current = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    found = {event: [group for group in groups if not own_group(group)] if isinstance(groups, list) else groups
             for event, groups in current.items()}
    for event, groups in (hooks or {}).items():
        mine = found.get(event)
        found[event] = list(groups) + (mine if isinstance(mine, list) else [])
    if hooks:
        found["disabled"] = False  # the user's level wins over the project's (Letta Code's areHooksDisabled)
    found = {event: groups for event, groups in found.items() if groups != [] or current.get(event) == []}
    if found != current or (not found and "hooks" in settings):
        if found:
            settings["hooks"] = found
        else:
            settings.pop("hooks", None)
        os.makedirs(home, mode=0o700, exist_ok=True)
        write_json(settings_path, settings)
    return read_json(settings_path) == settings if hooks else True


def local(agent):
    return isinstance(agent, dict) and str(agent.get("baseUrl", "")).startswith("local:")


def letta_home():
    return os.environ.get("LETTA_CONFIG_DIR") or os.path.join(os.environ.get("HOME") or "/root", ".letta")


def sync(letta):
    config = read_json(CONFIG) or {}
    servers = [server for server in config.get("servers") or [] if isinstance(server, dict) and server.get("name")]
    home = letta_home()
    settings_path = os.path.join(home, "settings.json")
    names_path = os.path.join(home, "sandbox-ai-mcp.json")
    before = read_json(names_path)
    before = [name for name in before if isinstance(name, str)] if isinstance(before, list) else []
    ours = [server["name"] for server in servers]
    created = None
    if not servers and not before:
        return created
    settings = read_json(settings_path)
    agents = settings.get("agents") if isinstance(settings, dict) else None
    if servers and not config.get("cloud") and not any(local(agent) for agent in agents or []):
        # The agent a first start would create, made now: its servers are there from its first turn.
        model = config.get("model")
        made = subprocess.run([letta, "agents", "create", *(["--model", model] if model else [])],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        try:
            created = json.loads(made.stdout).get("id") if made.returncode == 0 else None
        except (ValueError, AttributeError):
            created = None
        if not (isinstance(created, str) and created.startswith("agent-")):  # (said: its servers would be missing)
            why = [line for line in (made.stderr or made.stdout or "").splitlines() if line.strip()]
            print(f"sandbox-ai: letta agents create did not make the agent for the --mcp servers (exit "
                  f"{made.returncode}{': ' + why[-1][:300] if why else ''}); Letta Code makes one without them",
                  file=sys.stderr)
        settings = read_json(settings_path)
        agents = settings.get("agents") if isinstance(settings, dict) else None
    if not isinstance(agents, list):
        agents = []
    changed = False
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if not isinstance(agent.get("mcpServers", []), list):  # (not Letta Code's form: left as it was written)
            print(f"sandbox-ai: an agent's mcpServers in {settings_path} is no list; the --mcp servers were not laid "
                  "into it", file=sys.stderr)
            continue
        kept = [server for server in agent.get("mcpServers") or []
                if not (isinstance(server, dict) and server.get("name") in set(before) | set(ours))]
        wanted = kept + servers
        if agent.get("mcpServers", []) != wanted:
            if wanted:
                agent["mcpServers"] = wanted
            else:
                agent.pop("mcpServers", None)
            changed = True
    if changed:
        write_json(settings_path, settings)
    os.makedirs(home, mode=0o700, exist_ok=True)
    write_json(names_path, ours)
    return created if isinstance(created, str) and created.startswith("agent-") else None


# The arguments that choose an agent or conversation themselves: then the created agent is not named.
CHOOSING = ("--agent", "-a", "--new-agent", "--name", "-n", "--conv", "--conversation", "--resume", "-r",
            "--ephemeral", "--from-agent")
# Letta Code's options that take a value as the next word: that word is no subcommand. --model above all: the launcher
# puts --model HANDLE before the arguments (--model, --llm), and its handle counted as a subcommand, so a first start
# with --mcp went on without the agent made for it - Letta Code made another one, without the servers (CI 0.1.187).
VALUED = ("--model", "-m", "--agent", "-a", "--name", "-n", "--conv", "--conversation", "--from-agent", "--system",
          "--toolset", "--permission-mode", "--output-format", "--input-format", "--max-turns", "--tools",
          "--allowedTools", "--disallowedTools", "--backend", "--listen", "--memory-blocks", "--init-blocks",
          "--block-value", "--base-tools", "--skills", "--from-af")


def subcommand(options):
    """Letta Code's subcommand among OPTIONS: the first word that is neither an option nor a VALUED option's value,
    or None."""
    skip = False
    for arg in options:
        if skip:
            skip = False
        elif arg in VALUED:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


RULE_HOOK = "--internal-hook letta "
PROJECT_FILES = (".letta/settings.local.json", ".letta/settings.json")  # (Letta Code's order: they run first)


def project_hooks(config, folder):
    """Why Letta Code may not start in FOLDER (its working folder): the project's settings there hold hooks, which
    Letta Code runs before the user's - sandbox-ai's rule hook among them - and whose rewrite of a call wins; only
    while the rules hold (CONFIG's hooks have the rule hook) and the human did not trust them (project_hooks). A file
    that cannot be read as JSON counts as one with hooks (it cannot be checked). None: it may start."""
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
    rules = any(isinstance(group, dict) and any(isinstance(hook, dict) and RULE_HOOK in str(hook.get("command"))
                                                for hook in group.get("hooks") or [])
                for groups in hooks.values() if isinstance(groups, list) for group in groups)
    if not rules or config.get("project_hooks") is True:
        return None
    for name in PROJECT_FILES:
        path = os.path.join(folder, name)
        if not os.path.lexists(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:  # (followed as Letta Code follows it)
                settings = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):
            return f"{name} cannot be checked for hooks (not JSON)"
        found = settings.get("hooks") if isinstance(settings, dict) else None
        events = sorted(event for event, groups in found.items() if isinstance(groups, list) and groups) \
            if isinstance(found, dict) else []
        if events:
            return f"{name} has hooks ({', '.join(events)})"
    return None


def main():
    letta, args = sys.argv[1], sys.argv[2:]
    created = None
    try:
        created = sync(letta)
    except Exception as error:  # noqa: BLE001 - Letta Code starts all the same
        print(f"sandbox-ai: the --mcp servers were not laid into Letta Code's settings: {error}", file=sys.stderr)
    try:
        note_mod(read_json(CONFIG) or {}, letta_home())
    except Exception as error:  # noqa: BLE001 - Letta Code starts all the same
        print(f"sandbox-ai: the network note's mod was not written: {error}", file=sys.stderr)
    config = read_json(CONFIG)
    try:  # an unreadable start configuration may have asked for hooks: refused, never started without them
        laid = isinstance(config, dict) and lay_hooks(config, letta_home())
    except Exception as error:  # noqa: BLE001 - refused below
        print(f"sandbox-ai: {error}", file=sys.stderr)
        laid = False
    if not laid:  # the rules for Letta Code's own tools hold only through these hooks: never started without them
        print("sandbox-ai: Letta Code is not started: the hooks of this run (the rules for its own tools, the kits' "
              f"approved hooks) could not be laid into {os.path.join(letta_home(), 'settings.json')}", file=sys.stderr)
        sys.exit(1)
    found = project_hooks(config, os.getcwd())
    if found:  # they would run before sandbox-ai's rule check, and their rewrite of a call would win
        print(f"sandbox-ai: Letta Code is not started: the project's {found}: Letta Code runs them before sandbox-ai's "
              "rule check, and their rewrite of a call would win. Remove them, or trust them with the start option "
              "--letta-enable project-hooks", file=sys.stderr)
        sys.exit(1)
    options = args[:args.index("--")] if "--" in args else args
    if created and subcommand(options) is None and not any(arg.split("=", 1)[0] in CHOOSING for arg in options):
        args = ["--agent", created, *args]  # the first start goes on with the agent made for it
    # (a name, as the launcher gives it under Podman - "letta" - is looked up in PATH, as sync's subprocess does)
    os.execvp(letta, [letta, *args])


if __name__ == "__main__":
    main()
'''


def files(a, config):
    """{path in the session directory: content}: the start script and what it lays in (CONFIG: start_config)."""
    return {f"{SESSION_DIR}/{START.rsplit('/', 1)[1]}": START_SCRIPT,
            f"{SESSION_DIR}/{START_CONFIG.rsplit('/', 1)[1]}": json.dumps(config, indent=2) + "\n"}


def environment(a):
    """Letta Code's variables: the local backend with Letta Cloud's address closed (unless the cloud feature), no
    update check (unless updates), no keychain, no nested sandbox, the telemetry opt-outs (never dropped)."""
    dropped = {variable for name in features(a) for variable in FEATURES[name].get("drops", [])}
    env = {key: value for key, value in BASE_ENV.items() if key not in dropped}
    env.update(TELEMETRY_ENV)
    return env


def command(a, binary, args):
    """Letta Code's command line: sandbox-ai's start script, which becomes Letta Code."""
    return ["python3", "-I", START, binary, *args]


def model_args(a):
    """The agent's own arguments for --model (without --llm, whose model llm.letta_arguments gives)."""
    model = getattr(a, "model", None)
    return ["--model", model] if model and not getattr(a, "llm", None) else []


def plan(a):
    """The dry run's "letta": the reviewed release, its backend, what is on and what stays off, and whether it is
    experimental here."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "experimental": True, "backend": "cloud" if "cloud" in names else "local",
            # Where its agents, memory and conversations are: the cloud feature drops the local backend.
            "storage": "Letta Cloud (your Letta account, api.letta.com)" if "cloud" in names else "~/" + STORAGE,
            "start": START, "own_sandbox": False,
            # Never switchable: opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "blocked_paths": [f"{host}{path}" for host, path in TELEMETRY_PATHS],
                          "environment": sorted(TELEMETRY_ENV)},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"]} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """Letta Code has no login sandbox-ai takes in: its models' keys come with --api-key or --llm (or letta
    connect); its Letta Cloud sign-in (--letta-enable cloud) stays in the sandbox's HOME."""
    return False


def help_text():
    """--letta-enable's help: every feature with what it does."""
    return ("Letta Code only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
