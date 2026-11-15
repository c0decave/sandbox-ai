"""sandbox-ai's telemetry guard for the agents' own commands: Claude Code, Codex, OpenCode, Qwen Code, Goose and
DeepSeek Harness (Hermes has its own, in its Python: hermes_guard.py).

The agents' telemetry is always off in sandbox-ai (the user's rule) - also from inside the sandbox. The launcher pins
it for the agent it starts (variables, each agent's managed configuration, the proxy), but an agent could start a
second copy of itself from its shell without them: `qwen --telemetry ...`, `QWEN_USAGE_STATISTICS_ENABLED=true qwen`,
`env -u GOOSE_TELEMETRY_OFF GOOSE_TELEMETRY_ENABLED=true goose`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
opencode`, `ENABLE_BETA_TRACING_DETAILED=1 BETA_TRACING_ENDPOINT=... claude`, `dsh` without the launcher's patch.

So every agent image sandbox-ai builds has its command guarded (Containerfile.*: `--install AGENT`):

- Codex, Qwen Code and DSH are Node programs: their entry file (what `codex`, `qwen`, `dsh` run) imports
  telemetry-guard.mjs first, so the guard runs in the agent's own process however the entry is started;
- Claude Code, OpenCode and Goose are native programs: `/usr/local/bin/AGENT` is a shim that runs this file, which
  checks and then becomes the program (exec) kept in REAL_DIR.

Both forms decide alike (refused, pinned_environment; the JavaScript is generated from this file): an argument or a
variable that would turn telemetry on - a pinned opt-out set to another value, a variable named for telemetry,
OpenTelemetry/OTLP, tracing or Datadog with a value of its own, the few that would take a managed configuration away -
is refused with MESSAGE (exit status 2); otherwise every opt-out variable is set again and the agent runs. The
launcher mounts its own copy of both files read-only over the image's (agent_sandbox.telemetry_guard_mounts), so a
newer launcher's guard holds without a rebuild; the image's install is read-only.

Standalone, standard library only: it runs in the sandbox under Debian's python3 (-I -S) before the agent.
"""
import json
import os
from pathlib import Path
import re
import sys

MESSAGE = "sandbox-ai: {name} telemetry is always off in sandbox-ai and cannot be turned on"  # (possessive)
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "qwen": "Qwen Code", "goose": "Goose",
         "dsh": "DeepSeek Harness"}
AGENTS = tuple(NAMES)
# How each command is guarded: "node" - its entry file imports GUARD_JS; "exec" - a shim runs this file, which then
# becomes the program kept at REAL_DIR/AGENT.
KINDS = {"claude": "exec", "codex": "node", "opencode": "exec", "qwen": "node", "goose": "exec", "dsh": "node"}
GUARD_DIR = "/usr/local/lib/sandbox-ai"
GUARD_PY = GUARD_DIR + "/telemetry_guard.py"
GUARD_JS = GUARD_DIR + "/telemetry-guard.mjs"
REAL_DIR = GUARD_DIR + "/real"
COMMAND_DIR = "/usr/local/bin"
PYTHON = "/usr/bin/python3"

# Tools an agent runs that report usage unless told not to: the common DO_NOT_TRACK and the tools' own switches.
# The skills of the built-in kits (docs/skills-review.md): hyperframes' CLI and media-use scripts send PostHog events
# unless HYPERFRAMES_NO_TELEMETRY or DO_NOT_TRACK, and at run time check for updates, install a newer CLI, optional
# packages and skills unless HYPERFRAMES_NO_UPDATE_CHECK, _NO_AUTO_INSTALL, _NO_OPTIONAL_INSTALL, _SKIP_SKILLS (read
# in hyperframes 0.8.214's dist: telemetry/policy.ts, autoUpdate, the optional-install loader, skills); superpowers'
# visual companion loads a remote logo with its version unless SUPERPOWERS_DISABLE_TELEMETRY. (agent_sandbox's
# TELEMETRY_TOOLS is this.)
TOOLS = {"DO_NOT_TRACK": "1", "OPENSPEC_TELEMETRY": "0", "HF_HUB_DISABLE_TELEMETRY": "1",
         "NEXT_TELEMETRY_DISABLED": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "GATSBY_TELEMETRY_DISABLED": "1",
         "ASTRO_TELEMETRY_DISABLED": "1", "HYPERFRAMES_NO_TELEMETRY": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1",
         "HYPERFRAMES_NO_AUTO_INSTALL": "1", "HYPERFRAMES_NO_OPTIONAL_INSTALL": "1",
         "HYPERFRAMES_SKIP_SKILLS": "1", "SUPERPOWERS_DISABLE_TELEMETRY": "1",
         # OpenTelemetry: no SDK and no exporter, an endpoint of yours included.
         "OTEL_SDK_DISABLED": "true", "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none",
         "OTEL_LOGS_EXPORTER": "none"}
# Each agent's own opt-outs, as the launcher sets them (agent_sandbox.TELEMETRY_ENV, qwen.py, goose.py, dsh.py).
AGENT_PINS = {
    # Claude Code 2.1.322: its event logging, error reports, feedback, OpenTelemetry export and the beta tracing
    # (ENABLE_BETA_TRACING_DETAILED with BETA_TRACING_ENDPOINT exports traces and logs whatever
    # CLAUDE_CODE_ENABLE_TELEMETRY says - measured: 0 turns it off).
    "claude": {"DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_BUG_COMMAND": "1",
               "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_ENABLE_TELEMETRY": "0", "ENABLE_BETA_TRACING_DETAILED": "0"},
    "codex": {},  # its switches are configuration keys: /etc/codex/managed_config.toml wins over every other layer
    "opencode": {"OPENCODE_DISABLE_AUTOUPDATE": "1"},
    # The system settings stay the launcher's (QWEN_CODE_SYSTEM_SETTINGS_PATH names them; another path drops them).
    "qwen": {"QWEN_USAGE_STATISTICS_ENABLED": "false", "QWEN_TELEMETRY_ENABLED": "false",
             "GEMINI_TELEMETRY_ENABLED": "false", "QWEN_CODE_SYSTEM_SETTINGS_PATH": "/etc/qwen-code/settings.json"},
    "goose": {"GOOSE_TELEMETRY_OFF": "1", "GOOSE_TELEMETRY_ENABLED": "false"},
    "dsh": {"DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_OTLP_URL": "http://127.0.0.1:9/v1/logs"},
}
# Other agents' opt-outs a process may inherit (Hermes': hermes_guard.PINNED_ENV): taken as they are.
OTHER_PINS = {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}
# A variable named for telemetry, OpenTelemetry or OTLP, tracing or Datadog may hold only sandbox-ai's value (or
# none): Claude Code alone reads dozens of them (OTEL_*, ANT_OTEL_*, BETA_TRACING_ENDPOINT, CLAUDE_CODE_BYOC_ENABLE_
# DATADOG, CLAUDE_CODE_ENHANCED_TELEMETRY_BETA ...), Qwen Code its QWEN_TELEMETRY_*, OpenCode exports to any
# OTEL_EXPORTER_OTLP_ENDPOINT, DSH's analytics read DSH_PRODUCT_ANALYTICS_OTLP_URL.
TELEMETRY_NAME = re.compile(r"TELEMETRY|(?:^|_)OTEL(?:_|$)|OTLP|TRACING|DATADOG")
# Variables that would take an agent's managed configuration (or its pins) away: refused with any value but none
# (None) or those listed. Claude Code keeps the variables it was started with over every settings file, the managed
# one included, when CLAUDE_CODE_ENVIRONMENT_KIND is byoc or its entry point a desktop app's (2.1.322: its host-spawn
# keys); OpenCode reads its managed configuration from OPENCODE_TEST_MANAGED_CONFIG_DIR instead of /etc/opencode.
REFUSED_ENV = {
    "claude": {"CLAUDE_CODE_ENVIRONMENT_KIND": (None, "Claude Code's managed settings stay the last word"),
               "CLAUDE_CODE_ENTRYPOINT": (("claude-desktop", "claude-desktop-3p", "local-agent"),
                                          "Claude Code's managed settings stay the last word")},
    "opencode": {"OPENCODE_TEST_MANAGED_CONFIG_DIR": (None, "OpenCode's configuration stays the one at /etc/opencode")},
}
# Named like telemetry, but no switch of it (Codex' own hand-over between its daemon's processes).
EXEMPT_ENV = {"codex": ("CODEX_DAEMON_TELEMETRY_HANDOFF",)}
# Codex: -c/--config KEY=VALUE of these tables or keys (they win over config.toml, not over the managed
# configuration) - refused unless VALUE is sandbox-ai's own (the launcher gives the agent it starts these).
CODEX_TABLES = ("analytics", "feedback", "otel")
CODEX_PINNED = {"check_for_update_on_startup": "false", "analytics.enabled": "false", "feedback.enabled": "false",
                "otel.metrics_exporter": "none", "otel.exporter": "none", "otel.trace_exporter": "none"}
# DSH: sandbox-ai's patch (dsh.PATCH, read-only at /etc/dsh) goes on every profile's command line - the last
# overlay, with its telemetry rows (and the OTel service they need) disabled - where a `dsh` typed in the sandbox
# lacks it.
DSH_PATCH = "/etc/dsh/sandbox-ai.patch.yml"


def possessive(name):
    """NAME's - or NAME' for a name ending in s (DeepSeek Harness')."""
    return name + ("'" if name.endswith("s") else "'s")


def message(agent):
    """The one message of every refusal: "sandbox-ai: <Agent>'s telemetry is always off ..." """
    return MESSAGE.format(name=possessive(NAMES[agent]))


def pinned(agent):
    """{variable: value} AGENT's guard sets in every start: the tools' opt-outs and its own."""
    return {**TOOLS, **AGENT_PINS[agent]}


def accepted():
    """{variable: value} of every agent's opt-outs: a process may have inherited another agent's (a shell of Claude
    Code starting Qwen Code)."""
    found = dict(OTHER_PINS)
    for agent in AGENTS:
        found.update(pinned(agent))
    return found


def telemetry_name(name):
    return bool(TELEMETRY_NAME.search(name.upper()))


def refusals(agent, environ):
    """(name, what, why) of each variable in ENVIRON that would turn AGENT's telemetry on or take its pins away. An
    empty value counts as unset (pinned_environment sets the opt-out again)."""
    pins, known = pinned(agent), accepted()
    for name, (values, why) in REFUSED_ENV.get(agent, {}).items():
        value = environ.get(name, "")
        if value.strip() and (values is None or value.strip() in values):
            yield name, f"{name}={value}", why
    for name in sorted(environ):
        value = environ[name]
        if not value.strip() or name in EXEMPT_ENV.get(agent, ()) or name in REFUSED_ENV.get(agent, {}):
            continue
        if name in pins and value != pins[name]:
            yield name, f"{name}={value}", f"it stays {pins[name]}"
        elif name not in pins and telemetry_name(name) and value != known.get(name):
            yield name, f"{name}={value}", ("it stays " + known[name]) if name in known else "telemetry is always off"


def refused_environment(agent, environ):
    """(what, why) of the first of refusals(AGENT, ENVIRON), or None."""
    return next(((what, why) for _, what, why in refusals(agent, environ)), None)


def refused_names(agent, environ):
    """The names of every variable in ENVIRON AGENT's guard would refuse (the launcher does not pass them)."""
    return [name for name, _, _ in refusals(agent, environ)]


def options(args):
    """ARGS before a "--" of their own: what the agent reads as its options."""
    args = [str(arg) for arg in args]
    return args[:args.index("--")] if "--" in args else args


def codex_override(args):
    """[(argument as given, KEY, VALUE)] of Codex' -c/--config overrides among ARGS."""
    found, words = [], options(args)
    for index, word in enumerate(words):
        if word in ("-c", "--config"):
            override = words[index + 1] if index + 1 < len(words) else ""
            shown = f"{word} {override}"
        elif word.startswith("--config="):
            override, shown = word[len("--config="):], word
        elif word.startswith("-c") and len(word) > 2 and not word.startswith("--"):
            override, shown = word[2:].lstrip("="), word
        else:
            continue
        key, _, value = override.partition("=")
        found.append((shown, re.sub(r"\s+", "", key).replace('"', "").replace("'", "").lower(), value))
    return found


def codex_key_refused(key, value):
    """Codex' override KEY=VALUE would set a telemetry key to anything but sandbox-ai's value."""
    parts = key.split(".")
    if parts[:1] == ["profiles"] and len(parts) > 2:  # profiles.NAME.analytics...
        parts = parts[2:]
    if not (parts[0] in CODEX_TABLES or parts[0] == "check_for_update_on_startup"):
        return False
    plain = value.strip().strip("'\"").strip().lower()
    return CODEX_PINNED.get(".".join(parts)) != plain


def refused_args(agent, args):
    """(what, why) of AGENT's command-line ARGS that would turn telemetry on, or None. Only before a "--"."""
    if agent == "qwen":
        for word in options(args):
            flag = word.split("=", 1)[0].lower()
            if flag.startswith("--telemetry") or flag.startswith("--no-telemetry"):
                return word, "its telemetry options win over every setting"
    if agent == "codex":
        for shown, key, value in codex_override(args):
            if codex_key_refused(key, value):
                return shown, "its analytics, feedback, OpenTelemetry and update keys stay sandbox-ai's"
    return None


def refused(agent, args, environ):
    """(what, why) of what would turn AGENT's telemetry on - a variable of ENVIRON (refused_environment) or an
    argument (refused_args; Qwen Code's relaunch arguments in QWEN_CODE_RELAUNCH_ARGS too) - or None."""
    found = refused_environment(agent, environ) or refused_args(agent, args)
    if found is None and agent == "qwen" and environ.get("QWEN_CODE_RELAUNCH_ARGS"):
        try:
            relaunch = json.loads(environ["QWEN_CODE_RELAUNCH_ARGS"])
        except ValueError:
            relaunch = None
        if isinstance(relaunch, list):
            found = refused_args(agent, relaunch)
    return found


def pinned_environment(agent, environ):
    """ENVIRON for AGENT: every opt-out set (again), a telemetry variable left empty dropped."""
    env = {name: value for name, value in environ.items()
           if value.strip() or not (telemetry_name(name) or name in REFUSED_ENV.get(agent, {}))}
    env.update(pinned(agent))
    return env


def dsh_args(args, patch=DSH_PATCH, exists=os.path.exists):
    """DSH's ARGS with sandbox-ai's patch right after the profile's name (DSH's launcher reads its own flags there;
    dsh.command_args does the same for the start), where it exists and is not given: `NAME ...`, `--profile NAME ...`,
    `--profile=NAME ...`. Not for `plugin`, DSH's own flags first (--help, --version, dumps) or a default dump."""
    args = [str(arg) for arg in args]
    if not args or patch in args or not exists(patch) or "--dump-default-config" in options(args):
        return args
    if args[0].startswith("--profile="):
        return [args[0], "--patch", patch, *args[1:]]
    if args[0] == "--profile" and len(args) > 1:
        return [*args[:2], "--patch", patch, *args[2:]]
    if args[0].startswith("-") or args[0] == "plugin":
        return args
    return [args[0], "--patch", patch, *args[1:]]


def run(agent, args, environ=None, execve=None, real_dir=REAL_DIR, stderr=None):
    """The guarded command of an exec agent: MESSAGE and 2 for what would turn its telemetry on, else become the
    agent (REAL_DIR/AGENT) with every opt-out set."""
    stderr = stderr or sys.stderr
    if agent not in NAMES:
        stderr.write(f"sandbox-ai: the telemetry guard knows no agent {agent!r}\n")
        return 2
    environ = dict(os.environ if environ is None else environ)
    found = refused(agent, args, environ)
    if found is not None:
        stderr.write(f"{message(agent)} ({found[0]} is refused: {found[1]})\n")
        return 2
    real = f"{real_dir}/{agent}"
    try:
        (execve or os.execve)(real, [agent, *args], pinned_environment(agent, environ))
    except OSError as exc:
        stderr.write(f"sandbox-ai: {NAMES[agent]} cannot be started ({real}: {exc.strerror or exc})\n")
        return 127
    return 0  # (only where EXECVE returns: the tests')


def import_line(agent, guard_js=GUARD_JS):
    """The line a Node agent's entry file starts with (after its #!): the guard, before anything of the agent's."""
    return (f'import "file://{guard_js}?agent={agent}"; '
            f"// sandbox-ai: {possessive(NAMES[agent])} telemetry is always off (sandbox/telemetry_guard.py)")


def shim(agent, guard_py=GUARD_PY):
    """The command of an exec agent (/usr/local/bin/AGENT): this file, which becomes the agent."""
    return (f"#!/bin/sh\n# sandbox-ai: {possessive(NAMES[agent])} telemetry is always off (sandbox/telemetry_guard.py)\n"
            f'exec {PYTHON} -IS {guard_py} {agent} "$@"\n')


def install(agent, command_dir=COMMAND_DIR, guard_dir=GUARD_DIR):
    """At the image's build (Containerfile.*): guard AGENT's command - a Node agent's entry file imports the guard, an
    exec agent's command becomes the shim and the program moves to REAL_DIR. Writes the JavaScript beside this file.
    Done once: a second call changes nothing."""
    command, guard = Path(command_dir) / agent, Path(guard_dir)
    guard.mkdir(parents=True, exist_ok=True)
    (guard / Path(GUARD_JS).name).write_text(javascript())
    if KINDS[agent] == "node":
        entry = command.resolve(strict=True)
        text = entry.read_text()
        line = import_line(agent, str(guard / Path(GUARD_JS).name))
        if line not in text:
            first, newline, rest = text.partition("\n")
            entry.write_text(f"{first}\n{line}\n{rest}" if first.startswith("#!") and newline else f"{line}\n{text}")
        return entry
    real = guard / "real" / agent
    real.parent.mkdir(parents=True, exist_ok=True)
    if not (real.exists() or real.is_symlink()):
        if command.is_symlink():
            real.symlink_to(command.resolve(strict=True))
            command.unlink()
        else:
            os.replace(command, real)
    if command.exists() or command.is_symlink():
        command.unlink()
    command.write_text(shim(agent, str(guard / Path(GUARD_PY).name)))
    command.chmod(0o755)
    return real


def spec(agent):
    """What the JavaScript guard needs of AGENT."""
    return {"name": NAMES[agent], "message": message(agent), "pinned": pinned(agent),
            "refused": {name: [list(values) if values else None, why]
                        for name, (values, why) in REFUSED_ENV.get(agent, {}).items()},
            "exempt": list(EXEMPT_ENV.get(agent, ()))}


JAVASCRIPT = r"""// sandbox-ai's telemetry guard for a Node agent (generated by sandbox/telemetry_guard.py: do not edit).
// The agent's entry file imports it first; read-only in the sandbox. Telemetry is always off in sandbox-ai.
import { existsSync } from "node:fs";

const SPECS = __SPECS__;
const ACCEPTED = __ACCEPTED__;
const TELEMETRY_NAME = /TELEMETRY|(?:^|_)OTEL(?:_|$)|OTLP|TRACING|DATADOG/;
const CODEX_TABLES = __CODEX_TABLES__;
const CODEX_PINNED = __CODEX_PINNED__;
const DSH_PATCH = __DSH_PATCH__;

const agent = new URL(import.meta.url).searchParams.get("agent");
const spec = Object.hasOwn(SPECS, agent) ? SPECS[agent] : null;

const blank = (value) => String(value ?? "").trim() === "";
const telemetryName = (name) => TELEMETRY_NAME.test(name.toUpperCase());
const options = (args) => (args.includes("--") ? args.slice(0, args.indexOf("--")) : args);

function refusedEnvironment(env) {
  for (const [name, [values, why]] of Object.entries(spec.refused)) {
    const value = env[name] ?? "";
    if (!blank(value) && (values === null || values.includes(value.trim()))) return [`${name}=${value}`, why];
  }
  for (const name of Object.keys(env).sort()) {
    const value = env[name];
    if (blank(value) || spec.exempt.includes(name) || Object.hasOwn(spec.refused, name)) continue;
    if (Object.hasOwn(spec.pinned, name) && value !== spec.pinned[name]) return [`${name}=${value}`, `it stays ${spec.pinned[name]}`];
    if (!Object.hasOwn(spec.pinned, name) && telemetryName(name) && value !== ACCEPTED[name]) {
      return [`${name}=${value}`, Object.hasOwn(ACCEPTED, name) ? `it stays ${ACCEPTED[name]}` : "telemetry is always off"];
    }
  }
  return null;
}

function codexOverrides(args) {
  const found = [];
  const words = options(args);
  words.forEach((word, index) => {
    let override, shown;
    if (word === "-c" || word === "--config") {
      override = index + 1 < words.length ? words[index + 1] : "";
      shown = `${word} ${override}`;
    } else if (word.startsWith("--config=")) {
      override = word.slice("--config=".length);
      shown = word;
    } else if (word.startsWith("-c") && word.length > 2 && !word.startsWith("--")) {
      override = word.slice(2).replace(/^=+/, "");
      shown = word;
    } else return;
    const at = override.indexOf("=");
    const key = (at < 0 ? override : override.slice(0, at)).replace(/\s+/g, "").replace(/["']/g, "").toLowerCase();
    found.push([shown, key, at < 0 ? "" : override.slice(at + 1)]);
  });
  return found;
}

function codexKeyRefused(key, value) {
  let parts = key.split(".");
  if (parts[0] === "profiles" && parts.length > 2) parts = parts.slice(2);
  if (!(CODEX_TABLES.includes(parts[0]) || parts[0] === "check_for_update_on_startup")) return false;
  const plain = value.trim().replace(/^['"]+|['"]+$/g, "").trim().toLowerCase();
  return CODEX_PINNED[parts.join(".")] !== plain;
}

function refusedArgs(args) {
  if (agent === "qwen") {
    for (const word of options(args)) {
      const flag = word.split("=", 1)[0].toLowerCase();
      if (flag.startsWith("--telemetry") || flag.startsWith("--no-telemetry")) {
        return [word, "its telemetry options win over every setting"];
      }
    }
  }
  if (agent === "codex") {
    for (const [shown, key, value] of codexOverrides(args)) {
      if (codexKeyRefused(key, value)) return [shown, "its analytics, feedback, OpenTelemetry and update keys stay sandbox-ai's"];
    }
  }
  return null;
}

function refused(args, env) {
  let found = refusedEnvironment(env) ?? refusedArgs(args);
  if (found === null && agent === "qwen" && env.QWEN_CODE_RELAUNCH_ARGS) {
    let relaunch = null;
    try {
      relaunch = JSON.parse(env.QWEN_CODE_RELAUNCH_ARGS);
    } catch {
      relaunch = null;
    }
    if (Array.isArray(relaunch)) found = refusedArgs(relaunch.map(String));
  }
  return found;
}

function dshArgs(args) {
  if (args.length === 0 || args.includes(DSH_PATCH) || !existsSync(DSH_PATCH)) return args;
  if (options(args).includes("--dump-default-config")) return args;
  if (args[0].startsWith("--profile=")) return [args[0], "--patch", DSH_PATCH, ...args.slice(1)];
  if (args[0] === "--profile" && args.length > 1) return [...args.slice(0, 2), "--patch", DSH_PATCH, ...args.slice(2)];
  if (args[0].startsWith("-") || args[0] === "plugin") return args;
  return [args[0], "--patch", DSH_PATCH, ...args.slice(1)];
}

if (spec === null) {
  process.stderr.write(`sandbox-ai: the telemetry guard knows no agent ${JSON.stringify(agent)}\n`);
  process.exit(2);
}
const args = process.argv.slice(2);
const found = refused(args, process.env);
if (found !== null) {
  process.stderr.write(`${spec.message} (${found[0]} is refused: ${found[1]})\n`);
  process.exit(2);
}
for (const [name, value] of Object.entries(process.env)) {
  if (blank(value) && (telemetryName(name) || Object.hasOwn(spec.refused, name))) delete process.env[name];
}
Object.assign(process.env, spec.pinned);
if (agent === "dsh") process.argv.splice(2, args.length, ...dshArgs(args));
"""


def javascript():
    """The JavaScript guard (GUARD_JS) with every agent's spec: the same decisions as refused, pinned_environment and
    dsh_args (tests/test_telemetry_guard.py runs both on the same cases)."""
    values = {"__SPECS__": {agent: spec(agent) for agent in AGENTS if KINDS[agent] == "node"},
              "__ACCEPTED__": accepted(), "__CODEX_TABLES__": list(CODEX_TABLES), "__CODEX_PINNED__": CODEX_PINNED,
              "__DSH_PATCH__": DSH_PATCH}
    text = JAVASCRIPT
    for placeholder, value in values.items():
        text = text.replace(placeholder, json.dumps(value, sort_keys=True))
    return text


def main(argv):
    """telemetry_guard.py AGENT [ARGS...]: the guarded command (the shim's); --install AGENT...: guard the image's
    commands (Containerfile.*); --javascript: print GUARD_JS."""
    if argv[:1] == ["--install"] and len(argv) > 1:
        for agent in argv[1:]:
            print(f"sandbox-ai: {possessive(NAMES[agent])} command guarded ({install(agent)})")
        return 0
    if argv[:1] == ["--javascript"]:
        sys.stdout.write(javascript())
        return 0
    if not argv:
        sys.stderr.write("usage: telemetry_guard.py AGENT [ARGS...] | --install AGENT... | --javascript\n")
        return 2
    return run(argv[0], argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
