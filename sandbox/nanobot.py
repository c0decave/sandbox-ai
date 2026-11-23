#!/usr/bin/env python3
"""nanobot (HKUDS/nanobot, MIT, a Python agent with a terminal client and a WebUI) as a sandbox-ai agent: what
upstream does that a sandbox turns off, the switches that turn each part on again (--nanobot-enable, the form of
Hermes' --hermes-enable), and sandbox-ai's settings for nanobot.

nanobot reads one file, ~/.nanobot/config.json; its NANOBOT_* variables count only when that file does not exist, and
nanobot writes the file itself (onboarding, the WebUI's settings). So the launcher writes its settings for each start
into the session directory, mounted read-only at /etc/nanobot (sandbox-ai.json), together with PIN_SCRIPT
(pin_config.py). The image's nanobot command runs that script before nanobot every time (Containerfile.nanobot): it
lays sandbox-ai's settings over the user's file, the user's other keys stay. What a feature opens on the network is
held by the firewall and the connection proxy, not by the file.

The review behind every default here: docs/NANOBOT_REVIEW.md (nanobot 0.4.0).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/nanobot-sandbox (Containerfile.nanobot): the newest one at least 14
# days old when it was taken in. Its PyPI wheels (one per platform: each carries the terminal client), their SHA-256
# in Containerfile.nanobot.lock with every dependency's (scripts/nanobot_lock.py).
VERSION = "0.4.0"
TAG = "v" + VERSION
COMMIT = "8f7a7d17590de7ab187650381894dba2b9e060ab"
REPOSITORY = "https://github.com/HKUDS/nanobot"
WHEELS = {"x86_64": ("nanobot_ai-0.4.0-py3-none-manylinux_2_17_x86_64.whl",
                     "6cd6ad40b9aaf3087f91ef8a0b5484bade1100595493755bef98d23045182a47"),
          "aarch64": ("nanobot_ai-0.4.0-py3-none-manylinux_2_17_aarch64.whl",
                      "4a4e6adb481718b06cb9e3acf4d0fc72a8ef00132a54ec457d04d265cfc7629d")}
MANAGED_DIR = "/etc/nanobot"
SETTINGS = MANAGED_DIR + "/sandbox-ai.json"  # sandbox-ai's settings for this start (read-only)
PIN_FILE = MANAGED_DIR + "/pin_config.py"  # lays them over CONFIG before every start (the image's nanobot command)
SESSION_DIR = "nanobot"  # in the session directory, mounted read-only at MANAGED_DIR
CONFIG = "/root/.nanobot/config.json"  # nanobot's own file (the user's, in the sandbox HOME)
NAME = "nanobot"
LOOPBACK_HOST = "127.0.0.1"
WEB_PORT = 8765  # the WebUI's and the terminal client's WebSocket (channels.websocket)
GATEWAY_PORT = 18790  # the gateway's health endpoint
API_PORT = 8900  # nanobot serve, the OpenAI-compatible API
TIKTOKEN_DIR = "/opt/nanobot/tiktoken"  # the tokenizer's vocabulary, fetched once at build time (read-only)
# Reasoning effort (agents.defaults.reasoningEffort; nanobot passes it to the provider).
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

# What --nanobot-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host), the secrets
# it gives nanobot as variables (--secret-env NAME=SECRET; sandbox-ai secrets set SECRET) and the settings it changes.
FEATURES = {
    "scheduler": {
        "hosts": [],
        "config": {"gateway": {"heartbeat": {"enabled": True}}, "agents": {"defaults": {"dream": {"enabled": True}}}},
        "help": "nanobot's scheduled turns: the heartbeat (every 30 minutes a turn on HEARTBEAT.md) and Dream (every "
                "2 hours a memory consolidation turn) in its gateway while the sandbox runs, and nanobot gateway "
                "(--background, restart, stop); they use the model and reach what the sandbox lets through"},
    "gateway:telegram": {
        "hosts": ["api.telegram.org"], "secrets": {"TELEGRAM_BOT_TOKEN": "nanobot_telegram"},
        "config": {"channels": {"telegram": {"enabled": True, "token": "${TELEGRAM_BOT_TOKEN}"}}},
        "help": "control nanobot from Telegram: its gateway polls api.telegram.org with the bot token of the secret "
                "nanobot_telegram while the sandbox runs; who may talk to it: nanobot's pairing (/pairing approve "
                "CODE) or allowFrom in ~/.nanobot/config.json"},
    "web-search": {
        "hosts": ["duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"],
        "help": "web_search without a key of yours: DuckDuckGo (a keyed search provider needs its own host and "
                "--api-key)"},
    "web-reader": {
        "hosts": ["r.jina.ai", "s.jina.ai"],
        "config": {"tools": {"web": {"fetch": {"useJinaReader": True}}}},
        "help": "web_fetch through Jina Reader (r.jina.ai gets every URL first); without it nanobot fetches pages "
                "itself"},
    "updates": {
        "hosts": ["pypi.org", "raw.githubusercontent.com"],
        "help": "nanobot's update check (nanobot update --check, the WebUI's About page); nanobot itself is updated "
                "by a new review and sandbox-ai build nanobot: its install is read-only in the sandbox"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*(?::[a-z]+)?")
GATEWAYS = ("telegram",)

# Telemetry is never switchable for nanobot (the user's rule): no feature, no --telemetry level and no option of the
# start turns it on. nanobot 0.4.0 sends none; its only tracing (Langfuse, with LANGFUSE_SECRET_KEY and a package the
# sandbox cannot install) is off by variables, and its host and those of the analytics and crash reporting services
# a dependency could use are never opened (a start that would open them is refused) and refused by the connection
# proxy in every nanobot run.
TELEMETRY_HOSTS = ("*.langfuse.com", "*.posthog.com", "*.sentry.io")
TELEMETRY_ENV = {
    "LANGFUSE_SECRET_KEY": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_TRACING_ENABLED": "false",
    "OTEL_SDK_DISABLED": "true", "DO_NOT_TRACK": "1",
}
# Its update check (release_feed.py, update.py): refused quietly by the proxy unless --nanobot-enable updates, also
# where a start opens the shared host (raw.githubusercontent.com) for something else.
UPDATE_RULES = (("raw.githubusercontent.com", "/HKUDS/nanobot/**"), ("pypi.org", "/pypi/nanobot-ai/**"))

# The agent's arguments that would undo a default: a subcommand whose feature is off, a server on another address than
# the sandbox's loopback (nanobot serve --host/-H), another configuration file than the one sandbox-ai's settings are
# laid over (--config/-c, --home).
SUBCOMMAND_FEATURES = {"update": "updates", "gateway": "scheduler"}
# nanobot's options that take a value (nanobot --help of the release and its subcommands').
VALUE_OPTIONS = {"--message", "-m", "--session", "-s", "--workspace", "-w", "--config", "-c", "--theme", "--port",
                 "-p", "--gateway-port", "--host", "-H", "--timeout", "-t", "--home"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# sandbox-ai's settings in every sandbox (docs/NANOBOT_REVIEW.md has each one and the source behind it), laid over
# the user's ~/.nanobot/config.json before every start.
BASE_CONFIG = {
    # nanobot's own command sandbox (bubblewrap) stays off: sandbox-ai's sandbox is the boundary.
    "tools": {"exec": {"sandbox": ""},
              # Pages are fetched by nanobot itself, not first by Jina Reader (--nanobot-enable web-reader).
              "web": {"fetch": {"useJinaReader": False}},
              "webuiAllowRemotePackageInstall": False},
    # Every server of nanobot's on the sandbox's loopback, the WebSocket with its token.
    "gateway": {"host": LOOPBACK_HOST, "heartbeat": {"enabled": False}},
    "api": {"host": LOOPBACK_HOST},
    "channels": {"websocket": {"host": LOOPBACK_HOST, "websocketRequiresToken": True},
                 # Off and without a token unless --nanobot-enable gateway:telegram (a token reference left behind
                 # would stop nanobot at its next start without the variable).
                 "telegram": {"enabled": False, "token": ""}},
    # No memory consolidation turns unless --nanobot-enable scheduler.
    "agents": {"defaults": {"dream": {"enabled": False}}},
}

# The provider nanobot starts with when the start gives exactly one of these hosts a key (--api-key HOST=NAME) and no
# --llm: (nanobot's provider, the variable it is given under). The key in the file is the placeholder the agent sees;
# the proxy puts the key in on the wire.
API_PROVIDERS = {"api.anthropic.com": ("anthropic", "ANTHROPIC_API_KEY"),
                 "api.openai.com": ("openai", "OPENAI_API_KEY"),
                 "openrouter.ai": ("openrouter", "OPENROUTER_API_KEY"),
                 "api.deepseek.com": ("deepseek", "DEEPSEEK_API_KEY"),
                 "generativelanguage.googleapis.com": ("gemini", "GEMINI_API_KEY")}

# Lays SETTINGS over nanobot's file (written into the session directory as PIN_FILE and run by the image's nanobot
# command before nanobot; standard library only, Python's isolated mode). Dicts merge, everything else is replaced;
# the file is replaced as a whole (never written through a link the agent put there), private to the user.
PIN_SCRIPT = '''\
"""sandbox-ai: lays /etc/nanobot/sandbox-ai.json over ~/.nanobot/config.json (sandbox/nanobot.py, PIN_SCRIPT)."""
import json
import os
from pathlib import Path
import sys
import tempfile


def merge(base, over):
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def main(settings, target):
    pins = json.loads(Path(settings).read_text())
    target = Path(target)
    current = {}
    try:
        current = json.loads(target.read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError) as exc:
        print(f"sandbox-ai: {target} is not readable JSON ({exc}); nanobot is not started", file=sys.stderr)
        return 1
    if not isinstance(current, dict):
        print(f"sandbox-ai: {target} is not a JSON object; nanobot is not started", file=sys.stderr)
        return 1
    merged = merge(current, pins)
    try:
        try:
            text = json.dumps(merged, indent=2, ensure_ascii=False) + "\\n"
            data = text.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate (an escape such as \\\\ud800 in the file): kept as its escape
            data = (json.dumps(merged, indent=2) + "\\n").encode("ascii")
    except (ValueError, RecursionError) as exc:
        print(f"sandbox-ai: {target} cannot be written again ({exc}); nanobot is not started", file=sys.stderr)
        return 1
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(name, target)
    except OSError as exc:  # (a full disk: no cut temporary file is left beside it)
        try:
            os.unlink(name)
        except OSError:
            pass
        print(f"sandbox-ai: {target} could not be written ({exc}); nanobot is not started", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else here / "sandbox-ai.json",
                  sys.argv[2] if len(sys.argv) > 2 else Path.home() / ".nanobot" / "config.json"))
'''


def merge(base, over):
    """BASE with OVER laid over it leaf by leaf (dicts merge, everything else replaces), as PIN_SCRIPT does."""
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def parse(values):
    """The features of --nanobot-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given;
    "gateway:all" names every chat channel. ValueError names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            names = [f"gateway:{kind}" for kind in GATEWAYS] if name == "gateway:all" else [name]
            for item in names:
                if not FEATURE_SYNTAX.fullmatch(item) or item not in FEATURES:
                    raise ValueError(f"--nanobot-enable {part.strip()!r}: not a nanobot feature; one of "
                                     f"{', '.join(FEATURES)}")
                if item not in found:
                    found.append(item)
    return found


def features(a):
    """The features this start turns on (none when it is not nanobot's)."""
    return parse(getattr(a, "nanobot_enable", None)) if getattr(a, "agent", None) == "nanobot" else []


def hosts(names):
    """The hosts the features NAMES open, each once."""
    return list(dict.fromkeys(host for name in names for host in FEATURES[name]["hosts"]))


def secrets(names):
    """{variable: secret} the features NAMES give nanobot."""
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


def subcommand(options):
    """nanobot's subcommand among OPTIONS: the first word that is neither an option nor an option's value (nanobot -m
    update: a message, no subcommand), or None."""
    skip = False
    for arg in options:
        if skip:
            skip = False
        elif arg in VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def refused_arg(args, names=()):
    """(argument, why) of the first agent argument that a sandbox refuses with the features NAMES, or None."""
    options = agent_options(args or [])
    first = subcommand(options)
    if first in SUBCOMMAND_FEATURES and SUBCOMMAND_FEATURES[first] not in names:
        feature = SUBCOMMAND_FEATURES[first]
        return first, f"nanobot {first} needs --nanobot-enable {feature} ({FEATURES[feature]['help'].split(':')[0]})"
    for arg in options:
        # (-cFILE too: click takes a short option's value attached)
        if arg in ("--config", "-c", "--home") or arg.startswith(("--config=", "--home=", "-c")):
            return arg, "sandbox-ai's settings are laid over ~/.nanobot/config.json; nanobot keeps that file"
    for index, arg in enumerate(options):
        value = (options[index + 1] if index + 1 < len(options) else "") if arg in ("--host", "-H") else \
            arg.split("=", 1)[1] if arg.startswith("--host=") else arg[2:] if arg.startswith("-H") else None
        if value is not None and value not in LOOPBACK:
            return arg, "nanobot's servers listen on the sandbox's loopback only (127.0.0.1)"
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what nanobot cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "nanobot_enable", None) and a.agent != "nanobot" and "nanobot" not in companions:
        raise ValueError(f"--nanobot-enable is for nanobot (sandbox-ai nanobot, or --with nanobot), not {a.agent}")
    if a.agent != "nanobot" and "nanobot" not in companions:
        return
    names = parse(getattr(a, "nanobot_enable", None)) if a.agent == "nanobot" or "nanobot" in companions else []
    if hooks_active:
        raise ValueError("rules for the agent's own tools (a ruleset's \"tool\" rules, the security mode "
                         "paranoid's) cannot be enforced for nanobot: version 0.4.0 has hooks only for Python programs "
                         "that use its SDK (AgentHook, whose errors it swallows), none its configuration or a start "
                         "can install; start without them, in the mode high or laxer (the network, MCP and device "
                         "rules hold as for every agent: its web fetches meet the HTTP rules at the connection "
                         "proxy)")
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "nanobot" else [], names)
    if refused:
        raise ValueError(f"{refused[0]} in nanobot's arguments: {refused[1]}")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--nanobot-enable {','.join(names)} needs the network; drop --offline")


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
    """nanobot's telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS (--allow-host,
    a wildcard over it), let it pass the proxy unseen (--no-inspect), or open everything without the proxy that
    refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"nanobot's telemetry is always off: {', '.join(opened)} is never opened for nanobot; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"nanobot's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse it; "
                         "drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("nanobot's telemetry is always off: --egress-all-allow without the connection proxy would "
                         "let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules(a=None):
    """The proxy's rules for every nanobot run (the ruleset "telemetry"): each of rule_hosts() refused, and the update
    check refused quietly unless --nanobot-enable updates."""
    rules = [{"id": f"telemetry-nanobot-{number}", "host": host, "path": "/**", "decision": "deny",
              "reason": "nanobot's telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]
    if a is None or "updates" not in features(a):
        rules += [{"id": f"nanobot-updates-{number}", "host": host, "path": path, "decision": "deny",
                   "reason": "nanobot's update check (--nanobot-enable updates opens it)"}
                  for number, (host, path) in enumerate(UPDATE_RULES, 1)]
    return rules


def mcp_servers(names, bridge_args, timeouts=None, timeout=30):
    """nanobot's tools.mcpServers for the --mcp servers NAMES: each a stdio server that runs the launcher's bridge
    (BRIDGE_ARGS(name)), with its wait for one call (toolTimeout, seconds) from TIMEOUTS ({name: seconds}, the
    launcher's mcp_tool_timeouts), else TIMEOUT (nanobot's own default)."""
    timeouts = timeouts or {}
    return {name: {"type": "stdio", "command": "python3", "args": list(bridge_args(name)),
                   "toolTimeout": timeouts.get(name, timeout)}
            for name in dict.fromkeys(names)}


def api_provider(a, placeholders=None):
    """(nanobot's provider, the placeholder its key is) for this start's --api-key (API_PROVIDERS), or None: none,
    several, an --llm, or a key under another variable than the one named there. PLACEHOLDERS: {variable: what the
    agent sees} (the launcher's api_key_environment)."""
    if getattr(a, "llm", None):
        return None
    found = {}
    for value in getattr(a, "api_key", None) or []:
        host, _, rest = str(value).partition("=")
        name = rest.partition("=")[0]
        if host.lower() in API_PROVIDERS and API_PROVIDERS[host.lower()][1] == name:
            found[API_PROVIDERS[host.lower()][0]] = (placeholders or {}).get(name, "")
    return next(iter(found.items())) if len(found) == 1 else None


def managed_config(a, mcp=None, llm=None, placeholders=None):
    """sandbox-ai's settings for this start (SETTINGS): the sandbox's defaults, what the features turn on again, the
    provider of the start's --api-key, --model and --effort, the --mcp servers (MCP: their tools.mcpServers) and
    --llm's model (LLM: llm.nanobot_config). Later layers win."""
    config = merge({}, BASE_CONFIG)
    for name in features(a):
        config = merge(config, FEATURES[name].get("config", {}))
    provider = api_provider(a, placeholders)
    if provider:
        config = merge(config, {"providers": {provider[0]: {"apiKey": provider[1]}},
                                "agents": {"defaults": {"provider": provider[0]}}})
    defaults = {}
    if getattr(a, "model", None):
        defaults["model"] = a.model
    if getattr(a, "effort", None):
        defaults["reasoningEffort"] = a.effort
    if defaults:
        config = merge(config, {"agents": {"defaults": defaults}})
    if mcp:
        config = merge(config, {"tools": {"mcpServers": mcp}})
    if llm:
        config = merge(config, llm)
    shown = thinking(a)
    if shown is not None:
        # --show-thinking: the channels' showReasoning (the CLI's and every channel's) and the websocket channel's
        # own, which wins over it for the terminal client and the WebUI (channels/manager.py: _resolve_bool_override).
        config = merge(config, {"channels": {"showReasoning": shown, "websocket": {"showReasoning": shown}}})
    return config


def thinking(a):
    """--show-thinking of this start: True (on), False (off), None (nanobot's own default: shown). nanobot 0.4.0 sends
    its model's reasoning to the terminal and to a channel only while showReasoning is true (config/schema.py:
    ChannelsConfig.show_reasoning; cli/agent.py, channels/manager.py)."""
    value = getattr(a, "show_thinking", None)
    return None if value is None or getattr(a, "helper_of", None) else value == "on"


NOTE_SKILL = "sandbox-ai-network"  # the run's network note (agent_sandbox.nanobot_note_skill), in every turn


def note_skill(text):
    """SKILL.md of the run's network note (how this run reaches the network through its Tor or VPN) for nanobot:
    always (agent/skills.py get_always_skills), so its text is in nanobot's system prompt every turn."""
    return ("---\nname: " + NOTE_SKILL + "\ndescription: " + json.dumps("How this run reaches the network (sandbox-ai: "
            "its Tor or VPN); in every turn.") + "\nalways: true\n---\n\n" + text.strip() + "\n")


def files(a, config):
    """{path in the session directory: content}: sandbox-ai's settings (SETTINGS) and the script that lays them over
    nanobot's file (PIN_FILE)."""
    return {f"{SESSION_DIR}/{SETTINGS.rsplit('/', 1)[1]}": json.dumps(config, indent=2) + "\n",
            f"{SESSION_DIR}/{PIN_FILE.rsplit('/', 1)[1]}": PIN_SCRIPT}


def environment(a):
    """nanobot's variables: the terminal client of the wheel (never a download of another), the tokenizer's
    vocabulary in the image, the telemetry opt-outs."""
    return {"NANOBOT_TUI_NO_DOWNLOAD": "1", "TIKTOKEN_CACHE_DIR": TIKTOKEN_DIR, **TELEMETRY_ENV}


def command_args(args):
    """nanobot's arguments: as given; nanobot webui without --no-open gets it (there is no browser in the agent's
    container: the WebUI is for the sandbox's own browser, --browser, and the app)."""
    options = agent_options(args)
    if subcommand(options) == "webui" and "--no-open" not in options:
        position = args.index("webui") + 1
        return [*args[:position], "--no-open", *args[position:]]
    return list(args)


def plan(a):
    """The dry run's "nanobot": the reviewed release, the settings' place, what is on and what stays off."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "repository": REPOSITORY,
            "settings": SETTINGS, "config": CONFIG, "own_sandbox": "off",
            "loopback": {"webui": f"{LOOPBACK_HOST}:{WEB_PORT}", "gateway": f"{LOOPBACK_HOST}:{GATEWAY_PORT}",
                         "api": f"{LOOPBACK_HOST}:{API_PORT}"},
            # Never switchable: opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV)},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"],
                         "secrets": sorted(FEATURES[name].get("secrets", {}).values())} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """nanobot has no login in sandbox-ai: its providers' keys come with --api-key or --llm."""
    return False


def help_text():
    """--nanobot-enable's help: every feature with what it does."""
    return ("nanobot only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
