#!/usr/bin/env python3
"""OpenCode 2 (anomalyco/opencode, MIT; npm @opencode/cli) as a sandbox-ai agent of its own, sandbox-ai opencode2,
beside OpenCode 1 (sandbox-ai opencode, npm opencode-ai): what upstream does that a sandbox turns off, the switches
that turn each part on again (--opencode2-enable, the form of Hermes' --hermes-enable), OpenCode 2's variables and
the configuration layer sandbox-ai gives it.

OpenCode 2 merges its configuration from /etc/opencode, ~/.config/opencode, OPENCODE_CONFIG and the project's files;
OPENCODE_CONFIG_CONTENT comes last and wins over every file (measured: opencode debug config). So what must hold for a
start is in that variable or another variable of the agent's process (the agent cannot change its own environment):
no update check, no catalog download, the --mcp servers, -D, --model, the telemetry opt-outs. Its shared background
service (one server per user that outlives a command) is switched off in the sandbox's ~/.config/opencode/service.json,
so every command runs its own server and ends with it.

Why OpenCode 2 is an agent of its own and not an upgrade of sandbox-ai opencode, and the review behind every default
here: docs/OPENCODE2_REVIEW.md (OpenCode 2.2.5).

Standard library only.
"""
import datetime
import json
import re

# The release reviewed and built into localhost/opencode2-sandbox (Containerfile.opencode2): the newest one at least
# 14 days old when it was taken in. Only the platform package's tarball from npm's registry, checked against its
# sha512 integrity (Containerfile.opencode2.lock, scripts/opencode2_lock.py): never the @opencode/cli launcher, whose
# install script copies the binary in or runs npm install itself.
PACKAGE = "@opencode/cli"
VERSION = "2.2.5"
TAG = "v" + VERSION
COMMIT = "9bbb8ce18c80f099e0e448775f59d965ead40a21"
PUBLISHED = "2026-10-30T23:19:37Z"  # the last of its packages below (the x64 baseline build)
REPOSITORY = "https://github.com/anomalyco/opencode"
REGISTRY = "https://registry.npmjs.org"
# Debian architecture -> (npm package, its sha512 integrity on the registry). amd64-baseline: for x64 CPUs without
# AVX2 (upstream's launcher makes the same choice).
PLATFORMS = {
    "amd64": ("@opencode/cli-linux-x64",
              "sha512-BkHAfTqNsg/qphyHNrnH+DGkOxZsGjamNpM5DvwoEvz7ZAPNw8mEbMXGTCDOAH+/+40Eqeie6KNZBZZQwPCjaA=="),
    "amd64-baseline": ("@opencode/cli-linux-x64-baseline",
                       "sha512-oPOT+EMmdz56HnSFPRVwZ0QTVL5defhOacnwzazu0XOuCWUtEKh8icIn323SdCLtqZtzjuU1mt6p2/13V93WWg=="),
    "arm64": ("@opencode/cli-linux-arm64",
              "sha512-O4qOy/WRYQmhClMB/5qBaoYfzaDK1+PJRQOfU80ixyj5ruQbCxraH+VW2UyHb6RWOlUv41fec+isd62LvCuwuA=="),
}
# The pin rule: a third-party release only once it is at least this old. The next release, ready for its review:
# 2.2.6 waits for connecting MCP servers before opencode run's first model call (2.2.5 does not:
# docs/OPENCODE2_REVIEW.md, "Known gap"). scripts/opencode2_lock.py refuses a release younger than this.
MIN_AGE = datetime.timedelta(days=14)
NEXT = {"version": "2.2.6", "eligible": "2026-11-20"}  # its x64 binary published 2026-11-06 23:38 UTC

NAME = "OpenCode 2"
BINARY = "opencode2"  # the image's command (upstream's second name for its binary; "opencode" is the same)
SERVICE_FILE = ".config/opencode/service.json"  # its shared background service's settings, in the sandbox's HOME
# sandbox-ai's read-only directory for OpenCode 2 (not OpenCode's own /etc/opencode): the configuration layer of the
# start (the same as OPENCODE_CONFIG_CONTENT, to read), and with --llm the provider file (OPENCODE_CONFIG names it)
# and the loop guard's instructions (llm.py: OPENCODE2_CONFIG, OPENCODE2_LOOP_FILE).
MANAGED_DIR = "/etc/opencode2"
SESSION_DIR = "opencode2"  # in the session directory, mounted read-only at MANAGED_DIR
LAYER = MANAGED_DIR + "/sandbox-ai-layer.json"
LLM_CONFIG = MANAGED_DIR + "/sandbox-ai-llm.json"
LOOP_FILE = MANAGED_DIR + "/sandbox-ai-loop-guard.md"
# sandbox-ai's V2 plugin for the rules of OpenCode 2's own tools and the approved kits' hooks (OpenCode 1's plugin does
# not run in OpenCode 2): a plugin directory (OpenCode 2.2.5 takes a configured local plugin only as a directory:
# config/plugin/source.ts), named in OPENCODE_CONFIG_CONTENT's "plugins"; its settings beside it.
HOOKS_PLUGIN = MANAGED_DIR + "/sandbox-ai-hooks"
HOOKS_CONFIG = HOOKS_PLUGIN + "/config.json"
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")

# What --opencode2-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host) and the
# variables it no longer sets.
FEATURES = {
    "models-catalog": {
        "hosts": ["models.opencode.ai"],
        "drops": ["OPENCODE_DISABLE_MODELS_FETCH"],
        "help": "the model catalog (models.dev data from models.opencode.ai) at every start; without it OpenCode 2 "
                "uses the catalog built into its binary"},
    "updates": {
        "hosts": ["opencode.ai"],
        "drops": ["OPENCODE_DISABLE_AUTOUPDATE"],
        "help": "OpenCode 2's update check and its notice (opencode.ai/update); OpenCode 2 itself is updated by a new "
                "review and sandbox-ai build opencode2: its install is read-only and opencode upgrade is refused"},
    "plugins": {
        "hosts": ["registry.npmjs.org"],
        "help": "plugins from npm (the configuration's plugins, opencode plugin add|update|check); local plugins "
                "(.opencode/plugins) load without it"},
    "web-search": {
        "hosts": ["mcp.exa.ai", "mcp.firecrawl.dev", "search.parallel.ai", "api.tavily.com", "agent.tinyfish.ai"],
        "help": "its web search tool's providers (Exa, Firecrawl, Parallel, Tavily, TinyFish; a provider's key with "
                "--api-key)"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*")

# Telemetry is never switchable for OpenCode 2 (the user's rule): no feature, no --telemetry level and no option of
# the start turns it on. OpenCode 2.2.5 sends no usage telemetry (measured); what it carries is pinned off anyway:
# Bun's crash reporter (bun.report) and OpenTelemetry's exporters. bun.report is never opened and the proxy refuses it.
TELEMETRY_HOSTS = ("bun.report",)
TELEMETRY_ENV = {
    "DO_NOT_TRACK": "1",
    "BUN_ENABLE_CRASH_REPORTING": "0",  # Bun's crash handler: no report to bun.report
    "OTEL_SDK_DISABLED": "true",  # OpenTelemetry: nothing exported, whatever OTEL_EXPORTER_OTLP_* says
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
}
# Never opened either, and refused by the proxy: `opencode pair --remote` exposes the sandbox's server to the internet
# through OpenTunnel.
NEVER_HOSTS = {"opentunnel.xyz": "OpenCode 2's remote pairing (opencode pair --remote) would expose the sandbox's "
                                 "server to the internet"}
# The update check's path on opencode.ai (open as OpenCode's own provider): refused unless --opencode2-enable updates.
UPDATE_RULES = (("opencode.ai", "/update/**"),)
# Every start's other variables (docs/OPENCODE2_REVIEW.md has each).
BASE_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",  # no update check
    "OPENCODE_DISABLE_MODELS_FETCH": "1",  # the bundled catalog, not models.opencode.ai
}
# The configuration layer's settings that hold in every start (OPENCODE_CONFIG_CONTENT): no update check (V2's
# "update", V1's "autoupdate", both read).
UPDATE_CONFIG = {"update": "disable", "autoupdate": False}
DANGER_PERMISSIONS = [{"action": "*", "resource": "*", "effect": "allow"}]  # -D: no questions

# The agent's arguments that would undo a default: what installs or removes OpenCode itself (refused always: the
# install is read-only and a new release is a new review), the plugin installs without their feature, remote pairing
# and a server off the sandbox's loopback.
SUBCOMMANDS_REFUSED = {"upgrade": "OpenCode 2 is updated by a new review and sandbox-ai build opencode2 (its install "
                                  "is read-only)",
                       "update": "OpenCode 2 is updated by a new review and sandbox-ai build opencode2 (its install "
                                 "is read-only)",
                       "uninstall": "OpenCode 2's install is part of the image (read-only)"}
PLUGIN_INSTALLS = ("add", "update", "check")


def parse(values):
    """The features of --opencode2-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given.
    ValueError names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not FEATURE_SYNTAX.fullmatch(name) or name not in FEATURES:
                raise ValueError(f"--opencode2-enable {part.strip()!r}: not an OpenCode 2 feature; one of "
                                 f"{', '.join(FEATURES)}")
            if name not in found:
                found.append(name)
    return found


def features(a):
    """The features this start turns on (none when it is not OpenCode 2's)."""
    return parse(getattr(a, "opencode2_enable", None)) if getattr(a, "agent", None) == "opencode2" else []


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


def refused_arg(args, names=()):
    """(argument, why) of the first agent argument that a sandbox refuses with the features NAMES, or None."""
    options = agent_options(args or [])
    words = [arg for arg in options if not arg.startswith("-")]
    first = words[0] if words else None
    if first in SUBCOMMANDS_REFUSED:
        return first, SUBCOMMANDS_REFUSED[first]
    if first == "plugin" and len(words) > 1 and words[1] in PLUGIN_INSTALLS and "plugins" not in names:
        return f"plugin {words[1]}", ("opencode plugin add|update|check installs from npm: it needs "
                                      "--opencode2-enable plugins")
    if first == "pair":
        for arg in options:
            if arg == "--remote" or arg.startswith("--remote="):
                return arg, NEVER_HOSTS["opentunnel.xyz"] + "; not in a sandbox"
    if first == "serve":
        for index, arg in enumerate(options):  # every one: the last given is the one it takes
            value = option_value(options, index, "--hostname")
            if value is not None and not value.startswith("-") and address_host(value) not in LOOPBACK:
                return arg, "opencode serve serves on the sandbox's loopback only (--hostname 127.0.0.1)"
    return None


def address_host(value):
    """The host of an address VALUE (HOST, HOST:PORT, [V6]:PORT, a URL), lower-case."""
    rest = str(value).split("://", 1)[-1].split("/", 1)[0]
    if rest.startswith("["):
        return rest.split("]", 1)[0].lower() + "]"
    return (rest.rsplit(":", 1)[0] if rest.count(":") == 1 else rest).lower()


def check(a, hooks_active=False, companions=()):
    """Refuse what OpenCode 2 cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "opencode2_enable", None) and a.agent != "opencode2" and "opencode2" not in companions:
        raise ValueError(f"--opencode2-enable is for OpenCode 2 (sandbox-ai opencode2, or --with opencode2), not "
                         f"{a.agent}")
    if a.agent != "opencode2" and "opencode2" not in companions:
        return
    names = parse(getattr(a, "opencode2_enable", None)) if a.agent == "opencode2" or "opencode2" in companions else []
    if getattr(a, "local_config", None):
        raise ValueError("--local-config is OpenCode 1's; OpenCode 2 reads ~/.config/opencode of the sandbox's HOME "
                         "and the project's opencode.json")
    if getattr(a, "import_codex_auth", None):
        raise ValueError("--import-codex-auth is OpenCode 1's; connect OpenCode 2 in the sandbox (/connect), with "
                         "--api-key or --llm")
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "opencode2" else [], names)
    if refused:
        raise ValueError(f"{refused[0]} in OpenCode 2's arguments: {refused[1]}")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--opencode2-enable {','.join(names)} needs the network; drop --offline")


def host_matches_any(host, pattern):
    """HOST and PATTERN (each a name or *.DOMAIN) can name the same server (the launcher's rule)."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
    return host == pattern or bare(host).endswith("." + bare(pattern)) or bare(pattern).endswith("." + bare(host)) \
        or bare(host) == bare(pattern)


def telemetry_hosts_in(values):
    """The TELEMETRY_HOSTS and NEVER_HOSTS that host names or *.DOMAIN patterns VALUES would reach."""
    return sorted({host for value in values for host in (*TELEMETRY_HOSTS, *NEVER_HOSTS)
                   if host_matches_any(str(value), host)})


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
    """OpenCode 2's telemetry is never reachable: refused, a start that would open one of TELEMETRY_HOSTS or
    NEVER_HOSTS (--allow-host, a wildcard over it), let one pass the proxy unseen (--no-inspect), or open everything
    without the proxy that refuses them (--egress-all-allow with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"OpenCode 2's telemetry is always off: {', '.join(opened)} is never opened for OpenCode 2; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"OpenCode 2's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse "
                         "it; drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("OpenCode 2's telemetry is always off: --egress-all-allow without the connection proxy "
                         "would let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse in every OpenCode 2 run: TELEMETRY_HOSTS, then NEVER_HOSTS."""
    return list(dict.fromkeys([*TELEMETRY_HOSTS, *NEVER_HOSTS]))


def telemetry_rules(a=None):
    """The proxy's rules for every OpenCode 2 run (the ruleset "telemetry"): each of rule_hosts() refused, and the
    update check's path on opencode.ai refused quietly unless --opencode2-enable updates."""
    rules = [{"id": f"telemetry-opencode2-{number}", "host": host, "path": "/**", "decision": "deny",
              "reason": "OpenCode 2's telemetry is always off"} for number, host in enumerate(TELEMETRY_HOSTS, 1)]
    rules += [{"id": f"opencode2-never-{number}", "host": host, "path": "/**", "decision": "deny", "reason": why}
              for number, (host, why) in enumerate(NEVER_HOSTS.items(), 1)]
    if a is None or "updates" not in features(a):
        rules += [{"id": f"opencode2-updates-{number}", "host": host, "path": path, "decision": "deny",
                   "reason": "OpenCode 2's update check (--opencode2-enable updates opens it)"}
                  for number, (host, path) in enumerate(UPDATE_RULES, 1)]
    return rules


def mcp_servers(names, bridge_args, timeouts=None, timeout=300):
    """OpenCode 2's MCP servers for the --mcp servers NAMES (its native mcp.servers): each a stdio server that runs the
    launcher's bridge (BRIDGE_ARGS(name)), its tools the model's own (codemode false: not behind Code Mode's
    execute), its waits in milliseconds (TIMEOUTS: {name: seconds}, else TIMEOUT)."""
    servers = {}
    for name in dict.fromkeys(names):
        wait = int((timeouts or {}).get(name, timeout)) * 1000
        servers[name] = {"type": "local", "command": ["python3", *bridge_args(name)], "codemode": False,
                         "timeout": {"startup": wait, "catalog": wait, "execution": wait}}
    return {"servers": servers}


def config_content(a, mcp=None, model=None, note=None, hooks=False, plugins=()):
    """OPENCODE_CONFIG_CONTENT: the layer above every file of OpenCode 2's - no update check (unless updates), the
    --mcp servers (MCP: mcp_servers()), -D's allow-all permissions, the model (MODEL: --llm's provider/model, else
    --model), the run's network note as one more instruction file (NOTE: its path), sandbox-ai's hooks plugin
    (HOOKS: rules or approved kit hooks this run; HOOKS_PLUGIN), then the kits' approved plugins (PLUGINS: their
    folders): OpenCode 2 sets plugins up in the order of their operations (packages/core/src/plugin/supervisor.ts), so
    the rule plugin's check comes first and the call it checked is frozen before theirs run; each is an activation of
    its own, so one that fails is disabled alone (packages/core/src/plugin.ts), never the rule plugin."""
    content = {"update": "notify"} if "updates" in features(a) else dict(UPDATE_CONFIG)
    if hooks or plugins:
        content["plugins"] = [*([HOOKS_PLUGIN] if hooks else []), *plugins]
    if mcp:
        content["mcp"] = mcp
    if note:
        # OpenCode 2 reads the instructions of every configuration layer, each file once (its
        # ConfigInstructionPlugin: a Set over the layers' instructions): your AGENTS.md and the loop guard's file of
        # --llm (OPENCODE_CONFIG) stay.
        content["instructions"] = [note]
    if getattr(a, "dangerously_skip_permissions", False):
        content["permissions"] = [dict(rule) for rule in DANGER_PERMISSIONS]
    model = model or getattr(a, "model", None)
    if model:
        content["model"] = model
    return content


def environment(a, content=None):
    """OpenCode 2's variables: no update check and no catalog download (unless their features), the configuration
    layer CONTENT (config_content), the telemetry opt-outs (never dropped)."""
    dropped = {variable for name in features(a) for variable in FEATURES[name].get("drops", [])}
    env = {key: value for key, value in BASE_ENV.items() if key not in dropped}
    if getattr(a, "helper_of", None):
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"  # the lead shares /work: no plugins or settings from there
    if content:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content, separators=(",", ":"))
    env.update(TELEMETRY_ENV)
    return env


def files(a, content, llm_files=None, hooks=None):
    """{path in the session directory: content}: the configuration layer CONTENT (config_content) as a file to read,
    LLM_FILES ({name: content}: llm.opencode2_files) with --llm, and with HOOKS ({"rules", "modules"}: the launcher's
    module_hooks) sandbox-ai's hooks plugin: its package.json, server.js (HOOKS_SCRIPT) and config.json."""
    found = {f"{SESSION_DIR}/{LAYER.rsplit('/', 1)[1]}": json.dumps(content, indent=2) + "\n"}
    found.update({f"{SESSION_DIR}/{name}": text for name, text in (llm_files or {}).items()})
    if hooks:
        folder = f"{SESSION_DIR}/{HOOKS_PLUGIN.rsplit('/', 1)[1]}"
        found[f"{folder}/package.json"] = json.dumps({"type": "module"}) + "\n"
        found[f"{folder}/server.js"] = HOOKS_SCRIPT
        found[f"{folder}/config.json"] = json.dumps({"rules": bool(hooks.get("rules")),
                                                     "modules": list(hooks.get("modules") or [])}, indent=2) + "\n"
    return found


# sandbox-ai's V2 plugin (HOOKS_PLUGIN/server.js, read-only): the Promise plugin API of OpenCode 2.2.5
# (packages/plugin/src/promise: a default export {id, setup}; ctx.tool.hook("execute.before"|"execute.after", fn), a
# rejection of execute.before refuses that call alone). OpenCode 2 runs the call with event.tool and event.input as
# the hooks left them, so the checked event is frozen: a plugin after this one that changes it throws, and its call
# fails instead of running changed.
HOOKS_SCRIPT = r'''// sandbox-ai: the rules for OpenCode 2's own tools (README, Rulesets: "The agent's own tools") and the approved
// kits' hooks. Installed read-only by the launcher. Each tool call asks the sandbox's supervisor first; a refusal, or
// anything that goes wrong on the way, refuses the call.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const CONFIG = "/etc/opencode2/sandbox-ai-hooks/config.json";
const HOOK = ["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "opencode2"];

function readConfig() {
  try {
    const config = JSON.parse(readFileSync(CONFIG, "utf8"));
    return config && typeof config === "object" && !Array.isArray(config) ? config : {};
  } catch {
    return { rules: true, unreadable: true };  // never fewer rules than asked for: every call is refused
  }
}

function hook(event, message) {
  return new Promise((resolve) => {
    const refused = (why) => resolve({ code: 2, reason: `sandbox-ai: the tool call is refused: ${why}` });
    let child;
    try {
      child = spawn(HOOK[0], [...HOOK.slice(1), event], { stdio: ["pipe", "ignore", "pipe"] });
    } catch (error) {
      refused(`the hook did not start (${error})`);
      return;
    }
    let reason = "";
    child.stderr.on("data", (chunk) => { if (reason.length < 4000) reason += chunk; });
    child.on("error", (error) => refused(`the hook did not start (${error})`));
    child.on("close", (code) => resolve({ code: code === null ? 2 : code, reason }));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(message));
  });
}

function freeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const key of Object.keys(value)) freeze(value[key]);
  }
  return value;
}

export default {
  id: "sandbox-ai-hooks",
  setup: async (ctx) => {
    const config = readConfig();
    const checking = config.rules === true;
    const modules = Array.isArray(config.modules) ? config.modules.filter((file) => typeof file === "string") : [];
    const checked = new Map();
    const shape = (event) => JSON.stringify([event.tool, event.input]) ?? "null";
    const ask = async (event) => {
      if (config.unreadable) return "sandbox-ai: the tool call is refused: the rules' settings could not be read";
      const answer = await hook("PreToolUse", { tool: event.tool, args: event.input, sessionID: event.sessionID,
                                                callID: event.id, cwd: process.cwd() });
      return answer.code === 0 ? null : (answer.reason.trim() || "sandbox-ai: the tool call is refused");
    };
    if (checking) {
      await ctx.tool.hook("execute.before", async (event) => {
        const why = await ask(event);
        if (why) throw new Error(why);
        if (modules.length) {
          if (checked.size >= 10000) checked.clear();
          checked.set(event.id, shape(event));
        } else {
          freeze(event);
        }
      });
    }
    for (const file of modules) {
      try {
        const plugin = (await import(pathToFileURL(file).href)).default;
        if (!plugin || typeof plugin.setup !== "function") throw new Error("its default export is no {id, setup}");
        await plugin.setup(ctx);
      } catch (error) {
        console.error(`sandbox-ai: the kit's hook ${file} did not load: ${error?.message ?? error}`);
      }
    }
    if (checking && modules.length) {
      await ctx.tool.hook("execute.before", async (event) => {
        const before = checked.get(event.id);
        checked.delete(event.id);
        if (before !== shape(event)) {
          const why = await ask(event);
          if (why) throw new Error(why);
        }
        freeze(event);
      });
    }
    if (checking) {
      await ctx.tool.hook("execute.after", async (event) => {
        const text = JSON.stringify(event.status === "completed" ? event.result : event.error) ?? "";
        await hook("PostToolUse", { tool: event.tool, sessionID: event.sessionID, callID: event.id,
                                    cwd: process.cwd(), result_bytes: Buffer.byteLength(text) });
      });
    }
  },
};
'''


def service_settings(text):
    """The sandbox's ~/.config/opencode/service.json with the shared background service off ("disabled": true), from
    its TEXT (None: no file). None when it is so already; whatever else the file holds (its password) stays. A file
    that is not a JSON object is replaced."""
    try:
        document = json.loads(text) if text is not None else {}
    except (ValueError, RecursionError):  # damaged, or nested deeper than Python reads
        document = {}
    if not isinstance(document, dict):
        document = {}
    if document.get("disabled") is True:
        return None
    document["disabled"] = True
    return json.dumps(document, indent=2) + "\n"


def plan(a):
    """The dry run's "opencode2": the reviewed release, what is on and what stays off."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "background_service": False, "own_sandbox": False,
            # Never switchable: opt-out variables set, these hosts never opened and refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV)},
            "never": sorted(NEVER_HOSTS),
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"]} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """OpenCode 2 has no login sandbox-ai takes in: it keeps saved accounts in its own database
    (~/.local/share/opencode/opencode.db of the sandbox's HOME, /connect); keys come with --api-key or --llm."""
    return False


def eligible(published, now=None):
    """Whether a release PUBLISHED (ISO 8601, UTC) is old enough for the pin rule (MIN_AGE) at NOW (default: now)."""
    when = datetime.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now - when >= MIN_AGE


def help_text():
    """--opencode2-enable's help: every feature with what it does."""
    return ("OpenCode 2 only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
