#!/usr/bin/env python3
"""Pi (earendil-works/pi, MIT, formerly badlogic/pi-mono: the pi coding agent, a TypeScript CLI) as a sandbox-ai
agent: what upstream does that a sandbox turns off, the switches that turn each part on again (--pi-enable, the form
of Hermes' --hermes-enable), Pi's variables and sandbox-ai's extension for it.

Pi keeps every setting in files of the user's (~/.pi/agent/settings.json, models.json, mcp.json, auth.json; the
project's .pi/ after its trust question); it has no system layer that wins over them. So what must hold is a variable
of the agent's process (the agent cannot change its own environment): no automatic network activity (PI_OFFLINE: no
model catalog refresh, version check, install report, package update check, install of missing packages or download
of fd and ripgrep), no version check, no install telemetry (PI_TELEMETRY=0). What changes per start - the --mcp
servers, --llm's provider, the loop guard - is code Pi loads read-only: sandbox-ai's extension (EXTENSION_SCRIPT, at
/etc/pi/sandbox-ai.js) with its settings (/etc/pi/sandbox-ai.json), written into the session directory and mounted
read-only at /etc/pi; the image's pi command loads it with -e whenever it is there (Containerfile.pi), so every pi of
the sandbox gets it - the agent, a helper's, one started from the agent's shell.

The review behind every default here: docs/PI_REVIEW.md (Pi 1.4.0).

Standard library only.
"""
import json
import re

# The release reviewed and built into localhost/pi-sandbox (Containerfile.pi): the newest one at least 14 days old
# when it was taken in. PACKAGE@VERSION from npm (its own dependencies pinned to exact versions), locked with versions
# published before LOCK_BEFORE (scripts/agent_npm_lock.py, Containerfile.pi.lock): at least 14 days before the review.
PACKAGE = "@earendil-works/pi-coding-agent"
VERSION = "1.4.0"
COMMIT = "4a080252f06caf63a525d7873c5d94d3c23887e0"
INTEGRITY = "sha512-Z41LpZ3yYHSC/UUps6Mr39UMFPwH8elhJBG9DY1P7kuzsE5qSK/6eMj2EHzO5evJRQWLbKSqUMw+4nCNtIyiQg=="
LOCK_BEFORE = "2026-11-01T00:00:00Z"
REPOSITORY = "https://github.com/earendil-works/pi"
MANAGED_DIR = "/etc/pi"
SESSION_DIR = "pi"  # in the session directory, mounted read-only at MANAGED_DIR
EXTENSION = MANAGED_DIR + "/sandbox-ai.js"  # sandbox-ai's extension (the image's pi command loads it with -e)
EXTENSION_CONFIG = MANAGED_DIR + "/sandbox-ai.json"  # what it registers: {"servers", "provider", "instructions", ...}
NAME = "Pi"
AGENT_DIR = ".pi/agent"  # Pi's folder in the HOME: settings, models, MCP servers, auth.json, sessions, skills
STORAGE = AGENT_DIR + "/sessions"  # its sessions (JSONL trees, one folder per working directory)
MEMORY_NOTES = AGENT_DIR + "/AGENTS.md"  # sandbox-ai's project notes for Pi (kits.MEMORY_FILE); Pi reads it itself
LLM_PROVIDER = "sandbox-ai"  # --llm: the provider sandbox-ai's extension registers (llm.pi_llm)
DANGER_ARGS = []  # -D: Pi asks before nothing anyway (no approvals; this sandbox is the boundary)
EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")  # --effort: Pi's --thinking

# What --pi-enable turns on again, each with the hosts it opens (TCP/443, pinned like --allow-host) and the variables
# it no longer sets.
FEATURES = {
    "model-catalog": {
        "hosts": ["pi.dev"],
        "drops": ["PI_OFFLINE"],
        "help": "Pi's model catalogs from pi.dev (refreshed at start and in /model, at most daily; pi update "
                "--models); without it Pi has the catalog that came with its release. Its version check and install "
                "report stay off"},
    "updates": {
        "hosts": ["pi.dev"],
        "drops": ["PI_OFFLINE", "PI_SKIP_VERSION_CHECK"],
        "help": "Pi's version check at start (pi.dev): a notice only; Pi itself is updated by a new review and "
                "sandbox-ai build pi (its install is read-only, pi update is refused)"},
    "packages": {
        "hosts": ["registry.npmjs.org", "github.com", "codeload.github.com"],
        "help": "pi install, pi update --extensions and -e npm:/git: sources: Pi packages (extensions, skills, "
                "prompts, themes) from npm and GitHub, installed into the sandbox's HOME (installed ones load "
                "without it)"},
    "share": {
        "hosts": ["api.github.com"],
        "help": "/share: the session as a secret GitHub gist through gh (signed in in the sandbox); without it the "
                "proxy refuses creating gists in Pi's runs"},
}
FEATURE_SYNTAX = re.compile(r"[a-z][a-z-]*")

# Telemetry is never switchable for Pi (the user's rule): no feature, no --telemetry level and no option of the start
# turns it on. Pi's install report (pi.dev/api/report-install at a HOME's first interactive start and after every
# update) and its provider attribution follow PI_TELEMETRY, which wins over the setting enableInstallTelemetry
# (core/telemetry.js); PI_OFFLINE stops the report as well. /bug's upload to the developers (radius.pi.dev) is crash
# reporting: PI_OFFLINE stops it, and the proxy refuses both paths in every Pi run, also where a feature or --api-key
# opens pi.dev or radius.pi.dev. No host of Pi's is telemetry alone.
TELEMETRY_HOSTS = ()
TELEMETRY_PATHS = (("pi.dev", "/api/report-install/**"), ("radius.pi.dev", "/v1/bug-reports/**"))
TELEMETRY_ENV = {
    "PI_TELEMETRY": "0",  # core/telemetry.js: isInstallTelemetryEnabled (the install report, the attribution)
}
# /share without the share feature: Pi creates a secret gist through gh (POST api.github.com/gists), refused in Pi's runs.
SHARE_RULE = ("api.github.com", "/gists/**")
# Every start's other variables (docs/PI_REVIEW.md has each and the source behind it).
BASE_ENV = {
    "PI_OFFLINE": "1",  # no automatic network activity: catalogs, version check, report, package checks, downloads
    "PI_SKIP_VERSION_CHECK": "1",  # the version check, also where a feature drops PI_OFFLINE
}

# The agent's arguments that would undo a default: Pi updating itself (always: its install is read-only, and its own
# advice is a pipe into a shell), packages from npm and git without the packages feature, catalogs without
# model-catalog.
PACKAGE_SOURCE = re.compile(r"(?:npm:|git:|git\+|https?://|ssh://|git@|github\.com/|gitlab\.com/)", re.I)
SELF_TARGETS = ("--self", "self", "pi")


def parse(values):
    """The features of --pi-enable FEATURE[,FEATURE...] (repeatable), each once, in the order given. ValueError
    names what is unknown."""
    found = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not FEATURE_SYNTAX.fullmatch(name) or name not in FEATURES:
                raise ValueError(f"--pi-enable {part.strip()!r}: not a Pi feature; one of {', '.join(FEATURES)}")
            if name not in found:
                found.append(name)
    return found


def features(a):
    """The features this start turns on (none when it is not Pi's)."""
    return parse(getattr(a, "pi_enable", None)) if getattr(a, "agent", None) == "pi" else []


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


def needs(feature, names, what):
    """None if FEATURE is among NAMES, else (why) for WHAT."""
    if feature in names:
        return None
    return f"{what} needs --pi-enable {feature} ({FEATURES[feature]['help'].split(':')[0]})"


def update_refusal(options, names):
    """Why `pi update OPTIONS...` is refused with the features NAMES, or None."""
    targets = [arg for arg in options[1:] if arg not in ("--force", "-a", "--approve", "-na", "--no-approve")]
    if not targets or any(arg in SELF_TARGETS or arg == "--all" for arg in targets):
        return ("pi update updates Pi itself, whose install is read-only in the sandbox (a new Pi is a new review, "
                "then sandbox-ai build pi)")
    wanted = []
    for arg in targets:
        if arg in ("--models",):
            wanted.append(("model-catalog", "pi update --models"))
        elif not arg.startswith("-") or arg.startswith("--extension"):
            wanted.append(("packages", "pi update with a package"))
    for feature, what in wanted:
        why = needs(feature, names, what)
        if why:
            return why
    return None


def refused_arg(args, names=()):
    """(argument, why) of the first agent argument that a sandbox refuses with the features NAMES, or None."""
    options = agent_options(args or [])
    first = options[0] if options else None
    if first == "update":
        why = update_refusal(options, names)
        return (first, why) if why else None
    if first == "install":
        why = needs("packages", names, "pi install")
        return (first, why) if why else None
    if first in ("remove", "uninstall", "list", "config", "auth", "mcp"):
        return None  # local: the user's settings and packages in the sandbox's HOME
    for index, arg in enumerate(options):
        name, _, attached = arg.partition("=")
        if name in ("-e", "--extension"):
            value = attached or (options[index + 1] if index + 1 < len(options) else "")
            if PACKAGE_SOURCE.match(value):
                why = needs("packages", names, f"-e {value}")
                if why:
                    return arg, why
    return None


def check(a, hooks_active=False, companions=()):
    """Refuse what Pi cannot do in a sandbox (yet), before anything starts."""
    if getattr(a, "pi_enable", None) and a.agent != "pi" and "pi" not in companions:
        raise ValueError(f"--pi-enable is for Pi (sandbox-ai pi, or --with pi), not {a.agent}")
    if a.agent != "pi" and "pi" not in companions:
        return
    names = parse(getattr(a, "pi_enable", None)) if a.agent == "pi" or "pi" in companions else []
    refused = refused_arg((getattr(a, "agent_args", None) or []) if a.agent == "pi" else [], names)
    if refused:
        raise ValueError(f"{refused[0]} in Pi's arguments: {refused[1]}")
    check_telemetry(a)
    if names and getattr(a, "offline", False):
        raise ValueError(f"--pi-enable {','.join(names)} needs the network; drop --offline")


def host_matches_any(host, pattern):
    """HOST and PATTERN (each a name or *.DOMAIN) can name the same server (the launcher's rule)."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
    return host == pattern or bare(host).endswith("." + bare(pattern)) or bare(pattern).endswith("." + bare(host)) \
        or bare(host) == bare(pattern)


def telemetry_hosts_in(values):
    """The TELEMETRY_HOSTS that host names or *.DOMAIN patterns VALUES would reach (Pi has none: its telemetry is
    paths on hosts with other uses, TELEMETRY_PATHS)."""
    return sorted({telemetry for value in values for telemetry in TELEMETRY_HOSTS
                   if host_matches_any(str(value), telemetry)})


def check_telemetry(a):
    """Pi's telemetry is never reachable: refused, a start that would let pi.dev or radius.pi.dev (whose telemetry
    paths the proxy refuses) pass the proxy unseen (--no-inspect), or open everything without the proxy that refuses
    them (--egress-all-allow with --no-proxy)."""
    hidden = list(getattr(a, "no_inspect", []) or [])
    unseen = telemetry_hosts_in(hidden) + sorted({host for host, _ in TELEMETRY_PATHS
                                                  if any(host_matches_any(str(value), host) for value in hidden)})
    if unseen:
        raise ValueError(f"Pi's telemetry is always off: the proxy must see {', '.join(unseen)} to refuse it; drop "
                         "that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("Pi's telemetry is always off: --egress-all-allow without the connection proxy would let it "
                         "out; keep the proxy (drop --no-proxy)")


def telemetry_rules(a=None):
    """The proxy's rules for every Pi run (the ruleset "telemetry"): the install report on pi.dev and the bug reports
    on radius.pi.dev refused, and creating gists unless --pi-enable share."""
    rules = [{"id": f"telemetry-pi-path-{number}", "host": host, "path": path, "decision": "deny",
              "reason": "Pi's telemetry is always off"}
             for number, (host, path) in enumerate(TELEMETRY_PATHS, 1)]
    if a is None or "share" not in features(a):
        rules.append({"id": "pi-share", "host": SHARE_RULE[0], "methods": ["POST"], "path": SHARE_RULE[1],
                      "decision": "deny", "reason": "Pi's /share (--pi-enable share opens it)"})
    return rules


def mcp_servers(names, bridge_args, timeouts=None):
    """The --mcp servers NAMES for sandbox-ai's extension (pi.registerMcpServer): each a stdio server that runs the
    launcher's bridge (BRIDGE_ARGS(name)), declared to the model like a built-in tool (exposure direct: Pi's default,
    codemode, would reach it only through its JavaScript tool), with its wait for one call from TIMEOUTS ({name:
    seconds}, the launcher's mcp_tool_timeouts) when given."""
    timeouts = timeouts or {}
    return [{"name": name, "config": {"command": "python3", "args": list(bridge_args(name)), "exposure": "direct",
                                      **({"timeout": timeouts[name]} if timeouts.get(name) else {})}}
            for name in dict.fromkeys(names)]


def extension_config(a, servers=None, llm=None, hooks=None):
    """What sandbox-ai's extension registers for this start: the --mcp servers (SERVERS: mcp_servers), --llm's
    provider and the loop guard (LLM: llm.pi_llm, {"provider", "instructions", "steps"}), the run's hooks (HOOKS:
    {"rules": the rules for Pi's own tools hold, "modules": the approved kits' extension modules, in kit order})."""
    config = {"servers": list(servers or [])}
    if hooks and (hooks.get("rules") or hooks.get("modules")):
        config["hooks"] = {"rules": bool(hooks.get("rules")), "modules": list(hooks.get("modules") or [])}
    if llm:
        config["provider"] = llm.get("provider")
        if llm.get("instructions"):
            config["instructions"] = llm["instructions"]
        if llm.get("steps"):
            config["steps"] = llm["steps"]
    return config


# sandbox-ai's extension for Pi (EXTENSION, read-only): Pi's own extension API registers the --mcp servers and --llm's
# provider, adds the loop guard's instructions to the system prompt and stops a run at its turn limit. It reads
# EXTENSION_CONFIG; whatever goes wrong there, Pi starts all the same (an unreadable file registers nothing).
EXTENSION_SCRIPT = r'''// sandbox-ai's extension for Pi (read-only; the image's pi command loads it with -e).
// It registers what the launcher wrote for this start into /etc/pi/sandbox-ai.json: the --mcp servers (each the
// launcher's stdio bridge), --llm's provider, the loop guard's instructions and turn limit, and the rules for Pi's own
// tools (README, Rulesets: "The agent's own tools") with the approved kits' hooks.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const CONFIG = "/etc/pi/sandbox-ai.json";
const HOOK = ["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "pi"];

// One call of the hook client: {code, reason}. A refusal, or anything that goes wrong on the way, is not 0.
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

// The input as checked, frozen: Pi runs the call with this very object, and an extension after this one that tries
// to change it throws (strict code) or changes nothing - Pi blocks a call whose tool_call handler throws.
function freeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const key of Object.keys(value)) freeze(value[key]);
  }
  return value;
}

// The rules for Pi's own tools: every tool_call asks the sandbox's supervisor first (the rules, the human where a
// rule says ask). The approved kits' extensions load after that check; a last handler asks again for a call whose
// input one of them changed, then freezes it. Each tool_result is recorded.
async function rules(pi, hooks) {
  const checking = hooks.rules === true;
  const modules = Array.isArray(hooks.modules) ? hooks.modules.filter((file) => typeof file === "string") : [];
  const checked = new Map();
  const ask = async (event, ctx) => {
    const answer = await hook("PreToolUse", { tool: event.toolName, args: event.input, callID: event.toolCallId,
                                              cwd: (ctx && typeof ctx.cwd === "string") ? ctx.cwd : process.cwd() });
    return answer.code === 0 ? null : (answer.reason.trim() || "sandbox-ai: the tool call is refused");
  };
  if (checking) {
    pi.on("tool_call", async (event, ctx) => {
      const why = await ask(event, ctx);
      if (why) return { block: true, reason: why };
      if (!modules.length) {
        freeze(event.input);
      } else {
        if (checked.size >= 10000) checked.clear();
        checked.set(event.toolCallId, JSON.stringify(event.input) ?? "null");
      }
      return undefined;
    });
  }
  for (const file of modules) {
    try {
      const factory = (await import(pathToFileURL(file).href)).default;
      if (typeof factory !== "function") throw new Error("its default export is no function(pi)");
      await factory(pi);
    } catch (error) {
      console.error(`sandbox-ai: the kit's hook ${file} did not load: ${error?.message ?? error}`);
    }
  }
  if (checking && modules.length) {
    pi.on("tool_call", async (event, ctx) => {
      const before = checked.get(event.toolCallId);
      checked.delete(event.toolCallId);
      if (before !== (JSON.stringify(event.input) ?? "null")) {
        const why = await ask(event, ctx);
        if (why) return { block: true, reason: why };
      }
      freeze(event.input);
      return undefined;
    });
  }
  if (checking) {
    pi.on("tool_result", async (event, ctx) => {
      const text = JSON.stringify(event.content ?? null) ?? "";
      await hook("PostToolUse", { tool: event.toolName, callID: event.toolCallId,
                                  cwd: (ctx && typeof ctx.cwd === "string") ? ctx.cwd : process.cwd(),
                                  result_bytes: Buffer.byteLength(text) });
      return undefined;
    });
  }
}

function readConfig() {
  try {
    const config = JSON.parse(readFileSync(CONFIG, "utf8"));
    return config && typeof config === "object" && !Array.isArray(config) ? config : {};
  } catch {
    return {};
  }
}

export default async function sandboxAi(pi) {
  const config = readConfig();
  const provider = config.provider;
  if (provider && typeof provider.id === "string" && provider.config && typeof provider.config === "object") {
    try {
      pi.registerProvider(provider.id, provider.config);
    } catch (error) {
      console.error(`sandbox-ai: --llm's provider was not registered: ${error?.message ?? error}`);
    }
  }
  for (const server of Array.isArray(config.servers) ? config.servers : []) {
    if (!server || typeof server.name !== "string" || !server.config || typeof server.config !== "object") continue;
    try {
      pi.registerMcpServer(server.name, server.config);
    } catch (error) {
      console.error(`sandbox-ai: the MCP server ${server.name} was not registered: ${error?.message ?? error}`);
    }
  }
  const instructions = typeof config.instructions === "string" ? config.instructions : "";
  if (instructions) {
    pi.on("before_agent_start", (event) => {
      const options = event.systemPromptOptions;
      options.sections = { ...(options.sections || {}), sandbox_ai_loop_guard: instructions };
    });
  }
  const steps = Number.isInteger(config.steps) && config.steps > 0 ? config.steps : 0;
  if (steps) {
    let turns = 0;
    pi.on("agent_start", () => {
      turns = 0;
    });
    pi.on("turn_end", (_event, ctx) => {
      turns += 1;
      if (turns < steps) return;
      const why = `sandbox-ai: the loop guard's limit of ${steps} turns is reached; the run stops`;
      if (ctx.hasUI) ctx.ui.notify(why, "warning");
      else console.error(why);
      ctx.abort();
    });
  }
  if (config.hooks && typeof config.hooks === "object") {
    await rules(pi, config.hooks);
  }
}
'''


def files(a, config):
    """{path in the session directory: content}: the extension and what it registers (CONFIG: extension_config)."""
    return {f"{SESSION_DIR}/{EXTENSION.rsplit('/', 1)[1]}": EXTENSION_SCRIPT,
            f"{SESSION_DIR}/{EXTENSION_CONFIG.rsplit('/', 1)[1]}": json.dumps(config, indent=2) + "\n"}


def environment(a):
    """Pi's variables: no automatic network activity (unless model-catalog or updates), no version check (unless
    updates), the telemetry opt-out (never dropped)."""
    dropped = {variable for name in features(a) for variable in FEATURES[name].get("drops", [])}
    env = {key: value for key, value in BASE_ENV.items() if key not in dropped}
    env.update(TELEMETRY_ENV)
    return env


def model_effort_args(a):
    """Pi's own arguments for --model (without --llm, whose model llm.agent_arguments gives) and --effort
    (--thinking)."""
    args = []
    model = getattr(a, "model", None)
    if model and not getattr(a, "llm", None):
        args += ["--model", model]
    if getattr(a, "effort", None):
        args += ["--thinking", a.effort]
    return args


def plan(a):
    """The dry run's "pi": the reviewed release, its extension, what is on and what stays off."""
    names = features(a)
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "extension": EXTENSION, "sessions": "~/" + STORAGE, "own_sandbox": False,
            "approvals": False,  # Pi asks before nothing (upstream's design): the sandbox is the boundary
            # Never switchable: the opt-out variable set, these paths refused by the proxy.
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "blocked_paths": [f"{host}{path}" for host, path in TELEMETRY_PATHS],
                          "environment": sorted(TELEMETRY_ENV)},
            "enabled": [{"feature": name, "hosts": FEATURES[name]["hosts"]} for name in names],
            "off": [name for name in FEATURES if name not in names]}


def login_document_ok(document):
    """Pi's auth.json (its /login: a provider entry each, {"type": "oauth", "access", "refresh", "expires"} or
    {"type": "api_key", "key"}; the form of OpenCode's): every entry an object with a type."""
    return isinstance(document, dict) and bool(document) and all(
        isinstance(entry, dict) and entry.get("type") in ("oauth", "api_key") for entry in document.values())


def help_text():
    """--pi-enable's help: every feature with what it does."""
    return ("Pi only: turn on again what a sandbox turns off, each with only its own hosts opened: "
            + "; ".join(f"{name}: {spec['help']}" for name, spec in FEATURES.items())
            + ". Comma-separated or repeated; telemetry is never switchable")
