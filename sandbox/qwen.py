#!/usr/bin/env python3
"""Qwen Code (QwenLM/qwen-code, Apache-2.0, a fork of Gemini CLI) as a sandbox-ai agent: what upstream does that a
sandbox turns off, the switches that turn each part on again (--qwen-enable, the form of Hermes' --hermes-enable), and
Qwen Code's system settings.

Qwen Code reads system settings that override the user's and the workspace's from /etc/qwen-code/settings.json
(QWEN_CODE_SYSTEM_SETTINGS_PATH). The launcher writes them into the session directory and mounts them read-only at
/etc/qwen-code, like the other agents' managed settings. "$version" is the settings format's current one, so Qwen Code
never tries to migrate (rewrite) the file.

The review behind every default here: docs/QWEN_CODE_REVIEW.md (Qwen Code 0.26.4).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/qwen-sandbox (Containerfile.qwen): the newest one at least 14 days
# old when it was taken in. PACKAGE@VERSION from npm, its dependencies locked with versions published before
# LOCK_BEFORE (scripts/agent_npm_lock.py, Containerfile.qwen.lock).
PACKAGE = "@qwen-code/qwen-code"
VERSION = "0.26.4"
COMMIT = "715a550cfca0135e00d4015d1ca82a8167e81fff"
INTEGRITY = ("sha512-YeRiUaH3d+JxPiYUS4eGalsKRTuIEUo75JWPWdVBoptdbAH3FJdWbGtjs/syECEUTH5qk0nu8tRb1HMEKn0I2Q==")
LOCK_BEFORE = "2026-10-30T00:00:00Z"
MANAGED_DIR = "/etc/qwen-code"
SETTINGS = MANAGED_DIR + "/settings.json"
SESSION_DIR = "qwen"  # in the session directory, mounted read-only at MANAGED_DIR
SETTINGS_VERSION = 4  # Qwen Code 0.26's settings format: a file at it is never migrated (written)
NAME = "Qwen Code"
KEY_VARIABLE = "OPENAI_API_KEY"  # where Qwen Code's OpenAI-compatible auth reads its key (--llm: the placeholder)

# What --qwen-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host), the system
# settings it changes and the variables it no longer sets.
FEATURES = {
    "updates": {
        "hosts": ["registry.npmjs.org"],
        "settings": {"general": {"enableAutoUpdate": True}},
        "help": "Qwen Code's update check and its notice (npm's registry); Qwen Code itself is updated by "
                "rebuilding the image (sandbox-ai build qwen): its install is read-only in the sandbox"},
    "model-catalog": {
        "hosts": ["models.dev"],
        "drops": ["QWEN_CODE_MODELS_DEV_REFRESH"],
        "help": "the daily refresh of the model catalog (context windows, prices) from models.dev; without it the "
                "catalog bundled with Qwen Code serves"},
    "extensions": {
        "hosts": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                  "objects.githubusercontent.com", "registry.npmjs.org"],
        "help": "qwen extensions install|update from GitHub, Git and npm (installed extensions load without it)"},
    # Qwen Code 0.26.4 runs an event's hooks project, user, system - sandbox-ai's rule hook -, then the extensions'
    # (HookRegistry.getSourcePriority) and the last updatedInput wins (HookAggregator.mergeWithOrLogic): an
    # extension's hook could rewrite a call after the check. While rules hold, no extension loads (--extensions none:
    # extension_args) unless the human trusts them here.
    "extension-hooks": {
        "hosts": [],
        "help": "the installed extensions while rules for Qwen Code's own tools hold, with their hooks - which Qwen "
                "Code runs after sandbox-ai's rule check, so they could rewrite a call after it; without it a start "
                "with rules runs Qwen Code with --extensions none"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*")
TRUSTS = ("extension-hooks",)  # features that trust something of the HOME and open no host

# Telemetry is never switchable for Qwen Code (the user's rule): no feature, no --telemetry level and no option of
# the start turns it on. Its usage statistics (on upstream: Alibaba Cloud RUM) and its OpenTelemetry export are
# pinned off in the system settings, the variables say the same (QWEN_USAGE_STATISTICS_ENABLED wins over the
# setting), the agent's own --telemetry options (which win over both) are refused, and the hosts are never opened
# and refused by the connection proxy in every Qwen Code run. play.googleapis.com is Gemini CLI's Clearcut logger,
# gone from this fork but blocked all the same.
TELEMETRY_HOSTS = ("*.rum.aliyuncs.com", "play.googleapis.com")
TELEMETRY_ENV = {
    "QWEN_USAGE_STATISTICS_ENABLED": "false", "QWEN_TELEMETRY_ENABLED": "false",
    "GEMINI_TELEMETRY_ENABLED": "false",  # Gemini CLI's name for the same switch (harmless where unread)
    "OTEL_SDK_DISABLED": "true", "DO_NOT_TRACK": "1",
}
TELEMETRY_PINNED = {
    "privacy": {"usageStatisticsEnabled": False},
    "telemetry": {"enabled": False, "logPrompts": False, "includeSensitiveSpanAttributes": False},
}
# The agent's arguments that would undo a pin: the telemetry options (they win over settings and variables) and
# Qwen Code's own sandbox (a container of the host's engine, outside this sandbox).
REFUSED_ARGS = {"--telemetry": "telemetry is always off for Qwen Code",
                "--sandbox": "Qwen Code's own sandbox (Docker, Podman, Seatbelt) would run it outside this one",
                "-s": "Qwen Code's own sandbox (Docker, Podman, Seatbelt) would run it outside this one"}

# Qwen Code's system settings in every sandbox (docs/QWEN_CODE_REVIEW.md has each switch and the source behind it).
BASE_SETTINGS = {
    "$version": SETTINGS_VERSION,
    # Its own sandbox stays off: Docker, Podman and Seatbelt would run the agent outside this one (and the image
    # has none of them). QWEN_SANDBOX=false and SANDBOX=sandbox-ai say the same.
    "tools": {"sandbox": False},
    # No IDE in the sandbox to connect to.
    "ide": {"enabled": False},
}
# In every start (telemetry is always off): no update check unless a feature enables it. Telemetry itself is
# TELEMETRY_PINNED, in every start.
TELEMETRY_SETTINGS = {"general": {"enableAutoUpdate": False}}
# QWEN_CODE_SYNCHRONIZED_OUTPUT=1: Qwen Code wraps everything it writes at once in a synchronized update (DEC mode
# 2026), not only Ink's frames - also its clearing and repaint after a resize. It leaves that out whenever TMUX is set,
# which in a sandbox it always is (sessions run in tmux): tmux 3.3 ignores the mode (harmless), tmux 3.7 and later (a
# host's, with the bwrap backend) holds the pane's redraw until the update's end. The terminal relay makes every
# redraw whole for the attached terminal either way (runtime.TerminalBursts).
BASE_ENV = {"QWEN_CODE_SYSTEM_SETTINGS_PATH": SETTINGS, "QWEN_SANDBOX": "false", "SANDBOX": "sandbox-ai",
            "QWEN_CODE_MODELS_DEV_REFRESH": "off", "QWEN_CODE_SYNCHRONIZED_OUTPUT": "1"}
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")  # model.reasoningEffort
# -D (--dangerously-skip-permissions): Qwen Code's own approvals off - YOLO from its system settings, which win over
# the user's and the project's, for every Qwen Code in the sandbox (not only the first process, which also gets
# --yolo: a `qwen` typed in a shell, a restart), and folder trust off, so /work counts as trusted: with folder trust
# on (a ~/.qwen/settings.json of the kept HOME) Qwen Code 0.26.4 turns every approval mode but default and plan into
# default in a folder it does not trust ('Approval mode overridden to "default" because the current folder is not
# trusted'), --yolo included. Only Qwen Code's own prompts go: the sandbox, its proxy and its rules hold as ever.
DANGER_SETTINGS = {"tools": {"approvalMode": "yolo"}, "security": {"folderTrust": {"enabled": False}}}
# model.maxSessionTurns in every start (the user's choice): every model request of the session counts (a prompt and
# each tool result's follow-up; Qwen Code 0.26.4's LlmClient.sessionTurnCount); past it the session stops. --llm with
# the loop guard's steps set (defaults.json, the server's, --llm-param loop_guard.steps=N) takes those instead.
MAX_SESSION_TURNS = 120
# --llm: a large local model on a long context may take minutes before its first token ("No stream activity for
# 240000ms" was the user's LM Studio). The waits come from one value, llm.agent_wait: the provider entry's
# generationConfig (llm.qwen_generation) and the variables (llm.qwen_wait_environment, in llm.agent_environment).


def merge(base, over):
    """BASE with OVER laid over it leaf by leaf (dicts merge, everything else replaces)."""
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def parse(values):
    """The features of --qwen-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given. ValueError
    names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not FEATURE_SYNTAX.fullmatch(name) or name not in FEATURES:
                raise ValueError(f"--qwen-enable {part.strip()!r}: not a Qwen Code feature; one of "
                                 f"{', '.join(FEATURES)}")
            if name not in found:
                found.append(name)
    return found


def features(a):
    """The features this start turns on (none when it is not Qwen Code's)."""
    return parse(getattr(a, "qwen_enable", None)) if getattr(a, "agent", None) == "qwen" else []


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


def refused_arg(args):
    """(argument, why) of the first agent argument that would undo a pin, or None."""
    for arg in agent_options(args or []):
        name = arg.split("=", 1)[0]
        if name == "--telemetry" or name.startswith("--telemetry-"):
            return arg, REFUSED_ARGS["--telemetry"]
        # -s alone or in a cluster of short flags (-ys); --sandbox, --sandbox=docker, --sandbox-image.
        if name in ("--sandbox", "--sandbox-image") or re.fullmatch(r"-[a-zA-Z]*s[a-zA-Z]*", name):
            return arg, REFUSED_ARGS["--sandbox"]
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what Qwen Code cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "qwen_enable", None) and a.agent != "qwen" and "qwen" not in companions:
        raise ValueError(f"--qwen-enable is for Qwen Code (sandbox-ai qwen, or --with qwen), not {a.agent}")
    if a.agent != "qwen" and "qwen" not in companions:
        return
    names = parse(getattr(a, "qwen_enable", None)) if a.agent == "qwen" or "qwen" in companions else []
    if hooks_active:
        off = hooks_off_arg((getattr(a, "agent_args", None) or []) if a.agent == "qwen" else [])
        if off:
            raise ValueError(f"{off} in Qwen Code's arguments: it loads no hook at all, and the rules for Qwen "
                             "Code's own tools (a ruleset's \"tool\" rules) hold only through its hooks; drop it")
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "qwen" else [])
    if refused:
        raise ValueError(f"{refused[0]} in Qwen Code's arguments: {refused[1]}; drop it")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--qwen-enable {','.join(names)} needs the network; drop --offline")


# Qwen Code's arguments that load no hook at all (0.26.4: Config.getDisableAllHooks is true with --bare and
# --safe-mode): refused while rules for its own tools hold, which only its hooks enforce.
HOOKS_OFF_ARGS = ("--bare", "--safe-mode")


def extension_args(a, hooks_active=False):
    """["--extensions", "none"] for Qwen Code while rules for its own tools hold (HOOKS_ACTIVE): no extension
    loads, nor its hooks, which would run after the rule check (FEATURES "extension-hooks"). Nothing when the human
    trusts them (--qwen-enable extension-hooks) or names the extensions in its arguments (--extensions, -e)."""
    if getattr(a, "agent", None) != "qwen" or not hooks_active:
        return []
    try:
        if "extension-hooks" in features(a):
            return []
    except ValueError:
        return []  # (check() refuses the start)
    for arg in getattr(a, "agent_args", None) or []:
        if arg == "--":
            break
        if isinstance(arg, str) and arg.split("=", 1)[0] in ("--extensions", "-e"):
            return []
    return ["--extensions", "none"]


def hooks_off_arg(args):
    """The first of HOOKS_OFF_ARGS (or --NAME=VALUE of one) in Qwen Code's ARGS before a "--", else None."""
    for arg in args:
        if arg == "--":
            return None
        if isinstance(arg, str) and arg.split("=", 1)[0] in HOOKS_OFF_ARGS:
            return arg.split("=", 1)[0]
    return None


def hook_settings(groups):
    """The system settings' part for the run's hooks (GROUPS: {EVENT: [{"matcher", "hooks"}]}, the rule hooks first,
    then the approved kit hooks): every group sequential - Qwen Code then runs all of an event's hooks one after the
    other, the project's and the user's before the system's (HookRegistry's source order), each seeing the input the
    one before rewrote - and hooks never switched off (disableAllHooks, which the system settings decide)."""
    return {"disableAllHooks": False,
            "hooks": {event: [{**entry, "sequential": True} for entry in entries] for event, entries in groups.items()}}


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
    """Qwen Code's telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS
    (--allow-host, a wildcard over it), let it pass the proxy unseen (--no-inspect), or open everything without the
    proxy that refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"Qwen Code's telemetry is always off: {', '.join(opened)} is never opened for Qwen Code; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"Qwen Code's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse "
                         "it; drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("Qwen Code's telemetry is always off: --egress-all-allow without the connection proxy "
                         "would let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules():
    """The proxy's rules for every Qwen Code run (the ruleset "telemetry"): each of rule_hosts() refused."""
    return [{"id": f"telemetry-qwen-{number}", "host": host, "path": "/**", "decision": "deny",
             "reason": "Qwen Code's telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]


def mcp_args(names, bridge_args, timeouts=None):
    """Qwen Code's --mcp-config for the --mcp servers NAMES: each the launcher's stdio bridge (BRIDGE_ARGS(name)),
    with its wait for one call from TIMEOUTS ({name: seconds}, the launcher's mcp_tool_timeouts; Qwen Code counts
    milliseconds); a name not in it keeps Qwen Code's own.

    alwaysLoadTools: the servers' tools are declared to the model directly. Qwen Code 0.26 defers every MCP tool by
    default (DiscoveredMCPTool is shouldDefer; ToolRegistry.getFunctionDeclarations leaves a deferred tool out unless
    its alwaysLoad, which discovery takes from the server's alwaysLoadTools): the model saw only the tool_search and
    tool_call bridge, and a model that does not search first never found the servers the start was given."""
    timeouts = timeouts or {}
    servers = {name: {"command": "python3", "args": list(bridge_args(name)), "alwaysLoadTools": True,
                      **({"timeout": timeouts[name] * 1000} if name in timeouts else {})}
               for name in dict.fromkeys(names)}
    return ["--mcp-config", json.dumps({"mcpServers": servers})] if servers else []


def managed_settings(a, telemetry="off", llm=None, hooks=None):
    """Qwen Code's /etc/qwen-code/settings.json for this start: the sandbox's defaults, the update switch (at every
    level: telemetry is always off), the session's turn limit, what the features turn on again, --effort, --llm's
    model (LLM: llm.qwen_settings; the loop guard's steps where set), YOLO with -D, the run's hooks (HOOKS: the
    launcher's hook groups, hook_settings) and, last, the telemetry pins. Later layers win."""
    settings = merge(merge(merge({}, BASE_SETTINGS), TELEMETRY_SETTINGS), {"model": {"maxSessionTurns":
                                                                                     MAX_SESSION_TURNS}})
    for name in features(a):
        settings = merge(settings, FEATURES[name].get("settings", {}))
    if getattr(a, "effort", None):
        settings = merge(settings, {"model": {"reasoningEffort": a.effort}})
    if llm:
        settings = merge(settings, llm)
    if getattr(a, "dangerously_skip_permissions", False):
        settings = merge(settings, DANGER_SETTINGS)
    if hooks:
        settings = merge(settings, hook_settings(hooks))
    return merge(settings, TELEMETRY_PINNED)  # after everything: never turned on


def files(a, settings):
    """{path in the session directory: content}: the system settings."""
    return {f"{SESSION_DIR}/settings.json": json.dumps(settings, indent=2) + "\n"}


def environment(a):
    """Qwen Code's variables: where its system settings are, its own sandbox off, the telemetry opt-outs, no model
    catalog download unless enabled, synchronized output also in tmux (--llm's waits: llm.agent_environment)."""
    dropped = {variable for name in features(a) for variable in FEATURES[name].get("drops", [])}
    return {key: value for key, value in {**BASE_ENV, **TELEMETRY_ENV}.items() if key not in dropped}


def plan(a):
    """The dry run's "qwen": the reviewed release, the settings' place, what is on and what stays off."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}",
            "system_settings": SETTINGS, "own_sandbox": False,
            # Never switchable: pinned off, opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": sorted(TELEMETRY_PINNED),
                          "refused_arguments": ["--telemetry", "--telemetry-*"]},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"]} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """A Qwen OAuth login (~/.qwen/oauth_creds.json): an access or a refresh token."""
    return isinstance(document, dict) and any(isinstance(document.get(key), str) and document[key]
                                              for key in ("access_token", "refresh_token"))


def help_text():
    """--qwen-enable's help: every feature with what it does."""
    return ("Qwen Code only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
