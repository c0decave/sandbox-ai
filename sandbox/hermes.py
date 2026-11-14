#!/usr/bin/env python3
"""Hermes Agent (NousResearch/hermes-agent, MIT) as sandbox-ai's fourth agent: what upstream does that a sandbox
turns off, the switches that turn each part on again (--hermes-enable), and Hermes' managed configuration.

Hermes reads an administrator's configuration from /etc/hermes (its "managed scope", hermes_cli/managed_scope.py):
config.yaml is laid over the user's ~/.hermes/config.yaml leaf by leaf and the agent cannot change those keys. The
launcher writes it into the session directory and mounts it read-only at /etc/hermes, like Claude Code's and
Codex's managed settings. JSON is YAML, so no YAML writer is needed.

The review behind every default here: docs/HERMES_REVIEW.md (Hermes v0.22.5).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/hermes-sandbox (Containerfile.hermes): the newest one at least 14
# days old when it was taken in.
VERSION = "v0.22.5"
COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
MANAGED_DIR = "/etc/hermes"
SESSION_DIR = "hermes"  # in the session directory, mounted read-only at MANAGED_DIR
SKILLS_DIR = MANAGED_DIR + "/skills"  # sandbox-ai's own skills for Hermes (the loop guard's)
KIT_SKILLS = "/root/.agents/skills"  # the kits' skills (kits.LAYOUT), read-only
# Hermes' CLI starts its gateway itself, in the background (hermes gateway run --idle-exit, a process of this
# sandbox): cron, the Kanban dispatcher and the messaging platforms live there. It stays up while a platform is
# connected or the board has work, and ends itself when idle. Its log: ~/.hermes/logs/.
EXPERIMENTAL = "hermes"  # not live-tested yet: the start says so (a note, never a refusal)
# The placeholder Hermes gets for OpenRouter's key: Hermes ignores an OpenRouter key without OpenRouter's prefix
# (hermes_cli/auth.py, KNOWN_PROVIDER_KEY_PREFIXES), so the proxy's placeholder is shaped like one (llm.py keeps the
# same values: llm.placeholder_for, llm.hermes_config).
OPENROUTER_HOST = "openrouter.ai"
OPENROUTER_PLACEHOLDER = "sk-or-v1-sandbox-ai-placeholder"
MESSAGING = ("telegram", "discord", "slack")

# What --hermes-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host), the
# secrets it gives Hermes as variables (--secret-env NAME=SECRET; sandbox-ai secrets set SECRET), the managed
# configuration it changes and whether it works in Hermes' gateway (a messaging platform, the Kanban dispatcher).
FEATURES = {
    "gateway:telegram": {
        "hosts": ["api.telegram.org"], "secrets": {"TELEGRAM_BOT_TOKEN": "hermes_telegram"}, "gateway": True,
        "help": "control Hermes from Telegram: its gateway polls api.telegram.org with the bot token of the secret "
                "hermes_telegram; who may talk to it: Hermes' pairing (hermes pairing approve telegram CODE in the "
                "shell window)"},
    "gateway:discord": {
        "hosts": ["discord.com", "gateway.discord.gg", "cdn.discordapp.com", "media.discordapp.net"],
        "secrets": {"DISCORD_BOT_TOKEN": "hermes_discord"}, "gateway": True,
        "help": "control Hermes from Discord (the bot token of the secret hermes_discord)"},
    "gateway:slack": {
        "hosts": ["slack.com", "wss-primary.slack.com", "wss-backup.slack.com", "files.slack.com"],
        "secrets": {"SLACK_BOT_TOKEN": "hermes_slack", "SLACK_APP_TOKEN": "hermes_slack_app"}, "gateway": True,
        "help": "control Hermes from Slack in Socket Mode (the secrets hermes_slack, xoxb-, and hermes_slack_app, "
                "xapp-)"},
    "kanban": {
        "hosts": [], "gateway": True,
        "config": {"kanban": {"dispatch_in_gateway": True}},
        "help": "Hermes' Kanban board: its gateway (in the background, without any messaging platform) dispatches "
                "the board's tasks to workers in this sandbox (hermes kanban watch shows them)"},
    "skills-hub": {
        "hosts": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                  "objects.githubusercontent.com", "skills.sh", "clawhub.ai"],
        "help": "hermes skills search|install from the skills hub (GitHub, skills.sh, ClawHub); Hermes scans "
                "what it installs"},
    "web-search": {
        "hosts": ["mcp.exa.ai", "search.parallel.ai", "api.keenable.ai"],
        "config": {"web": {"keyless_fallback": True, "keyless_rescue": True}},
        "help": "web_search and web_extract without a key of yours: the queries go to Exa's, Parallel's and "
                "Keenable's free endpoints"},
    "tool-gateway": {
        "hosts": ["portal.nousresearch.com", "tool-gateway.nousresearch.com", "firecrawl-gateway.nousresearch.com",
                  "fal-queue-gateway.nousresearch.com", "krea-gateway.nousresearch.com",
                  "openai-audio-gateway.nousresearch.com", "connector-gateway.nousresearch.com"],
        "help": "the Nous Tool Gateway and connectors (web search, images, speech through Nous; needs a paid Nous "
                "Portal login); its remote terminal (Modal) stays off"},
    "free-tier": {
        "hosts": ["portal.nousresearch.com", "welcome-api.nousresearch.com"],
        "config": {"nous": {"guest": True}},
        "help": "the Nous free tier: with no provider of yours Hermes creates an anonymous Nous account on first use"},
    "model-catalog": {
        "hosts": ["hermes-agent.nousresearch.com", "models.dev"],
        "config": {"model_catalog": {"enabled": True}},
        "help": "the model lists Hermes fetches (its curated catalog, models.dev for context windows and prices)"},
    "updates": {
        "hosts": ["github.com", "api.github.com"],
        "config": {"updates": {"check": True}, "plugins": {"auto_update_check_hours": 24}},
        "help": "Hermes' update and plugin update checks; Hermes itself is updated by rebuilding the image "
                "(sandbox-ai build hermes): its install is read-only in the sandbox"},
    "lazy-installs": {
        "hosts": ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
        "config": {"security": {"allow_lazy_installs": True}, "lsp": {"install_strategy": "auto"}},
        "help": "packages Hermes installs on demand (speech, documents, language servers) from PyPI and npm"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*(?::[a-z]+)?")

# Telemetry is never switchable for Hermes (the user's rule): no feature, no --telemetry level and no option of the
# start turns it on. Hermes' own (shared metrics to telemetry.nousresearch.com, the OTLP exports, the macOS
# computer-use driver's PostHog events) is pinned off in the managed configuration and its documented opt-out
# variables are set; its hosts and those of the analytics and crash reporting services a dependency could use are
# never opened (a start that would open them is refused) and the connection proxy refuses them in every Hermes run.
TELEMETRY_HOSTS = ("telemetry.nousresearch.com", "*.posthog.com", "*.sentry.io")
TELEMETRY_ENV = {
    "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",  # the computer-use driver's PostHog events (tools/computer_use)
    "OTEL_SDK_DISABLED": "true",  # OpenTelemetry (opentelemetry-api is in the lock; no exporter is)
    "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_TELEMETRY": "1",  # the common opt-outs of tools Hermes may run
}
TELEMETRY_PINNED = {
    "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
    "monitoring": {"gateway_health_export": {"enabled": False}, "export": {"otlp": {"enabled": False}}},
    "computer_use": {"cua_telemetry": False},
}

# Hermes' managed configuration in every sandbox (docs/HERMES_REVIEW.md has each switch and the source behind it).
BASE_CONFIG = {
    # Only the local terminal: docker, ssh, modal, daytona, vercel_sandbox and singularity run the agent's commands
    # outside this sandbox. Pinned here, TERMINAL_ENV=local in the environment, their SDKs are not installed, there is
    # no Docker socket and the firewall opens none of their hosts.
    "terminal": {"backend": "local"},
    # Borrowing ~/.codex and ~/.claude logins would use up the other agents' rotating refresh tokens.
    "auth": {"adopt_external_logins": False},
    # No anonymous Nous account (free tier) unless --hermes-enable free-tier.
    "nous": {"guest": False},
    # No queries to third-party keyless search endpoints unless --hermes-enable web-search.
    "web": {"keyless_fallback": False, "keyless_rescue": False},
    # Nothing installed at run time (PyPI, npm language servers) unless --hermes-enable lazy-installs.
    "security": {"allow_lazy_installs": False},
    "lsp": {"install_strategy": "manual"},
    # Hermes' own egress proxy is for its Docker backend; sandbox-ai's connection proxy is the one in front.
    "proxy": {"enabled": False},
    # A skill's !`cmd` snippets are never run when it is loaded.
    "skills": {"inline_shell": False, "external_dirs": [KIT_SKILLS]},
    # The gateway Hermes' CLI starts dispatches no Kanban task (spawning workers) unless --hermes-enable kanban.
    "kanban": {"dispatch_in_gateway": False},
}
# In every start (telemetry is always off): no update or catalog checks unless a feature enables one. Telemetry itself
# is TELEMETRY_PINNED, in every start.
TELEMETRY_CONFIG = {
    "updates": {"check": False},
    "plugins": {"auto_update_check_hours": 0, "auto_apply": False},
    "model_catalog": {"enabled": False},
}


def merge(base, over):
    """BASE with OVER laid over it leaf by leaf (dicts merge, everything else replaces), as Hermes merges."""
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def parse(values):
    """The features of --hermes-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given;
    "gateway:all" names every messaging platform. ValueError names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            names = [f"gateway:{platform}" for platform in MESSAGING] if name == "gateway:all" else [name]
            for item in names:
                if not FEATURE_SYNTAX.fullmatch(item) or item not in FEATURES:
                    raise ValueError(f"--hermes-enable {part.strip()!r}: not a Hermes feature; one of "
                                     f"{', '.join(FEATURES)} (gateway:all: every platform)")
                if item not in found:
                    found.append(item)
    return found


def features(a):
    """The features this start turns on (none when it is not Hermes')."""
    return parse(getattr(a, "hermes_enable", None)) if getattr(a, "agent", None) == "hermes" else []


def gateway(a):
    """A feature of this start works in Hermes' gateway (a messaging platform or the Kanban dispatcher)."""
    return any(FEATURES[name].get("gateway") for name in features(a))


def hosts(names):
    """The hosts the features NAMES open, each once."""
    return list(dict.fromkeys(host for name in names for host in FEATURES[name]["hosts"]))


def secrets(names):
    """{variable: secret} the features NAMES give Hermes."""
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
    given = {value.partition("=")[0] for value in a.secret_env}
    for variable, secret in secrets(names).items():
        if variable not in given:
            a.secret_env.append(f"{variable}={secret}")


def check(a, hooks_active=False, companions=()):
    """Refuse what Hermes cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "hermes_enable", None) and a.agent != "hermes":
        raise ValueError(f"--hermes-enable is for Hermes (sandbox-ai hermes), not {a.agent}")
    if a.agent != "hermes" and "hermes" not in companions:
        return
    names = parse(getattr(a, "hermes_enable", None)) if a.agent == "hermes" else []
    if a.agent != "hermes" or companions:
        raise ValueError("--with: Hermes is not in the team image; give Hermes another agent as a helper "
                         "(sandbox-ai hermes --helper claude) or Hermes to another agent (--helper hermes)")
    if hooks_active:
        raise ValueError("rules for the agent's own tools (a ruleset's \"tool\" rules, the security mode "
                         "paranoid's) are not enforced for Hermes yet; start without them, in the mode high or "
                         "laxer (the network, MCP and device rules hold as for every agent)")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--hermes-enable {','.join(names)} needs the network; drop --offline")


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
    """Hermes' telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS (--allow-host,
    a wildcard over it, a feature) or let it pass the proxy unseen (--no-inspect), or that opens everything without
    the proxy that refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"Hermes' telemetry is always off: {', '.join(opened)} is never opened for Hermes; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"Hermes' telemetry is always off: the proxy must see {', '.join(unseen)} to refuse it; "
                         "drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("Hermes' telemetry is always off: --egress-all-allow without the connection proxy would "
                         "let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules():
    """The proxy's rules for every Hermes run (the ruleset "telemetry"): each of rule_hosts() refused."""
    return [{"id": f"telemetry-hermes-{number}", "host": host, "path": "/**", "decision": "deny",
             "reason": "Hermes' telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]


def experimental_note():
    return ("Hermes: not live-tested yet (its image and a real start have not passed their live test)")


def mcp_servers(names, bridge_args, timeouts=None):
    """Hermes' mcp_servers for the --mcp servers NAMES: each the launcher's stdio bridge (BRIDGE_ARGS(name)), the
    servers' own LLM requests (MCP sampling) off: they would spend the agent's model on a server's behalf. Its wait
    for one call ("timeout", seconds) from TIMEOUTS ({name: seconds}, the launcher's mcp_tool_timeouts); a name not
    in it keeps Hermes' own."""
    timeouts = timeouts or {}
    return {name: {"command": "python3", "args": list(bridge_args(name)), "sampling": {"enabled": False},
                   **({"timeout": timeouts[name]} if name in timeouts else {})}
            for name in dict.fromkeys(names)}


LOOP_SKILL = "sandbox-ai-loop-guard"


def loop_skill(text):
    """SKILL.md of the loop guard's instructions (llm.LOOP_TEXT) for Hermes."""
    return ("---\nname: " + LOOP_SKILL + "\ndescription: How to work without loops on a local model (sandbox-ai's "
            "loop guard); loaded in every session.\n---\n\n" + text)


def managed_config(a, telemetry="off", mcp=None, llm=None):
    """Hermes' /etc/hermes/config.yaml for this start: the sandbox's defaults, the telemetry switches (at every
    level: telemetry is always off), what the features turn on again, the --mcp servers (MCP: their mcp_servers) and --llm's model
    (LLM: llm.hermes_config, with the loop guard's skill among the external skills). Later layers win."""
    config = merge(merge({}, BASE_CONFIG), TELEMETRY_CONFIG)
    names = features(a)
    for name in names:
        config = merge(config, FEATURES[name].get("config", {}))
    if mcp:
        config = merge(config, {"mcp_servers": mcp})
    config = merge(config, TELEMETRY_PINNED)  # after the features: never turned on
    if llm:
        skills = llm.get("skills", {}).get("auto_load")
        config = merge(config, {key: value for key, value in llm.items() if key != "skills"})
        if skills:
            config["skills"] = {**config["skills"], "auto_load": skills,
                                "external_dirs": [*config["skills"]["external_dirs"], SKILLS_DIR]}
    return config


def files(a, config, loop_text=None):
    """{path in the session directory: content}: the managed configuration (JSON, which YAML reads), its .env (the
    terminal backend once more), the loop guard's skill when it is on."""
    found = {f"{SESSION_DIR}/config.yaml": json.dumps(config, indent=2) + "\n",
             f"{SESSION_DIR}/.env": "TERMINAL_ENV=local\n"}
    if loop_text and LOOP_SKILL in config.get("skills", {}).get("auto_load", []):
        found[f"{SESSION_DIR}/skills/{LOOP_SKILL}/SKILL.md"] = loop_skill(loop_text)
    return found


def environment(a, placeholder=None):
    """Hermes' variables: its home, the local terminal, no installs at run time unless enabled, and for an --llm at
    OpenRouter the placeholder where Hermes looks for OpenRouter's key."""
    env = {"HERMES_HOME": "/root/.hermes", "TERMINAL_ENV": "local", **TELEMETRY_ENV}
    if "lazy-installs" not in features(a):
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    if placeholder:
        env["OPENROUTER_API_KEY"] = placeholder
    return env


def plan(a):
    """The dry run's "hermes": the reviewed release, the managed configuration's place, what is on and what stays
    off, and whether it is experimental here."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "experimental": True, "managed_config": f"{MANAGED_DIR}/config.yaml",
            "terminal_backend": "local", "gateway": gateway(a),
            # Never switchable: pinned off, opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": sorted(TELEMETRY_PINNED)},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"],
                         "secrets": sorted(FEATURES[name].get("secrets", {}).values())} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """A Hermes login (~/.hermes/auth.json): a provider's state or a credential pool with an entry."""
    if not isinstance(document, dict):
        return False
    providers, pool = document.get("providers"), document.get("credential_pool")
    return any(isinstance(section, dict) and any(isinstance(value, (dict, list)) and value
                                                 for value in section.values())
               for section in (providers, pool))


def help_text():
    """--hermes-enable's help: every feature with what it does."""
    return ("Hermes only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; gateway:all is every platform")
