"""sandbox-ai's telemetry guard for the agents' own commands: Claude Code, Codex, OpenCode, Qwen Code, Goose,
DeepSeek Harness, OpenCode 2, Pi and Letta Code, and for the Python of OpenHands and nanobot (Hermes has its own, in
its Python: hermes_guard.py).

The agents' telemetry is always off in sandbox-ai (the user's rule) - also from inside the sandbox. The launcher pins
it for the agent it starts (variables, each agent's managed configuration, the proxy), but an agent could start a
second copy of itself from its shell without them: `qwen --telemetry ...`, `QWEN_USAGE_STATISTICS_ENABLED=true qwen`,
`env -u GOOSE_TELEMETRY_OFF GOOSE_TELEMETRY_ENABLED=true goose`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
opencode`, `ENABLE_BETA_TRACING_DETAILED=1 BETA_TRACING_ENDPOINT=... claude`, `dsh` without the launcher's patch.

So every agent image sandbox-ai builds has its command guarded (Containerfile.*: `--install AGENT`):

- Codex, Qwen Code, DSH, Pi and Letta Code are Node programs: their entry file (what `codex`, `qwen`, `dsh`, `letta`
  run; Pi's bundle behind the image's `pi` wrapper: ENTRIES) imports telemetry-guard.mjs first, so the guard runs in
  the agent's own process however the entry is started;
- Claude Code, OpenCode, Goose and OpenCode 2 are native programs: `/usr/local/bin/AGENT` (and its ALIASES: the
  OpenCode 2 image's `opencode`) is a shim that runs this file, which checks and then becomes the program (exec) kept
  in REAL_DIR;
- OpenHands and nanobot are Python programs in a venv of their own: a .pth in its site-packages (SITE_PACKAGES, PTH)
  runs python_guard at the start of every process of that Python - the agent, its servers, a script run with it - and
  OpenHands' own `openhands` command (Debian's python3, which starts the Canvas) calls it first as well.

Both forms decide alike (refused, pinned_environment; the JavaScript is generated from this file): an argument or a
variable that would turn telemetry on - a pinned opt-out set to another value, a variable named for telemetry,
OpenTelemetry/OTLP, tracing or Datadog with a value of its own, the few that would take a managed configuration away -
is refused with MESSAGE (exit status 2); otherwise every opt-out variable is set again and the agent runs. The
launcher mounts its own copy of both files read-only over the image's (agent_sandbox.telemetry_guard_mounts), so a
newer launcher's guard holds without a rebuild; the image's install is read-only.

Codex's guard also turns a plain `codex login` into the device-code login (codex_login_args): the browser login's
callback to the sandbox's 127.0.0.1:1455 is out of reach of a browser outside the sandbox.

Standalone, standard library only: it runs in the sandbox under Debian's python3 (-I -S) before the agent.
"""
import json
import os
from pathlib import Path
import re
import sys

MESSAGE = "sandbox-ai: {name} telemetry is always off in sandbox-ai and cannot be turned on"  # (possessive)
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "qwen": "Qwen Code", "goose": "Goose",
         "dsh": "DeepSeek Harness", "opencode2": "OpenCode 2", "pi": "Pi", "letta": "Letta Code",
         "openhands": "OpenHands", "nanobot": "nanobot"}
AGENTS = tuple(NAMES)
# How each command is guarded: "node" - its entry file imports GUARD_JS; "exec" - a shim runs this file, which then
# becomes the program kept at REAL_DIR/AGENT; "python" - a .pth in its venv's site-packages runs python_guard.
KINDS = {"claude": "exec", "codex": "node", "opencode": "exec", "qwen": "node", "goose": "exec", "dsh": "node",
         "opencode2": "exec", "pi": "node", "letta": "node", "openhands": "python", "nanobot": "python"}
# Other names of an exec agent's program in its image, each a shim of the agent's as well (Containerfile.opencode2:
# `opencode2` is a link to `opencode`, the binary) - unless that name is another agent's guarded command already (the
# team image: `opencode` is OpenCode 1's, and OpenCode 2 is only `opencode2` there).
ALIASES = {"opencode2": ("opencode",)}
# A Node agent whose command is no link to its entry: the entry itself (Containerfile.pi: `pi` is sandbox-ai's sh
# wrapper that loads its extension, then runs Pi's bundle).
ENTRIES = {"pi": "/opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"}
# A Python agent's venv (Containerfile.openhands, .nanobot: uv's CPython 3.12) and the .pth that loads the guard there.
SITE_PACKAGES = {"openhands": "/opt/openhands/venv/lib/python3.12/site-packages",
                 "nanobot": "/opt/nanobot/venv/lib/python3.12/site-packages"}
PTH = "sandbox-ai-telemetry-off.pth"
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
    # Goose 1.57.0 (goose.py: TELEMETRY_ENV) also has a Langfuse tracing layer (crates/goose/src/tracing/
    # langfuse_layer.rs: it posts to LANGFUSE_URL/api/public/ingestion once LANGFUSE_PUBLIC_KEY and
    # LANGFUSE_SECRET_KEY, or the LANGFUSE_INIT_PROJECT_ pair, are set - measured with the release binary): the keys
    # are empty.
    "goose": {"GOOSE_TELEMETRY_OFF": "1", "GOOSE_TELEMETRY_ENABLED": "false", "LANGFUSE_PUBLIC_KEY": "",
              "LANGFUSE_SECRET_KEY": "", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "",
              "LANGFUSE_INIT_PROJECT_SECRET_KEY": ""},
    "dsh": {"DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_OTLP_URL": "http://127.0.0.1:9/v1/logs"},
    # OpenCode 2.2.5 (opencode2.py: TELEMETRY_ENV): Bun's crash reporter; its OpenTelemetry is TOOLS'.
    "opencode2": {"BUN_ENABLE_CRASH_REPORTING": "0"},
    # Pi 1.4.0 (pi.py: TELEMETRY_ENV): its install report and provider attribution (core/telemetry.js).
    "pi": {"PI_TELEMETRY": "0"},
    # Letta Code 0.34.36 (letta.py: TELEMETRY_ENV): its usage and error events, Scarf's install analytics.
    "letta": {"LETTA_CODE_TELEM": "0", "SCARF_ANALYTICS": "false"},
    # OpenHands (openhands.py: TELEMETRY_ENV): the Canvas' PostHog, the agent server's and the automation backend's
    # exporters and consent, browser-use's PostHog, Laminar (with a key) and the SDK's OpenTelemetry endpoints.
    "openhands": {"AGENT_CANVAS_DISABLE_TELEMETRY": "1", "VITE_DO_NOT_TRACK": "1", "OH_TELEMETRY_EXPORTER": "none",
                  "OH_TELEMETRY_CONSENT": "denied", "OH_TELEMETRY_CONSENT_MODE": "override",
                  "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false", "LMNR_PROJECT_API_KEY": "",
                  "OTEL_ENDPOINT": "", "OTEL_EXPORTER_OTLP_ENDPOINT": "", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""},
    # nanobot 0.4.0 (nanobot.py: TELEMETRY_ENV): Langfuse's tracing, which starts with its keys - empty.
    "nanobot": {"LANGFUSE_SECRET_KEY": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_TRACING_ENABLED": "false"},
}
# Variables of an agent's own telemetry service that are not named for telemetry: they may hold only the pinned
# value (or none) - Laminar's (OpenHands' tracing), Langfuse's (nanobot's: its host, keys, sampling; Goose's: its
# keys and LANGFUSE_URL, _HOST, _BASE_URL).
AGENT_NAMES = {"openhands": r"^LMNR_", "nanobot": r"^LANGFUSE_", "goose": r"^LANGFUSE_"}
# Other agents' opt-outs a process may inherit (Hermes': hermes_guard.PINNED_ENV): taken as they are.
OTHER_PINS = {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}
# A variable named for telemetry, OpenTelemetry or OTLP, tracing or Datadog may hold only sandbox-ai's value (or
# none): Claude Code alone reads dozens of them (OTEL_*, ANT_OTEL_*, BETA_TRACING_ENDPOINT, CLAUDE_CODE_BYOC_ENABLE_
# DATADOG, CLAUDE_CODE_ENHANCED_TELEMETRY_BETA ...), Qwen Code its QWEN_TELEMETRY_*, OpenCode exports to any
# OTEL_EXPORTER_OTLP_ENDPOINT, DSH's analytics read DSH_PRODUCT_ANALYTICS_OTLP_URL.
TELEMETRY_NAME = re.compile(r"TELEMETRY|(?:^|_)OTEL(?:_|$)|OTLP|TRACING|DATADOG")
# Variables that would take an agent's managed configuration (or its pins) away: refused with any value but none
# (None), with those listed, or ({"only": VALUES}) with any but VALUES (compared in lower case, untrimmed). Claude Code keeps the variables it was started with over every settings file, the managed
# one included, when CLAUDE_CODE_ENVIRONMENT_KIND is byoc or its entry point a desktop app's (2.1.322: its host-spawn
# keys); OpenCode reads its managed configuration from OPENCODE_TEST_MANAGED_CONFIG_DIR instead of /etc/opencode.
REFUSED_ENV = {
    "claude": {"CLAUDE_CODE_ENVIRONMENT_KIND": (None, "Claude Code's managed settings stay the last word"),
               "CLAUDE_CODE_ENTRYPOINT": (("claude-desktop", "claude-desktop-3p", "local-agent"),
                                          "Claude Code's managed settings stay the last word")},
    "opencode": {"OPENCODE_TEST_MANAGED_CONFIG_DIR": (None, "OpenCode's configuration stays the one at /etc/opencode")},
    "opencode2": {"OPENCODE_TEST_MANAGED_CONFIG_DIR": (None, "OpenCode 2's system configuration stays the image's")},
    # Pi: PI_OFFLINE (no automatic network activity: the install report and /bug's upload too) is the launcher's
    # unless --pi-enable leaves it out; it may stay on or unset, never be switched off. Pi 1.4.0 is offline only for
    # 1, true or yes in any case, untrimmed (main.js isTruthyEnvFlag, package-manager.js and tools-manager.js
    # isOfflineModeEnabled): "OFF", "on", "2" or " 1" switch it off as "0" does.
    "pi": {"PI_OFFLINE": ({"only": ("1", "true", "yes")}, "Pi's offline mode stays on")},
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


def telemetry_name(name, agent=None):
    """Whether NAME is a telemetry variable (TELEMETRY_NAME), or one of AGENT's telemetry service (AGENT_NAMES)."""
    return bool(TELEMETRY_NAME.search(name.upper())) or \
        bool(agent in AGENT_NAMES and re.search(AGENT_NAMES[agent], name.upper()))


def refusals(agent, environ):
    """(name, what, why) of each variable in ENVIRON that would turn AGENT's telemetry on or take its pins away. An
    empty value counts as unset (pinned_environment sets the opt-out again)."""
    pins, known = pinned(agent), accepted()
    for name, (values, why) in REFUSED_ENV.get(agent, {}).items():
        value = environ.get(name, "")
        if value.strip() and (values is None or (value.lower() not in values["only"] if isinstance(values, dict)
                                                 else value.strip() in values)):
            yield name, f"{name}={value}", why
    for name in sorted(environ):
        value = environ[name]
        if not value.strip() or name in EXEMPT_ENV.get(agent, ()) or name in REFUSED_ENV.get(agent, {}):
            continue
        if name in pins and value != pins[name]:
            yield name, f"{name}={value}", f"it stays {pins[name]}"
        elif name not in pins and telemetry_name(name, agent) and value != known.get(name):
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
        except (ValueError, RecursionError):
            relaunch = None
        if isinstance(relaunch, list):
            found = refused_args(agent, relaunch)
    return found


def pinned_environment(agent, environ):
    """ENVIRON for AGENT: every opt-out set (again), a telemetry variable left empty dropped."""
    env = {name: value for name, value in environ.items()
           if value.strip() or not (telemetry_name(name, agent) or name in REFUSED_ENV.get(agent, {}))}
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


# Codex's login (0.169.0): a plain `codex login` starts a login server on the sandbox's 127.0.0.1:1455 and sends the
# browser back there (redirect_uri=http://127.0.0.1:1455/auth/callback) - a browser outside the sandbox never reaches
# it. In a sandbox it runs the device-code login instead (--device-auth: a code typed on auth.openai.com, no callback),
# as `sandbox-ai login codex` does; one line on stderr says why. Left as they are: a login of its own kind
# (CODEX_LOGIN_KEPT), `codex login status|help`, and any login while SANDBOX_AI_LOGIN_CALLBACK=1 (the browser login on
# purpose: sandbox-ai relays its one callback when the human opens the link from the browser question, approvals).
CODEX_LOGIN_KEPT = ("--device-auth", "--with-api-key", "--with-access-token", "--api-key", "-h", "--help")
# Codex's top-level options that take a value (codex --help): the word after them is no subcommand.
CODEX_VALUE_OPTIONS = ("-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-i",
                       "--image", "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
                       "--add-dir", "-a", "--ask-for-approval")
LOGIN_CALLBACK_ENV = "SANDBOX_AI_LOGIN_CALLBACK"
CODEX_LOGIN_NOTE = ("sandbox-ai: `codex login` runs the device-code login in a sandbox (--device-auth): a browser "
                    "outside the sandbox cannot reach the login's callback on the sandbox's 127.0.0.1:1455 "
                    "(SANDBOX_AI_LOGIN_CALLBACK=1 keeps the browser login; its callback is relayed when you open the "
                    "link from sandbox-ai's question)\n")


def codex_login_args(args, environ):
    """Codex's ARGS with --device-auth added after a plain `login` (CODEX_LOGIN_NOTE says why), or None when they stay
    as they are (no login, a login of its own kind, its status or help, LOGIN_CALLBACK_ENV=1 in ENVIRON)."""
    if str(environ.get(LOGIN_CALLBACK_ENV, "")).strip() == "1":
        return None
    words = options(args)
    index = 0
    while index < len(words):
        word = words[index]
        if word in CODEX_VALUE_OPTIONS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        break
    if index >= len(words) or words[index] != "login":
        return None
    rest = [str(arg) for arg in args][index + 1:]
    if any(word in CODEX_LOGIN_KEPT or word.split("=", 1)[0] in CODEX_LOGIN_KEPT for word in options(rest)):
        return None
    positional = [word for word in options(rest) if not word.startswith("-")]
    if positional[:1] and positional[0] in ("status", "help"):
        return None
    args = [str(arg) for arg in args]
    return [*args[:index + 1], "--device-auth", *args[index + 1:]]


# Goose's hooks (sandbox/goose.py: HOOKS_PLUGIN, HOOKS_LINK): sandbox-ai's plugin with the rules for Goose's own
# tools and the approved kit hooks lies read-only in /etc/goose; Goose takes plugins only from its plugin folders.
GOOSE_PLUGIN = "/etc/goose/sandbox-ai-hooks"
GOOSE_PLUGIN_NAME = "sandbox-ai-hooks"


def goose_hooks(environ, home=None, cwd=None):
    """None if Goose about to start gets sandbox-ai's hooks plugin (or there is none this run), else why not. Links
    the plugin into the user's plugin folder (~/.agents/plugins) when the link is missing; refuses what would leave it
    out: GOOSE_PATH_ROOT (another plugin folder), something else under its name there or in the project's
    .agents/plugins (the project's comes first and takes the name), the plugin disabled in a settings.json of Goose's
    (the user's, the project's, its local one) or in the user's config.yaml."""
    if not os.path.isfile(f"{GOOSE_PLUGIN}/hooks/hooks.json"):
        return None
    if (environ.get("GOOSE_PATH_ROOT") or "").strip():
        return "GOOSE_PATH_ROOT moves Goose's plugin folder away from sandbox-ai's hooks"
    home = home or environ.get("HOME") or "/root"
    try:
        cwd = cwd or os.getcwd()
    except OSError:
        cwd = "/"
    link = os.path.join(home, ".agents", "plugins", GOOSE_PLUGIN_NAME)
    try:
        if not os.path.lexists(link):
            os.makedirs(os.path.dirname(link), mode=0o755, exist_ok=True)
            os.symlink(GOOSE_PLUGIN, link)
        if os.path.realpath(link) != os.path.realpath(GOOSE_PLUGIN):
            return f"{link} is not sandbox-ai's hooks plugin ({GOOSE_PLUGIN}); remove it"
    except OSError as exc:
        return f"sandbox-ai's hooks plugin could not be linked at {link} ({exc.strerror or exc})"
    project = os.path.join(cwd, ".agents", "plugins", GOOSE_PLUGIN_NAME)
    if os.path.lexists(project) and os.path.realpath(project) != os.path.realpath(GOOSE_PLUGIN):
        return f"{project} takes the name of sandbox-ai's hooks plugin; remove it"
    for settings in (os.path.join(home, ".config", "goose", "settings.json"),
                     os.path.join(cwd, ".config", "goose", "settings.json"),
                     os.path.join(cwd, ".config", "goose", "settings.local.json")):
        try:
            with open(settings, encoding="utf-8") as stream:
                document = json.load(stream)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError) as exc:
            return f"{settings} cannot be read ({exc}); Goose would not start its plugins as sandbox-ai's hooks need"
        disabled = document.get("disabled") if isinstance(document, dict) else None
        if isinstance(disabled, list) and GOOSE_PLUGIN_NAME in disabled:
            return f"{settings} disables {GOOSE_PLUGIN_NAME}; take it out of \"disabled\""
    try:
        with open(os.path.join(home, ".config", "goose", "config.yaml"), encoding="utf-8") as stream:
            config = stream.read()
    except OSError:
        config = ""
    for found in re.finditer(re.escape(link), config):
        if re.match(r"[\"']?\s*:\s*(\{[^}]*\benabled\s*:\s*false|\n(?:[ \t]+\S[^\n]*\n)*?[ \t]+enabled\s*:\s*false)",
                    config[found.end():]):
            return f"~/.config/goose/config.yaml disables {link} (plugins: enabled: false); set it to true"
    return None


def run(agent, args, environ=None, execve=None, real_dir=REAL_DIR, stderr=None):
    """The guarded command of an exec agent: MESSAGE and 2 for what would turn its telemetry on, else become the
    agent (REAL_DIR/AGENT) with every opt-out set. Goose: not without sandbox-ai's hooks plugin when the run has one
    (goose_hooks)."""
    stderr = stderr or sys.stderr
    if agent not in NAMES:
        stderr.write(f"sandbox-ai: the telemetry guard knows no agent {agent!r}\n")
        return 2
    environ = dict(os.environ if environ is None else environ)
    found = refused(agent, args, environ)
    if found is not None:
        stderr.write(f"{message(agent)} ({found[0]} is refused: {found[1]})\n")
        return 2
    hooks = goose_hooks(environ) if agent == "goose" else None
    if hooks:
        stderr.write(f"sandbox-ai: Goose is not started: the rules for its own tools and the kits' approved hooks "
                     f"hold through sandbox-ai's hooks plugin, and {hooks}\n")
        return 2
    real = f"{real_dir}/{agent}"
    try:
        (execve or os.execve)(real, [agent, *args], pinned_environment(agent, environ))
    except OSError as exc:
        stderr.write(f"sandbox-ai: {NAMES[agent]} cannot be started ({real}: {exc.strerror or exc})\n")
        return 127
    return 0  # (only where EXECVE returns: the tests')


def python_guard(agent, environ=None, stderr=None, exit=None):
    """In every process of a Python agent's venv (its .pth: pth_line): MESSAGE and exit status 2 (EXIT, else
    os._exit: a .pth cannot end the interpreter otherwise) for what would turn its telemetry on, else ENVIRON
    (os.environ) pinned in place, which every process it starts inherits."""
    environ = os.environ if environ is None else environ
    found = refused(agent, [], environ)
    if found is not None:
        stream = stderr or sys.stderr
        try:
            stream.write(f"{message(agent)} ({found[0]} is refused: {found[1]})\n")
            stream.flush()
        finally:
            (exit or os._exit)(2)
        return
    pinned_env = pinned_environment(agent, environ)
    for name in [name for name in environ if name not in pinned_env]:
        del environ[name]
    environ.update(pinned_env)


def pth_line(agent, guard_py=GUARD_PY):
    """The .pth (PTH) in a Python agent's site-packages: GUARD_PY loaded by its path (nothing added to sys.path), its
    python_guard run - at the start of every process of that Python."""
    return (f"import importlib.util as _sai; _sai_spec = _sai.spec_from_file_location('sandbox_ai_telemetry_guard', "
            f"{guard_py!r}); _sai_guard = _sai.module_from_spec(_sai_spec); _sai_spec.loader.exec_module(_sai_guard); "
            f"_sai_guard.python_guard({agent!r})\n")


def commonjs(entry):
    """Whether the Node ENTRY is CommonJS for sure (.cjs, or "type": "commonjs" in the nearest package.json), where
    the guard's import cannot go. Without a "type" Node takes a file with an import as an ES module; the image's
    check of the command after --install proves it."""
    entry = Path(entry)
    if entry.suffix in (".mjs", ".cjs"):
        return entry.suffix == ".cjs"
    for folder in entry.parents:
        package = folder / "package.json"
        if package.is_file():
            try:
                return json.loads(package.read_text()).get("type") == "commonjs"
            except (OSError, ValueError, AttributeError, RecursionError):
                return False
    return False


def import_line(agent, guard_js=GUARD_JS):
    """The line a Node agent's entry file starts with (after its #!): the guard, before anything of the agent's."""
    return (f'import "file://{guard_js}?agent={agent}"; '
            f"// sandbox-ai: {possessive(NAMES[agent])} telemetry is always off (sandbox/telemetry_guard.py)")


def shim(agent, guard_py=GUARD_PY):
    """The command of an exec agent (/usr/local/bin/AGENT): this file, which becomes the agent."""
    return (f"#!/bin/sh\n# sandbox-ai: {possessive(NAMES[agent])} telemetry is always off (sandbox/telemetry_guard.py)\n"
            f'exec {PYTHON} -IS {guard_py} {agent} "$@"\n')


def install(agent, command_dir=COMMAND_DIR, guard_dir=GUARD_DIR, entry=None, site_dir=None):
    """At the image's build (Containerfile.*): guard AGENT's command - a Node agent's entry file (ENTRY: ENTRIES', else
    what its command links to; not CommonJS) imports the guard, an exec agent's command and ALIASES become the shim and
    the program moves to REAL_DIR, a Python agent's site-packages (SITE_DIR: SITE_PACKAGES') gets the .pth. Writes the
    JavaScript beside this file. Done once: a second call changes nothing."""
    command, guard = Path(command_dir) / agent, Path(guard_dir)
    guard.mkdir(parents=True, exist_ok=True)
    (guard / Path(GUARD_JS).name).write_text(javascript())
    if KINDS[agent] == "python":
        site = Path(site_dir or SITE_PACKAGES[agent])
        site.mkdir(parents=True, exist_ok=True)
        pth = site / PTH
        pth.write_text(pth_line(agent, str(guard / Path(GUARD_PY).name)))
        return pth
    if KINDS[agent] == "node":
        entry = Path(entry or ENTRIES.get(agent) or command).resolve(strict=True)
        text = entry.read_text()
        if commonjs(entry):
            raise SystemExit(f"sandbox-ai: {entry} is not an ES module: {possessive(NAMES[agent])} telemetry guard "
                             "cannot be imported there")
        line = import_line(agent, str(guard / Path(GUARD_JS).name))
        if line not in text:
            first, newline, rest = text.partition("\n")
            entry.write_text(f"{first}\n{line}\n{rest}" if first.startswith("#!") and newline else f"{line}\n{text}")
        return entry
    real = guard / "real" / agent
    real.parent.mkdir(parents=True, exist_ok=True)
    names = [command, *(Path(command_dir) / alias for alias in ALIASES.get(agent, ()))]
    if not (real.exists() or real.is_symlink()):
        program = command.resolve(strict=True)
        if command.is_symlink() and program not in {name.resolve() for name in names[1:] if name.exists()}:
            real.symlink_to(program)
        else:  # the command is the program, or a link to one of its other names here: the program moves
            os.replace(program, real)
    for name in names:
        if name != command and others_shim(name, agent, str(guard / Path(GUARD_PY).name)):
            continue  # another agent's command (the team image: `opencode` is OpenCode 1's), left to it
        if name.exists() or name.is_symlink():
            name.unlink()
        name.write_text(shim(agent, str(guard / Path(GUARD_PY).name)))
        name.chmod(0o755)
    return real


def others_shim(path, agent, guard_py=GUARD_PY):
    """PATH is the guarded command of an agent other than AGENT (its shim): an alias of AGENT never takes it over."""
    try:
        text = Path(path).read_text() if Path(path).is_file() and not Path(path).is_symlink() else ""
    except (OSError, UnicodeDecodeError):
        return False
    return any(text == shim(other, guard_py) for other in NAMES if other != agent)


def spec(agent):
    """What the JavaScript guard needs of AGENT."""
    return {"name": NAMES[agent], "message": message(agent), "pinned": pinned(agent),
            "refused": {name: [{"only": list(values["only"])} if isinstance(values, dict) else
                               list(values) if values else None, why]
                        for name, (values, why) in REFUSED_ENV.get(agent, {}).items()},
            "exempt": list(EXEMPT_ENV.get(agent, ())), "names": AGENT_NAMES.get(agent)}


JAVASCRIPT = r"""// sandbox-ai's telemetry guard for a Node agent (generated by sandbox/telemetry_guard.py: do not edit).
// The agent's entry file imports it first; read-only in the sandbox. Telemetry is always off in sandbox-ai.
import { existsSync } from "node:fs";

const SPECS = __SPECS__;
const ACCEPTED = __ACCEPTED__;
const TELEMETRY_NAME = /TELEMETRY|(?:^|_)OTEL(?:_|$)|OTLP|TRACING|DATADOG/;
const CODEX_TABLES = __CODEX_TABLES__;
const CODEX_PINNED = __CODEX_PINNED__;
const DSH_PATCH = __DSH_PATCH__;
const CODEX_LOGIN_KEPT = __CODEX_LOGIN_KEPT__;
const CODEX_VALUE_OPTIONS = __CODEX_VALUE_OPTIONS__;
const LOGIN_CALLBACK_ENV = __LOGIN_CALLBACK_ENV__;
const CODEX_LOGIN_NOTE = __CODEX_LOGIN_NOTE__;

const agent = new URL(import.meta.url).searchParams.get("agent");
const spec = Object.hasOwn(SPECS, agent) ? SPECS[agent] : null;

const blank = (value) => String(value ?? "").trim() === "";
const telemetryName = (name) =>
  TELEMETRY_NAME.test(name.toUpperCase()) || (spec?.names ? new RegExp(spec.names).test(name.toUpperCase()) : false);
const options = (args) => (args.includes("--") ? args.slice(0, args.indexOf("--")) : args);

function refusedEnvironment(env) {
  for (const [name, [values, why]] of Object.entries(spec.refused)) {
    const value = env[name] ?? "";
    const hit = values === null || (Array.isArray(values) ? values.includes(value.trim())
      : !values.only.includes(value.toLowerCase()));
    if (!blank(value) && hit) return [`${name}=${value}`, why];
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

function codexLoginArgs(args, env) {
  if (String(env[LOGIN_CALLBACK_ENV] ?? "").trim() === "1") return null;
  const words = options(args);
  let index = 0;
  while (index < words.length) {
    const word = words[index];
    if (CODEX_VALUE_OPTIONS.includes(word)) { index += 2; continue; }
    if (word.startsWith("-")) { index += 1; continue; }
    break;
  }
  if (index >= words.length || words[index] !== "login") return null;
  const rest = options(args.slice(index + 1));
  if (rest.some((word) => CODEX_LOGIN_KEPT.includes(word) || CODEX_LOGIN_KEPT.includes(word.split("=", 1)[0]))) return null;
  const positional = rest.filter((word) => !word.startsWith("-"));
  if (positional.length && ["status", "help"].includes(positional[0])) return null;
  return [...args.slice(0, index + 1), "--device-auth", ...args.slice(index + 1)];
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
if (agent === "codex") {
  const login = codexLoginArgs(args, process.env);
  if (login !== null) {
    process.stderr.write(CODEX_LOGIN_NOTE);
    process.argv.splice(2, args.length, ...login);
  }
}
"""


def javascript():
    """The JavaScript guard (GUARD_JS) with every agent's spec: the same decisions as refused, pinned_environment and
    dsh_args (tests/test_telemetry_guard.py runs both on the same cases)."""
    values = {"__SPECS__": {agent: spec(agent) for agent in AGENTS if KINDS[agent] == "node"},
              "__ACCEPTED__": accepted(), "__CODEX_TABLES__": list(CODEX_TABLES), "__CODEX_PINNED__": CODEX_PINNED,
              "__DSH_PATCH__": DSH_PATCH, "__CODEX_LOGIN_KEPT__": list(CODEX_LOGIN_KEPT),
              "__CODEX_VALUE_OPTIONS__": list(CODEX_VALUE_OPTIONS), "__LOGIN_CALLBACK_ENV__": LOGIN_CALLBACK_ENV,
              "__CODEX_LOGIN_NOTE__": CODEX_LOGIN_NOTE}
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
