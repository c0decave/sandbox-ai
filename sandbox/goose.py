#!/usr/bin/env python3
"""Goose (aaif-goose/goose, formerly block/goose, Apache-2.0, a Rust CLI) as a sandbox-ai agent: what upstream does
that a sandbox turns off, the switches that turn each part on again (--goose-enable, the form of Hermes'
--hermes-enable), Goose's read-only system configuration and its variables.

Goose reads its configuration from the environment first (a variable of the key's name in capitals wins over every
file), then from /etc/goose/config.yaml (the system's), the files of GOOSE_ADDITIONAL_CONFIG_FILES and last the
user's ~/.config/goose/config.yaml, each laid over the one before (crates/goose/src/config/base.rs). So everything
that must hold is a variable of the agent's process (the agent cannot change its own environment; a `goose` it
starts from its shell is sandbox-ai's guard, which sets them again: telemetry_guard.py), and the system
file, which the launcher writes into the session directory and mounts read-only at /etc/goose, carries the
sandbox's defaults: approvals, the --mcp servers as extensions. The user's own file stays theirs.

The review behind every default here: docs/GOOSE_REVIEW.md (Goose 1.57.0).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/goose-sandbox (Containerfile.goose): the newest one at least 14 days
# old when it was taken in. The release's Linux tarballs, each checked against its SHA-256 (Containerfile.goose.lock,
# the digests GitHub's release API gives; the release has no SHA256SUMS file).
VERSION = "1.57.0"
TAG = "v" + VERSION
COMMIT = "30bba95105e59bc3e7df624cf6b4d433690cfe12"
REPOSITORY = "https://github.com/aaif-goose/goose"
ASSETS = {"x86_64": ("goose-x86_64-unknown-linux-gnu.tar.gz",
                     "10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3"),
          "aarch64": ("goose-aarch64-unknown-linux-gnu.tar.gz",
                      "c32605acfd560aeb2adbf17ec359cc0217e28022a7e647b35a6af5dcfca91749")}
MANAGED_DIR = "/etc/goose"
CONFIG = MANAGED_DIR + "/config.yaml"  # Goose's system configuration (the lowest layer, read-only here)
LOOP_FILE = MANAGED_DIR + "/sandbox-ai-loop-guard.md"  # the loop guard's text, every turn (GOOSE_MOIM_MESSAGE_FILE)
SESSION_DIR = "goose"  # in the session directory, mounted read-only at MANAGED_DIR
EXPERIMENTAL = "goose"  # not live-tested yet: the start says so (a note, never a refusal)
NAME = "Goose"
SERVE_PORT = 3284  # goose serve's own default port, on the sandbox's loopback (--goose-enable scheduler)
LOG_DIR = "$HOME/.local/state/goose/logs"  # Goose's own log folder, where the background parts write theirs
# Goose's approvals in a sandbox: smart_approve (it asks before what changes something, reads freely). Upstream's
# default is auto (no questions); -D gives it that again (agent_sandbox.DANGER_ENV).
MODE = "smart_approve"
EFFORTS = ("off", "low", "medium", "high", "max")  # GOOSE_THINKING_EFFORT (goose-provider-types: ThinkingEffort)

# What --goose-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host), the secrets
# it gives Goose as variables (--secret-env NAME=SECRET; sandbox-ai secrets set SECRET) and the part of Goose it runs
# in the background of the session ("serve": goose serve with its scheduler, "gateway": the Telegram gateway).
FEATURES = {
    "scheduler": {
        "hosts": [], "background": "serve",
        "help": "Goose's scheduler: goose serve runs in the background on the sandbox's loopback (127.0.0.1:3284, "
                "a key of its own per start) and runs the recipes goose schedule add set up, while the sandbox "
                "runs; they reach what the sandbox lets through"},
    "gateway:telegram": {
        "hosts": ["api.telegram.org"], "secrets": {"TELEGRAM_BOT_TOKEN": "goose_telegram"}, "background": "gateway",
        "help": "control Goose from Telegram: its gateway runs in the background and polls api.telegram.org with "
                "the bot token of the secret goose_telegram; who may talk to it: goose gateway pair telegram in the "
                "shell window"},
    "local-inference": {
        # (Hugging Face's file CDNs are under hf.co: cdn-lfs.huggingface.co and cdn-lfs-us-1.huggingface.co have
        # no address any more, and a start that pins its hosts failed at their lookup - CI 2026-11-14)
        "hosts": ["huggingface.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co", "cdn-lfs-eu-1.hf.co",
                  "cas-bridge.xethub.hf.co"],
        "help": "Goose's built-in llama.cpp: goose local-models search|download fetch GGUF models from Hugging Face "
                "(the sandbox's processors only: this build has no GPU support); without it, a model server of "
                "yours through --llm"},
    "model-catalog": {
        "hosts": ["models.dev"],
        "help": "the model catalog (context windows, prices) Goose fetches from models.dev at every start; without "
                "it the proxy refuses the download quietly and the catalog it keeps serves"},
    "updates": {
        "hosts": ["github.com", "api.github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com", "tmaproduction.blob.core.windows.net"],
        "help": "goose update: it fetches the newest release and checks its Sigstore provenance; it cannot replace "
                "the read-only install, so Goose itself is updated by a new review and sandbox-ai build goose"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*(?::[a-z]+)?")
GATEWAYS = ("telegram",)  # goose gateway start TYPE (crates/goose/src/gateway: create_gateway)

# Telemetry is never switchable for Goose (the user's rule): no feature, no --telemetry level and no option of the
# start turns it on. Its PostHog usage data (opt-in upstream, asked at the first interactive start with "Yes"
# preselected) and its OpenTelemetry export (a user's otel_exporter_otlp_endpoint) are off by variables that win over
# every configuration file; the PostHog hosts are never opened and the connection proxy refuses them in every Goose
# run.
TELEMETRY_HOSTS = ("*.posthog.com",)
TELEMETRY_ENV = {
    "GOOSE_TELEMETRY_OFF": "1",  # forces it off before any choice is read (posthog.rs: TELEMETRY_DISABLED_BY_ENV)
    "GOOSE_TELEMETRY_ENABLED": "false",  # the choice itself, so the consent dialog is never shown
    "OTEL_SDK_DISABLED": "true",  # otel/otlp.rs: disables every signal, a user's endpoint included
    "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
    "DO_NOT_TRACK": "1",
    # Its Langfuse tracing (tracing/langfuse_layer.rs) starts with these keys (or the INIT_PROJECT_ pair): empty.
    "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY": "",
}
TELEMETRY_PINNED = {"GOOSE_TELEMETRY_ENABLED": False}
# The model catalog Goose downloads at every start (model_catalog.rs, no switch): refused quietly by the proxy unless
# --goose-enable model-catalog, so a start with --ask never asks about it.
CATALOG_HOST = "models.dev"

# The agent's arguments that would undo a default: a subcommand whose feature is off, goose serve on another address
# than the sandbox's loopback, or without its key.
SUBCOMMAND_FEATURES = {"update": "updates", "local-models": "local-inference", "lm": "local-inference",
                       "gateway": "gateway:telegram", "gw": "gateway:telegram"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Goose's system configuration in every sandbox (docs/GOOSE_REVIEW.md has each setting and the source behind it).
BASE_CONFIG = {
    "GOOSE_MODE": MODE,
    # The keyring needs a Secret Service, which the sandbox has not: secrets of `goose configure` go to
    # ~/.config/goose/secrets.yaml in the sandbox's HOME (GOOSE_DISABLE_KEYRING=1 says the same).
    "GOOSE_DISABLE_KEYRING": True,
}


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
    """The features of --goose-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given;
    "gateway:all" names every gateway. ValueError names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            names = [f"gateway:{kind}" for kind in GATEWAYS] if name == "gateway:all" else [name]
            for item in names:
                if not FEATURE_SYNTAX.fullmatch(item) or item not in FEATURES:
                    raise ValueError(f"--goose-enable {part.strip()!r}: not a Goose feature; one of "
                                     f"{', '.join(FEATURES)}")
                if item not in found:
                    found.append(item)
    return found


def features(a):
    """The features this start turns on (none when it is not Goose's)."""
    return parse(getattr(a, "goose_enable", None)) if getattr(a, "agent", None) == "goose" else []


def hosts(names):
    """The hosts the features NAMES open, each once."""
    return list(dict.fromkeys(host for name in names for host in FEATURES[name]["hosts"]))


def secrets(names):
    """{variable: secret} the features NAMES give Goose."""
    return {variable: secret for name in names for variable, secret in FEATURES[name].get("secrets", {}).items()}


def network_options(a):
    """What the features open and give, as the launcher's own options: their hosts as --allow-host, their secrets
    as --secret-env NAME=SECRET (one the command line names already is kept as given). Errors are left to check()."""
    try:
        names = features(a)
    except ValueError:
        return
    if getattr(a, "offline", False):
        return  # check() refuses the combination
    for host in hosts(names):
        if host not in a.allow_host:
            a.allow_host.append(host)
    given = {value.partition("=")[0] for value in getattr(a, "secret_env", [])}
    for variable, secret in secrets(names).items():
        if variable not in given:
            a.secret_env.append(f"{variable}={secret}")


def agent_options(args):
    """The options among the agent's arguments ARGS (before a "--" of their own)."""
    return list(args[:args.index("--")]) if "--" in args else list(args)


def refused_arg(args, names=()):
    """(argument, why) of the first agent argument that a sandbox refuses with the features NAMES, or None."""
    options = agent_options(args or [])
    first = next((arg for arg in options if not arg.startswith("-")), None)
    if first in SUBCOMMAND_FEATURES and SUBCOMMAND_FEATURES[first] not in names:
        feature = SUBCOMMAND_FEATURES[first]
        return first, f"goose {first} needs --goose-enable {feature} ({FEATURES[feature]['help'].split(':')[0]})"
    if first == "serve":
        for index, arg in enumerate(options):
            value = options[index + 1] if arg == "--host" and index + 1 < len(options) else \
                arg.split("=", 1)[1] if arg.startswith("--host=") else None
            if value is not None and value not in LOOPBACK:
                return arg, "goose serve listens on the sandbox's loopback only (127.0.0.1)"
            if arg == "--dangerously-unauthenticated":
                return arg, "goose serve keeps its key (GOOSE_SERVER__SECRET_KEY)"
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what Goose cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "goose_enable", None) and a.agent != "goose" and "goose" not in companions:
        raise ValueError(f"--goose-enable is for Goose (sandbox-ai goose, or --with goose), not {a.agent}")
    if a.agent != "goose" and "goose" not in companions:
        return
    names = parse(getattr(a, "goose_enable", None)) if a.agent == "goose" or "goose" in companions else []
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "goose" else [], names)
    if refused:
        raise ValueError(f"{refused[0]} in Goose's arguments: {refused[1]}")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--goose-enable {','.join(names)} needs the network; drop --offline")


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
    """Goose's telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS (--allow-host,
    a wildcard over it), let it pass the proxy unseen (--no-inspect), or open everything without the proxy that
    refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"Goose's telemetry is always off: {', '.join(opened)} is never opened for Goose; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"Goose's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse it; "
                         "drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("Goose's telemetry is always off: --egress-all-allow without the connection proxy would "
                         "let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules(a=None):
    """The proxy's rules for every Goose run (the ruleset "telemetry"): each of rule_hosts() refused, and the
    model catalog's download refused quietly unless --goose-enable model-catalog."""
    rules = [{"id": f"telemetry-goose-{number}", "host": host, "path": "/**", "decision": "deny",
              "reason": "Goose's telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]
    if a is None or "model-catalog" not in features(a):
        rules.append({"id": "goose-model-catalog", "host": CATALOG_HOST, "path": "/**", "decision": "deny",
                      "reason": "Goose's model catalog download (--goose-enable model-catalog opens it)"})
    return rules


def experimental_note():
    return ("Goose: not live-tested yet (its image and a real start have not passed their live test)")


def extensions(names, bridge_args, timeouts=None, timeout=300):
    """Goose's extensions for the --mcp servers NAMES: each a stdio extension that runs the launcher's bridge
    (BRIDGE_ARGS(name)), with its wait for one call (seconds) from TIMEOUTS ({name: seconds}, the launcher's
    mcp_tool_timeouts), else TIMEOUT (Goose's own default)."""
    timeouts = timeouts or {}
    return {name: {"type": "stdio", "name": name, "cmd": "python3", "args": list(bridge_args(name)),
                   "enabled": True, "bundled": False, "envs": {}, "env_keys": [],
                   "description": f"sandbox-ai's MCP server {name}",
                   "timeout": timeouts.get(name, timeout)}
            for name in dict.fromkeys(names)}


# The provider Goose starts with when the start gives exactly one of these hosts a key (--api-key HOST=NAME) and no
# --llm: (Goose's provider, the variable it reads the key from). In the system configuration, so the user's own
# choice (goose configure) still wins.
API_PROVIDERS = {"api.anthropic.com": ("anthropic", "ANTHROPIC_API_KEY"),
                 "api.openai.com": ("openai", "OPENAI_API_KEY"),
                 "openrouter.ai": ("openrouter", "OPENROUTER_API_KEY"),
                 "generativelanguage.googleapis.com": ("google", "GOOGLE_API_KEY"),
                 "api.x.ai": ("xai", "XAI_API_KEY")}


def api_provider(a):
    """Goose's provider for this start's --api-key (API_PROVIDERS), or None: none, several, an --llm, or a key
    under another variable than the one Goose reads."""
    if getattr(a, "llm", None):
        return None
    found = set()
    for value in getattr(a, "api_key", None) or []:
        host, _, rest = str(value).partition("=")
        name = rest.partition("=")[0]
        if host.lower() in API_PROVIDERS and API_PROVIDERS[host.lower()][1] == name:
            found.add(API_PROVIDERS[host.lower()][0])
    return found.pop() if len(found) == 1 else None


def thinking(a):
    """--show-thinking of this start: True (on), False (off), None (Goose's own default: hidden). Goose 1.57.0 shows
    its model's thinking when GOOSE_CLI_SHOW_THINKING is set to any value, else when its configuration says true
    (crates/goose-cli/src/session/output.rs: thinking_enabled)."""
    value = getattr(a, "show_thinking", None)
    return None if value is None or getattr(a, "helper_of", None) else value == "on"


def managed_config(a, mcp=None):
    """Goose's /etc/goose/config.yaml for this start: the sandbox's defaults, the provider of the start's --api-key,
    the --mcp servers (MCP: their extensions) and, last, the telemetry pin (the variables pin it as well, and they
    win)."""
    config = merge({}, BASE_CONFIG)
    provider = api_provider(a)
    if provider:
        config["GOOSE_PROVIDER"] = provider
    if mcp:
        config = merge(config, {"extensions": mcp})
    if thinking(a) is not None:  # --show-thinking: the configuration's boolean (the variable is set only for on)
        config["GOOSE_CLI_SHOW_THINKING"] = thinking(a)
    return merge(config, TELEMETRY_PINNED)


# The run's hooks in Goose 1.57.0: Goose runs hooks only from plugins (crates/goose/src/hooks: <plugin>/hooks/
# hooks.json, the Open Plugins format, Claude Code's) in the user's plugin folder (~/.agents/plugins) and the
# project's (.agents/plugins); no system layer. sandbox-ai's plugin lies read-only in MANAGED_DIR (HOOKS_PLUGIN); the
# image's goose command (telemetry_guard.goose_hooks) links it into the user's plugin folder at every start and refuses
# to start Goose while the plugin is disabled, shadowed by the project's or moved away (GOOSE_PATH_ROOT). Goose loads
# hooks once, when its agent starts. A PreToolUse hook that fails blocks the call (on_failure: block; Goose's default
# lets it through); Goose's matcher is a regular expression on its own tool names, so "*" is left out (all tools).
HOOKS_PLUGIN = MANAGED_DIR + "/sandbox-ai-hooks"
HOOKS_LINK = "/root/.agents/plugins/sandbox-ai-hooks"  # Goose's user plugin folder (config/paths.rs: Plugins)
HOOK_TIMEOUT = 30  # seconds: Goose's DEFAULT_HOOK_TIMEOUT_SECS


def plugin_hooks(groups):
    """The hooks.json of sandbox-ai's plugin for the launcher's hook GROUPS ({EVENT: [{"matcher", "hooks"}]})."""
    hooks = {}
    for event, entries in groups.items():
        for entry in entries:
            handlers = [{"type": "command", "command": handler["command"],
                         "timeout": handler.get("timeout", HOOK_TIMEOUT),
                         **({"on_failure": "block"} if event == "PreToolUse" else {})} for handler in entry["hooks"]]
            matcher = entry.get("matcher")
            hooks.setdefault(event, []).append({**({"matcher": matcher} if matcher not in (None, "", "*") else {}),
                                                "hooks": handlers})
    return {"hooks": hooks}


def files(a, config, loop_text=None, hooks=None):
    """{path in the session directory: content}: the system configuration (JSON, which YAML reads), the loop
    guard's text when --llm turns it on (llm.goose_environment points GOOSE_MOIM_MESSAGE_FILE at it), and with HOOKS
    (the launcher's hook groups) sandbox-ai's plugin with them (HOOKS_PLUGIN)."""
    found = {f"{SESSION_DIR}/config.yaml": json.dumps(config, indent=2) + "\n"}
    if loop_text:
        found[f"{SESSION_DIR}/{LOOP_FILE.rsplit('/', 1)[1]}"] = loop_text
    if hooks:
        found[f"{SESSION_DIR}/{HOOKS_PLUGIN.rsplit('/', 1)[1]}/hooks/hooks.json"] = \
            json.dumps(plugin_hooks(hooks), indent=2) + "\n"
    return found


def environment(a):
    """Goose's variables: no keyring, the telemetry opt-outs (they win over every configuration file), --model and
    --effort (without --llm, whose own variables come after these)."""
    env = {"GOOSE_DISABLE_KEYRING": "1", **TELEMETRY_ENV}
    if getattr(a, "model", None):
        env["GOOSE_MODEL"] = a.model
    if getattr(a, "effort", None):
        env["GOOSE_THINKING_EFFORT"] = a.effort
    if thinking(a):
        env["GOOSE_CLI_SHOW_THINKING"] = "1"  # (off: never set - any value of it is on)
    return env


def background_script(names):
    """The shell lines that start the features' background parts before Goose itself ($0: Goose's binary)."""
    lines = []
    kinds = {FEATURES[name].get("background") for name in names}
    if kinds - {None}:
        lines.append(f'logs="{LOG_DIR}"; mkdir -p "$logs"')
    if "serve" in kinds:
        # A key of its own per start: goose serve refuses to run without one (unless told not to, which is refused).
        lines.append('GOOSE_SERVER__SECRET_KEY=$(od -An -N24 -tx1 /dev/urandom | tr -d " \\n") '
                     f'"$0" serve --host 127.0.0.1 --port {SERVE_PORT} >>"$logs/sandbox-ai-serve.log" 2>&1 &')
    for kind in GATEWAYS:
        if f"gateway:{kind}" in names:
            variable = next(iter(FEATURES[f"gateway:{kind}"]["secrets"]))
            lines.append(f'[ -n "${variable}" ] && "$0" gateway start --bot-token "${variable}" {kind} '
                         f'>>"$logs/sandbox-ai-gateway-{kind}.log" 2>&1 &')
    return lines


def command(a, binary, args):
    """Goose's command line: Goose itself, or with background features a shell that starts them first and then
    becomes Goose (exec: the session's program is Goose)."""
    lines = background_script(features(a)) if not getattr(a, "helper_of", None) else []
    if not lines:
        return [binary, *args]
    return ["/bin/sh", "-c", "\n".join([*lines, 'exec "$0" "$@"']), binary, *args]


def plan(a):
    """The dry run's "goose": the reviewed release, the configuration's place, what is on and what stays off, and
    whether it is experimental here."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "repository": REPOSITORY, "experimental": True,
            "system_config": CONFIG, "mode": MODE,
            # Never switchable: opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": sorted(TELEMETRY_PINNED)},
            "background": sorted({FEATURES[name]["background"] for name in names if FEATURES[name].get("background")}),
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"],
                         "secrets": sorted(FEATURES[name].get("secrets", {}).values())} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """Goose has no login of its own: its providers' keys come with --api-key or --llm (or goose configure)."""
    return False


def help_text():
    """--goose-enable's help: every feature with what it does."""
    return ("Goose only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")

