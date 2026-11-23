#!/usr/bin/env python3
"""OpenHands (Agent Canvas + Agent Server; OpenHands/OpenHands and OpenHands/software-agent-sdk, MIT) as a sandbox-ai
agent: what upstream does that a sandbox turns off, the switches that turn a part on again (--openhands-enable, the
form of Goose's --goose-enable), the settings the start lays over OpenHands' own, and its variables.

OpenHands today is two packages: Agent Canvas (npm @openhands/agent-canvas), a web UI whose launcher starts the
agent server and its automation backend with uvx (fetched from PyPI at every start upstream), and the agent server
itself (PyPI openhands-agent-server with openhands-sdk, -tools, -workspace; openhands-automation beside it). In the
sandbox both are installed from hash-pinned locks (Containerfile.openhands: the npm lock, Containerfile.openhands
-python.lock), and the image's own `openhands` command (sandbox/openhands_cli.py) starts the Canvas with a uvx of its
own in front of the real one: it runs the installed agent server and automation backend, and nothing else of
OpenHands' (no other version, no git ref, no download). The agent server runs locally (its "local" runtime: the
agent's commands run in this sandbox, which is the boundary; its Docker runtime is refused).

How the human uses it: `sandbox-ai openhands` serves the Canvas web UI on the sandbox's loopback (127.0.0.1:8000),
for the sandbox's own browser (--browser, the same pod and loopback), like DSH's web UI. One task without it:
`sandbox-ai openhands -- task "TEXT"` (also the --helper form).

OpenHands keeps its settings (the model, MCP servers, approvals) in ~/.openhands/settings.json, encrypted, written
through its API. The launcher writes what this start sets (SETTINGS: JSON, in the session directory, mounted
read-only at /etc/openhands) and the image's command applies it through the agent server's API once the server is
up, at every start: the telemetry consent (denied, managed), the approvals, and the start's --llm, --api-key, --model,
--effort and --mcp. What the user changes in the UI afterwards is kept until a start sets that value again.

The review behind every default here: docs/OPENHANDS_REVIEW.md (Agent Canvas 1.34.0, Agent Server 1.58.1).

Standard library only.
"""
import json
import re
import sys

# The releases reviewed and built into localhost/openhands-sandbox (Containerfile.openhands): the newest Agent Canvas
# at least 14 days old when it was taken in, with the agent server it was released with (its config/defaults.json:
# versions.agentServer, versions.automation). Every dependency, npm's and Python's, published before LOCK_BEFORE
# (scripts/agent_npm_lock.py openhands, scripts/openhands_lock.py).
PACKAGE = "@openhands/agent-canvas"
VERSION = "1.34.0"
COMMIT = "6c120073f5a1e1c838ba6fdd9a075c79ff997b98"
INTEGRITY = "sha512-1/XtmD1IcaXevKzjakUQDVCpV4W5f7s1+mP9YTQ6LqaopNsHeR7mVzzlnrKhY6K86xajz7jgI8d4vBrXRCL9Eg=="
SERVER_VERSION = "1.58.1"  # openhands-agent-server, -sdk, -tools, -workspace (one release of the SDK repository)
SERVER_COMMIT = "60192ed54db73ac658ac1a07462916126c32d3a0"
AUTOMATION_VERSION = "1.26.0"  # openhands-automation (it pins openhands-sdk==1.58.1)
AUTOMATION_COMMIT = "3e8c54cb7c4f72f46178acef2ac8d3afc7cbe118"
PYTHON_PACKAGES = (f"openhands-agent-server=={SERVER_VERSION}", f"openhands-sdk=={SERVER_VERSION}",
                   f"openhands-tools=={SERVER_VERSION}", f"openhands-workspace=={SERVER_VERSION}",
                   f"openhands-automation=={AUTOMATION_VERSION}")
PYTHON = "3.12"  # CPython from uv's builds (the Canvas asks uvx for >=3.12,<3.15)
LOCK_BEFORE = "2026-10-31T00:00:00Z"
REPOSITORY = "https://github.com/OpenHands/OpenHands"
SDK_REPOSITORY = "https://github.com/OpenHands/software-agent-sdk"
MANAGED_DIR = "/etc/openhands"
SESSION_DIR = "openhands"  # in the session directory, mounted read-only at MANAGED_DIR
SETTINGS = MANAGED_DIR + "/settings.json"  # what this start lays over OpenHands' settings (openhands_cli.py applies it)
NAME = "OpenHands"
HOME = "/root/.openhands"  # its settings, conversations, keys and the automation database
PROGRAM = "openhands"  # the image's command (sandbox/openhands_cli.py)
# sandbox-ai openhands without arguments: the Canvas on the sandbox's loopback, for the sandbox's own browser
# (--browser: the same pod, the same loopback).
WEB_PORT = 8000
DEFAULT_ARGS = ("--host", "127.0.0.1", "--port", str(WEB_PORT))
WEB_URL = f"http://127.0.0.1:{WEB_PORT}/"
TASK = "task"  # openhands task [--resume] --text=TEXT: one task without the UI (openhands_cli.py)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# OpenHands' approvals in a sandbox: its confirmation mode with the LLM security analyzer (it asks before an action
# the analyzer rates risky). Upstream's default is no confirmation; -D gives it that again.
EFFORTS = ("none", "low", "medium", "high", "xhigh")  # the SDK's LLM.reasoning_effort

# What --openhands-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host) and the
# variables it gives the image's command.
FEATURES = {
    "automations": {
        "hosts": [], "env": {"SANDBOX_AI_OPENHANDS_AUTOMATIONS": "1"},
        "help": "the Canvas' automation backend (scheduled and on-demand automations; openhands-automation on the "
                "sandbox's loopback): it runs them while the sandbox runs, and they reach what the sandbox lets "
                "through; without it the Canvas shows automations as unavailable"},
    "model-catalog": {
        "hosts": ["raw.githubusercontent.com"], "env": {"LITELLM_LOCAL_MODEL_COST_MAP": "False"},
        "help": "LiteLLM's model catalog (context windows, prices), downloaded from raw.githubusercontent.com when "
                "the agent server starts; without it the catalog LiteLLM ships serves"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*")

# Telemetry is never switchable for OpenHands (the user's rule): no feature, no --telemetry level and no option of the
# start turns it on. The Canvas' PostHog analytics (through OpenHands' own reverse proxy z.openhands.dev, and its
# install event, which upstream sends before any consent), the agent server's and the automation backend's PostHog
# exporters, browser-use's PostHog telemetry, Laminar tracing and OpenTelemetry are off by variables the agent cannot
# change, the consent is recorded as denied (managed) in its settings at every start, and their hosts are never
# opened and refused by the connection proxy in every OpenHands run.
TELEMETRY_HOSTS = ("z.openhands.dev", "*.posthog.com", "api.lmnr.ai")
TELEMETRY_ENV = {
    "AGENT_CANVAS_DISABLE_TELEMETRY": "1",  # the Canvas page: window.__AGENT_CANVAS_DO_NOT_TRACK__ (no install event)
    "VITE_DO_NOT_TRACK": "1",  # the Canvas launcher: no PostHog key for the agent server or the automation backend
    "DO_NOT_TRACK": "1",  # the agent server's kill switch (telemetry/policy.py)
    "OH_TELEMETRY_EXPORTER": "none", "OH_TELEMETRY_CONSENT": "denied", "OH_TELEMETRY_CONSENT_MODE": "override",
    "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false",  # browser-use (eu.i.posthog.com)
    "LMNR_PROJECT_API_KEY": "",  # Laminar tracing (api.lmnr.ai) starts only with a key
    "OTEL_SDK_DISABLED": "true", "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    # The SDK traces when any of these names an endpoint (observability/laminar.py: _OBSERVABILITY_ENV_KEYS).
    "OTEL_ENDPOINT": "", "OTEL_EXPORTER_OTLP_ENDPOINT": "", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}
# Laid into OpenHands' settings at every start: the frontend's consent, recorded as denied and managed (no dialog).
TELEMETRY_PINNED = {"misc_settings_diff": {"telemetry": {"consent": "denied", "managed": True}}}

# The variables of every OpenHands run: the reviewed versions only (the image's uvx refuses any other), the local
# runtime only, no editor or browser tool the image does not have, LiteLLM's catalog from its own copy.
BASE_ENV = {
    "OH_AGENT_SERVER_VERSION": SERVER_VERSION, "OH_AUTOMATION_VERSION": AUTOMATION_VERSION,
    "OH_AGENT_SERVER_LOCAL_PATH": "", "OH_AGENT_SERVER_GIT_REF": "", "OH_AUTOMATION_LOCAL_PATH": "",
    "OH_AUTOMATION_GIT_REF": "",
    "OH_CONVERSATION_RUNTIME": "local", "OH_CONVERSATION_RUNTIME_SELECTABLE": "false",
    "OH_ENABLE_VSCODE": "false", "OH_ENABLE_BROWSER": "false", "VITE_ENABLE_BROWSER_TOOLS": "false",
    "OH_BIND_HOST": "127.0.0.1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "OPENHANDS_SUPPRESS_BANNER": "1",
}
# The Canvas' own options a sandbox refuses (the image's command refuses them as well).
REFUSED_ARGS = {"--public": "the Canvas stays on the sandbox's loopback with its key in the page (no public mode)",
                "--automation-ref": "the automation backend is the reviewed release only",
                "--automation-repo": "the automation backend is the reviewed release only",
                "--dynamic": "the Canvas serves its built pages (no Vite dev server in the image)"}
# Variables a start may not set (--env, --secret-env): they would undo a default.
REFUSED_ENV = {"OH_CONVERSATION_RUNTIME", "OH_CONVERSATION_IMAGE", "OH_AGENT_SERVER_LOCAL_PATH",
               "OH_AGENT_SERVER_GIT_REF", "OH_AGENT_SERVER_VERSION", "OH_AUTOMATION_LOCAL_PATH",
               "OH_AUTOMATION_GIT_REF", "OH_AUTOMATION_VERSION", "LOCAL_BACKEND_API_KEY"}


def parse(values):
    """The features of --openhands-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given.
    ValueError names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not FEATURE_SYNTAX.fullmatch(name) or name not in FEATURES:
                raise ValueError(f"--openhands-enable {part.strip()!r}: not an OpenHands feature; one of "
                                 f"{', '.join(FEATURES)}")
            if name not in found:
                found.append(name)
    return found


def features(a):
    """The features this start turns on (none when it is not OpenHands')."""
    return parse(getattr(a, "openhands_enable", None)) if getattr(a, "agent", None) == "openhands" else []


def hosts(names):
    """The hosts the features NAMES open, each once."""
    return list(dict.fromkeys(host for name in names for host in FEATURES[name]["hosts"]))


def network_options(a):
    """What the features open, as the launcher's own options: their hosts as --allow-host. Errors are left to
    check()."""
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
    """(argument, why) of the first agent argument that a sandbox refuses, or None."""
    options = agent_options(args or [])
    if options[:1] == [TASK]:
        return None  # a task's text is the user's
    for index, arg in enumerate(options):
        name = arg.split("=", 1)[0]
        if name in REFUSED_ARGS:
            return name, REFUSED_ARGS[name]
        value = host_value(options, index)
        if value is not None and value not in LOOPBACK:
            return arg, "the Canvas listens on the sandbox's loopback only (127.0.0.1)"
    return None


def host_value(options, index):
    """The address OPTIONS[INDEX] gives the Canvas (--host VALUE, --host=VALUE, -H VALUE, or -HVALUE: Node's parsers
    take a short option's value attached), else None."""
    arg = options[index]
    following = options[index + 1] if index + 1 < len(options) else ""
    if arg in ("-H", "--host"):
        return following
    if arg.startswith("--host="):
        return arg.split("=", 1)[1]
    if arg.startswith("-H"):
        return arg[2:]
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what OpenHands cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "openhands_enable", None) and a.agent != "openhands" and "openhands" not in companions:
        raise ValueError(f"--openhands-enable is for OpenHands (sandbox-ai openhands, or --with openhands), not "
                         f"{a.agent}")
    if a.agent != "openhands" and "openhands" not in companions:
        return
    names = parse(getattr(a, "openhands_enable", None)) if a.agent == "openhands" or "openhands" in companions else []
    if hooks_active:
        raise ValueError("rules for the agent's own tools (a ruleset's \"tool\" rules, the security mode "
                         "paranoid's) cannot be enforced for OpenHands: its agent server 1.58.1 takes hooks only with "
                         "each conversation, from its client (the Canvas sends the project's .openhands/hooks.json, "
                         "which the agent can write, or the conversation's settings), and has no layer a start can "
                         "install for every conversation; start without them, in the mode high or laxer (the "
                         "network, MCP and device rules hold as for every agent: its web fetches meet the HTTP rules "
                         "at the connection proxy)")
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "openhands" else [])
    if refused:
        raise ValueError(f"{refused[0]} in OpenHands' arguments: {refused[1]}")
    given = {value.partition("=")[0] for value in getattr(a, "secret_env", None) or []} | \
        set(getattr(a, "env", None) or [])
    undone = sorted(given & REFUSED_ENV)
    if undone:
        raise ValueError(f"{undone[0]}: sandbox-ai sets it for OpenHands (the reviewed release, its local runtime, "
                         "a key of its own per start); drop it from --env/--secret-env")
    check_telemetry(a)
    if names and getattr(a, "offline", False) and hosts(names):
        raise ValueError(f"--openhands-enable {','.join(names)} needs the network; drop --offline")


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
    """OpenHands' telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS
    (--allow-host, a wildcard over it), let it pass the proxy unseen (--no-inspect), or open everything without the
    proxy that refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"OpenHands' telemetry is always off: {', '.join(opened)} is never opened for OpenHands; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"OpenHands' telemetry is always off: the proxy must see {', '.join(unseen)} to refuse it; "
                         "drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("OpenHands' telemetry is always off: --egress-all-allow without the connection proxy would "
                         "let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules():
    """The proxy's rules for every OpenHands run (the ruleset "telemetry"): each of rule_hosts() refused."""
    return [{"id": f"telemetry-openhands-{number}", "host": host, "path": "/**", "decision": "deny",
             "reason": "OpenHands' telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]


def canvas_port(args):
    """The Canvas' port among its options ARGS: --port VALUE, --port=VALUE, -p VALUE or -pVALUE (Node's parsers take
    a short option's value attached), else WEB_PORT."""
    options = agent_options(list(args or []))
    for index, arg in enumerate(options):
        value = options[index + 1] if arg in ("-p", "--port") and index + 1 < len(options) else \
            arg.split("=", 1)[1] if arg.startswith("--port=") else \
            arg[2:] if arg.startswith("-p") and arg[2:].isdigit() else None
        if value is not None and value.isdigit() and 0 < int(value) < 65536:
            return int(value)
    return WEB_PORT


def browser_start(a):
    """The page the sandbox's browser starts on for an OpenHands start: its Agent Canvas (the only interactive surface,
    on the sandbox's loopback; the page gets its key there), else None - a task, its help, a helper's or an MCP
    server's sandbox has no Canvas."""
    args = list(getattr(a, "agent_args", None) or [])
    if getattr(a, "agent", None) != "openhands" or getattr(a, "helper_of", None) or \
            getattr(a, "mcp_socket_dir", None) or getattr(a, "probe", False) or getattr(a, "binary", None):
        return None
    if args and (args[0] == TASK or args[0] in ("-h", "--help", "-v", "--version", "--info")):
        return None
    return f"http://127.0.0.1:{canvas_port(args)}/"


def canvas_note(a):
    """The start's note when the Agent Canvas, OpenHands' only interactive surface, runs on the sandbox's loopback with
    nothing that shows it (no --browser; not a task, not a helper), else None: no browser of the host reaches it."""
    args = list(getattr(a, "agent_args", None) or [])
    if getattr(a, "browser", None) or getattr(a, "helper_of", None) or getattr(a, "mcp_socket_dir", None) or \
            getattr(a, "probe", False) or getattr(a, "binary", None):
        return None
    if args and (args[0] == TASK or args[0] in ("-h", "--help", "-v", "--version", "--info")):
        return None
    if sys.platform == "darwin" and getattr(a, "backend", None) == "podman" and not getattr(a, "podman_url", None):
        # (a Mac: the session browser you see is refused there, browser.check)
        return ("OpenHands' Agent Canvas, its web UI, runs on the sandbox's loopback (http://127.0.0.1:8000), which no "
                "browser of this machine reaches, and the session browser's live view (--browser) is Linux-only for "
                "now, not on this Mac: give it one task without the UI: sandbox-ai openhands -- task \"TASK\"")
    return ("OpenHands' Agent Canvas, its web UI, runs on the sandbox's loopback (http://127.0.0.1:8000), which no "
            "browser of this machine reaches: start with --browser to see it in the sandbox's own browser (Podman; "
            "the app's Browser button), or give it one task without the UI: sandbox-ai openhands -- task \"TASK\"")


def mcp_servers(names, bridge_args, timeouts=None):
    """OpenHands' MCP servers (agent_settings.mcp_config) for the --mcp servers NAMES: each a stdio server that runs
    the launcher's bridge (BRIDGE_ARGS(name)), with its wait for one call (seconds) from TIMEOUTS ({name: seconds},
    the launcher's mcp_tool_timeouts); a name not in it keeps OpenHands' own."""
    timeouts = timeouts or {}
    return {name: {"transport": "stdio", "command": "python3", "args": list(bridge_args(name)),
                   "description": f"sandbox-ai's MCP server {name}",
                   **({"timeout": float(timeouts[name])} if name in timeouts else {})}
            for name in dict.fromkeys(names)}


# The provider OpenHands (LiteLLM) uses when the start gives exactly one of these hosts a key (--api-key HOST=NAME)
# and no --llm: (LiteLLM's prefix of the model's name, the variable LiteLLM reads the key from). The placeholder the
# agent sees is set as the LLM's key, so the UI counts it as set; the proxy puts the key in on the wire.
API_PROVIDERS = {"api.anthropic.com": ("anthropic", "ANTHROPIC_API_KEY"),
                 "api.openai.com": ("openai", "OPENAI_API_KEY"),
                 "openrouter.ai": ("openrouter", "OPENROUTER_API_KEY"),
                 "generativelanguage.googleapis.com": ("gemini", "GEMINI_API_KEY"),
                 "api.x.ai": ("xai", "XAI_API_KEY")}


def api_provider(a):
    """(LiteLLM's prefix, the key's variable) for this start's --api-key (API_PROVIDERS), or None: none, several, an
    --llm, or a key under another variable than the one LiteLLM reads."""
    if getattr(a, "llm", None):
        return None
    found = set()
    for value in getattr(a, "api_key", None) or []:
        host, _, rest = str(value).partition("=")
        name = rest.partition("=")[0]
        if host.lower() in API_PROVIDERS and API_PROVIDERS[host.lower()][1] == name:
            found.add(API_PROVIDERS[host.lower()])
    return found.pop() if len(found) == 1 else None


def model_name(model, prefix):
    """MODEL as LiteLLM names it for the provider PREFIX ("anthropic/claude-..."); a name with a provider of its own
    ("openrouter/x/y" for openrouter, "openai/..." for openai) is kept."""
    return model if model.startswith(prefix + "/") else f"{prefix}/{model}"


def llm_settings(a, placeholder=None, llm=None):
    """The LLM part of agent_settings for this start: --llm's server (LLM: llm.openhands_llm), else --model with the
    provider of a single --api-key (its PLACEHOLDER as the key), else --model alone; --effort as the reasoning
    effort. {} when the start sets none of them."""
    settings = {}
    if llm:
        settings.update(llm)
    else:
        provider = api_provider(a)
        if provider and placeholder:
            settings["api_key"] = placeholder
        if getattr(a, "model", None):
            settings["model"] = model_name(a.model, provider[0]) if provider else a.model
    if getattr(a, "effort", None):
        settings["reasoning_effort"] = a.effort
    return settings


def managed_settings(a, mcp=None, llm=None, placeholder=None, loop=None):
    """The settings this start lays over OpenHands' (SETTINGS; openhands_cli.py applies them through the agent
    server's API): the approvals (-D: none), the LLM (llm_settings), the --mcp servers (MCP: mcp_servers), the loop
    guard (LOOP: {"steps": N, "instructions": TEXT}) and, last, the telemetry consent."""
    agent = {}
    model = llm_settings(a, placeholder, llm)
    if model:
        agent["llm"] = model
    if mcp:
        agent["mcp_config"] = mcp
    conversation = {"confirmation_mode": not getattr(a, "dangerously_skip_permissions", False),
                    "security_analyzer": "llm"}
    loop = loop or {}
    if loop.get("steps"):
        conversation["max_iterations"] = int(loop["steps"])
    if loop.get("instructions"):
        agent["agent_context"] = {"system_message_suffix": loop["instructions"]}
    settings = {"agent_settings_diff": agent, "conversation_settings_diff": conversation}
    # The --mcp servers this start set, so the next start removes those it does not set again.
    settings["sandbox_ai"] = {"mcp": sorted(mcp or {})}
    settings.update(json.loads(json.dumps(TELEMETRY_PINNED)))
    return settings


def files(a, settings):
    """{path in the session directory: content}: the settings this start lays over OpenHands'."""
    return {f"{SESSION_DIR}/settings.json": json.dumps(settings, indent=2) + "\n"}


def environment(a):
    """OpenHands' variables: the reviewed versions and the local runtime (BASE_ENV), the telemetry opt-outs (after
    them: they win), and those of the enabled features."""
    env = {**BASE_ENV, **TELEMETRY_ENV}
    for name in features(a):
        env.update(FEATURES[name].get("env", {}))
    return env


def command_args(args):
    """OpenHands' arguments: none given, the Canvas on the sandbox's loopback (DEFAULT_ARGS); Canvas options without
    an address, that address added; a task as given."""
    args = list(args)
    if not args:
        return list(DEFAULT_ARGS)
    if args[0] == TASK or args[0] in ("-h", "--help", "-v", "--version", "--info"):
        return args
    options = agent_options(args)
    if not any(host_value(options, index) is not None for index in range(len(options))):
        args = ["--host", "127.0.0.1", *args]
    return args


def plan(a):
    """The dry run's "openhands": the reviewed releases, the settings' place, the web UI, what is on and off, the
    telemetry."""
    names = features(a)
    args = getattr(a, "agent_args", None) or []
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "agent_server": {"version": SERVER_VERSION, "commit": SERVER_COMMIT, "repository": SDK_REPOSITORY},
            "automation": {"version": AUTOMATION_VERSION, "commit": AUTOMATION_COMMIT},
            "settings": SETTINGS, "home": HOME, "runtime": "local",
            "web_ui": None if args[:1] == [TASK] else WEB_URL,
            "confirmation_mode": not getattr(a, "dangerously_skip_permissions", False),
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": ["misc_settings.telemetry.consent"]},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"]} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """OpenHands has no login of its own: its model's key comes with --api-key or --llm (or its settings page)."""
    return False


def help_text():
    """--openhands-enable's help: every feature with what it does."""
    return ("OpenHands only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
