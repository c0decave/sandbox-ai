#!/usr/bin/env python3
"""Rootless agent launchers; standard library only. See SANDBOX.md."""
import argparse
import base64
import contextlib
import copy
import errno
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import grp
import pwd
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent


def load_runtime():
    """The in-sandbox runtime (runtime.py): loaded by path, because launchers run with python -I."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_runtime", HERE / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = load_runtime()


def load_launch_log():
    """launchlog.py (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1), once per process, with the proxy's redaction."""
    shared = sys.modules.get("sandbox_ai_launchlog")
    if shared is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with LOADING:
            shared = sys.modules.setdefault("sandbox_ai_launchlog", module)
    shared.set_redactor(RUNTIME.redact_text)
    return shared


LOG = load_launch_log()


def logged_run(area, what, argv, **kwargs):
    """subprocess.run(ARGV, **KWARGS) with its line in launcher.log (LOG.run: WHAT, the command, its time and end)."""
    return LOG.run(area, what, argv, runner=subprocess.run, **kwargs)


def load_modes():
    """The security modes (modes.py): a table and small checks, loaded by path like the runtime."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_modes", HERE / "modes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODES = load_modes()


def load_fixes():
    """fixes.py: the fix plan - what a start lacks and what makes each, offered instead of only refused."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_fixes", HERE / "fixes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXES = load_fixes()


def load_telemetry_guard():
    """telemetry_guard.py: the guard of the agents' own commands in their images (and the opt-out variables it pins),
    loaded by path like the runtime."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_telemetry_guard", HERE / "telemetry_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TELEMETRY_GUARD = load_telemetry_guard()


def load_cloud_models():
    """cloud_models.py: the table of cloud models of each agent (cloud_models.json) and its checks, loaded by path
    like the runtime."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_cloud_models", HERE / "cloud_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLOUD_MODELS = load_cloud_models()


def cloud_model_ids():
    """{agent: (model id, ...)} of the table of cloud models (AGENT_MODELS); {} when the table cannot be read (an
    installation's damaged file: a start with --model and --effort says so, nothing else depends on it)."""
    try:
        return CLOUD_MODELS.known_ids()
    except ValueError:
        return {}


# Shared constants and helpers, and the runtime functions under their former names.
LOG_DIR = RUNTIME.LOG_DIR
VNC_DIR = RUNTIME.VNC_DIR
TMUX_DIR = RUNTIME.TMUX_DIR
ATTACH_SOCKET = RUNTIME.ATTACH_SOCKET
SHELL_SOCKET = RUNTIME.SHELL_SOCKET
GUI_CONFIG = RUNTIME.GUI_CONFIG
DROP_ALL = RUNTIME.DROP_ALL
INOTIFY_EVENTS = RUNTIME.INOTIFY_EVENTS
x_socket = RUNTIME.x_socket
listen_unix = RUNTIME.listen_unix
read_frames = RUNTIME.read_frames
attach_relay = RUNTIME.attach_relay
relay_terminal = RUNTIME.relay_terminal
attach_frame = RUNTIME.attach_frame
attach_client = RUNTIME.attach_client
shell_client = RUNTIME.shell_client
mcp_serve = RUNTIME.mcp_serve
mcp_bridge = RUNTIME.mcp_bridge
namespace_state = RUNTIME.namespace_state
verify_inner = RUNTIME.verify_inner
wait_child = RUNTIME.wait_child
inner_bootstrap = RUNTIME.inner_bootstrap
start_gui = RUNTIME.start_gui
log_event = RUNTIME.log_event
start_loggers = RUNTIME.start_loggers
poll_denied = RUNTIME.poll_denied
dns_reply = RUNTIME.dns_reply
dns_logger = RUNTIME.dns_logger
file_logger = RUNTIME.file_logger
firejail_command = RUNTIME.firejail_command
base_env = RUNTIME.base_env
tmux_command = RUNTIME.tmux_command
inner_guard = RUNTIME.inner_guard
stop_process = RUNTIME.stop_process
PROXY_DIR = RUNTIME.PROXY_DIR
PACKAGES_DIR = RUNTIME.PACKAGES_DIR  # the proxy's mount of the host's copy of the known malicious packages
PROXY_PORT = RUNTIME.PROXY_PORT
PROXY_UID = RUNTIME.PROXY_UID
CA_DIR = RUNTIME.CA_DIR
CONTROL_DIR = RUNTIME.CONTROL_DIR
BROWSER_DIR = PurePosixPath(RUNTIME.BROWSER_SOCKET).parent
BROWSER_SHIMS = ("xdg-open", "sensible-browser", "x-www-browser", "www-browser", "open")
host_matches = RUNTIME.host_matches
# Environment variables that point TLS libraries and tools at the bundle with the session CA.
CA_BUNDLE_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "PIP_CERT",
                       "CARGO_HTTP_CAINFO", "AWS_CA_BUNDLE", "DENO_CERT", "NIX_SSL_CERT_FILE", "CODEX_CA_CERTIFICATE")
PROXY_BODY_LIMIT = 64 << 10
CONNECTION_LOGS_LIMIT = 1 << 30   # bytes of connections-*.jsonl kept per log directory
LLM_TRAFFIC_LOGS_LIMIT = 2 << 30  # bytes of llm-traffic-*.jsonl (and their rotated parts) kept per log directory
LLM_TRAFFIC_LIMIT_MIB = (1, 64)   # --llm-traffic-limit: MiB of one record of the LLM traffic log
PROVIDERS = {
    "codex": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    # console.anthropic.com serves OAuth token refresh for many Claude Code versions.
    "claude": ["api.anthropic.com", "console.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"],
    "opencode": ["opencode.ai"],
    # OpenCode 2's: the same (OpenCode Zen, Console and Go; its update check's path there is refused unless
    # --opencode2-enable updates).
    "opencode2": ["opencode.ai"],
    # Hermes' maker's provider: the Nous Portal (its login, sandbox-ai login hermes, and its inference API).
    "hermes": ["portal.nousresearch.com", "inference-api.nousresearch.com"],
    # DeepSeek Harness' maker's provider: its API (chat, its web search tool) and the platform account (sign-in in
    # its web UI, balance).
    "dsh": ["api.deepseek.com", "platform.deepseek.com"],
    # Qwen Code's: Qwen OAuth (chat.qwen.ai's device login, inference at the token's resource URL) and DashScope.
    "qwen": ["chat.qwen.ai", "portal.qwen.ai", "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"],
    # Goose has no maker's model service: its provider comes with --llm (a server of yours, OpenRouter) or
    # --api-key (Anthropic's, OpenAI's ... API), which open their own hosts.
    "goose": [],
    # Nor has OpenHands: --llm, --api-key (LiteLLM's providers) or its settings page.
    "openhands": [],
    # nanobot neither (its provider: --llm or --api-key, as Goose's).
    "nanobot": [],
    # Letta Code's maker's service is Letta Cloud (--letta-enable cloud opens it); its models come with --llm or
    # --api-key like Goose's.
    "letta": [],
    # Pi has no maker's model service: --llm, --api-key, or its /login's providers (opened like OpenCode's: --frontier,
    # --allow-host).
    "pi": [],
}
# --frontier: OpenAI and Anthropic endpoints for any agent (e.g. OpenCode).
FRONTIER = PROVIDERS["codex"] + PROVIDERS["claude"]
# --allow-SET: the host sets ("host sets" in every text since 2026-11-24; "preset" is a start template, --preset),
# common destinations, TCP/443 via pinned DNS like --allow-host. (PRESETS and a.presets: their names in the code.)
PRESETS = {
    "pypi": ["pypi.org", "files.pythonhosted.org"],
    "github": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
               "objects.githubusercontent.com", "release-assets.githubusercontent.com"],
    "gitlab": ["gitlab.com", "registry.gitlab.com"],
    "kernel-org": ["kernel.org", "www.kernel.org", "git.kernel.org", "cdn.kernel.org", "mirrors.kernel.org",
                   "lore.kernel.org"],
    "duckduckgo": ["duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com", "links.duckduckgo.com",
                   "api.duckduckgo.com"],
    "npm": ["registry.npmjs.org"],
}


# Published, stable ingress ranges (TCP/443 only). They keep the API reachable
# when DNS rotates after launch. OpenAI publishes none; it stays DNS-pinned.
HOST_NETWORKS = {"api.anthropic.com": ["160.79.104.0/23", "2607:6bc0::/48"]}
# Per-agent switches for --dangerously-skip-permissions.
DANGER_ARGS = {"codex": ["--dangerously-bypass-approvals-and-sandbox"],
               "claude": ["--dangerously-skip-permissions"], "opencode": [], "opencode2": [], "hermes": [],
               "dsh": [], "qwen": ["--yolo"], "goose": [], "openhands": [], "nanobot": [], "letta": ["--yolo"],
               "pi": []}  # Pi asks before nothing (no approvals): -D changes nothing
# --model and --effort: the effort levels each agent takes, its own flag's (the table of cloud models lists the same
# per agent and, per model, those the model takes: a test keeps both the same); OpenCode sets the effort per model in
# its own configuration. AGENT_MODELS (below): the names each agent knows for sure, the table's; any other model name
# is passed on as it is.
AGENT_EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
                 # Codex 0.169.0 (codex-rs/protocol openai_models.rs ReasoningEffort; models-manager/models.json: the
                 # GPT-6 models take up to max and ultra, none of its models takes minimal any more)
                 "codex": ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                 "opencode": (), "opencode2": (),
                 "hermes": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
                 # DSH: reasoning per model route; Qwen Code: model.reasoningEffort in its system settings.
                 "dsh": (), "qwen": ("none", "low", "medium", "high", "xhigh", "max"),
                 # Goose: GOOSE_THINKING_EFFORT (goose.EFFORTS).
                 "goose": ("off", "low", "medium", "high", "max"),
                 # OpenHands: the LLM's reasoning_effort in its settings (openhands.EFFORTS).
                 "openhands": ("none", "low", "medium", "high", "xhigh"),
                 # nanobot: agents.defaults.reasoningEffort in sandbox-ai's settings (nanobot.EFFORTS).
                 "nanobot": ("none", "low", "medium", "high", "xhigh", "max"),
                 # Letta Code: reasoning per model, set with its /model.
                 "letta": (),
                 # Pi: its --thinking (pi.EFFORTS).
                 "pi": ("off", "minimal", "low", "medium", "high", "xhigh", "max")}
AGENT_MODELS = {agent: ids.get(agent, ()) for ids in (cloud_model_ids(),) for agent in AGENT_EFFORTS}
# --show-thinking on|off: whether the agent shows its model's thinking (reasoning) in the terminal, through the
# agent's own switch as its pinned release has it (checked in each release; without the option the agent's own
# default holds): {agent: (its default, the switch)}. An agent that has no switch is in THINKING_NONE with what it
# offers instead; --show-thinking is refused for it.
#   claude    Claude Code 2.1.322 (the binary): the setting showThinkingSummaries ("Request API-side thinking
#             summaries and show them in the conversation and in the transcript view (ctrl+o)"; default false),
#             given for this run with --settings JSON (over your settings files; your own --settings after it wins)
#   codex     Codex 0.169.0 (the binary's config keys): hide_agent_reasoning (default false: the reasoning summaries
#             are shown) and show_raw_agent_reasoning (the raw reasoning a local model streams), with -c
#   opencode  OpenCode 1.19.5 (the binary): the TUI's thinking_mode show|hide in ~/.local/state/opencode/kv.json
#             (default hide; /thinking in the TUI toggles it and keeps it there)
#   hermes    Hermes v0.22.5 (hermes_cli/cli_init_mixin.py): display.show_reasoning (default true) and the CLI's own
#             display.platforms.cli.show_reasoning (what /reasoning show|hide saves), in its managed configuration
#   dsh       sandbox-ai's chat (dsh_chat.py --thinking show|hide; /thinking in the chat); DSH's web UI has its own
#   goose     Goose 1.57.0 (crates/goose-cli/src/session/output.rs thinking_enabled): GOOSE_CLI_SHOW_THINKING - any
#             value of the variable turns it on, else the configuration's boolean (default false)
#   opencode2 OpenCode 2.2.5 (the binary's CLI config schema): session.thinking show|hide of the TUI (default hide;
#             "Expand/Collapse thinking" in its palette saves it there) and mini.thinking (its mini mode, default
#             show) in ~/.config/opencode/cli.json - the file, not OPENCODE_CLI_CONFIG_CONTENT, which would win over
#             the palette's toggle for the whole session
#   pi        Pi 1.4.0 (core/settings-manager.js getHideThinkingBlock): hideThinkingBlock in ~/.pi/agent/settings.json
#             (default false: shown; its /settings saves it there)
#   nanobot   nanobot 0.4.0 (config/schema.py ChannelsConfig.show_reasoning, channels/manager.py): the channels'
#             showReasoning (default true) - the CLI's and every channel's, so the terminal client's and the WebUI's
#             (the websocket channel's own key too) - in sandbox-ai's settings
AGENT_THINKING = {"claude": ("off", "showThinkingSummaries (--settings)"),
                  "codex": ("on", "hide_agent_reasoning, show_raw_agent_reasoning (-c)"),
                  "opencode": ("off", "thinking_mode in ~/.local/state/opencode/kv.json"),
                  "hermes": ("on", "display.show_reasoning (managed configuration)"),
                  "dsh": ("on", "sandbox-ai's chat (--thinking)"),
                  "goose": ("off", "GOOSE_CLI_SHOW_THINKING"),
                  "opencode2": ("off", "session.thinking and mini.thinking in ~/.config/opencode/cli.json"),
                  "pi": ("on", "hideThinkingBlock in ~/.pi/agent/settings.json"),
                  "nanobot": ("on", "channels.showReasoning (sandbox-ai's settings)")}
# Checked in each one's pinned release: the thinking is there, folded, and only a key or a click in the session
# unfolds it - no setting, no variable, no option starts it unfolded or hides it.
#   qwen      Qwen Code 0.26.4 (startInteractiveUI: useState(false) of thoughtExpanded, toggleThinkingExpanded)
#   openhands Agent Canvas 1.34.0 (collapsible-thinking.js: useState(false) per message); the SDK's reasoning_summary
#             only asks OpenAI's Responses API for summaries, it neither shows nor hides anything
#   letta     Letta Code 0.34.36 (transcript-display-state.ts: expanded.thinking = false, Ctrl+T toggles it)
THINKING_NONE = {"qwen": "Qwen Code 0.26.4 has no setting for it: its thinking is folded in the terminal, and "
                         "Ctrl+O (or Alt+T) unfolds it in the session",
                 "openhands": "OpenHands' Agent Canvas 1.34.0 has no setting for it: each message's thinking is "
                              "folded (Thinking), and a click unfolds it",
                 "letta": "Letta Code 0.34.36 has no setting for it: its thinking is folded in the terminal "
                          "(Thought for ...), and Ctrl+T unfolds it in the session"}
MODEL_NAME = r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}"
DANGER_ENV = {"codex": {}, "claude": {"IS_SANDBOX": "1"},
              "opencode": {"OPENCODE_PERMISSION": '{"*":"allow"}'},
              # OpenCode 2 has no OPENCODE_PERMISSION: its allow-all rule is in its configuration layer
              # (opencode2.config_content).
              "opencode2": {},
              # Hermes' --yolo, as a variable: it holds for its subcommands too (hermes kanban, hermes -z).
              "hermes": {"HERMES_YOLO_MODE": "1"},
              # DSH: no approvals and no inner confinement of its commands (this sandbox is the boundary).
              "dsh": {"DSH_PERMISSION_MODE": "danger-full-access"}, "qwen": {},
              # Goose: its mode auto (upstream's default) instead of the sandbox's smart_approve (goose.MODE).
              "goose": {"GOOSE_MODE": "auto"},
              # OpenHands: its confirmation mode off, in the start's settings (openhands.managed_settings).
              "openhands": {},
              # nanobot asks nothing before a tool runs (its exec tool has no approvals): nothing to switch.
              "nanobot": {}, "letta": {}, "pi": {}}
# --telemetry: what the agents report to their makers - always off, for every agent (the user's rule since 0.1.95).
# off (the default): no usage statistics, error reports, feedback uploads, session sharing, OpenTelemetry exports or
# update checks; strict: also every other call that is not the model's. Nothing turns telemetry on: the opt-out
# variables are set after --env, --secret-env and --llm (agent_environment: they cannot undo them), Codex's keys are
# forced in its ~/.codex/config.toml and on its command line, OpenCode's in OPENCODE_CONFIG_CONTENT. Names checked in
# the sources: Claude Code 2.1.317 (the binary), Codex rust-v0.166.0 (codex-rs/config: analytics, feedback, otel,
# check_for_update_on_startup, features), OpenCode v1.19.5 (packages/core/src/flag/flag.ts, effect/runtime-flags.ts;
# "share" and "autoupdate" in its config).
TELEMETRY_LEVELS = ("off", "strict")
# The level on (the agents' own settings) is gone since 0.1.95. An old one - an option, defaults.json, a project
# file, a preset, a kept start, a team file, a resumed session's options - never fails a start: it is ignored (as if
# not given: off, or strict where your defaults or the security mode say so), with one note.
TELEMETRY_RETIRED = "on"
TELEMETRY_RETIRED_NOTE = "agent-sandbox: telemetry is always off since 0.1.95; 'on' is ignored"
_telemetry_retired_noted = False


def note_telemetry_retired():
    """The note that an old --telemetry on (or "telemetry": "on") is ignored: once per process."""
    global _telemetry_retired_noted
    if not _telemetry_retired_noted:
        _telemetry_retired_noted = True
        print(TELEMETRY_RETIRED_NOTE, file=sys.stderr)


class TelemetryChoices(tuple):
    """--telemetry's choices: off and strict (what --help, completions and errors name); the retired on is taken as
    well, so an old one never fails a start (TelemetryOption ignores it)."""
    def __contains__(self, value):
        return value == TELEMETRY_RETIRED or tuple.__contains__(self, value)


class TelemetryOption(argparse.Action):
    """--telemetry LEVEL: off or strict; the retired on counts as not given, with the note."""
    def __call__(self, parser, namespace, values, option_string=None):
        if values == TELEMETRY_RETIRED:
            note_telemetry_retired()
            values = None
        setattr(namespace, self.dest, values)


# Tools an agent runs that report usage unless told not to (the common DO_NOT_TRACK, the kits' tools' own switches,
# OpenTelemetry's): telemetry_guard.TOOLS, which the guard of each agent's command pins as well.
TELEMETRY_TOOLS = dict(TELEMETRY_GUARD.TOOLS)
TELEMETRY_ENV = {
    # CLAUDE_CODE_ENABLE_TELEMETRY: Claude Code's OpenTelemetry export (metrics and events to an OTLP endpoint).
    # ENABLE_BETA_TRACING_DETAILED=0: its beta tracing exports traces and logs to a BETA_TRACING_ENDPOINT whatever
    # CLAUDE_CODE_ENABLE_TELEMETRY says (telemetry_guard.AGENT_PINS, which these are).
    "claude": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["claude"]},
               # Also plugin and marketplace downloads, release notes and the like.
               "strict": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}},
    # OpenCode sends no usage statistics (OTLP only with an endpoint of yours); it checks for updates.
    "opencode": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["opencode"]},
                 "strict": {"OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}},
    # OpenCode 2: its telemetry (Bun's crash reports, OpenTelemetry) is pinned off at every level by variables, its
    # update check and catalog download unless --opencode2-enable turns one on (opencode2.py).
    "opencode2": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["opencode2"]}, "strict": {}},
    "codex": {"off": dict(TELEMETRY_TOOLS), "strict": {}},  # its switches: codex_telemetry_defaults(), _args()
    # Hermes v0.22.5 sends nothing unless its shared metrics are opted in; its switches (and those of its update and
    # catalog checks) are in its managed configuration (hermes.py, TELEMETRY_CONFIG).
    "hermes": {"off": dict(TELEMETRY_TOOLS), "strict": {}},
    # DSH and Qwen Code: their telemetry is pinned off at every level (dsh.py, qwen.py: TELEMETRY_ENV), their update
    # and catalog checks too unless --qwen-enable turns one on.
    "dsh": {"off": dict(TELEMETRY_TOOLS), "strict": {}},
    "qwen": {"off": dict(TELEMETRY_TOOLS), "strict": {}},
    # Goose: its telemetry is pinned off at every level by variables (goose.py: TELEMETRY_ENV).
    "goose": {"off": dict(TELEMETRY_TOOLS), "strict": {}},
    # OpenHands: its telemetry is pinned off at every level by variables and its settings (openhands.py).
    "openhands": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["openhands"]}, "strict": {}},
    # nanobot sends none; its tracing's variables are pinned off (nanobot.py: TELEMETRY_ENV), its update check refused
    # unless --nanobot-enable updates.
    "nanobot": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["nanobot"]}, "strict": {}},
    # Letta Code: its telemetry and update check are pinned off by variables (letta.py: TELEMETRY_ENV, BASE_ENV).
    "letta": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["letta"]}, "strict": {}},
    # Pi: its install report pinned off by a variable (pi.py: TELEMETRY_ENV), its version check and catalogs off
    # (PI_OFFLINE) unless --pi-enable turns one on.
    "pi": {"off": {**TELEMETRY_TOOLS, **TELEMETRY_GUARD.AGENT_PINS["pi"]}, "strict": {}},
}
OPENCODE_TELEMETRY_CONFIG = {"share": "disabled", "autoupdate": False}  # in OPENCODE_CONFIG_CONTENT
# Telemetry is always off also for an agent started from the sandbox's shell, without the launcher's variables and
# options: its command is guarded in its image (telemetry_guard.py: it refuses what would turn telemetry on, with one
# message, and pins the opt-out variables), and its own managed configuration - the layer that wins over every file,
# variable and option of its own in the pinned versions' sources - pins the switches wherever it reads them, mounted
# read-only (telemetry_guarded, telemetry_guard_files, telemetry_guard_mounts): Claude Code's managed settings ("env":
# a settings file's env wins over the environment Claude Code was started with, the managed one over every other
# file and --settings), Codex's managed_config.toml (the layer above -c and every config.toml: codex-rs/config's
# loader), OpenCode's /etc/opencode/opencode.json (merged after OPENCODE_CONFIG_CONTENT and every file of its own).
TELEMETRY_MANAGED = {"claude": ("/etc/claude-code", "managed-settings.json"),
                     "codex": ("/etc/codex", "managed_config.toml"),
                     "opencode": ("/etc/opencode", "opencode.json")}
OPENCODE_MANAGED_CONFIG = {**OPENCODE_TELEMETRY_CONFIG, "experimental": {"openTelemetry": False}}
TELEMETRY_GUARD_DIR = "telemetry-guard"  # in the session directory: the guard's files, mounted over the image's
# Telemetry sent to hosts the agent needs anyway, refused by the proxy as well (the ruleset "telemetry"): Claude
# Code's event log on its API host, Codex's analytics on ChatGPT's (codex-rs/analytics/src/client.rs).
TELEMETRY_PATHS = {"claude": [("api.anthropic.com", "/api/event_logging/**")],
                   "codex": [("chatgpt.com", "/backend-api/codex/analytics-events/**")]}
# (table, key, value, TOML) forced in the sandbox's ~/.codex/config.toml (a value of the file's own is replaced) and
# given to the lead as -c overrides, which win over every file (codex_telemetry_args).
CODEX_TELEMETRY = {
    "off": [(None, "check_for_update_on_startup", False, "false"), ("analytics", "enabled", False, "false"),
            ("feedback", "enabled", False, "false"), ("otel", "metrics_exporter", "none", '"none"'),
            ("otel", "exporter", "none", '"none"'), ("otel", "trace_exporter", "none", '"none"')],
    "strict": [("features", "plugins", False, "false")],  # the plugin sync with chatgpt.com
}
# Written into a sandbox HOME's ~/.claude/settings.json where not already set.
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_DEFAULT_EFFORT = "high"
AUTH_TARGETS = {"codex": ".codex/auth.json", "claude": ".claude/.credentials.json",
                "opencode": ".local/share/opencode/auth.json",
                # OpenCode 2: no login sandbox-ai takes in; its saved accounts (/connect) stay in its database in the
                # sandbox's HOME (opencode2.login_document_ok).
                "opencode2": ".local/share/opencode/auth.json", "hermes": ".hermes/auth.json",
                "dsh": ".dsh/.credentials.yaml", "qwen": ".qwen/oauth_creds.json",
                # Goose has no login: never taken from the host or kept (goose.login_document_ok).
                "goose": ".config/goose/secrets.yaml",
                # Nor has OpenHands: its settings (with the keys typed into its page) stay the host's.
                "openhands": ".openhands/settings.json",
                # nanobot has no login in sandbox-ai: never taken from the host or kept (nanobot.login_document_ok).
                "nanobot": ".nanobot/config.json",
                # Letta Code: no login sandbox-ai takes in; its provider keys (letta connect) stay in the sandbox.
                "letta": ".letta/lc-local-backend/providers/auth.json",
                # Pi: its /login's auth.json (a provider entry each, OpenCode's form: pi.login_document_ok).
                "pi": ".pi/agent/auth.json"}
# Agents with no login of their own (NO_LOGIN_AGENTS): no file of the host's is copied in automatically or with
# --copy-auth - Goose's secrets.yaml holds every provider key `goose configure` kept on the host. Agents without a
# `sandbox-ai login` command (LOGIN_COMMANDLESS): a missing login is not told to be fixed with one.
NO_LOGIN_AGENTS = ("opencode2", "goose", "openhands", "nanobot", "letta")
# --with AGENT: every agent (the team image: Claude Code, Codex and OpenCode, the others its layers: team_image.py).
TEAM_BASE_AGENTS = ("claude", "codex", "opencode")
COMPANION_AGENTS = tuple(sorted(AUTH_TARGETS))
# How the lead gives an agent --with a task from the shell and reads its answer: the agent's one-shot mode (what a
# helper runs for a task, agent_mcp.command, without the helper's own flags), the task after "--" or as the option's
# own value. The sandbox-ai skill names each (tests/test_with_all_agents.py).
COMPANION_COMMANDS = {
    "claude": 'claude -p --output-format json -- "TASK"',
    "codex": 'codex exec --skip-git-repo-check -- "TASK"',
    "opencode": 'opencode run -- "TASK"',
    "opencode2": 'opencode2 run -- "TASK"',
    "hermes": 'hermes --oneshot="TASK"',
    "qwen": 'qwen --prompt="TASK"',
    "goose": 'goose run --quiet --text="TASK"',
    "openhands": 'openhands task --text="TASK"',
    "nanobot": 'nanobot agent --no-markdown --message="TASK"',
    "letta": 'letta -p --new -- "TASK"',
    "pi": 'pi -p -- "TASK"',
    "dsh": 'echo "TASK" | dsh headless -',
}
LOGIN_COMMANDLESS = ("opencode2", "dsh", "goose", "openhands", "nanobot", "letta")
CAPS = ["SYS_ADMIN", "NET_ADMIN", "SETUID", "SETGID", "SYS_CHROOT",
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETPCAP"]
DEV = Path("/dev")
# Directories (below /dev) passed whole as bind mounts: devices plugged in later appear inside too.
DEVICE_DIRS = ("bus/usb", "dri", "snd")
# Never passed to an agent: (pattern for the path below /dev, what it would give the agent).
DEVICE_REFUSED = [
    (r"mem|kmem|port|kcore", "raw access to the host's memory and I/O ports"),
    (r"console|tty|tty[0-9]+|vcsa?u?[0-9]*|fb[0-9]+", "your consoles and screens (their contents, typing into them)"),
    (r"input(/.*)?|uinput|uhid|hidraw[0-9]+|usb/hiddev[0-9]+|usbmon[0-9]+",
     "keyboards, security keys and other input devices (reading or faking keystrokes, all USB traffic)"),
    (r"kmsg|ttyprintk|snapshot|watchdog[0-9]*|tpm(rm)?[0-9]*|mei[0-9]*|cpu(/.*)?|mtd.*|nvram|hpet|userfaultfd",
     "host internals (kernel log, watchdog, TPM, management engine, CPU registers, firmware)"),
    (r"loop-control|mapper(/.*)?|btrfs-control|sg[0-9]+|bsg(/.*)?|nvme[0-9]+|ng[0-9]+n[0-9]+|vfio(/.*)?|iommu",
     "your disks or direct hardware access"),
    (r"vsock|vhost-.*", "channels to the host or virtual machines that bypass the network firewall"),
    (r"null|zero|full|random|urandom|ptmx|pts(/.*)?|shm(/.*)?|fd(/.*)?|mqueue(/.*)?|std(in|out|err)",
     "nothing: every sandbox has it already"),
]
# Passed, with a warning at the start.
DEVICE_WARN = [
    (r"video[0-9]+|media[0-9]+|v4l(/.*)?", "a camera: the agent can take pictures and videos"),
    (r"snd(/.*)?", "audio: the agent can record through your microphones"),
    (r"bus/usb(/.*)?", "raw USB: the agent can use every USB device your user may open, security keys and "
                       "smart-card readers included"),
]
GPU_KINDS = ("nvidia", "dri", "rocm")
NVIDIA_NODES = r"nvidia[0-9]+|nvidiactl|nvidia-uvm|nvidia-uvm-tools|nvidia-modeset"
# Where nvidia-ctk writes Container Device Interface specifications.
CDI_DIRS = (Path("/etc/cdi"), Path("/var/run/cdi"), Path("/run/cdi"))
# pasta without gateway mapping or any port forwarding (-t/-u inbound, -T/-U to the host).
PASTA_OPTIONS = ("--no-map-gw", "-t", "none", "-u", "none", "-T", "none", "-U", "none")
POD_NETWORK = "pasta:" + ",".join(PASTA_OPTIONS)
# A chosen --subnet/--subnet6 is laid out as slirp4netns lays out its own: the sandbox at the 100th address,
# the gateway at the 2nd, slirp4netns's DNS forwarder at the 3rd. pasta gets the same addresses.
# (For IPv6 the sandbox gets ::100, written like the others: the 0x100th address.)
SUBNET_HOSTS = {"address": 100, "gateway": 2, "dns": 3}
# What each backend uses without a --subnet: slirp4netns its built-in networks (IPv6 cannot be changed),
# pasta the host's own addresses.
SLIRP_SUBNETS = ("10.0.2.0/24", "fd00::/64")
# slirp4netns accepts /1 to /25 (the 100th address must fit); below /8 a sandbox would hide large parts of
# the private ranges. IPv6: pasta and slirp4netns configure the interface as a /64.
SUBNET_PREFIXES = {4: (8, 25), 6: (64, 64)}
SUBNET_RANGES = {4: [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                       "100.64.0.0/10")],
                 6: [ipaddress.ip_network("fc00::/7")]}
ROUTE_FILES = (Path("/proc/net/route"), Path("/proc/net/ipv6_route"))
DEFAULTS_KEYS = {"version", "subnet", "subnet6", "mode", "canaries", "keep_days", "telemetry", "preset_dirs",
                 "safeguard_mute", "name_style", "desktop_notifications", "loop_guard", "llm_traffic",
                 "llm_traffic_limit", "file_reads", "log_days", "connection_bodies", "ask_timeout",
                 "monitor_interval", "budget", "checkpoint_start", "checkpoint_every", "checkpoint_mounts",
                 "checkpoint_days", "checkpoint_count", "checkpoint_gb", "checkpoint_exclude", "update_mode",
                 "update_window", "update_source", "hide_secrets",
                 # the storage check and its automatic rules (storage.py: Settings > Storage in the app)
                 "storage_check", "storage_threshold_gb", "storage_growth_gb", "storage_caches_days",
                 "storage_compact_days", "storage_pack_days", "storage_logs_days", "package_min_age_days",
                 # what a start lacks (fixes.py): one question for everything (all, the default) or one per fix (each)
                 "fix_questions",
                 # the agent's intro (intro.py): false starts without it (--intro turns it on for a start)
                 "agent_intro"}
# The update mode's keys of your defaults (updates.py: `sandbox-ai update mode` sets them and its timer).
UPDATE_KEYS = ("update_mode", "update_window", "update_source")
# The workspace checkpoints' keys of your defaults (checkpoints.py SETTINGS: what each means, its default).
CHECKPOINT_KEYS = ("checkpoint_start", "checkpoint_every", "checkpoint_mounts", "checkpoint_days", "checkpoint_count",
                   "checkpoint_gb", "checkpoint_exclude")
# How long a request waits for the answer to its question (--ask-timeout, defaults ask_timeout; runtime.APPROVAL_SECONDS
# and runtime.ASK_TIMEOUT_RANGE - a test keeps them the same). Unanswered by then, the try fails and the question
# stays open (runtime.NOT_ANSWERED).
ASK_TIMEOUT_DEFAULT = 60
ASK_TIMEOUT_RANGE = (5, 3600)


def checkpoint_minutes(value):
    """--checkpoint-every MINUTES: whole minutes, 0 (none) to checkpoints.EVERY_LIMIT."""
    try:
        minutes = int(str(value).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is no number of minutes") from None
    if not 0 <= minutes <= CHECKPOINT_EVERY_LIMIT:
        raise argparse.ArgumentTypeError(f"{minutes}: between 0 (none) and {CHECKPOINT_EVERY_LIMIT} minutes")
    return minutes


CHECKPOINT_EVERY_LIMIT = 1440  # checkpoints.EVERY_LIMIT (a test keeps them the same)


def ask_seconds(value):
    """--ask-timeout SECONDS (and defaults ask_timeout): whole seconds within ASK_TIMEOUT_RANGE."""
    try:
        seconds = int(str(value).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is no number of seconds") from None
    if not ASK_TIMEOUT_RANGE[0] <= seconds <= ASK_TIMEOUT_RANGE[1]:
        raise argparse.ArgumentTypeError(f"{seconds}: between {ASK_TIMEOUT_RANGE[0]} and {ASK_TIMEOUT_RANGE[1]} "
                                         "seconds")
    return seconds
# What the launchers show on the desktop (approvals.desktop_notification): everything, only a canary's alarm, nothing.
DESKTOP_NOTIFICATIONS = ("all", "alarms", "none")
NAME_STYLES = ("codename", "project")  # starts.NAME_STYLES: how a start without --name is named
SESSION_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"
# The directory of a start: its first name, with a suffix when that directory is taken (sessions.new_key).
START_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}"
__version__ = "0.2.2"
CONFIG_NAME = ".sandbox-ai.json"
RULES_NAME = "rules.json"  # ~/.config/sandbox-ai/rules.json: your own rules, for every start
# Label on session pods: the session's tmux directory, so a stale pod can be told apart from someone else's.
SESSION_LABEL = "sandbox-ai.session"
RUN_LABEL = "sandbox-ai.run"  # every pod a launcher creates: its run (liveness.RUN_LABEL)
CONFIG_KEYS = {"version", "agent", "options", "agent_args", "workspaces", "description", "mcp_servers", "rules",
               "log_debug", "mode", "canaries", "name", "keep", "budget"}
MCP_NAME = r"[A-Za-z0-9_-]{1,32}"
VPN_PROFILE_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"  # sandbox-ai vpn (vpn.py)
MCP_AGENT_DIR = "/run/sandbox-mcp"    # agent side: the supervisor's relay of each server, for this run
MCP_UPSTREAM_DIR = "/sandbox/mcp-upstream"  # supervisor side: the servers' socket directories, read-only
MCP_SERVE_DIR = "/sandbox/mcp-sock"   # server side: where the server sandbox listens
MCP_APPROVALS_DIR = "/sandbox/mcp-approvals"  # server side, read-only: the answers to its questions
# The agent's hooks (rules for its own tools): the command the agents run before and after each tool, how long
# they wait for it (the hook client gives up earlier: runtime.HOOK_TIMEOUT), and where Claude Code and Codex read
# their managed configuration (mounted read-only from the session directory) and OpenCode its plugin.
HOOK_COMMAND = "/usr/bin/python3 -I /sandbox/runtime.py --internal-hook {agent} {event}"
HOOK_AGENT_TIMEOUT = 120
# A kit's PreToolUse hook while rules hold: the wrapper runs it (within its own timeout, in seconds) and has a
# rewritten tool input (updatedInput) checked by the rules before it reaches the agent (runtime.kit_hook).
KIT_HOOK_COMMAND = "/usr/bin/python3 -I /sandbox/runtime.py --internal-kit-hook {agent} {timeout} {command}"
KIT_HOOK_TIMEOUT = 60  # what Claude Code and Codex give a hook that names no timeout
MANAGED_DIRS = {"claude": "/etc/claude-code", "codex": "/etc/codex"}
OPENCODE_HOOK_PLUGIN = "/sandbox/session/opencode-hooks.js"
OPENCODE_HOOK_RECHECK = "/sandbox/session/opencode-hooks-recheck.js"  # after the kits' plugins: changed args
KIT_HOOKS_TARGET = "/sandbox/session/kits/hooks"  # the approved kits' hook folders, copied for the run
# The approved Claude plugins' folders, copied for the run: their hooks run from there (${CLAUDE_PLUGIN_ROOT}), never
# from the plugin store, which can change after the start checked it (kits.checked_copy).
PLUGIN_HOOKS_TARGET = "/sandbox/session/kits/plugin-hooks"
OPENCODE_HOOKS_JS = r'''// sandbox-ai: the rules for the agent's own tools (README, Rulesets: "The agent's own tools").
// Installed by the launcher. Each tool call asks the sandbox's supervisor first; a refusal, or anything that
// goes wrong on the way, stops the call (the hook client exits with status 2 for both).
import { spawn } from "node:child_process";

const HOOK = ["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "opencode"];

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

// RECHECK: this copy comes after the approved kits' plugins (OpenCode runs plugins one after the other, in order)
// and asks again only for a call whose arguments a plugin changed after the first copy's check.
const RECHECK = false;
const CHECKED = (globalThis[Symbol.for("sandbox-ai.checked")] ??= new Map());

export const SandboxAiHooks = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    const args = JSON.stringify(output.args) ?? "null";
    if (RECHECK) {
      const checked = CHECKED.get(input.callID);
      CHECKED.delete(input.callID);
      if (checked === args) {
        return;
      }
    }
    const answer = await hook("PreToolUse", { tool: input.tool, args: output.args, sessionID: input.sessionID,
                                              callID: input.callID, cwd: directory });
    if (answer.code !== 0) {
      throw new Error(answer.reason.trim() || "sandbox-ai: the tool call is refused");
    }
    if (!RECHECK && CHECKED.size < 10000) {
      CHECKED.set(input.callID, args);
    }
  },
  "tool.execute.after": async (input, output) => {
    if (RECHECK) {
      return;
    }
    const text = output && typeof output.output === "string" ? output.output : null;
    await hook("PostToolUse", { tool: input.tool, sessionID: input.sessionID, callID: input.callID, cwd: directory,
                                result_bytes: text === null ? null : Buffer.byteLength(text) });
  },
});
'''
# Options a project file may not set: internal ones, config selection itself,
# workspace order (main workspace = where the file lives) and sessions.
CONFIG_FORBIDDEN = ("--config", "--no-config", "--tmux-socket-dir", "--pod-name", "--mcp-socket-dir", "--helper-of",
                    "--vpn-lent-by",
                    "--workspace", "--workspace-name", "--workspaces-as-given",
                    "--session", "--dry-run", "--mcp-approvals-dir", "--rules", "--mode", "--name", "--project-name",
                    # what a start lacks is fixed unasked only where the human said so on the command line
                    "--fix", "--fix-only", "--fix-plan",
                    # another session's login is this machine's, never a project's choice (the Board passes it with
                    # the start, as --name)
                    "--auth-from-session",
                    "--start-id", "--")
# Mutually exclusive auth choices: one given on the command line replaces the file's.
AUTH_OPTIONS = {"--copy-auth": False, "--no-auth": False, "--auth-file": True, "--import-codex-auth": True,
                "--auth-from-session": True}
CONFIG_LIMIT = 1 << 20
# Claude's ~/.claude.json keeps per-project history and often exceeds CONFIG_LIMIT.
CLAUDE_STATE_LIMIT = 64 << 20
GUI_DISPLAY = ":99"
WAYLAND_TARGET = "/run/user/0/wayland-0"
DEFAULT_NAMESERVERS = ["1.1.1.1", "8.8.8.8"]
# Network profiles: named bundles of network options, shared by the launcher and the wizard.
PROFILES = {
    "development": {"presets": ["pypi", "github"], "frontier": True,
                    "help": "the agent's provider plus PyPI, GitHub and the OpenAI and Anthropic APIs"},
    "frontier": {"frontier": True, "help": "the agent's provider plus the OpenAI and Anthropic APIs"},
    "dns": {"dns": True, "help": "real DNS lookups (addresses stay closed)"},
    "egress-all": {"egress_all_allow": True, "help": "every outbound destination; inbound stays closed"},
    "offline": {"offline": True, "help": "no network at all"},
}


# Short forms of the options: one letter for the frequent ones (these can be bundled, e.g. -Dn),
# two letters for the rest. A two-letter form is an option of its own: -gl is --allow-gitlab, not -g -l.
SHORT_OPTIONS = {
    "--version": "-V", "--backend": "-b", "--workspace": "-w", "--workspace-ro": "-W", "--hide": "-hf", "--mount": "-m",
    "--no-checkpoint": "-ncp", "--checkpoint-every": "-ce", "--checkpoint-mounts": "-cm",
    "--device": "-d", "--gpu": "-G", "--profile": "-p", "--mode-at-least": "-ml", "--preset": "-ps", "--preset-value": "-pv", "--allow": "-a", "--allow-host": "-A", "--allow-domain": "-ad",
    "--allow-shared-wildcard": "-sw", "--yes": "-ys",
    "--allow-pypi": "-y", "--allow-github": "-g", "--allow-gitlab": "-gl", "--allow-kernel-org": "-ak",
    "--allow-duckduckgo": "-dd", "--allow-npm": "-an", "--allow-tcp": "-t", "--allow-host-port": "-P", "--git-token-env": "-ge",
    "--git-token-file": "-gf", "--git-credentials": "-gc", "--git-identity": "-I", "--identity": "-id", "--privacy-check": "-pc",
    "--privacy-rules": "-pr", "--no-proxy": "-X", "--no-inspect": "-x", "--allow-http": "-ah", "--no-provider-defaults": "-nd",
    "--frontier": "-f", "--dangerously-skip-permissions": "-D", "--local-config": "-L", "--offline": "-o",
    "--egress-all-allow": "-E", "--dns": "-R", "--nameserver": "-ns", "--subnet": "-sn", "--subnet6": "-s6",
    "--network": "-nw", "--ip": "-ip",
    "--ip6": "-i6", "--image": "-i", "--podman-url": "-pu", "--binary": "-B", "--runtime": "-rt",
    "--toolchain": "-tc", "--toolchain-offline": "-to",
    "--copy-auth": "-ca", "--no-auth": "-na", "--auth-file": "-af", "--import-codex-auth": "-ic",
    "--auth-from-session": "-as",
    "--image-variant": "-iv", "--nested-containers": "-ct", "--service": "-sv",
    "--copy": "-cp", "--state-dir": "-S", "--kit": "-k", "--kit-dir": "-K", "--no-default-kit": "-nk",
    "--no-memory": "-nm", "--no-plugins": "-np", "--intro": "-in", "--no-intro": "-ni", "--env": "-e", "--log-dir": "-l", "--gui": "-gu",
    "--gui-screen": "-gs", "--audio": "-au", "--gui-vnc": "-gv", "--gui-wayland": "-gw",
    "--browser": "-bw", "--browser-control": "-bc", "--browser-profile": "-bp",
    "--cairn": "-cr", "--cairn-workers": "-cw", "--cairn-origin": "-co", "--cairn-goal": "-cg", "--cairn-hint": "-ch",
    "--cairn-board": "-cb", "--artex": "-ax", "--artex-data": "-xd", "--artex-port": "-xp",
    "--tmux": "-T", "--no-tmux": "-nt",
    "--mcp": "-M", "--mcp-limits": "-mx", "--mcp-mount": "-mt", "--mcp-env": "-mv", "--helper": "-hl", "--with": "-wa", "--session": "-s", "--name": "-N", "--name-style": "-nv", "--project-name": "-pn", "--keep": "-kp",
    "--no-keep": "-nkp", "--config": "-c", "--no-config": "-nc", "--dry-run": "-n",
    "--fix": "-fx", "--fix-only": "-fo", "--fix-plan": "-fp",
    "--probe": "-pb", "--memory": "-me", "--cpus": "-cu", "--pids": "-pi", "--tmp-size": "-ts", "--ask": "-Q", "--ask-timeout": "-at",
    "--secret-env": "-se", "--api-key": "-ky", "--model": "-mm", "--effort": "-ef", "--show-thinking": "-sk", "--publish": "-pl", "--git-token-secret": "-gk", "--token-proxy": "-tp", "--git-logins": "-gn", "--rules": "-ru",
    "--log-debug": "-ld", "--llm": "-lm", "--llm-param": "-lp", "--no-llm-profile": "-nl", "--use-case": "-uc", "--llm-models": "-lo", "--llm-tools": "-lw", "--vpn": "-vp", "--vpn-check": "-vc",
    "--vpn-bypass-host-ports": "-vb", "--vpn-bypass-tcp": "-vt", "--mode": "-mo",
    "--canaries": "-cn", "--no-canaries": "-nn", "--allow-package": "-ap", "--no-package-guard": "-ng",
    "--allow-send": "-sd",
    "--telemetry": "-tm", "--hermes-enable": "-he", "--qwen-enable": "-qe",
    "--llm-traffic": "-lt", "--llm-traffic-limit": "-ll", "--goose-enable": "-gx", "--budget": "-bu",
    "--openhands-enable": "-ox",
    "--nanobot-enable": "-nb",
    "--letta-enable": "-lx",
    "--opencode2-enable": "-oe",
    "--pi-enable": "-px",
}
HELP_EPILOG = """\
Every option has a short form (shown above). One-letter forms can be bundled
(-Dn = --dangerously-skip-permissions --dry-run) and take a value directly
(-w~/src/app); two-letter forms such as -gl are options of their own. Arguments
for the agent itself follow --.

Hardware for the agent, safest first:
  1. A service on the host, reached over the network: ser2net or socat
     (serial ports, RFC 2217), openocd or gdbserver (debug probes), the adb
     server (Android devices), an inference server such as Ollama or
     llama.cpp (GPU), allowed with --allow-host-port PORT (Podman) or
     --allow-tcp IP:PORT. No host driver is exposed and replugging does not
     matter, but you run and set up the service.
  2. --device PATH: the device itself. Often works out of the box (GPU
     render nodes, /dev/kvm, cameras, sound, devices your desktop session
     grants you through udev's uaccess tag), but the agent reaches the
     device's driver in your host's kernel. Access through groups such as
     dialout, uucp or plugdev is not kept: allow your user with a udev rule.
  3. --gpu nvidia|dri|rocm: the GPU devices in one option, with the same
     trade-off as --device.

Examples (more in QUICKSTART.md):
  sandbox-ai AGENT                                this directory as /work; detach: Ctrl-b d
  sandbox-ai AGENT -w ~/src/app -p development -D PyPI, GitHub, both APIs; no prompts
  sandbox-ai AGENT -n                             show the plan, start nothing
  sandbox-ai AGENT -N web                         named web: sandbox-ai attach|stop|resume web
  sandbox-ai AGENT --no-keep -- -p "task"         a task whose HOME goes when it ends
"""

# The agents by the names their --help gives them (sandbox-ai --help lists them alike: tests/test_main_help.py).
AGENT_TITLES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "opencode2": "OpenCode 2",
                "hermes": "Hermes Agent", "dsh": "DeepSeek Harness", "qwen": "Qwen Code", "goose": "Goose",
                "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code", "pi": "Pi"}
# The features of one agent (--AGENT-enable): in an agent's own --help under "Agent", the other agents' last (they
# apply to an agent started beside it with --with).
AGENT_FEATURES = {"hermes": "--hermes-enable", "qwen": "--qwen-enable", "goose": "--goose-enable",
                  "openhands": "--openhands-enable", "nanobot": "--nanobot-enable", "letta": "--letta-enable",
                  "opencode2": "--opencode2-enable", "pi": "--pi-enable"}
OTHER_FEATURES = "Features of the other agents (for one started beside it with --with AGENT)"

HELP_GROUPS = [  # --help: options by topic, in this order
    ("Presets", ["--preset", "--preset-value"]),
    ("Security mode", ["--mode", "--mode-at-least"]),
    ("Workspace and files", ["--workspace", "--workspace-ro", "--hide", "--mount", "--copy", "--state-dir", "--config",
                             "--no-config", "--no-checkpoint", "--checkpoint-every", "--checkpoint-mounts"]),
    ("Network", ["--profile", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--yes", "--allow-pypi", "--allow-github", "--allow-gitlab",
                 "--allow-kernel-org", "--allow-duckduckgo", "--allow-npm", "--frontier", "--allow", "--allow-tcp",
                 "--allow-host-port", "--no-provider-defaults", "--dns", "--nameserver", "--offline",
                 "--egress-all-allow", "--subnet", "--subnet6", "--network", "--ip", "--ip6"]),
    ("VPN and Tor", ["--vpn", "--vpn-check", "--vpn-bypass-host-ports", "--vpn-bypass-tcp"]),
    ("Connection proxy, approvals and privacy", ["--ask", "--ask-timeout", "--rules", "--allow-http", "--privacy-check",
                                                 "--privacy-rules", "--no-inspect", "--no-proxy", "--canaries",
                                                 "--no-canaries", "--allow-package", "--no-package-guard",
                                                 "--allow-send", "--telemetry", "--budget"]),
    ("Git", ["--identity", "--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret", "--git-logins", "--token-proxy",
             "--git-credentials"]),
    ("Agent", ["--model", "--effort", "--show-thinking", "--dangerously-skip-permissions", "--local-config",
               "--intro", "--no-intro", *AGENT_FEATURES.values()]),
    ("Model server (LLM)", ["--llm", "--use-case", "--llm-models", "--llm-param", "--llm-tools",
                            "--no-llm-profile", "--llm-traffic", "--llm-traffic-limit"]),
    ("Login and secrets", ["--copy-auth", "--no-auth", "--auth-file", "--auth-from-session", "--import-codex-auth",
                           "--env", "--secret-env",
                           "--api-key", "--publish"]),
    ("MCP servers and helpers", ["--mcp", "--mcp-limits", "--mcp-mount", "--mcp-env", "--helper", "--with"]),
    ("Kits, memory and plugins", ["--kit", "--kit-dir", "--no-default-kit", "--no-memory", "--no-plugins"]),
    ("Sessions and terminal", ["--name", "--name-style", "--project-name", "--keep", "--no-keep", "--session",
                               "--tmux", "--no-tmux"]),
    ("Display, sound and browser", ["--gui", "--gui-screen", "--gui-vnc", "--gui-wayland", "--audio", "--browser",
                                    "--browser-control", "--browser-profile"]),
    ("Cairn (task board)", ["--cairn", "--cairn-workers", "--cairn-origin", "--cairn-goal", "--cairn-hint",
                            "--cairn-board"]),
    ("ARTEX (pentest platform)", ["--artex", "--artex-data", "--artex-port"]),
    ("Hardware and limits", ["--device", "--gpu", "--memory", "--cpus", "--pids", "--tmp-size"]),
    ("Engine", ["--backend", "--image", "--image-variant", "--service", "--nested-containers", "--podman-url", "--binary",
                "--runtime"]),
    ("Toolchains", ["--toolchain", "--toolchain-offline"]),
    ("Logs and checks", ["--log-dir", "--log-debug", "--dry-run", "--fix", "--fix-only", "--fix-plan", "--probe",
                         "--version", "--help"]),
]


def load_helpstyle():
    """helpstyle.py: the layout of every --help of sandbox-ai (loaded only for a help or an error message)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        loaded.install_colour()  # (its pages coloured as they are printed to a terminal; main.py's copy did it)
        loaded = sys.modules.setdefault("sandbox_ai_helpstyle", loaded)
    return loaded


class HelpFormatter:
    """Each option on one line with its short form and its value once ("-w, --workspace DIR[,DIR...]"), under its
    topic, laid out as every --help of sandbox-ai: helpstyle.Formatter, loaded when a help or an error is shown."""

    def __new__(cls, prog):
        return load_helpstyle().Formatter(prog, max_help_position=38)


def group_help(p, agent=None):
    """Sort the options into HELP_GROUPS for --help (parsing is unchanged); options not listed come last. With
    AGENT (its own --help) the features of the other agents follow in a group of their own."""
    listed = {name for _, names in HELP_GROUPS for name in names}
    others = [option for name, option in AGENT_FEATURES.items() if agent and name != agent]
    by_name = {action.option_strings[-1]: action for action in p._actions if action.option_strings}
    general = p._optionals
    for title, names in [*HELP_GROUPS, (OTHER_FEATURES, others)]:
        group = p.add_argument_group(title)
        for name in names:
            if title != OTHER_FEATURES and name in others:
                continue
            action = by_name.get(name)
            if action is not None and action in general._group_actions:
                general._group_actions.remove(action)
                group._group_actions.append(action)
    general.title = "Other options" if any(a.option_strings and a.option_strings[-1] not in listed and
                                           a.help != argparse.SUPPRESS for a in general._group_actions) else None
    p._action_groups.remove(general)
    p._action_groups.append(general)


# Options whose value may name several items, separated by commas and/or whitespace ("pypi.org, *.example.com",
# 10.0.0.5:22,10.0.0.6:22): none of their items can hold a comma or a space (names, addresses, networks, ports).
# Options whose values hold either by meaning stay one value each: --api-key HOST=NAME=SECRET, --allow-http rules
# ('HOST METHODS PATH'), paths and mounts, NAME=VALUE pairs, --profile (a choice; repeat it). One value per option
# everywhere: a second word after it is never another item (it would be the run of `net allow -A x RUN`, and every
# reader of saved option lists - project files, presets, MCP definitions, kept starts, the app, older hosts - takes
# one word per value); quoted, "a b" is the list.
LIST_OPTIONS = ("--allow", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow-tcp", "--allow-host-port",
                "--no-inspect", "--nameserver", "--vpn-bypass-tcp")
LIST_HELP = "one or more, separated by commas or spaces; repeatable"


def list_items(value):
    """The items of one VALUE of a LIST_OPTIONS option: split at commas and whitespace, empty ones (",,") left out,
    each once (the first kept), in their order. [] for a value without any."""
    return list(dict.fromkeys(item for item in re.split(r"[\s,]+", str(value)) if item))


class ListOption(argparse._AppendAction):
    """argparse action of a LIST_OPTIONS option: each item of the value (list_items) appended once, ITEM_TYPE applied
    to each (a bad one names itself); a value without any item is refused."""

    def __init__(self, *args, item_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.item_type = item_type

    def __call__(self, parser, namespace, values, option_string=None):
        items = list_items(values)
        if not items:
            raise argparse.ArgumentError(self, "no value (one or more, separated by commas or spaces)")
        current = list(getattr(namespace, self.dest, None) or [])  # (a copy: never the default list itself)
        for item in items:
            if self.item_type is not None:
                try:
                    item = self.item_type(item)
                except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
                    raise argparse.ArgumentError(self, f"{item!r}: {exc}") from None
            if item not in current:
                current.append(item)
        setattr(namespace, self.dest, current)


def spread_lists(tokens):
    """TOKENS (canonical: long option names, as canonical_options writes them) with each value of a LIST_OPTIONS
    option given as its items, one option each (--allow-host a,b: --allow-host a --allow-host b; --allow-tcp=X,Y:
    --allow-tcp=X --allow-tcp=Y). A value without any item stays as given (the parser refuses it); what follows --
    is the agent's, untouched."""
    actions = option_actions()
    result, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            result += tokens[index - 1:]
            break
        name, equals, value = token.partition("=")
        action = actions.get(name) if token.startswith("--") else None
        if action is None or action.nargs == 0:
            result.append(token)
        elif equals:
            items = list_items(value) if name in LIST_OPTIONS else []
            result += [f"{name}={item}" for item in items] if items else [token]
        elif index < len(tokens):
            value = tokens[index]
            index += 1
            items = list_items(value) if name in LIST_OPTIONS else []
            result += [word for item in items for word in (name, item)] if items else [token, value]
        else:
            result.append(token)
    return result


_OPTION_ACTIONS = None


def option_actions():
    """{option string: argparse action} of build_parser(), built once: what the code that looks options up by name
    (canonical_options, project files, merging, the modes) reads, never changes (each build took 4 ms, and a start
    looked several times)."""
    global _OPTION_ACTIONS
    if _OPTION_ACTIONS is None:
        _OPTION_ACTIONS = build_parser()._option_string_actions
    return _OPTION_ACTIONS


def build_parser(agent=None):
    """The launcher's option parser (also used by the wizard to split option lists); AGENT: the one its usage line and
    --help name (parsing is the same)."""
    # No prefix matching: abbreviations must not reach internal or forbidden options.
    agent = agent if agent in AUTH_TARGETS else None
    p = argparse.ArgumentParser(prog="sandbox-ai", usage=f"sandbox-ai {agent or 'AGENT'} [OPTIONS] [-- AGENT ARGS]",
                                description=(f"Start {AGENT_TITLES.get(agent, agent)} in a sandbox: " if agent else
                                             "Start a coding agent in a sandbox: ") +
                                "rootless Podman (or Bubblewrap) and Firejail, behind a firewall and a connection "
                                "proxy that asks you before anything new leaves." +
                                ("" if agent else f" AGENT is one of {', '.join(AUTH_TARGETS)}."),
                                epilog=HELP_EPILOG.replace("sandbox-ai AGENT", f"sandbox-ai {agent or 'claude'}"),
                                formatter_class=HelpFormatter, allow_abbrev=False)
    add = p.add_argument

    def add_with_short_form(*names, **kwargs):
        return add(*[SHORT_OPTIONS[name] for name in names if name in SHORT_OPTIONS], *names, **kwargs)
    p.add_argument = add_with_short_form
    p.add_argument("agent", choices=AUTH_TARGETS, help=argparse.SUPPRESS)  # named in the usage line
    p.add_argument("--version", action="version", version=f"sandbox-ai {__version__}")
    p.add_argument("--mode-at-least", choices=MODES.NAMES, metavar="MODE",
                   help="A floor for the security mode: MODE where the project file and your defaults choose a laxer "
                        "one, never lower than they (a preset's --mode is one; the app passes it so)")
    p.add_argument("--mode", choices=MODES.NAMES, metavar="MODE",
                   help="Security mode, a preset of the options below; options may only narrow it, in high and "
                        "paranoid one that widens it is refused (default: the project file's \"mode\", else "
                        "\"mode\" in ~/.config/sandbox-ai/defaults.json, else medium; never below the "
                        "administrator's min_mode). " + "; ".join(MODES.summaries()))
    p.add_argument("--backend", choices=list(BACKENDS), default="podman",
                   help="podman (default): a rootless pod from a prebuilt image; bwrap: Bubblewrap with the agent "
                        "installed on the host")
    p.add_argument("--workspace", action="append", metavar="DIR[,DIR...]",
                   help="Host directory at /work (default: current directory); further entries, repeated or "
                        "comma-separated, appear at /workspaces/<name>. A folder of another machine: "
                        "sshfs://[USER@]HOST[:PORT]/PATH, nfs://SERVER/EXPORT[/PATH] or "
                        "smb://[USER@]SERVER/SHARE[/PATH][?secret=NAME], mounted on this host for the run")
    p.add_argument("--workspace-ro", action="store_true", help="Mount every workspace read-only")
    p.add_argument("--hide", action="append", default=[], type=hide_glob, metavar="GLOB",
                   help="Keep the workspace files GLOB matches out of the agent's sight: an empty read-only file "
                        "(or folder) is mounted over each one found at the start (.env, .env.*, *.pem, id_*, "
                        "config/secrets/**; without '/' a name anywhere); a symlink is never hidden, a file Git "
                        "tracks stays in its objects (both said); repeatable")
    # NAME=DIR: the further workspace DIR (a --workspace) at /workspaces/NAME; what `workspace add --name` keeps.
    p.add_argument("--workspace-name", action="append", default=[], metavar="NAME=DIR", help=argparse.SUPPRESS)
    # The workspaces are exactly as given: a project configuration in the working directory does not take over.
    # Starts the app, the web interface and `resume` build pass it (their working directory is not the project).
    p.add_argument("--workspaces-as-given", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--mount", action="append", default=[], metavar="SRC:DEST:ro|rw",
                   help="Repeatable; DEST under /work, /mnt or /opt; SRC may be a folder of another machine "
                        "(sshfs://..., nfs://..., smb://..., mounted on this host)")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="No checkpoint of the workspaces when this start begins (default: one is taken on the host "
                        "before the agent runs, unless checkpoint_start is false in ~/.config/sandbox-ai/defaults.json;"
                        " sandbox-ai checkpoint restore NAME undoes what the agent did)")
    p.add_argument("--checkpoint-every", type=checkpoint_minutes, default=None, metavar="MINUTES",
                   help="Also a checkpoint every MINUTES while it runs, when something changed (default: "
                        "checkpoint_every of defaults.json, else none; 0: none)")
    p.add_argument("--checkpoint-mounts", action="store_true", default=None,
                   help="The checkpoints hold the rw --mount folders too, not only the workspaces (default: "
                        "checkpoint_mounts of defaults.json)")
    p.add_argument("--device", action="append", default=[], metavar="PATH|usb:VID:PID[:SERIAL]",
                   help="Hand a host device to the agent: a character device under /dev (symlinks such as "
                        "/dev/serial/by-id/... are resolved), one of the directories "
                        + ", ".join("/dev/" + d for d in DEVICE_DIRS) + " (devices plugged in later appear "
                        "there too), or a USB device by its IDs, usb:VENDOR:PRODUCT[:SERIAL] (its nodes, found "
                        "through sysfs; attached again when it is replugged under another node). Device rules of "
                        "the rulesets (--rules) allow, ask about or deny each. Its driver runs in your host's "
                        "kernel, so this weakens isolation; a service on the host reached over the network is "
                        "safer (see below). The agent can "
                        "open it only if your user may without supplementary groups; repeatable")
    p.add_argument("--gpu", choices=GPU_KINDS,
                   help="Hand the GPU to the agent: nvidia (Podman: through CDI, after `sudo nvidia-ctk cdi "
                        "generate --output=/etc/cdi/nvidia.yaml`; bwrap: /dev/nvidia*), dri (Intel/AMD render "
                        "nodes in /dev/dri; Mesa, Vulkan or VA-API come from your own image) or rocm (dri plus "
                        "/dev/kfd). Weakens isolation like --device")
    p.add_argument("--memory", default="8g", metavar="SIZE",
                   help="Memory limit of the sandbox, e.g. 4g or 512m (default: 8g; none: no limit)")
    p.add_argument("--cpus", default=None, metavar="N",
                   help="CPU limit, e.g. 2 or 1.5 (default: half of this host's CPUs; none: no limit)")
    p.add_argument("--pids", default="4096", metavar="N",
                   help="Limit on processes and threads (default: 4096; none: no limit)")
    p.add_argument("--tmp-size", metavar="SIZE",
                   help="Podman: the size of /tmp and of /var/tmp each, e.g. 6g (default: half the memory limit; files "
                        "there are memory, so each at most half of it)")
    p.add_argument("--preset", metavar="NAME",
                   help="Start from the preset NAME (sandbox-ai presets list): its options, MCP servers, ruleset and "
                        "first task; the command line wins over it. A bridge it proposes is asked about once the "
                        "start runs (said as a command without a terminal)")
    p.add_argument("--preset-value", action="append", default=[], metavar="KEY=VALUE",
                   help="A value the preset asks for (sandbox-ai presets show NAME); repeatable")
    p.add_argument("--profile", action="append", default=[], choices=PROFILES, metavar="NAME",
                   help="Network profile: " + "; ".join(f"{name}: {value['help']}" for name, value in PROFILES.items())
                        + "; repeatable")
    # --allow-net: the documented name (2026-11-24); --allow, its older name, stays an alias everywhere (the last name:
    # option_strings[-1] and canonical_options keep --allow, what every reader of saved options and older hosts know).
    p.add_argument("--allow-net", "--allow", dest="allow", action=ListOption, default=[], metavar="IP_OR_CIDR",
                   help="Every port and protocol of an address or network (IP or CIDR), e.g. a machine of your LAN; "
                        f"--allow is the same (its older name); {LIST_HELP}")
    # Lower case: host names are case-insensitive, EXAMPLE.org and example.org are one host (one pin, one entry).
    p.add_argument("--allow-host", action=ListOption, default=[], metavar="HOST", type=str.lower,
                   help=f"Pin hostname and allow TCP/443; {LIST_HELP}")
    # Exactly --allow-host DOMAIN --allow-host '*.DOMAIN' (expand_domains), with the same checks.
    p.add_argument("--allow-domain", action=ListOption, default=[], metavar="DOMAIN", type=str.lower,
                   help="DOMAIN and every name below it on TCP/443: --allow-host DOMAIN --allow-host '*.DOMAIN' in "
                        "one (*.DOMAIN, .DOMAIN and DOMAIN. name DOMAIN too); refused for a public suffix (co.uk) "
                        f"or a hosting service (github.io); {LIST_HELP}")
    # --allow-host '*.DOMAIN' below a shared service (expand_shared_wildcards), its risk confirmed by giving it so.
    p.add_argument("--allow-shared-wildcard", action=ListOption, default=[], metavar="'*.DOMAIN'", type=str.lower,
                   help="A host wildcard below a shared service where anyone can rent a name (*.blob.core.windows.net, "
                        "*.github.io): the agent could open any server anyone rents there and send data there. Giving "
                        "it here is your confirmation of that risk (--allow-host '*.DOMAIN' asks on a terminal and is "
                        "refused elsewhere). Refused in the modes high and paranoid and where the administrator's "
                        "policy forbids it; a top-level domain (*.com) and a country's public suffix (*.co.uk) are "
                        f"refused always; {LIST_HELP}")
    p.add_argument("--yes", action="store_true",
                   help="Answer the start's own risk question with yes, for scripts: a wildcard below a shared "
                        "service given with --allow-host (as --allow-shared-wildcard)")
    for preset, hosts in PRESETS.items():
        p.add_argument(f"--allow-{preset}", action="append_const", const=preset, dest="presets", default=[],
                       help=f"Host set: allow {', '.join(hosts)} (TCP/443)")
    p.add_argument("--allow-tcp", action=ListOption, default=[], metavar="IP:PORT",
                   help=f"Allow one TCP port on one address, e.g. a local LLM server; IPv6 as [ADDR]:PORT; {LIST_HELP}")
    p.add_argument("--allow-host-port", action=ListOption, default=[], metavar="PORT",
                   help="Podman: forward 127.0.0.1:PORT in the sandbox to the same TCP port on the host's loopback, "
                        f"e.g. a local Git server; {LIST_HELP}")
    p.add_argument("--git-token-env", action="append", default=[], metavar="URL=NAME",
                   help="Host variable NAME with an access token for Git over HTTP(S) at URL, e.g. "
                        "https://git.example.org=GITEA_TOKEN or https://github.com=GH_PAT (user as "
                        "http://USER@HOST:PORT); loopback URLs imply --allow-host-port, github.com --allow-github "
                        "and sets GH_TOKEN, other URLs --allow-host; repeatable")
    p.add_argument("--git-token-file", action="append", default=[], metavar="URL=FILE",
                   help="Like --git-token-env, but the token is read from FILE (yours, mode 0600, outside every "
                        "workspace); repeatable")
    p.add_argument("--git-token-secret", action="append", default=[], metavar="URL=SECRET",
                   help="Like --git-token-env, but the token is the secret SECRET of sandbox-ai secrets (your "
                        "keyring); repeatable")
    p.add_argument("--identity", metavar="NAME",
                   help="Work as the identity NAME of sandbox-ai identities: its commit name and address, its Git "
                        "logins instead of sandbox-ai git-logins' and, with --publish, its publish logins; the tokens "
                        "stay in the connection proxy (default: the identity default, your git-logins and "
                        "publish-logins)")
    p.add_argument("--git-logins", choices=("on", "off"), default="on",
                   help="sandbox-ai git-logins in the connection proxy (on, the default): your Git servers' logins are "
                        "added on the wire to Git's requests to them, never handed to the agent; off: without them")
    p.add_argument("--token-proxy", action="store_true",
                   help="Keep the --git-token-* tokens out of the sandbox: the connection proxy adds them to Git "
                        "requests to their HTTPS server; the agent can clone and push but never reads a token")
    p.add_argument("--git-credentials", type=Path, metavar="FILE",
                   help="Logins for Git over HTTP(S) from FILE in git's credential-store format, e.g. "
                        "~/.git-credentials (yours, mode 0600, outside every workspace); each server is reached "
                        "as with --git-token-env, a --git-token-* option for the same server wins; the agent "
                        "can read every login it is given")
    p.add_argument("--git-identity", action="store_true",
                   help="Commit in the sandbox as the host's git user.name and user.email")
    p.add_argument("--privacy-check", choices=["public", "always", "off"], default="public",
                   help="After the run, show what the next push of each writable Git workspace would publish "
                        "that should stay private: of workspaces with a public remote (public, the default), "
                        "of every one (always) or never (off); unless off, the agent's pre-commit hook warns "
                        "and its pre-push hook refuses such a push to a public remote")
    p.add_argument("--privacy-rules", type=Path, metavar="DIR",
                   help="Your own rules for the privacy check: DIR/global.rules.toml and DIR/PROJECT.rules.toml "
                        "(or .json), PROJECT being the workspace's directory name; outside every workspace, never "
                        "shown to the agent (default: ~/.config/sandbox-ai/privacy if it exists)")
    p.add_argument("--no-proxy", action="store_true",
                   help="No connection proxy: connections are neither logged nor opened, Git pushes are checked "
                        "only by the agent's hooks. By default every TCP connection of the agent passes a proxy "
                        "that opens TLS with a CA made for the run, logs each HTTP request with headers and "
                        "bodies (redacted, 64 KiB each) to connections-*.jsonl in the log directory, refuses "
                        "pushes the privacy check stops and SSH except to destinations allowed with --allow or "
                        "--allow-tcp")
    p.add_argument("--ask", choices=["on", "off", "allow"], default="on",
                   help="With the proxy, ask (sandbox-ai approvals, the app, a desktop notification) before a "
                        "connection to a destination the options do not allow, a lookup of a name they do not pin, "
                        "a push with privacy warnings, a request or tool call an ask rule holds, or opening a URL in "
                        "your browser; a request waits up to --ask-timeout for the answer, then fails while its "
                        "question stays open: answered later, it counts for the next try (default: on). off: refuse "
                        "all "
                        "of them without asking. allow: ask nothing and allow every question at once, for the run, "
                        "each recorded (sandbox-ai approvals history, the connection log); what rules deny, the "
                        "privacy check's blocking findings and a canary's alarm are no questions and stay as they "
                        "are")
    p.add_argument("--ask-timeout", type=ask_seconds, default=None, metavar="SECONDS",
                   help=f"How long a request waits for the answer to its question (default: ask_timeout of "
                        f"~/.config/sandbox-ai/defaults.json, else {ASK_TIMEOUT_DEFAULT}; "
                        f"{ASK_TIMEOUT_RANGE[0]}-{ASK_TIMEOUT_RANGE[1]}). Unanswered by then, that try fails and the "
                        "question stays open: answered later, the answer counts for the next try")
    p.add_argument("--allow-http", action="append", default=[], metavar="'HOST METHODS PATH'",
                   help="Allow only matching requests to HOST (or *.DOMAIN): METHODS such as GET,HEAD or *, PATH a "
                        "pattern where * matches within one path segment and ** across them, e.g. "
                        "'api.github.com GET,HEAD /repos/**'; once a host has rules, every other request to it is "
                        "refused; repeatable")
    p.add_argument("--rules", action="append", default=[], type=rules_file, metavar="FILE",
                   help="Allow, ask or deny per HTTP request (host, method, path, query, headers, JSON body) and per "
                        "MCP tool call (tool, arguments): a JSON ruleset outside every workspace, applied with "
                        f"~/.config/sandbox-ai/{RULES_NAME} and the administrator's and the project's rules; each can "
                        "only narrow what the others allow; builtin:NAME is one of sandbox-ai's own (github-persistence, "
                        "destructive, mcp-no-credentials, secret-files: sandbox-ai rules builtin); a ruleset with "
                        "\"mode\": \"audit\" only records what it would decide; repeatable")
    p.add_argument("--canaries", metavar="CATEGORIES",
                   help="Decoy files with tokens made for the run, which no service accepts: credentials (fake "
                        "~/.aws/credentials, gcloud, SSH key, gh, ~/.netrc, ~/.git-credentials) and home "
                        "(a password list, ~/.kube/config), the default; workspace (a .env in each workspace, kept out of Git and "
                        "removed at the end); comma-separated, or all. A read is reported, a token leaving "
                        "(proxy, DNS, push) is refused and stops the sandbox; rulesets' \"canary\" section decides "
                        "(sandbox-ai canaries list RUN)")
    p.add_argument("--no-canaries", action="store_const", const="none", dest="canaries",
                   help="No canary files")
    p.add_argument("--allow-package", action="append", default=[], metavar="ECOSYSTEM:NAME[@VERSION]",
                   help="The package guard passes this package in this run without asking about its age or a new "
                        "name: npm:left-pad, pypi:requests@2.32.0, crates:serde, go:github.com/x/y@v1.2.3; "
                        "repeatable. Never a version on the list of known malicious packages. For every run of the "
                        "project: sandbox-ai packages allow")
    p.add_argument("--no-package-guard", action="store_true",
                   help="Without the package guard: the proxy does not judge what npm, pip, cargo and go download "
                        "(known malicious versions refused, versions younger than 14 days and brand-new names asked "
                        "about; sandbox-ai packages). Refused in the modes high and paranoid")
    p.add_argument("--allow-send", action="append", default=[], metavar="WHAT@HOST",
                   help="The secret guard lets this pass in this run: WHAT - a kind of secret it finds in requests (a "
                        "token shape such as github-token or google-api-key, a value the run was given such as "
                        "env:NAME or git-token:HOST, or * for any) - on its way to HOST (a name, *.DOMAIN, or * for "
                        "any host): github-token@api.example.com, env:API_TOKEN@*.example.com; repeatable. Without "
                        "it such a request is reported (asked about or refused in the modes high and paranoid, which "
                        "refuse a * here). For every run of the project: sandbox-ai secret-guard allow")
    p.add_argument("--telemetry", choices=TelemetryChoices(TELEMETRY_LEVELS), action=TelemetryOption,
                   help="The agents' telemetry is always off, for every agent: no usage statistics, error reports, "
                        "feedback uploads, session sharing, OpenTelemetry exports or update checks (off, the "
                        "default); strict: also no other call that is not the model's (plugin downloads, model "
                        "lists, language servers; the default of --mode paranoid); \"telemetry\" in "
                        "~/.config/sandbox-ai/defaults.json. The former on is ignored, with a note")
    p.add_argument("--no-inspect", action=ListOption, default=[], metavar="HOST",
                   help="Pass TLS to HOST (or *.DOMAIN) through unopened, for programs that do not accept the "
                        f"session CA; only the name is logged, and pushes there are not checked; {LIST_HELP}")
    p.add_argument("--no-provider-defaults", action="store_true",
                   help="Do not open the agent's own provider hosts; only what other options allow")
    p.add_argument("--frontier", action="store_true", help="Also allow OpenAI and Anthropic API/auth hosts (TCP/443); everything else stays denied")
    p.add_argument("--dangerously-skip-permissions", action="store_true",
                   help="Start the agent without approval prompts; the outer sandbox and egress firewall stay in force")
    p.add_argument("--llm", metavar="NAME[:MODEL[,MODEL...]]",
                   help="Use the LLM server NAME of sandbox-ai llm (Ollama, LM Studio, llama.cpp, vLLM or another "
                        "OpenAI-compatible one, local or remote) with MODEL (default: the endpoint's model): opens "
                        "exactly its address and port and configures the agent for it; its key is added by the "
                        "connection proxy, the agent sees only a placeholder. With --no-provider-defaults the agent "
                        "reaches nothing else. The server's other models can be chosen in the agent (--llm-models). "
                        "NAME:MODEL,MODEL...: exactly these models, the first the agent's")
    p.add_argument("--llm-models", metavar="all|MODEL[,MODEL...]",
                   help="The models of --llm's server the agent offers besides its own (OpenCode /models, Qwen Code "
                        "/model, Hermes /model, Goose /model, DSH's web UI, Claude Code /model ID, Codex a profile per "
                        "model): all (the default) every model the server lists at the start, or these; the "
                        "connection proxy refuses requests for the others")
    p.add_argument("--llm-param", action="append", default=[], metavar="KEY=VALUE",
                   help="A sampling parameter of --llm's model for this start, in place of the agent's own value and "
                        "the endpoint's (sandbox-ai llm params lists them: temperature=0.7, top_k=40, "
                        "repeat_penalty=1.1, max_tokens=8192, num_ctx=32768 ...); loop_guard=on|off|N the loop "
                        "guard; applied by the connection proxy to that server's requests only; repeatable")
    p.add_argument("--llm-tools", metavar="all|core|TOOL[,TOOL...]",
                   help="Claude Code's own tools the connection proxy keeps in its requests to --llm's server: core "
                        "(the default) Bash, Read, Edit, Write, Glob, Grep and TodoWrite - the others are what local "
                        "models never used, ~10k tokens per request -, all every tool, core,TOOL,... adds some, "
                        "TOOL,TOOL,... names them exactly; every MCP tool stays. A model profile may set it; this "
                        "wins. A call of a tool left out gets a clear error")
    p.add_argument("--use-case", metavar="NAME",
                   help="The use case of this start (sandbox-ai llm recommend lists them: coding, sandbox, "
                        "untrusted-input, web-research, code-review, small-jobs, vision ...): with --llm NAME it picks "
                        "the server's model the local-model research found best for it in this agent (what is loaded, "
                        "what fits), and its settings for that model go over the model profile, under yours; a model "
                        "--llm names stays. A model the use case marks unsafe (one that ran a planted command where "
                        "the input is untrusted) is never picked on its own; named by you it starts with a warning. "
                        "Said at the start, with why")
    p.add_argument("--no-llm-profile", action="store_true",
                   help="Do not apply the model x harness profile of --llm's model in this agent (sandbox-ai llm "
                        "profiles: the local-model research's sampling, context window, turn limit and the "
                        "harness's switches, applied under your own values and said at the start)")
    p.add_argument("--local-config", type=Path, help="OpenCode local-only config: copy privately and disable default cloud access")
    p.add_argument("--offline", action="store_true", help="No outside network, including provider defaults")
    p.add_argument("--egress-all-allow", action="store_true",
                   help="Allow all outbound traffic (implies --dns); inbound stays closed")
    p.add_argument("--dns", action="store_true",
                   help=f"Real DNS via {' and '.join(DEFAULT_NAMESERVERS)} (UDP/TCP 53 to them only)")
    p.add_argument("--nameserver", action=ListOption, default=[], metavar="IP",
                   help=f"Use this resolver instead of the defaults, implies --dns; {LIST_HELP}")
    p.add_argument("--subnet", metavar="CIDR",
                   help="IPv4 network of the default sandbox network (pasta, slirp4netns), a private /8 to /25 such "
                        "as 10.20.0.0/24: the sandbox gets .100, the gateway .2 (default: ~/.config/sandbox-ai/"
                        "defaults.json, the policy, else the host's address with Podman, 10.0.2.0/24 with bwrap)")
    p.add_argument("--subnet6", metavar="CIDR",
                   help="Podman: IPv6 /64 of the default network in fc00::/7, such as fd20::/64: the sandbox gets "
                        "::100, the gateway ::2 (bwrap: slirp4netns always uses fd00::/64)")
    p.add_argument("--network", help="Existing Podman network (default: private rootless pasta)")
    p.add_argument("--ip", help="Static IPv4 address in --network")
    p.add_argument("--ip6", help="Static IPv6 address in --network")
    p.add_argument("--vpn", metavar="PROFILE",
                   help="Podman: put the VPN profile PROFILE in front (sandbox-ai vpn add ...): everything the sandbox "
                        "sends leaves through the tunnel, a kill switch blocks the rest (IPv4 and IPv6), and the "
                        "addresses and DNS seen from outside are checked before the agent starts")
    p.add_argument("--vpn-check", choices=["stop", "warn"], default="stop",
                   help="What a failed VPN check does: stop the start (default) or only warn")
    p.add_argument("--vpn-bypass-host-ports", action="store_true",
                   help="With a Tor profile: let the sandbox reach the host's loopback ports it names (--allow-host-port, "
                        "an --llm server or a Git server on this host) past Tor; whatever listens there reaches the "
                        "internet from your own address. Without it a Tor start refuses them")
    p.add_argument("--vpn-bypass-tcp", action=ListOption, default=[], metavar="ADDRESS:PORT",
                   help="With a Tor profile: reach exactly this IPv4 address and TCP port of your local network past "
                        "Tor (a model server on another machine, --llm); the rest of your network stays closed and "
                        "everything else goes through Tor. The connection proxy still checks it; whatever listens there "
                        f"could reach the internet from your own address; {LIST_HELP}")
    p.add_argument("--image", help="Prebuilt Podman image (default: localhost/<agent>-sandbox:latest); never pulled implicitly")
    p.add_argument("--image-variant", metavar="NAME",
                   help="Your image variant NAME of this agent (sandbox-ai images new NAME --from AGENT): "
                        "localhost/<agent>-sandbox-NAME:latest; a variant only another agent has is made for this "
                        "one too and built at the start")
    p.add_argument("--service", action="append", default=[], metavar="NAME[:KEY=VALUE,...]",
                   help="Podman: run one of your images (sandbox-ai images pull|build-image) as a service beside the "
                        "agent, e.g. db:image=postgres,port=5432,env=POSTGRES_PASSWORD=dev,data=/var/lib/postgresql/data"
                        ": a container in the sandbox's pod, reached at localhost:PORT, started before the agent and "
                        "removed with the run, behind the same firewall, connection proxy, allow-list and kill switch, "
                        "with no capabilities, a read-only root and its own limits. Its image is unchecked (nobody "
                        "reviewed it). Keys: image port ready ready-timeout env secret data keep user read-only memory "
                        "cpus pids device (device=: a device of this host for the service alone, never for the agent; "
                        "sandbox-ai images --help). sandbox-ai's own, built once with sandbox-ai images build-image "
                        "NAME --builtin NAME and started with their keys by --service NAME: android-emulator (the "
                        "Android emulator with /dev/kvm for itself: adb connect android-emulator:5555), adb (an adb "
                        "server for a USB device: adb:device=usb:VENDOR:PRODUCT). Repeatable")
    p.add_argument("--nested-containers", action="store_true",
                   help="Podman: the agent may run containers itself (podman, buildah, unshare -rn): it becomes root "
                        "of a user namespace of its own, with the subordinate IDs of the sandbox but the proxy's and "
                        "Tor's, /dev/fuse and /dev/net/tun, and fewer system calls blocked. What its containers send "
                        "leaves through the sandbox's firewall and connection proxy like its own connections. Weakens "
                        "the isolation: more of your host's kernel is reachable (not in the modes high and paranoid, "
                        "not behind Tor)")
    p.add_argument("--podman-url", metavar="URL", help="Podman service URL, e.g. unix:///example/podman.sock")
    p.add_argument("--binary", metavar="PATH", help="Agent executable (host path with bwrap; image path with podman)")
    p.add_argument("--runtime", action="append", default=[], metavar="DIR", help="bwrap: expose runtime directory read-only at same path")
    p.add_argument("--toolchain", action="append", default=[], metavar="NAME[@VERSION]",
                   help="Mount a build toolchain of your store (sandbox-ai toolchain add NAME: Go, Rust, a JDK, Node, "
                        "Python ... from the signed catalog) read-only at /opt/toolchains/NAME, its commands on the "
                        "agent's PATH, and open the registries it names (as --allow-host; not in the modes high and "
                        "paranoid, which mount it and keep them closed). NAME is the newest in the store, "
                        "NAME@VERSION that version or the newest it starts (jdk@17, python@3.12); one version of a "
                        "toolchain per sandbox. android-sdk (the Android SDK; android-sdk@36, @35) is built on this "
                        "machine from Google's downloads once you accepted their license (sandbox-ai toolchain add "
                        "android-sdk). Repeatable. Into a running sandbox: sandbox-ai toolchain attach RUN NAME")
    p.add_argument("--toolchain-offline", action="store_true",
                   help="Mount the toolchains without opening their registries")
    p.add_argument("--copy-auth", action="store_true",
                   help="Copy this agent's JSON credential files; they must exist on the host. A login already "
                        "in a reused sandbox HOME (session, --state-dir) is kept. Without any auth option, the "
                        "host's files are copied automatically when present and the sandbox has none yet")
    p.add_argument("--no-auth", action="store_true", help="Disable automatic copying of host credentials")
    p.add_argument("--auth-file", type=Path, help="Copy this JSON file to agent's standard auth path")
    p.add_argument("--auth-from-session", metavar="NAME",
                   help="Copy the login of the session NAME (sandbox-ai list; the same agent, running or with its "
                        "HOME kept) into this sandbox. Subscription logins rotate their refresh tokens: two sandboxes "
                        "refreshing one login outside the central store can log each other out (safer: sandbox-ai "
                        "login AGENT --from-session NAME). A refresh goes back into NAME's kept HOME at the end only "
                        "when NAME does not run then; a running session's HOME is never written")
    p.add_argument("--import-codex-auth", type=Path, metavar="FILE", help="OpenCode only: import a Codex ChatGPT auth.json as a private OAuth copy")
    p.add_argument("--copy", action="append", default=[], metavar="SRC:HOME_RELATIVE_DEST", help="Copy individual config/credential file; repeatable")
    p.add_argument("--state-dir", type=Path, metavar="DIR", help="Private persistent sandbox home in DIR; default: a HOME kept per start (sandbox-ai resume NAME), with --no-keep deleted after exit")
    p.add_argument("--kit", action="append", default=[], metavar="NAME",
                   help="Also use the kit ~/.local/share/sandbox-ai/kits/NAME/AGENT (skills, agents, commands, "
                        "prompts, memory template, marketplaces.json), else sandbox-ai's built-in kit NAME "
                        "(egm: Claude only); repeatable")
    p.add_argument("--kit-dir", action="append", default=[], metavar="DIR",
                   help="Also use the kit in DIR/AGENT; repeatable")
    p.add_argument("--no-default-kit", action="store_true",
                   help="Leave out the default kit ~/.local/share/sandbox-ai/home/AGENT and the built-in "
                        "skill sandbox-ai")
    p.add_argument("--no-memory", action="store_true",
                   help="No project memory: the agent's memory file and Claude's auto memory are neither "
                        "loaded nor kept")
    p.add_argument("--no-plugins", action="store_true",
                   help="No plugins from sandbox-ai's plugin store (Claude) or the kits (OpenCode)")
    p.add_argument("--intro", action="store_const", const=True, default=None,
                   help="Tell the agent what its sandbox has (the default): /sandbox/session/INTRO.md lists each "
                        "folder, toolchain, MCP server, service and device with where it is and how to use it - no "
                        "secret, no host path -, and one line where the agent reads its instructions names the file "
                        "(never a file of your repository). Rewritten when something is attached while it runs")
    p.add_argument("--no-intro", action="store_const", const=False, dest="intro",
                   help="Start without the intro: no INTRO.md, no line in the agent's instructions (your default: "
                        "sandbox-ai defaults set agent_intro false; an environment's: SANDBOX_AI_INTRO=0)")
    p.add_argument("--env", action="append", default=[], metavar="NAME", help="Pass named host variable to agent only; values never printed")
    p.add_argument("--secret-env", action="append", default=[], metavar="NAME[=SECRET]",
                   help="Give the agent the environment variable NAME with the value of the secret SECRET (default: "
                        "NAME) from sandbox-ai secrets; values never printed; repeatable")
    p.add_argument("--model", metavar="MODEL",
                   help="The agent's model: Claude Code's alias (fable, opus, sonnet, haiku) or full name, Codex's "
                        "model name, OpenCode's provider/model; each agent's known ones: sandbox-ai models (any "
                        "other is passed on as it is; default: the agent's own; with --llm the model comes from --llm "
                        "NAME:MODEL)")
    p.add_argument("--effort", metavar="LEVEL",
                   help="How hard the agent thinks: Claude Code low, medium, high, xhigh, max; Codex none, minimal "
                        "to xhigh, max, ultra (model_reasoning_effort); Hermes none to max; Qwen Code none, low to "
                        "max; Goose off, low to max; OpenHands none, low to xhigh; nanobot none, low to max; Pi off, "
                        "minimal to max (its --thinking); a known model takes only its own levels (sandbox-ai "
                        "models); OpenCode, OpenCode 2, DSH and Letta Code set it per model in their own "
                        "configuration (refused)")
    p.add_argument("--show-thinking", choices=("on", "off"),
                   help="Show the model's thinking (reasoning) in the agent's terminal: on or off, through the agent's "
                        "own switch (Claude Code showThinkingSummaries, Codex hide_agent_reasoning and "
                        "show_raw_agent_reasoning, OpenCode thinking_mode, OpenCode 2 session.thinking, Hermes "
                        "display.show_reasoning, DSH sandbox-ai's chat, Goose GOOSE_CLI_SHOW_THINKING, Pi "
                        "hideThinkingBlock, nanobot showReasoning); default: the agent's own. Refused for Qwen Code "
                        "(Ctrl+O unfolds its thinking), OpenHands (a click unfolds it) and Letta Code (Ctrl+T "
                        "unfolds it), which have no such setting")
    p.add_argument("--api-key", action="append", default=[], metavar="HOST=NAME[=SECRET]",
                   help="An API key that stays out of the sandbox: the agent gets the variable NAME with a "
                        "placeholder, the connection proxy puts the secret SECRET (default: NAME) of sandbox-ai "
                        "secrets in its place on the wire, only in a credential header (Authorization, X-API-Key, "
                        "API-Key) of an HTTPS request to HOST. Opens HOST (TCP/443); repeatable")
    p.add_argument("--publish", action="append", default=[], metavar="KIND[,KIND]",
                   help="Publish with your logins of sandbox-ai publish-logins (npm, pypi, testpypi, crates, gitlab, "
                        "github): opens that kind's upload and API hosts, gives the agent's tools their usual variables "
                        "with a placeholder, the proxy adds the token on the wire, and every request that publishes is "
                        "asked first; repeatable")
    p.add_argument("--log-dir", type=Path,
                   help="Write egress-denied.jsonl, files.jsonl and the proxy's connections-*.jsonl here (outside "
                        "workspaces; hidden from the agent). Without it the connection log goes to "
                        "~/.local/state/sandbox-ai/logs/PROJECT-HASH and file access is not logged")
    p.add_argument("--log-debug", action="store_true",
                   help="Log more of what is refused: the bodies of refused requests, the TLS ClientHello's "
                        "versions, ciphers and extensions, and the whole process tree and open sockets of the "
                        "process behind a connection (credentials stay redacted)")
    p.add_argument("--llm-traffic", choices=RUNTIME.LLM_TRAFFIC_MODES,
                   help="What the LLM traffic log (llm-traffic-*.jsonl in the log directory; sandbox-ai llm traffic) "
                        "records: llm (the default) the requests to --llm's server, all also the agent's cloud "
                        "provider (Anthropic, OpenAI: the whole conversation, after TLS inspection), off nothing; "
                        "credentials redacted; \"llm_traffic\" in ~/.config/sandbox-ai/defaults.json")
    p.add_argument("--budget", type=budget_usd, metavar="USD",
                   help="The most this run's model requests may cost (priced per token by the connection proxy: "
                        "sandbox-ai spend; not with --no-proxy); at 80 %% you are told, at 100 %% its next request to a "
                        "provider is refused (raise it: sandbox-ai spend raise RUN USD); \"budget\" in the defaults "
                        "file or the project's .sandbox-ai.json")
    p.add_argument("--llm-traffic-limit", type=int, metavar="MIB",
                   help=f"The most one request's record of the LLM traffic log holds, {LLM_TRAFFIC_LIMIT_MIB[0]} to "
                        f"{LLM_TRAFFIC_LIMIT_MIB[1]} MiB (default {RUNTIME.LLM_TRAFFIC_RECORD >> 20}; a larger one is "
                        "shortened and marked truncated); \"llm_traffic_limit\" in the defaults file")
    p.add_argument("--gui", action="store_true", help=f"Virtual X display {GUI_DISPLAY} (Xvfb) for GUI apps and headed browsers")
    p.add_argument("--gui-screen", default="1920x1080x24", metavar="WxHxDEPTH", help="Virtual screen size (default: 1920x1080x24)")
    p.add_argument("--gui-vnc", type=Path, metavar="DIR",
                   help="Implies --gui; serve the display over VNC on the Unix socket DIR/vnc.sock (outside workspaces)")
    p.add_argument("--audio", choices=("virtual",),
                   help="Sound inside the sandbox: virtual starts PulseAudio with a null sink (record what is played: "
                        "text to speech, screen videos with sound); nothing reaches your sound card or microphones. "
                        "Needs PulseAudio in the image (sandbox-ai images recipe media)")
    p.add_argument("--gui-wayland", action="store_true",
                   help="Expose the host's Wayland socket: windows appear on your desktop (weakens isolation)")
    p.add_argument("--browser", nargs="?", const="chromium", choices=("chromium", "firefox"), metavar="KIND",
                   help="Podman: a browser in its own container beside the agent, on the agent's network (its "
                        "firewall, proxy, approvals, kill switch, VPN or Tor), with a display only the desktop app's "
                        "Browser button shows (VNC on a Unix socket, no TCP): chromium (the default) or firefox. "
                        "Build its image once: sandbox-ai build browser")
    p.add_argument("--browser-control", choices=("human", "agent", "both"),
                   help="Who drives --browser: human (the default: you, in the app; the agent cannot reach it), "
                        "agent (the agent, through Chrome's DevTools Protocol at http://127.0.0.1:9222 in the pod; "
                        "Chromium only) or both")
    p.add_argument("--browser-profile", metavar="fresh|session|DIR",
                   help="The --browser's profile: fresh (the default: new at every start, gone with the run), "
                        "session (kept with the session, outside its HOME) or an absolute folder DIR of yours "
                        "(outside workspaces)")
    p.add_argument("--cairn", action="store_true",
                   help="Podman: Cairn's board (a blackboard of facts, intents and hints for several agents) in its "
                        "own container beside the agent, on the agent's network, at http://127.0.0.1:8000 in the pod; "
                        "the sandbox's browser starts on it, and the session's agents are its workers. "
                        "Build its image once: sandbox-ai build cairn")
    p.add_argument("--cairn-workers", dest="cairn_workers_text", metavar="AGENT[=MODEL],...",
                   help="Which of the session's agents work for --cairn: any harness, the lead or --with (default: "
                        "each of them the session has, the best supported first), each with a model of its own if "
                        "given; how far each is supported: sandbox-ai cairn workers")
    p.add_argument("--cairn-origin", metavar="TEXT",
                   help="Cairn's project: what is given, the target and the scope (with --cairn-goal the project is "
                        "made at the start)")
    p.add_argument("--cairn-goal", metavar="TEXT", help="Cairn's project: what counts as done")
    p.add_argument("--cairn-hint", metavar="TEXT", help="Cairn's project: the first hint for its workers")
    p.add_argument("--cairn-board", choices=("fresh", "session"),
                   help="Cairn's database: fresh (the default: new at every start) or session (kept with the session)")
    p.add_argument("--artex", action="store_true",
                   help="Podman: ARTEX (an AI penetration-testing platform, AGPL-3.0) in its own container beside the "
                        "agent, on the agent's network: its targets only what the allow-list opens; its console "
                        "forwarded to this computer's 127.0.0.1 (sandbox-ai artex console: address and password). "
                        "Build its image once: sandbox-ai build artex")
    p.add_argument("--artex-data", choices=("fresh", "session"),
                   help="ARTEX's data: fresh (the default: new at every start) or session (kept with the session)")
    p.add_argument("--artex-port", metavar="PORT",
                   help="The port of this computer's 127.0.0.1 that --artex's console is forwarded to (default: 8787, "
                        "or the next free one after it)")
    p.add_argument("--tmux", action="store_const", const=True, default=None,
                   help="Run in tmux inside the sandbox: window 'agent' plus window 'shell' (bash). The default "
                        "whenever stdin and stdout are a terminal and the agent runs interactively; --tmux insists "
                        "(and fails without a terminal)")
    p.add_argument("--no-tmux", action="store_const", const=False, dest="tmux", help="Run the agent without tmux")
    p.add_argument("--mcp", action="append", default=[], metavar="NAME",
                   help="Connect a local MCP server running in its own sandbox; started if not running "
                        "(see sandbox-ai mcp list; built in: demo)")
    p.add_argument("--mcp-limits", action="append", default=[], metavar="NAME:memory=SIZE,cpus=N,pids=N",
                   help="Other limits for the --mcp server NAME in this start (each key optional; the server's own "
                        "are the default, sandbox-ai mcp list): it gets an instance of its own with them, shared with "
                        "the starts that ask for the same; repeatable")
    p.add_argument("--mcp-mount", action="append", default=[], metavar="NAME:TARGET=HOST[:ro|rw]",
                   help="Another host folder for the --mcp image server NAME in this start, at a TARGET its "
                        "definition mounts already (sandbox-ai mcp list), e.g. engram:/data=~/brains/app:rw - one "
                        "memory per project. Never a target it has not, never a read-only one writable; a writable "
                        "folder that is not there is created; none inside a workspace. It gets an instance of its own, "
                        "shared with the starts that give the same folders; repeatable")
    p.add_argument("--mcp-env", action="append", default=[], metavar="NAME:VAR=VALUE|NAME:VAR@SECRET",
                   help="A variable of the --mcp image server NAME for this start: VAR=VALUE a value that is no secret "
                        "(e.g. whatweb:ALLOW_PRIVATE_TARGETS=1; nothing of a secret's shape or name), VAR@SECRET the "
                        "secret SECRET of sandbox-ai secrets; in place of what its definition says of VAR (sandbox-ai "
                        "mcp list). It gets an instance of its own, shared with the starts that give the same; "
                        "repeatable")
    p.add_argument("--helper", action="append", default=[], choices=sorted(AUTH_TARGETS), metavar="AGENT",
                   help="Another agent (claude, codex, opencode, opencode2, hermes, dsh, qwen, goose, openhands, nanobot, letta, pi) as this agent's helper: it runs as an "
                        "MCP server in "
                        "a sandbox of its own (its login, its provider's hosts, kits, rules and hooks) on this run's "
                        "main workspace, and this agent gives it tasks with its tool ask")
    p.add_argument("--session", metavar="NAME",
                   help="A kept, named session (the older form of --name NAME): a stopped one of that name comes back; manage "
                        "with sandbox-ai list|attach|pause|resume|stop|delete")
    p.add_argument("--name", metavar="NAME",
                   help="Name of this start (default: a codename such as brisk-otter-lantern, see --name-style); "
                        "sandbox-ai list shows it, rename changes it, and the other commands take it")
    p.add_argument("--name-style", choices=NAME_STYLES, metavar="STYLE",
                   help="How a start without --name is named: codename (three words such as brisk-otter-lantern) or "
                        "project (AGENT-PROJECT-xxxx); default: \"name_style\" in "
                        "~/.config/sandbox-ai/defaults.json, else codename")
    p.add_argument("--project-name", metavar="NAME",
                   help="The PROJECT part of the names of --name-style project, AGENT-PROJECT-xxxx (default: the "
                        "main workspace's folder name; the project file's \"name\")")
    p.add_argument("--keep", action="store_const", const=True, default=None,
                   help="Keep the sandbox HOME, the options and logs when the start ends, so that sandbox-ai resume "
                        "NAME can bring it back (the default)")
    p.add_argument("--no-keep", action="store_const", const=False, dest="keep",
                   help="Discard the HOME when the start ends: nothing carries over to a next start (suits scripts)")
    # Internal: set by the session supervisor, not meant for direct use.
    p.add_argument("--start-id", help=argparse.SUPPRESS)
    p.add_argument("--tmux-socket-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--pod-name", help=argparse.SUPPRESS)
    p.add_argument("--mcp-socket-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--helper-of", metavar="NAME", help=argparse.SUPPRESS)  # this MCP server sandbox is helper NAME
    # This MCP server's (or helper's) sandbox reaches the network through the tunnel the run RUN lends it (its start's
    # --vpn: vpn.lends), with no tunnel of its own (mcp.server_network gives it).
    p.add_argument("--vpn-lent-by", metavar="RUN", help=argparse.SUPPRESS)
    # Every agent: the team image holds Claude Code, Codex and OpenCode, the others as its layers (team_image.py).
    p.add_argument("--with", dest="companions", action="extend", default=[], type=companion_list,
                   metavar="AGENT[,AGENT...]",
                   help="Other agents in the same sandbox (any of them; a comma list or repeated), for this agent to call from "
                        "the shell in its one-shot mode (claude -p, codex exec, opencode run, hermes --oneshot, qwen "
                        "--prompt, goose run, ...; the sandbox-ai skill names each): its login, its provider's "
                        "hosts, its managed configuration (MCP servers, telemetry off) and its rules join this run's; "
                        "needs the team image with it (sandbox-ai build team [--with AGENT,...]), which the start "
                        "builds when it is missing")
    p.add_argument("--mcp-approvals-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--hermes-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_hermes_module().help_text())
    p.add_argument("--qwen-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_qwen_module().help_text())
    p.add_argument("--goose-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_goose_module().help_text())
    p.add_argument("--openhands-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_openhands_module().help_text())
    p.add_argument("--nanobot-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_nanobot_module().help_text())
    p.add_argument("--letta-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_letta_module().help_text())
    p.add_argument("--opencode2-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_opencode2_module().help_text())
    p.add_argument("--pi-enable", action="append", default=[], metavar="FEATURE[,FEATURE...]",
                   help=load_pi_module().help_text())
    p.add_argument("--config", type=Path, metavar="FILE",
                   help=f"Project configuration (default: {CONFIG_NAME} in the main workspace, if trusted)")
    p.add_argument("--no-config", action="store_true", help=f"Ignore {CONFIG_NAME}")
    p.add_argument("--dry-run", action="store_true", help="Print plan; no DNS, auth reads, copies, pods or processes")
    p.add_argument("--fix", action="store_true",
                   help="What this start lacks and sandbox-ai can provide - its image not built, an MCP server not "
                        "added, a toolchain not in the store, an identity - is done first, without asking, then it "
                        "starts (for scripts; on a terminal a start asks: your default fix_questions, all or each). "
                        "Never what the security mode refuses (in high and paranoid nothing unsigned), never a "
                        "command that needs root. Not kept by a session, not allowed in a project configuration")
    p.add_argument("--fix-only", action=ListOption, default=[], metavar="ID",
                   help="As --fix, but exactly these fixes (their ids as --fix-plan names them, image:claude, "
                        f"mcp:jadx); what else the start lacks is refused; {LIST_HELP}")
    p.add_argument("--fix-plan", action="store_true",
                   help="Print as JSON what this start lacks and what would fix each (its commands, how long, "
                        "unsigned or security-relevant, what the security mode does not take); nothing is made or "
                        "started. What the app and the web interface ask about before a start")
    p.add_argument("--probe", action="store_true", help="Verify both sandbox layers and firewall, then exit without agent")
    group_help(p, agent)
    return p


# Options that have a documented name and an older one: {documented: the name every reader of saved options knows}.
# canonical_options writes the older one, so project files, presets, kept starts, MCP definitions, board bundles and
# older hosts read them as before; both are accepted everywhere (argparse: one action, two names).
OPTION_ALIASES = {"--allow-net": "--allow"}


def long_option(action):
    """The long name of an argparse action (its first --option; an alias's: the name OPTION_ALIASES keeps)."""
    name = next((name for name in action.option_strings if name.startswith("--")), action.option_strings[0])
    return OPTION_ALIASES.get(name, name)


def bare_optional(action, tokens, index):
    """Whether ACTION, an option with an optional value (--browser [KIND]), is given without one: TOKENS[INDEX] (the
    next word) is none, an option or "--". Such an option is written --NAME=CONST by canonical_options, so that
    the code that walks options (project files, modes, the app) never takes the next option for its value."""
    return action.nargs == "?" and (index >= len(tokens) or str(tokens[index]).startswith("-"))


def canonical_options(tokens):
    """TOKENS (options as on the command line, up to an optional --) with every short form written as its
    long option, a value attached to a one-letter form (-w/path) split off and bundled flags (-Dn) taken
    apart. Code that looks for options by name (project files, MCP definitions, merging) sees only
    long names; argparse reads both forms the same way."""
    actions = option_actions()
    result, index, value_next = [], 0, False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if value_next or token == "--":
            result += [token] if value_next else tokens[index - 1:]
            if not value_next:
                break
            value_next = False
            continue
        if token.startswith("--") or not token.startswith("-") or token == "-":
            name = token.split("=", 1)[0]
            if name in OPTION_ALIASES:  # --allow-net X: --allow X (its older name, which every reader knows)
                token = OPTION_ALIASES[name] + token[len(name):]
                name = OPTION_ALIASES[name]
            if name in actions and "=" not in token and bare_optional(actions[name], tokens, index):
                result.append(f"{name}={actions[name].const}")  # --browser alone: --browser=chromium
                continue
            result.append(token)
            value_next = name in actions and actions[name].nargs != 0 and "=" not in token
            continue
        action = actions.get(token)
        if action is not None:  # an exact short form: -w, -gl
            if bare_optional(action, tokens, index):
                result.append(f"{long_option(action)}={action.const}")
                continue
            result.append(long_option(action))
            value_next = action.nargs != 0
            continue
        name, equals, value = token.partition("=")
        if equals and name in actions and actions[name].nargs != 0:  # -w=/dir, -pr=/rules (as argparse reads them)
            result += [long_option(actions[name]), value]
            continue
        expanded, rest = [], token[1:]
        while rest:
            action = actions.get("-" + rest[0])
            if action is None:
                expanded = None
                break
            if action.nargs == 0:
                expanded.append(long_option(action))
                rest = rest[1:]
            elif rest[1:]:
                expanded += [long_option(action), rest[1:]]
                rest = ""
            else:  # -Dw DIR: the bundle's last option takes the next word
                expanded.append(long_option(action))
                value_next, rest = True, ""
        result += [token] if expanded is None else expanded
    return spread_lists(result)


def logical_cwd():
    """The working directory as the shell names it ($PWD, symlinks kept), so that a symlink on the way is seen."""
    pwd = os.environ.get("PWD", "")
    cwd = os.getcwd()
    try:
        if os.path.isabs(pwd) and os.path.samefile(pwd, cwd):
            return os.path.normpath(pwd)
    except OSError:
        pass
    return cwd


BACKENDS = ("podman", "bwrap")
POLICY_FILE = Path("/etc/sandbox-ai/policy.json")
POLICY_KEYS = {"version", "forbid", "backends", "max_approval_scope", "allow_within", "allow_hosts", "mcp_network",
               "subnet", "subnet6", "subnet_within", "require_vpn", "require_signed_updates", "message", "rules",
               "min_mode", "require_canaries", "forbid_keep", "lsm", "forbid_shared_wildcards"}
APPROVAL_SCOPES = ("once", "run", "always")


def load_policy(path=None):
    """The administrator's policy ({} without one): options nobody may use, allowed backends, the widest
    approval scope. Only a file root owns and only root can change counts: a policy the user could edit
    would not be one. A broken or unsafe policy file stops every start (fail closed)."""
    path = Path(path or POLICY_FILE)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"Policy {path}: {exc.strerror}") from None
    for item, label in ((info, str(path)), (os.stat(path.parent), str(path.parent))):
        if item.st_uid != 0 or item.st_mode & 0o022:
            raise ValueError(f"Policy {label} must be owned by root and writable by root only")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Policy {path} must be a regular file")
    try:
        policy = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"Policy {path} is not valid JSON: {exc}") from None
    if not isinstance(policy, dict) or policy.get("version") != 1 or set(policy) - POLICY_KEYS:
        raise ValueError(f"Policy {path}: version 1 with the keys {sorted(POLICY_KEYS)}")
    forbid, backends = policy.get("forbid", []), policy.get("backends", list(BACKENDS))
    if not isinstance(forbid, list) or not all(isinstance(v, str) and v.startswith("--") for v in forbid):
        raise ValueError(f"Policy {path}: forbid is a list of options such as \"--no-proxy\" or \"--ask=off\"")
    if not isinstance(backends, list) or not backends or set(backends) - set(BACKENDS):
        raise ValueError(f"Policy {path}: backends is a non-empty list of {list(BACKENDS)}")
    if policy.get("max_approval_scope", "always") not in APPROVAL_SCOPES:
        raise ValueError(f"Policy {path}: max_approval_scope is one of {list(APPROVAL_SCOPES)}")
    if "min_mode" in policy:
        MODES.check_name(policy["min_mode"], f"Policy {path}: min_mode")
    names = policy.get("allow_hosts", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and re.fullmatch(r"(\*\.)?[a-z0-9.-]{1,253}", n)
                                              for n in names):
        raise ValueError(f"Policy {path}: allow_hosts is a list of host names or *.DOMAIN")
    if policy.get("mcp_network", "full") not in ("none", "full"):
        raise ValueError(f"Policy {path}: mcp_network is none or full")
    if not load_vpn_module().policy_value_ok(policy.get("require_vpn", False)):
        raise ValueError(f"Policy {path}: require_vpn is true, false or a list of VPN profile names")
    if not load_canary_module().check_policy_value(policy.get("require_canaries", False)):
        raise ValueError(f"Policy {path}: require_canaries is true, false or a list of canary categories "
                         "(credentials, home, workspace)")
    if not isinstance(policy.get("forbid_shared_wildcards", False), bool):
        raise ValueError(f"Policy {path}: forbid_shared_wildcards is true or false (no host wildcard below a shared "
                         "service such as *.blob.core.windows.net, confirmed or not)")
    if not isinstance(policy.get("forbid_keep", False), bool):
        raise ValueError(f"Policy {path}: forbid_keep is true or false (every start discards its HOME at the end)")
    if "lsm" in policy and not lsm_value_ok(policy["lsm"]):
        raise ValueError(f"Policy {path}: lsm is {{\"apparmor\": \"PROFILE\"}} or {{\"selinux\": \"TYPE\"}}: the "
                         "confinement every sandbox's container is started in (a profile loaded on this host, a type "
                         "of its SELinux policy)")
    if not isinstance(policy.get("require_signed_updates", False), bool):
        raise ValueError(f"Policy {path}: require_signed_updates is true or false (install.sh, sandbox-ai update)")
    within = policy.get("allow_within")
    if within is not None:
        try:
            if not isinstance(within, list):
                raise TypeError
            [ipaddress.ip_network(n, strict=False) for n in within]
        except (TypeError, ValueError):
            raise ValueError(f"Policy {path}: allow_within is a list of networks such as \"10.0.0.0/8\"") from None
    for key, version in (("subnet", 4), ("subnet6", 6)):
        if key in policy:
            try:
                parse_subnet(policy[key], version, key)
            except ValueError as exc:
                raise ValueError(f"Policy {path}: {exc}") from None
    ceiling = policy.get("subnet_within")
    if ceiling is not None:
        try:
            if not isinstance(ceiling, list) or not ceiling or not all(isinstance(n, str) for n in ceiling):
                raise TypeError
            ceiling = [ipaddress.ip_network(n, strict=False) for n in ceiling]
        except (TypeError, ValueError):
            raise ValueError(f"Policy {path}: subnet_within is a list of networks such as \"10.200.0.0/16\"") from None
        for key, version in (("subnet", 4), ("subnet6", 6)):
            limits = [n for n in ceiling if n.version == version]
            # Without a default inside, Podman would take the host's address, which no ceiling holds.
            if limits and key not in policy:
                raise ValueError(f"Policy {path}: subnet_within with IPv{version} networks needs {key}, the "
                                 "default inside them")
            if limits and not any(ipaddress.ip_network(policy[key]).subnet_of(n) for n in limits):
                raise ValueError(f"Policy {path}: {key} {policy[key]} lies outside subnet_within")
    known = option_actions()
    for value in forbid:
        if value.split("=", 1)[0] not in known:
            raise ValueError(f"Policy {path}: unknown option {value}")
    if "rules" in policy:
        try:
            RUNTIME.Ruleset.validated(policy["rules"], f"Policy {path}: rules")
        except RUNTIME.RuleError as exc:
            raise ValueError(str(exc)) from None
    return policy


def check_policy(a, policy, path=None):
    """Refuse what the policy forbids, however it was chosen: on the command line, in a trusted project file,
    through a profile (--profile sets --egress-all-allow, for example) or a short form."""
    a.max_approval_scope = policy.get("max_approval_scope", "always")
    a.policy = {}
    a.policy_rules = policy.get("rules")  # the first ruleset of every run (see load_rulesets)
    a.require_canaries = policy.get("require_canaries")  # canary.categories() applies it
    a.forbid_keep = policy.get("forbid_keep") is True
    a.policy_lsm = policy.get("lsm")  # lsm_options, isolation_config: the supervisor refuses to run without it
    if not policy:
        return
    note = f" ({policy['message']})" if isinstance(policy.get("message"), str) else ""
    where = path or POLICY_FILE
    if a.forbid_keep and a.session is not None and not (a.tmux_socket_dir or a.mcp_socket_dir):
        raise ValueError(f"--session keeps the sandbox HOME, which the administrator's policy forbids (forbid_keep in "
                         f"{where}){note}; start without --session (--name NAME names the start): its HOME is "
                         "discarded at the end")
    # A HOME of your own outlives the run just as well (sandbox-ai's own sandboxes, a session's supervisor and
    # an MCP server, get theirs from it: not chosen by anyone).
    if a.forbid_keep and a.state_dir and not (a.tmux_socket_dir or a.mcp_socket_dir):
        raise ValueError(f"--state-dir keeps the sandbox HOME beyond the run, which the administrator's policy forbids "
                         f"(forbid_keep in {where}){note}; start without it: the HOME is discarded at the end")
    actions = option_actions()
    for entry in policy.get("forbid", []):
        option, _, wanted = entry.partition("=")
        action = actions[option]
        value = getattr(a, action.dest, None)
        if wanted:
            used = (wanted in [str(v) for v in value]) if isinstance(value, list) else str(value) == wanted
        elif option == "--dns":
            # --nameserver and --egress-all-allow imply it: DNS leaves either way.
            used = bool(a.dns or a.nameserver or a.egress_all_allow)
        elif option == "--image":
            # parse_args fills in the default image (the team image with --with); only another one counts (a
            # variant is another one).
            held = team_image_agents(value) if companions(a) else None
            used = value != f"localhost/{a.agent}-sandbox:latest" and not (held and set(team(a)) <= set(held))
        elif option == "--mcp":
            used = bool(a.mcp or a.helper)  # a helper is an MCP server with its agent's network
        elif action.nargs == 0 and isinstance(value, list):
            # --allow-pypi, --allow-github ...: one shared list, each option adds its own value.
            used = action.const in value
        elif action.nargs == 0:
            # store_true, and store_const such as --no-tmux (False, against the default None).
            used = value == action.const and value != action.default
        else:
            used = value not in (None, [], action.default)
        if used:
            origin = getattr(a, "vpn_origin", None) if action.dest == "vpn" else None  # --llm's VPN (llm.py)
            # Not chosen by the options, while the security mode added some: the mode's (modes.py).
            by_mode = bool(getattr(a, "mode_options", [])) and action.dest not in getattr(a, "mode_given", [])
            raise ValueError(f"{entry} is not allowed by the administrator's policy ({where}){note}"
                             + (f"; {origin} is reached through it" if origin else "")
                             + (f"; the security mode {a.mode} ({a.mode_origin}) sets it, choose a stricter one "
                                "with --mode" if by_mode else ""))
    if a.backend not in policy.get("backends", list(BACKENDS)):
        raise ValueError(f"The backend {a.backend} is not allowed by the administrator's policy ({where}){note}")
    if policy.get("require_vpn"):
        load_vpn_module().check_policy(a, policy, f" ({where}){note}")
    a.policy = {key: policy[key] for key in ("forbid", "allow_within", "allow_hosts") if key in policy}
    if policy.get("forbid_shared_wildcards") is True or "--allow-shared-wildcard" in policy.get("forbid", []):
        # Every way to one: --allow-shared-wildcard, --allow-host '*.DOMAIN' with its question or --yes, a project
        # file, and `sandbox-ai net allow` (the supervisor holds it as the forbidden --allow-shared-wildcard).
        for host in dict.fromkeys(h for h in a.allow_host if h.startswith("*.")):
            try:
                shared = RUNTIME.shared_wildcard(host)
            except ValueError:
                continue  # validate() says why
            if shared:
                raise ValueError(f"--allow-host {host} reaches {shared}, a shared service where anyone can rent a "
                                 "name, which the administrator's policy forbids (forbid_shared_wildcards in "
                                 f"{where}){note}; give the hosts by name instead")
        a.policy["forbid"] = sorted({*a.policy.get("forbid", []), "--allow-shared-wildcard"})
    check_ceiling(a, f" ({where}){note}")
    # After the forbid check: the policy's own default is no choice of the user's.
    apply_subnet_defaults(a, policy, f"the administrator's policy {where}")
    check_subnet_ceiling(a, policy, f" ({where}){note}")


def check_subnet_ceiling(a, policy, where=""):
    """The sandbox's own network lies inside the policy's subnet_within, for each IP version it names: a chosen
    --subnet/--subnet6, else what the backend uses (slirp4netns: 10.0.2.0/24 and always fd00::/64; pasta: the
    host's address, which is refused). --network leaves it to the Podman network (forbid --network for that)."""
    ceiling = policy.get("subnet_within")
    if ceiling is None or a.offline or a.network:
        return
    limits = [ipaddress.ip_network(n, strict=False) for n in ceiling]
    for key, version in (("subnet", 4), ("subnet6", 6)):
        mine = [n for n in limits if n.version == version]
        if not mine:
            continue
        bwrap = SLIRP_SUBNETS[version == 6] if a.backend == "bwrap" else None
        value = bwrap if version == 6 and bwrap else getattr(a, key) or bwrap
        if value is None:
            raise ValueError(f"The administrator's policy keeps the sandbox's own IPv{version} network inside "
                             f"{', '.join(map(str, mine))}; choose one with --{key}{where}")
        label = subnet_label(a, key) if value == getattr(a, key) else f"slirp4netns's {value}"
        net = parse_subnet(value, version, label)
        if not any(net.subnet_of(n) for n in mine):
            raise ValueError(f"{label} lies outside the networks the administrator's policy allows for the "
                             f"sandbox's own network (subnet_within: {', '.join(map(str, mine))}){where}")


def within_ceiling(a, network=None, host=None):
    """NETWORK (an address or CIDR) or HOST (a name) lies inside the administrator's ceiling (allow_within for
    addresses, allow_hosts for names), or there is no ceiling."""
    policy = getattr(a, "policy", {}) or {}
    if policy.get("allow_within") is None:
        return True
    if host is not None:
        return any(host == name or (name.startswith("*.") and host.endswith(name[1:])) for name in
                   policy.get("allow_hosts", []))
    network = ipaddress.ip_network(network, strict=False)
    return any(network.version == limit.version and network.subnet_of(limit)
               for limit in map(lambda n: ipaddress.ip_network(n, strict=False), policy["allow_within"]))


def check_ceiling(a, where=""):
    """Every way of widening at the start stays inside the ceiling: --allow, --allow-tcp, named hosts (--allow-host,
    presets, --frontier; the agent's own provider is always reachable), DNS to outside resolvers (data leaves
    through any of them), host ports (127.0.0.1), and --egress-all-allow."""
    if (getattr(a, "policy", {}) or {}).get("allow_within") is None:
        return
    if a.egress_all_allow:
        raise ValueError(f"--egress-all-allow reaches beyond the administrator's networks{where}")
    wanted = [str(ipaddress.ip_network(n, strict=False)) for n in a.allow]
    wanted += [address for address, _ in tcp_targets(a)]
    wanted += ["127.0.0.1"] if a.allow_host_port else []
    wanted += nameservers(a)
    for network in wanted:
        if not within_ceiling(a, network=network):
            raise ValueError(f"{network} lies outside the networks the administrator's policy allows{where}")
    own = set(PROVIDERS.get(a.agent, []))
    names = list(a.allow_host) + [h for preset in a.presets for h in PRESETS[preset]] + (FRONTIER if a.frontier else [])
    for host in dict.fromkeys(names):
        if host not in own and not within_ceiling(a, host=host):
            raise ValueError(f"{host} is not among the hosts the administrator's policy allows (allow_hosts){where}")


OBSOLETE_TOR_VARIANT = "tor"  # the variant of the gone recipe tor: not needed, Tor is the VPN


def check_image_variant(a):
    """--image-variant names a variant of this agent from sandbox-ai images (it fixes the image's name only:
    nothing is read from the variant at the start), for Podman, and replaces --image."""
    if not a.image_variant:
        return
    spec = importlib.util.spec_from_file_location("images_for_launcher", Path(__file__).resolve().parent / "images.py")
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    own = f"localhost/{a.agent}-sandbox:latest"
    named = f"localhost/{a.agent}-sandbox-{a.image_variant}:latest"
    if a.image_variant == OBSOLETE_TOR_VARIANT and (a.image_variant, a.agent) not in images.variants():
        # The recipe tor (Tor Browser, torsocks in the agent's image) is gone since 0.1.95: Tor is the VPN beside the
        # agent. A preset or board made with another agent's variant tor starts this agent's own image.
        print(f"agent-sandbox: --image-variant tor: no variant tor of {a.agent}; not needed - Tor is the VPN (--vpn), "
              f"so this start uses the agent's own image {own}", file=sys.stderr)
        a.image_variant = None
        if a.image == named:
            a.image = own
        return
    before = {name for name, agent in images.variants() if agent == a.agent}
    try:
        wanted = images.variant_image(a.agent, a.image_variant, adopt=True)
    except ValueError as exc:
        if not images.valid_name(a.image_variant) or a.backend != "podman":
            raise
        # A variant that is not there: one sandbox-ai has a recipe for is made from it and built (offered); another
        # is yours to define (what goes into it is your choice).
        name, lacking = a.image_variant, f"the image variant {a.image_variant} of {a.agent} is not there"
        target = images.shown_target(name, a.agent, images.variants() + [(name, a.agent)])
        if name in images.RECIPES:
            item = FIXES.fix("variant", name, lacking, "make it from its recipe and build it",
                             [["sandbox-ai", "images", "recipe", name, "--from", a.agent],
                              images.build_command(target).split()], duration="some minutes")
        else:
            item = FIXES.fix("variant", name, lacking, "define it, then build it",
                             [["sandbox-ai", "images", "new", name, "--from", a.agent],
                              images.build_command(target).split()],
                             manual="sandbox-ai has no recipe of that name: what goes into the variant is yours to "
                                    "say (sandbox-ai images --help)")
        raise FIXES.Missing(str(exc), [item]) from None
    if a.image_variant not in before:
        print(f"agent-sandbox: made the variant {a.image_variant} of {a.agent} from "
              f"{images.adopted_from(a.image_variant, a.agent)}'s (the same packages and files; its image is built "
              "at the start)", file=sys.stderr)
    if a.image != wanted:
        raise ValueError("--image and --image-variant exclude each other")
    if a.backend != "podman":
        raise ValueError("--image-variant applies only to the Podman backend")
    # A variant on a base image of yours (sandbox-ai images new NAME --base IMAGE): said at every start, unchecked.
    a.image_base = images.base_summary(f"{a.image_variant}@{a.agent}")


def variant_base(a):
    """The name of your image (sandbox-ai images pull|build-image) that --image-variant's variant is built on, or None
    (no variant, or one on the agent's own base). Read from the variant's file only: nothing is checked here."""
    name = getattr(a, "image_variant", None)
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,29}", str(name)):
        return None
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "images"
    # The agent's own variant of that name (images.variant_dir); one of before 0.1.95's fixes where it was left.
    path = root / "variants" / str(a.agent) / name / "image.json"
    if not path.exists() and not path.is_symlink():
        path = root / name / "image.json"
    try:
        document = json.loads(read_config_bytes(path, CONFIG_LIMIT))
    except (OSError, ValueError, RecursionError):
        return None
    base = document.get("base") if isinstance(document, dict) else None
    return base if isinstance(base, str) and base else None


def load_services_module():
    """services.py: your images from registries and Containerfiles, and the services (--service) of a start."""
    spec = importlib.util.spec_from_file_location("sandbox_services", HERE / "services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_browser_module():
    """browser.py: the sandbox's own browser (--browser), a container of sandbox-ai's browser image in the pod."""
    spec = importlib.util.spec_from_file_location("sandbox_browser", HERE / "browser.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_toolchains_module = None


def load_toolchains_module():
    """toolchains.py: build toolchains mounted read-only at /opt/toolchains/NAME (--toolchain, sandbox-ai toolchain)."""
    global _toolchains_module
    if _toolchains_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_toolchains", HERE / "toolchains.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _toolchains_module = module
    return _toolchains_module


def own_image(a):
    """The run's image is one of sandbox-ai's own (Debian bookworm, what a toolchain's needs are checked against): not
    an image you named (--image) and not a variant built on a base image of yours."""
    image = str(getattr(a, "image", "") or "")
    return bool(re.fullmatch(r"localhost/[a-z0-9-]+-sandbox(?:-[a-z0-9_.-]+)?:latest", image)) and \
        not variant_base(a)


def plan_toolchains(a):
    """--toolchain: a.toolchain_plan (toolchains.start_plan: the installed entries, checked against what this backend
    runs them on), and the registries they name join --allow-host and the --allow-PRESET options - not in the modes
    high and paranoid, which refuse every option that opens a host (the toolchains are mounted, their registries stay
    closed, said in a note), and not with --toolchain-offline or --offline. The notes to say."""
    a.toolchain_plan = None
    if not getattr(a, "toolchain", None):
        return []
    if a.podman_url:
        raise ValueError("--toolchain mounts a folder of this host's store, which another Podman service "
                         "(--podman-url) does not have")
    try:
        plan = load_toolchains_module().start_plan(
            a.toolchain, a.backend, own_image=own_image(a), strict=strict_mode(a),
            offline=bool(getattr(a, "toolchain_offline", False) or a.offline), mode=getattr(a, "mode", None),
            presets=tuple(PRESETS), policy_refuses=lambda kind, value: policy_refuses_registry(a, kind, value))
    except ValueError as exc:
        # One that is not in the store: offered to be added from the signed catalog (fixes.py), not only refused.
        lacking = load_toolchains_module().missing(a.toolchain)
        if not lacking:
            raise
        raise FIXES.Missing(str(exc), [toolchain_fix(value) for value in lacking]) from None
    a.allow_host = list(dict.fromkeys([*a.allow_host, *plan["hosts"]]))
    a.presets = list(dict.fromkeys([*a.presets, *plan["presets"]]))
    a.toolchain_plan = plan
    mounted = ", ".join(f"{entry['name']} {entry['version']} ({entry['sha256'][:12]})" for entry in plan["entries"])
    opened = ", ".join([*plan["hosts"], *(f"--allow-{preset}" for preset in plan["presets"])])
    LOG.info("toolchains", f"mounted read-only: {mounted}; registries opened: {opened or 'none'}"
                           + (f"; left closed: {', '.join(plan['closed'])}" if plan["closed"] else ""))
    return plan["notes"]


def policy_refuses_registry(a, kind, value):
    """Whether the administrator's policy (a.policy, as check_policy kept it) does not take a toolchain's registry:
    KIND "host" (a name) or "preset" (NAME of --allow-NAME). The toolchains are planned after check_policy, so what
    their manifests open is held to the same rules as the options it checked: the forbidden --allow-host (or one
    value of it), --allow-PRESET, --allow-shared-wildcard, and the ceiling of names (allow_hosts)."""
    forbid = set((getattr(a, "policy", {}) or {}).get("forbid", []))
    if kind == "preset":
        return f"--allow-{value}" in forbid or not all(within_ceiling(a, host=host) for host in PRESETS.get(value, ()))
    if "--allow-host" in forbid or f"--allow-host={value}" in forbid or not within_ceiling(a, host=value):
        return True
    if value.startswith("*.") and "--allow-shared-wildcard" in forbid:
        try:
            return bool(RUNTIME.shared_wildcard(value))
        except ValueError:
            return True
    return False


def toolchain_fix(value):
    """The fix for the toolchain VALUE (NAME[@VERSION]) that is not in the store: added from the signed catalog (its
    archive downloaded, its hash and signature checked - `toolchain add` refuses anything else)."""
    recipes = load_toolchains_module().module("toolchain_recipes")
    terms = recipes.TERMS.get(recipes.LICENSED.get(str(value).partition("@")[0]))
    if terms:
        # Built on this machine from its publisher's downloads, under a license: yours to accept - a question of
        # its own (No first), never done by --fix, refused in high and paranoid; its yes is the acceptance.
        return FIXES.fix("toolchain", value, f"the toolchain {value} is not in the store",
                         f"accept the {terms['title']} and build it here from {terms['by']}'s downloads (the signed "
                         "catalog's recipe)", [["sandbox-ai", "toolchain", "add", value, terms["option"]]],
                         duration="a download of about 1 GB",
                         security=f"by this you accept the {terms['title']} ({terms['url']}): {terms['says']}",
                         choice=f"license:toolchain:{value}", terms=recipes.LICENSED[str(value).partition("@")[0]])
    return FIXES.fix("toolchain", value, f"the toolchain {value} is not in the store",
                     "add it from the signed catalog", [["sandbox-ai", "toolchain", "add", value]],
                     duration="a download")


def toolchain_entries(a):
    plan = getattr(a, "toolchain_plan", None)
    return plan["entries"] if plan else []


def load_cairn_module():
    """cairn.py: Cairn's board (--cairn), a container of sandbox-ai's Cairn image in the pod; its workers the
    session's agents through cairn_relay.py."""
    spec = importlib.util.spec_from_file_location("sandbox_cairn", HERE / "cairn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_artex_module():
    """artex.py: ARTEX (--artex), a container of sandbox-ai's ARTEX image in the pod; its console forwarded to this
    computer's loopback."""
    spec = importlib.util.spec_from_file_location("sandbox_artex", HERE / "artex.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_artex(a):
    """--artex and its options (artex.check: the Podman backend, this machine's engine, its ports free, its data);
    a.artex_container gets its container, None without one."""
    a.artex_container = None
    return load_artex_module().check(a, launcher_namespace())


def check_cairn(a):
    """--cairn and its options (cairn.check: the Podman backend, this machine's engine, workers of the session's
    agents, the project's texts, its port free); a.cairn_container gets its container, None without one. Before the
    browser's check: --cairn brings the browser its board opens in."""
    a.cairn_container = None
    return load_cairn_module().check(a, launcher_namespace())


def check_browser(a):
    """--browser, --browser-control and --browser-profile (browser.check: the Podman backend, this machine's engine,
    Chromium for the agent's control, the live view on Linux, a profile folder outside the agent's reach);
    a.browser_container gets its container, None without one."""
    a.browser_container = None
    if not (getattr(a, "browser", None) or getattr(a, "browser_control", None) or
            getattr(a, "browser_profile", None)):
        return None
    return load_browser_module().check(a, launcher_namespace())


def check_services(a):
    """--service: each service checked (services.check: the Podman backend, no remote engine, not behind Tor, its
    image recorded, a user and ports the firewall's checks hold for); a.services gets them."""
    a.services = []
    if not getattr(a, "service", None):
        return []
    return load_services_module().check(a, launcher_namespace())


def exposed_registry():
    """Host trees sandboxes could write in earlier runs (see remember_exposed)."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "exposed.json"


def exposed_before():
    """The registry's trees; on first use it is seeded with what earlier versions exposed (see seed_exposed)."""
    path = exposed_registry()
    if not path.exists() and not path.is_symlink():
        return seed_exposed()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return []
    return [Path(p) for p in data if isinstance(p, str) and os.path.isabs(p)] if isinstance(data, list) else []


def seed_exposed():
    """Workspaces known from before the registry existed: projects with approvals or a trusted configuration,
    sessions and MCP servers. Their symlinks may have been planted by a sandbox of an older version."""
    found = []
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"
    for name, project in (("approved.json", lambda key: key), ("trusted.json", lambda key: os.path.dirname(key))):
        try:
            data = json.loads((config / name).read_text())
        except (OSError, ValueError, RecursionError):
            continue
        found += [project(key) for key in data if isinstance(key, str)] if isinstance(data, dict) else []
    found += session_workspaces()
    try:
        servers = json.loads((config / "mcp.json").read_text())
    except (OSError, ValueError, RecursionError):
        servers = {}
    for definition in (servers.get("servers", servers) if isinstance(servers, dict) else {}).values():
        if not isinstance(definition, dict):
            continue
        found += [definition["workspace"]] if isinstance(definition.get("workspace"), str) else []
        found += [m["host"] for m in definition.get("mounts", []) if isinstance(m, dict) and isinstance(m.get("host"), str)
                  and m.get("mode") == "rw"]
    found = [os.path.expanduser(p) for p in found]
    return [Path(p) for p in dict.fromkeys(found) if os.path.isabs(p)]


def session_workspaces():
    """The workspaces of the kept sessions (session.json: the main one and those of its -w options), as recorded."""
    found = []
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"
    for record in state.glob("sessions/*/session.json"):
        try:
            data = json.loads(record.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and isinstance(data.get("workspace"), str):
            found.append(data["workspace"])
        options = data.get("options", []) if isinstance(data, dict) else []
        for flag, value in zip(options, options[1:]) if isinstance(options, list) else []:
            if flag in ("-w", "--workspace") and isinstance(value, str):
                found += value.split(",")  # a session's other workspaces
    return found


def sandbox_writable(a=None):
    """[(tree, why)] of the host folders a sandbox can or could write, resolved: this run's (A: exposed_trees), those
    of earlier runs (exposed_before), the workspaces of the sandboxes running now (also those added during their run)
    and of the kept sessions. A symlink in one of them may have been planted by an agent."""
    found = {}

    def add(path, why):
        with contextlib.suppress(OSError, RuntimeError, ValueError, TypeError):
            path = Path(os.path.expanduser(str(path)))
            if path.is_absolute():
                found.setdefault(path.resolve(), why)
    for tree in exposed_trees(a) if a is not None else []:
        add(tree, "a folder this sandbox can write")
    for tree in exposed_before():
        add(tree, "a folder a sandbox could write in an earlier run")
    for record in sorted(runs_dir().glob("*/run.json")):
        try:
            data = json.loads(record.read_text())
            added = json.loads((record.parent / "workspaces.json").read_text()) \
                if (record.parent / "workspaces.json").is_file() else {}
        except (OSError, ValueError, RecursionError):
            continue
        name = printable(str(data.get("name") or data.get("run") or record.parent.name)) if isinstance(data, dict) \
            else record.parent.name
        if isinstance(data, dict) and isinstance(data.get("workspace"), str):
            add(data["workspace"], f"the workspace of the sandbox {name}")
        entries = added.get("workspaces") if isinstance(added, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("mode") == "rw" and isinstance(entry.get("host"), str):
                add(entry["host"], f"a workspace added to the sandbox {name}")
    for workspace in session_workspaces():
        add(workspace, "the workspace of a kept session")
    return list(found.items())


def kit_check(a=None):
    """CHECK(path, label) for the kits' host files (kits.stage, memory_prepare, declarations: called for the path as
    given, every symlink on its way and its target): refuses a path inside this run's agent-writable trees
    (check_host_source, with A) and a symlink that lies in a folder a sandbox can or could write (sandbox_writable).
    The user may link a kit entry to a skill in a Git checkout; an agent working in that checkout could replace the
    skill's folder with a link to ~/.ssh, and the next start would copy the keys."""
    trees = exposed_trees(a) if a is not None else []
    writable = sandbox_writable(a)

    def check(path, label):
        if a is not None:
            check_host_source(a, path, f"Kit file ({label})", trees)  # (each said once)
        path = Path(path)
        if not path.is_symlink():
            return
        with contextlib.suppress(OSError, RuntimeError):
            holder = path.parent.resolve()
            for tree, why in writable:
                if holder == tree or tree in holder.parents:
                    raise ValueError(
                        f"Kit file ({label}): {path} -> {os.readlink(path)} is a symlink inside {tree} ({why}); an "
                        "agent may have planted it to get another host folder (~/.ssh, say) copied into a sandbox. "
                        "Check it, then let the kit point at the real folder, or copy the files into the kit "
                        "(sandbox-ai kits add-skill KIT FOLDER)")
    return check


def kit_source_notes(plan, a=None):
    """Notes on the kit entries of PLAN (kits.scan) that come from a folder a sandbox can or could write: what an agent
    writes there reaches every sandbox the kit is given to."""
    writable, notes = sandbox_writable(a), []
    for items in plan["entries"].values():
        for label, source in items.values():
            with contextlib.suppress(OSError, RuntimeError):
                real = source.resolve()
                tree, why = next(((tree, why) for tree, why in writable if real == tree or tree in real.parents),
                                 (None, None))
                if tree is not None:
                    notes.append(f"{label}: {source} comes from {tree} ({why}): what an agent writes there reaches "
                                 "every sandbox this kit is given to")
    return notes


def remember_exposed(a, trees=None):
    """Add this run's agent-writable trees (or TREES) to the registry: a symlink found in one of them later may
    have been planted by an agent, to get another host directory mounted at a later start."""
    path = exposed_registry()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path.with_name(".exposed.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # two launches at once must not drop each other's trees
            new = [str(t) for t in (exposed_trees(a) if trees is None else trees)]
            known = list(dict.fromkeys(new + [str(t) for t in exposed_before()]))[:2000]
            temporary = path.with_name(f".exposed.{os.getpid()}.json")
            temporary.write_text(json.dumps(known))
            os.replace(temporary, path)
    except OSError as exc:
        print(f"agent-sandbox: warning: cannot record exposed directories in {path}: {exc}", file=sys.stderr)


def image_mount_hosts(project_document=None):
    """Resolved host folders of the MCP image servers in your mcp.json and in the trusted project file."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "mcp.json"
    try:
        servers = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        servers = {}
    servers = dict(servers) if isinstance(servers, dict) else {}
    project = (project_document or {}).get("mcp_servers") if isinstance(project_document, dict) else None
    if isinstance(project, dict):
        servers.update({f"project:{name}": value for name, value in project.items()})
    hosts = []
    for definition in servers.values():
        for mount in definition.get("mounts", []) if isinstance(definition, dict) else []:
            # (an absolute folder only: "@workspace", the start's workspace, is bound at each start - never a path
            # resolved against the folder the launcher runs in)
            if isinstance(mount, dict) and isinstance(mount.get("host"), str) and \
                    os.path.isabs(os.path.expanduser(mount["host"])):
                with contextlib.suppress(OSError, RuntimeError):
                    hosts.append(Path(os.path.expanduser(mount["host"])).resolve())
    return hosts


def planted_symlink(source, trees):
    """The first symlink on the way to SOURCE that lies in one of TREES, where a sandbox could have created it; None.
    The way is followed one link at a time (kits.link_hops): SOURCE itself, its folders, and every link their targets
    go through, so a link of yours (~/kit/skill -> ~/src/proj/skill) whose target an agent re-pointed is seen too.
    Links elsewhere (~/src -> /data/src, /home -> /var/home) are yours."""
    try:
        _, hops = load_kits_module().link_hops(source)
    except ValueError:
        return Path(source)  # a loop: nobody's own link
    for path, _ in hops:
        try:
            holder = path.parent.resolve()
        except (OSError, RuntimeError):
            continue
        if any(holder == tree or tree in holder.parents for tree in trees):
            return path
    return None


def check_planted(value, option, trees):
    """Refuse the host path VALUE (as given) if it goes through a symlink a sandbox may have planted."""
    source = Path(os.path.normpath(Path(value).expanduser().absolute()))
    link = planted_symlink(source, trees)
    if link is not None:
        raise ValueError(f"{option} {source} goes through the symlink {link} -> {os.readlink(link)}, inside a "
                         f"directory a sandbox could write; it may have been planted to get another host directory "
                         f"mounted. Check it, then give the real path: {shlex.quote(str(source.resolve()))}")


def project_tool_dir(directory, workspace):
    """DIRECTORY (on PATH, inside WORKSPACE) is the project's own tool directory: an activated virtualenv or
    node_modules/.bin. Any other PATH directory inside a workspace (~/.cargo/bin in ~/.cargo) is refused."""
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env and Path(virtual_env).expanduser().resolve() / "bin" == directory:
        return True
    return directory.name in ("bin", ".bin") and directory.parent.name in (".venv", "venv", "node_modules")


def tool_dirs_state(a):
    """{directory: {name: (size, mtime, inode)}} of the PATH directories inside the workspaces (a project's own
    .venv/bin or node_modules/.bin, which project_tool_dir lets through): what you run there next comes from the
    agent. Compared after the run by tool_dirs_changed."""
    workspaces = [Path(os.path.normpath(Path(w).absolute())) for w in a.workspaces]
    workspaces += [w.resolve() for w in workspaces]
    state = {}
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        literal = Path(os.path.normpath(entry))
        try:
            directory = literal.resolve()
        except (OSError, RuntimeError):  # RuntimeError: a symlink loop (Python before 3.13)
            continue
        # As written (a link in the workspace may lead out of it: the agent can swap that link) or where it leads.
        if not any(path == w or w in path.parents for w in workspaces for path in (literal, directory)):
            continue
        # "" is no file name: it keeps where the entry led at the start, to see a link swapped meanwhile.
        files = {"": ("target", str(directory))}
        with contextlib.suppress(OSError):
            for item in os.scandir(directory):
                with contextlib.suppress(OSError):
                    info = item.stat(follow_symlinks=False)
                    # ctime too: an agent can write the same size in place and reset the mtime, not the ctime.
                    files[item.name] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        state[str(literal)] = files
    return state


def tool_dirs_changed(before):
    """Notes on files added or changed during the run in the PATH directories recorded by tool_dirs_state."""
    notes = []
    for directory, files in before.items():
        # Where the entry led at the start: leading elsewhere now (the directory or a link on the way swapped by
        # the agent) would send the comparison to another folder, so it is named instead.
        target = files.get("", (None, None))[1]
        try:
            moved = not os.path.isdir(directory) or (target is not None and str(Path(directory).resolve()) != target)
        except (OSError, RuntimeError):  # a symlink loop the agent made there: replaced, as said below
            moved = True
        if moved:
            notes.append(f"the agent replaced {directory}, which is on your PATH, (by a link or something else); "
                         "check it before running anything from this shell")
            continue
        now = tool_dirs_state(argparse.Namespace(workspaces=[directory]))
        current = {k: v for k, v in now.get(directory, {}).items() if k}
        files = {k: v for k, v in files.items() if k}
        added = sorted(set(current) - set(files))
        changed = sorted(name for name in set(current) & set(files) if current[name] != files[name])
        if added or changed:
            names = ", ".join((added + changed)[:10]) + (" ..." if len(added + changed) > 10 else "")
            notes.append(f"the agent added or changed programs in {directory}, which is on your PATH: {names}; "
                         "check them before running anything from this shell")
    return notes


def private_locations():
    """([fixed], [path]): host directories whose contents run on the host or hold its keys (also the packages'
    copies in ~/.cache/sandbox-ai). A workspace may not
    be, contain or lie in a fixed one, nor be or lie in a PATH directory (~/.cargo/bin, ~/go/bin ...; a project
    may contain its own activated .venv/bin)."""
    home = Path.home().resolve()
    fixed = {Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),
             Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
             Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state"),
             home / ".local", home / ".ssh", home / ".gnupg", home / ".bin", home / "bin", home / ".mozilla",
             # The copies sandbox-ai.pyz and the AppImage run from (scripts/dist_boot.py).
             Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "sandbox-ai"}
    if sys.platform == "darwin":
        # macOS: login items and agents (run at the next login), the Keychains, and the settings and data of the
        # Mac's own programs.
        fixed |= {home / "Library" / name for name in ("LaunchAgents", "Keychains", "Application Support",
                                                       "Preferences")}
    path = {Path(entry) for entry in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(entry)}

    def resolved(paths):
        result = set()
        for item in paths:
            try:
                result.add(item.expanduser().resolve())
            except OSError:
                continue
        return sorted(item for item in result if item != Path("/"))
    return resolved(fixed), resolved(path)


# Files directly in HOME by what they are (any other file there: settings read or run as you).
HOME_FILES = {**dict.fromkeys((".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc",
                               ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".kshrc", ".cshrc", ".tcshrc", ".login",
                               ".xprofile", ".xinitrc", ".xsession"),
                              "a shell or session start file: what is written there runs as you at your next login"),
              **dict.fromkeys((".gitconfig",), "your Git configuration: its hooks, aliases and credential helpers "
                                               "run as you"),
              **dict.fromkeys((".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc", ".my.cnf", ".s3cfg"),
                              "a credentials file: the agent reads the passwords and tokens in it"),
              **dict.fromkeys((".claude.json",), "Claude Code's settings: its MCP servers are commands that run as "
                                                 "you, its projects your history")}


def login_locations():
    """[(path, label)]: the agents' login and configuration folders on the host (~/.claude, ~/.codex, OpenCode's,
    and where CLAUDE_CONFIG_DIR and CODEX_HOME point), and other folders of credentials (~/.aws, ~/.kube, ~/.docker):
    given with a warning that names them (check_private_location)."""
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    found = [(home / ".claude", "Claude Code's folder (its login, settings and hooks)"),
             (home / ".codex", "Codex's folder (its login and settings)"),
             (data / "opencode", "OpenCode's folder (its login)"), (config / "opencode", "OpenCode's settings"),
             (home / ".aws", "your AWS credentials"), (home / ".kube", "your Kubernetes credentials"),
             (home / ".docker", "your container registry logins"), (config / "gh", "the GitHub CLI's login")]
    for variable, label in (("CLAUDE_CONFIG_DIR", "Claude Code's folder (CLAUDE_CONFIG_DIR: its login and settings)"),
                            ("CODEX_HOME", "Codex's folder (CODEX_HOME: its login and settings)")):
        if os.path.isabs(os.environ.get(variable) or ""):
            found.insert(0, (Path(os.environ[variable]), label))
    result = []
    for path, label in found:
        try:
            result.append((Path(os.path.normpath(path.expanduser().absolute())).resolve(), label))
        except (OSError, RuntimeError):
            continue
    return [(path, label) for path, label in result if path != Path("/")]


def plugin_store_run(a):
    """A is the plugin installer's own start (sandbox-ai plugins claude): Claude with --no-plugins on the plugin
    store itself as its HOME. Its HOME holds the store's index (known_marketplaces.json, installed_plugins.json,
    extraKnownMarketplaces), which that run writes: --no-plugins must not clear it there."""
    if getattr(a, "agent", None) != "claude" or not getattr(a, "no_plugins", False) or not getattr(a, "state_dir", None):
        return False
    try:
        home = Path(os.path.normpath(Path(a.state_dir).expanduser().absolute())).resolve()
        data = Path(os.path.normpath((Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
                                      / "sandbox-ai").absolute())).resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    return home == data / "plugins" / "claude"


def own_folders(a):
    """[path] (resolved): the folders inside sandbox-ai's own (sandbox_ai_folders) that this start A is sandbox-ai's
    own internal start of, and so may be given: a session's or a kept start's own HOME (its --state-dir is its
    start directory's home/), an MCP server's (or helper's) own home/ and work/ beside the socket directory it serves
    on (--mcp-socket-dir), a team board's board/ for that server, a login's staging home/ and work/ (sandbox-ai login
    gives both, of one staging folder), the plugin installer's store and its work folder (sandbox-ai plugins), and
    the wizard's persistent HOME of this start's project. Every other start gets none: another session's HOME, an
    MCP server's or a login's are sandbox-ai's own folders like the rest."""
    def real(path):
        try:
            return Path(os.path.normpath(Path(path).expanduser().absolute())).resolve()
        except (OSError, RuntimeError, TypeError):
            return None
    state = real(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai")
    data = real(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai")
    home = real(a.state_dir) if getattr(a, "state_dir", None) else None
    workspaces = [real(w) for w in getattr(a, "workspaces", None) or []]
    work = workspaces[0] if workspaces else None
    found = []
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        kept = kept_start_dir(a)
        if kept is not None:
            found.append(real(kept / "home"))
    if getattr(a, "mcp_socket_dir", None) and state is not None:
        server = real(a.mcp_socket_dir).parent
        if server.parent == state / "mcp" and real(a.mcp_socket_dir).name == "sock":
            found += [server / "home", server / "work"]
            if server.name.startswith("team-"):
                found.append(real(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                                  / "sandbox-ai" / "teams" / server.name[len("team-"):] / "board"))
    if home is not None and work is not None and data is not None and home.parent == work.parent and \
            home.name == "home" and work.name == "work" and home.parent.parent == data / "auth" and \
            home.parent.name.startswith(".login-"):
        found += [home, work]
    if home is not None and data is not None and plugin_store_run(a):
        found.append(home)
        if work is not None and work.parent == data / "plugins" and work.name.startswith(".work-"):
            found.append(work)
    if home is not None and state is not None and home.parent == state / "home":
        given = {str(w) for w in getattr(a, "workspaces", None) or []} | {str(w) for w in workspaces if w}
        digests = {hashlib.sha256(os.fsencode(value)).hexdigest()[:12] for value in given}
        if any(home.name.endswith(f"-{digest}") for digest in digests):
            found.append(home)
    # The mount points of this start's remote folders (sshfs://, plan_remote_mounts): sandbox-ai's own, for it.
    found += [real(point) for point in getattr(a, "remote_given", None) or {}]
    return list(dict.fromkeys(path for path in found if path is not None))


def own_home(path, folders=()):
    """PATH (resolved) is or lies in one of FOLDERS (own_folders of the start): a HOME or work folder sandbox-ai
    keeps inside its own folders for exactly that start. Nothing is one for any other start: their siblings
    (session.json, logs, the registry) never are."""
    return any(path == Path(folder) or Path(folder) in path.parents for folder in folders)


def package_paths(environ=None):
    """[(path, label)]: the ready-made package this launcher runs from (sandbox-ai.pyz, the AppImage, sandbox-ai.app)
    and the copy it runs in, from $SANDBOX_AI_PACKAGE_COPY and $SANDBOX_AI_PACKAGE (set by the package's start,
    scripts/dist_boot.py). Each counts only for what it claims: the copy must be a folder of this user that holds this
    launcher (an inherited variable names another one), the package an existing file or folder of this user or root;
    anything else is ignored."""
    environ = os.environ if environ is None else environ
    found = []
    for variable, label, kinds in (("SANDBOX_AI_PACKAGE_COPY", "the copy sandbox-ai runs in", (stat.S_ISDIR,)),
                                   ("SANDBOX_AI_PACKAGE", "the package sandbox-ai runs from",
                                    (stat.S_ISREG, stat.S_ISDIR))):
        value = environ.get(variable, "")
        if not os.path.isabs(value):
            continue
        try:
            path = Path(value).resolve()
            info = path.stat()
        except (OSError, RuntimeError):
            continue
        if not any(kind(info.st_mode) for kind in kinds) or info.st_uid not in (os.getuid(), 0):
            continue
        if variable == "SANDBOX_AI_PACKAGE_COPY":
            if info.st_uid != os.getuid() or not (path == HERE or path in HERE.resolve().parents):
                return []  # not this launcher's copy: the package named beside it is not ours either
        found.append((path, label))
    return found if found and found[0][1].startswith("the copy") else []


def check_package_location(path, option, packages=None, warn=None):
    """An agent-writable host tree PATH (resolved) that is, holds or lies in the package this launcher runs from or
    its copy: you decide, so it is given, with a warning (WARN, default: warner) - the agent can rewrite it, and the
    next start runs that on your host. The warning, or None."""
    for package, label in package_paths() if packages is None else packages:
        inside = path == package or package in path.parents
        if inside or path in package.parents:
            move = "" if label.startswith("the copy") else \
                f"; or move it out (mv {shlex.quote(str(package))} ~/.local/bin/) and start it from there"
            text = (f"{option} {path} {'lies in' if inside and path != package else 'contains'} {package}, {label}: "
                    "the agent can change the code that runs on your host as you (at the next start); "
                    f"{SAFER_READ_ONLY}{move}")
            warner(warn)(text)
            return text
    return None


def sandbox_ai_folders():
    """sandbox-ai's own folders (its configuration and trust store, its state, sessions and approvals, its logins
    and installed versions, its packages' copies): a sandbox given one of them could change sandbox-ai itself. Both
    where this environment's XDG_* variables put them and the default places below HOME (~/.config/sandbox-ai,
    ~/.local/share/sandbox-ai, ~/.local/state/sandbox-ai, ~/.cache/sandbox-ai): another start, the app or the web UI
    may run with other XDG_* values, or none."""
    home = Path.home()
    found = []
    for variable, default in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                              ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        for base in dict.fromkeys([os.environ.get(variable) or str(home / default), str(home / default)]):
            try:
                found.append((Path(base).expanduser() / "sandbox-ai").resolve())
            except (OSError, RuntimeError):
                continue
    return list(dict.fromkeys(found))


# Said in every warning about a host path you chose: what lowers its risk.
SAFER_READ_ONLY = "read-only (--ro, a --mount with :ro) is the safer choice"
_WARNED = set()


def warner(warn=None):
    """WARN, else the launcher's own way of saying a warning about a host path you chose: one line "agent-sandbox:
    warning: TEXT" on standard error (and with it in launcher.log), each text once per process - a start checks some
    paths more than once (the trust store, the defaults file)."""
    if warn is not None:
        return warn

    def say(text):
        if text not in _WARNED:
            _WARNED.add(text)
            print(f"agent-sandbox: warning: {printable(text)}", file=sys.stderr)
    return say


def folded(path):
    """PATH as compared with the private locations: on macOS case-folded (APFS and HFS+ are case-insensitive by
    default: ~/library/launchagents is ~/Library/LaunchAgents there), elsewhere as it is."""
    return Path(str(path).casefold()) if sys.platform == "darwin" else Path(path)


def check_private_location(path, option, warn=None, own=(), refused=()):
    """A host tree PATH (resolved) in or over a private location (settings, keys, autostart entries, programs on your
    PATH, the agents' login folders, files directly in your HOME) or in or over sandbox-ai's own folders
    (sandbox_ai_folders, and REFUSED: those a start recorded for a later workspace add): you decide what a sandbox
    gets, so it is given, read-only or not, with a warning (WARN(text), default: warner) that names what is there -
    in sandbox-ai's own folders the agent can change sandbox-ai itself (its approvals, network rules and policy, the
    trust store, its own run's records). OWN: the folders of sandbox-ai's own internal start this is (own_folders),
    which it is given without a word. The warning, or None."""
    path = Path(path)
    if own_home(path, own):
        return None
    say = warner(warn)
    shown, path = path, folded(path)
    for own_folder in list(dict.fromkeys([*sandbox_ai_folders(), *map(Path, refused)])):
        shown_folder, own_folder = own_folder, folded(own_folder)
        if path == own_folder or own_folder in path.parents or path in own_folder.parents:
            inside = own_folder in path.parents or path == own_folder
            text = (f"{option} {shown} {'lies in' if inside else 'contains'} {shown_folder}, sandbox-ai's own "
                    "folder: the agent can change sandbox-ai itself - its approvals, network rules and policy, the "
                    f"trust store, its own run's records; {SAFER_READ_ONLY}, which lowers this to reading them "
                    "(request logs with their bodies, approvals, logins)")
            say(text)
            return text
    home = folded(Path.home().resolve())
    if path.parent == home and path.name and shown.exists() and not shown.is_dir():
        label = {folded(name).name: text for name, text in HOME_FILES.items()}.get(
            path.name, "a file of your HOME directory: settings that programs of yours read or run as you")
        text = f"{option} {shown} is {label}; where the agent may write, what it puts there acts on this host"
        say(text)
        return text
    for location, label in login_locations():
        inside = path == folded(location) or folded(location) in path.parents
        if inside or path in folded(location).parents:
            text = (f"{option} {shown} {'lies in' if inside else 'contains'} {location}, {label}: the agent sees the "
                    "logins and settings there, and where it may write, what it puts there acts as you")
            say(text)
            return text
    fixed, program_dirs = private_locations()
    for private in fixed + program_dirs:
        inside = path == folded(private) or folded(private) in path.parents
        if inside or (path in folded(private).parents and not project_tool_dir(private, shown)):
            text = (f"{option} {shown} {'lies in' if inside else 'contains'} {private}: the agent sees what is there "
                    "(settings, keys), and where it may write, what it puts there (autostart entries, programs on your "
                    "PATH) runs on this host")
            say(text)
            return text
    return None


def launcher_trees():
    """[path]: sandbox-ai's own launcher (HERE, the sandbox/ folder) and, when it runs from a sandbox-ai tree (a
    checkout or an installed version: install.sh beside sandbox/), that whole tree (the app in projects/, install.sh,
    scripts/); installed with pip into a virtualenv, that environment (its bin/ holds the sandbox-ai entry script and
    the Python that runs it); installed with pip --user, the folder of the user's entry scripts (~/.local/bin). An
    agent given any of it could rewrite what runs on the host at the next start."""
    import site
    import sysconfig
    trees = [HERE]
    root = HERE.parent
    if (root / "install.sh").is_file() and (root / "sandbox" / "agent_sandbox.py").is_file():
        trees.append(root)
    with contextlib.suppress(OSError, RuntimeError, KeyError, AttributeError):
        prefix = Path(sys.prefix).resolve()
        if sys.prefix != sys.base_prefix and prefix in HERE.parents:
            trees.append(prefix)
        if Path(site.getusersitepackages()).resolve() in HERE.parents:
            trees.append(Path(sysconfig.get_path("scripts", f"{os.name}_user")).resolve())
    return list(dict.fromkeys(trees))


def in_launcher_tree(path):
    """PATH (resolved) is or lies in a launcher tree (launcher_trees)."""
    return any(path == tree or tree in path.parents for tree in launcher_trees())


def check_launcher_location(path, option, trees=None, warn=None):
    """A host PATH (resolved) a sandbox gets writable that is, lies in or contains one of sandbox-ai's own launcher
    trees (TREES, default launcher_trees()): you decide, so it is given, with a warning (WARN, default: warner) - the
    agent can rewrite the launcher, and the next start runs that on your host. One check for every writable path:
    workspaces (also those added during the run), --mount rw, --state-dir, an MCP server's folders. Read-only, all
    of them may hold it without a word. The warning, or None."""
    shown = Path(path)
    path = folded(path)
    for tree in launcher_trees() if trees is None else trees:
        shown_tree, tree = Path(tree), folded(tree)
        if path == tree or tree in path.parents or path in tree.parents:
            where = "lies in" if tree in path.parents else "is" if path == tree else "contains"
            text = (f"{option} {shown} {where} sandbox-ai's own launcher ({shown_tree}): the agent can change the "
                    "code that runs on your host as you (at the next start, and with every sandbox-ai command); "
                    f"{SAFER_READ_ONLY}, or install sandbox-ai (./install.sh --from .) and start it from there")
            warner(warn)(text)
            return text
    return None


# How far sockets_below looks into a tree: entries in all, and folders deep.
SOCKET_SCAN_ENTRIES = 20000
SOCKET_SCAN_DEPTH = 12


def live_socket(path):
    """Something listens on the Unix socket file PATH (a connection is taken or waits, or it is no stream socket),
    or cannot be told; False for a stale one (refused: nothing listens there any longer)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.setblocking(False)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except OSError:
            return True  # waiting (backlog), a datagram socket (EPROTOTYPE), not allowed: someone is there
        return True


def sockets_below(path, entries=None, depth=None):
    """(socket, complete): the first Unix socket file at or below the host tree PATH whose listener lives
    (live_socket; a stale one left by a process that ended is no way to anything), and whether the whole tree was
    looked at (at most ENTRIES entries, DEPTH folders deep; links are not followed). A socket a sandbox is given
    reaches what listens on it (gpg-agent's, an SSH ControlMaster's, a desktop's), even through a read-only mount."""
    entries = SOCKET_SCAN_ENTRIES if entries is None else entries
    depth = SOCKET_SCAN_DEPTH if depth is None else depth
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        return None, True
    if stat.S_ISSOCK(info.st_mode):
        return (path, True) if live_socket(path) else (None, True)
    if not stat.S_ISDIR(info.st_mode):
        return None, True
    budget, pending = entries, [(path, 0)]
    while pending:
        folder, level = pending.pop()
        try:
            listing = os.scandir(folder)
        except OSError:
            continue
        with listing:
            items = iter(listing)
            while True:
                try:  # a kernel tree you chose (/proc) may fail half-way through a listing: what was read counts
                    item = next(items)
                except (StopIteration, OSError):
                    break
                budget -= 1
                if budget < 0:
                    return None, False
                try:
                    mode = item.stat(follow_symlinks=False).st_mode
                except OSError:
                    continue
                if stat.S_ISSOCK(mode) and live_socket(item.path):
                    return Path(item.path), True
                if stat.S_ISDIR(mode):
                    if level + 1 >= depth:
                        return None, False
                    pending.append((Path(item.path), level + 1))
    return None, True


def check_sockets_below(path, option, warn=None):
    """A host tree PATH (resolved) given to a sandbox that is or holds a Unix socket something listens on
    (sockets_below): you decide, so it is given, with a warning (WARN, default: warner) - whoever connects to it gets
    what runs behind it, even through a read-only mount; a warning too when the tree is too large to look at all of
    it. The warning, or None."""
    found, complete = sockets_below(path)
    if found is not None:
        text = (f"{option} {path} {'is' if Path(found) == Path(path) else 'holds'} the socket {found}, where a "
                "program of yours listens: the agent can connect to it and get what runs behind it (an agent of "
                "your keys, a shared SSH connection, a container engine: commands as you outside the sandbox), even "
                "through a read-only mount; stop that program, or give a folder beside it")
        warner(warn)(text)
        return text
    if not complete:
        text = (f"{option} {path} is too large to look through for sockets (more than {SOCKET_SCAN_ENTRIES} entries "
                f"or {SOCKET_SCAN_DEPTH} folders deep): sockets in it could not be ruled out")
        warner(warn)(text)
        return text
    return None


def check_given_path(path, option, writable, own=(), warn=None, refused=()):
    """Every host PATH (resolved) a sandbox is given, read-only or not (a workspace, --mount, --state-dir, an MCP
    image server's folder, a workspace added during the run): you decide what a sandbox gets, so it is given, with a
    warning (WARN, default: warner) for each risk it holds - a folder of your sockets, a file system of the kernel's
    own or its trees, a live socket in it, sandbox-ai's own folders (OWN: the folders of the internal start this is,
    said nothing of; REFUSED: further own folders a start recorded), a private location. Read-only, a file or folder
    of the launcher itself is given without a word (an MCP helper's script); WRITABLE, what is, lies in or holds the
    launcher's tree or the package it runs from is said. [warning] of what was said."""
    path, said = Path(path), []
    if str(path) in REMOTE_POINTS:
        return said  # a remote folder's mount point of this start (remotefs.warning says its risk)

    def say(text):
        said.append(text)
        warner(warn)(text)
    check_socket_location(path, option, warn=say)
    check_kernel_mounts(path, option, warn=say)
    if writable and not check_package_location(path, option, warn=say):  # the package's own words say where to
        check_launcher_location(path, option, warn=say)                   # move it
    if not (not writable and in_launcher_tree(path)):
        check_private_location(path, option, say, own, refused)
    check_sockets_below(path, option, say)
    return said


def project_directory(a):
    """The working directory when it holds a project configuration that is meant to apply - then it is the project
    (its main workspace, and the file that applies) - else None. Not with --no-config, --config or
    --workspaces-as-given, not without --workspace (then the working directory is the main workspace anyway), and
    not when the first workspace named is the working directory, or one lies in it or holds it (the user chose
    that folder)."""
    if a.no_config or a.config or getattr(a, "workspaces_as_given", False) or not a.workspace:
        return None
    here = logical_cwd()
    try:
        if not (Path(here) / CONFIG_NAME).is_file():
            return None
    except OSError:
        return None
    # The first workspace named is this directory (in any spelling: ., $PWD/, ../proj): it is the main one already.
    # One that lies in it or holds it is the folder the user chose (--workspace packages/web from a monorepo's
    # root): their order stands, and the project file that applies is that folder's.
    try:
        real = Path(here).resolve()
        named = [Path(os.path.expanduser(value)).resolve() for value in a.workspaces]
    except (OSError, RuntimeError):
        return None
    if not named or named[0] == real or any(real in path.parents or path in real.parents for path in named):
        return None
    return here


def parse_args(argv, parser=None):
    """PARSER: a build_parser() to read ARGV with, for a caller that reads the options of many starts (list --folders:
    building the parser, 150 options, took 4 ms for each start); the reading is the same as with a parser of its own."""
    # The separator belongs to this wrapper; everything afterwards is opaque.
    tail = []
    if "--" in argv:
        pos = argv.index("--")
        argv, tail = argv[:pos], argv[pos + 1:]
    if parser is None:
        a = build_parser(argv[0] if argv else None).parse_args(argv)
    else:
        a = parser.parse_args(argv)
        # argparse hands out an option's default (and constant) itself: a list this function adds to below (a
        # preset's hosts, a login's host) must be this reading's own, or the parser's next reading began with it.
        for action in parser._actions:
            value = getattr(a, action.dest, None)
            if isinstance(value, (list, dict, set)) and (value is action.default or value is action.const):
                setattr(a, action.dest, copy.deepcopy(value))
    expand_domains(a)
    expand_shared_wildcards(a)
    # Commas are invalid in workspace paths, so they can separate entries.
    a.workspaces = [part for value in (a.workspace or [logical_cwd()]) for part in value.split(",")]
    a.cwd_project = project_directory(a)
    if a.cwd_project:
        # The directory you are in holds a project configuration: it is the project (/work, and the file that
        # applies), and what --workspace names comes after it. Without a project file here, --workspace decides as
        # before. Starts built by sandbox-ai itself (the app, the web interface, team members, `resume` and
        # `restart`) pass --workspaces-as-given, so their own working directory can never take a project over.
        real = Path(a.cwd_project).resolve()  # named again in another spelling (--workspace .): listed once
        a.workspaces = [a.cwd_project, *(w for w in a.workspaces if Path(os.path.expanduser(w)).resolve() != real)]
    a.subnet_origin = {}  # where a --subnet/--subnet6 came from when no option set it (defaults, policy)
    a.workspace = a.workspaces[0]
    a.gui = a.gui or bool(a.gui_vnc)
    for name in a.profile:
        profile = PROFILES[name]
        a.presets += [preset for preset in profile.get("presets", []) if preset not in a.presets]
        for flag in ("frontier", "dns", "egress_all_allow", "offline"):
            if profile.get(flag):
                setattr(a, flag, True)
    a.implied_allows = {}  # what further options opened, by source (reach(): "what it may reach, and why")
    before = opened_now(a)
    credentials = []
    for kind, value in git_token_values(a):
        try:
            credentials.append(git_token_credential(value, kind))
        except ValueError:
            continue  # validate() reports it
    try:
        credentials += [credential for credential, _ in git_credentials_file(a)[0]]
    except ValueError:
        pass  # validate() reports it
    for credential in credentials:
        if credential["loopback"]:
            # Reachable only through pasta's host forward; validate() refuses other setups.
            if loopback_reachable(a) and str(credential["port"]) not in a.allow_host_port:
                a.allow_host_port.append(str(credential["port"]))
        elif credential["url"] == "https://github.com":
            if "github" not in a.presets:
                a.presets.append("github")  # api.github.com and the download hosts too
        elif credential["host"] not in a.allow_host:  # pins the name; other ports need --allow-tcp
            a.allow_host.append(credential["host"])
    before = note_opened(a, "git-login", before)
    for host, _, _ in api_key_values(a, check=False):
        if host not in a.allow_host:
            a.allow_host.append(host)  # validate() reports a wrong --api-key
    before = note_opened(a, "api-key", before)
    for host in publish_hosts(a):
        if host not in a.allow_host:
            a.allow_host.append(host)
    before = note_opened(a, "publish", before)
    if a.llm:
        llm = load_llm_module()
        llm.network_options(a, loopback_reachable(a))
        a.llm_vpn_note = llm.apply_vpn(a)
        before = note_opened(a, "llm", before)
    # --AGENT-enable: each feature's hosts (and secrets) for the run's agent, or for one --with: they are the run's.
    if "hermes" in team(a):
        load_hermes_module().network_options(agent_view(a, "hermes"))
    for agent in ("qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi"):
        if agent in team(a):
            agent_module(agent).network_options(agent_view(a, agent))
    note_opened(a, "feature", before)
    if a.image is None and companions(a) and not a.image_variant:
        # Every agent of the run in one image: the team image, with the layers of the agents beyond its base (a
        # built one that holds them all is taken instead when this one is not there: start_image_checks).
        a.image = team_image_for(team(a))
        a.team_image_chosen = True
    if a.image is None:
        a.image = f"localhost/{a.agent}-sandbox{'-' + a.image_variant if a.image_variant else ''}:latest"
    a.agent_args = tail
    return a


def settle_network(a, hosts, resolve_names):
    """Once the allowed HOSTS are resolved (RESOLVE_NAMES resolves further names the same way: by the host, the
    Podman machine or, with --vpn, from what the tunnel resolved): --llm's name on a port other than 443 is pinned
    and opened on that port alone, and no address lies inside the sandbox's own network."""
    a.llm_pins = load_llm_module().resolved_network(a, hosts, resolve_names) if a.llm else {}
    check_subnets(a, [addr for values in hosts.values() for addr in values])
    for key, version in (("subnet", "ipv4"), ("subnet6", "ipv6")):
        if getattr(a, key):
            entry = sandbox_network(a)[version]
            print(f"agent-sandbox: sandbox network {printable(subnet_label(a, key))}: the sandbox "
                  f"{entry['address']}, gateway {entry['gateway']}", file=sys.stderr)


def domain_of(value):
    """The domain --allow-domain VALUE names: lower case, without one leading "*." or "." and one trailing "."
    (*.example.com, .example.com and example.com. all name example.com). ValueError, naming --allow-domain, for a
    value that is no host name in ASCII (a name in another script is given in its xn-- form), for a top-level domain
    alone and for what --allow-host '*.DOMAIN' refuses: a public suffix (co.uk) or a hosting service (github.io), or
    a name below one, where anyone can have a name of their own."""
    shown = printable(str(value))[:100]
    name = str(value).lower()
    name = name[2:] if name.startswith("*.") else name[1:] if name.startswith(".") else name
    name = name[:-1] if name.endswith(".") else name
    if not name.isascii():
        raise ValueError(f"--allow-domain {shown}: a name outside ASCII is given in its xn-- form (as --allow-host "
                         "takes it)")
    if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                  for part in name.split(".")):
        raise ValueError(f"--allow-domain {shown}: not a domain name such as example.com")
    if "." not in name:
        raise ValueError(f"--allow-domain {shown}: a whole top-level domain, where anyone can have a name of their "
                         f"own; give a domain below it, such as example.{name}")
    try:
        RUNTIME.wildcard_suffix("*." + name)
    except RUNTIME.SharedWildcardError as exc:
        raise ValueError(f"--allow-domain {name}: refused: it reaches {exc.shared}, where anyone can have a name of "
                         f"their own (a hosting or cloud service): the agent could open any server anyone rents below "
                         f"{exc.shared}, and send data there; give the hosts by name instead, or accept that risk with "
                         f"--allow-shared-wildcard '*.{name}' (and --allow-host {name})") from None
    except ValueError as exc:
        raise ValueError(f"--allow-domain {name}: " + str(exc).replace("not a host wildcard here", "refused", 1)
                         .replace("not a host wildcard *.DOMAIN.TLD", "not a domain such as example.com", 1)) from None
    return name


def expand_shared_wildcards(a):
    """--allow-shared-wildcard '*.DOMAIN' as what it means: --allow-host '*.DOMAIN', its risk confirmed (the one place
    it is turned into it). a.shared_confirmed: the wildcards (lower case) whose risk the human confirmed - given so,
    or answered yes to on a terminal (confirm_shared_wildcards). Returns the host entries it added."""
    added = []
    a.shared_confirmed = set(getattr(a, "shared_confirmed", None) or ())
    for value in getattr(a, "allow_shared_wildcard", None) or []:
        host = str(value).lower()
        a.shared_confirmed.add(host)
        if host not in a.allow_host:
            a.allow_host.append(host)
            added.append(host)
    return added


def shared_wildcards(a):
    """{"*.DOMAIN": the shared suffix} of the run's host wildcards below a shared service whose risk the human
    confirmed (run.json and config.json "shared_wildcards", the plan, sandbox-ai report: marked as a risk)."""
    found = {}
    for host in host_wildcards(a):
        if host in (getattr(a, "shared_confirmed", None) or ()):
            with contextlib.suppress(ValueError):
                shared = RUNTIME.shared_wildcard(host)
                if shared:
                    found[host] = shared
    return dict(sorted(found.items()))


def confirm_shared_wildcards(a, ask=None):
    """The host wildcards of A below a shared service (--allow-host '*.DOMAIN') not confirmed yet: confirmed by --yes,
    or by the human's yes to "allow anyway? [y/N]" on a terminal (ASK: the question, for tests; None: asked on the
    terminal when standard input and error are one). What stays unconfirmed is refused by validate(), naming
    --allow-shared-wildcard. Then each confirmed one is said as a warning with its risk. Returns the risks said."""
    risks = []
    for host in dict.fromkeys(h for h in a.allow_host if h.startswith("*.")):
        try:
            risk = RUNTIME.shared_wildcard_risk(host)
        except ValueError:
            risk = None
        if not risk:
            continue
        if host not in a.shared_confirmed:
            if getattr(a, "yes", False):
                a.shared_confirmed.add(host)
                a.shared_answered = [*getattr(a, "shared_answered", []), host]
            else:
                question = ask
                if question is None and sys.stdin.isatty() and sys.stderr.isatty():
                    def question(text):
                        print(f"agent-sandbox: {printable(text)}", file=sys.stderr)
                        try:
                            return input("agent-sandbox: allow anyway? [y/N] ").strip().lower() in ("y", "yes")
                        except EOFError:
                            return False
                if question is not None and question(f"--allow-host {host}: {risk}"):
                    a.shared_confirmed.add(host)
                    a.shared_answered = [*getattr(a, "shared_answered", []), host]
        if host in a.shared_confirmed:
            risks.append(risk)
            warner()(f"--allow-shared-wildcard {host}: {risk} (you confirmed it)")
    return risks


def confirmed_argv(a, argv):
    """ARGV with what was confirmed by the start's risk question or --yes (a.shared_answered: wildcards below a shared
    service given with --allow-host) as --allow-shared-wildcard, before a "--": the options a session keeps for its
    supervisor and its later runs, which have no terminal to ask on (and must not ask unseen in tmux)."""
    added = [token for host in getattr(a, "shared_answered", None) or [] for token in ("--allow-shared-wildcard", host)]
    if not added:
        return list(argv)
    at = argv.index("--") if "--" in argv else len(argv)
    return [*argv[:at], *added, *argv[at:]]


def expand_domains(a):
    """--allow-domain DOMAIN as what it means, the one place it is turned into it: --allow-host DOMAIN and
    --allow-host '*.DOMAIN' (each once), so that everything after the parser (pins, wildcards, the policy, the
    modes, the plan) sees only those. A value domain_of refuses adds nothing (validate() reports it). Returns the
    host entries it added."""
    added = []
    for value in getattr(a, "allow_domain", None) or []:
        try:
            domain = domain_of(value)
        except ValueError:
            continue
        for host in (domain, "*." + domain):
            if host not in a.allow_host:
                a.allow_host.append(host)
                added.append(host)
    return added


def allowed_hosts(a):
    """The names pinned at the start and allowed on TCP/443 (no wildcards: see host_wildcards)."""
    if a.offline:
        return []
    defaults = [host for agent in team(a) for host in PROVIDERS[agent]]
    return list(dict.fromkeys(([] if a.no_provider_defaults or a.local_config else defaults)
                              + (FRONTIER if a.frontier else [])
                              + [host for preset in a.presets for host in PRESETS[preset]]
                              + [host for host in a.allow_host if not host.startswith("*.")]))


def host_wildcards(a):
    """--allow-host *.DOMAIN: every name below DOMAIN (not DOMAIN itself), each pinned and opened when it is looked
    up: the DNS stub holds the lookup, the launcher answers its question on the host (approvals.watch), the address
    goes back at once. As "*.DOMAIN", sorted."""
    if a.offline:
        return []
    return sorted({host for host in a.allow_host if host.startswith("*.")})


def opened_now(a):
    """What the options open so far: (names, TCP targets, host ports, presets), each a set (note_opened)."""
    return (set(a.allow_host), set(a.allow_tcp), set(a.allow_host_port), set(a.presets))


def note_opened(a, source, before):
    """Remember what was opened since BEFORE (opened_now) as opened by SOURCE (a.implied_allows: {"host:NAME",
    "tcp:IP:PORT", "port:N", "preset:NAME": source}); returns what is open now."""
    now = opened_now(a)
    for kind, was, current in zip(("host", "tcp", "port", "preset"), before, now):
        for value in current - was:
            a.implied_allows.setdefault(f"{kind}:{value}", source)
    return now


# The sources of what a start may reach (reach()), as `sandbox-ai net show` names them; SCOPES: how long each holds.
REACH_SOURCES = {"provider": "the agent's provider", "frontier": "--frontier", "profile": "the profile",
                 "project": "the project file", "option": "the start's options", "approved": "approved always",
                 "toolchain": "a toolchain's registries", "git-login": "a Git login", "api-key": "an API key",
                 "publish": "a publish login", "llm": "--llm", "feature": "an agent feature",
                 "policy": "the administrator's policy"}
REACH_SCOPES = {"project": "every start of the project", "approved": "every start of the project"}


def option_values(tokens, names):
    """{option: [values]} of the options NAMES in TOKENS (long forms, as canonical_options writes them; "--x=v" or
    "--x v"; a value may list several, separated by commas or spaces); a flag of NAMES that takes no value: [True]."""
    found, index, tokens = {}, 0, [str(t) for t in tokens or []]
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            break
        name, has, value = token.partition("=")
        if name not in names:
            continue
        if names[name] is None:  # a flag
            found.setdefault(name, []).append(True)
            continue
        if not has:
            if index >= len(tokens):
                break
            value, index = tokens[index], index + 1
        found.setdefault(name, []).extend(v for v in re.split(r"[\s,]+", value.lower() if names[name] == "lower"
                                                               else value) if v)
    return found


REACH_OPTIONS = {"--allow-host": "lower", "--allow-domain": "lower", "--allow-shared-wildcard": "lower",
                 "--allow-tcp": "", "--allow": "", "--allow-net": "", "--allow-host-port": "", "--nameserver": "", "--dns": None,
                 "--frontier": None, "--egress-all-allow": None, "--offline": None, "--profile": "",
                 **{f"--allow-{name}": None for name in PRESETS}}


def given_by(tokens):
    """What TOKENS (start options) name themselves: {"hosts", "tcp", "nets", "ports", "presets", "profiles"} (sets)
    and the flags "dns", "frontier", "egress", "offline"."""
    values = option_values(tokens, REACH_OPTIONS)
    hosts = set(values.get("--allow-host", [])) | set(values.get("--allow-shared-wildcard", []))
    for value in values.get("--allow-domain", []):
        try:
            domain = domain_of(value)
        except ValueError:
            continue
        hosts |= {domain, "*." + domain}
    return {"hosts": hosts, "tcp": set(values.get("--allow-tcp", [])),
            "nets": set(values.get("--allow", [])) | set(values.get("--allow-net", [])),
            "ports": set(values.get("--allow-host-port", [])), "nameservers": set(values.get("--nameserver", [])),
            "presets": {name for name in PRESETS if values.get(f"--allow-{name}")},
            "profiles": set(values.get("--profile", [])), "dns": bool(values.get("--dns")),
            "frontier": bool(values.get("--frontier")), "egress": bool(values.get("--egress-all-allow")),
            "offline": bool(values.get("--offline"))}


def reach(a):
    """What the start A may reach, and why: [{"kind", "what", "source", "detail"?, "hosts"?, "scope"?}] - its
    provider's hosts, the presets (with their names), host names and wildcards, TCP targets, networks, the host's
    ports, DNS, everything (--egress-all-allow) or nothing (--offline) - each with the source that opened it
    (REACH_SOURCES: the agent's provider, a profile, the project file, the start's own options, an approval
    "always", a toolchain, a Git login, an API key, --llm, an agent feature) and, where it outlasts this start, its
    scope (REACH_SCOPES). For run.json (`sandbox-ai net show` lists it beside what was allowed during the run, the
    app and the web show it) and the plan. What only the policy, the mode or a kill switch takes away is not in it."""
    project = given_by((getattr(a, "project_document", None) or {}).get("options") or [])
    typed = given_by(getattr(a, "start_tokens", None) or [])
    implied = getattr(a, "implied_allows", None) or {}
    remembered_hosts = set(getattr(a, "remembered_hosts", None) or [])
    approved_tcp = set(getattr(a, "approved_tcp", None) or [])
    plan = getattr(a, "toolchain_plan", None) or {}
    toolchain_hosts, toolchain_presets = set(plan.get("hosts") or []), set(plan.get("presets") or [])
    toolchain_names = ", ".join(str(e.get("name")) for e in plan.get("entries") or [] if isinstance(e, dict))
    profiles = [name for name in getattr(a, "profile", None) or [] if name in PROFILES]
    entries = []

    def add(kind, what, source, detail=None, **more):
        entry = {"kind": kind, "what": what, "source": source, **({"detail": detail} if detail else {}), **more}
        if source in REACH_SCOPES:
            entry["scope"] = REACH_SCOPES[source]
        entries.append(entry)

    def source_of(kind, value, given_key):
        """(source, detail) of VALUE: the project file, the start's options, an approval, a toolchain, another
        option that implies it (implied_allows), else the start's options (a preset's, the mode's, the app's)."""
        if value in project[given_key]:
            return "project", getattr(a, "project_config", None)
        if value in typed[given_key]:
            return "option", None
        if kind == "host" and value in remembered_hosts or kind == "tcp" and value in approved_tcp:
            return "approved", None
        if kind == "host" and value in toolchain_hosts:
            return "toolchain", toolchain_names
        if f"{kind}:{value}" in implied:
            source = implied[f"{kind}:{value}"]
            return source, getattr(a, "llm", None) if source == "llm" else None
        return "option", None

    if a.offline:
        add("nothing", "--offline", "project" if project["offline"] else "profile" if "offline" in profiles
            else "option", "offline" if "offline" in profiles and not project["offline"] else None)
        return entries
    if a.egress_all_allow:
        source = "project" if project["egress"] else "profile" if "egress-all" in profiles and not typed["egress"] \
            else "option"
        add("everything", "--egress-all-allow", source, "egress-all" if source == "profile" else None)
    covered = set()
    if not (a.no_provider_defaults or a.local_config):
        for agent in team(a):
            hosts = [host for host in PROVIDERS.get(agent, []) if host not in covered]
            if hosts:
                add("preset", agent, "provider", agent, hosts=hosts)
                covered.update(hosts)
    if a.frontier:
        hosts = [host for host in FRONTIER if host not in covered]
        if hosts:
            by_profile = [p for p in profiles if PROFILES[p].get("frontier")]
            source = "project" if project["frontier"] else "option" if typed["frontier"] or not by_profile \
                else "profile"
            add("preset", "--frontier", source, by_profile[0] if source == "profile" else None, hosts=hosts)
            covered.update(hosts)
    for name in a.presets:
        hosts = PRESETS.get(name, [])
        by_profile = [p for p in profiles if name in PROFILES[p].get("presets", [])]
        if name in project["presets"]:
            source, detail = "project", getattr(a, "project_config", None)
        elif name in typed["presets"]:
            source, detail = "option", None
        elif by_profile:
            source, detail = "profile", by_profile[0]
        elif name in toolchain_presets:
            source, detail = "toolchain", toolchain_names
        elif f"preset:{name}" in implied:
            source, detail = implied[f"preset:{name}"], None
        else:
            source, detail = "option", None
        add("preset", f"--allow-{name}", source, detail, hosts=list(hosts))
        covered.update(hosts)
    for host in allowed_hosts(a):
        if host not in covered:
            add("https", host, *source_of("host", host, "hosts"))
            covered.add(host)
    for wildcard in host_wildcards(a):
        add("wildcard", wildcard, *source_of("host", wildcard, "hosts"))
    for target in dict.fromkeys(a.allow_tcp):
        add("tcp", target, *source_of("tcp", target, "tcp"))
    for net in dict.fromkeys(a.allow):
        add("network", net, *source_of("net", net, "nets"))
    for port in dict.fromkeys(str(p) for p in a.allow_host_port):
        add("host-port", port, *source_of("port", port, "ports"))
    servers = nameservers(a)
    if servers:
        if a.nameserver:
            source = "project" if set(a.nameserver) <= project["nameservers"] else "option"
        elif a.egress_all_allow and not a.dns:
            source = entries[0]["source"] if entries and entries[0]["kind"] == "everything" else "option"
        else:
            source = "project" if project["dns"] else "option" if typed["dns"] or "dns" not in profiles else "profile"
        add("dns", ", ".join(servers), source, "dns" if source == "profile" else None)
    for target in getattr(a, "vpn_bypass_tcp", None) or []:
        add("tcp", str(target), "option", "past Tor")
    return entries


def reach_source_text(entry):
    """How `sandbox-ai net show` names the source of a reach() ENTRY: "the agent's provider (claude)",
    "the project file (/path/.sandbox-ai.json)"."""
    text = REACH_SOURCES.get(entry.get("source"), str(entry.get("source")))
    return f"{text} ({entry['detail']})" if entry.get("detail") else text


def recorded_reach(a):
    """reach(A) for run.json and the plan; [] where it cannot be told (never stops a start: it only explains)."""
    try:
        return reach(a)
    except Exception as exc:  # noqa: BLE001 - an explanation, never a reason to refuse a start
        LOG.warning("start", f"what the start may reach could not be listed: {exc}")
        return []


def host_ports(a):
    """Sorted unique ports from --allow-host-port."""
    ports = set()
    for value in a.allow_host_port:
        if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
            raise ValueError(f"--allow-host-port expects a TCP port 1-65535: {value!r}")
        ports.add(int(value))
    web = sorted(ports & set(web_service_ports()))
    if web:
        raise ValueError(f"--allow-host-port {web[0]}: the web interface (sandbox-ai web) listens there; no sandbox "
                         "may reach it")
    return sorted(ports)


def web_service_ports():
    """The ports every running `sandbox-ai web` listens on (sorted): each one's record (web/listen-PID.json; an
    older one's listen.json) while its PID lives. Two may run at once (loopback and the LAN): each port counts."""
    folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "web"
    try:
        paths = [folder / "listen.json", *folder.glob("listen-*.json")]
    except OSError:
        return []
    found = set()
    for path in paths:
        try:
            record = json.loads(path.read_text())
            port, pid = int(record["port"]), int(record["pid"])
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            continue
        if 1 <= port <= 65535:
            found.add(port)
    return sorted(found)


def pod_network(a):
    """Podman --network value; host ports are pasta's namespace-to-host forwards (-T), a --subnet/--subnet6 its
    address (-a), IPv4 prefix (-n; IPv6 is a /64) and gateway (-g), laid out as slirp4netns would."""
    if a.offline:
        return "none"
    if a.network:
        return a.network
    # macOS: pasta runs in podman machine's VM, its -T reaches the VM's loopback; the supervisor relays the ports
    # to the Mac instead (runtime.PortRelay).
    ports = host_ports(a) if not on_machine(a) else []
    options = list(PASTA_OPTIONS)
    if ports:
        index = options.index("-T")
        options[index:index + 2] = [part for port in ports for part in ("-T", str(port))]
    for key in ("subnet", "subnet6"):
        if getattr(a, key, None):
            net = ipaddress.ip_network(getattr(a, key))
            hosts = subnet_hosts(net)
            options += ["-a", hosts["address"], *(["-n", str(net.prefixlen)] if net.version == 4 else []),
                        "-g", hosts["gateway"]]
    if getattr(a, "subnet", None):
        # DNS sent to .3 goes to the host's resolvers, as slirp4netns's forwarder does; the firewall lets it
        # through only for --nameserver .3. Podman adds its own (169.254.1.1) only when none is given.
        options += ["--dns-forward", subnet_hosts(ipaddress.ip_network(a.subnet))["dns"]]
    return "pasta:" + ",".join(options)


def slirp_command(a, pid, ready_fd):
    """slirp4netns for the bwrap sandbox PID: its own network or --subnet (--cidr; slirp4netns places the sandbox
    at .100, its gateway at .2 and its DNS forwarder at .3), IPv6 always fd00::/64, the host's loopback hidden."""
    return ["slirp4netns", "--configure", "--disable-host-loopback", "--enable-sandbox", "--enable-seccomp",
            "--enable-ipv6", *([f"--cidr={a.subnet}"] if getattr(a, "subnet", None) else []),
            "--ready-fd", str(ready_fd), str(pid), "tap0"]


def subnet_hosts(net):
    """{"address", "gateway", "dns"} of the sandbox's own network NET (see SUBNET_HOSTS)."""
    return {name: str(net[offset if net.version == 4 else int(str(offset), 16)])
            for name, offset in SUBNET_HOSTS.items()}


def parse_subnet(value, version, option="--subnet"):
    """VALUE as the network of a sandbox's own default network, IPv4 or IPv6 by VERSION; ValueError says why not."""
    example = "10.20.0.0/24" if version == 4 else "fd20::/64"
    if not isinstance(value, str) or "%" in value or "/" not in value:
        raise ValueError(f"{option} expects a network with its prefix, such as {example}: {value!r}")
    try:
        net = ipaddress.ip_network(value)
    except ValueError:
        try:
            loose = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ValueError(f"{option} expects a network with its prefix, such as {example}: {value!r}") from None
        # slirp4netns refuses 10.0.2.100/24 as well: the network is named, not an address in it.
        raise ValueError(f"{option} {value}: give the network address, {loose}") from None
    if net.version != version:
        raise ValueError(f"{option} expects an IPv{version} network such as {example}: {value!r}")
    for kind in ("loopback", "link_local", "multicast"):
        if getattr(net, f"is_{kind}"):
            raise ValueError(f"{option} {value} is a {kind.replace('_', '-')} network; choose a private range")
    low, high = SUBNET_PREFIXES[version]
    if not low <= net.prefixlen <= high:
        raise ValueError(f"{option} {value}: the prefix must be " + (f"/{low}" if low == high else f"/{low} to /{high}")
                         + (" (the sandbox takes the 100th address)" if version == 4 else
                            " (pasta and slirp4netns configure the interface as a /64)"))
    if not any(net.subnet_of(r) for r in SUBNET_RANGES[version]):
        raise ValueError(f"{option} {value} is not a private range ({', '.join(map(str, SUBNET_RANGES[version]))}); "
                         "the sandbox would no longer reach the real addresses in it")
    return net


def host_routes(files=None):
    """Networks this host routes to (main table, default routes left out), from /proc/net/route and
    /proc/net/ipv6_route. An unreadable file counts as no routes: the check spares clashes, it guards nothing."""
    files = files or ROUTE_FILES
    routes = []
    for path, version in zip(files, (4, 6)):
        try:
            lines = Path(path).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines[1:] if version == 4 else lines:
            fields = line.split()
            try:
                if len(fields[1 if version == 4 else 0]) != (8 if version == 4 else 32):
                    continue
                if version == 4:
                    # Hexadecimal in the kernel's byte order.
                    address, mask = (int(fields[i], 16).to_bytes(4, sys.byteorder) for i in (1, 7))
                    net = ipaddress.IPv4Network((ipaddress.IPv4Address(address),
                                                 bin(int.from_bytes(mask, "big")).count("1")), strict=False)
                else:
                    net = ipaddress.IPv6Network((int(fields[0], 16), int(fields[1], 16)), strict=False)
            except (IndexError, ValueError, OverflowError):
                continue
            if net.prefixlen and not (net.is_loopback or net.is_link_local or net.is_multicast):
                routes.append(net)
    return routes


def defaults_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"


def load_user_defaults(a):
    """Your own defaults ({} without the file): {"version": 1, "subnet": CIDR, "subnet6": CIDR}. Options and
    the project file win over them; the administrator's policy binds them like options."""
    path = defaults_file()
    # The agent must not be able to choose the network of its next start.
    check_host_source(a, path, "The defaults file (XDG_CONFIG_HOME)")
    try:
        data = read_config_bytes(path, 1 << 16)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"{path}: {exc.strerror}") from None
    try:
        document = json.loads(data.decode())
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: invalid JSON") from None
    if not isinstance(document, dict) or document.get("version") != 1 or set(document) - DEFAULTS_KEYS:
        raise ValueError(f"{path}: expected version 1 with keys {sorted(DEFAULTS_KEYS)}")
    for key, version in (("subnet", 4), ("subnet6", 6)):
        if key in document:
            parse_subnet(document[key], version, f"{path}: {key}")
    if "mode" in document:
        MODES.check_name(document["mode"], f"{path}: mode")
    if "canaries" in document and not (document["canaries"] is False or isinstance(document["canaries"], list)):
        raise ValueError(f"{path}: canaries is false or a list of categories (credentials, home, workspace)")
    if "canaries" in document:
        load_canary_module().parse_categories(document["canaries"], f"{path}: canaries")
    if document.get("telemetry") == TELEMETRY_RETIRED:  # an old file's: ignored (telemetry is always off)
        note_telemetry_retired()
        document.pop("telemetry")
    if "telemetry" in document and document["telemetry"] not in TELEMETRY_LEVELS:
        raise ValueError(f"{path}: telemetry is one of {', '.join(TELEMETRY_LEVELS)}")
    if "name_style" in document and document["name_style"] not in NAME_STYLES:
        raise ValueError(f"{path}: name_style is one of {', '.join(NAME_STYLES)}")
    if "desktop_notifications" in document and document["desktop_notifications"] not in DESKTOP_NOTIFICATIONS:
        raise ValueError(f"{path}: desktop_notifications is one of {', '.join(DESKTOP_NOTIFICATIONS)}")
    if "preset_dirs" in document:
        # Extra preset directories (presets.py): their shape here; where they lie when one is used (presets.py and
        # check_host_sources: never where an agent writes).
        load_presets_module().check_dirs(document["preset_dirs"], f"{path}: preset_dirs")
    if "loop_guard" in document:  # the loop guard's layers for --llm starts (llm.py: LOOP_DEFAULTS)
        load_llm_module().check_loop(document["loop_guard"], str(path))
    if "llm_traffic" in document and document["llm_traffic"] not in RUNTIME.LLM_TRAFFIC_MODES:
        raise ValueError(f"{path}: llm_traffic is one of {', '.join(RUNTIME.LLM_TRAFFIC_MODES)}")
    if "llm_traffic_limit" in document:
        check_traffic_limit(document["llm_traffic_limit"], f"{path}: llm_traffic_limit")
    if "fix_questions" in document and document["fix_questions"] not in FIXES.QUESTIONS:
        raise ValueError(f"{path}: fix_questions is one of {', '.join(FIXES.QUESTIONS)} (what a start lacks: one "
                         "question for everything, or one per fix)")
    if "file_reads" in document and document["file_reads"] not in RUNTIME.FILE_READS:
        raise ValueError(f"{path}: file_reads is one of {', '.join(RUNTIME.FILE_READS)} (how the file log of --log-dir "
                         "keeps reads: summed per folder, each file, none)")
    if "log_days" in document:
        check_log_days(document["log_days"], f"{path}: log_days")
    storage_keys = sorted(key for key in document if isinstance(key, str) and key.startswith("storage_"))
    if storage_keys:  # (storage.py: the storage check's settings and its rules, each off unless set)
        spec = importlib.util.spec_from_file_location("sandbox_storage_defaults", HERE / "storage.py")
        storage_settings = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(storage_settings)
        for key in storage_keys:
            why = storage_settings.check_value(key, document[key])
            if why:
                raise ValueError(f"{path}: {why}")
    if "budget" in document:
        try:
            budget_usd(document["budget"], f"{path}: budget")
        except argparse.ArgumentTypeError as exc:
            raise ValueError(str(exc)) from None
    if "ask_timeout" in document and (isinstance(document["ask_timeout"], bool) or not isinstance(
            document["ask_timeout"], int) or not ASK_TIMEOUT_RANGE[0] <= document["ask_timeout"] <= ASK_TIMEOUT_RANGE[1]):
        raise ValueError(f"{path}: ask_timeout is a number of seconds, {ASK_TIMEOUT_RANGE[0]}-{ASK_TIMEOUT_RANGE[1]} "
                         "(how long a request waits for the answer to its question)")
    if "package_min_age_days" in document:  # the package guard's minimum release age (0: no age check)
        low, high = RUNTIME.PACKAGE_AGE_RANGE
        if isinstance(document["package_min_age_days"], bool) or not isinstance(document["package_min_age_days"], int) \
                or not low <= document["package_min_age_days"] <= high:
            raise ValueError(f"{path}: package_min_age_days is a number of days, {low}-{high} (the package guard asks "
                             "before a download of a version younger than that; 0: no age check)")
    if "monitor_interval" in document:  # the system monitor's seconds between two samples (monitor.py; 0: off)
        value = document["monitor_interval"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                value == 0 or load_monitor_module().INTERVAL_RANGE[0] <= value <= load_monitor_module().INTERVAL_RANGE[1]):
            raise ValueError(f"{path}: monitor_interval is the seconds between two samples of the system monitor, 1 to "
                             "3600 (default 10), or 0: no recording")
    for key in UPDATE_KEYS:  # the update mode (updates.py)
        if key in document:
            load_updates_module().check_setting(key, document[key], str(path))
    for key in CHECKPOINT_KEYS:  # the workspace checkpoints (checkpoints.py)
        if key in document:
            load_checkpoints_module().check_setting(key, document[key], str(path))
    if "connection_bodies" in document and document["connection_bodies"] not in RUNTIME.CONNECTION_BODIES:
        raise ValueError(f"{path}: connection_bodies is one of {', '.join(RUNTIME.CONNECTION_BODIES)} (text: a binary "
                         "answer is logged by its size, digest and first bytes; all: its first 64 KiB too)")
    if "agent_intro" in document and not isinstance(document["agent_intro"], bool):
        # (intro.py: /sandbox/session/INTRO.md and the line that names it in the agent's instructions)
        raise ValueError(f"{path}: agent_intro is true or false (false: a start tells its agent nothing about what its "
                         "sandbox has - no INTRO.md, no line in its instructions; --intro turns it on for one start)")
    if "hide_secrets" in document and not isinstance(document["hide_secrets"], bool):
        # (session_inspect.py: true leaves logins and keys out of the file views; false or none: listed and marked)
        raise ValueError(f"{path}: hide_secrets is true or false (true: files that look like logins or keys are left "
                         "out of the file views - sandbox-ai session files and memory, the app's and the web's)")
    if "safeguard_mute" in document:  # kinds of safeguard warnings never announced (still recorded)
        load_safeguards_module().check_mute(document["safeguard_mute"], f"{path}: safeguard_mute")
    days = document.get("keep_days", 1)
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 36500:
        raise ValueError(f"{path}: keep_days is a number of days (1 or more): kept starts unused for longer expire; "
                         "0: they never expire (without it: 90 days, applied by sandbox-ai gc and its daily pass)")
    return document


def apply_subnet_defaults(a, values, origin):
    """Fill --subnet/--subnet6 from VALUES (your defaults file, the policy) where no option chose them and the
    run has a default network to give them to: not --offline, not --network, IPv6 only with Podman."""
    if a.offline or a.network:
        return
    for key in ("subnet", "subnet6"):
        if getattr(a, key) is None and values.get(key) and (key == "subnet" or a.backend == "podman"):
            setattr(a, key, values[key])
            a.subnet_origin[key] = origin


def subnet_label(a, key):
    origin = getattr(a, "subnet_origin", {}).get(key)
    return f"--{key} {getattr(a, key)}" + (f" (from {origin})" if origin else "")


def subnet_destinations(a, addresses=()):
    """(label, network) of every destination the run allows by address, ADDRESSES (resolved hosts) included."""
    found = [(f"--allow {v}", ipaddress.ip_network(v, strict=False)) for v in a.allow]
    found += [(f"--allow-tcp {addr}:{port}", ipaddress.ip_network(addr)) for addr, port in tcp_targets(a)]
    found += [(f"the nameserver {ns}", ipaddress.ip_network(ns)) for ns in nameservers(a)]
    found += [(f"{net} (an allowed host's network)", ipaddress.ip_network(net))
              for net in host_networks(allowed_hosts(a))]
    found += [(f"{addr} (an allowed host's address)", ipaddress.ip_network(addr)) for addr in addresses]
    return found


def check_subnets(a, addresses=()):
    """A chosen --subnet/--subnet6 fits this run's default network and stays clear of the host's routes and of
    every destination the run allows: an allowed address inside the sandbox's own network would never be reached
    (it is the sandbox's own link, gateway or DNS forwarder), and widening there must not reach those instead.
    The one exception is --nameserver at .3, the backend's DNS forwarder to the host's resolvers."""
    chosen = {key: getattr(a, key, None) for key in ("subnet", "subnet6") if getattr(a, key, None)}
    if not chosen:
        return
    if a.network:
        raise ValueError("--subnet/--subnet6 set the default network; with --network the Podman network decides "
                         "(use --ip/--ip6)")
    if "subnet6" in chosen and a.backend == "bwrap":
        raise ValueError(f"{subnet_label(a, 'subnet6')}: slirp4netns (bwrap) has no IPv6 option and always uses "
                         f"{SLIRP_SUBNETS[1]}; use the Podman backend")
    routes = host_routes() if local_engine(a) else []  # a remote Podman's (or podman machine's) are not visible here
    destinations = subnet_destinations(a, addresses)
    for key, value in chosen.items():
        net = parse_subnet(value, 4 if key == "subnet" else 6, subnet_label(a, key))
        for route in routes:
            if route.version == net.version and route.overlaps(net):
                raise ValueError(f"{subnet_label(a, key)} overlaps {route}, a network this host routes to; the "
                                 "sandbox could no longer reach it. Choose another range")
        forwarder = ipaddress.ip_network(subnet_hosts(net)["dns"])
        for label, dest in destinations:
            if dest.version != net.version or not dest.overlaps(net):
                continue
            if label.startswith("the nameserver") and dest == forwarder:
                continue
            raise ValueError(f"{label} lies inside the sandbox's own network, {subnet_label(a, key)}: it would "
                             "reach the sandbox's link, gateway or DNS forwarder, not that destination. Choose "
                             "another range")


def in_subnet(a, address):
    """ADDRESS lies inside a chosen --subnet/--subnet6 (an invalid one is reported by check_subnets)."""
    try:
        address = ipaddress.ip_address(address)
        return any(address in ipaddress.ip_network(v) for v in (a.subnet, a.subnet6) if v)
    except ValueError:
        return False


def sandbox_network(a):
    """The sandbox's own addresses on its default network (the plan, the start's log); None without one."""
    if a.offline or a.network:
        return None
    result = {}
    for key, version in (("subnet", 4), ("subnet6", 6)):
        value = getattr(a, key, None)
        origin = getattr(a, "subnet_origin", {}).get(key) or ("--" + key if value else None)
        if value is None and a.backend == "bwrap":
            value, origin = SLIRP_SUBNETS[version == 6], "slirp4netns"
        if value is None:
            result[f"ipv{version}"] = {"subnet": None, "from": "the host's addresses (pasta)"}
            continue
        hosts = subnet_hosts(ipaddress.ip_network(value))
        if version == 6 and a.backend == "bwrap":
            hosts["address"] = None  # slirp4netns: from its router advertisements
        if version == 6 and a.backend == "podman":
            hosts["dns"] = None  # pasta forwards DNS for IPv4 only (--dns-forward at .3)
        result[f"ipv{version}"] = {"subnet": value, **hosts, "from": origin}
    return result


def read_private_file(path, option, limit):
    """Content of a private file: regular, not a symlink, owned by the user, mode 0600 or stricter.
    At most LIMIT + 1 bytes are read, so a longer file shows as longer than LIMIT.
    The content is never part of an error message."""
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        reason = "it is a symlink" if exc.errno == 40 else exc.strerror
        raise ValueError(f"{option}: cannot read {path}: {reason}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{option}: {path} is not a regular file")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"{option}: {path} must belong to you and be private: chmod 600 {shlex.quote(str(path))}")
        if info.st_nlink > 1:
            # Another name for the same file could lie in a tree the agent can write.
            raise ValueError(f"{option}: {path} has other hard links; use a file of its own")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    return data


def read_token_file(path, pattern, option):
    """A token from a private file (see read_private_file); the token is never part of an error message. A token
    file of the wizard's moved into sandbox-ai secrets (since 0.1.98) names its secret: read from there (locked: the
    message says how to unlock). A plain one in the wizard's folder moves there now when that needs nobody to type
    anything; a plain file of the user's own is read as it is, with a warning."""
    path = Path(path).expanduser()
    data = read_private_file(path, option, 4096)
    store = load_secret_store_module()
    if len(data) <= 4096 and store.token_reference(data):
        token = store.read_moved_token(path, data, option)
    else:
        try:
            token = data.decode("ascii").strip()
        except UnicodeDecodeError:
            token = ""
    if len(data) > 4096 or not re.fullmatch(pattern, token):
        raise ValueError(f"{option}: {path} does not hold a token (one line of letters, digits and _.~+/=-)")
    if not store.token_reference(data) and not store.move_token_on_use(path, option):
        store.warn_plain_token(path, option)
    return token


# Tokens of Gitea, Forgejo, GitLab, GitHub and others: one line of characters that
# cannot add fields to git's credential protocol.
GIT_TOKEN = r"[A-Za-z0-9_.~+/=-]{8,512}"
GIT_TOKEN_USER = r"[A-Za-z0-9_.-]{1,64}"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def git_token_values(a):
    """[(kind, URL=REF)] of --git-token-env (kind "env"), --git-token-file ("file") and --git-token-secret
    ("secret")."""
    return ([("env", value) for value in a.git_token_env] + [("file", value) for value in a.git_token_file]
            + [("secret", value) for value in getattr(a, "git_token_secret", [])])


def git_token_credential(value, kind="env"):
    """Parse URL=NAME (--git-token-env) or URL=FILE (--git-token-file); the token is read by git_tokens()."""
    option = f"--git-token-{kind}"
    # The URL cannot contain "=", so the first one separates it from the name or path.
    url, sep, ref = value.partition("=")
    if kind == "env" and (not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref)):
        raise ValueError(f"{option} expects URL=NAME, a variable NAME without a value: {value!r}")
    if kind == "file" and (not sep or not ref or any(ord(c) < 32 for c in ref)):
        raise ValueError(f"{option} expects URL=FILE: {value!r}")
    if kind == "secret" and (not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", ref)):
        raise ValueError(f"{option} expects URL=SECRET, the name of a secret (sandbox-ai secrets list): {value!r}")
    credential = git_server(url, option)
    if credential["user"] is not None and not re.fullmatch(GIT_TOKEN_USER, credential["user"]):
        raise ValueError(f"{option}: user may contain only letters, digits, '.', '_' and '-': {url!r}")
    return {**credential, "user": credential["user"] or "x-access-token", kind: ref}


def token_proxy_target(value, kind="env"):
    """The credential of a --git-token-KIND URL=REF whose token --token-proxy adds on the wire; ValueError when it
    cannot: plain http to anything but this host's loopback (the app's Board asks this same rule)."""
    credential = git_token_credential(value, kind)
    if not credential["url"].startswith("https://") and not credential["loopback"]:
        raise ValueError(f"--token-proxy: {credential['url']} is plain http; the proxy adds tokens only to "
                         "HTTPS servers whose certificate it checked, or to a server on this host's loopback")
    return credential


def git_server(url, option):
    """{url, user (None without one), host, port, loopback} of http(s)://[USER@]HOST[:PORT]; plain http
    only on loopback. "url" is the origin git matches credential.<url> against."""
    match = re.fullmatch(r"(https?)://(?:([^@/]*)@)?([A-Za-z0-9.-]+|\[::1\])(?::([0-9]{1,5}))?/?", url)
    if not match:
        raise ValueError(f"{option} expects http(s)://[USER@]HOST[:PORT], without a path: {url!r}")
    scheme, user, host, port = match[1], match[2], match[3].lower(), match[4]
    if user is not None and ":" in user:
        # A password in the URL would sit in the command line, a project file or the list: the token comes from
        # its own place (a variable, a file, sandbox-ai secrets) instead.
        raise ValueError(f"{option}: no password in the URL; the token comes from its variable, file or secret: "
                         f"{scheme}://{user.split(':', 1)[0]}:***@{host}")
    if not port or not 1 <= int(port) <= 65535:
        if port:
            raise ValueError(f"{option}: port out of range: {url!r}")
        port = 443 if scheme == "https" else 80
    port, loopback = int(port), host in LOOPBACK_HOSTS
    if host != "[::1]" and (len(host) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split("."))):
        raise ValueError(f"{option}: invalid host name: {url!r}")
    if scheme == "http" and not loopback:
        raise ValueError(f"{option}: plain http would send the token unencrypted; use https: {url!r}")
    # Git matches credential.<url> against the remote without user; default ports are normalized away.
    origin = f"{scheme}://{host}" + ("" if port == {"http": 80, "https": 443}[scheme] else f":{port}")
    return {"url": origin, "user": user, "host": host, "port": port, "loopback": loopback}


def loopback_reachable(a):
    """Whether the sandbox reaches the host's loopback (pasta's host forwards, --allow-host-port)."""
    return a.backend == "podman" and not a.network and not a.offline


GIT_CREDENTIALS_LIMIT = 1 << 16
# One line of git-credential-store: user and password are percent-encoded, so neither holds ":", "@" or "/".
# As git parses it, user and password end at the first ":" and "@"; anything from "/", "?" or "#"
# on is the path (credential.useHttpPath), which git ignores here.
GIT_CREDENTIALS_LINE = re.compile(r"(https?)://([^:@/?#]*):([^@/?#]*)@([^@/?#]+)(?:[/?#].*)?")


def git_credentials_line(line):
    """(server, user, password) of one line of a git-credential-store file; ValueError names no secret."""
    match = GIT_CREDENTIALS_LINE.fullmatch(line)
    if not match:
        raise ValueError("not a login (https://USER:PASSWORD@HOST[:PORT])")
    try:
        user, password = (urllib.parse.unquote(part, errors="strict") for part in (match[2], match[3]))
    except UnicodeDecodeError:
        raise ValueError("user or password is not UTF-8") from None
    if not user or not password:
        raise ValueError("user or password is empty")
    if len(user) > 256 or len(password) > 4096:
        raise ValueError("user or password is too long")
    # A line break would add fields to git's credential protocol.
    if any(ord(c) < 32 or ord(c) == 127 for c in user + password):
        raise ValueError("user or password contains control characters")
    # Only scheme, host and port reach error messages, never the login.
    try:
        host = urllib.parse.unquote(match[4])  # git's store writes HOST:PORT as HOST%3aPORT, [::1] as %5b%3a%3a1%5d
        if any(c in host for c in "@/?#"):
            raise ValueError("invalid host name")
        server = git_server(f"{match[1]}://{host}", "--git-credentials")
    except ValueError as exc:
        raise ValueError(str(exc).removeprefix("--git-credentials: ").replace(", without a path", "")
                         .replace("the token", "the login")) from None
    return server, user, password


def git_credentials_file(a):
    """([(credential, password)], notes) from --git-credentials, a file in git-credential-store's format:
    one https://USER:PASSWORD@HOST[:PORT][/PATH] per line, user and password percent-encoded.
    As in git, the first line for a server wins and a path does not narrow it; a --git-token-* option
    for the same server wins over the file. Lines the sandbox cannot use are skipped with a note that
    names the line, never its content. The file is read once per parsed argument set."""
    if not a.git_credentials:
        return [], []
    cached = getattr(a, "_git_credentials", None)
    if cached is not None and cached[0] == (a.backend, a.network, a.offline):
        return cached[1]
    option, path = "--git-credentials", Path(os.path.normpath(Path(a.git_credentials).expanduser().absolute()))
    data = read_private_file(path, option, GIT_CREDENTIALS_LIMIT)
    if len(data) > GIT_CREDENTIALS_LIMIT:
        raise ValueError(f"{option}: {path} is larger than {GIT_CREDENTIALS_LIMIT // 1024} KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{option}: {path} is not UTF-8 text") from None
    tokens = set()
    for kind, value in git_token_values(a):
        try:
            tokens.add(git_token_credential(value, kind)["url"])
        except ValueError:
            pass  # reported by git_tokens()
    entries, notes, seen = [], [], set()
    for number, line in enumerate(text.split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        where = f"{path} line {number}"
        try:
            server, user, password = git_credentials_line(line)
        except ValueError as exc:
            notes.append(f"{where} skipped: {exc}")
            continue
        url = server["url"]
        if url in tokens:
            notes.append(f"{where} skipped: {url} has a --git-token-* option")
        elif url in seen:
            notes.append(f"{where} skipped: an earlier line holds the login for {url}")
        elif server["loopback"] and not loopback_reachable(a):
            notes.append(f"{where} skipped: {url} is on the host's loopback, which only the Podman backend "
                         "with its default network reaches")
        else:
            seen.add(url)
            entries.append(({**server, "user": user, "credentials": str(path), "line": number}, password))
            if not server["loopback"] and server["port"] != 443:
                # The host is allowed by name on HTTPS's port only.
                notes.append(f"{where}: {url} uses port {server['port']}, which the sandbox reaches only "
                             f"with --allow-tcp IP:{server['port']}")
    if not entries:
        raise ValueError(f"{option}: {path} holds no login the sandbox can use"
                         + "".join(f"\n  {printable(note)}" for note in notes))
    a._git_credentials = ((a.backend, a.network, a.offline), (entries, notes))
    return entries, notes


def git_tokens(a):
    """[(credential, token)] for --git-token-env, --git-token-file and --git-credentials;
    tokens are never printed."""
    result, seen = [], {}
    for kind, value in git_token_values(a):
        credential = git_token_credential(value, kind)
        key = (credential["user"], kind, credential[kind])
        if credential["url"] in seen:
            # The same entry from the command line and the project file is fine; two tokens are not.
            if seen[credential["url"]] == key:
                continue
            raise ValueError(f"--git-token-{kind}: {credential['url']} given twice with different values")
        seen[credential["url"]] = key
        if credential["loopback"] and (a.backend != "podman" or a.network or a.offline):
            raise ValueError(f"--git-token-{kind}: {credential['url']} is on the host's loopback, which only the "
                             "Podman backend with its default network reaches (--allow-host-port)")
        result.append((credential, git_token_value(credential, kind)))
    return result + git_credentials_file(a)[0]


def token_problem(token):
    """Why TOKEN (a Git token as kept: already stripped of the blanks and line ends around it) is no access token, or
    None when it looks like one: never the value itself, only what is wrong with it (said where a secret, a file or a
    variable holds something else: a USER:TOKEN pair, a second line, a space inside, too short or too long)."""
    if re.fullmatch(GIT_TOKEN, token):
        return None
    user, colon, rest = token.partition(":")
    if colon and re.fullmatch(GIT_TOKEN_USER, user) and re.fullmatch(GIT_TOKEN, rest):
        return "it holds USER:TOKEN - keep only the token, the user goes into the URL or --user"
    if any(c in token for c in "\r\n"):
        return "it holds more than one line"
    if any(c.isspace() for c in token):
        return "it holds a space"
    if len(token) < 8:
        return f"it is {len(token)} characters long, a token has at least 8"
    if len(token) > 512:
        return f"it is {len(token)} characters long, a token has at most 512"
    return "it holds a character an access token never has (one line of letters, digits and _.~+/=-)"


def git_token_value(credential, kind):
    """The token of CREDENTIAL (git_token_credential) of --git-token-KIND: from the file, the secret store or
    the host variable it names; checked to look like a token. Never printed."""
    if kind == "file":
        return read_token_file(credential["file"], GIT_TOKEN, "--git-token-file")
    if kind == "secret":
        token = load_secret_store_module().get(credential["secret"])
        if token is None:
            raise ValueError(f"--git-token-secret: no secret {credential['secret']}; keep one with: "
                             f"sandbox-ai secrets set {credential['secret']}")
        token = token.strip()  # (a line end or blanks around it, kept by `secrets set` from a pipe: never the token's)
        if not re.fullmatch(GIT_TOKEN, token):
            user, colon, rest = token.partition(":")
            if colon and re.fullmatch(GIT_TOKEN_USER, user) and re.fullmatch(GIT_TOKEN, rest):
                # USER:TOKEN kept as one secret: the user goes into the URL, the secret holds the token alone.
                raise ValueError(f"the secret {credential['secret']} holds USER:TOKEN; keep only the token in it "
                                 f"(sandbox-ai secrets set {credential['secret']}) and name the user in the URL: "
                                 f"{credential['url'].replace('://', '://' + user + '@', 1)}={credential['secret']}")
            raise ValueError(f"the secret {credential['secret']} does not look like an access token (one line of "
                             "letters, digits and _.~+/=-, 8 to 512 characters)")
        return token
    name = credential["env"]
    if name not in os.environ:
        raise ValueError(f"Host variable is not set: {name}")
    if not re.fullmatch(GIT_TOKEN, os.environ[name]):
        raise ValueError(f"{name} does not look like an access token "
                         "(one line of letters, digits and _.~+/=-)")
    return os.environ[name]


# Git logins added during the run (`sandbox-ai git-logins give`, `net allow --git-token-*`) go to the run's connection
# proxy (control op "git-logins": no token in the sandbox). Only where the proxy cannot log in - no proxy, a server
# it does not inspect - and only when the human says so (--in-sandbox), the host writes one into the run's session
# directory, which the sandbox sees read-only (the agent can read it) and which is removed when the run ends; this
# helper, configured at every start, reads them when Git asks.
RUNTIME_GIT_TOKENS = "git-tokens.json"
RUNTIME_GIT_HELPER = "!python3 -I /sandbox/runtime.py --internal-git-credential"


_git_logins_module = None


class RunningLauncher:
    """This launcher as its submodules see it: each name looked up here when it is used (what a caller replaced
    included). Handing it over spares them loading agent_sandbox.py a second time (about 30 ms per start)."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


def load_git_logins_module():
    global _git_logins_module
    if _git_logins_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_git_logins", HERE / "git_logins.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _git_logins_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._launcher = RunningLauncher()
                _git_logins_module = loaded
    return _git_logins_module


def registry_git_tokens(a):
    """([(credential, token)], notes) of sandbox-ai git-logins for this run: with the connection proxy and
    --git-logins on, for the servers no --git-token-* option of the start names. Read once per start."""
    cached = getattr(a, "_registry_git_tokens", None)
    if cached is not None:
        return cached
    result = ([], [])
    # An MCP server's sandbox never gets them: a server must not push with your login.
    if getattr(a, "git_logins", "on") == "on" and proxy_enabled(a) and not getattr(a, "mcp_socket_dir", None):
        named = set()
        for kind, value in git_token_values(a):
            with contextlib.suppress(ValueError):
                named.add(git_token_credential(value, kind)["url"])
        _, identity = identity_of(a)
        result = load_git_logins_module().credentials(
            skip=named, entries=identity.get("git", []) if identity is not None else None,
            origin=f"identity {a.identity}" if identity is not None else "git-logins")
    a._registry_git_tokens = result
    return result


_identities_module = None


def load_identities_module():
    global _identities_module
    if _identities_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_identities", HERE / "identities.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _identities_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._modules["agent_sandbox"] = RunningLauncher()
                loaded._modules["git_logins"] = load_git_logins_module()
                loaded._modules["publish_logins"] = load_publish_logins_module()
                _identities_module = loaded
    return _identities_module


def identity_of(a):
    """(NAME, identity) of --identity NAME, or (None, None) for the identity default; ValueError for an unknown or
    wrong one. Read once per start."""
    name = getattr(a, "identity", None)
    if not name:
        return None, None
    cached = getattr(a, "_identity", None)
    if cached is None:
        module = load_identities_module()
        if not module.NAME.fullmatch(name) or name == module.DEFAULT:
            if name == module.DEFAULT:
                return None, None  # the same as no --identity
            raise ValueError(f"--identity {name!r}: lower-case letters, digits, - and _")
        try:
            cached = a._identity = (name, module.get(name))
        except ValueError as exc:
            if name in module.load():
                raise
            # An identity that is not there: offered to be made (empty: no logins, a commit name of its own to set).
            raise FIXES.Missing(str(exc), [FIXES.fix(
                "identity", name, f"the identity {name} is not on this host",
                "make it, empty (no logins; give it a commit name and logins later: sandbox-ai identities)",
                [["sandbox-ai", "identities", "add", "--description=made for a start (no logins)", "--", name]],
                duration="seconds")]) from None
    return cached


def check_identity(a):
    """--identity: a known identity, not with --git-identity (whose name would it be?), never for an MCP server."""
    name, identity = identity_of(a)
    if identity is None:
        return
    if a.git_identity:
        raise ValueError(f"--identity {name} brings its own commit name and address; drop --git-identity")
    if getattr(a, "mcp_socket_dir", None):
        raise ValueError("--identity is for an agent's sandbox, not an MCP server's")


def identity_plan(a):
    """The dry run's view of --identity: names, address, servers and kinds, never a secret."""
    name, identity = identity_of(a)
    if identity is None:
        return None
    return {"name": name, "commit_name": identity.get("name"), "commit_email": identity.get("email"),
            "git": [entry["url"] for entry in identity.get("git", [])],
            "publish": [entry["kind"] for entry in identity.get("publish", [])]}


def publish_logins_of(a):
    """The publish logins --publish uses: the identity's, or None for sandbox-ai publish-logins' (default)."""
    _, identity = identity_of(a)
    return identity.get("publish", []) if identity is not None else None


def proxied_git_tokens(a):
    """[(credential, token)] the connection proxy adds on the wire: --token-proxy's, then sandbox-ai git-logins'."""
    return (git_tokens(a) if a.token_proxy else []) + registry_git_tokens(a)[0]


def git_credential_helper(user_variable, token_variable):
    """Answers get requests with the user and the token in the two variables, read at use and never stored."""
    return (f"""!f() {{ test "$1" = get && printf 'username=%s\\npassword=%s\\n' "${user_variable}" """
            f""""${token_variable}"; }}; f""")


def host_git_identity():
    """(user.name, user.email) from the host's git configuration."""
    identity = []
    for key in ("user.name", "user.email"):
        try:
            # Global scope from HOME: a workspace's .git/config is written by the agent.
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            result = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True,
                                    timeout=10, stdin=subprocess.DEVNULL, cwd=Path.home(), env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"--git-identity: cannot run git on the host: {exc}") from None
        value = result.stdout.strip() if result.returncode == 0 else ""
        if not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"--git-identity: host git has no usable {key}; set it with git config --global {key} ...")
        identity.append(value)
    return tuple(identity)


def git_environment(a):
    """Git settings for the agent as GIT_CONFIG_* variables: no file in the sandbox HOME is written,
    and they take precedence over any ~/.gitconfig the agent left in a persistent HOME."""
    entries, env = [], {}
    if a.git_identity:
        name, email = host_git_identity()
        entries += [("user.name", name), ("user.email", email)]
    _, identity = identity_of(a)
    if identity is not None:
        entries += [(key, identity[field]) for key, field in (("user.name", "name"), ("user.email", "email"))
                    if identity.get(field)]
    if not a.token_proxy:
        # First, so that a server's own helpers below (the empty value resets the list) win over it.
        entries.append(("credential.helper", RUNTIME_GIT_HELPER))
    for index, (credential, token) in enumerate([] if a.token_proxy else git_tokens(a)):
        variable, user = f"SANDBOX_GIT_TOKEN_{index}", f"SANDBOX_GIT_USER_{index}"
        key = f"credential.{credential['url']}.helper"
        # The empty value drops helpers from config files (e.g. one the agent planted) for this URL first.
        entries += [(key, ""), (key, git_credential_helper(user, variable))]
        env[variable], env[user] = token, credential["user"]
        if credential["url"] == "https://github.com":
            env["GH_TOKEN"] = token  # the gh CLI
    if privacy_hooks_apply(a):
        entries.append(("core.hooksPath", PRIVACY_HOOKS))
    if getattr(a, "helper_of", None):
        # A helper works in the lead's /work: what the lead writes into /work/.git must not run in the helper's
        # sandbox when its agent runs git (Claude Code and Codex run git status at once). Command-line settings
        # win over the repository's: no fsmonitor, no repository hooks, no .gitattributes filters (GIT_ATTR_SOURCE,
        # Git 2.42 and newer). Not every Git setting can be switched off this way (README: helpers, limits).
        entries.append(("core.fsmonitor", "false"))
        if not privacy_hooks_apply(a):
            entries.append(("core.hooksPath", "/dev/null"))
        env["GIT_ATTR_SOURCE"] = EMPTY_TREE
    if entries:
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    return env


def tcp_targets(a):
    """[(address, port)] from --allow-tcp."""
    result = []
    for value in a.allow_tcp:
        match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([0-9.]+):(\d{1,5})", value)
        if not match:
            raise ValueError(f"--allow-tcp expects IP:PORT or [IPv6]:PORT: {value!r}")
        address, port = ipaddress.ip_address(match[1] or match[3]), int(match[2] or match[4])
        if not 1 <= port <= 65535:
            raise ValueError(f"--allow-tcp port out of range: {value!r}")
        result.append((str(address), port))
    return result


def config_store():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "trusted.json"


def trusted_configs(note=None):
    """{path: sha256 hex digest} of the trusted project files (config_store). A damaged store (no JSON object, an
    entry that is no path with a 64-hex digest: edited by hand, cut short) trusts nothing it cannot read: such
    entries are left out, each told to NOTE(text) (default: stderr); never a crash in a caller."""
    note = note or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    try:
        store = json.loads(config_store().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        note(f"the trusted list {config_store()} cannot be read; nothing in it is trusted")
        return {}
    if not isinstance(store, dict):
        note(f"the trusted list {config_store()} is damaged (no JSON object); nothing in it is trusted")
        return {}
    found = {}
    for key, digest in store.items():
        if isinstance(key, str) and os.path.isabs(key) and isinstance(digest, str) and \
                re.fullmatch(r"[0-9a-f]{64}", digest):
            found[key] = digest
        else:
            note(f"skipped a damaged entry of the trusted list: {printable(key)[:200]}")
    return found


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def config_key(path):
    """Trust key: the real directory plus the file name itself (never a link target)."""
    path = Path(path).expanduser().absolute()
    return path.parent.resolve() / path.name


def read_config_bytes(path, limit=CONFIG_LIMIT):
    """Contents of a regular, non-symlink file of bounded size, read once."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == 40:  # ELOOP: the final component is a symlink
            raise ValueError(f"{path} is a symlink; refusing to use it as a configuration") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"{path} is larger than {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_filelock = None


def settings_lock(file):
    """The lock of a read-modify-write of the settings file FILE (filelock.py): the CLI, the app and the web interface
    may change it at the same time, and none may lose the other's change."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _filelock = loaded
    return _filelock.locked(file)


def trust_config(path, digest=None):
    """Record a project configuration as trusted: the hash of the bytes reviewed, or of the file now. Under the
    trust store's lock: a `wizard forget` at the same time (the app and the web side by side) is never undone."""
    key = config_key(path)
    with settings_lock(config_store()):
        trusted = trusted_configs()
        trusted[str(key)] = digest or hashlib.sha256(read_config_bytes(key)).hexdigest()
        save_trusted(trusted)


def forget_config(path):
    """Remove a configuration from the trusted list (under the trust store's lock); True if it was there."""
    with settings_lock(config_store()):
        trusted = trusted_configs()
        if trusted.pop(str(config_key(path)), None) is None:
            return False
        save_trusted(trusted)
        return True


def save_trusted(trusted):
    store = config_store()
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".trusted-", dir=store.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(trusted, stream, indent=2)
        os.replace(temporary, store)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load_project_config(a):
    """(path, document) of the trusted project configuration, or (None, None)."""
    if a.no_config:
        return None, None
    path = config_key(a.config or Path(a.workspaces[0] or ".") / CONFIG_NAME)
    # The agent must not be able to write what it would trust at its next start.
    check_host_source(a, config_store(), "The trust store (XDG_CONFIG_HOME)",
                      trees=[Path(w).expanduser().resolve() for w in a.workspaces if w])
    trusted = trusted_configs()
    if not path.is_symlink() and not path.exists():
        if a.config:
            raise ValueError(f"Configuration not found: {path}")
        if str(path) in trusted:
            # Deleting a restrictive file must not silently drop its restrictions.
            raise ValueError(f"The trusted project configuration {path} is missing; restore it, start with "
                             f"--no-config, or run: sandbox-ai wizard forget {shlex.quote(str(path))}")
        return None, None
    # Hash and parse the very same bytes: no window to swap the file in between.
    data = read_config_bytes(path)
    known = trusted.get(str(path))
    if known != hashlib.sha256(data).hexdigest():
        state = "changed since it was trusted" if known else "not trusted"
        raise ValueError(f"Project configuration {path} is {state}. Review it, then run: "
                         f"sandbox-ai wizard trust {shlex.quote(str(path))} (or start with --no-config)")
    try:
        document = json.loads(data.decode())
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: invalid JSON") from None
    if not isinstance(document, dict) or document.get("version") != 1 or set(document) - CONFIG_KEYS:
        raise ValueError(f"{path}: expected version 1 with keys {sorted(CONFIG_KEYS)}")
    if document.get("agent") != a.agent:
        other = document.get("agent")
        if other in AGENT_PROGRAMS:
            raise ValueError(f"{path} is for {other}: start it with sandbox-ai {other}, or start {a.agent} with "
                             "--no-config")
        raise ValueError(f"{path} names no agent (\"agent\": {', '.join(sorted(AGENT_PROGRAMS))}); add one, or start "
                         "with --no-config")
    for key in ("options", "agent_args", "workspaces"):
        values = document.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{path}: {key} must be a list of strings")
    # Short forms (-S/dir, -Dn) are checked as what they are.
    document["options"] = canonical_options(document.get("options", []))
    for value in document.get("options", []):
        if any(value == flag or value.startswith(flag + "=") for flag in CONFIG_FORBIDDEN):
            hint = ' (the key "mode" chooses the security mode)' if value.split("=", 1)[0] == "--mode" else ""
            raise ValueError(f"{path}: option {value} is not allowed in a project configuration{hint}")
    options = document.get("options", [])
    for index, value in enumerate(options):
        if value == "--llm-traffic=all" or value == "--llm-traffic" and options[index + 1:index + 2] == ["all"]:
            raise ValueError(f"{path}: --llm-traffic all is not allowed in a project configuration: only your "
                             "command line or ~/.config/sandbox-ai/defaults.json records the cloud providers' traffic")
    if "mode" in document:
        MODES.check_name(document["mode"], f"{path}: mode")
    # Relative paths would resolve against wherever the launcher is started, not the file.
    relative = relative_paths(document.get("options", [])) + [
        w for w in document.get("workspaces", []) if not os.path.isabs(os.path.expanduser(w))]
    if relative:
        raise ValueError(f"{path}: use absolute paths (or ~/...), not: {', '.join(relative)}")
    if any("," in value for value in document.get("workspaces", [])):
        raise ValueError(f"{path}: workspaces cannot contain commas")
    if "rules" in document:
        try:
            RUNTIME.Ruleset.validated(document["rules"], f"{path}: rules")
        except RUNTIME.RuleError as exc:
            raise ValueError(str(exc)) from None
    if not isinstance(document.get("log_debug", False), bool):
        raise ValueError(f"{path}: log_debug is true or false")
    if "canaries" in document:
        if not (document["canaries"] is False or isinstance(document["canaries"], list)):
            raise ValueError(f"{path}: canaries is false or a list of categories (credentials, home, workspace)")
        load_canary_module().parse_categories(document["canaries"], f"{path}: canaries")
    if not isinstance(document.get("keep", True), bool):
        raise ValueError(f"{path}: keep is true or false")
    if "budget" in document:
        try:
            budget_usd(document["budget"], f"{path}: budget")
        except argparse.ArgumentTypeError as exc:
            raise ValueError(str(exc)) from None
    if "name" in document and not (isinstance(document["name"], str) and re.fullmatch(SESSION_NAME, document["name"])):
        raise ValueError(f"{path}: name (the PROJECT part of the automatic names) is 1-40 letters, digits, '_', '.' "
                         "or '-', starting with a letter or digit")
    return path, document


# Options with a host path; the function returns the path part of the value.
PATH_OPTIONS = {"--mount": lambda v: v.rsplit(":", 2)[0], "--copy": lambda v: v.rsplit(":", 1)[0],
                "--git-token-file": lambda v: v.partition("=")[2], "--local-config": str, "--auth-file": str,
                "--import-codex-auth": str, "--state-dir": str, "--log-dir": str, "--gui-vnc": str,
                "--git-credentials": str, "--runtime": str, "--workspace": str, "--kit-dir": str,
                "--privacy-rules": str, "--rules": lambda v: "" if v.startswith("builtin:") else v}


def relative_paths(options):
    """Values of path options in OPTIONS (a list as on the command line) that are relative."""
    result, pending = [], None
    for value in options:
        if pending:
            name, value = pending, value
            pending = None
        elif value in PATH_OPTIONS:
            pending = value
            continue
        elif value.split("=", 1)[0] in PATH_OPTIONS and "=" in value:
            name, value = value.split("=", 1)
        else:
            continue
        path = PATH_OPTIONS[name](value)
        if path and not os.path.isabs(os.path.expanduser(path)):
            result.append(f"{name} {value}")
    return result


def copies_credential(value):
    """Whether a --copy SRC:DEST value brings a login in: a credential target, or a credential file as source."""
    if ":" not in value:
        return False
    source, dest = value.rsplit(":", 1)
    if os.path.normpath(dest) in set(AUTH_TARGETS.values()) | {".claude.json"}:
        return True
    path = Path(os.path.normpath(Path(source).expanduser().absolute()))
    if path.name in (".credentials.json", "auth.json"):
        return True
    # A symlink or another name for a login file is a login file too.
    candidates = {candidate for agent in AUTH_TARGETS for candidate in auth_source_candidates(agent)}
    return path in candidates or path.resolve() in candidates


def auth_choices(options):
    """Indices of the tokens in OPTIONS (canonical, as on the command line) that make an auth choice:
    an AUTH_OPTIONS option or a --copy of a credential file, each with a value given as the next token."""
    actions = option_actions()
    chosen, index = set(), 0
    while index < len(options):
        name, equals, value = options[index].partition("=")
        takes = name in actions and actions[name].nargs != 0 and not equals
        if takes:
            value = options[index + 1] if index + 1 < len(options) else ""
        if name in AUTH_OPTIONS or (name == "--copy" and copies_credential(value)):
            chosen.update(range(index, index + (2 if takes else 1)))
        index += 2 if takes else 1
    return chosen


def merge_config(argv, a, document):
    """Arguments with the project file applied; the command line wins."""
    options, agent_args = list(argv[1:]), None
    if "--" in options:
        position = options.index("--")
        options, agent_args = options[:position], options[position + 1:]
    kept, skip = [], False
    for value in options:
        if skip:
            skip = False
        elif value == "--workspace":
            skip = True
        elif not value.startswith("--workspace="):
            kept.append(value)
    # A folder the command line (or a preset) names already is not added again, in whatever spelling the file has
    # it (~/src/lib, /home/me/src/lib/): twice, it would be refused as overlapping itself.
    def same(value):
        try:
            return Path(os.path.expanduser(value)).resolve()
        except (OSError, RuntimeError):
            return value
    named = {same(w) for w in a.workspaces}
    workspaces = a.workspaces + [w for w in document.get("workspaces", []) if same(w) not in named]
    file_options = document.get("options", []) + (["--log-debug"] if document.get("log_debug") is True else []) + \
        (["--project-name", document["name"]] if isinstance(document.get("name"), str) else []) + \
        ({True: ["--keep"], False: ["--no-keep"]}.get(document.get("keep")) if "keep" in document else []) + \
        (["--budget", str(document["budget"])] if "budget" in document else [])
    if "canaries" in document:  # before the command line's options: --canaries/--no-canaries there win
        chosen = document["canaries"]
        file_options = (["--canaries", ",".join(chosen)] if chosen else ["--no-canaries"]) + file_options
    if auth_choices(kept):
        chosen = auth_choices(file_options)
        file_options = [value for index, value in enumerate(file_options) if index not in chosen]
    return [argv[0], *file_options, *kept, "--workspace", ",".join(workspaces),
            "--", *(document.get("agent_args", []) if agent_args is None else agent_args)]


# Every workspace after the first lies below WORKSPACES_DIR, a small tmpfs of the sandbox's own (the root file
# system is read-only): `sandbox-ai workspace add` makes the mount point of a workspace added during the run there.
WORKSPACES_DIR = "/workspaces"
# no nr_inodes: podman refuses it in --tmpfs ("unknown mount option"); size=1m keeps the entries few anyway
WORKSPACES_TMPFS = "rw,nosuid,nodev,noexec,size=1m,mode=0755"


# --workspace-name NAME=DIR: what /workspaces/NAME may be called (as sandbox-ai workspace add --name checks it).
WORKSPACE_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"


def workspace_names(a):
    """{resolved host path: NAME} of the --workspace-name NAME=DIR options (hidden: `sandbox-ai workspace add --name`
    keeps a workspace so in a session's options, so that a restart puts it at /workspaces/NAME again)."""
    found = {}
    for value in getattr(a, "workspace_name", None) or []:
        name, separator, path = value.partition("=")
        if not separator or not path or not re.fullmatch(WORKSPACE_NAME, name):
            raise ValueError(f"--workspace-name NAME=DIR: NAME 1-64 letters, digits, '_', '.' or '-', starting with "
                             f"a letter or digit: {printable(value)!r}")
        found[os.path.realpath(os.path.expanduser(path))] = name
    return found


_remotefs_module = None
# The mount points of remote folders (sshfs://) this launcher planned or mounted (resolved): sandbox-ai's own folders
# for this start, which check_given_path does not look into (a socket on the other machine is no way to anything
# here; a walk through it would read the remote tree over the network).
REMOTE_POINTS = set()
# [(run, [mount point])] planned by this process and not mounted yet: main() removes what is left of them (a start
# refused after its plan, a dry run, a session's terminal).
REMOTE_PLANNED = []


def load_remotefs_module():
    """remotefs.py: remote folders (sshfs://) mounted on this host for a workspace or a mount; loaded once."""
    global _remotefs_module
    if _remotefs_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_remotefs", HERE / "remotefs.py")
        with LOADING:
            if _remotefs_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _remotefs_module = loaded
    return _remotefs_module


def plan_remote_mounts(a):
    """The remote folders (sshfs://...) of --workspace, --workspace-name and --mount get a mount point each in
    sandbox-ai's own folder for this run (remotefs.plan: made, empty, nothing mounted yet), which takes their place
    in the options: from here on every check of a workspace or mount applies to it as to a local folder. The
    sources are kept (a.remote_given: {point: source}; a.remote_original: the options as given, for
    restore_remote_options). [entry] planned."""
    remote = load_remotefs_module()
    a.remote_mounts = getattr(a, "remote_mounts", None) or []
    a.remote_given = getattr(a, "remote_given", None) or {}
    named = [value.partition("=") for value in getattr(a, "workspace_name", None) or []]
    if not (any(remote.is_remote(value) for value in a.workspaces) or
            any(remote.is_remote(value) for value in a.mount) or
            any(remote.is_remote(path) for _, _, path in named)):
        return []
    for value in [*a.workspaces, *(path for _, _, path in named if remote.is_remote(path))]:
        if remote.is_remote(value):
            remote.parse(value)  # every source checked before anything is made
    mounts = [(value, remote.split_mount(value)) for value in a.mount]
    for _, split in mounts:
        if split:
            remote.parse(split[0])
    a.run_id = getattr(a, "run_id", None) or (a.pod_name[len("agent-"):] if a.pod_name
                                              else f"{a.agent}-{os.urandom(4).hex()}")
    a.remote_original = (list(a.workspaces), list(a.mount), list(getattr(a, "workspace_name", None) or []))
    planned, by_source = [], {}

    def point_for(source, mode, sandbox):
        entry = remote.plan(a.run_id, source, mode, sandbox=sandbox)
        REMOTE_POINTS.add(str(Path(entry["point"]).resolve()))
        planned.append(entry)
        a.remote_given[entry["point"]] = entry["source"]
        return entry["point"]
    workspaces = []
    for index, value in enumerate(a.workspaces):
        if remote.is_remote(value):
            source = remote.parse(value)["source"]
            point = point_for(source, workspace_mode(a, index), "/work" if index == 0 else None)
            by_source[source] = point
            workspaces.append(point)
        else:
            workspaces.append(value)
    a.workspaces, a.workspace = workspaces, workspaces[0]
    a.workspace_name = [f"{name}={by_source.get(remote.parse(path)['source'], path)}" if remote.is_remote(path)
                        else f"{name}{separator}{path}" for name, separator, path in named]
    a.mount = [f"{point_for(remote.parse(split[0])['source'], split[2], split[1])}:{split[1]}:{split[2]}"
               if split else value for value, split in mounts]
    for point, dest in workspace_targets(a):
        for entry in planned:
            if entry["point"] == point and entry.get("sandbox") is None:
                entry["sandbox"] = dest
    a.remote_mounts = a.remote_mounts + planned
    REMOTE_PLANNED.append((a.run_id, [entry["point"] for entry in planned]))
    return planned


def restore_remote_options(a):
    """The options as given again (the remote sources in place of their mount points) and the planned mount points
    removed: before a session's supervisor, which plans and mounts them itself, and after a dry run. True if there
    were any."""
    original = getattr(a, "remote_original", None)
    if not original:
        return False
    a.workspaces, a.mount, a.workspace_name = list(original[0]), list(original[1]), list(original[2])
    a.workspace = a.workspaces[0]
    forget_planned_remote()
    a.remote_mounts, a.remote_given, a.remote_original = [], {}, None
    return True


def forget_planned_remote():
    """Remove the mount points this process planned and never mounted (rmdir only: nothing is mounted there)."""
    remote = load_remotefs_module() if REMOTE_PLANNED else None
    while REMOTE_PLANNED:
        run, points = REMOTE_PLANNED.pop()
        with contextlib.suppress(OSError, ValueError):
            remote.forget(run, points)


def mount_remote(a):
    """Mount the planned remote folders of this start (plan_remote_mounts) on this host, each said with its warning
    (remotefs.warning). The run's cleanup unmounts them (release_remote, after the pod, unless the sandbox outlives
    this launcher - then liveness.discard does once it has ended), the folders added during the run (`workspace add
    sshfs://...`) with them. A password or passphrase is asked only in a terminal. [point]."""
    planned = [entry for entry in getattr(a, "remote_mounts", None) or [] if entry.get("state") == "planned"]
    if not planned:
        return []
    remote = load_remotefs_module()
    interactive = sys.stdin is not None and sys.stdin.isatty() and a.session is None and not a.tmux_socket_dir
    done = []
    for entry in planned:
        print(f"agent-sandbox: mounting {printable(entry['source'])} on this host", file=sys.stderr)
        remote.mount(entry, interactive=interactive)
        entry["state"] = "mounted"
        remote.update(a.run_id, entry["point"], state="mounted", via=entry.get("via"))
        done.append(entry["point"])
        print(f"agent-sandbox: warning: {printable(remote.warning(entry, entry.get('sandbox') or entry['point']))}",
              file=sys.stderr)
    for run, points in REMOTE_PLANNED:
        if run == a.run_id:
            points[:] = [point for point in points if point not in done]
    return done


def release_remote(a):
    """Unmount every remote folder of this run (the start's and those added during it); what stays mounted is said.
    True if the run had any."""
    if not (load_remotefs_module().root() / str(a.run_id)).is_dir():
        return False
    # (sudo, for a kernel's NFS or SMB mount, may ask in the terminal of a start in the foreground)
    interactive = sys.stdin is not None and sys.stdin.isatty() and a.session is None and not a.tmux_socket_dir
    for source, point, why in load_remotefs_module().release(a.run_id, interactive=interactive):
        if why:
            print(f"agent-sandbox: warning: {printable(source or point)} could not be unmounted ({printable(why)}); "
                  f"sandbox-ai gc tries again (or by hand: fusermount3 -u {printable(point)}, for NFS or SMB: sudo "
                  f"umount {printable(point)})", file=sys.stderr)
    return True


def open_remote_connections(a):
    """A session started in a terminal: the connections to its remote folders' hosts opened here, where ssh may ask
    for a password or a passphrase (its supervisor runs in the background and never asks); kept open for a while
    for the supervisor's sshfs. [source] opened."""
    remote = load_remotefs_module()
    sources = [value for value in [*a.workspaces, *(remote.split_mount(m)[0] for m in a.mount if remote.is_remote(m))]
               if remote.is_remote(value)]
    if not sources or not (sys.stdin is not None and sys.stdin.isatty()):
        return []
    opened = []
    for source in dict.fromkeys(sources):
        if remote.open_shared(source):
            opened.append(source)
    return opened


def workspace_targets(a):
    """[(host path, sandbox path)]: the first workspace is always /work; one named by --workspace-name is at
    /workspaces/NAME, the others at their directory's name (-2, -3 ... after the names taken)."""
    names = workspace_names(a)
    targets, used = [(a.workspaces[0], "/work")], set(names.values())
    for value in a.workspaces[1:]:
        named = names.get(os.path.realpath(os.path.expanduser(value)))
        if named is not None:
            targets.append((value, f"/workspaces/{named}"))
            continue
        base = PurePosixPath(value).name or "root"
        name, suffix = base, 2
        while name in used:
            name, suffix = f"{base}-{suffix}", suffix + 1
        used.add(name)
        targets.append((value, f"/workspaces/{name}"))
    return targets


def gui_config(a):
    if not a.gui:
        return None
    # --nested-containers: the agent has an IPC namespace of its own (runtime.nested_ipc), so its X clients cannot
    # attach Xvfb's System V shared memory; the display offers no MIT-SHM then (they draw through the socket).
    return {"display": GUI_DISPLAY, "screen": a.gui_screen, "vnc": bool(a.gui_vnc),
            **({"no_shm": True} if nested(a) else {})}


def wayland_socket():
    display = os.environ.get("WAYLAND_DISPLAY")
    if not display:
        raise ValueError("--gui-wayland requires WAYLAND_DISPLAY on the host")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or (f"/run/user/{os.getuid()}"))
    path = Path(display) if display.startswith("/") else runtime / display
    if not path.is_socket():
        raise ValueError(f"Wayland socket not found: {path}")
    if any(c in str(path) for c in (",", "\n", "\r")):
        raise ValueError("Wayland socket path cannot contain commas or control characters")
    return path


# What the agent can do with a folder of the launcher's own that a workspace, mount or --state-dir you chose shows it
# (check_private_dir), by the option that names that folder.
PRIVATE_DIR_RISKS = {
    "--tmux-socket-dir": "connect to the tmux server of your sessions and type into them: commands as you on the host",
    "--mcp-socket-dir": "connect to the MCP servers' sockets of other sandboxes",
    "--mcp-approvals-dir": "answer its own MCP tool calls' questions (approve what it asks for)",
    "--log-dir": "read and rewrite this run's connection log",
    "--gui-vnc": "reach the VNC socket of the sandbox's display (and so its screen and keyboard)",
    "The default log directory (set --log-dir)": "read and rewrite this run's connection log",
    "--browser-profile": "read and change the browser's profile (its logins and cookies)",
}


def check_private_dir(a, path, option, warn=None):
    """A host directory of the launcher's own for this run (OPTION names it) that the agent can reach through a
    workspace, mount or --state-dir you chose: given, with a warning (WARN, default: warner) of what the agent can
    do with it (PRIVATE_DIR_RISKS). Commas or control characters: refused (no mount can name it). The warning, or
    None."""
    path = Path(path).expanduser().resolve()
    if any(c in str(path) for c in (",", "\n", "\r")):
        raise ValueError(f"{option} cannot contain commas or control characters")
    exposed = [Path(w).expanduser().resolve() for w in a.workspaces] + [Path(parse_mount(m)[0]) for m in a.mount]
    exposed += [a.state_dir.expanduser().resolve()] if a.state_dir else []
    other = next((other for other in exposed if path == other or other in path.parents or path in other.parents),
                 None)
    if other is None:
        return None
    risk = PRIVATE_DIR_RISKS.get(option, "reach what the launcher keeps there for this run")
    text = (f"{option} {path} {'lies in' if other in path.parents else 'is' if path == other else 'holds'} {other}, "
            f"a workspace, mount or --state-dir: the agent can {risk}; {SAFER_READ_ONLY}, or give a folder beside it")
    warner(warn)(text)
    return text


def exposed_trees(a):
    """Host trees the agent can write: workspaces, rw mounts and the persistent HOME."""
    trees = [Path(w).expanduser().resolve() for w in a.workspaces]
    trees += [Path(src) for src, _, mode in map(parse_mount, a.mount) if mode == "rw"]
    trees += [a.state_dir.expanduser().resolve()] if a.state_dir else []
    return trees


def check_host_source(a, path, option, trees=None, warn=None):
    """A host file or directory the launcher reads or writes (OPTION: what it is) inside a tree the agent can change
    (TREES, default exposed_trees: the workspaces, rw mounts and --state-dir you chose): given, with a warning (WARN,
    default: warner) - what the agent writes there the launcher reads, and a link it puts there could make the
    launcher act on another file of yours. What sandbox-ai keeps safe: it never follows a link on the way to it in a
    tree a sandbox could write in an earlier run (planted_symlink: refused, an agent may have put it there). The
    warning, or None."""
    literal = Path(os.path.normpath(Path(path).expanduser().absolute()))
    for candidate in dict.fromkeys([literal, literal.resolve()]):
        for tree in exposed_trees(a) if trees is None else trees:
            if candidate == tree or tree in candidate.parents:
                link = planted_symlink(literal, exposed_before())
                if link is not None:
                    raise ValueError(f"{option} {path} goes through the link {link} -> {os.readlink(link)}, inside a "
                                     "folder a sandbox could write: an agent may have planted it to make the launcher "
                                     "read or write another file of yours, and sandbox-ai does not follow it. Check "
                                     "it, then remove the link or give the real folder")
                what = ("the agent can replace it with a link to another folder of yours before the next start "
                        "(which refuses such a link then)" if option in ("--mount", "--state-dir", "--workspace")
                        else "the launcher reads or writes it there, so the agent can change what it reads, and a "
                             "link it puts in its place could make the launcher act on another file of yours")
                text = (f"{option} {path} lies inside {tree}, which the agent can change: {what}; "
                        f"{SAFER_READ_ONLY}, or keep it outside workspaces, rw mounts and --state-dir")
                warner(warn)(text)
                return text
    return None


HELPER_PREFIX = "helper-"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # Git's empty tree: no .gitattributes for a helper
LONG_TOOL_TIMEOUT = 2000  # seconds an agent waits for a helper's or a team board's call (a helper's task: 1800 at most)
# Seconds an agent waits for one call of an --mcp server whose definition names no tool_timeout (mcp.py): Codex's
# own default of 60 s cut off servers whose tools run longer (exec: commands up to 120 s, up to 600 s when asked);
# ten minutes and one more, so that a server's own limit of 600 s answers first.
DEFAULT_MCP_TOOL_TIMEOUT = 660
# Seconds an agent waits for an --mcp server to start and answer: each connection starts an instance of its own (a
# container), more than Codex's 10 s and Claude Code's 30 s for a server in Node or with a browser.
MCP_STARTUP_TIMEOUT = 60


def mcp_tool_timeouts(a):
    """{NAME: seconds} for every --mcp server of this start: how long its agent waits for one call. A helper's and a
    team board's LONG_TOOL_TIMEOUT, else the server's tool_timeout (its definition: from its catalog entry, `mcp set
    --tool-timeout` or the project file), else DEFAULT_MCP_TOOL_TIMEOUT - also when the definitions cannot be read
    (the start says why elsewhere)."""
    names = list(dict.fromkeys(getattr(a, "mcp", None) or []))
    found = {name: LONG_TOOL_TIMEOUT for name in names if name.startswith((HELPER_PREFIX, "team-"))}
    if len(found) < len(names):
        try:
            servers = {**load_mcp_module().definitions(getattr(a, "project_document", None)),
                       **(getattr(a, "helper_definitions", None) or {})}
        except (OSError, ValueError):
            servers = {}
        for name in names:
            timeout = (servers.get(name) or {}).get("tool_timeout") if isinstance(servers.get(name), dict) else None
            valid = isinstance(timeout, int) and not isinstance(timeout, bool) and 10 <= timeout <= 3600
            found.setdefault(name, timeout if valid else DEFAULT_MCP_TOOL_TIMEOUT)
    return {name: found[name] for name in names}


def helper_name(agent, workspace):
    """The MCP server name of AGENT as the helper on WORKSPACE: one per agent and main workspace, so another
    project never gets this one's helper (and its files)."""
    return f"{HELPER_PREFIX}{agent}-{hashlib.sha256(str(workspace).encode()).hexdigest()[:8]}"


def add_helpers(a):
    """--helper AGENT: each helper becomes an MCP server of this run (a.mcp), defined here (a.helper_definitions):
    the agent in a sandbox of its own on the main workspace, with this run's --rules and project rules for its own
    tools, and --dangerously-skip-permissions if this run has it."""
    a.helper_definitions = {}
    if not a.helper:
        return
    if a.mcp_socket_dir or a.probe:
        raise ValueError("--helper is for an agent's start, not for MCP servers or probes")
    if a.offline or getattr(a, "llm", None):
        raise ValueError(f"--helper: a helper reaches its own provider, which {'--offline' if a.offline else '--llm'} "
                         "keeps out of this run")
    document = getattr(a, "project_document", None) or {}
    keys = list(getattr(a, "api_key", None) or [])
    for agent in dict.fromkeys(a.helper):
        if agent in NO_LOGIN_AGENTS and not keys:
            # Its model comes only from an --api-key (it has no login to bring its provider): a helper started without
            # one reached no host at all, and every task failed.
            title = UNSWITCHABLE_TELEMETRY.get(agent, agent)
            with_login = [name for name in dict.fromkeys(("claude", "codex", "opencode", *AUTH_TARGETS))
                          if name not in NO_LOGIN_AGENTS]
            raise ValueError(f"--helper {agent}: {title} has no login, so as a helper it reaches a model only with "
                             f"this start's --api-key HOST=NAME[=SECRET] (it gets this start's keys); add one, or "
                             f"take a helper with a login ({', '.join(with_login)})")
        name = helper_name(agent, a.workspaces[0])
        # What narrows the lead holds for its helper too: a read-only workspace, the security mode, no login.
        a.helper_definitions[name] = {
            "helper": agent, "workspace": a.workspaces[0], "workspace_ro": bool(a.workspace_ro),
            "mode": getattr(a, "mode", None) or "medium", "no_auth": bool(a.no_auth),
            "skip_permissions": bool(a.dangerously_skip_permissions), "telemetry": telemetry_level(a),
            "rules": [str(Path(path).expanduser().absolute()) for path in a.rules],
            "project_rules": document.get("rules") if isinstance(document, dict) else None,
            # An agent without a login reaches its model with the lead's keys (their placeholders, the proxy puts the
            # key in on the wire there too); an agent with one brings its own provider.
            **({"api_keys": keys} if agent in NO_LOGIN_AGENTS else {}),
            "description": f"{agent} as a helper on {a.workspaces[0]}"}
        if name not in a.mcp:
            a.mcp.append(name)


def check_mcp_names(a):
    """Every --mcp server of this start is checked before anything is made or started - a dry run says it too - and
    every problem at once, each with its fix (mcp.start_problems), instead of one at a time deep in the start
    (mcp.ensure_running), which left a session behind that failed the same way at every resume: the server is defined
    (built-in, sandbox-ai mcp add, a team's, the trusted project file's or a --helper's), what the start asks for it
    (--mcp-limits, --mcp-mount, --mcp-env), its folders, variables, secrets, image, VPN, and an instance of its name
    that runs with another definition (the start gets one of its own: said). a.mcp_instances: the instance of each
    server that is not its name."""
    names = [name for name in dict.fromkeys(a.mcp) if re.fullmatch(MCP_NAME, name)]
    a.mcp_instances = {}
    for option, key in (("--mcp-limits", "mcp_limits"), ("--mcp-mount", "mcp_mount"), ("--mcp-env", "mcp_env")):
        if getattr(a, key, None) and (a.probe or a.mcp_socket_dir):
            raise ValueError(f"{option} is for an agent's start with --mcp, not for a probe or an MCP server's sandbox")
    mcp = load_mcp_module() if names or any(getattr(a, key, None) for key in ("mcp_limits", "mcp_mount", "mcp_env")) \
        else None
    for option, key, parse, what in (("--mcp-limits", "mcp_limits", "start_limits", "limits"),
                                     ("--mcp-mount", "mcp_mount", "start_mounts", "folders"),
                                     ("--mcp-env", "mcp_env", "start_env", "variables")):
        if not getattr(a, key, None):
            continue
        with contextlib.suppress(ValueError):  # (start_problems says what is wrong with a value)
            for name in getattr(mcp, parse)(a):
                if name not in names:
                    print(f"agent-sandbox: {option} {name}: this start does not use the MCP server {name} (--mcp "
                          f"{name}); its {what} are not used", file=sys.stderr)
    if mcp is not None and getattr(a, "mcp_env", None) and not (a.probe or a.mcp_socket_dir) and \
            (a.session is None or a.dry_run):  # (a session's supervisor says it, relayed to its terminal: once)
        for text in mcp.code_env_warnings(a):  # your choice: set, the risk named
            warner()(text)
    if a.probe or a.mcp_socket_dir or not names:
        if mcp is not None and not names:
            for parse in ("start_limits", "start_mounts", "start_env"):
                getattr(mcp, parse)(a)  # a value that is not valid is refused all the same
        a.image_fixes = []
        images = [] if a.mcp_socket_dir else start_image_problems(a)
        if images:
            raise missing_error(load_mcp_module().problems_text(images, about="this start"), a.image_fixes)
        return
    servers = {**mcp.definitions(getattr(a, "project_document", None)),
               **(getattr(a, "helper_definitions", None) or {})}
    missing = [name for name in names if name not in servers]
    hints, fixes = [], []
    if missing and (not a.dry_run or getattr(a, "fix_plan", False)):
        # One that its catalog lists without a signed image yet (pending): built here from its source, asked first
        # on a terminal - else the refusal names the command that builds it (mcp_build_offers). Not in a dry run:
        # the app's and the Board's checks are dry runs, often, and must not wait for the catalogs' download (the
        # app reads them itself before a start and asks there).
        read = {}

        def catalogs():  # (the catalogs read once - over the network - for the hints and for the fixes)
            if not read:
                try:
                    read["entries"] = mcp.catalog_entries()
                except Exception as exc:  # noqa: BLE001 - said by who asked
                    read["error"] = exc
            if "error" in read:
                raise read["error"]
            return read["entries"]
        _added, hints = mcp_build_offers(a, mcp, missing, catalogs=catalogs)
        # What adds each (fixes.py): from its signed catalog, or built here unsigned - offered with everything else
        # the start lacks (asked once on a terminal, done by --fix), each as its security mode takes it.
        fixes = mcp_fixes(a, mcp, missing, catalogs=catalogs)
    problems = [unknown_mcp_message(a, missing, servers) + "".join(f". {hint}" for hint in hints)] if missing else []
    # Every image is asked of Podman at once, each `podman image exists` in a thread of its own (one after another,
    # a start with three image servers waited for four Podman starts): the agent's own (asked meanwhile, answered
    # below) and those of its servers (start_problems takes the answers; one it did not foresee it asks itself).
    asked = [image for image in dict.fromkeys(
        servers[name]["image"] for name in names if isinstance(servers.get(name), dict) and
        isinstance(servers[name].get("image"), str) and not ("rootfs" in servers[name] and a.backend == "bwrap"))] \
        if os.environ.get(IMAGE_CHECK_OFF) != "1" and shutil.which("podman") else []  # (else: none asked)
    a.image_fixes = []  # (what makes each image that is not there: start_image_problems notes it)
    agent_images = in_background(start_image_problems, a, what="whether Podman has the start's own images") \
        if asked else (lambda: start_image_problems(a))
    answers = in_threads({image: (lambda image=image: mcp.image_present(image)) for image in asked})

    def present(image):
        answer = answers.get(image, present)  # (present: not asked; an exception: asked again, to raise it here)
        return answer if answer is None or isinstance(answer, bool) else mcp.image_present(image)
    found, instances, notes = mcp.start_problems(a, names, servers, present=present, fixes=fixes)
    problems += found
    if any(mcp.hostinit_module().MISSING in problem for problem in found):
        # A server Podman would start with --init on a host without its init binary: the package, with the rest.
        fixes.append(FIXES.init_fix("an MCP server of this start"))
    images = agent_images()  # the agent's own image too: before any MCP server is started
    if problems or images:
        error = missing_error(mcp.problems_text(problems + images, **({"about": "this start"} if images else {})),
                              [*a.image_fixes, *fixes])
        error.checked = "mcp"  # (launch: this check has said all it finds - looking ahead does not run it again)
        raise error
    a.mcp_instances = instances
    with contextlib.suppress(Exception):  # (a newer image its catalog pins: said, the start goes on with its own)
        notes = [*notes, *mcp_update_notes(a)]
    for note in notes:
        print(f"agent-sandbox: {note}", file=sys.stderr)


IMAGE_CHECK_OFF = "SANDBOX_AI_MCP_NO_IMAGE_CHECK"  # =1: no image is asked of Podman before a start (tests' images)


def image_fix(a, agent, image):
    """How IMAGE, which the start A needs for AGENT, gets onto this host: its build (sandbox-ai build ...), else a
    pull."""
    if image == f"localhost/{agent}-sandbox:latest":
        return f"sandbox-ai build {agent}"
    if getattr(a, "image_variant", None) and image == f"localhost/{agent}-sandbox-{a.image_variant}:latest":
        # NAME@AGENT where the same name is a variant of another agent too (images.shown_target)
        try:
            spec = importlib.util.spec_from_file_location("images_for_fix", Path(__file__).resolve().parent / "images.py")
            images = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(images)
            target = images.shown_target(a.image_variant, agent)
        except Exception as exc:  # noqa: BLE001 - the name alone, as before
            LOG.debug("image", f"the variant {a.image_variant}'s name for its build is its name alone", exc)
            target = a.image_variant
        return f"sandbox-ai build --variant {target}"
    if team_image_agents(image) is not None:
        return load_team_image_module().build_command(team_image_agents(image))
    if image == CAIRN_IMAGE:
        return "sandbox-ai build cairn"
    if image == ARTEX_IMAGE:
        return "sandbox-ai build artex"
    if image == BROWSER_IMAGE:
        return "sandbox-ai build browser"
    if image in OWN_VPN_IMAGES:  # (a VPN's container: vpn.agent_up builds a missing one as the agent's is built)
        return OWN_VPN_IMAGES[image]
    return f"podman pull {image}, or build it"


def start_image_problems(a):
    """[problem] of the images the start A needs that Podman of its host does not have - the agent's (its variant,
    the team's, an --image), its helpers', the browser's - each with how it gets there (image_fix), asked with
    `podman image exists` before anything is made: nothing is pulled at a start, and a start used to start its MCP
    servers first and fail at Podman's "image not known" afterwards. [] with Bubblewrap, without Podman here (the
    start says so later), for an MCP server's own sandbox, and with SANDBOX_AI_MCP_NO_IMAGE_CHECK=1."""
    checks = start_image_checks(a)
    found = checks()
    a.image_fixes = list(checks.fixes)  # (fixes.py: what makes each one - the refusal carries them)
    return found


def in_background(function, *args, what=None):
    """FUNCTION(*ARGS) begun in a thread of its own; the function returned waits for it and gives its result (or
    raises its exception). WHAT: what it does, in words, for its lines in launcher.log (begun, done in N s, failed);
    else the function's name."""
    outcome = {}
    what = what or getattr(function, "__qualname__", None) or repr(function)

    def run():
        begun = time.monotonic()
        LOG.debug("background", f"begun: {what}")
        try:
            outcome["result"] = function(*args)
            LOG.debug("background", f"done in {time.monotonic() - begun:.1f} s: {what}")
        except BaseException as exc:  # noqa: BLE001 - raised again where the result is asked for
            LOG.debug("background", f"failed after {time.monotonic() - begun:.1f} s: {what}", exc)
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def result():
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]
    return result


def in_threads(calls):
    """{key: result} of CALLS ({key: function without arguments}), each run in a thread of its own, all at once (a
    call that raises gives its exception as its result); {} for none."""
    results = {}

    def run(key, call):
        try:
            results[key] = call()
        except Exception as exc:  # noqa: BLE001 - handed to the caller as the result
            results[key] = exc
    threads = [threading.Thread(target=run, args=item, daemon=True) for item in calls.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def start_vpn_images(a):
    """{profile name: the image of its container} of the VPN profiles the start A runs: its own (--vpn) and those of
    the MCP servers it starts with a VPN of their own - sandbox-ai's Tor and VPN images, built by the start when
    missing (image_fix), before the servers behind them start. {} when none (or a profile cannot be read: the start
    says so where it is used)."""
    found = {}
    try:
        vpn = load_vpn_module()
        names = [a.vpn] if getattr(a, "vpn", None) else []
        if getattr(a, "mcp", None):
            servers = load_mcp_module().definitions(getattr(a, "project_document", None))
            names += [servers[n]["vpn"] for n in dict.fromkeys(a.mcp)
                      if isinstance(servers.get(n), dict) and isinstance(servers[n].get("vpn"), str)]
        for name in dict.fromkeys(names):
            profile = getattr(a, "vpn_profile", None) if name == getattr(a, "vpn", None) else None
            found[name] = vpn.image(profile or vpn.load_profile(name))
    except Exception as exc:  # noqa: BLE001 - a profile not read here is refused where it is used, with its reason
        LOG.debug("image", "the VPN images of the start could not all be read", exc)
    return found


def start_image_checks(a):
    """start_image_problems, its Podman calls begun now (each image in a thread of its own) and its problems returned
    by the function given back, once they are answered: other checks run meanwhile."""
    if a.backend != "podman" or os.environ.get(IMAGE_CHECK_OFF) == "1" or getattr(a, "mcp_socket_dir", None) or \
            not shutil.which("podman"):
        def none():
            return []
        none.fixes = []
        return none
    wanted = {a.image: a.agent} if a.image else {}
    for helper in dict.fromkeys(getattr(a, "helper", None) or []):
        wanted.setdefault(f"localhost/{helper}-sandbox:latest", helper)
    if getattr(a, "cairn", False):
        wanted.setdefault(CAIRN_IMAGE, "Cairn's board")
    if getattr(a, "artex", False):
        wanted.setdefault(ARTEX_IMAGE, "ARTEX")
    if getattr(a, "browser", None) or (getattr(a, "cairn", False) and not on_machine(a)):
        wanted.setdefault(BROWSER_IMAGE, "the browser")
    for name, image in start_vpn_images(a).items():  # (before the MCP servers behind it start: built here too)
        wanted.setdefault(image, f"the VPN {name}")
    answers, failed, build_failed = {}, {}, set()

    def agent_of(image):
        return wanted.get(image, "the start")

    def exists(image):
        # Exit status 0: there, 1: not there; anything else is Podman failing to answer (its storage locked, its
        # user session missing, a remote connection down) - said as such, never as "not built".
        try:
            result = logged_run("image", f"is the image of {agent_of(image)} there", podman_command(a, "image", "exists", image),
                             ok=(0, 1), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return  # not known: the start says what Podman says (the failure is in launcher.log)
        if result.returncode in (0, 1):
            answers[image] = result.returncode == 0
            if result.returncode == 1:
                LOG.info("image", f"{image} (for {agent_of(image)}) is not on this host"
                                  + (f" (Podman at {a.podman_url})" if a.podman_url else ""))
        else:
            lines = [line for line in (result.stderr or "").strip().splitlines()
                     if line.strip() and not PODMAN_WARNING.match(line.strip())]
            failed[image] = (result.returncode, printable("\n".join(lines[-3:])))
    threads = [threading.Thread(target=exists, args=(image,), daemon=True) for image in wanted] \
        if len(wanted) > 1 else []
    for thread in threads:
        thread.start()
    for image in wanted if not threads else ():
        exists(image)  # one: asked here

    def problems():
        for thread in threads:
            thread.join()
        if getattr(a, "team_image_chosen", False) and answers.get(a.image) is False:
            # The team image with exactly the run's agents is not there: one that holds them all and more is taken.
            picked = pick_team_image(a.image, team_images_present(a))
            if picked != a.image:
                print(f"agent-sandbox: the team image {a.image} is not built; taking {picked}, which holds "
                      f"{', '.join(team(a))} as well", file=sys.stderr)
                LOG.info("image", f"{a.image} is not there: {picked} holds the run's agents")
                wanted[picked] = wanted.pop(a.image)
                answers.pop(a.image, None)
                answers[picked] = True
                a.image = picked
        for image, agent in list(wanted.items()):
            if answers.get(image) is False and will_build(a, agent, image):
                # A dry run (the app's check before a start) refuses nothing the start itself builds.
                print(f"agent-sandbox: the image {image} is not built for this user yet: the start builds it first "
                      f"({image_fix(a, agent, image)})", file=sys.stderr)
                answers[image] = True
            elif answers.get(image) is False:
                outcome = {}
                # Where the start asks first what it may make (a terminal, --fix: fixes_first), the image is built
                # by the fix plan after the human's yes - with everything else that is missing, in one question.
                if not fixes_first(a) and build_missing_image(a, agent, image, outcome):
                    answers[image] = True
                elif outcome.get("failed"):
                    build_failed.add(image)  # (its build ran just now and failed: said with the fix below)
        where = f" (Podman at {printable(a.podman_url)})" if a.podman_url else ""
        if any(answer is False for answer in answers.values()):
            where += podman_store_note(a)
        for image, agent in wanted.items():
            if answers.get(image) is False:
                LOG.warning("image", f"the start is refused: the image of {agent} ({image}) is not built on this "
                                     f"host{where}; the fix: {image_fix(a, agent, image)}")
            elif image in failed:
                LOG.warning("image", f"the start is refused: Podman could not say whether {image} (for {agent}) is "
                                     f"there: exit status {failed[image][0]}: {failed[image][1]}")
            elif image not in answers:
                LOG.warning("image", f"Podman gave no answer for {image} (for {agent}); the start goes on and Podman "
                                     "says what is wrong when the sandbox is made")
        return [f"The image of {agent} ({image}) is not built on this host{where} (nothing is pulled at a start): "
                f"{image_fix(a, agent, image)}" + ("\n  (its build failed just now - what it said is above: fix that "
                                                   "first)" if image in build_failed else "")
                if answers.get(image) is False else
                f"Podman could not say whether the image of {agent} ({image}) is there (podman image exists: exit "
                f"status {failed[image][0]}{': ' + failed[image][1] if failed[image][1] else ''}); "
                "check the host with: sandbox-ai doctor"
                for image, agent in wanted.items() if answers.get(image) is False or image in failed]

    def checked():
        found = problems()
        # What makes each image that is not there (fixes.py): none with SANDBOX_AI_NO_AUTO_BUILD=1 (nothing is
        # made then, the refusal names the command as before), none for one whose build failed just now.
        checked.fixes = [] if os.environ.get(AUTO_BUILD_OFF) == "1" else \
            [image_fix_item(a, agent, image) for image, agent in wanted.items()
             if answers.get(image) is False and image not in build_failed]
        return found
    checked.fixes = []
    return checked


AUTO_BUILD_OFF = "SANDBOX_AI_NO_AUTO_BUILD"  # =1: a start builds no missing image of its own (tests)


def team_images_present(a):
    """The names of the team images Podman of the start A has ([] when it cannot say)."""
    try:
        done = logged_run("image", "which team images are there", podman_command(
            a, "images", "--format", "{{.Repository}}:{{.Tag}}"), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [name for name in dict.fromkeys(line.strip() for line in done.stdout.splitlines())
            if team_image_agents(name) is not None]


def will_build(a, agent, image):
    """In a dry run: the start without --dry-run would build IMAGE itself (build_missing_image) - not in the dry
    run of --fix-plan, which lists it as what the start lacks."""
    return bool(a.dry_run) and image_fix(a, agent, image).startswith("sandbox-ai build ") and not a.podman_url \
        and os.environ.get(AUTO_BUILD_OFF) != "1" and not getattr(a, "fix_plan", False)


def internal_start(a):
    """A start nobody typed: a session's supervisor, an MCP server's own sandbox, a helper's - they ask nothing and
    fix nothing by themselves (the start that made them did)."""
    return bool(getattr(a, "tmux_socket_dir", None) or getattr(a, "mcp_socket_dir", None)
                or getattr(a, "helper_of", None))


def fixes_first(a):
    """What this start lacks is made by its fix plan (fixes.py: asked on a terminal, or --fix, or only listed for
    --fix-plan), not silently on the way: a start typed on a terminal, or with --fix or --fix-only. Elsewhere (a
    script without --fix, a session's supervisor) the start builds its own missing image itself, as before."""
    if internal_start(a) or os.environ.get(AUTO_BUILD_OFF) == "1":
        return False
    return bool(getattr(a, "fix", False) or getattr(a, "fix_only", None) or getattr(a, "fix_plan", False)
                or getattr(a, "fix_collect", False) or (not a.dry_run and FIXES.on_terminal()))


def image_fix_item(a, agent, image):
    """The fix for IMAGE, which the start A needs for AGENT and Podman does not have: sandbox-ai's own images are
    built (the pinned, checked build of `sandbox-ai build`); an image you named from a registry is pulled - nobody
    reviewed it, said so; a local image of yours is yours to build. Its id: image:AGENT for the agent's own image,
    else image:REFERENCE."""
    how = image_fix(a, agent, image)
    lacking = f"the image of {agent} ({image}) is not built on this host"
    name = agent if image == f"localhost/{agent}-sandbox:latest" else image  # (image:claude; another by its name)
    if a.podman_url:
        return FIXES.fix("image", name, lacking, "build it where that Podman runs",
                         manual=f"the start uses another Podman service ({a.podman_url}), which builds nothing here")
    if how.startswith("sandbox-ai build "):
        return FIXES.fix("image", name, lacking, "build it", [how.split()],
                         duration="~5 min" if image == f"localhost/{agent}-sandbox:latest" else "some minutes")
    if image.startswith("localhost/"):
        return FIXES.fix("image", name, f"the image {image} is not on this host", "build it",
                         manual="an image of your own: build it as you made it")
    return FIXES.fix("image", name, f"the image {image} is not on this host", "pull it from its registry",
                     [["podman", "pull", image]], duration="a download", unsigned=True,
                     security="an image nobody reviewed is pulled, and the agent runs in it")


def build_missing_image(a, agent, image, outcome=None):
    """True once IMAGE, one of sandbox-ai's own images (image_fix: sandbox-ai build ...) that the start A needs and
    Podman does not have, is built now - the same pinned, checked build as `sandbox-ai build`, its progress on
    standard error - and Podman then has it. False (the start says what is missing and how to build it) for an
    image that is pulled, a dry run, a remote Podman (--podman-url), with SANDBOX_AI_NO_AUTO_BUILD=1, or when the
    build fails (then OUTCOME, a dict if given, gets "failed": True)."""
    fix = image_fix(a, agent, image)
    if not fix.startswith("sandbox-ai build ") or a.dry_run or a.podman_url or os.environ.get(AUTO_BUILD_OFF) == "1":
        LOG.info("image", f"{image} (for {agent}) is not built here automatically: " + (
            "it is pulled, not built" if not fix.startswith("sandbox-ai build ") else "a dry run" if a.dry_run else
            "a remote Podman (--podman-url)" if a.podman_url else f"{AUTO_BUILD_OFF}=1"))
        return False
    launcher = Path(__file__).resolve().with_name("main.py")
    print(f"agent-sandbox: the image {image} is not built for this user yet: building it now ({fix}; once, a few "
          "minutes)", file=sys.stderr)
    try:
        built = logged_run("image", f"build of {image} for {agent} (the start waits for it)",
                        [sys.executable, "-I", str(launcher), *fix.split()[1:]], stdin=subprocess.DEVNULL,
                        stdout=2, check=False).returncode == 0
        there = built and logged_run("image", f"is {image} there after its build", podman_command(a, "image", "exists", image),
                                  ok=(0, 1), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30, check=False).returncode == 0
        LOG.info("image", f"{image} built for the start" if there else
                 f"the build of {image} failed (its output above, on the terminal or in supervisor.log)" if not built
                 else f"{image} was built, but Podman of this start does not have it (another image store?)")
        if not built and outcome is not None:
            outcome["failed"] = True
        return there
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        LOG.warning("image", f"the build of {image} could not run", exc)
        return False


def podman_store_note(a):
    """" (Podman's image store: PATH, user NAME)" - where the start looked for an image it did not find, so a build
    into another store (another user, HOME, XDG_DATA_HOME or containers configuration than the start's) shows; ""
    when Podman does not say."""
    try:
        result = logged_run("image", "where Podman keeps its images",
                         podman_command(a, "info", "--format", "{{.Store.GraphRoot}}"), stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    root = (result.stdout or "").strip().splitlines()[-1:] if result.returncode == 0 else []
    if not root:
        return ""
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        user = str(os.getuid())
    return f" (Podman's image store: {printable(root[0])}, user {printable(user)})"


MCP_BUILD_PLACEHOLDER = "localhost/sandbox-ai-mcp-planned@sha256:" + "0" * 64  # (the mode's check of a planned one)


def mcp_mode_refusal(a, name, definition):
    """Why the security mode of the start A refuses the MCP server NAME were it DEFINITION (a server not added yet:
    one its catalog lists without a signed image, built only when the mode takes it), else None - the mode's own
    rules (modes.widening) asked with that definition among the defined ones."""
    l = launcher_namespace()
    loaded = l.load_mcp_module

    class Planned:
        def __getattr__(self, attribute):
            return getattr(loaded(), attribute)

        @staticmethod
        def definitions(project=None):
            return {**loaded().definitions(project), name: definition}
    l.load_mcp_module = Planned
    for given, why in MODES.widening(l, a, a.mode):
        if given.startswith("--mcp ") and name in given.split()[1::2]:
            other = MODES.laxer(l, a, given, a.mode, None)  # (an offer never widens the mode: the laxer one is named)
            return f"the security mode {a.mode} refuses it ({why})" + (
                f"; the mode {other} takes it (--mode {other})" if other else "")
    return None


def mcp_build_offers(a, mcp, missing, ask=None, catalogs=None):
    """(names added, hints) for the MCP servers MISSING of the start A that a catalog lists without a signed image yet
    (pending: reviewed, not published): on a terminal (ASK: the question, for tests) each is offered to be built here
    from its source at the catalog's exact commit and added unsigned (mcp add NAME --build: what, which commit, the
    archive's hash - asked; a no builds nothing) - never in a dry run, with a remote Podman, or where the security
    mode would refuse the server anyway (check_mcp_names does not ask in a dry run at all). HINTS: for the refusal,
    the command that builds each one not built (or why it cannot be). A server in no catalog gets no hint
    (unknown_mcp_message says how to add one)."""
    try:
        entries, _problems = (catalogs or mcp.catalog_entries)()
        builder = mcp.build_module()
        pending = {name: builder.pending_entry(entries, name) for name in missing}
    except Exception as exc:  # noqa: BLE001 - only for the offer: the refusal stands as it is without it
        LOG.debug("mcp", "the MCP catalogs could not be read for the missing servers", exc)
        return [], []
    added, hints = [], []
    # (asked only where a question is given: a start asks about everything it lacks at once - mcp_fixes, fixes.py)
    terminal = ask is not None
    mounts = mcp.start_mounts(a) if getattr(a, "mcp_mount", None) else {}
    for name in missing:
        if not isinstance(pending.get(name), tuple):
            continue
        prefix, entry = pending[name]
        unsigned = (entry.get("_signature") or {}).get("state") != "good"
        given = [f"{target}={value['host']}" + (f":{value['mode']}" if value.get("mode") else "")
                 for target, value in mounts.get(name, {}).items()]
        command = (f"sandbox-ai mcp add {name} --build" + (" --unsigned" if unsigned else "")
                   + "".join(f" --mount {printable(value)}" for value in given))
        refused = mcp_mode_refusal(a, name, {"image": MCP_BUILD_PLACEHOLDER,
                                             "network": "full" if entry.get("network") in ("full", "lan") else
                                             str(entry.get("network") or "none")})
        if refused:
            hints.append(f"{name}: its catalog has no signed image of it yet, and {refused}")
            continue
        if getattr(a, "mode", None) in FIXES.STRICT:
            # An offer never widens the mode: built here it would be an unsigned server (fixes.judge).
            hints.append(f"{name}: its catalog ({mcp.catalog_label(entry.get('_catalog') or prefix)}) lists it, but "
                         f"has no signed image of it yet; built here it would be an unsigned server, which is not "
                         f"offered in the security mode {a.mode} - the mode {FIXES.LAXER} takes it (--mode "
                         f"{FIXES.LAXER})")
            continue
        hint = (f"{name}: its catalog ({mcp.catalog_label(entry.get('_catalog') or prefix)}) lists it, but has no "
                f"signed image of it yet - `{command}` builds it here from its reviewed source (the commit asked "
                "first) and adds it unsigned; then start again")
        if a.dry_run or getattr(a, "podman_url", None) or not terminal:
            hints.append(hint)
            continue
        question = ask
        if question is None:
            def question(text):
                print(f"agent-sandbox: {printable(text)}", file=sys.stderr)
                try:
                    return input("agent-sandbox: build it? [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
                except EOFError:
                    return False
        print(f"agent-sandbox: the MCP server {name} is not added here; its catalog lists it without a signed image "
              "yet, so it can be built here from its source (asked next)", file=sys.stderr)
        try:
            builder.build_and_add(f"{prefix}:{name}", {"mounts": given}, unsigned=unsigned, confirm=question,
                                  out=lambda text: print(text, file=sys.stderr))
        except ValueError as exc:  # (declined, refused, or its build failed: said, with the log's end)
            LOG.info("mcp", f"{name} not built for the start: {str(exc).splitlines()[0]}")
            hints.append(f"{name} is not built: {exc}" if "not confirmed" not in str(exc) else hint)
            continue
        added.append(name)
    return added, hints


def fix_questions_default():
    """Your default fix_questions (fixes.QUESTIONS: all, each) for a live action's question; "all" when it is not
    set or the defaults file cannot be read (a start says why)."""
    try:
        document = json.loads(read_config_bytes(defaults_file(), 1 << 16).decode())
        value = document.get("fix_questions") if isinstance(document, dict) else None
    except (OSError, ValueError, RecursionError, AttributeError):
        value = None
    return value if value in FIXES.QUESTIONS else FIXES.QUESTIONS[0]


def offer_fixes(error, mode=None, about="this command", again="run it again", terminal=None, read=None):
    """What a live action (mcp attach, toolchain attach, kits attach) does with the refusal ERROR that carries fixes
    (fixes.py), as a start does: on a terminal the fixes are listed and asked about (your fix_questions), done with
    their output, and the ids done returned - the action is then tried again; declined, without a terminal, or with
    only what sandbox-ai cannot do: a ValueError with the commands that fix it. MODE: the security mode of the run it
    acts on (an offer never widens it). READ: the answers (tests)."""
    return FIXES.resolve(str(error), getattr(error, "fixes", None) or [], mode=mode or MODES.NAMES[2],
                         questions=fix_questions_default(), terminal=terminal, read=read, printable=printable,
                         about=about, again=again, flag=None)


def bwrap_agent_fixes(a, binary):
    """The fixes (fixes.py) for an agent's executable BINARY that this host does not have, with Bubblewrap (which
    runs the host's own agent): a choice - the same start with Podman instead (the agent comes with its image: built
    when it is not), or the agent installed on this host (its npm package where sandbox-ai knows it: shown, never
    run - a global npm install is yours to do)."""
    name = Path(str(binary)).name
    found = []
    if shutil.which("podman") and not getattr(a, "probe", False):
        found.append(FIXES.fix("option", "backend-podman", f"{name}, the agent's executable, is not installed on "
                                                           "this host (Bubblewrap runs the host's own agent)",
                               "start it with Podman instead: the agent comes with its image",
                               options={"add": ["--backend", "podman"], "remove": ["--backend"]},
                               choice=f"executable:{name}"))
    try:
        spec = importlib.util.spec_from_file_location("images_for_executable", HERE / "images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        package = images.AGENT_PACKAGES.get(name)
    except Exception as exc:  # noqa: BLE001 - the package not named then
        LOG.debug("fixes", "the agents' npm packages were not read", exc)
        package = None
    found.append(FIXES.fix("tool", name, f"{name}, the agent's executable, is not installed on this host",
                           "install it on this host", [["npm", "install", "-g", package]] if package else [],
                           manual="an agent on this host is yours to install" + ("" if package else
                                                                                  f" (its own installer: see {name}'s "
                                                                                  "documentation)"),
                           choice=f"executable:{name}" if found else ""))
    return found


def missing_error(message, fixes):
    """The refusal MESSAGE as a FIXES.Missing when something makes what it lacks (FIXES), else as the ValueError it
    always was."""
    return FIXES.Missing(message, fixes) if fixes else ValueError(message)


def mcp_fixes(a, mcp, missing, catalogs=None):
    """The fixes (fixes.py) for the MCP servers MISSING of the start A, by the catalogs: one a signed catalog has
    with its image is added from it; one it lists without a signed image yet (pending) is built here from its
    reviewed source and added UNSIGNED; one of an unsigned catalog is added --unsigned (said so); each with the
    folders the start gives it (--mcp-mount; a folder it does not give is the server's default: /work the start's
    workspace, any other a folder of its own - said), the variables its entry requires declared (--env NAME), and
    refused where the security mode would not take the server. One `mcp add` would refuse otherwise is shown with
    its command and why, not done; one in several catalogs or in none likewise. [] when the catalogs cannot be read, or with a
    remote Podman (--podman-url)."""
    if getattr(a, "podman_url", None):
        return []
    try:
        entries, _problems = (catalogs or mcp.catalog_entries)()
        builder = mcp.build_module()
        mounts = mcp.start_mounts(a) if getattr(a, "mcp_mount", None) else {}
    except Exception as exc:  # noqa: BLE001 - only for the offer: the refusal stands as it is without it
        LOG.debug("mcp", "the MCP catalogs could not be read for what adds the missing servers", exc)
        return []
    found = []
    for name in missing:
        lacking = f"the MCP server {name} is not added on this host"
        matches = [(prefix, entry) for prefix, entry in entries if entry.get("name") == name]
        chosen = (getattr(a, "preset_catalogs", None) or {}).get(name)
        named = name
        if len(matches) > 1 and any(prefix == chosen for prefix, _ in matches):
            # In several catalogs, and the preset named the one it means (mcp:binwalk): that one, by that name.
            matches, named = [(prefix, entry) for prefix, entry in matches if prefix == chosen], f"{chosen}:{name}"
        if not matches:
            found.append(FIXES.fix("mcp", name, lacking, "define it yourself, or start without it",
                                   manual="no catalog lists it (sandbox-ai mcp catalog)"))
            continue
        if len(matches) > 1:
            choices = " | ".join(f"sandbox-ai mcp add {prefix}:{name}" for prefix, _ in matches)
            found.append(FIXES.fix("mcp", name, lacking, "add the one you want",
                                   manual=f"it is in several catalogs, choose one: {choices}"))
            continue
        prefix, entry = matches[0]
        state = (entry.get("_signature") or {}).get("state")
        label = mcp.catalog_label(entry.get("_catalog") or prefix)
        if state == "bad":
            found.append(FIXES.fix("mcp", name, lacking, "nothing of that catalog is used",
                                   manual=f"the signature of its catalog ({label}) does not verify"))
            continue
        pending = isinstance(builder.pending_entry(entries, named), tuple)
        given = [f"{target}={value['host']}" + (f":{value['mode']}" if value.get("mode") else "")
                 for target, value in mounts.get(name, {}).items()]
        # A folder the start does not give needs no --mount: `mcp add` gives it its default (/work: the start's
        # workspace, any other a folder of the server's own), said in the fix. A variable its entry requires is
        # declared with it (--env NAME: read from your environment or the secret NAME when the server starts).
        required = [v["name"] for v in entry.get("env", []) if isinstance(v, dict) and v.get("required")
                    and isinstance(v.get("name"), str)]
        command = ["sandbox-ai", "mcp", "add", named, *(["--build", "--yes"] if pending else []),
                   *(["--unsigned"] if state != "good" else []),
                   *(word for value in given for word in ("--mount", value)),
                   *(word for variable in required for word in ("--env", variable))]
        network = str(entry.get("network") or "none")
        refused = mcp_mode_refusal(a, name, {"image": MCP_BUILD_PLACEHOLDER,
                                             "network": "full" if network in ("full", "lan") else network})
        problem, folders = None, ""
        if not refused:
            try:  # (what `mcp add` would refuse before anything is pulled or built - shown then, never guessed)
                planned = mcp.definition_from_entry(
                    {**entry, "kind": "oci", "image": entry.get("image") or MCP_BUILD_PLACEHOLDER,
                     **({"digest": "sha256:" + "0" * 64} if pending else {})}, given, required)
                folders = mcp.folders_text(name, planned)
            except ValueError as exc:
                problem = str(exc)
        if pending:
            title = "build it here from its reviewed source (the catalog's repository at the catalog's commit) and add it"
            security = "nobody signed the image that is built here; it is shown as unsigned wherever it appears"
        elif state != "good":
            title = f"add it from its catalog ({label}), which is not signed"
            security = "nobody vouched for that catalog's entry"
        else:
            title = f"add it from its signed catalog ({label}; its image is pulled by digest)"
            security = ""
        if network != "none" and not refused:
            title += f", with network ({network}" + (f": {', '.join(map(str, entry.get('hosts') or []))}"
                                                       if network == "allowlist" else "") + ")"
        if folders:  # (where its folders are: the start's own --mcp-mount, else their defaults)
            title += f"; its folders - {folders}"
        if required:
            title += (f"; its variable{'s' if len(required) > 1 else ''} {', '.join(required)} from your environment "
                      "or the secret of that name")
        found.append(FIXES.fix("mcp", name, lacking, title, [command], duration="minutes" if pending else "a download",
                               unsigned=pending or state != "good", security=security,
                               refused=refused or "", manual="" if refused or not problem else problem))
    return found


def unknown_mcp_message(a, missing, servers):
    """What a start that names the MCP servers MISSING (not among SERVERS) is told: which ones, which are defined, and
    - when the project file of its workspace defines one but is not read (--no-config) - why."""
    text = f"Unknown MCP server: {', '.join(missing)}; defined: {', '.join(sorted(servers)) or 'none'}"
    path = Path(a.workspaces[0] or ".").expanduser() / CONFIG_NAME if a.workspaces else None
    try:
        own = None
        if path and path.is_file():
            with open(path, "rb") as stream:
                own = json.loads(stream.read(CONFIG_LIMIT)).get("mcp_servers")
    except (OSError, ValueError, AttributeError, RecursionError):
        own = None  # only for the hint: the file is not trusted here, nothing of it is used
    named = [name for name in missing if isinstance(own, dict) and name in own]
    if named and not getattr(a, "project_document", None):
        why = "it is started with --no-config" if a.no_config else "it reads another configuration"
        text += (f". {printable(str(path))} defines {', '.join(named)}, but this start reads no project file there "
                 f"({why}): start it without --no-config once that file is trusted (sandbox-ai wizard trust FILE)")
    return (text + "; add a server with sandbox-ai mcp add (sandbox-ai mcp catalog lists the catalogs' servers), "
            "or start without it")


def check_workspace_creatable(workspace):
    """A workspace that is not there yet is made at the start (prepare_workspace): refused now - in a dry run too -
    when it cannot be, e.g. a folder of another machine whose parent belongs to root here."""
    if workspace.exists():
        return
    parent = workspace.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.exists() and not parent.is_dir():
        raise ValueError(f"Workspace {printable(str(workspace))} does not exist and cannot be created: "
                         f"{printable(str(parent))} is a file, not a folder")
    if not (parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)):
        raise ValueError(f"Workspace {printable(str(workspace))} does not exist and cannot be created: "
                         f"{printable(str(parent))} is not writable for you. Choose a folder you can write (for "
                         "another machine: a folder of that machine), or create it first")


def mcp_instance(a, name):
    """The instance of MCP server NAME this start reaches: NAME, or NAME.HASH with limits, folders or variables of its
    own (--mcp-limits, --mcp-mount, --mcp-env; check_mcp_names finds it, mcp.instance_for) or of its own where NAME
    runs with another definition (mcp.start_instance; ensure_running keeps what it started)."""
    found = getattr(a, "mcp_instances", None)
    if found is None:
        found = {}
        if getattr(a, "mcp_limits", None) or getattr(a, "mcp_mount", None) or getattr(a, "mcp_env", None):
            mcp = load_mcp_module()
            named = [*(mcp.start_limits(a) if getattr(a, "mcp_limits", None) else {}),
                     *(mcp.start_mounts(a) if getattr(a, "mcp_mount", None) else {}),
                     *(mcp.start_env(a) if getattr(a, "mcp_env", None) else {})]
            found = {name: mcp.instance_of(a, name) for name in dict.fromkeys(named) if name in a.mcp}
        a.mcp_instances = found
    return found.get(name, name)


def mcp_plan(a):
    """The dry run's MCP servers: each with the instance this start reaches (mcp_instance), its socket and the limits
    it runs with there ({memory, cpus, pids}; None where its definition is not known, or for a helper), and the
    folders the start gives it (--mcp-mount: [{target, host, mode}], [] for none) and the VPN it reaches the network
    through (its own, or the start's: with a Tor profile a Tor container of its own; None without)."""
    names = list(dict.fromkeys(a.mcp))
    if not names:
        return []
    mcp = load_mcp_module()
    try:
        servers = {**mcp.definitions(getattr(a, "project_document", None)),
                   **(getattr(a, "helper_definitions", None) or {})}
        wanted, folders = mcp.start_limits(a), mcp.start_mounts(a)
    except (OSError, ValueError):
        servers, wanted, folders = {}, {}, {}
    try:  # the VPN the start puts each server behind (its own Tor container for a Tor profile)
        started = mcp.vpn_for_servers(a, names)
    except (OSError, ValueError):
        started = {}
    found = []
    for name in names:
        instance, limits, mounts, vpn = mcp_instance(a, name), None, [], None
        if name in servers:
            with contextlib.suppress(ValueError):
                limits = mcp.default_limits(mcp.with_limits(name, servers[name], wanted.get(name)))
            with contextlib.suppress(ValueError):
                mounts = [{key: m.get(key, "ro") for key in ("target", "host", "mode")}
                          for m in mcp.with_mounts(name, servers[name], folders.get(name)).get("mounts", [])
                          if m["target"] in folders.get(name, {})]
            with contextlib.suppress(OSError, ValueError):
                reaches, own = mcp.network_of(servers[name])
                vpn = (own or started.get(name)) if reaches else None
        found.append({"name": name, "instance": instance, "socket": str(mcp_socket_dir(instance) / "server.sock"),
                      "limits": limits, "own_limits": bool(wanted.get(name)), "own_mounts": mounts,
                      # The VPN it reaches the network through (with a Tor profile its own Tor), None without one;
                      # whether that is the start's own tunnel, lent to it (one key, one tunnel: vpn.lends).
                      "vpn": vpn and str(vpn), "vpn_lent": vpn is not None and mcp.lent_of(started.get(name))[0]})
    return found


def mcp_socket_dir(name):
    """Host directory holding the Unix socket of MCP server NAME (an instance: mcp_instance)."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "mcp" / name / "sock"


def remove_volume(a, volume):
    """The run's own Podman volume VOLUME goes (--cairn on macOS: its relay's socket); quietly, as the pod does."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(podman_command(a, "volume", "rm", "--force", volume), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=120, check=False)


def mcp_volume(directory):
    """macOS: the Podman volume that holds the socket of the MCP server whose host socket directory is DIRECTORY
    (mcp_socket_dir): sandbox-ai-mcp-NAME-HASH, the hash of that directory telling state roots apart."""
    directory = Path(directory).expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(str(directory))).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", directory.parent.name)[:64] or "server"
    return f"sandbox-ai-mcp-{name}-{digest}"


def mcp_bridge_args(name):
    return ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge", f"{MCP_AGENT_DIR}/{name}/server.sock"]


def mcp_agent_args(a):
    """Agent CLI arguments registering every --mcp server as a stdio command."""
    names = list(dict.fromkeys(a.mcp))
    if a.agent not in ("claude", "codex", "qwen") or not names:
        return []
    # Each server's wait for one call (mcp_tool_timeouts): Codex's default of 60 s cut off a helper's task, a team
    # member's wait and every server whose tools run longer.
    timeouts = mcp_tool_timeouts(a)
    if a.agent == "claude":
        # Claude Code counts milliseconds; its per-server "timeout" (MCP_TOOL_TIMEOUT for older ones: agent_environment).
        servers = {name: {"command": "python3", "args": mcp_bridge_args(name), "timeout": timeouts[name] * 1000}
                   for name in names}
        return ["--mcp-config", json.dumps({"mcpServers": servers})]
    if a.agent == "codex":
        return [value for name in names for value in
                ("-c", f'mcp_servers.{name}.command="python3"', "-c", f"mcp_servers.{name}.args={json.dumps(mcp_bridge_args(name))}",
                 "-c", f"mcp_servers.{name}.tool_timeout_sec={timeouts[name]}",
                 "-c", f"mcp_servers.{name}.startup_timeout_sec={MCP_STARTUP_TIMEOUT}")]
    return load_qwen_module().mcp_args(names, mcp_bridge_args, timeouts)


def nameservers(a):
    if a.offline:
        return []
    if a.nameserver:
        return list(dict.fromkeys(a.nameserver))
    if getattr(a, "vpn_dns", None) and (a.dns or a.egress_all_allow):
        return list(a.vpn_dns)  # the VPN's resolvers, reached through the tunnel (vpn.py)
    return list(DEFAULT_NAMESERVERS) if a.dns or a.egress_all_allow else []


def resolver_files(a):
    """(resolv.conf, nsswitch.conf) contents for the sandbox."""
    servers = nameservers(a)
    if servers:
        resolv = "".join(f"nameserver {server}\n" for server in servers)
    elif a.log_dir:
        # Lookups of unpinned names reach the local logging stub (NXDOMAIN).
        resolv = "nameserver 127.0.0.1\n"
    else:
        resolv = "# DNS is pinned in /etc/hosts; no DNS egress.\n"
    dns = bool(servers or a.log_dir)
    return resolv, "passwd: files\ngroup: files\nhosts: files" + (" dns" if dns else "") + "\n"


def host_networks(hosts):
    return [net for host in hosts for net in HOST_NETWORKS.get(host, [])]


def safe_dest(dest):
    path = PurePosixPath(dest)
    if not path.is_absolute() or ".." in path.parts or str(path) != dest:
        raise ValueError(f"Non-canonical mount destination: {dest}")
    if not any(path == PurePosixPath(base) or PurePosixPath(base) in path.parents
               for base in ("/work", "/mnt", "/opt")):
        raise ValueError("Mount destinations must be under /work, /mnt or /opt")
    return dest


def parse_mount(value):
    remote = load_remotefs_module()
    if remote.is_remote(value):
        # A remote folder (sshfs://...:DEST:ro|rw): its source as given until plan_remote_mounts puts its mount
        # point in its place (a check that runs before - the defaults file's - sees no folder of this host in it).
        source, dest, mode = remote.split_mount(value)
        remote.parse(source)
        safe_dest(dest)
        return source, dest, mode
    try:
        source, dest, mode = value.rsplit(":", 2)
    except ValueError:
        raise ValueError("Mount syntax: SRC:DEST:ro|rw") from None
    try:
        source = Path(source).expanduser().resolve(strict=True)
    except FileNotFoundError:
        # (a kept start's saved --mount whose folder is gone: how to start it without)
        raise ValueError(f"Mount source unavailable: --mount {printable(value)}: "
                         f"{printable(str(Path(source).expanduser()))} is not there (a kept start whose folder is "
                         "gone starts without it: sandbox-ai resume NAME --drop-option --mount)") from None
    except OSError as exc:
        raise ValueError(f"Mount source unavailable: {exc}") from None
    if source.is_char_device() or source.is_block_device():
        # Technically impossible: the engines bind with nodev, a device opened through such a mount refuses.
        raise ValueError(f"--mount {source} is a device: a mount of it cannot be used in the sandbox (Podman and "
                         "Bubblewrap mount with nodev); give it with --device")
    if mode not in ("ro", "rw") or not (source.is_dir() or source.is_file() or source.is_socket()
                                        or source.is_fifo()):
        raise ValueError("Mount must be a regular file/directory with ro or rw mode")
    # A socket (/var/run/docker.sock: the host's root), the kernel's and the devices' trees: you decide - given, with
    # the warning of check_given_path (check_host_sources).
    safe_dest(dest)
    if any(c in str(source) + dest for c in (",", "\n", "\r", "\0")):
        raise ValueError("Mount paths cannot contain commas or control characters")
    return str(source), dest, mode


def device_mode(path):
    """st_mode of PATH (a separate function, so that tests can present files as devices)."""
    return os.stat(path).st_mode


def parse_device(value, option="--device"):
    """(path, kind) of a host device for the agent, symlinks resolved: kind "node" for a character
    device, "dir" for one of DEVICE_DIRS (or a directory below one). Refuses DEVICE_REFUSED."""
    if not value or any(c in value for c in (",", ":", "=", "\n", "\r", "\0")):
        raise ValueError(f"{option} expects a path under /dev without commas, colons or control characters: "
                         f"{printable(value)!r}")
    path = Path(value)
    if not path.is_absolute() or DEV not in path.parents:
        raise ValueError(f"{option} expects a path under {DEV}: {value!r}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{option} {value}: {exc.strerror or exc}") from None
    if DEV not in resolved.parents or any(c in str(resolved) for c in (",", ":", "=", "\n", "\r", "\0")):
        raise ValueError(f"{option} {value} leads to {printable(resolved)}, not to a device under {DEV}")
    below = resolved.relative_to(DEV).as_posix()
    for pattern, reason in DEVICE_REFUSED:
        if re.fullmatch(pattern, below):
            raise ValueError(f"{option} {value}: refused; {resolved} would give the agent {reason}")
    mode = device_mode(resolved)
    if stat.S_ISDIR(mode):
        if not any(below == d or below.startswith(d + "/") for d in DEVICE_DIRS):
            raise ValueError(f"{option} {value}: of directories only "
                             + ", ".join(f"{DEV}/{d}" for d in DEVICE_DIRS)
                             + " (or one below them) can be passed whole; name single devices otherwise")
        return str(resolved), "dir"
    if stat.S_ISBLK(mode):
        raise ValueError(f"{option} {value}: block devices (disks, partitions) are never passed to an agent")
    if not stat.S_ISCHR(mode):
        raise ValueError(f"{option} {value}: {resolved} is not a device")
    return str(resolved), "node"


def nvidia_cdi_available():
    """True if a CDI specification (from nvidia-ctk cdi generate) names NVIDIA GPUs."""
    for directory in CDI_DIRS:
        try:
            files = sorted(directory.iterdir())
        except OSError:
            continue
        for path in files:
            if path.suffix not in (".yaml", ".yml", ".json"):
                continue
            try:
                with open(path, "rb") as stream:
                    if b"nvidia.com/gpu" in stream.read(CONFIG_LIMIT):
                        return True
            except OSError:
                continue
    return False


def gpu_devices(a):
    """[(path, kind)] of --gpu; kind "cdi" is a CDI device name (Podman's way to NVIDIA GPUs)."""
    if not a.gpu:
        return []
    if a.gpu == "nvidia":
        if a.backend == "podman":
            # A remote Podman (--podman-url) has its own CDI specifications, so only a local one is checked.
            if not a.podman_url and not nvidia_cdi_available():
                raise ValueError("--gpu nvidia: no CDI specification for NVIDIA GPUs in "
                                 + ", ".join(map(str, CDI_DIRS)) + "; install the NVIDIA Container Toolkit and "
                                 "run: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml")
            return [("nvidia.com/gpu=all", "cdi")]
        try:
            names = sorted(path.name for path in DEV.iterdir() if re.fullmatch(NVIDIA_NODES, path.name))
        except OSError:
            names = []
        if not any(re.fullmatch(r"nvidia[0-9]+", name) for name in names):
            raise ValueError(f"--gpu nvidia: no {DEV}/nvidia0; is the NVIDIA driver loaded?")
        return [parse_device(str(DEV / name), "--gpu nvidia") for name in names]
    if not (DEV / "dri").is_dir():
        raise ValueError(f"--gpu {a.gpu}: no {DEV}/dri; is a graphics driver loaded?")
    found = [parse_device(str(DEV / "dri"), f"--gpu {a.gpu}")]
    if a.gpu == "rocm":
        if not (DEV / "kfd").exists():
            raise ValueError(f"--gpu rocm: no {DEV}/kfd; is the amdgpu driver with ROCm support loaded?")
        found.append(parse_device(str(DEV / "kfd"), "--gpu rocm"))
    return found


def device_entries(a):
    """[{"value", "path", "kind", "facts", "notes", "device"}] of every device asked for (--device, then --gpu):
    paths checked by parse_device, usb:VENDOR:PRODUCT[:SERIAL] resolved to the device's nodes through sysfs
    (devices.py), each with what the device rules judge (its FACTS)."""
    module = load_devices_module()
    entries = []
    for value in a.device:
        if value.startswith("usb:"):
            nodes, notes, device = module.plan(value, launcher=launcher_view())
            entries += [{"value": value, **node, "notes": notes if index == 0 else [], "device": device}
                        for index, node in enumerate(nodes)]
        else:
            path, kind = parse_device(value)
            facts = module.identity(path, value, dev_root=DEV) if kind == "node" else \
                module.other_facts(path, kind, value, DEV)
            entries.append({"value": value, "path": path, "kind": kind, "facts": facts, "notes": [], "device": None})
    for path, kind in gpu_devices(a):
        facts = module.other_facts(path, kind, dev_root=DEV) if kind != "node" else module.identity(path, dev_root=DEV)
        entries.append({"value": f"--gpu {a.gpu}", "path": path, "kind": kind, "facts": facts, "notes": [],
                        "device": None})
    return entries


def launcher_view():
    """This launcher as devices.plan uses it (its device checks and /dev), with what a caller replaced in it."""
    return argparse.Namespace(parse_device=parse_device, DEV=DEV)


def devices(a, held=False):
    """[(path, kind)] of every device for the agent (--device, then --gpu), without repeats and
    without single devices inside a directory that is passed whole. Devices an "ask" rule holds are left out
    (they are attached once allowed); HELD: only those."""
    asked = {path for path, verdict in (getattr(a, "device_verdicts", None) or {}).items()
             if verdict["decision"] == "ask"}
    found = list(dict.fromkeys((entry["path"], entry["kind"]) for entry in device_entries(a)))
    directories = [Path(path) for path, kind in found if kind == "dir"]
    return [(path, kind) for path, kind in found
            if not any(directory in Path(path).parents for directory in directories) and (path in asked) == held]


def check_device_rules(a):
    """The device rules of the run's rulesets on every device: a deny stops the start, an ask holds the device
    for a question once the sandbox runs (a.device_verdicts: {path: verdict})."""
    a.device_verdicts = {}
    rules = RUNTIME.Ruleset(load_rulesets(a))
    if not rules.devices_active or not (a.device or a.gpu):
        return
    for entry in device_entries(a):
        verdict = rules.device(entry["facts"])
        what = entry["value"] if entry["value"] == entry["path"] else f"{entry['value']} ({entry['path']})"
        if verdict["decision"] == "deny":
            raise ValueError(f"--device {printable(what)}: {printable(verdict['reason'])}")
        if verdict["decision"] == "ask" and a.ask == "off" and not a.dry_run:
            raise ValueError(f"--device {printable(what)}: rule {verdict['rule']} ({verdict['ruleset']}) asks about "
                             "it, and --ask is off")
        a.device_verdicts[entry["path"]] = verdict


def device_grants(a):
    """The grants of the start for devices.json (see devices.py): one per --device value and per --gpu."""
    module = load_devices_module()
    verdicts = getattr(a, "device_verdicts", None) or {}
    kept = {path for path, _ in devices(a)} | {path for path, _ in devices(a, held=True)}
    grants = []
    for value in dict.fromkeys(entry["value"] for entry in device_entries(a)):
        entries = [entry for entry in device_entries(a) if entry["value"] == value and entry["path"] in kept]
        if not entries:
            continue
        judged = [verdicts.get(entry["path"]) or {"decision": "allow", "rule": None, "ruleset": None}
                  for entry in entries]
        grant = module.grant_from(value, entries, judged, entries[0]["device"], "start")
        if any(verdict["decision"] == "ask" for verdict in judged):
            grant.update(state="held", pending=list(grant["nodes"]), pending_numbers=dict(grant["nodes"]), nodes={})
        else:
            grant["state"] = "attached"
        grants.append(grant)
    return grants


def device_access(path):
    """True if this user may open PATH for reading and writing as owner, through its primary group,
    as anyone or through a POSIX ACL entry (udev's uaccess): the sandbox keeps no supplementary groups."""
    info = os.stat(path)
    uid, gid = os.getuid(), os.getgid()
    try:
        raw = os.getxattr(path, "system.posix_acl_access")
    except OSError:
        raw = b""
    entries = []
    if len(raw) >= 4 and struct.unpack_from("<I", raw)[0] == 2:
        entries = [struct.unpack_from("<HHI", raw, offset) for offset in range(4, len(raw) - 7, 8)]
    if not entries:
        bits = info.st_mode >> 6 if info.st_uid == uid else info.st_mode >> 3 if info.st_gid == gid else info.st_mode
        return bits & 6 == 6
    mask = next((perm for tag, perm, _ in entries if tag == 0x10), 7)
    if info.st_uid == uid:
        return any(tag == 0x01 and perm & 6 == 6 for tag, perm, _ in entries)
    named = [perm for tag, perm, ident in entries if tag == 0x02 and ident == uid]
    if named:
        return named[0] & mask & 6 == 6
    groups = [perm for tag, perm, ident in entries
              if (tag == 0x04 and info.st_gid == gid) or (tag == 0x08 and ident == gid)]
    if groups:
        return any(perm & mask & 6 == 6 for perm in groups)
    return any(tag == 0x20 and perm & 6 == 6 for tag, perm, _ in entries)


def usb_device_dir(path, sys_root=Path("/sys")):
    """The sysfs directory of the USB device behind the character device PATH (a /dev/bus/usb node, or a tty,
    hidraw or video device on USB), or None."""
    try:
        rdev = os.stat(path).st_rdev
        current = (sys_root / "dev" / "char" / f"{os.major(rdev)}:{os.minor(rdev)}").resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / "idVendor").is_file() and (candidate / "idProduct").is_file():
            return candidate
        if candidate == sys_root:
            break
    return None


def usb_class_bluetooth(device_dir):
    """The USB device in DEVICE_DIR is a Bluetooth adapter (class e0, protocol 01, on the device or an interface)."""
    def read(path):
        try:
            return path.read_text().strip().lower()
        except OSError:
            return ""
    if read(device_dir / "bDeviceClass") == "e0":
        return True
    return any(read(interface / "bInterfaceClass") == "e0" and read(interface / "bInterfaceProtocol") == "01"
               for interface in device_dir.glob(f"{device_dir.name}:*"))


def udev_hint(path, sys_root=Path("/sys")):
    """A udev rule line that gives your user the device PATH, with its USB IDs where known."""
    subsystem = "tty" if "/tty" in str(path) else "hidraw" if "hidraw" in str(path) else "usb"
    device = usb_device_dir(path, sys_root)
    if device is None:
        return None
    try:
        vendor, product = ((device / name).read_text().strip() for name in ("idVendor", "idProduct"))
    except OSError:
        return None
    if not (re.fullmatch(r"[0-9a-f]{4}", vendor) and re.fullmatch(r"[0-9a-f]{4}", product)):
        return None
    return (f'SUBSYSTEM=="{subsystem}", ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}", '
            'TAG+="uaccess"')


def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def device_notes(a):
    """Messages for the start: which devices the agent gets, what they expose, and which it cannot open."""
    found = devices(a)
    held = devices(a, held=True)
    notes = [f"{value}: {note}" for entry in device_entries(a) for value, note in
             ((entry["value"], note) for note in entry["notes"])] if any(v.startswith("usb:") for v in a.device) else []
    if held:
        notes.append("held for a question (a device rule asks; attached once you allow it with sandbox-ai "
                     "approvals): " + ", ".join(path for path, _ in held))
    if not found:
        return notes
    notes += ["devices for the agent (their drivers run in your host's kernel): " + ", ".join(
        "NVIDIA GPUs (CDI)" if kind == "cdi" else f"{path} (directory: devices plugged in later appear too)"
        if kind == "dir" else path for path, kind in found)]
    for path, kind in found:
        if kind == "cdi":
            continue
        below = Path(path).relative_to(DEV).as_posix()
        for pattern, reason in DEVICE_WARN:
            if re.fullmatch(pattern, below):
                notes.append(f"warning: {path} is {reason}")
        if kind == "dir":
            nodes = [Path(root) / name for root, _, names in os.walk(path) for name in names]
            nodes = [node for node in nodes if not node.is_symlink() and stat.S_ISCHR(device_mode(node))]
            if nodes and not any(device_access(node) for node in nodes):
                notes.append(f"warning: your user cannot open any device in {path} without supplementary "
                             "groups, so the agent cannot either; see the README (Hardware) for a udev rule")
        else:
            usb = usb_device_dir(path)
            if usb is not None and usb_class_bluetooth(usb):
                notes.append(f"warning: {path} is a Bluetooth adapter; the host's kernel driver (btusb) owns it, "
                             "so the agent cannot use Bluetooth through this node. Bluetooth goes through BlueZ on "
                             "the host: run the Bluetooth part there and let the agent reach it over the network "
                             "(--allow-host-port PORT), see --help (Hardware)")
            if not device_access(path):
                group = os.stat(path).st_gid
                via = f" (only through group {group_name(group)})" if os.stat(path).st_mode & 0o060 else ""
                rule = udev_hint(path)
                notes.append(f"warning: your user cannot open {path} directly{via}; the sandbox keeps no "
                             "supplementary groups, so the agent cannot either. "
                             + (f"Give your user the device with a udev rule in /etc/udev/rules.d/70-sandbox-ai.rules: "
                                f"{rule} (then: sudo udevadm control --reload && sudo udevadm trigger, and replug)"
                                if rule else "See the README (Hardware) for a udev rule"))
    return notes


def validate(a):
    if a.offline and (git_token_values(a) or a.git_credentials):
        raise ValueError("Git tokens need network access to their server; drop --offline (or --profile offline)")
    if a.offline and (a.allow or a.allow_host or a.allow_domain or a.allow_tcp or a.allow_host_port or a.presets or a.frontier
                      or a.network or a.ip or a.ip6 or a.dns or a.nameserver or a.egress_all_allow
                      or a.subnet or a.subnet6):
        raise ValueError("--offline conflicts with network permissions/options")
    if getattr(a, "llm_traffic_limit", None) is not None:
        check_traffic_limit(a.llm_traffic_limit, "--llm-traffic-limit")
    tcp_targets(a)
    git_tokens(a)
    if host_ports(a) and (a.backend != "podman" or a.network):
        raise ValueError("--allow-host-port needs the Podman backend with its default pasta network")
    for value in a.nameserver:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError(f"--nameserver expects a single IP address: {value!r}") from None
        if "%" in value:  # A scope ID may hold any text, which would reach resolv.conf and nft.
            raise ValueError(f"--nameserver does not accept an IPv6 scope ID: {value!r}")
    if a.local_config and (a.agent != "opencode" or not a.local_config.expanduser().is_file()):
        raise ValueError("--local-config requires OpenCode and an existing configuration file")
    for value in a.allow:
        if "%" in value:  # a scope ID may hold any text, which would reach nft
            raise ValueError(f"--allow does not accept an IPv6 scope ID: {value!r}")
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            hint = "; a host name goes to --allow-host (-A)" if re.search(r"[A-Za-z]", value) else ""
            raise ValueError(f"--allow takes an address or a network (192.0.2.10, 192.0.2.0/24): {value!r}{hint}") \
                from None
    # --allow-domain first: its refusals name it, not the --allow-host '*.DOMAIN' it became (expand_domains).
    domains = {"*." + domain_of(value): value for value in a.allow_domain}
    for host in a.allow_host:
        if host.startswith("*."):
            given = f"--allow-domain {domains[host]}" if host in domains else f"--allow-host {host}"
            try:
                RUNTIME.wildcard_suffix(host, shared_ok=host in (getattr(a, "shared_confirmed", None) or ()))
            except RUNTIME.SharedWildcardError as exc:
                raise ValueError(f"--allow-host {host}: {exc} (on a terminal the start asks; --yes answers yes "
                                 "for scripts)") from None
            except ValueError as exc:
                raise ValueError(f"--allow-host {host}: {exc}") from None
            if not asking(a) and not a.egress_all_allow:
                raise ValueError(f"{given} needs the sandbox's questions (not with --ask off): each name "
                                 "below it is opened as its lookup is answered")
            if nameservers(a) and not a.egress_all_allow:
                raise ValueError(f"{given} with --dns or --nameserver: lookups would go past the sandbox's "
                                 "DNS stub, the names below it would resolve but stay closed; name the hosts one by "
                                 "one, or start without real DNS")
            continue
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split(".")):
            raise ValueError(f"Invalid hostname: {host!r}")
    if a.network and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", a.network):
        raise ValueError("--network must name an existing private Podman network")
    if a.network in ("host", "none", "private", "bridge", "default", "slirp4netns", "pasta"):
        raise ValueError("Use a named Podman network, not a special network mode")
    if (a.ip or a.ip6) and not a.network:
        raise ValueError("Static IP requires --network")
    for value, version in [(a.ip, 4), (a.ip6, 6)]:
        if value and (ipaddress.ip_address(value).version != version or "%" in value):
            raise ValueError(f"Expected IPv{version} address without scope ID")
    if a.backend == "bwrap" and (a.network or a.ip or a.ip6 or a.podman_url):
        raise ValueError("--network/--ip/--ip6/--podman-url apply only to Podman")
    if a.backend == "bwrap" and getattr(a, "policy_lsm", None):
        raise ValueError(f"the administrator's policy starts every sandbox in a confinement of this host's security "
                         f"module (lsm in {POLICY_FILE}), which Bubblewrap cannot apply; use --backend podman")
    syscall_filter(a)  # (SANDBOX_AI_SYSCALL_FILTER: a known value, and never off in a strict mode)
    check_subnets(a)
    if a.backend == "podman" and a.runtime:
        raise ValueError("--runtime applies only to bwrap; install runtimes in the image")
    if sum(bool(value) for value in (a.copy_auth, a.auth_file, a.import_codex_auth, a.no_auth,
                                     getattr(a, "auth_from_session", None))) > 1:
        raise ValueError("Choose one of --copy-auth, --auth-file, --auth-from-session, --import-codex-auth or "
                         "--no-auth")
    if getattr(a, "auth_from_session", None) is not None:
        takes = load_session_login_module().AGENTS
        if a.agent not in takes:
            raise ValueError(f"--auth-from-session: {a.agent} has no login sandbox-ai takes from a session (only "
                             f"{', '.join(takes)})")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", a.auth_from_session):
            raise ValueError(f"--auth-from-session: not a start's name: {printable(a.auth_from_session)[:80]}")
    if a.copy_auth and a.agent in NO_LOGIN_AGENTS:
        raise ValueError(f"--copy-auth: {a.agent} has no login of its own to copy (nothing of the host's {a.agent} "
                         "is copied in); give it a key with --api-key HOST=NAME, or a model server with --llm")
    auth_targets = set(AUTH_TARGETS.values()) | {".claude.json"}
    if a.no_auth and any(":" in v and str(PurePosixPath(v.rsplit(":", 1)[1])) in auth_targets for v in a.copy):
        raise ValueError("--no-auth conflicts with --copy of a credential file")
    if a.import_codex_auth and a.agent != "opencode":
        raise ValueError("--import-codex-auth is supported only for OpenCode")
    for value in a.secret_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:=[A-Za-z_][A-Za-z0-9_]{0,63})?", value):
            raise ValueError(f"--secret-env expects NAME or NAME=SECRET (letters, digits, _): {value!r}")
    check_api_keys(a)
    check_budget(a)
    check_model_effort(a)
    check_show_thinking(a)
    check_codex_telemetry_args(a)
    check_identity(a)
    check_publish(a)
    for name in a.env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("--env expects a variable NAME, without a value")
        if name not in os.environ:
            raise ValueError(f"Host variable is not set: {name}")
        if name in ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
    for mount in a.mount:
        parse_mount(mount)
    resource_limits(a)
    check_platform(a)
    check_machine_shares(a)
    if a.podman_url and (a.device or a.gpu in ("dri", "rocm")):
        raise ValueError("--device and --gpu dri|rocm name devices of this host; with --podman-url only "
                         "--gpu nvidia (the remote's CDI) is possible")
    devices(a)
    for name in a.kit:
        load_kits_module().kit_path(name, a.agent)
    check_kits_there(a)
    for value in a.copy:
        if ":" in value:
            dest = PurePosixPath(os.path.normpath(value.rsplit(":", 1)[1]))
            for managed in kit_managed_paths(a):
                if dest == managed or managed in dest.parents:
                    raise ValueError(f"--copy {dest}: sandbox-ai fills {managed} from the kits or the project "
                                     "memory; put the file into a kit (or start with --no-default-kit/--no-memory)")
    for value, option in ((a.name, "--name"), (a.project_name, "--project-name")):
        if value is not None and not re.fullmatch(SESSION_NAME, value):
            raise ValueError(f"{option} NAME: 1-40 characters from letters, digits, '_', '.', '-', starting with a "
                             "letter or digit")
    if a.name is not None:
        if a.session is not None:
            raise ValueError("--session NAME names the start already; drop --name")
        if a.name.startswith("mcp-"):
            raise ValueError("--name: names starting with mcp- belong to the MCP server sandboxes")
    if a.start_id is not None and not (re.fullmatch(START_ID, a.start_id) and a.tmux_socket_dir):
        raise ValueError("Invalid --start-id")
    if a.session is not None:
        if not re.fullmatch(SESSION_NAME, a.session):
            raise ValueError("--session NAME: 1-40 characters from letters, digits, '_', '.', '-'")
        # A session is tmux already, so --tmux is redundant there, not an error.
        if a.state_dir or a.probe or a.tmux_socket_dir or a.pod_name:
            raise ValueError("--session manages its own state; drop --state-dir/--probe")
    for name in a.mcp:
        check_mcp_name(name)
    if a.pod_name and not re.fullmatch(r"agent-[A-Za-z0-9_.-]{1,60}", a.pod_name):
        raise ValueError("Invalid pod name")
    for value in a.no_inspect:
        name = value[2:] if value.startswith("*.") else value
        if len(name) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name) or any(
                not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", part) for part in name.split(".")):
            raise ValueError(f"--no-inspect expects a host name or *.DOMAIN: {value!r}")
        bare = name.lower().rstrip(".")
        public = sorted(host for host in load_privacy_module().PUBLIC_GIT_HOSTS
                        if bare == host or bare.endswith("." + host) or host.endswith("." + bare))
        if public and a.privacy_check != "off":
            raise ValueError(f"--no-inspect {value} would let pushes to {', '.join(public)} pass unchecked; the "
                             "privacy check needs to see them (or start with --privacy-check off)")
    if a.token_proxy:
        if not proxy_enabled(a):
            raise ValueError("--token-proxy needs the connection proxy (not with --no-proxy or --offline)")
        if a.git_credentials:
            raise ValueError("--token-proxy takes --git-token-env, --git-token-file or --git-token-secret, not "
                             "--git-credentials")
        for kind, value in git_token_values(a):
            token_proxy_target(value, kind)
    if a.allow_http:
        if not proxy_enabled(a):
            raise ValueError("--allow-http needs the connection proxy (not with --no-proxy or --offline)")
        http_rules(a)
    if a.llm:
        llm = load_llm_module()
        # The agent must not be able to change where the key goes.
        check_host_source(a, llm.store_path(), "The LLM endpoints file (XDG_CONFIG_HOME)")
        check_host_source(a, llm.ca_dir(), "The LLM endpoints' CAs (XDG_CONFIG_HOME)")
        llm.validate(a, proxy_enabled(a), loopback_reachable(a), within_ceiling)
    elif getattr(a, "llm_param", None):
        raise ValueError("--llm-param sets a parameter of --llm's model; add --llm NAME[:MODEL]")
    elif getattr(a, "llm_models", None):
        raise ValueError("--llm-models names models of --llm's server; add --llm NAME[:MODEL]")
    elif getattr(a, "llm_tools", None) is not None:
        raise ValueError("--llm-tools chooses Claude Code's tools for --llm's model; add --llm NAME[:MODEL]")
    elif getattr(a, "use_case", None):
        raise ValueError("--use-case chooses among the models of a server of yours; add --llm NAME (sandbox-ai llm "
                         "recommend shows what it would choose)")
    # Inside an agent-visible tree the Firejail blacklist would not hide them.
    for path, option in [(a.log_dir, "--log-dir"), (a.gui_vnc, "--gui-vnc"), (a.tmux_socket_dir, "--tmux-socket-dir"),
                         (a.mcp_socket_dir, "--mcp-socket-dir"), (a.mcp_approvals_dir, "--mcp-approvals-dir")]:
        if path:
            check_private_dir(a, path, option)
    match = re.fullmatch(r"(\d{1,5})x(\d{1,5})x(8|16|24|32)", a.gui_screen)
    if not match or not all(16 <= int(match[i]) <= 16384 for i in (1, 2)):
        raise ValueError("--gui-screen expects WIDTHxHEIGHTxDEPTH, e.g. 1920x1080x24")
    if a.gui_wayland:
        wayland_socket()
    if not all(a.workspaces):
        raise ValueError("Empty --workspace entry")
    resolved = []
    for value in a.workspaces:
        source = Path(value).expanduser().absolute()
        if any(path.is_symlink() and not path.exists() for path in (source, *source.parents)):
            raise ValueError("Workspace path contains a dangling symlink")
        workspace = source.resolve(strict=False)
        # An agent of an earlier run may have planted a link to get a host directory mounted read-write.
        check_planted(source, "Workspace", exposed_before())
        if (workspace.exists() and not workspace.is_dir()) or any(c in str(workspace) for c in (",", "\n", "\r")):
            raise ValueError("Workspace must be a directory without commas/control characters")
        check_workspace_creatable(workspace)
        resolved.append(workspace)
    for index, first in enumerate(resolved):
        for second in resolved[index + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError(f"Workspaces must not overlap: {first} and {second}")
    for workspace in resolved:
        check_workspace_scope(workspace, own_folders(argparse.Namespace(**{**vars(a), "workspaces": resolved})))
    a.workspaces = [str(path) for path in resolved]
    a.workspace = a.workspaces[0]
    workspace = resolved[0]
    names = workspace_names(a)
    if len(set(names.values())) != len(names) or len(names) != len(a.workspace_name):
        raise ValueError("--workspace-name: each NAME and each DIR once")
    for path, name in names.items():
        if path == a.workspaces[0]:
            raise ValueError(f"--workspace-name {name}={printable(path)}: the first workspace is /work")
        if path not in a.workspaces[1:]:
            raise ValueError(f"--workspace-name {name}={printable(path)}: names no --workspace of this start")
    add_helpers(a)
    check_mcp_names(a)
    # A small model with many MCP tools did worse in the local-model research: said, the human decides.
    a.mcp_tools_report = mcp_tool_budget(a)
    if a.mcp_tools_report is not None:
        for line in load_mcp_budget_module().warning_lines(a.mcp_tools_report, a.mcp_tools_report["endpoint"]):
            print(line, file=sys.stderr)
    check_companions(a)
    if any(parse_mount(m)[1] == "/work" for m in a.mount):
        raise ValueError("Use --workspace to mount /work")
    bindings = [(workspace, PurePosixPath("/work"))]
    for value in a.mount:
        src, dest, _ = parse_mount(value)
        dest = PurePosixPath(dest)
        # A later parent bind hides earlier child mounts. Inspect the tree
        # visible at this step, rather than the longest historic destination.
        for host, mounted in reversed(bindings):
            if mounted == dest or mounted in dest.parents:
                current = host
                for part in dest.relative_to(mounted).parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError(f"Mount destination traverses a symlink: {dest}")
                break
        bindings.append((Path(src), dest))
    check_host_sources(a)
    check_rulesets(a)
    check_device_rules(a)
    load_vpn_module().validate(a)  # --vpn, and no VPN key within the sandbox's reach
    check_nested(a)  # after the VPN's: never behind Tor
    check_services(a)  # after the VPN's too: never behind Tor
    check_cairn(a)  # after the services' too (their names and ports); it may bring the browser
    check_artex(a)  # after the services' too: their names and ports
    check_browser(a)  # after the services': their names and ports
    check_tmp_size(a)
    check_agent_hooks(a)
    check_enable_agents(a)
    if a.agent == "hermes" or "hermes" in companions(a) or getattr(a, "hermes_enable", None):
        load_hermes_module().check(a, hooks_active(a), companions(a))
    if a.agent == "qwen" or "qwen" in companions(a) or getattr(a, "qwen_enable", None):
        load_qwen_module().check(a, hooks_active(a), companions(a))
    if a.agent == "dsh" or "dsh" in companions(a):
        load_dsh_module().check(a, hooks_active(a), companions(a))
    if a.agent == "goose" or "goose" in companions(a) or getattr(a, "goose_enable", None):
        load_goose_module().check(a, hooks_active(a), companions(a))
    if a.agent == "openhands" or "openhands" in companions(a) or getattr(a, "openhands_enable", None):
        load_openhands_module().check(a, hooks_active(a), companions(a))
    if a.agent == "nanobot" or "nanobot" in companions(a) or getattr(a, "nanobot_enable", None):
        load_nanobot_module().check(a, hooks_active(a), companions(a))
    if a.agent == "letta" or "letta" in companions(a) or getattr(a, "letta_enable", None):
        load_letta_module().check(a, hooks_active(a), companions(a))
    if a.agent == "opencode2" or "opencode2" in companions(a) or getattr(a, "opencode2_enable", None):
        load_opencode2_module().check(a, hooks_active(a), companions(a))
    if a.agent == "pi" or "pi" in companions(a) or getattr(a, "pi_enable", None):
        load_pi_module().check(a, hooks_active(a), companions(a))
    check_telemetry_unproxied(a)
    for note in audit_tool_notes(a):
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    a.canary_categories = load_canary_module().categories(a)
    a.log_explicit = bool(a.log_dir)
    if a.log_dir is None and proxy_enabled(a):
        # The connection log is always kept; a workspace containing this directory needs --log-dir elsewhere.
        a.log_dir = default_log_dir(workspace)
        check_private_dir(a, a.log_dir, "The default log directory (set --log-dir)")
        check_output_dir(a.log_dir, "The default log directory (set --log-dir)")


# The agents whose telemetry is never switchable (hermes.py, qwen.py, dsh.py, goose.py, openhands.py, nanobot.py, letta.py): their
# telemetry hosts are
# refused by the connection proxy in every run, and never opened. Without the proxy, only networks of your own may be
# opened by address: a public one (--allow 0.0.0.0/0, a CDN's range, --allow-tcp to a public address) holds addresses
# of those hosts, which nothing refuses then (as --egress-all-allow with --no-proxy, which each module refuses).
UNSWITCHABLE_TELEMETRY = {"hermes": "Hermes", "qwen": "Qwen Code", "dsh": "DeepSeek Harness", "goose": "Goose",
                          "openhands": "OpenHands",
                          "nanobot": "nanobot",
                          "letta": "Letta Code",
                          "opencode2": "OpenCode 2",
                          "pi": "Pi"}
OWN_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "fc00::/7", "fe80::/10", "::1/128"))


def own_network(value):
    """VALUE (an address or network) lies wholly in a private, loopback or link-local network: no telemetry host's."""
    try:
        network = ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return False
    return any(network.version == own.version and network.subnet_of(own) for own in OWN_NETWORKS)


def pinned_telemetry_environment(agent):
    """The opt-out variables of AGENT's telemetry where it is never switchable (UNSWITCHABLE_TELEMETRY: its module's
    TELEMETRY_ENV), {} for another agent. For a start of its image with --binary too: the agent started from a shell
    there reads them as well."""
    if agent not in UNSWITCHABLE_TELEMETRY:
        return {}
    module = load_hermes_module() if agent == "hermes" else agent_module(agent)
    return dict(module.TELEMETRY_ENV)


def check_telemetry_unproxied(a):
    """Refuse a start of an agent whose telemetry is never switchable (UNSWITCHABLE_TELEMETRY) that opens a public
    network by address without the connection proxy (--no-proxy with --allow or --allow-tcp): nothing would refuse
    its telemetry hosts there. ValueError names the option and the fix."""
    names = [UNSWITCHABLE_TELEMETRY[agent] for agent in image_agents(a) if agent in UNSWITCHABLE_TELEMETRY]
    if not names or not getattr(a, "no_proxy", False) or getattr(a, "offline", False):
        return
    name = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    public = [f"--allow {value}" for value in getattr(a, "allow", None) or [] if not own_network(value)]
    public += [f"--allow-tcp {address}:{port}" if ":" not in address else f"--allow-tcp [{address}]:{port}"
               for address, port in tcp_targets(a) if not own_network(address)]
    if public:
        raise ValueError(f"{name}'s telemetry is always off: {', '.join(public)} opens public addresses, and "
                         f"without the connection proxy nothing refuses {name}'s telemetry hosts there; keep the "
                         "proxy (drop --no-proxy), or open only networks of your own (private, loopback, link-local)")


def check_output_dir(path, option, warn=None):
    """A host directory PATH bound read-write into the sandbox for the launcher's output (--log-dir, --gui-vnc,
    --browser-profile) that is, holds or lies in a folder of the user's sockets, a private location (sandbox-ai's
    own folders for them in XDG_STATE_HOME, where the wizard puts them, excepted) or a kernel file system: you
    decide, so it is given, with a warning (WARN, default: warner) for each."""
    path = Path(path).expanduser().resolve()
    check_socket_location(path, option, warn)
    check_kernel_mounts(path, option, warn=warn)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state").expanduser().resolve()
    if not any(own in path.parents for own in (state / "sandbox-ai" / "logs", state / "sandbox-ai" / "vnc")):
        check_private_location(path, option, warn)


def default_log_dir(project):
    """Log directory the wizard uses for PROJECT: per project, also for equal directory names."""
    project = Path(project)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state / "sandbox-ai" / "logs" / f"{project.name or 'root'}-{digest}"


# The kernel's own file systems (also read-only they hand out the host: /proc/PID/root, cgroup files, devices).
KERNEL_FILESYSTEMS = frozenset({"proc", "sysfs", "cgroup", "cgroup2", "devtmpfs", "devpts", "mqueue", "debugfs",
                                "tracefs", "securityfs", "bpf", "configfs", "fusectl", "pstore", "efivarfs",
                                "binfmt_misc", "hugetlbfs"})


_GNUPG_SOCKETS = {}


def gnupg_socket_dir():
    """The folder of GnuPG's sockets (gpg-agent's, its SSH agent's: `gpgconf --list-dirs socketdir`), or None (no
    gpgconf, no answer within 5 s). Asked once per HOME and GNUPGHOME."""
    key = (os.environ.get("HOME"), os.environ.get("GNUPGHOME"))
    if key not in _GNUPG_SOCKETS:
        found = None
        if shutil.which("gpgconf"):
            with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
                result = subprocess.run(["gpgconf", "--list-dirs", "socketdir"], capture_output=True, text=True,
                                        timeout=5, stdin=subprocess.DEVNULL)
                line = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() \
                    else ""
                # gpgconf escapes a few characters (%3a for ':' ...).
                line = urllib.parse.unquote(line)
                found = Path(line) if os.path.isabs(line) else None
        _GNUPG_SOCKETS[key] = found
    return _GNUPG_SOCKETS[key]


def socket_locations():
    """[(path, label)]: the folders that hold the user's sockets (XDG_RUNTIME_DIR, /run/user/UID: the session's bus,
    Podman's and systemd's sockets, GnuPG's agent there or in ~/.gnupg and GNUPGHOME; sandbox-ai's own: the runs'
    control sockets, wherever a run registers (runs_dir: also XDG_STATE_HOME's without XDG_RUNTIME_DIR, and the
    default place), and the shared SSH connections to your hosts, also where hosts.py keeps them without
    XDG_RUNTIME_DIR). A socket can be connected through a read-only mount too, so nothing given to a sandbox may be,
    hold or lie in one."""
    found = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        found.append((Path(os.environ["XDG_RUNTIME_DIR"]), "XDG_RUNTIME_DIR (the user's sockets)"))
    found.append((Path(f"/run/user/{os.getuid()}"), "/run/user/UID (the user's sockets)"))
    # Where runs register their control sockets (net.run_dirs looks there too).
    found.append((runs_dir(), "sandbox-ai's runs (their control sockets)"))
    for state in dict.fromkeys([os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"),
                                str(Path.home() / ".local/state")]):
        found.append((Path(state) / "sandbox-ai" / "runs", "sandbox-ai's runs (their control sockets)"))
    found.append((Path.home() / ".gnupg", "GnuPG's folder (gpg-agent's sockets, also its SSH agent)"))
    if os.path.isabs(os.environ.get("GNUPGHOME") or ""):
        found.append((Path(os.environ["GNUPGHOME"]), "GNUPGHOME (gpg-agent's sockets)"))
    if gnupg_socket_dir() is not None:
        found.append((gnupg_socket_dir(), "GnuPG's socket folder (gpgconf --list-dirs socketdir)"))
    found.append((Path.home() / "Library" / "Caches" / "sandbox-ai", "sandbox-ai's SSH connections (macOS)"))
    for temporary in {os.environ.get("TMPDIR") or "/tmp", "/tmp"}:
        found.append((Path(temporary) / f"sandbox-ai-{os.getuid()}", "sandbox-ai's SSH connections"))
    # (where they go when another user made sandbox-ai-UID first: hosts.temp_root)
    cache = os.environ.get("XDG_CACHE_HOME") or ""
    found.append((Path(cache if os.path.isabs(cache) else Path.home() / ".cache") / "sandbox-ai" / "run",
                  "sandbox-ai's SSH connections and sockets"))
    result = []
    for path, label in found:
        try:
            result.append((Path(os.path.normpath(path.expanduser().absolute())).resolve(), label))
        except (OSError, RuntimeError):
            continue
    return list(dict.fromkeys(result))


def check_socket_location(path, option, warn=None):
    """A host PATH (resolved) given to a sandbox, read-only or not, that is, holds or lies in a folder of the user's
    sockets (socket_locations): you decide, so it is given, with a warning (WARN, default: warner) - a socket there
    reaches what runs as you, even through a read-only mount. The warning, or None."""
    path = Path(path)
    for folder, label in socket_locations():
        if path == folder or folder in path.parents or path in folder.parents and path != Path("/"):
            text = (f"{option} {path} {'lies in' if folder in path.parents else 'is or holds'} {folder}, {label}: "
                    "the agent can connect to a socket there and reach what runs as you (also on your other hosts: "
                    "commands, keys, the session's bus), even through a read-only mount - "
                    f"{SAFER_READ_ONLY} for its files, not for its sockets")
            warner(warn)(text)
            return text
    return None


def kernel_mounts_below(path, mountinfo=Path("/proc/self/mountinfo")):
    """[(mount point, file system type)] of the kernel's own file systems (KERNEL_FILESYSTEMS) at or below PATH
    (resolved) in this mount namespace: Podman's and Bubblewrap's binds are recursive, so a procfs, sysfs or cgroup
    mounted below a workspace (a chroot, a build root) would reach the agent. An unreadable table: [] (the start
    goes on as before; the live add refuses on its own)."""
    try:
        text = Path(mountinfo).read_text(errors="replace")
    except OSError:
        return []
    prefix = str(path).rstrip("/") + "/"
    found = []
    for line in text.splitlines():
        head, separator, tail = line.partition(" - ")
        fields, kind = head.split(), tail.split()[:1]
        if not separator or len(fields) < 5 or not kind or kind[0] not in KERNEL_FILESYSTEMS:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if point == str(path) or point.startswith(prefix):
            found.append((point, kind[0]))
    return found


def kernel_mount_holding(path, mountinfo=Path("/proc/self/mountinfo")):
    """(mount point, file system type) of the kernel's own file system (KERNEL_FILESYSTEMS) that PATH (resolved)
    lies in, by the mount table of this namespace, else None: the mount with the longest mount point that is PATH or
    one of its parents (the last such line on equal points: an overmount lies on top). A kernel file system mounted
    on a shorter one of them later in the table may hide that mount: it counts as well (refused rather than
    guessed). A procfs at /mnt/chroot/proc hands out /proc/PID/root also to a workspace /mnt/chroot/proc/1, which
    kernel_mounts_below does not see. An unreadable table: None (as kernel_mounts_below)."""
    try:
        text = Path(mountinfo).read_text(errors="replace")
    except OSError:
        return None
    target = str(path).rstrip("/") or "/"
    matches = []
    for line in text.splitlines():
        head, separator, tail = line.partition(" - ")
        fields, kind = head.split(), tail.split()[:1]
        if not separator or len(fields) < 5 or not kind:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]).rstrip("/") or "/"
        if point == "/" or target == point or target.startswith(point + "/"):
            matches.append((point, kind[0]))
    if not matches:
        return None
    longest = max(len(point) for point, _ in matches)
    index = max(i for i, (point, _) in enumerate(matches) if len(point) == longest)
    for point, kind in [matches[index]] + matches[index + 1:]:
        if kind in KERNEL_FILESYSTEMS:
            return point, kind
    return None


# The kernel's and the devices' trees: what a sandbox given them gets, said whatever file system is mounted there.
KERNEL_TREES = ((Path("/proc"), "the host's processes (/proc/PID/root reaches every file of theirs, /proc/PID/environ "
                                "their tokens)"),
                (Path("/sys"), "the kernel's and the devices' settings"),
                (DEV, "the host's devices"))
ENGINE_BIND = ("if Podman or Bubblewrap cannot bind it, the start fails with the engine's own message (it cannot be "
               "given then)")


def check_kernel_mounts(path, option, mountinfo=Path("/proc/self/mountinfo"), warn=None):
    """A host PATH (resolved) given to a sandbox that is, lies in or holds the kernel's and the devices' trees
    (KERNEL_TREES), lies in a file system of the kernel's own (kernel_mount_holding) or holds one
    (kernel_mounts_below): you decide, so it is given, with a warning (WARN, default: warner) of what the sandbox
    gets with it - read-only too - and that the engine may not be able to bind it. The warning, or None."""
    say = warner(warn)
    path = Path(path)
    for tree, what in KERNEL_TREES:
        if path == tree or tree in path.parents or path in tree.parents:
            where = "lies in" if tree in path.parents else "is" if path == tree else "holds"
            text = (f"{option} {path} {where} {tree}, the kernel's and the devices' tree: the agent gets {what} with "
                    f"it, even read-only; {SAFER_READ_ONLY} all the same, --device gives a single device; "
                    f"{ENGINE_BIND}")
            say(text)
            return text
    holding = kernel_mount_holding(path, mountinfo)
    if holding:
        text = (f"{option} {path} lies in {holding[0]}, a file system of the kernel's own ({holding[1]}): the agent "
                f"gets the host's processes, devices or control files with it, even read-only; {SAFER_READ_ONLY} all "
                f"the same; {ENGINE_BIND}")
        say(text)
        return text
    found = kernel_mounts_below(path, mountinfo)
    if found:
        shown = ", ".join(f"{point} ({kind})" for point, kind in found[:5]) + (" ..." if len(found) > 5 else "")
        text = (f"{option} {path} holds file systems of the kernel's own: {shown}; the agent gets them with it (the "
                "mounts are recursive): the host's processes, devices or control files, even read-only; "
                f"{SAFER_READ_ONLY} all the same; {ENGINE_BIND}")
        say(text)
        return text
    return None


def check_workspace_scope(workspace, own=(), warn=None, refused=()):
    """A workspace (a folder, or a single file added during the run) that hands the agent the launcher itself or the
    host user's world - the root directory, HOME, the temporary directory, XDG_RUNTIME_DIR, the launcher's tree
    (launcher_trees: anything at, below or above it), the kernel's trees, a folder of sockets, sandbox-ai's own
    folders (OWN: those of the internal start this is; REFUSED: further ones a start recorded), a private location:
    you decide, so it is given, with a warning (WARN, default: warner) for each risk that names it.
    [warning] of what was said."""
    workspace, said = Path(workspace), []

    def say(text):
        said.append(text)
        warner(warn)(text)
    everything = ("the agent can change your shell start files, credentials and sandbox-ai's trust store: "
                  f"everything you can do on this host; {SAFER_READ_ONLY}, though it still reads all of it (keys, "
                  "tokens)")
    risky = {Path("/"): ("the root directory", everything), Path.home().resolve(): ("your HOME directory", everything),
             Path(tempfile.gettempdir()).resolve(): (
                 "the temporary directory", "the agent sees and can change what your other programs keep there "
                 f"(their sockets, lock files, downloads, other users' files); {SAFER_READ_ONLY}")}
    if os.environ.get("XDG_RUNTIME_DIR"):
        risky.setdefault(Path(os.environ["XDG_RUNTIME_DIR"]).resolve(), (
            "XDG_RUNTIME_DIR", "the agent can connect to the sockets of your session there (its bus, Podman's, "
                               "gpg-agent's, your SSH connections) and run what they run as you, even through a "
                               f"read-only mount; {SAFER_READ_ONLY} for its files, not for its sockets"))
    for path, (label, risk) in risky.items():
        if workspace == path or workspace in path.parents:
            say(f"Workspace {workspace} {'is' if workspace == path else 'contains'} {label}: {risk}")
    check_given_path(workspace, "Workspace", True, own, say, refused)
    return said


def host_sources(a):
    """[(host path, label)]: every host path the launcher reads, mounts or writes back to that must lie outside the
    trees the agent can write (exposed_trees). Recorded in run.json, so that a workspace added during the run
    (sandbox-ai workspace add) is held to them as well."""
    found = []
    if a.copy_auth or auth_mode(a) == "auto":
        # CODEX_HOME, CLAUDE_CONFIG_DIR or XDG_* inside a workspace would let the agent plant a
        # symlink there that we would follow; Claude's file is also the write-back target.
        for source in auth_source_candidates(a.agent):
            found.append((source, "Credential file (from CODEX_HOME, CLAUDE_CONFIG_DIR or XDG_DATA_HOME)"))
    if a.agent == "opencode":
        found.append((opencode_catalog(), "OpenCode model catalog (from XDG_CACHE_HOME)"))
    for path, option in [(a.local_config, "--local-config"), (a.auth_file, "--auth-file"),
                         (a.import_codex_auth, "--import-codex-auth")]:
        if path:
            found.append((path, option))
    for value in a.git_token_file:
        # A token file the agent could replace would hand it any host file as a "token".
        found.append((git_token_credential(value, "file")["file"], "--git-token-file"))
    if a.git_credentials:
        found.append((a.git_credentials, "--git-credentials"))
    # The control socket and the approvals live there; the agent must not reach them through a mount.
    found.append((runs_dir(), "The run directory (XDG_RUNTIME_DIR, else XDG_STATE_HOME)"))
    if privacy_rules_dir(a):
        # The agent must neither read the rules (they name what is private) nor change them.
        found.append((privacy_rules_dir(a), "--privacy-rules"))
    # Rules the agent could rewrite would be no rules (and it should not learn them either).
    found.append((user_rules_file(), "Your rules (XDG_CONFIG_HOME)"))
    # The preset this start comes from (--preset): an agent must not write what it is started with.
    preset = _presets_module.LOADED if _presets_module is not None else None
    if preset and preset.get("_origin") != "built-in":
        found.append((preset["_path"], f"The preset {preset.get('id')} ({preset['_origin']})"))
    for path in a.rules:
        found.append((path, "--rules"))
    for value in a.copy:
        if ":" in value:
            found.append((value.rsplit(":", 1)[0], "--copy"))
    if kits_apply(a):
        kits = load_kits_module()
        for label, path in kits.sources(a):
            found.append((path, f"Kit ({label})"))
        for value in dict.fromkeys(a.kit_dir):  # the folder given, also beside its AGENT/ (held both ways)
            found.append((Path(os.path.normpath(Path(value).expanduser().absolute())), "--kit-dir"))
        # Approvals the agent could write would be no approvals.
        found.append((kits.trust_file(), "Approved kit hooks (XDG_CONFIG_HOME)"))
        if not a.no_default_kit:
            found.append((kits.default_home(a.agent), "Default kit"))
        if not a.no_memory:
            found.append((kits.memory_store(a.workspaces[0], a.agent), "Project memory"))
        if a.agent == "claude" and not a.no_plugins:
            found.append((kits.plugin_store("claude"), "Plugin store"))
    return found


def host_stores(a):
    """[(path, label)] of the stores no agent-writable tree may lie in either, whatever this start's agent and
    options (every sandbox reads them, or the next start of another agent does): the default kits of every agent,
    the kits directory, the project memory and the plugin store. The plugin installer's own start (own_folders: its
    HOME is the plugin store) is the store's only writer."""
    kits = load_kits_module()
    own = own_folders(a)
    stores = [(kits.default_home(agent), f"the default kit of {agent}") for agent in kits.AGENTS]
    stores += [(kits.kits_dir(), "the kits directory"), (kits.state_root() / "memory", "the project memory")]
    for agent in kits.AGENTS:
        store = kits.plugin_store(agent)
        with contextlib.suppress(OSError, RuntimeError):
            if store.expanduser().resolve() in own:
                continue
        stores.append((store, "the plugin store"))
    return stores


def check_stores(trees, stores, warn=None):
    """A workspace, rw mount or --state-dir you chose (TREES) inside a store or kit (STORES: [(path, label)]): given,
    with a warning (WARN, default: warner) - the agent can change what every sandbox gets from it (or what its own
    next run reads back). [warning] of what was said."""
    said = []
    for tree in trees:
        for path, label in stores:
            path = Path(os.path.normpath(Path(path).expanduser().absolute()))
            if tree == path or path in tree.parents or tree == path.resolve() or path.resolve() in tree.parents:
                text = (f"{tree} lies inside {label} ({path}): the agent can change what every sandbox gets from it "
                        f"(and what its own next run reads back); {SAFER_READ_ONLY}, or choose another directory")
                said.append(text)
                warner(warn)(text)
    return said


def check_held_both_ways(trees, path, label, warn=None):
    """An agent-writable tree of TREES that lies inside the host path PATH the launcher reads (LABEL: what it is;
    --privacy-rules, a kit's folder): given, with a warning (WARN, default: warner) - the agent can change what the
    launcher reads there (check_host_source says the other way round, PATH inside a tree). The warning, or None."""
    literal = Path(os.path.normpath(Path(path).expanduser().absolute()))
    for held in dict.fromkeys([literal, literal.resolve()]):
        for tree in trees:
            if held in Path(tree).parents:
                text = (f"{tree} lies inside {path} ({label}), which the launcher reads: the agent can change what "
                        f"it reads there; {SAFER_READ_ONLY}, or choose a directory beside it")
                warner(warn)(text)
                return text
    return None


def check_copy_source(value, own=(), warn=None):
    """A --copy SRC:DEST whose source is read into the sandbox HOME: as a mount of it, a file of the kernel's own file
    systems (/proc/self/environ: the launcher's own environment, the host's tokens with it), of sandbox-ai's own
    folders (its secret store, logins and approvals: else a secret reaches the agent only as a placeholder the proxy
    fills in) or of a private location (keys, credentials, settings that run as you) is copied with a warning (WARN,
    default: warner). A login copied in on purpose (copies_credential: --copy of a login file, the manual auth mode)
    is the auth choice's, not warned about again. The first warning, or None."""
    if ":" not in value or copies_credential(value):
        return None
    try:
        path = Path(value.rsplit(":", 1)[0]).expanduser().resolve()
    except (OSError, RuntimeError):
        return None  # (copy_private says what is wrong with it)
    return check_kernel_mounts(path, "--copy", warn=warn) or check_private_location(path, "--copy", warn, own)


def private_source(option, source):
    """Why the host path SOURCE that OPTION (--copy, --mount) of a preset passed on names is not for that preset to
    give a sandbox: it is, holds or lies in a private location (keys, logins, credentials, settings that run as you:
    check_private_location's warning), sandbox-ai's own folders or a file system of the kernel's own. None
    otherwise (also when it cannot be resolved: the start says what is wrong with it)."""
    try:
        path = Path(source).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    quiet = lambda _text: None  # noqa: E731 - (what would be said is the preset's refusal)
    return check_kernel_mounts(path, option, warn=quiet) or check_private_location(path, option, warn=quiet)


def check_host_sources(a):
    """Every host path the launcher reads, mounts or writes back to lies outside agent-writable trees."""
    for path, label in host_sources(a):
        check_host_source(a, path, label)
        if label != "--copy":  # a copy goes into the sandbox only; what the launcher reads for itself is held both ways
            check_held_both_ways(exposed_trees(a), path, label)
    check_stores(exposed_trees(a), host_stores(a))
    workspaces = [Path(w).expanduser().resolve() for w in a.workspaces]
    state = [a.state_dir.expanduser().resolve()] if a.state_dir else []
    mounts = [(value, *parse_mount(value)) for value in a.mount]
    for value, src, _, _ in mounts:
        # Visible through a workspace or another rw mount, it could become a symlink anywhere.
        others = [Path(o_src) for o_value, o_src, _, mode in mounts if mode == "rw" and o_value != value]
        check_host_source(a, src, "--mount", workspaces + state + others)
    if a.state_dir:
        check_host_source(a, a.state_dir, "--state-dir",
                          workspaces + [Path(src) for _, src, _, _ in mounts])
    # The agent could replace a directory on the way to a workspace with a symlink, so that the
    # next run mounts any host directory read-write.
    rw = [Path(src) for _, src, _, mode in mounts if mode == "rw"]
    for workspace in a.workspaces:
        check_host_source(a, workspace, "--workspace", state + rw)
    # The same for every other host path: a symlink planted in an earlier run, or a private location.
    before = exposed_before()
    check_host_source(a, exposed_registry(), "The registry of exposed directories (XDG_STATE_HOME)")
    own = own_folders(a)
    for value, src, _, mode in mounts:
        check_planted(value.rsplit(":", 2)[0], "--mount", before)
        # Read-only too: a socket can be connected through it, a kernel file system read, sandbox-ai's own folders
        # read (its logins, the approvals) and a private location's keys.
        check_given_path(Path(src).resolve(), "--mount", mode == "rw", own)
    for value in a.copy:
        check_copy_source(value, own)
    if a.state_dir:
        check_planted(a.state_dir, "--state-dir", before)
        check_given_path(a.state_dir.expanduser().resolve(), "--state-dir", True, own)
    for path, option in [(a.log_dir, "--log-dir"), (a.gui_vnc, "--gui-vnc")]:
        if path:
            check_planted(path, option, before)
            check_output_dir(path, option)
    if a.kit_dir:
        writable = [tree for tree, _ in sandbox_writable(a)]  # also the running sandboxes' and kept sessions'
        for value in a.kit_dir:
            check_planted(value, "--kit-dir", writable)
    # An MCP image server mounts its folders again for every connection: the agent must not be able to swap
    # one of them for a link, through a workspace or rw mount that holds it.
    # (Each connection checks that the folder is still the one it was: one swapped for a link is refused then.)
    for host in image_mount_hosts(getattr(a, "project_document", None)):
        for tree in exposed_trees(a):
            if tree in host.parents:
                warner()(f"{tree} contains {host}, a folder an MCP image server mounts (see "
                         f"{Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')}/sandbox-ai/mcp.json): "
                         "the agent can change what the server keeps there, or replace the folder with a link (the "
                         f"server's next connection is refused then); {SAFER_READ_ONLY}, or use a workspace beside it")
    # The same for the folders this start gives its MCP servers (--mcp-mount).
    if getattr(a, "mcp_mount", None):
        for host in load_mcp_module().start_mount_hosts(a):
            for tree in exposed_trees(a):
                if tree in host.parents:
                    warner()(f"--mcp-mount: {host} lies in {tree}, which the agent can write: it can change what the "
                             "MCP server keeps there behind its back, or replace the folder with a link (the "
                             f"server's next connection is refused then); {SAFER_READ_ONLY}, or keep the server's "
                             "folder outside the workspace (e.g. ~/brains/PROJECT)")


def prepare_workspace(path, warn=True, say=True):
    """Create the workspace PATH (with the folders above it), said; an existing one is used, with a warning unless
    WARN is false (a folder sandbox-ai made for this very start, such as the plugin installer's work folder); SAY:
    that it was made is said on stderr. What
    keeps it from being made is said in words (a ValueError), never as a bare errno."""
    workspace = Path(path)
    try:
        workspace.mkdir(parents=True)
    except FileExistsError:
        if not workspace.is_dir():
            raise ValueError(f"Workspace path exists but is not a directory: {workspace}") from None
        if warn:
            print(f"agent-sandbox: warning: workspace exists already; using it: {workspace}", file=sys.stderr)
        return
    except PermissionError as exc:
        raise ValueError(f"Workspace {printable(str(workspace))} cannot be created: "
                         f"{printable(str(exc.filename or workspace))} is not writable for you") from None
    except NotADirectoryError:
        blocking = next((part for part in workspace.parents if part.exists() and not part.is_dir()), workspace.parent)
        raise ValueError(f"Workspace {printable(str(workspace))} cannot be created: {printable(str(blocking))} is a "
                         "file, not a folder") from None
    except OSError as exc:
        raise ValueError(f"Workspace {printable(str(workspace))} cannot be created: "
                         f"{printable(exc.strerror or str(exc))}") from None
    if say:
        print(f"agent-sandbox: created the workspace {printable(str(workspace))} (it was not there)", file=sys.stderr)


def check_new_workspace(value, own=(), warn=None):
    """The folder VALUE named as a workspace that may not be there yet, checked as a start checks a workspace
    (validate): resolved, or a ValueError with the reason - a dangling link or one an agent planted in its path, a
    file in its way, a folder that cannot be made here. A risk you chose (the root, HOME, the temporary directory,
    XDG_RUNTIME_DIR, the kernel's trees, sandbox-ai's own tree or folders) is said with WARN (default: warner), never
    refused. Nothing is made (create_workspace makes it)."""
    source = Path(value).expanduser().absolute()
    if any(c in str(source) for c in (",", "\n", "\r")):
        raise ValueError("Workspace must be a directory without commas/control characters")
    if any(path.is_symlink() and not path.exists() for path in (source, *source.parents)):
        raise ValueError(f"Workspace path contains a dangling symlink: {printable(str(source))}")
    workspace = source.resolve(strict=False)
    check_planted(source, "Workspace", exposed_before())
    if workspace.exists() and not workspace.is_dir():
        raise ValueError(f"Workspace {printable(str(workspace))} is a file, not a folder")
    check_workspace_creatable(workspace)
    check_workspace_scope(workspace, own, warn)
    return workspace


def create_workspace(value, warn=None):
    """Make the folder VALUE (with the folders above it) that a start names as its workspace or project folder - the
    app's Create now, and its Start and Apply after a yes: checked first as a start checks it (check_new_workspace),
    so it is refused with the reason where a start would refuse it, and a risk a start would say is said with WARN
    (the app shows it). Returns the folder (one that is there is used as it is)."""
    workspace = check_new_workspace(value, warn=warn)
    prepare_workspace(workspace, warn=False, say=False)  # (the app says what it made)
    return workspace


def resolve_hosts(hosts):
    result = {}
    for host in hosts:
        try:
            addresses = sorted({str(ipaddress.ip_address(row[4][0])) for row in
                                socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot resolve allowed host {host}: {exc}") from None
        if not addresses:
            raise ValueError(f"No addresses for allowed host: {host}")
        result[host] = addresses
    return result


def hosts_file(hosts, own=()):
    """The sandbox's /etc/hosts: localhost, the sandbox's own host names OWN (the pod's, Bubblewrap's agent-sandbox)
    on the loopback addresses - git, sudo, hostname -f look their own name up; without it the lookup went to the DNS
    stub and was refused and logged as a name outside the allowed hosts - then the pinned HOSTS."""
    names = " ".join(["localhost", *dict.fromkeys(n for n in own if n and n != "localhost")])
    return f"127.0.0.1 {names}\n::1 {names}\n" + "".join(
        f"{addr} {name}\n" for name, addresses in hosts.items() for addr in addresses)


def own_host_names(a):
    """The host names the sandbox of A may call itself: its pod's (Podman gives a pod's containers the pod's name
    as host name), agent-sandbox (Bubblewrap's) and the names of its services."""
    names = ["agent-sandbox"]
    if getattr(a, "run_id", None) or getattr(a, "vpn_pod", None) or getattr(a, "pod_name", None):
        names.append(pod_name(a))
    # The services beside it (--service) share its network namespace: each one's name is the loopback too
    # (adb connect android-emulator:5555, psql -h db), as localhost:PORT is.
    names += [str(service.get("name")) for service in getattr(a, "services", None) or []]
    return [name for name in names if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", name or "")]


def podman_command(a, *args):
    return ["podman", *(["--remote", "--url", a.podman_url] if a.podman_url else []), *args]


PODMAN_HINTS = (
    # (a pattern in Podman's message, lower case; what helps), the first that matches. The message is read without
    # Podman's WARN lines (without a user session four of them come first, whatever the error).
    (r"image not known|no such image|image not found", "the image is not built on this host: sandbox-ai build {agent}"),
    # (before the runtime directory's: "mkdir /run/user/UID/...: no space left on device" - it is there, but full)
    (r"no space left on device|disk quota exceeded",
     "a file system Podman writes is full - its image store (podman info --format '{{{{.Store.GraphRoot}}}}'), its run "
     "directory (RunRoot, under XDG_RUNTIME_DIR: mostly a small tmpfs) or /tmp - or out of inodes: df -h and df -i "
     "show which; free space there (sandbox-ai gc removes what sandbox-ai left over), then start again"),
    (r"insufficient uids|insufficient gids",
     "the image uses higher IDs than /etc/subuid and /etc/subgid delegate (65536 cover every image); "
     "sandbox-ai doctor says how to get more"),
    (r"write to [ug]id_map failed",
     "the delegated IDs cannot be mapped: in an unprivileged container (LXC, Proxmox pct) they must lie within its "
     "own IDs, on a host newuidmap needs its setuid bit or file capabilities; sandbox-ai doctor says which"),
    (r"cannot re-exec process to join the existing user namespace|pause process",
     "a Podman process of an earlier session is in the way: podman system migrate"),
    (r"/dev/net/tun",
     "pasta needs /dev/net/tun, which this system lacks: in an LXC or Proxmox container its host passes it in "
     "(lxc.cgroup2.devices.allow: c 10:200 rwm and lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file "
     "in /etc/pve/lxc/ID.conf, restart it); on a host: sudo modprobe tun"),
    (r"could not find pasta|pasta.{0,40}(not found|no such file)|executable file not found",
     "the network needs pasta: install the package passt (Podman 4.4 or newer)"),
    (r"\bpasta\b|\bpasst\b", "pasta could not set up the network (its message above says why); sandbox-ai doctor "
                             "checks pasta and /dev/net/tun"),
    (r"newuidmap|newgidmap|subuid|subgid|cannot set up namespace|user namespace",
     "rootless Podman needs subordinate IDs: sandbox-ai doctor says which and how"),
    (r"xdg_runtime_dir|runtime directory|/run/user",
     "Podman has no runtime directory: log in by ssh or on the desktop (not with su or sudo), or run: loginctl "
     "enable-linger"),
    (r"cgroup", "Podman cannot use cgroups here: loginctl enable-linger (a systemd user session), or "
                "cgroup_manager = \"cgroupfs\" in ~/.config/containers/containers.conf"),
)
PODMAN_WARNING = re.compile(r"^(WARN\[\d+\]|time=\"[^\"]*\" level=warning)")


def podman_failure(a, exc):
    """What the human reads when the first Podman command of a start failed: who failed (125: Podman itself;
    126, 127: a program, pasta or the image's python3, could not be run or found; else the lookup inside the
    image), the end of the message without Podman's warnings (printable), and what helps when it names a known
    cause."""
    lines = [line for line in (exc.stderr or exc.stdout or "").strip().splitlines()
             if line.strip() and not PODMAN_WARNING.match(line.strip())]
    message = printable("\n".join(lines[-6:]))
    found = next((hint for pattern, hint in PODMAN_HINTS if re.search(pattern, message.lower())), None)
    hint = found.format(agent=a.agent) if found else "check the host with: sandbox-ai doctor"
    # 126 and 127: a program Podman needs did not start (pasta for the network, or the image's python3): missing,
    # or failing at once (pasta without /dev/net/tun gives 127 too).
    who = {125: "Podman failed", 126: "Podman could not start a program it needs (pasta, or the image's python3)",
           127: "Podman could not start a program it needs (pasta, or the image's python3)"}.get(
        exc.returncode, "the host lookup in the image failed")
    return f"{who} (exit status {exc.returncode}) before the sandbox started" + (f": {message}" if message else "") \
        + f"\n  -> {hint}"


def resolve_pod_hosts(a, hosts):
    if not hosts:
        return {}
    # DNS belongs to the selected engine's network, which can differ from the
    # launcher's namespace. No credentials or workspace enter this resolver.
    script = "import json,socket,sys; print(json.dumps({h:sorted({r[4][0] for r in socket.getaddrinfo(h,443,type=socket.SOCK_STREAM)}) for h in json.loads(sys.argv[1])}))"
    cmd = podman_command(a, "run", "--rm", "--pull=never", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--network",
                         a.network or POD_NETWORK,
                         f"--entrypoint={RUNTIME.SUPERVISOR_PYTHON[0]}", a.image, *RUNTIME.SUPERVISOR_PYTHON[1:], "-c",
                         script, json.dumps(hosts))
    try:
        result = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise ValueError("Podman did not resolve the allowed hosts within 45 s (the image's first start, or no "
                         "DNS on the host?); check the host with: sandbox-ai doctor") from None
    except subprocess.CalledProcessError as exc:
        # Podman's own message says why (exit status 125: Podman failed, not the program in the container).
        raise ValueError(podman_failure(a, exc)) from None
    answer = json.loads(result.stdout)
    if not isinstance(answer, dict) or set(answer) != set(hosts):
        raise ValueError("Podman DNS returned an incomplete host list")
    for host in hosts:
        if not isinstance(answer[host], list) or not answer[host]:
            raise ValueError(f"Podman DNS returned no addresses for {host}")
        answer[host] = sorted({str(ipaddress.ip_address(addr)) for addr in answer[host]})
    return answer


def looked_up_together(resolve, *groups, ahead=False):
    """RESOLVE for a start whose names come in GROUPS looked up apart (the allowed hosts, the names approved always,
    the --llm server's): all of them asked at once first - one Podman container instead of one per group - and each
    later lookup of names among them answered from that. When that one lookup fails (a name that does not resolve
    now), nothing is kept and each group is asked as before, with what it says when it fails. AHEAD (looked up while
    something else runs): asked even for one group alone."""
    together = list(dict.fromkeys(name for group in groups for name in group))
    if sum(1 for group in groups if group) < (1 if ahead else 2):
        return resolve  # one lookup at most anyway
    try:
        answer = resolve(together)
    except (ValueError, OSError, subprocess.SubprocessError):
        return resolve

    def lookup(names):
        names = list(names)
        if all(name in answer for name in names):
            return {name: list(answer[name]) for name in names}
        return resolve(names)
    return lookup


def looked_up_ahead(ahead, resolve):
    """The lookup that AHEAD (in_background of looked_up_together, or None) gives once it is done; RESOLVE when there
    is none or it gave nothing (each group then asked as before)."""
    if ahead is None:
        return resolve
    try:
        return ahead()
    except Exception:  # noqa: BLE001 - the names are asked as before, with what that says when it fails
        return resolve


def firewall_rules(networks, addresses, log=False, dns=(), allow_all=False, tcp=(), proxy=None, vpn_mark=None,
                   vpn_rules=(), machine_host=None, check_mark=None, lend_mark=None):
    """The sandbox's nftables table. PROXY ({"port", "loopback"}) redirects every TCP connection of the agent
    to the connection proxy, except the proxy's own (PROXY_UID) and those to loopback ports other than
    LOOPBACK (the host's forwarded ports). The chain "runtime" holds what `sandbox-ai net` allows later.
    VPN_MARK (an nft match, see vpn.py) lets the VPN's own packets pass, unredirected: only a process with
    CAP_NET_ADMIN can mark one, and the VPN's kill switch limits where they go. VPN_RULES (vpn.py: Tor's
    loopback ports) open the output chain, before loopback is accepted. MACHINE_HOST (macOS): the Mac's address in
    podman machine, reached only by the supervisor's relays of host ports (runtime.HOST_RELAY_MARK). CHECK_MARK (with
    a VPN: vpn_runtime.CHECK_MARK): the VPN container's own checks and lookups during the run pass this table, neither
    redirected to the proxy nor held by the allow-list, so that they measure the tunnel; the VPN's kill switch still
    holds them (only a process with CAP_NET_ADMIN can mark a packet, and the agent has none). The chain "cut" is the
    run's own kill switch (sandbox-ai net cut, runtime.cut_rules): empty until the human cuts the run's network, then
    it drops what the cut forbids before any other rule of the output chain (only the VPN container's checks come
    first). LEND_MARK (a start that lends its tunnel to its own MCP servers: vpn_runtime.LEND_MARK): the connections
    the VPN container makes for those servers pass this table too, unredirected and whatever the allow-list says -
    they are the servers', whose own sandboxes judged them - but only AFTER the cut chain: the kill switch holds them
    as it holds the agent's own packets (an emergency stop that left the servers' way out open would be none)."""
    for rule in vpn_rules:
        if not re.fullmatch(r"[a-z0-9 .:/,=-]{1,200}", rule):
            raise ValueError(f"not a firewall rule of a VPN: {rule!r}")
    lines = ["table inet agent_sandbox {"]
    if log:
        # Dropped destinations are collected here and polled by the bootstrap;
        # kernel log targets are disabled outside the initial network namespace.
        lines += [f" set denied{v} {{ type ipv{v}_addr . inet_proto . inet_service; flags dynamic; size 65536; }}"
                  for v in (4, 6)]
    lines += [
             " chain input { type filter hook input priority 0; policy drop;",
             '  iifname "lo" accept', "  ct state established,related accept",
             "  ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept", " }",
             " chain forward { type filter hook forward priority 0; policy drop; }",
             " chain runtime {", " }",
             # The kill switch (sandbox-ai net cut; runtime.cut_rules fills it during the run): before everything else.
             " chain cut {", " }",
             " chain output { type filter hook output priority 0; policy drop;",
             *([f"  meta mark {int(check_mark):#x} accept"] if check_mark else []),
             "  jump cut",
             # The start's tunnel, lent to its own MCP servers: after the cut, never before it.
             *([f"  meta mark {int(lend_mark):#x} accept"] if lend_mark else []),
             *[f"  {rule}" for rule in vpn_rules],
             '  oifname "lo" accept', "  ct state established,related accept",
             "  ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept"]
    if proxy:
        # Redirected to the proxy: after the NAT hook the packet goes to the proxy's port on loopback, but
        # this hook still sees the original output interface, so "oifname lo" does not match it.
        port = int(proxy["port"])
        lines += [f"  ct status dnat ip daddr 127.0.0.1 tcp dport {port} accept",
                  f"  ct status dnat ip6 daddr ::1 tcp dport {port} accept"]
    if vpn_mark:
        lines.append(f"  {vpn_mark} accept")
    if machine_host:
        lines.append(f"  meta mark {RUNTIME.HOST_RELAY_MARK:#x} ip daddr {ipaddress.IPv4Address(machine_host)} "
                     "meta l4proto tcp accept")
    lines += ["  jump runtime"]
    for raw in networks:
        net = ipaddress.ip_network(raw, strict=False)
        lines.append(f"  {'ip' if net.version == 4 else 'ip6'} daddr {net} accept")
    for raw in sorted(set(addresses)):
        net = ipaddress.ip_network(raw, strict=False)
        dest = net.network_address if net.num_addresses == 1 else net
        lines.append(f"  {'ip' if net.version == 4 else 'ip6'} daddr {dest} tcp dport 443 accept")
    for raw, port in tcp:
        addr = ipaddress.ip_address(raw)
        lines.append(f"  {'ip' if addr.version == 4 else 'ip6'} daddr {addr} tcp dport {int(port)} accept")
    for raw in dns:
        addr = ipaddress.ip_address(raw)
        family = "ip" if addr.version == 4 else "ip6"
        lines += [f"  {family} daddr {addr} {proto} dport 53 accept" for proto in ("udp", "tcp")]
    if allow_all:
        lines.append("  accept")
    elif log:
        lines += ["  meta nfproto ipv4 meta l4proto { tcp, udp } add @denied4 { ip daddr . meta l4proto . th dport }",
                  "  meta nfproto ipv6 meta l4proto { tcp, udp } add @denied6 { ip6 daddr . meta l4proto . th dport }"]
    lines.append(" }")
    if proxy:
        ports = sorted({int(port) for port in proxy.get("loopback", [])})
        # The relay chain first: the proxy chain jumps to it (a chain must exist before a rule names it).
        lines += [" chain relay {", " }",
                  " chain proxy { type nat hook output priority -100; policy accept;",
                  f"  meta skuid {PROXY_UID} return", *([f"  {vpn_mark} return"] if vpn_mark else []),
                  *([f"  meta mark {int(check_mark):#x} return"] if check_mark else []),
                  *([f"  meta mark {int(lend_mark):#x} return"] if lend_mark else []),
                  *([f"  meta mark {RUNTIME.HOST_RELAY_MARK:#x} return"] if machine_host else []),
                  # Host ports relayed during the run (sandbox-ai net allow --allow-host-port) go through the proxy too.
                  "  jump relay",
                  f"  ip daddr 127.0.0.0/8 tcp dport != {{ {', '.join(map(str, ports))} }} return" if ports
                  else "  ip daddr 127.0.0.0/8 return",
                  # The same for IPv6: pasta forwards [::1]:PORT too, and a push there must meet the proxy as well.
                  f"  ip6 daddr ::1 tcp dport != {{ {', '.join(map(str, ports))} }} return" if ports
                  else "  ip6 daddr ::1 return",
                  f"  meta l4proto tcp redirect to :{int(proxy['port'])}", " }"]
    return "\n".join(lines + ["}", ""])


def copy_private(source, home, relative, content=None):
    """Copy SOURCE (or write CONTENT bytes instead) to a new private file in the sandbox HOME."""
    source = Path(source).expanduser().resolve(strict=True)
    relative = PurePosixPath(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Copy destination must be a relative path inside sandbox home")
    target = home.joinpath(*relative.parts)
    for parent in [target, *target.parents]:
        if parent == home:
            break
        if parent.is_symlink():
            raise ValueError("Copy destination contains a symlink")
    if target.exists():
        raise ValueError(f"Refusing to overwrite existing sandbox file: {relative}; omit --copy-auth/--copy when reusing state")
    if not source.is_file():
        raise ValueError("Copy source must be a regular file")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent
    while parent != home:
        parent.chmod(0o700)
        parent = parent.parent
    if content is not None:
        with target.open("xb") as dst:
            os.chmod(target, 0o600)
            dst.write(content)
        return
    with source.open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        shutil.copyfileobj(src, dst)


# The only parts of the host's ~/.claude.json a sandbox gets: the account and
# first-run state. MCP servers (often with API keys), project paths and their
# history stay on the host.
CLAUDE_STATE_KEYS = ("oauthAccount", "userID", "hasCompletedOnboarding", "lastOnboardingVersion", "theme")


def claude_state_subset(source):
    """CLAUDE_STATE_KEYS of the host's ~/.claude.json as JSON bytes, or None if unreadable."""
    try:
        document = json.loads(read_config_bytes(Path(source).resolve(), CLAUDE_STATE_LIMIT))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    return json.dumps({key: document[key] for key in CLAUDE_STATE_KEYS if key in document}, indent=2).encode()


def copy_auth_file(source, home, dest):
    """Copy one discovered credential file; ~/.claude.json only as its account subset, Claude's
    .credentials.json only as its login (it also holds OAuth tokens of the host's MCP servers)."""
    if dest == AUTH_TARGETS["claude"]:
        oauth = claude_oauth(source)
        if oauth is None:
            print(f"agent-sandbox: {source} holds no usable Claude login; not copied", file=sys.stderr)
            return None
        login = {**oauth_fields(oauth, OAUTH_INFO_FIELDS), **oauth_fields(oauth, OAUTH_TOKEN_FIELDS)}
        return copy_private(source, home, dest, json.dumps({"claudeAiOauth": login}).encode())
    if dest != ".claude.json":
        return copy_private(source, home, dest)
    content = claude_state_subset(source)
    if content is None:
        print(f"agent-sandbox: cannot read {source}; Claude starts without its account state", file=sys.stderr)
        return None
    return copy_private(source, home, dest, content)


def auth_store(agent=None):
    """sandbox-ai's own logins for sandboxes (sandbox-ai login AGENT): one small HOME per agent,
    kept apart from the host's login so that host and sandboxes never share a rotating refresh token."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "auth"
    return base / agent if agent else base


def store_login(agent):
    """True if the login store holds a login for AGENT; sandboxes then use it instead of the host's."""
    path = auth_store(agent) / AUTH_TARGETS[agent]
    if agent == "claude":
        return claude_oauth(path, follow=False) is not None
    return stored_document(agent, path) is not None


def stored_document(agent, path):
    """The Codex or OpenCode login at PATH if it has the agent's structure, else None; no symlinks."""
    try:
        document = json.loads(read_config_bytes(path))
    except (OSError, ValueError, RecursionError):
        return None
    return document if login_document_ok(agent, document) else None


def login_document_ok(agent, document):
    """Codex: ChatGPT tokens with an account id, or an API key. OpenCode: provider entries with a type."""
    if not isinstance(document, dict) or not document:
        return False
    if agent == "hermes":
        return load_hermes_module().login_document_ok(document)
    if agent in ("dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi"):
        return agent_module(agent).login_document_ok(document)
    if agent == "codex":
        tokens = document.get("tokens")
        return (isinstance(tokens, dict) and all(isinstance(tokens.get(key), str) and tokens[key]
                                                 for key in ("access_token", "refresh_token", "account_id"))
                or isinstance(document.get("OPENAI_API_KEY"), str) and bool(document["OPENAI_API_KEY"]))
    return all(isinstance(entry, dict) and isinstance(entry.get("type"), str) for entry in document.values())


def merge_refreshed(agent, stored, sandbox):
    """STORED with only the refreshed tokens of the same account taken from SANDBOX, or None.

    The sandbox's file is the agent's to write: it must not add providers, change API keys or switch the
    stored login to another account, since every later sandbox of this agent would use it."""
    if not isinstance(sandbox, dict):
        return None
    merged = json.loads(json.dumps(stored))
    if agent == "codex":
        old, new = stored.get("tokens"), sandbox.get("tokens")
        if not (isinstance(old, dict) and isinstance(new, dict) and old.get("account_id") == new.get("account_id")):
            return None
        for key in ("id_token", "access_token", "refresh_token"):
            if isinstance(new.get(key), str) and new[key]:
                merged["tokens"][key] = new[key]
        if isinstance(sandbox.get("last_refresh"), str):
            merged["last_refresh"] = sandbox["last_refresh"]
    elif agent == "qwen":
        # Qwen OAuth: the refreshed tokens of the same login (its resource URL unchanged), nothing else.
        if not (isinstance(sandbox.get("refresh_token"), str) and sandbox["refresh_token"]
                and sandbox.get("resource_url") == stored.get("resource_url")):
            return None
        for key in ("access_token", "refresh_token"):
            if isinstance(sandbox.get(key), str) and sandbox[key]:
                merged[key] = sandbox[key]
        if isinstance(sandbox.get("expiry_date"), int) and not isinstance(sandbox["expiry_date"], bool):
            merged["expiry_date"] = sandbox["expiry_date"]
    elif agent in ("hermes", "dsh", "goose", "openhands", "nanobot", "letta", "opencode2"):
        return None
    else:
        for provider, old in stored.items():
            new = sandbox.get(provider)
            if not (isinstance(old, dict) and old.get("type") == "oauth" and isinstance(new, dict)
                    and new.get("type") == "oauth" and new.get("accountId") == old.get("accountId")):
                continue
            for key in ("access", "refresh"):
                if isinstance(new.get(key), str) and new[key]:
                    merged[provider][key] = new[key]
            if timestamp_ms(new.get("expires")):
                merged[provider]["expires"] = new["expires"]
    return merged if merged != stored else None


def store_sources(agent):
    store = auth_store(agent)
    return [(store / AUTH_TARGETS[agent], AUTH_TARGETS[agent])] + (
        [(store / ".claude.json", ".claude.json")] if agent == "claude" else [])


def auth_source_candidates(agent):
    """Every path auth_sources() may pick, computed without touching the file system."""
    home = Path.home()
    store = [source for source, _ in store_sources(agent)]
    if agent == "codex":
        return store + [Path(os.environ.get("CODEX_HOME") or (home / ".codex")) / "auth.json"]
    if agent == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
        return store + [config / ".credentials.json", config / ".claude.json", home / ".claude.json"]
    return store + [Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share")) / "opencode/auth.json"]


TEAM_IMAGE = "localhost/team-sandbox:latest"  # Claude Code, Codex and OpenCode in one image (--with)
# The other agents --with: layers on it, one image per set of them (team_image.py: localhost/team-A-B-sandbox).
CAIRN_IMAGE = "localhost/sandbox-ai-cairn:latest"  # --cairn's own container, cairn.py's IMAGE (sandbox-ai build cairn)
ARTEX_IMAGE = "localhost/sandbox-ai-artex:latest"  # --artex's own container, artex.py's IMAGE (sandbox-ai build artex)
BROWSER_IMAGE = "localhost/sandbox-ai-browser:latest"  # --browser's own container (sandbox-ai build browser)
# The VPN's and Tor's containers (vpn.VPN_IMAGE, vpn.TOR_IMAGE): sandbox-ai's own images, built as the agent's are.
OWN_VPN_IMAGES = {"localhost/sandbox-ai-tor:latest": "sandbox-ai build tor",
                  "localhost/sandbox-ai-vpn:latest": "sandbox-ai build vpn"}


def companion_choices():
    """The agents --with takes: every agent of the team image."""
    return [agent for agent in sorted(AUTH_TARGETS) if agent not in NOT_IN_TEAM]


def companion_list(value):
    """--with's VALUE as a list of agents: AGENT or AGENT,AGENT... (blanks around a name and an empty last item are
    allowed; `--with hermes,qwen` = `--with hermes --with qwen`); argparse.ArgumentTypeError names a wrong one and
    the choices."""
    names = [name.strip() for name in str(value).split(",")]
    if names and names[-1] == "" and len(names) > 1:
        names.pop()
    choices = companion_choices()
    wrong = [name for name in names if name not in choices]
    if wrong:
        raise argparse.ArgumentTypeError(
            f"invalid agent {wrong[0]!r} (choose from {', '.join(choices)}; several as AGENT,AGENT or --with repeated)")
    return names


def companions(a):
    """The agents --with, without the lead itself, each once."""
    return [agent for agent in dict.fromkeys(getattr(a, "companions", None) or []) if agent != a.agent]


_team_image_module = None


def load_team_image_module():
    """team_image.py: the team image's names and its layers; loaded once."""
    global _team_image_module
    if _team_image_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_team_image", HERE / "team_image.py")
        with LOADING:
            if _team_image_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _team_image_module = loaded
    return _team_image_module


def team_image_for(agents):
    """The team image that holds AGENTS: TEAM_IMAGE, or it with the others as layers (localhost/team-A-B-sandbox).
    ValueError for an agent no team image has."""
    return load_team_image_module().image(agents)


def team_image_agents(image):
    """The agents the team image IMAGE holds (its base's, then its layers), or None for another image."""
    return load_team_image_module().agents_of(image) if image else None


def pick_team_image(wanted, present):
    """The team image a start with --with takes: WANTED (team_image_for the run's agents) when PRESENT (the image
    names Podman has) has it, else the smallest one there that holds every agent of WANTED (the fewest agents, then
    by name), else WANTED (which the start builds or names). Any other image: WANTED as it is."""
    needed = team_image_agents(wanted)
    if needed is None or wanted in present:
        return wanted
    holding = [(len(agents), name) for name, agents in ((name, team_image_agents(name)) for name in present)
               if agents is not None and set(needed) <= set(agents)]
    return min(holding)[1] if holding else wanted


def image_agents(a):
    """The agents whose command the sandbox has: the start's agent and its --with, and with Podman every agent of its
    team image (Cairn's: Claude Code, Codex and OpenCode) - each can be typed in its shell. With Bubblewrap the
    host's own."""
    agents = team(a)
    if getattr(a, "backend", "podman") != "bwrap":
        image = getattr(a, "image", None)
        held = list(TEAM_BASE_AGENTS) if image == CAIRN_IMAGE else team_image_agents(image)
        agents = [*agents, *(held or [])]
    return list(dict.fromkeys(agents))


def team(a):
    """Every agent of the run: the lead, then the agents --with."""
    return [a.agent, *companions(a)]


def check_companions(a):
    """--with AGENT: other agents in this sandbox, for an agent's own start."""
    if a.agent in (a.companions or []):
        raise ValueError(f"--with {a.agent}: that is the agent of this start")
    if a.companions and (a.mcp_socket_dir or a.probe):
        raise ValueError("--with is for an agent's start, not for MCP servers or probes")
    if companions(a) and (a.offline or getattr(a, "llm", None)):
        raise ValueError(f"--with: each agent reaches its own provider, which {'--offline' if a.offline else '--llm'} "
                         "keeps out of this run")
    if {"opencode", "opencode2"} <= set(team(a)):
        raise ValueError("--with: OpenCode 2 and OpenCode (1) read the same folders of the HOME (~/.config/opencode, "
                         "~/.local/share/opencode) and the same variables (OPENCODE_CONFIG_CONTENT): one of them per "
                         "sandbox; give the other a sandbox of its own (--helper, or a team member)")
    for agent in companions(a):
        if agent in NO_LOGIN_AGENTS and not getattr(a, "api_key", None) and not getattr(a, "dry_run", False):
            title = UNSWITCHABLE_TELEMETRY.get(agent, agent)
            print(f"agent-sandbox: note: --with {agent}: {title} has no login; it reaches a model with this start's "
                  "--api-key HOST=NAME[=SECRET] (it gets this start's keys), or with what you set up for it in the "
                  "sandbox's HOME", file=sys.stderr)


def companion_view(a, agent, strict=True):
    """The start A as AGENT's own configuration sees it when AGENT is one of its --with (or, STRICT=False, another
    agent of its image): the run's MCP servers, limits, network, telemetry level, --dangerously-skip-permissions and
    AGENT's own --AGENT-enable; not the lead's arguments, model, effort or thinking, which are its agent's. A shallow
    copy: what its features open (network_options) is the run's."""
    if strict and agent not in companions(a):
        raise ValueError(f"{agent} is no agent --with of this start")
    view = copy.copy(a)
    view.agent, view.companions, view.agent_args, view.companion_of = agent, [], [], a.agent
    view.model = view.effort = view.show_thinking = None
    # The kits' and plugins' hooks are the lead's (staged for its agent): a companion gets the rule hooks alone, as
    # Claude Code, Codex and OpenCode --with do (hook_files).
    view.kit_hooks, view.plugin_hooks = [], []
    return view


def agent_view(a, agent):
    """A itself for its own agent, companion_view for one --with."""
    return a if agent == a.agent else companion_view(a, agent)


def managed_companions(a):
    """The agents --with that get a read-only configuration of their own, as in their own image (Hermes', DSH's
    patch, Qwen Code's system settings, Goose's, OpenHands', nanobot's, Letta Code's start, OpenCode 2's layer, Pi's
    extension), when the sandbox runs its agent."""
    if not runs_agent(a) or a.probe or getattr(a, "mcp_socket_dir", None):
        return []
    return [agent for agent in companions(a) if agent == "hermes" or agent_module(agent) is not None]


def companion_files(a):
    """{path in the session directory: content} of the managed configuration of each agent --with (managed_companions):
    each as its own start writes it for this run (hermes_files, agent_files). Qwen Code's system settings hold the
    run's MCP servers too (its own start passes them as --mcp-config, which the lead does not type)."""
    files = {}
    for agent in managed_companions(a):
        view = companion_view(a, agent)
        found = hermes_files(view) if agent == "hermes" else agent_files(view)
        if agent == "qwen" and a.mcp:
            qwen = load_qwen_module()
            path = f"{qwen.SESSION_DIR}/settings.json"
            settings = json.loads(found[path])
            settings["mcpServers"] = json.loads(qwen.mcp_args(a.mcp, mcp_bridge_args,
                                                              mcp_tool_timeouts(a))[1])["mcpServers"]
            found[path] = json.dumps(settings, indent=2) + "\n"
        files.update(found)
    return files


def companion_managed_mounts(a, session):
    """[(directory in SESSION, target)]: the managed configuration of each agent --with (companion_files), mounted
    read-only where its own image has it (/etc/hermes, /etc/qwen-code ...)."""
    found = []
    for agent in managed_companions(a):
        module = load_hermes_module() if agent == "hermes" else agent_module(agent)
        found.append((Path(session) / module.SESSION_DIR, module.MANAGED_DIR))
    return found


def companion_prelude(a):
    """Shell lines the sandbox runs before its agent for the agents --with that need a step first: Hermes' shared
    metrics question answered (as before each start of Hermes: hermes.start_command), Letta Code's start script
    laying the run's MCP servers into its agents (it then becomes /bin/true; Letta Code keeps its servers per agent).
    [] when the sandbox does not run its agent."""
    lines = []
    if not managed_companions(a):
        return lines
    if "hermes" in companions(a):
        hermes = load_hermes_module()
        lines.append(f"{hermes.shell_quote(hermes.HERMES_PYTHON)} -I "
                     f"{hermes.shell_quote(hermes.MANAGED_DIR + '/' + hermes.TELEMETRY_ANSWER)}")
    if "letta" in companions(a) and (a.mcp or hooks_active(a)):
        # Without rule hooks a failure is said by the script and the lead starts all the same; with them, the
        # sandbox does not start without them (as Letta Code's own start refuses to).
        lines.append(f"python3 -I {load_letta_module().START} /bin/true" + (" || exit 1" if hooks_active(a)
                                                                            else " || true"))
    return lines


def with_companion_prelude(a, command):
    """COMMAND (the sandbox's agent) after companion_prelude's lines, through /bin/sh (exec: the session's program is
    still the agent); COMMAND itself without any."""
    lines = companion_prelude(a)
    if not lines:
        return command
    return ["/bin/sh", "-c", "\n".join([*lines, 'exec "$0" "$@"']), *command]


# Codex in a sandbox that has no login, with nothing else configured for it: Codex's device sign-in runs first, in
# the agent's own terminal (its page and one-time code: the desktop app opens the page in the human's browser and
# shows the code; on a terminal it is opened by hand). Before, Codex showed its own sign-in menu, whose first choice
# (a browser login with a callback to the sandbox's 127.0.0.1:1455) cannot finish from a browser outside the sandbox.
# Decided here for what the start's options say, and in the sandbox for what is there when it runs: a login in its
# HOME (the login store's or the host's copied in, a kept HOME's own), a key in its environment, a model provider of
# its own in its config.toml (a kit's: Codex needs no OpenAI login then), a terminal.
DEVICE_LOGIN_PRELUDE = """if [ -t 0 ] && [ -t 1 ] && [ ! -s "$HOME/.codex/auth.json" ] && \
[ -z "${{OPENAI_API_KEY:-}}${{CODEX_API_KEY:-}}" ] && \
! grep -Eqs '^[[:space:]]*model_provider[[:space:]]*=' "$HOME/.codex/config.toml"; then
  echo "agent-sandbox: Codex has no login in this sandbox: its device sign-in starts now - open the link it shows \
and enter the one-time code (Ctrl-C leaves it out: Codex then asks itself)" >&2
  trap : INT
  if "$0" {options} login --device-auth && [ -s "$HOME/.codex/auth.json" ]; then
    echo "agent-sandbox: signed in; this login stays in this sandbox's HOME - for every Codex sandbox, log in \
once on the host: sandbox-ai login codex" >&2
  else
    echo "agent-sandbox: no login was made; Codex starts without one" >&2
  fi
  trap - INT
fi"""


def device_login_prelude(a):
    """Shell lines the sandbox runs before Codex (its $0) when Codex is the start's own, interactive agent and
    nothing else is configured for its login - no --no-auth, --auth-file, --copy-auth, --auth-from-session,
    --import-codex-auth, --llm, --api-key or --local-config, no model provider of its own (-c model_provider=...),
    the provider's hosts reachable (not --offline or --no-provider-defaults): Codex's device sign-in, if the
    sandbox then has no login (DEVICE_LOGIN_PRELUDE), with
    Codex's telemetry off as for the agent itself. [] otherwise: for every other agent, a task, a probe, a helper."""
    if a.agent != "codex" or a.binary is not None or a.probe or getattr(a, "helper_of", None) or \
            getattr(a, "mcp_socket_dir", None):
        return []
    if auth_mode(a) != "auto" or getattr(a, "api_key", None) or a.local_config or a.offline or \
            a.no_provider_defaults or not interactive_args(a.agent, a.agent_args):
        return []
    if any("model_provider" in str(arg) for arg in a.agent_args):
        return []  # (-c model_provider=...: another provider, chosen for this run)
    return [DEVICE_LOGIN_PRELUDE.format(options=shlex.join(codex_telemetry_args(telemetry_level(a))))]


def with_start_prelude(a, command):
    """COMMAND (the sandbox's agent) after what runs before it at a start - Codex's device sign-in where it has no
    login (device_login_prelude), then companion_prelude's lines - through /bin/sh (exec: the session's program is
    still the agent); COMMAND itself without any. An agent started again in its window (agent_restart_script) gets
    with_companion_prelude's only."""
    lines = [*device_login_prelude(a), *companion_prelude(a)]
    if not lines:
        return command
    return ["/bin/sh", "-c", "\n".join([*lines, 'exec "$0" "$@"']), *command]


def companion_auth(a, agent=None):
    """How the agents --with (or AGENT among them) get their logins: "none" with --no-auth or --llm (the lead's
    choice holds for them); "manual" for AGENT when a --copy writes its login file (that one is kept, never
    replaced by the host's); else "auto": the login store's or the host's default files, when there are any."""
    if a.no_auth or getattr(a, "llm", None):
        return "none"
    targets = {AUTH_TARGETS[agent], *([".claude.json"] if agent == "claude" else [])} if agent else set()
    if any(":" in value and str(PurePosixPath(value.rsplit(":", 1)[1])) in targets for value in a.copy):
        return "manual"
    return "auto"


def companion_logins(a, home):
    """Copy each --with agent's login into HOME (auth_sources, like the lead's automatic mode); a login already in a
    reused HOME is kept. Returns [(source, target)]."""
    copied = []
    if companion_auth(a) == "none":
        return copied
    for agent in companions(a):
        if companion_auth(a, agent) != "auto" or agent in NO_LOGIN_AGENTS:
            continue  # (one without a login of its own: nothing of the host's, e.g. Goose's secrets.yaml)
        for source, dest in auth_sources(agent):
            target = home / dest
            if target.is_symlink():
                raise ValueError(f"Auth destination is a symlink: {dest}")
            if source.is_file() and not target.exists():
                copy_auth_file(source, home, dest)
                copied.append((source, dest))
    return copied


def telemetry_level(a):
    """off or strict: --telemetry, else "telemetry" in your defaults file, else off; never below the security mode's
    floor (strict for paranoid; modes.py refuses an option below it). Never on: that level is gone (an old one is
    ignored where it is read: TelemetryOption, load_user_defaults), and anything else counts as off."""
    chosen = getattr(a, "telemetry", None) or getattr(a, "telemetry_default", None) or "off"
    chosen = chosen if chosen in TELEMETRY_LEVELS else "off"
    floor = MODES.MODES.get(getattr(a, "mode", None) or "medium", {}).get("telemetry")
    return floor if floor in TELEMETRY_LEVELS and TELEMETRY_LEVELS.index(floor) > TELEMETRY_LEVELS.index(chosen) \
        else chosen


def telemetry_environment(a):
    """The variables that keep the run's agents (the lead and the agents --with) from reporting to their makers:
    set last (agent_environment), so nothing passed undoes them."""
    level, env = telemetry_level(a), {}
    if not runs_agent(a):
        return env
    for agent in team(a):
        env.update(TELEMETRY_ENV[agent]["off"])
        if level == "strict":
            env.update(TELEMETRY_ENV[agent]["strict"])
    return env


def telemetry_plan(a):
    """The dry run's "telemetry": the level, where it came from and what it sets."""
    level = telemetry_level(a)
    origin = ("--telemetry" if getattr(a, "telemetry", None) else
              str(defaults_file()) if getattr(a, "telemetry_default", None) else "default")
    if level != (getattr(a, "telemetry", None) or getattr(a, "telemetry_default", None) or "off"):
        origin = f"--mode {a.mode}"
    agents = team(a) if runs_agent(a) else []
    return {"level": level, "origin": origin, "environment": sorted(telemetry_environment(a)),
            "opencode_config": OPENCODE_TELEMETRY_CONFIG if "opencode" in agents else None,
            "codex_config": [".".join(filter(None, (table, key))) for table, key, _, _ in codex_telemetry_keys(level)]
            if "codex" in agents else [],
            # Also from the sandbox's shell: each command guarded, each managed configuration pinned (read-only).
            "guarded": {agent: {"command": f"{TELEMETRY_GUARD.COMMAND_DIR}/{agent}",
                                **({"managed": "/".join(TELEMETRY_MANAGED[agent])} if agent in TELEMETRY_MANAGED
                                   else {}),
                                # (an alias another agent of the image has as its own command stays that agent's:
                                # the team image's `opencode` is OpenCode 1's)
                                **({"also": [f"{TELEMETRY_GUARD.COMMAND_DIR}/{alias}"
                                             for alias in TELEMETRY_GUARD.ALIASES[agent]
                                             if alias not in image_agents(a)]}
                                   if agent in TELEMETRY_GUARD.ALIASES else {}),
                                **({"python": f"{TELEMETRY_GUARD.SITE_PACKAGES[agent]}/{TELEMETRY_GUARD.PTH}"}
                                   if TELEMETRY_GUARD.KINDS[agent] == "python" else {})}
                        for agent in telemetry_guarded(a)}}


def unswitchable_telemetry_rules(a):
    """The proxy's rules for the telemetry of A's agent, one whose telemetry is never switchable (its module's): at
    every level."""
    if a.agent == "hermes":
        return load_hermes_module().telemetry_rules()  # Hermes': at every level, never switchable
    if a.agent in ("dsh", "qwen", "openhands"):
        return agent_module(a.agent).telemetry_rules()  # theirs too: at every level, never switchable
    # Goose's (and its model catalog's download unless enabled), nanobot's (and its update check unless enabled),
    # Letta Code's (its hosts and its paths on api.letta.com), OpenCode 2's (remote pairing's, its update check's
    # path), Pi's (its report and bug paths, gists unless share).
    return agent_module(a.agent).telemetry_rules(a)


def telemetry_rules(a):
    """The ruleset "telemetry" (every run with the proxy: telemetry is always off): the agents' telemetry to the hosts
    they need anyway is refused even if an agent ignores its switches. Not for hosts passed unopened
    (--no-inspect), whose paths the proxy cannot see. Hermes' telemetry hosts at every level: never switchable
    (hermes.telemetry_rules; hermes.check_telemetry refuses a start that would open or hide them)."""
    if not proxy_enabled(a) or a.offline:
        return None
    # The switchable ones for the agents the run starts; those never switchable (below) for every start of their
    # image, --binary too: a program started there (a shell, the agent from it) is that agent all the same.
    rules = [{"id": f"telemetry-{agent}-{number}", "host": host, "path": path, "decision": "deny",
              "reason": f"--telemetry {telemetry_level(a)}: {agent}'s telemetry"}
             for agent in team(a) for number, (host, path) in enumerate(TELEMETRY_PATHS.get(agent, []), 1)
             if not any(host_matches_any(host, value) for value in a.no_inspect)] \
        if runs_agent(a) else []
    # Every agent of the sandbox whose telemetry is never switchable: the start's and those --with as their own starts
    # have them; the others of its team image (they can be typed in its shell) their telemetry's and what is never
    # opened - not what a feature of theirs would open (Pi's gists, Goose's model catalog), which the run's agents
    # may need.
    for agent in image_agents(a):
        if agent in UNSWITCHABLE_TELEMETRY:
            found = unswitchable_telemetry_rules(agent_view(a, agent) if agent in team(a)
                                                 else companion_view(a, agent, strict=False))
            rules += found if agent in team(a) else [rule for rule in found if rule["id"].startswith(
                f"telemetry-{agent}-") or rule["id"].startswith(f"{agent}-never-")]
    return {"version": 1, "description": "the agents' telemetry (--telemetry)", "http": rules} if rules else None


def codex_telemetry_keys(level):
    return CODEX_TELEMETRY["off"] + (CODEX_TELEMETRY["strict"] if level == "strict" else [])


def codex_telemetry_args(level):
    """Codex's -c overrides of its telemetry keys: they win over every config.toml (the lead's command line)."""
    return [value for table, key, _, literal in codex_telemetry_keys(level)
            for value in ("-c", f"{'.'.join(filter(None, (table, key)))}={literal}")]


CODEX_TELEMETRY_TABLES = ("analytics", "feedback", "otel")


def check_codex_telemetry_args(a):
    """Codex's own -c/--config arguments may not set its telemetry keys (they would win over the launcher's):
    ValueError naming the argument."""
    if a.agent != "codex":
        return
    args = list(a.agent_args)
    options = args[:args.index("--")] if "--" in args else args
    names = {".".join(filter(None, (table, key))) for table, key, _, _ in codex_telemetry_keys(telemetry_level(a))}
    for index, value in enumerate(options):
        if value in ("-c", "--config"):
            override = options[index + 1] if index + 1 < len(options) else ""
        elif value.startswith("--config="):
            override = value[len("--config="):]
        elif value.startswith("-c") and len(value) > 2:
            override = value[2:].lstrip("=")
        else:
            continue
        key = re.sub(r"\s+", "", override.partition("=")[0]).replace('"', "")
        if key in names or key.split(".", 1)[0] in CODEX_TELEMETRY_TABLES:
            raise ValueError(f"Codex's telemetry is always off: drop {printable(value)} "
                             f"{printable(override) if value in ('-c', '--config') else ''}".rstrip()
                             + " from its arguments (sandbox-ai sets its analytics, feedback, otel and update keys)")


TOML_TABLE_HEADER = r"^[ \t]*\[[ \t]*{name}[ \t]*\][ \t]*(?:#[^\n]*)?$"


def codex_config_with(text, keys, toml):
    """TEXT (~/.codex/config.toml) with each (table, key, value, TOML) of KEYS, and [(name, value)] of those the file
    set otherwise (replaced: telemetry is always off). A key the file sets on a line of its own (key = ..., or
    table.key = ... at the top) is rewritten there; top-level keys it lacks go first, a key of a table the file has
    goes below its [table] header, a table it lacks at the end. The result must parse to the same document with
    exactly these keys set, else ValueError: nothing is written that Codex could read otherwise."""
    original, document = text, toml.loads(text)
    top, tables, below, replaced = [], {}, {}, []
    for table, key, value, literal in keys:
        holder = document if table is None else document.get(table)
        if holder is not None and not isinstance(holder, dict):
            raise ValueError(f"{table} is not a table")
        if holder is not None and key in holder:
            if holder[key] != value:
                replaced.append((".".join(filter(None, (table, key))), holder[key]))
                text = codex_config_replaced(text, table, key, literal)
            continue
        line = f"{key} = {literal}"
        if table is None:
            top.append(line)
        elif holder is None:
            tables.setdefault(table, []).append(line)
        else:
            below.setdefault(table, []).append(line)
    for table, lines in below.items():
        headers = list(re.finditer(TOML_TABLE_HEADER.format(name=re.escape(table)), text, re.M))
        if len(headers) != 1:
            raise ValueError(f"[{table}] is not written as one plain [{table}] header")
        end = headers[0].end()
        text = text[:end] + "\n" + "\n".join(lines) + text[end:]
    if top:
        # A key before every table is a top-level key, whatever the file holds below it.
        text = "\n".join(top) + "\n" + text
    for table, lines in tables.items():
        text = text + ("" if not text or text.endswith("\n") else "\n") + f"\n[{table}]\n" + "\n".join(lines) + "\n"
    expected = toml.loads(original)  # a copy of DOCUMENT, dates and all
    for table, key, value, _ in keys:
        holder = expected if table is None else expected.setdefault(table, {})
        holder[key] = value
    if toml.loads(text) != expected:
        raise ValueError("the changed file would read differently")
    return text, replaced


def codex_config_replaced(text, table, key, literal):
    """TEXT with the line that sets KEY (of TABLE; None: top level) set to LITERAL: key = ... in the table's part
    (the top's for None), or table.key = ... at the top. ValueError where no such line is found (an inline table,
    a value over several lines): codex_config_with's check would refuse what is left anyway."""
    headers = [match.start() for match in re.finditer(r"^[ \t]*\[", text, re.M)]
    regions = []
    if table is None:
        regions.append((0, headers[0] if headers else len(text), re.escape(key)))
    else:
        for match in re.finditer(TOML_TABLE_HEADER.format(name=re.escape(table)), text, re.M):
            end = next((start for start in headers if start > match.start()), len(text))
            regions.append((match.end(), end, re.escape(key)))
        regions.append((0, headers[0] if headers else len(text), rf"{re.escape(table)}[ \t]*\.[ \t]*{re.escape(key)}"))
    for start, end, name in regions:
        line = re.compile(rf'^([ \t]*)(?:{name}|"{name}")[ \t]*=[^\n]*$', re.M).search(text, start, end)
        if line:
            written = ".".join(filter(None, (table, key))) if "." in line.group(0).split("=", 1)[0] else key
            return text[:line.start()] + f"{line.group(1)}{written} = {literal}" + text[line.end():]
    raise ValueError(f"{'.'.join(filter(None, (table, key)))} is not set on a line of its own")


def codex_telemetry_defaults(home, level):
    """Codex's telemetry, always off: ~/.codex/config.toml in the sandbox HOME turns off its analytics, feedback
    uploads, metrics (statsig), OpenTelemetry exports and update check (strict: the plugin sync too). A value of a
    --state-dir's or session's own file that would turn one on is replaced, and said (the lead gets the same as -c
    overrides: codex_telemetry_args)."""
    path = check_home_path(home, home / ".codex/config.toml")
    toml = load_privacy_module()._toml_module()
    keys = codex_telemetry_keys(level)
    try:
        text = read_config_bytes(path).decode() if path.exists() else ""
        if toml is None:
            if text.strip():
                raise ValueError("no TOML parser to read it with")
            content, kept = "\n".join(f"[{t}]\n{k} = {v}" if t else f"{k} = {v}" for t, k, _, v in keys) + "\n", []
        else:
            content, kept = codex_config_with(text, keys, toml)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"agent-sandbox: Codex's ~/.codex/config.toml is left as it is ({printable(str(exc))}); its telemetry "
              "is still off for the agent sandbox-ai starts (-c overrides), not for a Codex started from its shell: "
              "set analytics.enabled = false, feedback.enabled = false, otel.metrics_exporter = \"none\" and "
              "check_for_update_on_startup = false there", file=sys.stderr)
        return
    for name, value in kept:
        print(f"agent-sandbox: Codex: {name} = {printable(json.dumps(value))} in ~/.codex/config.toml is replaced "
              "(telemetry is always off)", file=sys.stderr)
    if content != text:
        replace_private(content.encode(), home, ".codex/config.toml")


def codex_own_instructions(home):
    """developer_instructions of the sandbox HOME's ~/.codex/config.toml (yours: a kit's, a session's), or None:
    the start's note is given as -c developer_instructions, which replaces the file's for the run - so the note is
    joined after yours (agent_command). A file that cannot be read or parsed: None (the note alone, as before)."""
    toml = load_privacy_module()._toml_module()
    if toml is None:
        return None
    try:
        path = check_home_path(home, home / ".codex/config.toml")
        value = toml.loads(read_config_bytes(path).decode()).get("developer_instructions") if path.exists() else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, str) and value.strip() and len(value) <= 65536 else None


def codex_companion_defaults(home):
    """Codex --with is started from the shell, without the launcher's --sandbox: its own sandbox cannot set up
    inside sandbox-ai's, so ~/.codex/config.toml says sandbox_mode = "danger-full-access" unless it says
    otherwise (sandbox-ai's sandbox is the boundary, as for the lead)."""
    path = check_home_path(home, home / ".codex/config.toml")
    try:
        text = read_config_bytes(path).decode() if path.exists() else ""
    except (OSError, ValueError) as exc:
        print(f"agent-sandbox: left sandbox ~/.codex/config.toml unchanged ({exc}); run codex with "
              "--sandbox danger-full-access", file=sys.stderr)
        return
    toml = load_privacy_module()._toml_module()
    if toml is None:
        if text.strip():  # without a parser only a file of its own is written
            print("agent-sandbox: left sandbox ~/.codex/config.toml unchanged (no TOML parser); run codex with "
                  "--sandbox danger-full-access", file=sys.stderr)
            return
    else:
        try:
            if "sandbox_mode" in toml.loads(text):
                return  # the choice in the file stays
        except ValueError:
            print("agent-sandbox: left sandbox ~/.codex/config.toml unchanged (it is not valid TOML); run codex "
                  "with --sandbox danger-full-access", file=sys.stderr)
            return
    # A key before every table is a top-level key, whatever the file holds below it.
    content = 'sandbox_mode = "danger-full-access"\n' + text
    if toml is not None:
        toml.loads(content)  # never write a file Codex could not read
    replace_private(content.encode(), home, ".codex/config.toml")


def auth_sources(agent):
    """[(source, sandbox target)]: the login store's files if it holds a login for AGENT, else the host's."""
    if store_login(agent):
        return store_sources(agent)
    home = Path.home()
    if agent == "codex":
        return [(Path(os.environ.get("CODEX_HOME") or (home / ".codex")) / "auth.json", AUTH_TARGETS[agent])]
    if agent == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
        # A custom config directory can contain .claude.json; the default
        # HOME-based installation stores it next to ~/.claude instead.
        metadata = config / ".claude.json"
        if not metadata.is_file():
            metadata = home / ".claude.json"
        return [(config / ".credentials.json", AUTH_TARGETS[agent]),
                (metadata, ".claude.json")]
    if agent == "hermes":
        return [(Path(os.environ.get("HERMES_HOME") or (home / ".hermes")) / "auth.json", AUTH_TARGETS[agent])]
    if agent == "dsh":
        return [(Path(os.environ.get("DSH_HOME") or (home / ".dsh")) / ".credentials.yaml", AUTH_TARGETS[agent])]
    if agent == "qwen":
        return [(Path(os.environ.get("QWEN_HOME") or (home / ".qwen")) / "oauth_creds.json", AUTH_TARGETS[agent])]
    if agent == "goose":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        return [(config / "goose" / "secrets.yaml", AUTH_TARGETS[agent])]
    if agent == "openhands":
        return [(home / ".openhands" / "settings.json", AUTH_TARGETS[agent])]
    if agent == "nanobot":
        return [(home / ".nanobot" / "config.json", AUTH_TARGETS[agent])]
    if agent == "letta":
        root = Path(os.environ.get("LETTA_CONFIG_DIR") or (home / ".letta"))
        return [(root / "lc-local-backend" / "providers" / "auth.json", AUTH_TARGETS[agent])]
    if agent == "pi":
        return [(Path(os.environ.get("PI_CODING_AGENT_DIR") or (home / ".pi/agent")) / "auth.json", AUTH_TARGETS[agent])]
    data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share"))
    return [(data / "opencode/auth.json", AUTH_TARGETS[agent])]


def opencode_catalog():
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "opencode/models.json"


def auth_mode(a):
    if a.no_auth:
        return "none"
    if a.copy_auth:
        return "copy"
    if a.auth_file:
        return "file"
    if a.import_codex_auth:
        return "codex_import"
    if getattr(a, "auth_from_session", None):
        return "session"
    targets = {AUTH_TARGETS[a.agent]}
    if a.agent == "claude":
        targets.add(".claude.json")
    if any(":" in value and str(PurePosixPath(value.rsplit(":", 1)[1])) in targets for value in a.copy):
        return "manual"
    # --llm: the endpoint is the provider; the cloud login is not needed, so it is not handed over.
    return "none" if getattr(a, "llm", None) else "auto"


def auth_plan(a):
    """Planned auth copies as [(host source, sandbox target)]; paths only, never contents.

    Automatic mode is resolved at start, so a dry run never discovers credentials."""
    mode = auth_mode(a)
    if mode == "auto":
        return "automatic at start: existing default credential files of this agent"
    if mode == "copy":
        sources = auth_sources(a.agent)
        plan = [sources[0]] + [(src, dest) for src, dest in sources[1:] if src.is_file()]
    elif mode == "file":
        plan = [(a.auth_file.expanduser(), AUTH_TARGETS[a.agent])]
    elif mode == "codex_import":
        plan = [(a.import_codex_auth.expanduser(), AUTH_TARGETS["opencode"])]
    elif mode == "session":  # (read at the start: a dry run reads no session's HOME)
        plan = [(f"session {a.auth_from_session}", AUTH_TARGETS[a.agent])] + (
            [(f"session {a.auth_from_session}", ".claude.json")] if a.agent == "claude" else [])
    elif mode == "manual":
        targets = {AUTH_TARGETS[a.agent], ".claude.json"}
        plan = [(Path(value.rsplit(":", 1)[0]).expanduser(), value.rsplit(":", 1)[1]) for value in a.copy
                if str(PurePosixPath(value.rsplit(":", 1)[1])) in targets]
    else:
        plan = []
    return [{"source": str(src), "target": "/root/" + str(PurePosixPath(dest))} for src, dest in plan]


def prepare_home(a, home):
    """Populate sandbox HOME; returns the auth copies made as [(source, relative target)]."""
    copies, copied = [], []
    if a.copy_auth and a.agent in NO_LOGIN_AGENTS:  # (validate says so first)
        raise ValueError(f"--copy-auth: {a.agent} has no login of its own to copy")
    if a.copy_auth:
        sources = auth_sources(a.agent)
        if not sources[0][0].is_file():
            raise ValueError(f"Auth JSON missing: {sources[0][0]}; log in with `sandbox-ai login {a.agent}` "
                             "or on the host, or use --auth-file")
        # A login already in a reused HOME is kept (Claude's is refreshed by the newest-wins sync below).
        copies.extend((src, dest, True) for src, dest in sources if src.is_file() and not (home / dest).exists())
    if a.auth_file:
        copies.append((a.auth_file, AUTH_TARGETS[a.agent], False))
    if getattr(a, "auth_from_session", None):
        # Another session's login (session_login.copy_into: read as untrusted, checked; a login already in a reused
        # HOME is kept). What write_back compares at the end goes into a.session_login.
        copied += load_session_login_module().copy_into(a, home)
    for value in a.copy:
        if ":" not in value:
            raise ValueError("Copy syntax: SRC:HOME_RELATIVE_DEST")
        src, dest = value.rsplit(":", 1)
        copies.append((Path(src), dest, False))
    for source, dest, discovered in copies:
        (copy_auth_file if discovered else copy_private)(source, home, dest)
        copied.append((source, dest))
    if a.local_config:
        replace_private(read_config_bytes(Path(a.local_config).expanduser().resolve()), home,
                        ".config/opencode/opencode.jsonc")
    if a.import_codex_auth:
        imported = opencode_auth_from_codex(a.import_codex_auth)
        with tempfile.TemporaryDirectory(prefix="agent-auth-import-") as directory:
            source = Path(directory) / "auth.json"
            source.write_text(json.dumps(imported))
            source.chmod(0o600)
            copy_private(source, home, AUTH_TARGETS["opencode"])
        copied.append((a.import_codex_auth, AUTH_TARGETS["opencode"]))
    if auth_mode(a) == "auto" and a.agent not in NO_LOGIN_AGENTS:
        for source, dest in auth_sources(a.agent):
            target = home / dest
            if target.is_symlink():
                raise ValueError(f"Auth destination is a symlink: {dest}")
            if source.is_file() and not target.exists():
                copy_auth_file(source, home, dest)
                copied.append((source, dest))
    pair = claude_credential_pair(a, home)
    if pair:
        check_home_path(home, pair[1])
    if pair and pair[1].exists():
        with claude_login_lock():  # never between another run's compare and its swap
            try:
                if claude_recover_writeback(home, pair):
                    print("agent-sandbox: the Claude login refreshed in an earlier run of this HOME was never "
                          "written back; wrote it back now", file=sys.stderr)
            except (OSError, ValueError) as exc:
                print(f"agent-sandbox: could not write back the Claude login of an earlier run: {exc}",
                      file=sys.stderr)
            # The source is the authority: its login replaces whatever a previous run left.
            sync_claude_credentials(*pair, sandbox="target", newer_only=False)
    if a.state_dir:
        # Recorded only while this HOME shares the source's login; any other run (--no-auth, another
        # account) forgets it, so a later start never takes that login for a missed write-back.
        remember_sync("claude", home, oauth_token_digest(claude_oauth(pair[0])) if pair else None)
        for agent in team(a):  # the agents --with too: their logins may come from the store
            sync_store_login(a, home, agent)
    copied += companion_logins(a, home)
    if "claude" in team(a):
        claude_default_settings(home, skip_danger_prompt=a.dangerously_skip_permissions)
        claude_default_state(home)
        claude_trusted_workspaces(a, home)
        if a.state_dir and not a.mcp_socket_dir:
            # A kept HOME: servers an attach --live of a run killed on the way left in ~/.claude.json.
            with contextlib.suppress(Exception):
                for name in load_mcp_module().forget_live_entries(home):
                    print(f"agent-sandbox: took the MCP server {printable(name)} an earlier run attached while it "
                          "ran out of ~/.claude.json (this start names its servers itself)", file=sys.stderr)
    if "codex" in team(a) and not getattr(a, "helper_of", None):
        codex_trusted_workspaces(a, home)  # a helper's /work is the lead's too: never trusted for its settings
    if "codex" in companions(a):
        codex_companion_defaults(home)
    if "codex" in team(a) and runs_agent(a):
        codex_telemetry_defaults(home, telemetry_level(a))
    if a.agent == "codex" and runs_agent(a):
        # Your own developer_instructions stay: the start's note (-c developer_instructions) is joined after them.
        a.codex_own_instructions = codex_own_instructions(home)
    if "opencode2" in team(a) and runs_agent(a):
        # Its shared background service off: every command runs its own server and ends with it (opencode2.py).
        relative = load_opencode2_module().SERVICE_FILE
        try:
            target = check_home_path(home, home / relative)
            try:
                current = read_config_bytes(target).decode("utf-8", "replace")
            except (FileNotFoundError, ValueError):
                current = None  # none, or not a regular file of a sane size: replaced
            settings = load_opencode2_module().service_settings(current)
            if settings is not None:
                replace_private(settings.encode(), home, relative)
        except (OSError, ValueError) as exc:
            print(f"agent-sandbox: left sandbox ~/{relative} unchanged ({exc}); OpenCode 2 may start its shared "
                  "background service", file=sys.stderr)
    if "opencode" in team(a):
        catalog = opencode_catalog()
        target = home / ".cache/opencode/models.json"
        if catalog.is_file() and not target.exists():
            copy_private(catalog, home, ".cache/opencode/models.json")
    if a.llm:
        load_llm_module().prepare_home(a, home, replace_private, update_json_defaults, CLAUDE_STATE_LIMIT)
    if a.agent == "opencode" and thinking_shown(a) is not None and runs_agent(a):
        opencode_thinking(home, thinking_shown(a))
    if a.agent in HOME_THINKING and thinking_shown(a) is not None and runs_agent(a):
        home_thinking(home, a.agent, thinking_shown(a))
    for name in [".codex", ".claude", ".config", ".local/share", ".cache"]:
        # A symlink planted in a persistent HOME must not make us create directories elsewhere.
        check_home_path(home, home / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    return copied


def replace_private(content, home, relative):
    """Atomically replace a sandbox-HOME file with CONTENT unless it already matches; never follows symlinks."""
    target = check_home_path(home, home / relative)
    try:
        if read_config_bytes(target) == content:
            return False
    except FileNotFoundError:
        pass
    except ValueError:
        if target.is_symlink():
            raise
        # Not a regular file (FIFO, device, oversized): replaced below without reading it.
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".replace-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


# Only these claudeAiOauth fields cross the sandbox boundary, with these types.
# Rotating token fields always come from the newer side; descriptive fields
# are taken from it only when the receiving side has none.
OAUTH_TOKEN_FIELDS = {"accessToken": str, "refreshToken": str, "expiresAt": (int, float),
                      "refreshTokenExpiresAt": (int, float)}
OAUTH_INFO_FIELDS = {"scopes": list, "subscriptionType": str, "subscriptionTypeSource": str,
                     "rateLimitTier": str}


def oauth_fields(oauth, fields):
    return {key: value for key, value in oauth.items() if key in fields and isinstance(value, fields[key])
            and not isinstance(value, bool) and (not isinstance(value, list) or all(isinstance(v, str) for v in value))}


def check_home_path(home, path):
    """Refuse a sandbox-HOME path whose file or parent directories are symlinks the agent planted."""
    for part in [path, *path.parents]:
        if part == home:
            return path
        if part.is_symlink():
            raise ValueError(f"Sandbox path is a symlink, refusing to use it: {part}")
    raise ValueError(f"Path outside the sandbox HOME: {path}")


STATE_LOCK = ".sandbox-ai.lock"


@contextlib.contextmanager
def state_dir_lock(home, option="--state-dir"):
    """Exclusive lock on a --state-dir for the whole run: two runs on one HOME would overwrite each
    other's logins and settings. The agent can write the HOME, so the file is never followed as a
    symlink, never waited on as a FIFO, and the PID goes only into a private file of ours. The same for a kept
    profile of the sandbox's browser (OPTION --browser-profile), which its browser can write."""
    try:
        fd = os.open(home / STATE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600)
    except OSError as exc:
        raise ValueError(f"{option} {home}: cannot open its lock file {STATE_LOCK}: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        # A hard link would make us overwrite another file with the PID.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError(f"{option} {home}: {STATE_LOCK} is not a private regular file; remove it")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 16, 0).decode(errors="replace").strip()
            holder = f" (PID {holder})" if holder.isdigit() else ""
            raise ValueError(f"{option} {home} is in use by another run{holder}; wait for it to end or "
                             "choose another directory") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield fd
    finally:
        os.close(fd)


def update_json_defaults(home, relative, apply, limit=CONFIG_LIMIT, keep_mode=False):
    """Let apply() add missing keys to a sandbox-HOME JSON object; existing values always win.

    apply(document) returns whether it changed anything; an unchanged file is
    never rewritten, so a Claude already running on a shared HOME keeps its
    writes. KEEP_MODE: a file replaced keeps its permission bits (else 0600). Returns None when done, else why the
    file was left untouched."""
    path = check_home_path(home, home / relative)
    exists = path.exists()
    mode = 0o600
    if keep_mode and exists:
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(path.stat().st_mode) & 0o666 or 0o600
    try:
        document = json.loads(read_config_bytes(path, limit)) if exists else {}
    except (OSError, ValueError, RecursionError) as exc:
        return str(exc) or type(exc).__name__  # Never rewrite a file we cannot parse.
    if not isinstance(document, dict):
        return f"{path} does not hold a JSON object"
    if not apply(document) and exists:
        return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".defaults-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(document, stream, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return None


def claude_default_settings(home, skip_danger_prompt=False):
    """Default Claude model and effort; existing settings always win. SKIP_DANGER_PROMPT
    (--dangerously-skip-permissions): Claude's "Bypass Permissions mode" confirmation is answered already;
    the choice was made on the launcher's command line, and the sandbox stays in force."""
    def apply(document):
        changed = "model" not in document or "modelSettings" not in document
        if skip_danger_prompt and document.get("skipDangerousModePermissionPrompt") is not True:
            document["skipDangerousModePermissionPrompt"], changed = True, True
        document.setdefault("model", CLAUDE_DEFAULT_MODEL)
        models = document.setdefault("modelSettings", {})
        if isinstance(models, dict):
            changed |= CLAUDE_DEFAULT_MODEL not in models
            current = models.setdefault(CLAUDE_DEFAULT_MODEL, {})
            if isinstance(current, dict):
                changed |= "effortLevel" not in current
                current.setdefault("effortLevel", CLAUDE_DEFAULT_EFFORT)
        return changed
    update_json_defaults(home, ".claude/settings.json", apply)


def claude_default_state(home):
    """Mark first-run onboarding done when the sandbox holds a usable login.

    Without hasCompletedOnboarding (and a theme) in ~/.claude.json, Claude Code
    runs its setup screens, which include the login step, even though
    ~/.claude/.credentials.json is valid. That happens whenever the host has no
    ~/.claude.json to copy or a persistent sandbox HOME kept an incomplete one."""
    credentials = check_home_path(home, home / AUTH_TARGETS["claude"])
    oauth = claude_oauth(credentials, follow=False)
    if oauth is None:
        return  # No login to keep: let Claude's onboarding offer one.
    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if (isinstance(refresh_expiry, (int, float)) and not isinstance(refresh_expiry, bool)
            and refresh_expiry / 1000 <= time.time()):
        return  # The login cannot be refreshed any more; onboarding offers a new one.
    def apply(document):
        changed = False
        # Claude never writes false here; anything but true would re-run setup.
        if document.get("hasCompletedOnboarding") is not True:
            document["hasCompletedOnboarding"] = changed = True
        if not isinstance(document.get("theme"), str) or not document["theme"]:
            document["theme"], changed = "dark", True
        return changed
    problem = update_json_defaults(home, ".claude.json", apply, CLAUDE_STATE_LIMIT)
    if problem:
        print(f"agent-sandbox: left sandbox ~/.claude.json unchanged ({problem}); "
              "Claude may ask to log in again", file=sys.stderr)


def claude_trusted_workspaces(a, home):
    """Mark the sandbox paths of the workspaces as trusted, so Claude does not ask on every start:
    you chose these directories yourself. Other projects in ~/.claude.json stay as they are."""
    def apply(document):
        projects = document.setdefault("projects", {})
        if not isinstance(projects, dict):
            return False
        changed = False
        for _, dest in workspace_targets(a):
            project = projects.setdefault(dest, {})
            if isinstance(project, dict) and project.get("hasTrustDialogAccepted") is not True:
                project["hasTrustDialogAccepted"] = changed = True
        return changed
    problem = update_json_defaults(home, ".claude.json", apply, CLAUDE_STATE_LIMIT)
    if problem:
        print(f"agent-sandbox: left sandbox ~/.claude.json unchanged ({problem}); "
              "Claude may ask whether you trust the workspace", file=sys.stderr)


def codex_trusted_workspaces(a, home):
    """The same for Codex: [projects."PATH"] trust_level = "trusted" in ~/.codex/config.toml for every
    workspace without an entry; an entry already there (also "untrusted") is kept."""
    path = check_home_path(home, home / ".codex/config.toml")
    try:
        text = read_config_bytes(path).decode() if path.exists() else ""
    except (OSError, ValueError) as exc:
        print(f"agent-sandbox: left sandbox ~/.codex/config.toml unchanged ({exc}); "
              "Codex may ask whether you trust the workspace", file=sys.stderr)
        return
    toml = load_privacy_module()._toml_module()
    dests = [dest for _, dest in workspace_targets(a) if not any(c in dest for c in '"\\\n\r')]
    if toml is not None:
        try:
            projects = toml.loads(text).get("projects", {})
        except ValueError:
            print("agent-sandbox: left sandbox ~/.codex/config.toml unchanged (it is not valid TOML); "
                  "Codex may ask whether you trust the workspace", file=sys.stderr)
            return
        missing = [dest for dest in dests if not (isinstance(projects, dict) and dest in projects)]
    else:
        # Without a TOML parser (Python before 3.11, no tomli): only a file that never mentions projects.
        missing = [] if "projects" in text else dests
    if not missing:
        return
    addition = "".join(f'\n[projects."{dest}"]\ntrust_level = "trusted"\n' for dest in missing)
    new = (text.rstrip("\n") + "\n" + addition).lstrip("\n")
    if toml is not None:
        try:
            toml.loads(new)
        except ValueError:  # e.g. projects written as an inline table: a table cannot be added to it
            return
    replace_private(new.encode(), home, ".codex/config.toml")


def claude_oauth(path, follow=True):
    """Validated claudeAiOauth block, or None.

    follow=False for sandbox-side files: no symlinks, regular files of bounded
    size only, so a planted FIFO, device or huge file cannot hang or exhaust us."""
    try:
        data = Path(path).read_bytes() if follow else read_config_bytes(path)
    except (OSError, ValueError):
        return None
    return claude_oauth_from(data)


def claude_oauth_from(data):
    """The validated claudeAiOauth block of a credentials file's contents (bytes), or None."""
    try:
        oauth = json.loads(data)["claudeAiOauth"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    if not (isinstance(oauth, dict) and timestamp_ms(oauth.get("expiresAt"))
            and all(isinstance(oauth.get(key), str) and oauth[key] for key in ("accessToken", "refreshToken"))):
        return None
    if "refreshTokenExpiresAt" in oauth and not timestamp_ms(oauth["refreshTokenExpiresAt"]):
        return None
    return oauth


def timestamp_ms(value):
    """A plausible millisecond timestamp: a finite number in 0 <= value < 2**53, not a bool."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and value == value
            and 0 <= value < 2 ** 53)


# Access tokens live hours; anything claiming far longer is not a real refresh.
OAUTH_MAX_LIFETIME = 31 * 24 * 3600


def sync_claude_credentials(source, target, sandbox=None, newer_only=True, accounts=None):
    """Anthropic rotates refresh tokens: whichever side refreshed last holds the
    only valid pair. Copy it over the older side; never downgrade.

    sandbox: "source" or "target", the side the agent could have tampered with.
    newer_only=False lets SOURCE win whenever its tokens differ: the host at
    start, because the sandbox's expiresAt is the agent's to choose.
    A sandbox's login (sandbox="source") replaces only a login of the same account, checked with Anthropic
    (claude_same_account; ACCOUNTS: its cache, a state dict), never another account's or none."""
    new = claude_oauth(source, follow=sandbox != "source")
    if new is None or Path(target).exists() and Path(source).resolve() == Path(target).resolve():
        return False
    if sandbox == "source" and not 0 < new["expiresAt"] / 1000 <= time.time() + OAUTH_MAX_LIFETIME:
        return False
    old = claude_oauth(target, follow=sandbox != "target")
    if old is not None and (old["expiresAt"] >= new["expiresAt"] if newer_only else
                            all(old.get(key) == new.get(key) for key in OAUTH_TOKEN_FIELDS)):
        return False
    if sandbox == "source" and (old is None or claude_same_account(
            {} if accounts is None else accounts, target, old, new, final=True) != "same"):
        return False
    if not Path(target).parent.is_dir():
        return False
    if Path(target).is_symlink():
        raise ValueError(f"Credential target is a symlink: {target}")
    try:
        # The sandbox side is read without following links and bounded in size.
        data = read_config_bytes(target) if sandbox == "target" else Path(target).read_bytes()
    except (OSError, ValueError):
        data = None
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=Path(target).parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(merge_claude_login(data, new))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


def merge_claude_login(data, new):
    """The credentials file DATA (bytes, or None) with the login NEW in its claudeAiOauth block, as bytes: NEW's
    token fields, descriptive fields from DATA where it has them (else NEW's), everything else in DATA kept (the
    OAuth tokens of MCP servers). Contents DATA cannot be read as a JSON object count as empty."""
    try:
        document = json.loads(data) if data is not None else {}
    except (ValueError, RecursionError):
        document = {}
    document = document if isinstance(document, dict) else {}
    current = document.get("claudeAiOauth")
    current = current if isinstance(current, dict) else {}
    document["claudeAiOauth"] = {**oauth_fields(new, OAUTH_INFO_FIELDS), **oauth_fields(current, OAUTH_INFO_FIELDS),
                                 **oauth_fields(new, OAUTH_TOKEN_FIELDS)}
    return json.dumps(document).encode()


def claude_token_status(path):
    """One line about the copied Claude login; never token contents."""
    oauth = claude_oauth(path, follow=False)
    if oauth is None:
        return "no usable Claude login in the sandbox; run `claude auth login` on the host"
    hours = (oauth["expiresAt"] / 1000 - time.time()) / 3600
    if hours <= 0:
        return "Claude access token expired; Claude refreshes it on first use (needs network)"
    return f"Claude access token valid for {hours:.1f} h"


def credential_digest(path):
    """SHA-256 of a host credential file, or None if it does not exist or cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def oauth_token_digest(oauth):
    """SHA-256 of a Claude login's tokens, or None; stands for them without revealing them."""
    if oauth is None:
        return None
    return hashlib.sha256(json.dumps(oauth_fields(oauth, OAUTH_TOKEN_FIELDS), sort_keys=True).encode()).hexdigest()


# Which Anthropic account a Claude OAuth access token belongs to, asked the way Claude Code asks it itself (taken
# from the bundle of Claude Code 2.1.319: GET `${BASE_API_URL}/api/oauth/profile` with BASE_API_URL
# https://api.anthropic.com and the headers Authorization: Bearer <access token>, Content-Type: application/json;
# the answer {"account": {"uuid": ...}, "organization": {"uuid": ...}} is what Claude keeps in ~/.claude.json as
# oauthAccount.accountUuid and .organizationUuid). Should Anthropic move it, every lookup fails and no sandbox login
# is written back any more (fail closed): then this constant is the one place to change.
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_PROFILE_TIMEOUT = 5
CLAUDE_PROFILE_LIMIT = 1 << 20
# Seconds after a failed lookup (network, rate limit, server error) before Anthropic is asked again during a run.
CLAUDE_ACCOUNT_RETRY = 60.0


def claude_profile_account(access_token, timeout=CLAUDE_PROFILE_TIMEOUT):
    """(account uuid, organization uuid) Anthropic names for the Claude OAuth ACCESS_TOKEN, or None when it refuses
    the token (401, 403) or names no account. OSError when it could not be asked (network, timeout, rate limit,
    server error, a redirect): then nothing is known.

    Called by the launcher on the host, never inside a sandbox. TLS is verified; a redirect is not followed (it
    would carry the token along); the token appears in no exception and no message."""
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None  # urllib then raises the 3xx as an HTTPError

    request = urllib.request.Request(CLAUDE_PROFILE_URL, headers={
        "Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Cache-Control": "no-cache",
        "User-Agent": f"sandbox-ai/{__version__}"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(CLAUDE_PROFILE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            return None
        raise OSError(f"Anthropic answered the account lookup with HTTP {exc.code}") from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise OSError(f"the account lookup at Anthropic failed ({type(exc).__name__})") from None
    try:
        document = json.loads(body) if len(body) <= CLAUDE_PROFILE_LIMIT else None
    except (ValueError, RecursionError):
        raise OSError("Anthropic's answer to the account lookup is no JSON") from None
    if not isinstance(document, dict):
        return None
    ids = tuple(document[key].get("uuid") if isinstance(document.get(key), dict) else None
                for key in ("account", "organization"))
    return ids if all(isinstance(value, str) and value for value in ids) else None


def claude_account_file(source):
    """The Claude account state (.claude.json) that belongs to the credentials file SOURCE, as auth_sources picks
    it: the login store's own, else the one in the config directory if it has one, else ~/.claude.json."""
    store = auth_store("claude")
    if Path(source) == store / AUTH_TARGETS["claude"]:
        return store / ".claude.json"
    beside = Path(source).parent / ".claude.json"
    return beside if beside.is_file() else Path.home() / ".claude.json"


def claude_file_account(path):
    """(account uuid, organization uuid) of oauthAccount in the Claude account state PATH, or None. Only for the
    host's or the login store's own file: what a sandbox's file says is the agent's to write."""
    try:
        document = json.loads(read_config_bytes(Path(path).resolve(), CLAUDE_STATE_LIMIT))
    except (OSError, ValueError, RecursionError):
        return None
    account = document.get("oauthAccount") if isinstance(document, dict) else None
    if not isinstance(account, dict):
        return None
    ids = account.get("accountUuid"), account.get("organizationUuid")
    return ids if all(isinstance(value, str) and value for value in ids) else None


def claude_token_account(state, oauth, final=False):
    """The account of the Claude login OAUTH as Anthropic names it for its access token (claude_profile_account):
    the (account, organization) pair, False when Anthropic refuses the token or names none, "failed" when it could
    not be asked now, "later" while a failed lookup is less than CLAUDE_ACCOUNT_RETRY seconds ago (FINAL, the end of
    a run, asks anyway). Each token is asked about once: answers are kept in STATE["accounts"] under the token's
    digest, the time of the next try in STATE["account_retry"]."""
    cache = state.setdefault("accounts", {})
    key = hashlib.sha256(oauth["accessToken"].encode()).hexdigest()
    if key in cache:
        return cache[key]
    retry = state.get("account_retry")
    if retry is not None and time.monotonic() < retry and not final:
        return "later"
    try:
        answer = claude_profile_account(oauth["accessToken"])
    except OSError:
        state["account_retry"] = time.monotonic() + CLAUDE_ACCOUNT_RETRY
        return "failed"
    state["account_retry"] = None
    if len(cache) >= 64:
        cache.clear()
    cache[key] = tuple(answer) if answer else False
    return cache[key]


def claude_same_account(state, host, source, box, final=False):
    """Whether the sandbox's Claude login BOX belongs to the same account (and organization) as SOURCE, the login of
    the credentials file HOST (the host's or the login store's): "same"; "other" (another account); "unknown" (one
    of the two cannot be checked); "failed" or "later" (Anthropic could not be asked now; claude_token_account).
    Only "same" lets a sandbox's login replace the source's.

    The source's account is the oauthAccount of its own .claude.json (claude_account_file), else Anthropic's answer
    for the source's access token while that has not expired. The sandbox's is always Anthropic's answer for the
    sandbox's access token: the sandbox's files are the agent's to write. STATE caches the answers (a run's state,
    or {} for one check)."""
    mine = claude_file_account(claude_account_file(host))
    if mine is None:
        known = hashlib.sha256(source["accessToken"].encode()).hexdigest() in state.get("accounts", {})
        if not known and source["expiresAt"] <= time.time() * 1000:
            return "unknown"  # the source's access token has run out: Anthropic cannot be asked with it
        mine = claude_token_account(state, source, final)  # known: e.g. the sandbox's pair written back before
    if mine is False:
        return "unknown"
    if isinstance(mine, str):
        return mine
    theirs = claude_token_account(state, box, final)
    if theirs is False:
        return "unknown"
    if isinstance(theirs, str):
        return theirs
    return "same" if tuple(theirs) == tuple(mine) else "other"


def claude_refusal_note(verdict, where, retry=True):
    """The status line for a sandbox login not written back to WHERE ("the host", "sandbox-ai's login store") after
    the account check VERDICT (claude_same_account); RETRY: during a run, where it is asked again. Never a token, an
    account id or an address."""
    if verdict in ("failed", "later"):
        return (f"could not ask Anthropic which account the sandbox's Claude login belongs to: not written back to "
                f"{where}" + ("; asked again at the next change, a minute later at the earliest" if retry else ""))
    return (f"the sandbox holds a Claude login of another account (or one that could not be checked): not written "
            f"back to {where}; {where} keeps its own login")


def claude_writeback_state(pair):
    """What write-back compares against later: the source's login and the tokens copied in.

    Only the login counts, not the whole file: Claude on the host rewrites the file for other
    reasons too (MCP OAuth tokens), and refusing the write-back then would leave the source
    with a refresh token the sandbox has already used up."""
    host, sandbox = pair
    oauth, host_oauth = claude_oauth(sandbox, follow=False), claude_oauth(host)
    return {"host": oauth_token_digest(host_oauth), "host_login": host_oauth is not None,
            "tokens": None if oauth is None else oauth_fields(oauth, OAUTH_TOKEN_FIELDS)}


def claude_writeback(home, pair, before):
    """At the end of a run: the last claude_login_sync, after the agent has stopped; a status line or None.

    Only a refresh of the login the source handed in counts: the source must have had credentials, must
    still hold what it last shared with the sandbox, the sandbox tokens must differ from that, and Anthropic must
    name the same account for them (asked even if a lookup failed less than a minute ago: the last chance).
    A login the source got meanwhile (the host's own, another sandbox's refresh) always wins."""
    check_home_path(home, pair[1])
    return claude_login_sync(home, pair, before, final=True)


# Seconds between two looks at a running sandbox's Claude login and its source (lstat only, unless one changed).
CLAUDE_SYNC_INTERVAL = 2.0
CLAUDE_LOCK_TIMEOUT = 30


@contextlib.contextmanager
def claude_login_lock(timeout=CLAUDE_LOCK_TIMEOUT):
    """Exclusive lock of every sandbox-ai process on this host around comparing and replacing a Claude login
    (the source's and a sandbox's copy), so that one's compare-and-swap never interleaves with another's.
    The lock file is launcher-private, next to the login-sync record, outside every sandbox. TimeoutError
    after TIMEOUT seconds."""
    path = login_sync_store().with_name("claude-login.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another sandbox-ai process holds {path}") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def open_home_dir(home, relative):
    """A descriptor of the directory that holds RELATIVE in the sandbox HOME, reached without following a link:
    the agent may swap a directory for a link while it runs. ValueError for a link or a non-directory on the
    way, FileNotFoundError for a missing one."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("..", "/") for part in parts):
        raise ValueError(f"Path outside the sandbox HOME: {relative}")
    fd = os.open(home, flags)
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"Sandbox path is a symlink or no directory, refusing to use it: "
                                     f"{Path(home) / relative}") from None
                raise
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_home_file(home, relative, limit=CONFIG_LIMIT):
    """Contents of the regular file RELATIVE in the sandbox HOME, read once while the agent may be writing the
    HOME: no link followed on the way or at the end, no FIFO or device, at most LIMIT bytes. FileNotFoundError
    if it is not there, ValueError for anything else than a regular file."""
    parent = open_home_dir(home, relative)
    try:
        fd = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{Path(home) / relative} is a symlink; refusing to use it") from None
        raise
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{Path(home) / relative} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"{Path(home) / relative} is larger than {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def replace_home_file(home, relative, content):
    """Atomically replace the file RELATIVE in the sandbox HOME with CONTENT (mode 0600) while the agent may be
    writing the HOME: written next to it and renamed over it through a directory descriptor, never through a
    link (a link in its place is replaced, not followed)."""
    import uuid
    parent = open_home_dir(home, relative)
    name = PurePosixPath(relative).name
    temporary = f".{name}.sandbox-ai-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            raise
    finally:
        os.close(parent)


def claude_login_sync(home, pair, state, final=False):
    """Compare a sandbox's Claude login with its source (host or login store) once, under claude_login_lock;
    a status line or None. STATE (claude_writeback_state) holds what the two last shared and follows each sync.

    Refresh tokens rotate, so whichever side refreshed last holds the only valid pair, and it must reach the
    other side before that one refreshes with the used-up token:
    - the sandbox refreshed and the source still holds what they shared: the new tokens go to the source now
      (only a plausible pair newer than the source's, only of the source's account as Anthropic names it
      (claude_same_account); never a login into a source that had none);
    - the source holds another login than the sandbox (another sandbox's refresh, the host's own Claude, a new
      `sandbox-ai login`) and the sandbox did not refresh since: that login goes into the sandbox, where Claude
      reads its file again before refreshing and takes the newer pair instead of sending the used one;
    - both changed: the source wins.
    A sandbox login of another account (a /login in the sandbox, tokens the agent planted), or one whose account
    cannot be checked, stays in the sandbox and the source keeps its own. Anthropic is asked outside the lock, then
    both sides are compared once more under it; when it cannot be asked, nothing is written back and it is asked
    again a minute later at the earliest (at once when FINAL, the end of a run).
    The sandbox side is read and replaced without following links; tokens never appear in a status line."""
    host, sandbox = pair
    if state.get("host") is None or not state.get("host_login"):
        return None  # Never create a login in the source, not even in an existing file.
    relative = str(Path(sandbox).relative_to(home))
    store = auth_store() in Path(host).parents
    where = "sandbox-ai's login store" if store else "the host"
    checked = None  # ((source digest, sandbox digest), verdict) of the account check made outside the lock
    for _ in range(2):
        with claude_login_lock():
            try:
                data = read_home_file(home, relative)
            except (FileNotFoundError, ValueError):
                data = None  # none, or nothing a login can be read from without following a link: none to sync
            box, source = (claude_oauth_from(data) if data is not None else None), claude_oauth(host)
            if source is None:
                return None  # logged out, or being rewritten: nothing to give or take now
            tokens = oauth_fields(box, OAUTH_TOKEN_FIELDS) if box is not None else None
            changed = box is not None and tokens != state["tokens"]
            if not (changed and oauth_token_digest(source) == state["host"]):
                if box is None or oauth_token_digest(box) in (oauth_token_digest(source), state.get("refused")):
                    return None
                replace_home_file(home, relative, merge_claude_login(data, source))
                state["host"], state["tokens"] = oauth_token_digest(source), oauth_fields(source, OAUTH_TOKEN_FIELDS)
                remember_sync("claude", home, state["host"])
                if changed:
                    return f"the Claude login {'in' if store else 'on'} {where} changed during the run; kept that one"
                return f"took over the newer Claude login from {where} (refreshed elsewhere)"
            # The agent chooses the sandbox's expiresAt: only a plausible pair newer than the source's goes back.
            if not source["expiresAt"] < box["expiresAt"] <= (time.time() + OAUTH_MAX_LIFETIME) * 1000:
                state["tokens"], state["refused"] = tokens, oauth_token_digest(box)
                return None
            key = oauth_token_digest(source), oauth_token_digest(box)
            if checked is not None:
                if checked[0] != key:
                    return None  # changed again while Anthropic was asked: checked at the next look
                if checked[1] in ("failed", "later"):
                    return claude_refusal_note(checked[1], where) if checked[1] == "failed" else None
                if checked[1] != "same":
                    state["tokens"], state["refused"] = tokens, oauth_token_digest(box)
                    return claude_refusal_note(checked[1], where)
                if Path(host).is_symlink():
                    raise ValueError(f"Credential target is a symlink: {host}")
                write_private_file(host, merge_claude_login(Path(host).read_bytes(), box))
                state["host"], state["tokens"] = oauth_token_digest(claude_oauth(host)), tokens
                remember_sync("claude", home, state["host"])
                return f"refreshed Claude credentials written back to {where}"
        # A write-back to check: whose login the sandbox holds is asked outside the lock (up to two requests of
        # at most CLAUDE_PROFILE_TIMEOUT seconds each), so that no other run waits for Anthropic.
        checked = key, claude_same_account(state, host, source, box, final)
    return None


def watch_claude_login(home, pair, state, stop, interval=None):
    """For the whole run, until STOP is set: whenever the sandbox's Claude login or its source changed (an
    lstat every INTERVAL seconds, by default CLAUDE_SYNC_INTERVAL), sync them (claude_login_sync), so that a
    refresh on either side reaches the other at once and not only when the run ends. Status lines are collected
    in STATE["notes"] and printed after the run, since the agent's terminal is busy meanwhile. A write-back whose
    account check could not ask Anthropic (claude_login_sync) is tried again once its retry is due, also when
    nothing changed meanwhile. Never raises."""
    interval = CLAUDE_SYNC_INTERVAL if interval is None else interval
    def signature():
        def one(path, follow):
            try:
                info = os.stat(path, follow_symlinks=follow)
            except OSError:
                return None
            return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        return one(pair[0], True), one(pair[1], False), one(Path(pair[1]).parent, False)

    seen = None
    notes = state.setdefault("notes", [])
    while not stop.wait(interval):
        current = signature()
        retry = state.get("account_retry")
        if current == seen and (retry is None or time.monotonic() < retry):
            continue
        if current == seen:
            state["account_retry"] = None  # a lookup that failed a minute ago is due again, once
        # What was seen before the sync: a change that lands while it runs (the agent writing a refreshed token
        # just then) is seen at the next look, not hidden in a signature taken afterwards. The sync's own writes
        # cost one more look, which finds nothing to do.
        seen = current
        try:
            message = claude_login_sync(home, pair, state)
        except Exception as exc:  # noqa: BLE001 - the run goes on; said after it
            message = f"could not sync the Claude login during the run: {exc}"
        if message:
            notes.append(message)


def claude_sync_notes(state):
    """The status lines watch_claude_login collected during a run, each once, with how often it happened."""
    notes = (state or {}).get("notes") or []
    return [note + (f" ({notes.count(note)} times during the run)" if notes.count(note) > 1 else " (during the run)")
            for note in dict.fromkeys(notes)]


def login_sync_store():
    """Launcher-private record per persistent HOME and agent: a digest of the login it last shared
    with its source (host or login store). Digests only, never tokens."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "login-sync.json"


def load_login_sync():
    try:
        record = json.loads(login_sync_store().read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def sync_key(agent, home):
    return f"{agent}:{Path(home).resolve()}"


def remember_sync(agent, home, digest):
    """Record that HOME and its source share the login with DIGEST (None forgets it); entries of
    removed HOMEs are dropped. Under a lock of its own (two runs starting or ending at once must not drop each
    other's entry; not claude_login_lock, which some callers hold already)."""
    store = login_sync_store()
    try:
        store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(store.with_name("login-sync.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return  # only a recovery aid
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remember_sync_locked(agent, home, digest, store)
    finally:
        os.close(lock)


def remember_sync_locked(agent, home, digest, store):
    known = load_login_sync()
    record = {key: value for key, value in known.items() if Path(key.partition(":")[2]).is_dir()}
    if digest is None:
        if sync_key(agent, home) not in record and record == known:
            return  # nothing to forget: do not create or rewrite the file
        record.pop(sync_key(agent, home), None)
    else:
        record[sync_key(agent, home)] = digest
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".login-sync-", dir=store.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, store)
    except OSError:
        if temporary is not None:  # only a recovery aid, but no leftovers
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def claude_recover_writeback(home, pair):
    """A persistent HOME whose previous run refreshed the login but never wrote it back (the launcher
    was killed): the source still holds exactly the tokens it last shared, so the HOME's newer pair
    is the only valid one. Write it back before the source would overwrite it."""
    source, sandbox = pair
    shared = load_login_sync().get(sync_key("claude", home))
    current, copied = claude_oauth(source), claude_oauth(sandbox, follow=False)
    if not shared or current is None or copied is None or oauth_token_digest(current) != shared:
        return False
    if oauth_token_digest(copied) == shared:
        return False
    if not 0 < copied["expiresAt"] / 1000 <= time.time() + OAUTH_MAX_LIFETIME or current["expiresAt"] >= copied["expiresAt"]:
        return False  # what sync_claude_credentials refuses anyway: not worth asking Anthropic about
    accounts = {}
    verdict = claude_same_account(accounts, source, current, copied, final=True)
    if verdict != "same":
        where = "sandbox-ai's login store" if auth_store() in Path(source).parents else "the host"
        print(f"agent-sandbox: {claude_refusal_note(verdict, where, retry=False)} (the login an earlier run of this "
              "HOME left)",
              file=sys.stderr)
        return False
    return sync_claude_credentials(sandbox, source, sandbox="source", accounts=accounts)


def store_writeback_state(a, home, agent=None):
    """Codex and OpenCode logins from the login store: what to compare at the end, to keep a refresh. AGENT: an
    agent --with (its login is copied the automatic way), by default the lead."""
    agent = agent or a.agent
    automatic = auth_mode(a) in ("auto", "copy") if agent == a.agent else companion_auth(a, agent) == "auto"
    if agent == "claude" or a.probe or not automatic or not store_login(agent):
        return None
    source = auth_store(agent) / AUTH_TARGETS[agent]
    before = {"source": source, "store": credential_digest(source), "sandbox": sandbox_digest(home, AUTH_TARGETS[agent]),
              "agent": agent}
    # Only a login that came from the store goes back to it, never one a persistent HOME had of its own.
    return before if before["store"] is not None and before["store"] == before["sandbox"] else None


def sandbox_digest(home, relative):
    try:
        return hashlib.sha256(read_config_bytes(check_home_path(home, home / relative))).hexdigest()
    except (OSError, ValueError):
        return None


def store_writeback(home, a, before):
    """Write tokens the agent refreshed in the sandbox back to the login store; a status line or None.
    Only the refreshed tokens of the stored account are taken over (merge_refreshed). The store feeds
    only sandboxes, never the host's own login."""
    agent = before.get("agent") or a.agent
    relative, source = AUTH_TARGETS[agent], before["source"]
    if credential_digest(source) != before["store"]:
        return "the stored login changed during the run; kept the stored one"
    path = check_home_path(home, home / relative)
    stored = stored_document(agent, source)
    try:
        data = read_config_bytes(path)
        sandbox = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return None
    if stored is None or hashlib.sha256(data).hexdigest() == before["sandbox"]:
        return None
    merged = merge_refreshed(agent, stored, sandbox)
    if merged is None:
        return None
    content = json.dumps(merged, indent=2).encode()
    write_private_file(source, content)
    if a.state_dir:
        # A persistent HOME keeps exactly what the store holds, so it can follow later refreshes.
        replace_private(content, home, relative)
        remember_sync(agent, home, hashlib.sha256(content).hexdigest())
    return f"refreshed {agent} login written back to sandbox-ai's login store"


def sync_store_login(a, home, agent=None):
    """A persistent HOME with a Codex or OpenCode login copied from the store, unchanged since: take over
    the store's newer tokens (another sandbox refreshed them, which used up the HOME's). AGENT: an agent --with,
    by default the lead."""
    agent = agent or a.agent
    automatic = auth_mode(a) in ("auto", "copy") if agent == a.agent else companion_auth(a, agent) == "auto"
    if agent == "claude" or a.probe or not automatic or not store_login(agent):
        if agent != "claude":
            remember_sync(agent, home, None)
        return
    relative = AUTH_TARGETS[agent]
    store = auth_store(agent) / relative
    last, current, stored = (load_login_sync().get(sync_key(agent, home)), sandbox_digest(home, relative),
                             credential_digest(store))
    if last and current == last and stored != last:
        replace_private(read_config_bytes(store), home, relative)
        print("agent-sandbox: took over the newer stored login (refreshed by another sandbox)", file=sys.stderr)
        current = sandbox_digest(home, relative)
    remember_sync(agent, home, stored if current == stored else None)


def write_private_file(path, content):
    """Atomically replace PATH (a launcher-owned file) with CONTENT, mode 0600."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to write through it")
    fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def login_state(agent):
    """(where, problem) of the login sandboxes of AGENT get: where is "store" or "host",
    problem None, "missing" or "expired" (Claude's refresh token has run out)."""
    where = "store" if store_login(agent) else "host"
    source = auth_sources(agent)[0][0]
    if agent != "claude":
        return where, None if source.is_file() else "missing"
    oauth = claude_oauth(source, follow=where == "host")
    if oauth is None:
        return where, "missing"
    refresh = oauth.get("refreshTokenExpiresAt")
    if refresh is not None and refresh / 1000 <= time.time():
        return where, "expired"
    return where, None


RUNTIME_KEY_VARIABLE = re.compile(r"API_KEY|AUTH_TOKEN|OAUTH_TOKEN|ACCESS_TOKEN")  # a variable that holds a login


def login_problem(a):
    """(where, problem) of the login the start A would get: problem "missing" or "expired", None when it has one or
    needs none here (another way to sign in was chosen, a probe, a HOME that keeps its own, an agent without a login
    command); where: "store" or "host"."""
    if (auth_mode(a) not in ("auto", "copy") or a.probe or a.local_config or a.tmux_socket_dir
            or a.mcp_socket_dir or a.agent in LOGIN_COMMANDLESS):
        return None, None
    home = (Path(a.state_dir).expanduser() if a.state_dir else
            load_sessions_module().session_dir(a.session) / "home" if a.session is not None else None)
    if home and (home / AUTH_TARGETS[a.agent]).exists():
        return None, None  # a persistent HOME keeps its own login
    if a.agent == "pi" and (getattr(a, "llm", None) or getattr(a, "api_key", None)):
        return None, None  # Pi's model comes with --llm or --api-key: its login (/login's providers) is not needed
    return login_state(a.agent)


def login_fix(a):
    """The fix (fixes.py) for the login the start A would get and the host does not have (or that expired): logged
    in now inside a sandbox, in this terminal, kept for every later sandbox of that agent. Not needed - the start
    goes on without it and the agent asks for it itself - so it is offered with what else the start lacks, never a
    reason to stop. None when there is a login, or the agent runs without a terminal of its own."""
    if getattr(a, "llm", None) or getattr(a, "api_key", None) or getattr(a, "offline", False) or \
            any(RUNTIME_KEY_VARIABLE.search(str(name).partition("=")[0]) for name in [*a.env, *a.secret_env]):
        return None  # (its model comes another way - a server of yours, a key: no login is offered)
    where, problem = login_problem(a)
    if problem is None or not interactive_args(a.agent, a.agent_args):
        return None
    place = "sandbox-ai's login store" if where == "store" else "the host"
    return FIXES.fix("login", a.agent, f"{place} has {'no' if problem == 'missing' else 'an expired'} {a.agent} login",
                     "log in now inside a sandbox, in this terminal (kept for all its sandboxes)",
                     [["sandbox-ai", "login", a.agent, "--backend", a.backend]], terminal=True, needed=False)


def check_login(a):
    """Before the start: say when the login sandboxes would get is missing or expired, and in a terminal
    offer to log in now inside a sandbox (sandbox-ai login), stored for all later sandboxes."""
    if a.dry_run:
        return
    where, problem = login_problem(a)
    if problem is None:
        return
    place = "sandbox-ai's login store" if where == "store" else "the host"
    message = f"{place} has {'no' if problem == 'missing' else 'an expired'} {a.agent} login"
    if (problem == "expired" and sys.stdin.isatty() and sys.stdout.isatty()
            and interactive_args(a.agent, a.agent_args)):
        try:
            answer = input(f"agent-sandbox: {message}. Log in now inside a sandbox and keep that login for all "
                           f"{a.agent} sandboxes? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
            print(file=sys.stderr)
        except KeyboardInterrupt:
            print(file=sys.stderr)
            raise SystemExit(130) from None
        if answer in ("", "y", "yes", "j", "ja"):
            login_options = ["--backend", a.backend, *(["--podman-url", a.podman_url] if a.podman_url else [])]
            if load_login_module().login(a.agent, login_options) != 0:
                raise ValueError(f"Login failed; try again with: sandbox-ai login {a.agent}")
            return
    if a.agent == "claude" and where == "host" and problem == "missing" and claude_keychain_login():
        # Claude Code on a Mac keeps its login in the Keychain, not in ~/.claude/.credentials.json: nothing a
        # sandbox could be given (and reading it would ask the human for the Keychain's password every start).
        print("agent-sandbox: Claude Code keeps this Mac's login in the macOS Keychain, which sandboxes cannot use; "
              "log in once with `sandbox-ai login claude` (kept for all sandboxes)", file=sys.stderr)
        return
    print(f"agent-sandbox: {message}; log in with `sandbox-ai login {a.agent}` (kept for all sandboxes) "
          "or on the host", file=sys.stderr)


# The Keychain item in which Claude Code keeps its login on macOS.
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def claude_keychain_login(runner=None):
    """macOS: the Keychain holds a Claude Code login (asked for its attributes only, never its value: that would
    show the Keychain's question to the human)."""
    if sys.platform != "darwin":
        return False
    try:
        return (runner or subprocess.run)(["/usr/bin/security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE],
                                          stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                          check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def load_login_module():
    spec = importlib.util.spec_from_file_location("sandbox_login", HERE / "login.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_kits_module():
    spec = importlib.util.spec_from_file_location("sandbox_kits", HERE / "kits.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRIVACY_DIR = "/sandbox/session/privacy"
PRIVACY_HOOKS = f"{PRIVACY_DIR}/hooks"


_privacy_module = None


def load_privacy_module():
    """privacy.py, loaded once (it keeps no state of its own): a start asked for it ten times - its rules' folder, its
    context, the proxy's push check, the hooks - each a load of 3 ms."""
    global _privacy_module
    if _privacy_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_privacy", HERE / "privacy.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _privacy_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _privacy_module = loaded
    return _privacy_module


def privacy_hooks_apply(a):
    """The agent's pre-commit and pre-push hooks run the privacy check (not in probes or MCP servers)."""
    return a.privacy_check != "off" and kits_apply(a)


def privacy_rules_dir(a):
    """--privacy-rules, else the default directory if it exists, else None."""
    if a.privacy_rules:
        return Path(os.path.normpath(Path(a.privacy_rules).expanduser().absolute()))
    default = load_privacy_module().default_rules_dir()
    return default if default.is_dir() else None


def privacy_context(a):
    """What the privacy check derives rules from: the host's values plus the private addresses, Git servers,
    account names and tokens this run is given."""
    context = load_privacy_module().host_context()
    context["addresses"] = [address for address, _ in tcp_targets(a)]
    context["servers"], context["tokens"] = [], []
    for credential, token in git_tokens(a) + registry_git_tokens(a)[0]:
        context["servers"].append({key: credential.get(key) for key in ("host", "port", "user")})
        context["tokens"].append(token)
    return context


def workspace_mode(a, index):
    """ro or rw for the workspace at INDEX of workspace_targets(a): --workspace-ro makes every one read-only, the
    security mode high (modes.py) all but the first."""
    return "ro" if a.workspace_ro or (index and getattr(a, "mode_other_workspaces_ro", False)) else "rw"


def privacy_workspaces(a):
    """[(host path, project name)] of the workspaces the privacy check looks at: the writable ones."""
    paths = [Path(src).expanduser().resolve() for index, (src, _) in enumerate(workspace_targets(a))
             if workspace_mode(a, index) == "rw"]
    return [(path, path.name) for path in paths]


def prepare_privacy(a, session):
    """Rules of the privacy check, read before the run so that a broken rule file stops the start, and the
    agent's hooks in the session directory. Returns the function the check after the run needs, or None."""
    if a.privacy_check == "off" or not kits_apply(a):
        return None
    privacy = load_privacy_module()
    context = privacy_context(a)
    derived = privacy.derived_rules(context)
    rules_dir = privacy_rules_dir(a)
    rules = {}
    try:
        for _, project in privacy_workspaces(a):
            rules[project] = privacy.builtin_rules().add(derived).add(privacy.user_rules(rules_dir, project))
    except privacy.RulesUnavailable as exc:
        raise ValueError(f"--privacy-rules: {exc}") from None
    own_hosts = privacy.private_hosts(context)
    # The agent's hooks get the built-in and derived rules without the tokens (it has those anyway, but a
    # file of them is one more copy); your own rule files never enter the sandbox.
    hook_rules = privacy.derived_rules(dict(context, tokens=[]))
    privacy.write_hooks(session / "privacy", f"{PRIVACY_DIR}/privacy.py", hook_rules, own_hosts)
    def rules_for(project):
        if project not in rules:
            rules[project] = privacy.builtin_rules().add(derived).add(privacy.user_rules(rules_dir, project))
        return rules[project]

    def check():
        workspaces = privacy_workspaces(a)
        for path in added_workspaces_of_run(a):
            if path not in [p for p, _ in workspaces]:
                workspaces.append((path, path.name))
        try:
            privacy.check_after_run(workspaces, rules_for, own_hosts, a.privacy_check,
                                    lambda line: print(f"agent-sandbox: privacy: {printable(line)}", file=sys.stderr))
        except privacy.RulesUnavailable as exc:
            print(f"agent-sandbox: privacy: not checked: {printable(exc)}", file=sys.stderr)
    return check


def added_workspaces_of_run(a):
    """The writable host directories `sandbox-ai workspace add` gave this run (also those removed again: what was
    committed there during the run is checked after it)."""
    try:
        data = json.loads((runs_dir() / a.run_id / "workspaces.json").read_text())
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return []
    entries = data.get("workspaces") if isinstance(data, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("mode") == "rw" and isinstance(entry.get("host"), str) \
                and entry["host"].startswith("/") and entry.get("kind", "dir") == "dir":  # a file: no project
            path = Path(entry["host"])
            if path not in found:
                found.append(path)
    return found


CGROUP_CONTROLLERS = {"memory": "memory", "cpus": "cpu", "pids": "pids"}
# Telemetry and error reporting the agents try on their own: looked up often, never worth a question.
ASK_QUIET = ("statsig.com", "statsig.anthropic.com", "featuregates.org", "sentry.io", "datadoghq.com", "segment.io",
             "segment.com", "mixpanel.com", "amplitude.com", "posthog.com", "featureassets.org",
             "prodregistryv2.org", "honeycomb.io")


def resource_limits(a):
    """{memory, cpus, pids} to enforce: the defaults (8 GiB, half the CPUs, 4096 processes) unless an option
    changes or switches them off (none)."""
    limits = {}
    memory = str(a.memory).strip().lower()
    if memory != "none":
        if not re.fullmatch(r"[1-9][0-9]{0,6}[kmg]?", memory):
            raise ValueError(f"--memory expects a size such as 4g or 512m, or none: {a.memory!r}")
        limits["memory"] = memory
    cpus = str(a.cpus).strip().lower() if a.cpus is not None else str(max(1, engine_cpus(a) // 2))
    if cpus != "none":
        try:
            value = float(cpus)
        except ValueError:
            value = 0
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", cpus) or not 0 < value <= engine_cpus(a):
            raise ValueError(f"--cpus expects a number from 0.01 to {engine_cpus(a)} (the engine's CPUs), or none")
        limits["cpus"] = cpus
    pids = str(a.pids).strip().lower()
    if pids != "none":
        if not re.fullmatch(r"[0-9]{2,7}", pids) or int(pids) < 16:
            raise ValueError(f"--pids expects a number of at least 16, or none: {a.pids!r}")
        limits["pids"] = int(pids)
    return limits


def delegated_controllers(path=None):
    """The cgroup controllers systemd delegates to this user (rootless limits need them), or None if unknown."""
    path = Path(path or f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                       "cgroup.controllers")
    try:
        return set(path.read_text().split())
    except OSError:
        return None


def local_engine(a):
    """True where the container engine runs on this host's own kernel: Linux without --podman-url. Its cgroups,
    routes, /dev and systemd are this host's. On macOS Podman runs in a Linux VM (podman machine): as remote as
    --podman-url, so the same checks are left to the VM (TODO.md: macOS support)."""
    return not a.podman_url and sys.platform == "linux"


# podman machine's address of the Mac (gvproxy's host address, which it forwards to the Mac's loopback; what
# host.containers.internal names in the VM). SANDBOX_AI_MACHINE_HOST overrides it for a machine set up otherwise.
MACHINE_HOST = "192.168.127.254"


def on_machine(a):
    """This run's Podman container runs in podman machine's VM on this Mac: Unix sockets do not cross its shared
    folders, so the run's sockets stay in the VM and the host reaches them through `podman exec` (TODO.md: macOS
    support, stage 2)."""
    return sys.platform == "darwin" and a.backend == "podman" and not a.podman_url


def machine_host():
    """The Mac's address as the sandbox's supervisor reaches it in podman machine (MACHINE_HOST)."""
    value = os.environ.get("SANDBOX_AI_MACHINE_HOST") or MACHINE_HOST
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ValueError(f"SANDBOX_AI_MACHINE_HOST must be an IPv4 address: {value!r}") from None


def machine_record(record):
    """A run (its run.json RECORD) whose sockets are in podman machine's VM: started on this Mac with Podman."""
    return sys.platform == "darwin" and isinstance(record, dict) and record.get("backend") == "podman" \
        and not record.get("podman_url")


def run_container(record):
    """The Podman container of the agent of the run RECORD (run.json or session.json): "container", else POD-agent."""
    container = record.get("container") if isinstance(record, dict) else None
    if isinstance(container, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", container):
        return container
    pod = record.get("pod") if isinstance(record, dict) else None
    if isinstance(pod, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,190}", pod):
        return f"{pod}-agent"
    raise ValueError("the run's record names no Podman container to reach it through")


def machine_exec(record, args, interactive=True, tty=False):
    """`podman exec` of runtime.py ARGS in the container of the run RECORD (macOS): how the host reaches the
    supervisor's control socket and the terminal relay in podman machine's VM. Only runtime.py's clients run there,
    never a command the sandbox chose. Your choice of what the sandbox's output may do on your terminal
    (SANDBOX_AI_TERMINAL_FILTER, runtime.TerminalFilter) goes with it: the attach and shell clients filter there."""
    choice = (os.environ.get(RUNTIME.TERMINAL_FILTER_ENV) or "").strip().lower()
    return ["podman", *(["--remote", "--url", record["podman_url"]] if record.get("podman_url") else []), "exec",
            *(["--interactive"] if interactive else []), *(["--tty"] if tty else []), "--user=0:0",
            *(["--env", f"{RUNTIME.TERMINAL_FILTER_ENV}={choice}"] if choice in RUNTIME.TERMINAL_FILTERS else []),
            run_container(record), *RUNTIME.SUPERVISOR_PYTHON, "/sandbox/runtime.py", *args]


_machine = None


def podman_machine(runner=None):
    """`podman machine inspect` of the default machine (macOS), read once: {} when there is none."""
    global _machine
    if _machine is None or runner is not None:
        try:
            out = (runner or subprocess.run)(["podman", "machine", "inspect"], capture_output=True, text=True,
                                              timeout=30, check=False).stdout
            data = json.loads(out)
            found = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        except (OSError, ValueError, subprocess.TimeoutExpired, RecursionError):
            found = {}
        if runner is not None:
            return found
        _machine = found
    return _machine


def machine_paths(a):
    """[(what, host path)] a start mounts from this host into podman machine's VM."""
    paths = [("the workspace", Path(w).expanduser()) for w in a.workspaces if w]
    paths += [("--mount", Path(src)) for src, _, _ in map(parse_mount, a.mount)]
    paths += [("--state-dir", a.state_dir.expanduser())] if a.state_dir else []
    # The folders mounted for the logs and the display's VNC socket (both bind mounts into the VM's container too).
    paths += [(option, Path(value).expanduser()) for option, value in (("--log-dir", a.log_dir),
                                                                       ("--gui-vnc", a.gui_vnc)) if value]
    paths += [("sandbox-ai's files", HERE), ("$TMPDIR (the run's session)", Path(tempfile.gettempdir()))]
    return paths


def check_machine_shares(a, machine=None):
    """macOS: every host path a start mounts must lie in a share of podman machine (by default /Users, /private and
    /var/folders), or the VM mounts an empty directory in its place. Refused with the share to add."""
    if sys.platform != "darwin" or a.backend != "podman" or a.podman_url:
        return
    machine = podman_machine() if machine is None else machine
    shares = [str(m.get("Source", "")).rstrip("/") for m in machine.get("Mounts") or [] if isinstance(m, dict)]
    shares = [s for s in shares if s]
    if not shares:
        return  # no machine to ask (doctor says so); podman itself refuses then
    for what, path in machine_paths(a):
        real = os.path.realpath(path)
        if not any(real == share or real.startswith(share + "/") for share in shares):
            raise ValueError(f"{what} {real} is not shared with podman machine (shares: {', '.join(shares)}); "
                             f"keep it under one of them, or add one: podman machine stop; podman machine set -v "
                             f"{real}:{real}")


def engine_cpus(a):
    """The CPUs the engine's machine has: this host's on Linux, podman machine's VM on macOS."""
    # getattr: `sandbox-ai limits` validates its values with a namespace of the three limits alone.
    if sys.platform == "darwin" and not getattr(a, "podman_url", None):
        cpus = (podman_machine().get("Resources") or {}).get("CPUs")
        if isinstance(cpus, int) and cpus > 0:
            return cpus
    return os.cpu_count() or 1


def check_platform(a):
    """Refuse what needs Linux on this host when it is macOS: Bubblewrap, this host's devices and Wayland."""
    if sys.platform != "darwin":
        return
    if a.backend == "bwrap":
        raise ValueError("--backend bwrap needs Linux (Bubblewrap); on macOS sandbox-ai runs with Podman "
                         "(podman machine)")
    if a.device or a.gpu in ("dri", "rocm"):
        raise ValueError("--device and --gpu dri|rocm hand devices of a Linux host to the sandbox; on macOS the "
                         "sandbox runs in Podman's Linux VM, which has none of the Mac's")
    if getattr(a, "gui_wayland", False):
        raise ValueError("--gui-wayland needs a Wayland session (Linux); use --gui or --gui-vnc DIR")


def enforced_limits(a):
    """(limits that can be enforced here, notes about those that cannot)."""
    limits, notes = resource_limits(a), []
    if limits and local_engine(a) and not user_manager_running():
        # Without a systemd user session rootless Podman falls back to cgroupfs, where it cannot set limits,
        # and Bubblewrap has no scope to run in.
        return {}, ["no systemd user session (podman: cgroupfs), so --memory, --cpus and --pids are not enforced; "
                    "`loginctl enable-linger` or a desktop login provides one"]
    controllers = delegated_controllers() if local_engine(a) else None
    if controllers is not None:
        for key, controller in CGROUP_CONTROLLERS.items():
            if key in limits and controller not in controllers:
                del limits[key]
                notes.append(f"the {controller} controller is not delegated to your user, so --{key} is not "
                             "enforced (systemd: Delegate=yes in user@.service; see sandbox-ai doctor)")
    return limits, notes


def user_manager_running():
    """True if a systemd user manager runs for this user and its bus is there (systemd-run --user, and
    rootless Podman's systemd cgroup manager, need it)."""
    return Path(f"/run/user/{os.getuid()}/bus").exists()


def user_bus_environment():
    """The environment systemd-run --user needs, independent of the caller's XDG_RUNTIME_DIR."""
    runtime = f"/run/user/{os.getuid()}"
    return {**os.environ, "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def limits_prefix(a):
    """Command prefix that runs Bubblewrap in a systemd scope with the limits, or [] if there is none."""
    limits = enforced_limits(a)[0]
    if not limits or not shutil.which("systemd-run") or not user_manager_running():
        return []
    properties = []
    if "memory" in limits:
        # Swap as much again, as Podman's --memory does (its default --memory-swap is twice --memory).
        properties += ["-p", f"MemoryMax={limits['memory'].upper()}", "-p", f"MemorySwapMax={limits['memory'].upper()}"]
    if "cpus" in limits:
        properties += ["-p", f"CPUQuota={int(round(float(limits['cpus']) * 100))}%"]
    if "pids" in limits:
        properties += ["-p", f"TasksMax={limits['pids']}"]
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *properties, "--"]


def load_llm_module():
    spec = importlib.util.spec_from_file_location("sandbox_llm", HERE / "llm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_secret_store_module():
    spec = importlib.util.spec_from_file_location("sandbox_secret_store", HERE / "secret_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def secret_environment(a):
    """{NAME: value} of --secret-env NAME[=SECRET], read from sandbox-ai secrets; values never printed. Read once per
    start (the secret guard registers the same values with the proxy: secret_guard_values)."""
    cached = getattr(a, "_secret_environment", None)
    if cached is not None and cached[0] == tuple(a.secret_env):
        return dict(cached[1])
    result = {}
    for value in a.secret_env:
        name, _, secret = value.partition("=")
        secret = secret or name
        for part in (name, secret):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", part):
                raise ValueError(f"--secret-env expects NAME or NAME=SECRET (letters, digits, _): {value!r}")
        if name in ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
        token = load_secret_store_module().get(secret)
        if token is None:
            raise ValueError(f"--secret-env {value}: no secret {secret}; keep one with: sandbox-ai secrets set {secret}")
        LOG.secret(token)  # never in launcher.log, whatever line it reaches
        result[name] = token
    a._secret_environment = (tuple(a.secret_env), dict(result))
    return result


API_KEY_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
API_KEY_PLACEHOLDER = "sandbox-ai-api-key-placeholder-{name}"
SANDBOX_OWNED = ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")


def api_key_values(a, check=True):
    """[(host, NAME, SECRET)] of --api-key HOST=NAME[=SECRET]. CHECK: ValueError for a wrong one; otherwise wrong
    ones are left out (validate() reports them)."""
    found = []
    for value in getattr(a, "api_key", []) or []:
        host, _, rest = value.partition("=")
        name, _, secret = rest.partition("=")
        host, secret = host.lower().rstrip("."), secret or name
        ok = bool(API_KEY_HOST.fullmatch(host)) and bool(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:=[A-Za-z_][A-Za-z0-9_]{0,63})?", rest))
        if not ok:
            if check:
                raise ValueError(f"--api-key expects HOST=NAME or HOST=NAME=SECRET (a host name, not an address; "
                                 f"NAME and SECRET letters, digits, _): {value!r}")
            continue
        found.append((host, name, secret))
    return found


def check_api_keys(a, get=None):
    """--api-key: well-formed, each variable once, not one of the sandbox's own, only where the proxy opens the
    request (with the proxy, the host inspected), and each secret kept (GET reads the store; False skips that)."""
    keys = api_key_values(a)
    names = [name for _, name, _ in keys]
    secret_names = {value.partition("=")[0] for value in a.secret_env} | set(a.env)
    for host, name, _ in keys:
        if names.count(name) > 1 or name in secret_names:
            raise ValueError(f"--api-key: the variable {name} is given twice")
        if name in SANDBOX_OWNED:
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
        if any(host_matches_any(host, value) for value in a.no_inspect):  # *.DOMAIN too
            raise ValueError(f"--api-key {host}: the proxy adds the key only to requests it opens; drop --no-inspect {host}")
    if keys and a.no_proxy:
        raise ValueError("--api-key needs the connection proxy (it adds the key on the wire); drop --no-proxy")
    if keys and get is not False:
        api_key_entries(a, get)  # a secret that is not kept stops a dry run too, before anything runs


def api_key_environment(a):
    """{NAME: placeholder} for --api-key: what the agent sees instead of the key."""
    return {name: API_KEY_PLACEHOLDER.format(name=name) for _, name, _ in api_key_values(a)}


def api_key_entries(a, get=None):
    """proxy.json's "api_keys": host, placeholder and key, in the proxy's directory the agent cannot read; ValueError
    when a secret is not kept."""
    get = get or load_secret_store_module().get
    entries = []
    for host, name, secret in api_key_values(a):
        key = get(secret)
        if key is None:
            raise ValueError(f"--api-key {host}={name}: no secret {secret}; keep one with: sandbox-ai secrets set {secret}")
        if not key or any(ord(c) < 33 or ord(c) == 127 for c in key):
            raise ValueError(f"--api-key {host}={name}: the secret {secret} is not usable in a header")
        entries.append({"host": host, "port": 443, "name": name, "placeholder": API_KEY_PLACEHOLDER.format(name=name),
                        "key": key})
    return entries


_publish_logins_module = None


def load_publish_logins_module():
    global _publish_logins_module
    if _publish_logins_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_publish_logins", HERE / "publish_logins.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _publish_logins_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._launcher = RunningLauncher()
                _publish_logins_module = loaded
    return _publish_logins_module


def publish_kinds(a):
    """The kinds of --publish, each once (ValueError for an unknown one)."""
    return load_publish_logins_module().kinds_value(getattr(a, "publish", []))


def publish_hosts(a):
    """The hosts --publish opens ([] when a kind or its login is wrong: validate() reports that)."""
    try:
        return [host for entry in load_publish_logins_module().plan(publish_kinds(a), publish_logins_of(a))
                for host in entry["hosts"]]
    except ValueError:
        return []


NPMRC_TARGET = "/sandbox/session/npmrc"


def publish_environment(a):
    """The agent's variables for --publish: placeholders and user names, never a token; npm's configuration."""
    kinds = publish_kinds(a)
    if not kinds:
        return {}
    module = load_publish_logins_module()
    env = module.environment(kinds, publish_logins_of(a))
    if module.npmrc(kinds, publish_logins_of(a)):
        env["NPM_CONFIG_GLOBALCONFIG"] = NPMRC_TARGET  # yours, ~/.npmrc, still applies
    return env


def publish_login_fix(a, kind):
    """The fix (fixes.py) for --publish KIND without a login (yours, or the identity's with --identity): its token
    kept as the secret KIND_TOKEN (asked in this terminal, never shown; one kept already is taken) and the login
    added with it. Security-relevant, said: the agent can publish as you (the proxy adds the token on the wire, each
    upload is asked first)."""
    secret = f"{kind.upper()}_TOKEN"
    try:
        kept = load_secret_store_module().get(secret) is not None
    except Exception as exc:  # noqa: BLE001 - not read: asked for
        LOG.debug("fixes", f"whether the secret {secret} is kept was not read", exc)
        kept = False
    name, _identity = identity_of(a)
    add = (["sandbox-ai", "identities", "publish", name, kind, "--secret", secret] if name else
           ["sandbox-ai", "publish-logins", "add", kind, "--secret", secret])
    return FIXES.fix("publish", kind, f"there is no publish login for {kind}" + (f" of the identity {name}" if name
                                                                                   else ""),
                     ("add it with your token, kept as the secret " + secret
                      + (" (kept already)" if kept else " (its value is asked in this terminal, never shown)")),
                     [*([] if kept else [["sandbox-ai", "secrets", "set", secret]]), add], duration="seconds",
                     terminal=not kept,
                     security=f"the agent can publish to {kind} as you: the connection proxy adds the token on the "
                              "wire (the agent never sees it), each upload is asked first")


def check_publish(a, get=None):
    """--publish: known kinds with a login each, the proxy to add the tokens, their hosts inspected, no variable
    given twice, never in an MCP server's sandbox, and each token kept (GET reads the store; False skips that)."""
    kinds = publish_kinds(a)
    if not kinds:
        return
    module = load_publish_logins_module()
    logins = publish_logins_of(a)
    try:
        found = module.plan(kinds, logins)
    except ValueError as exc:
        have = {entry["kind"] for entry in (module.load() if logins is None else logins)}
        lacking = [kind for kind in kinds if kind not in have]
        if not lacking:
            raise
        fixes = [publish_login_fix(a, kind) for kind in lacking]
        if logins is None:
            raise FIXES.Missing(str(exc), fixes) from None
        raise FIXES.Missing(f"--publish {lacking[0]}: the identity {a.identity} has no login for it; add one with: "
                            f"sandbox-ai identities publish {a.identity} {lacking[0]} --secret NAME", fixes) from exc
    if a.no_proxy:
        raise ValueError("--publish needs the connection proxy (it adds the tokens on the wire); drop --no-proxy")
    if getattr(a, "mcp_socket_dir", None):
        raise ValueError("--publish is for an agent's sandbox, not an MCP server's")
    inspected_off = {h.lower() for h in a.no_inspect}
    for entry in found:
        for host in entry["hosts"]:
            if any(host_matches_any(host, value) for value in inspected_off):
                raise ValueError(f"--publish {entry['kind']}: the proxy adds the token only to requests it opens; "
                                 f"drop --no-inspect {host}")
    taken = {value.partition("=")[0] for value in a.secret_env} | set(a.env) | {n for _, n, _ in api_key_values(a)}
    for name in module.environment(kinds, logins):
        if name in taken:
            raise ValueError(f"--publish: the variable {name} is given twice")
    if "github" in kinds and not a.token_proxy:
        urls = set()
        for kind, value in git_token_values(a):
            with contextlib.suppress(ValueError):  # a wrong one: validate() reports it
                urls.add(git_token_credential(value, kind)["url"])
        if "https://github.com" in urls:
            raise ValueError("--publish github: a --git-token-* option for https://github.com gives the agent "
                             "GH_TOKEN itself; add --token-proxy or drop one of them")
    if get is not False:
        module.proxy_entries(kinds, get or load_secret_store_module().get, logins)


def write_browser_shims(directory):
    """xdg-open and friends for the sandbox (DIRECTORY is the session's bin/, /sandbox/session/bin there)."""
    directory.mkdir(mode=0o755, exist_ok=True)
    for name in BROWSER_SHIMS:
        path = directory / name
        path.write_text('#!/bin/sh\nexec python3 -I /sandbox/runtime.py --internal-open "$@"\n')
        path.chmod(0o755)


def load_approvals_module():
    spec = importlib.util.spec_from_file_location("sandbox_approvals", HERE / "approvals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def asking(a):
    """Approvals are asked for: the proxy runs and --ask is on or allow (not in probes). With allow the launcher
    answers each question itself (asks_nobody)."""
    return proxy_enabled(a) and a.ask in ("on", "allow") and not a.probe


def tor_run(a):
    """A start behind a Tor profile (--vpn)."""
    return (getattr(a, "vpn_profile", None) or {}).get("type") == "tor"


def network_note(a):
    """The run's network note (vpn.agent_note: how this run reaches the network through its Tor or VPN profile) for an
    agent the sandbox runs itself, else None. Every agent sees it from its start (NOTE_PLACES)."""
    if not getattr(a, "vpn", None) or not getattr(a, "vpn_profile", None) or not runs_agent(a) or a.probe:
        return None
    return load_vpn_module().agent_note(a)


# Where each agent reads the start's note (start_note: the run's network note and the line that names its intro,
# /sandbox/session/INTRO.md) from its start - beside /sandbox/session/network.md, which the skill names. Never a file
# of the user's repository.
NOTE_PLACES = {"claude": "/etc/claude-code/CLAUDE.md (Claude Code's managed instructions, read-only)",
               "codex": "-c developer_instructions (a developer message of its own)",
               "opencode": "OPENCODE_CONFIG_CONTENT's instructions: /sandbox/session/network.md (your AGENTS.md stays)",
               "qwen": "--append-system-prompt",
               "dsh": "/etc/dsh/sandbox-ai.patch.yml: the system-prompt row's personaPrefix",
               "goose": "GOOSE_MOIM_MESSAGE_FILE /etc/goose/sandbox-ai-loop-guard.md (Top Of Mind: every turn)",
               "hermes": "/etc/hermes/skills/sandbox-ai-network (a skill Hermes loads in every session)",
               "openhands": "/etc/openhands/settings.json: the agent's system_message_suffix",
               "pi": "/etc/pi/sandbox-ai.json: a section of Pi's system prompt (sandbox-ai's extension)",
               "opencode2": "OPENCODE_CONFIG_CONTENT's instructions: /sandbox/session/network.md (your AGENTS.md "
                            "stays)",
               "nanobot": "~/.nanobot/workspace/skills/sandbox-ai-network (an always-loaded skill: in its system "
                          "prompt every turn; read-only)",
               "letta": "~/.letta/mods/sandbox-ai-network.mjs (a mod of sandbox-ai's: a system reminder before the "
                        "first message of each conversation)"}


_intro_module = None


def load_intro_module():
    """intro.py: the agent's intro (/sandbox/session/INTRO.md: what its sandbox has) and the line that names it."""
    global _intro_module
    if _intro_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_intro", HERE / "intro.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _intro_module = module
    return _intro_module


INTRO_VARIABLE = "SANDBOX_AI_INTRO"  # =0: no start of this environment tells its agent (=1: each does); over your
#                                      defaults file, under --intro and --no-intro (a script's or a CI job's choice;
#                                      sandbox-ai's own tests start with =0, the intro's tests without it)


def intro_on(a):
    """The agent is told what its sandbox has (intro.py): an agent the sandbox runs itself (no probe, no MCP server's
    sandbox), unless --no-intro - or the environment's SANDBOX_AI_INTRO=0, or your default agent_intro false, each
    without --intro."""
    if not runs_agent(a) or a.probe or getattr(a, "mcp_socket_dir", None):
        return False
    chosen = getattr(a, "intro", None)
    if chosen is not None:
        return bool(chosen)
    if os.environ.get(INTRO_VARIABLE) in ("0", "1"):
        return os.environ[INTRO_VARIABLE] == "1"
    return getattr(a, "intro_default", None) is not False


def start_note(a):
    """What the agent reads from its start, where NOTE_PLACES says: the run's network note (network_note) and the
    line that names its intro (intro.LINE); None when the run has neither."""
    return joined_text(network_note(a), load_intro_module().LINE if intro_on(a) else None)


def note_path(a):
    """The file a harness that takes a file reads at its start (OpenCode, OpenCode 2, Letta Code): the notes joined
    (/sandbox/session/agent-note.md) when the run has an intro, the network note's own file when it has only that,
    None for neither."""
    if intro_on(a):
        return load_intro_module().NOTE_PATH
    return load_vpn_module().NOTE_PATH if network_note(a) else None


def intro_plan(a):
    """The dry run's "intro": the file, its text, the line and where this agent reads it; None without (--no-intro)."""
    if not intro_on(a):
        return None
    return load_intro_module().plan(a, launcher_namespace(), note_place(a))


def note_place(a):
    """Where this run's agent reads the start's note (NOTE_PLACES), with the file a harness that takes one is given
    (note_path: the notes joined when the run has an intro)."""
    return NOTE_PLACES[a.agent].replace(load_vpn_module().NOTE_PATH, note_path(a) or load_vpn_module().NOTE_PATH)


def note_files(a):
    """{path in the session directory: content} of what the agent reads from its start: the network note's
    network.md (/sandbox/session/network.md), the intro's INTRO.md and agent-note.md (the notes joined, for a harness
    that takes a file), and for Claude Code its managed CLAUDE.md (hook_mount mounts that directory at
    /etc/claude-code)."""
    note, whole = network_note(a), start_note(a)
    files = {load_vpn_module().NOTE_FILE: note} if note else {}
    if whole and a.agent == "claude":
        files["claude-code/CLAUDE.md"] = whole
    if intro_on(a):
        intro = load_intro_module()
        files[intro.FILE] = intro.text(intro.resources(a, launcher_namespace()))
        files[intro.NOTE_FILE] = whole
    return files


def joined_text(*parts):
    """The PARTS that are not empty, one paragraph each (the loop guard's text and the network note in one place)."""
    given = [part for part in parts if part]
    return "\n\n".join([*(part.rstrip("\n") for part in given[:-1]), given[-1]]) if given else None


def host_browser(a):
    """The sandbox's xdg-open goes to your browser (as a question): in a run that asks, never behind Tor, where your
    browser would open the agent's URL from your own address."""
    return asking(a) and not tor_run(a)


def dsh_session_browser(a):
    """DeepSeek Harness' web UI opens in the session browser: a DSH start of its chat in the terminal (the web UI
    beside it) or of its web UI (dsh.py: interactive) with a browser you see (--browser, control human or both). Its
    launch URL (with its token) reaches the launcher on the sandbox's browser socket (dsh_web.py), which opens exactly
    that URL there."""
    return (a.agent == "dsh" and a.binary is None and not a.probe and not getattr(a, "helper_of", None)
            and bool(getattr(a, "browser_container", None)) and load_browser_module().human_sees(a)
            and load_dsh_module().interactive(a.agent_args))


def dsh_web_port(a):
    """DSH's web UI's port in the sandbox (dsh.web_port): not one the firewall relays to the host (host_ports: a
    connection there would go to the host's port) nor a service's."""
    taken = set(host_ports(a)) | {int(port) for service in getattr(a, "services", None) or []
                                  for port in service.get("ports", [])}
    return load_dsh_module().web_port(taken)


def dsh_web_ui_port(a):
    """The port DSH's web UI listens on in this run (dsh_web_port) when it runs - its web profile, or the chat with
    the session browser - else None. In run.json: `sandbox-ai net allow --allow-host-port` never relays the host's
    port there (the relay would take the sandbox's 127.0.0.1:PORT from DSH)."""
    if a.agent != "dsh" or a.binary is not None or a.probe or getattr(a, "helper_of", None):
        return None
    if not (load_dsh_module().web_profile(a.agent_args or []) or dsh_session_browser(a)):
        return None
    return dsh_web_port(a)


def browser_requests(a):
    """The sandbox's browser socket (BROWSER_DIR/open.sock) is served: URLs to your browser (host_browser), or DSH's
    web UI to the session browser (dsh_session_browser)."""
    return host_browser(a) or dsh_session_browser(a)


def session_browser_opener(a):
    """approvals.browser_reply's SESSION for this run: DSH's web UI into the session browser (its VNC folder), else
    None."""
    if not dsh_session_browser(a):
        return None
    browser = load_browser_module()
    return load_dsh_module().session_opener(lambda url: browser.request_open(a.browser_vnc_dir, url),
                                            log=lambda text: LOG.info("browser", text))


def asks_nobody(a):
    """--ask allow in a run that asks: every question is answered "allow" by the launcher, at once (approvals.watch
    and serve_browser with auto). Only the launcher's own options set it: nothing in the sandbox can (no
    `sandbox-ai net` option, no control request, no file the agent reaches)."""
    return asking(a) and a.ask == "allow"


ASK_ALLOW_WARNING = (
    "warning: --ask allow: nothing is asked, every question is allowed at once (for the run): connections and "
    "lookups the options do not allow, pushes with privacy warnings, requests, MCP and tool calls an ask rule holds "
    "(writes in mode high and calls in a tainted run too), devices a rule asks about, URLs to open in your browser. "
    "Each is recorded as \"allowed automatically (--ask allow)\": sandbox-ai approvals history, the connection "
    "log, sandbox-ai logs. Still refused: what rules deny, the privacy check's blocking findings, what you blocked "
    "always; a canary's alarm still pauses the sandbox and waits for you, and so does a request the secret guard "
    "holds (a secret on its way to another host: allow where it may go beforehand with --allow-send)")


def ask_allow_notes(a):
    """The warnings a start with --ask allow prints (MODES.check refused it already where a floor sets a strict
    mode): what is answered, and that a strict mode's questions are answered too."""
    if a.ask != "allow" or a.probe:
        return []
    if not asking(a):
        return ["--ask allow does nothing here: without the proxy (--no-proxy, --offline) nothing is asked, so "
                "nothing the options do not allow is reached"]
    notes = [ASK_ALLOW_WARNING]
    if a.mode in MODES.STRICT:
        notes.append(f"warning: --ask allow answers the questions of the security mode {a.mode} "
                     f"({a.mode_origin}) too: you chose both, so both hold - what the mode refuses stays refused, "
                     "what it would ask about is allowed (writes to hosts other than the agent's provider, MCP and "
                     "tool calls once untrusted content entered the run)"
                     + ("; answers approve once only (the mode)" if MODES.MODES[a.mode].get("approval_scope") == "once"
                        else ""))
    return notes


def apply_remembered_approvals(a):
    """Add what was approved "always" for this project (sandbox-ai approvals) to the options; the notes."""
    approvals = load_approvals_module()
    # In a tree the agent can write (a folder you chose holds XDG_CONFIG_HOME): read like the trust store, the
    # defaults file and the kit hook approvals beside it - you decide - with a warning that the agent could have
    # approved its own wishes there. A link planted on the way stays unfollowed: not read then.
    try:
        inside = check_host_source(a, approvals.approved_file(), "Remembered approvals (XDG_CONFIG_HOME)",
                                   warn=lambda _text: None)
    except ValueError as exc:
        return [f"{exc}; not read"]
    if inside:
        warner()(f"{inside} (the agent could have approved its own wishes there for its next start)")
    # Blocked always: the supervisor refuses these at once, without a question (config.json "ask_blocked").
    a.blocked_approvals = approvals.blocked_for(Path(a.workspaces[0]).expanduser())
    remembered = approvals.approved_for(Path(a.workspaces[0]).expanduser())
    already = set(allowed_hosts(a))
    hosts = [h for h in remembered["hosts"] if h not in already]
    tcp = [t for t in remembered["tcp"] if t not in a.allow_tcp]
    # Under the administrator's ceiling, what lies outside it is not applied (it may predate the policy).
    outside = [h for h in hosts if not within_ceiling(a, host=h)]
    outside += [t for t in tcp if not within_ceiling(a, network=tcp_targets(argparse.Namespace(allow_tcp=[t]))[0][0])]
    hosts, tcp = [h for h in hosts if h not in outside], [t for t in tcp if t not in outside]
    ignored = [f"not applied, outside the administrator's policy: {', '.join(outside)}"] if outside else []
    # An agent whose telemetry is never switchable: its telemetry hosts stay closed (approved for another agent of
    # the project, they are left out here; joined to --allow-host, check_telemetry would refuse the whole start).
    agent = getattr(a, "agent", None)
    if agent in UNSWITCHABLE_TELEMETRY:
        module = load_hermes_module() if agent == "hermes" else agent_module(agent)
        telemetry = [h for h in hosts if module.telemetry_hosts_in([h])]
        hosts = [h for h in hosts if h not in telemetry]
        ignored += [f"not applied, {UNSWITCHABLE_TELEMETRY[agent]}'s telemetry (never opened): "
                    f"{', '.join(telemetry)}"] if telemetry else []
    # One approved before a --subnet that now holds it would stop every start (see check_subnets).
    own = [t for t in tcp if in_subnet(a, tcp_targets(argparse.Namespace(allow_tcp=[t]))[0][0])]
    tcp = [t for t in tcp if t not in own]
    ignored += [f"not applied, inside the sandbox's own network: {', '.join(own)}"] if own else []
    a.allow_host += hosts
    a.remembered_hosts = hosts  # resolved apart from the rest (see remembered_pins)
    a.allow_tcp += tcp
    # Approved, not chosen as an option: such addresses never get SSH (see proxy_config).
    a.approved_tcp = list(getattr(a, "approved_tcp", [])) + tcp
    if not (hosts or tcp) or a.offline:
        return ignored
    return ignored + [f"allowed because you approved them always for this project: {', '.join(hosts + tcp)} "
            f"(remove in {load_approvals_module().approved_file()})"]


def remembered_pins(a, names, resolve):
    """({name: addresses}, notes) for NAMES remembered "always" (approved lookups, connections whose name a
    certificate proved), resolved as RESOLVE resolves --allow-host but only to what an approval may open: public
    addresses, or ones inside the administrator's allow_within. A name that does not resolve now, or only to
    other addresses (a moved service, DNS rebinding to the LAN), is left out and the notes say so: it never
    stops the start, and never opens the LAN or this host."""
    resolved, notes = {}, []
    try:
        resolved = resolve(list(names)) if names else {}
    except (ValueError, OSError, subprocess.SubprocessError):
        for name in names:  # one at a time: one name that no longer resolves must not take the others along
            try:
                resolved.update(resolve([name]))
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
    ceiling = (getattr(a, "policy", {}) or {}).get("allow_within") is not None
    pins = {}
    for name in names:
        found = resolved.get(name) or []
        kept = [address for address in found if RUNTIME.public_address(ipaddress.ip_address(address))
                or (ceiling and within_ceiling(a, network=address))]
        if not found:
            notes.append(f"{name} (approved always) is not opened: it does not resolve now")
        elif not kept:
            notes.append(f"{name} (approved always) is not opened: it resolves only to addresses that are not "
                         f"public ({', '.join(found)})")
        else:
            pins[name] = kept
            if len(kept) < len(found):
                notes.append(f"{name} (approved always): not opened, not public: "
                             + ", ".join(address for address in found if address not in kept))
    return pins, notes


def apply_remembered_pins(a, names, resolve):
    """The pins of the remembered NAMES (remembered_pins), with what was left out dropped from --allow-host and
    told on stderr."""
    if not names:
        return {}
    pins, notes = remembered_pins(a, names, resolve)
    a.allow_host = [name for name in a.allow_host if name not in names or name in pins]
    for note in notes:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    return pins


def proxy_enabled(a):
    """The connection proxy runs unless switched off or there is no network."""
    return not a.no_proxy and not a.offline


def proxy_allow(a, hosts):
    """What the proxy may connect to: the firewall's permissions, as proxy.json's "allow"."""
    addresses = [addr for values in hosts.values() for addr in values] + host_networks(hosts)
    return {"all": a.egress_all_allow, "nets": [str(ipaddress.ip_network(n, strict=False)) for n in a.allow],
            "https": sorted({str(ipaddress.ip_network(n, strict=False)) for n in addresses}),
            "tcp": [[addr, port] for addr, port in tcp_targets(a)] + [[ns, 53] for ns in nameservers(a)],
            "loopback": host_ports(a)}


HTTP_RULE = re.compile(r"(\*\.)?([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+) +(\*|[A-Z]{3,10}(?:,[A-Z]{3,10})*) +(/[!-~]{0,500})")


def http_rules(a):
    """[{"host", "methods", "path"}] from --allow-http: what the proxy lets through to a host that has rules."""
    rules = []
    for value in a.allow_http:
        match = HTTP_RULE.fullmatch(value.strip())
        if not match:
            raise ValueError(f"--allow-http expects 'HOST METHODS PATH', e.g. 'api.github.com GET,HEAD /repos/*': "
                             f"{printable(value)!r}")
        host = (match[1] or "") + match[2]
        if any(host_matches_any(host, value) for value in a.no_inspect):
            raise ValueError(f"--allow-http {host}: requests to a host under --no-inspect are not seen, so "
                             "rules for it cannot hold")
        rules.append({"host": host, "methods": match[3].split(","), "path": match[4]})
    return rules


def user_rules_file():
    """Your own ruleset for every start (XDG_CONFIG_HOME/sandbox-ai/rules.json), used when it exists."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / RULES_NAME


BUILTIN_RULESETS = HERE / "rulesets"  # sandbox-ai's own rulesets, opt-in: --rules builtin:NAME


def builtin_ruleset_names():
    """The names of sandbox-ai's own rulesets (sandbox/rulesets/NAME.json)."""
    try:
        return sorted(path.stem for path in BUILTIN_RULESETS.glob("*.json"))
    except OSError:
        return []


def rules_file(value):
    """--rules VALUE as the file it names: builtin:NAME is sandbox-ai's own ruleset NAME (README, Rulesets: ready-made
    rulesets), anything else the path given."""
    text = str(value)
    if text.startswith("builtin:"):
        name = text[len("builtin:"):]
        if name not in builtin_ruleset_names():
            raise argparse.ArgumentTypeError(f"no built-in ruleset {name!r}; there are: "
                                             f"{', '.join(builtin_ruleset_names())}")
        return BUILTIN_RULESETS / f"{name}.json"
    return Path(text)


def ruleset_label(path):
    """How a --rules file is named in logs and answers: builtin:NAME for sandbox-ai's own, else --rules FILENAME."""
    path = Path(path)
    if path.parent == BUILTIN_RULESETS and path.suffix == ".json":
        return f"builtin:{path.stem}"
    return f"--rules {path.name}"


def audit_mode(source):
    """{"mode": "audit"} for an audit ruleset (it decides nothing, its verdicts are recorded), else {}: what each part
    of the rulesets handed to the proxy, the relays and the hooks keeps of it."""
    return {"mode": "audit"} if source.get("mode") == "audit" else {}


def read_ruleset(path, name):
    """The ruleset in the file PATH, checked; NAME stands for it in logs and in what the agent is told."""
    try:
        # Non-blocking: a FIFO in its place must not hang the start.
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"Rules {path}: {exc.strerror}") from None
    with open(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Rules {path} is not a regular file")
        data = stream.read(CONFIG_LIMIT + 1)
    if len(data) > CONFIG_LIMIT:
        raise ValueError(f"Rules {path}: larger than {CONFIG_LIMIT} bytes")
    try:
        document, duplicated = RUNTIME.Ruleset.parse_json(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Rules {path} is not valid JSON: {exc}") from None
    if duplicated:
        raise ValueError(f"Rules {path}: a key is given twice")
    try:
        return RUNTIME.Ruleset.validated(document, f"Rules {path}")
    except RUNTIME.RuleError as exc:
        raise ValueError(str(exc)) from None


def load_rulesets(a):
    """The rulesets of this run, in order: the administrator's policy, your rules.json, each --rules file,
    --allow-http, the trusted project file's "rules", the agents' telemetry (--telemetry). Each decides on its own and the strictest decision wins
    (no ruleset can widen another); the order names who decided. Read once; a broken file stops the start."""
    if getattr(a, "rulesets", None) is not None:
        return a.rulesets
    sources = []
    policy_rules = a.policy_rules if hasattr(a, "policy_rules") else load_policy().get("rules")
    if policy_rules is not None:
        try:
            sources.append({"name": "policy", **RUNTIME.Ruleset.validated(policy_rules, f"Policy {POLICY_FILE}: rules")})
        except RUNTIME.RuleError as exc:
            raise ValueError(str(exc)) from None
    mode_rules = getattr(a, "mode_rules", None)  # the security mode's own (modes.py), right after the policy's
    if mode_rules is not None:
        sources.append({"name": f"mode {a.mode}", **RUNTIME.Ruleset.validated(mode_rules, f"mode {a.mode}")})
    files = [(user_rules_file(), RULES_NAME)] if user_rules_file().exists() else []
    files += [(path, ruleset_label(path)) for path in a.rules]
    taken = {"policy", "--allow-http", "project", *(s["name"] for s in sources)}
    for path, name in files:
        unique, number = name, 2
        while unique in taken:  # two files of one name: told apart in logs and answers
            unique, number = f"{name} ({number})", number + 1
        taken.add(unique)
        sources.append({"name": unique, **read_ruleset(path, unique)})
    if a.allow_http:
        sources.append({"name": "--allow-http", **RUNTIME.Ruleset.validated({"version": 1, "http": [
            {"id": f"allow-http-{index}", "decision": "allow", **rule} for index, rule in enumerate(http_rules(a), 1)]},
            "--allow-http")})
    document = getattr(a, "project_document", None) or {}
    if "rules" in document:
        try:
            sources.append({"name": "project", **RUNTIME.Ruleset.validated(document["rules"], "project rules")})
        except RUNTIME.RuleError as exc:
            raise ValueError(str(exc)) from None
    publish = load_publish_logins_module().rules(publish_kinds(a), publish_logins_of(a)) \
        if getattr(a, "publish", None) else None
    if publish:
        sources.append({"name": "publish", **RUNTIME.Ruleset.validated(publish, "--publish")})
    telemetry = telemetry_rules(a)  # last: it only refuses, and the others keep their places in logs and answers
    if telemetry:
        sources.append({"name": "telemetry", **RUNTIME.Ruleset.validated(telemetry, "telemetry")})
    a.rulesets = sources
    return sources


def ask_timeout(a):
    """How long a request of the run A waits for the answer to its question (--ask-timeout, defaults ask_timeout)."""
    seconds = getattr(a, "ask_timeout", None)
    return seconds if isinstance(seconds, int) and not isinstance(seconds, bool) else ASK_TIMEOUT_DEFAULT


def check_rulesets(a):
    """Read the rulesets and refuse a start where they could not hold: HTTP rules without the proxy, or for a
    host whose TLS passes unopened (--no-inspect)."""
    http = [source for source in load_rulesets(a) if source["http"] or source["defaults"]["http"] != "allow"]
    if http and not proxy_enabled(a) and not a.offline:
        raise ValueError(f"The HTTP rules of {', '.join(source['name'] for source in http)} need the connection "
                         "proxy (not with --no-proxy)")
    for source in http:
        hosts = [host for rule in source["http"] for host in rule["host"]]
        hosts += ["*"] if source["defaults"]["http"] != "allow" else []
        for value in a.no_inspect:
            for host in hosts:
                if host == "*" or host_matches_any(host, value):
                    raise ValueError(f"--no-inspect {value}: the HTTP rules of {source['name']} ({host}) cannot hold "
                                     "for requests the proxy does not open")


def mcp_gateway(a, name, approvals=None):
    """The gateway configuration of MCP server NAME ({"server", "rules", "approvals"}) from this run's rulesets,
    or None when none has rules for it. APPROVALS: where the relay reads the answers to its questions."""
    sources = []
    for source in load_rulesets(a):
        # The rules for tainted runs hold in the run's own relay (mcp_relay_config), which knows its taint.
        rules = [rule for rule in source["mcp"]
                 if not rule.get("when") and ("*" in rule["server"] or name in rule["server"])]
        if rules or source["defaults"]["mcp"] != "allow":
            sources.append({"name": source["name"], "version": 1, **audit_mode(source),
                            "defaults": {"mcp": source["defaults"]["mcp"]}, "mcp": rules})
    if not sources:
        return None
    config = {"server": name, "rules": sources}
    if approvals and RUNTIME.Ruleset(sources).mcp_asks():
        config["approvals"] = str(approvals)
        config["timeout"] = ask_timeout(a)  # (its own questions wait as long as the run's)
    return config


def host_matches_any(host, pattern):
    """HOST (a name or *.DOMAIN) and PATTERN (the same) can name the same server."""
    host, pattern = host.lower(), pattern.lower()
    bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
    return host == pattern or bare(host).endswith("." + bare(pattern)) or bare(pattern).endswith("." + bare(host)) \
        or bare(host) == bare(pattern)


def proxy_config(a, hosts, log_name):
    """proxy.json: allow-list, hosts that stay unopened, SSH policy, body limit."""
    privacy = load_privacy_module()
    public_git = sorted({addr for name, values in hosts.items() for addr in values
                         if any(name == h or name.endswith("." + h) for h in privacy.PUBLIC_GIT_HOSTS)})
    explicit = [str(ipaddress.ip_network(n, strict=False)) for n in a.allow]
    # --allow-tcp IP:PORT gives SSH to that port only; what was merely approved "always" gets none.
    approved_targets = set(tcp_targets(argparse.Namespace(allow_tcp=getattr(a, "approved_tcp", []))))
    explicit_tcp = [[addr, port] for addr, port in tcp_targets(a) if (addr, port) not in approved_targets]
    return {"port": PROXY_PORT, "log": log_name, "body_limit": PROXY_BODY_LIMIT,
            "ask_timeout": ask_timeout(a),  # a request waits this long for the supervisor's answer (and a little more)
            # the LLM traffic log (--llm-traffic): what it records (None: nothing), how much of one request
            "traffic": llm_traffic_config(a),
            # What the model requests cost and the budgets that stop the run (sandbox-ai spend).
            "spend": spend_config(a),
            "no_inspect": [value.lower() for value in a.no_inspect], "allow": proxy_allow(a, hosts),
            "ssh": {"block": a.privacy_check != "off", "allowed": explicit, "allowed_tcp": explicit_tcp,
                    "public_git": public_git},
            "push_check": a.privacy_check != "off",
            "debug": a.log_debug,
            # binary answers by their digest and first bytes (runtime.binary_answer), or "all": whole up to the limit
            "connection_bodies": getattr(a, "connection_bodies", None) or "text",
            # The agent's own provider: never a source of untrusted content, never held by a rule for tainted runs.
            "providers": provider_hosts(a),
            # --token-proxy: the tokens live here, in the proxy's directory, which the agent cannot read.
            # sandbox-ai git-logins: the same, for every run with the proxy; the start's options win for their server.
            "git_tokens": [{"host": credential["host"], "port": credential["port"], "user": credential["user"],
                            "loopback": credential["loopback"], "token": token}
                           for credential, token in proxied_git_tokens(a)],
            # --llm: the endpoint's key and header secrets, added on the wire; its own CA.
            # OpenCode's session title: the name this start runs under (sandbox-ai list), never the model's answer.
            "llm": load_llm_module().proxy_entry(a, title=run_name(a)) if a.llm else [],
            # --api-key: the keys, added on the wire in place of the agent's placeholder.
            "api_keys": (api_key_entries(a) if a.api_key else [])
            + (load_publish_logins_module().proxy_entries(publish_kinds(a), load_secret_store_module().get,
                                                          publish_logins_of(a))
               if getattr(a, "publish", None) else []),
            # Rules of a host also hold for its pinned addresses: a request to the bare IP is no way around them.
            "http_rules": [{**rule, "addresses": sorted({addr for name, values in {**hosts, **getattr(a, "llm_pins", {})}.items()
                                                         for addr in values if host_matches_any(name, rule["host"])})}
                           for rule in http_rules(a)],
            # --canaries: the tokens of the run's decoy files in every form the proxy looks for (canary.py)
            **({"canary": a.canary_plan.proxy_entry()} if getattr(a, "canary_plan", None) else {}),
            # The package guard: what it decides for downloads from npm, PyPI, crates.io and Go's module proxy.
            **({"packages": package_guard_config(a)} if package_guard(a) else {}),
            # The secret guard: the secrets this run is handed, in every form a request may carry them, the token
            # shapes, and what a finding on its way to another host does.
            **({"secrets": secret_guard_config(a)} if secret_guard(a) else {}),
            **proxy_rules(a, hosts)}


_packages_module = None


def load_packages_module():
    """packages.py (the package guard's host side), loaded once and handed this launcher's runtime."""
    global _packages_module
    if _packages_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_packages", HERE / "packages.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._modules["runtime"] = RUNTIME  # the running one: never loaded a second time (40 ms of a start)
        _packages_module = module
    return _packages_module


def package_guard(a):
    """The package guard runs in this start: wherever the connection proxy does, unless --no-package-guard."""
    return proxy_enabled(a) and not getattr(a, "no_package_guard", False)


def package_allowed(a):
    """The packages this run's guard passes without a question: --allow-package and what `sandbox-ai packages allow`
    keeps for the project (the first workspace), each in its normal form. ValueError names an entry that is none."""
    packages = load_packages_module()
    try:
        given = [packages.spec_text(spec) for spec in getattr(a, "allow_package", None) or []]
    except ValueError as exc:
        raise ValueError(f"--allow-package {exc}") from None
    kept = []
    if getattr(a, "workspaces", None) and packages.allow_file().exists():
        # In a tree the agent can write (a folder you chose holds XDG_CONFIG_HOME): read, as the remembered approvals
        # beside it are - you decide - with a warning that the agent could have allowed its own packages there. A
        # link planted on the way is not followed: not read then.
        try:
            inside = check_host_source(a, packages.allow_file(), "The packages allow-list (XDG_CONFIG_HOME)",
                                       warn=lambda _text: None)
        except ValueError as exc:
            warner()(f"{exc}; not read")
            return list(dict.fromkeys(given))
        if inside:
            warner()(f"{inside} (the agent could have allowed its own packages there for its next start)")
        kept = packages.allowed_for(Path(a.workspaces[0]).expanduser())
    return list(dict.fromkeys([*given, *kept]))


def package_guard_config(a):
    """proxy.json's "packages" (runtime.PackageGuard): the guard's decisions - the defaults (the minimum age your
    defaults.json sets: package_min_age_days), the security mode's and every ruleset's "packages" section, the
    strictest of each (packages.proxy_settings) - and the allow-list."""
    return load_packages_module().proxy_settings(load_rulesets(a), getattr(a, "mode_packages", None),
                                                 getattr(a, "mode", None), package_allowed(a),
                                                 getattr(a, "package_min_age_default", None),
                                                 getattr(a, "mode_packages_base", None))


_secret_guard_module = None


def load_secret_guard_module():
    """secret_guard.py (the secret guard's host side), loaded once and handed this launcher's runtime."""
    global _secret_guard_module
    if _secret_guard_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_secret_guard", HERE / "secret_guard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._modules["runtime"] = RUNTIME  # the running one: never loaded a second time
        _secret_guard_module = module
    return _secret_guard_module


def secret_guard(a):
    """The secret guard runs in this start: wherever the connection proxy does."""
    return proxy_enabled(a)


def secret_guard_values(a, read=True):
    """([value], [note]) for this run's secret guard: every secret the run is handed in plain text, as
    secret_guard.value_entry has it ({"id", "label", "value", "hosts"}) - --secret-env; --env variables whose name
    says secret or whose value has a token's shape; the Git tokens that enter the sandbox (--git-token-env, -file,
    -secret and --git-credentials without --token-proxy), each given for its server; the secret variables of the
    start's MCP image servers. Read once per start; the values are never printed, and the notes name none.
    READ false (the dry run): what would be registered, by name alone - {"id", "label", "hosts"} without a value.
    No secret is read for it: no keyring is asked, and a secret that is not kept fails nothing there."""
    cached = getattr(a, "_secret_guard_values", None) or {}
    if read in cached:
        return cached[read]
    guard = load_secret_guard_module()
    found, notes, taken = [], [], set()

    def add(ident, label, value, hosts=()):
        if ident in taken:
            return
        if not read:
            taken.add(ident)
            found.append({"id": ident, "label": label, "hosts": list(dict.fromkeys(hosts))})
            return
        entry = guard.value_entry(ident, label, value, hosts)
        if entry is not None:
            taken.add(ident)
            found.append(entry)
        elif isinstance(value, str) and value.strip():
            size = f"fewer than {guard.VALUE_MIN}" if len(value.strip()) < guard.VALUE_MIN else \
                f"more than {guard.VALUE_MAX}"
            notes.append(f"note: the secret guard does not look for {label}: a value of {size} characters is not "
                         "told from ordinary traffic")
    given = secret_environment(a) if read else {value.partition("=")[0]: None for value in a.secret_env}
    for name, value in given.items():
        add(f"env:{name}", f"the value of {name} (--secret-env)", value, guard.env_hosts(name))
    for name in a.env:
        value = os.environ.get(name)  # (your environment, not the secret store: read for the dry run too)
        if isinstance(value, str) and (guard.secret_name(name) or guard.shape_of(value)):
            add(f"env:{name}", f"the value of {name} (--env)", value, guard.env_hosts(name))
    if not a.token_proxy:
        tokens = git_tokens(a) if read else \
            [(git_token_credential(value, kind), None) for kind, value in git_token_values(a)] + \
            [(credential, None) for credential, _ in git_credentials_file(a)[0]]
        for credential, token in tokens:
            option = next((f"--git-token-{kind}" for kind in ("env", "file", "secret") if kind in credential),
                          "--git-credentials")
            add(f"git-token:{credential['host']}", f"the Git token for {credential['host']} ({option})", token,
                guard.server_hosts(credential["host"]))
    if getattr(a, "mcp", None) and not (a.probe or a.mcp_socket_dir):
        # An image server's variables stay in its own sandbox; what it hands the agent of them is the agent's then.
        try:
            mcp = load_mcp_module()
            servers = mcp.start_servers(a)
            for name in dict.fromkeys(a.mcp):
                definition = mcp.start_definition(name, a, servers)[1]
                if "image" not in definition and "rootfs" not in definition:
                    continue
                secret = set(definition.get("env_secrets") or {}) | {
                    variable for variable in [*definition.get("env", []), *definition.get("env_optional", [])]
                    if guard.secret_name(variable) or variable not in os.environ}
                values = mcp.run_variables(name, definition)[0] if secret and read else dict.fromkeys(secret)
                for variable in sorted(secret & set(values)):
                    add(f"mcp:{name}:{variable}", f"the value of {variable} (MCP server {name})", values[variable])
        except Exception as exc:  # noqa: BLE001 - the start's own checks say what is wrong with a server; the guard
            # goes on with what it knows, and says that it does not know these
            notes.append(f"note: the secret guard does not know the variables of this start's MCP servers: "
                         f"{printable(str(exc))[:200]}")
    for entry in found if read else ():
        LOG.secret(entry["value"])
    a._secret_guard_values = {**cached, read: (found[:guard.VALUES_LIMIT], notes)}
    return a._secret_guard_values[read]


def secret_guard_allowed(a):
    """What this run's guard lets pass: --allow-send and what `sandbox-ai secret-guard allow` keeps for the project
    (the first workspace), each WHAT@HOST in its normal form. ValueError names an entry that is none."""
    guard = load_secret_guard_module()
    try:
        given = [guard.spec_text(spec) for spec in getattr(a, "allow_send", None) or []]
    except ValueError as exc:
        raise ValueError(f"--allow-send {exc}") from None
    kept = []
    if getattr(a, "workspaces", None) and guard.allow_file().exists():
        # In a tree the agent can write (a folder you chose holds XDG_CONFIG_HOME): read, as the remembered approvals
        # beside it are - you decide - with a warning that the agent could have allowed its own secrets there. A link
        # planted on the way is not followed: not read then.
        try:
            inside = check_host_source(a, guard.allow_file(), "The secret guard's allow-list (XDG_CONFIG_HOME)",
                                       warn=lambda _text: None)
        except ValueError as exc:
            warner()(f"{exc}; not read")
            return list(dict.fromkeys(given))
        if inside:
            warner()(f"{inside} (the agent could have allowed a secret's way out there for its next start)")
        kept = guard.allowed_for(Path(a.workspaces[0]).expanduser())
    return list(dict.fromkeys([*given, *kept]))


def secret_guard_config(a):
    """proxy.json's "secrets" (runtime.SecretGuard): the guard's decisions - the defaults, the security mode's and
    every ruleset's "secrets" section, the strictest of each (secret_guard.proxy_settings) -, the run's secrets in
    every form a request may carry them, the token shapes, the allow-list and the run's model providers."""
    return load_secret_guard_module().proxy_settings(
        secret_guard_values(a)[0], load_rulesets(a), secret_guard_allowed(a), provider_hosts(a),
        getattr(a, "mode_secrets", None), getattr(a, "mode", None))


def secret_guard_plan(a):
    """The dry run's "secret_guard": what the guard decides, which values it would know (their ids and labels, by the
    options alone: no secret is read for a dry run, never a value or a form of one), how many shapes (`sandbox-ai
    secret-guard` names them), and what is allowed; None without the guard."""
    if not secret_guard(a):
        return None
    config = load_secret_guard_module().proxy_settings(
        [], load_rulesets(a), secret_guard_allowed(a), provider_hosts(a), getattr(a, "mode_secrets", None),
        getattr(a, "mode", None))
    return {"on_send": config["on_send"], "on_shape": config["on_shape"], "by": config["by"],
            "values": secret_guard_values(a, read=False)[0], "shapes": len(config["shapes"]),
            "allow": config["allow"]}


def secret_guard_notes(a):
    """What the start says about the secret guard: a secret it cannot look for (too short), and where it sees
    nothing of a run that was handed secrets - a host whose TLS passes unopened (--no-inspect)."""
    if not secret_guard(a):
        return []
    values, notes = secret_guard_values(a)
    if values and a.no_inspect:
        notes = [*notes, f"note: the secret guard does not see requests to {', '.join(a.no_inspect)}: their TLS "
                         "passes unopened (--no-inspect)"]
    return notes


def no_inspect_notes(a):
    """What the start says of hosts whose TLS passes unopened (--no-inspect): the proxy holds such a connection to
    the name the program gives (SNI, proven by that host's certificate), but cannot see the request inside, and a
    front end that serves other sites too (a CDN) routes it by the Host the request names - any of its sites."""
    if not a.no_inspect or not proxy_enabled(a):
        return []
    return [f"note: --no-inspect {', '.join(a.no_inspect)}: TLS passes unopened - nothing the proxy checks (rules, the "
            "push check, its guards, canaries) sees what goes there, and a front end that serves other sites too (a CDN) can carry the "
            "connection on to any of them: only the name the program gives is checked"]


def package_guard_mount(a):
    """The folder with the copy of the known malicious packages, for the proxy's mount (PACKAGES_DIR), or None:
    no guard in this start, or no copy on this machine."""
    return load_packages_module().mount_dir() if package_guard(a) else None


def package_guard_notes(a):
    """What the start says about the package guard, where a package registry is open to this run: there is no copy
    of the known malicious packages (or an old one), or a registry's TLS passes unopened (--no-inspect), where the
    guard sees nothing."""
    registries = sorted(RUNTIME.PACKAGE_REGISTRIES)
    opened = set(allowed_hosts(a))
    reachable = a.egress_all_allow or any(host in opened for host in registries)
    if not package_guard(a) or not reachable:
        return []
    notes = []
    info = load_packages_module().status()
    if not info["present"]:
        notes.append("note: the package guard has no copy of the known malicious packages on this machine (it checks "
                     "release age and new names only): sandbox-ai packages sync")
    elif info["stale"] or info["problem"]:
        notes.append(f"note: the package guard's copy of the known malicious packages is "
                     + (f"damaged ({info['problem']})" if info["problem"] else f"{info['age_days']} days old")
                     + ": sandbox-ai packages sync")
    unopened = [host for host in registries if any(host_matches_any(host, value) for value in a.no_inspect)]
    if unopened:
        notes.append(f"note: the package guard does not see downloads from {', '.join(unopened)}: its TLS passes "
                     "unopened (--no-inspect)")
    return notes


def sandbox_identity(a):
    """Which sandbox this is, as its logs and questions name it: run, agent, backend, role (an agent or an
    MCP server's sandbox), and the pod, session or MCP server where there is one."""
    identity = {"run": getattr(a, "run_id", None), "agent": a.agent, "backend": a.backend,
                "role": "mcp-server" if a.mcp_socket_dir else "agent", "pod": a.pod_name, "session": a.session,
                "mcp": a.mcp_socket_dir.expanduser().parent.name if a.mcp_socket_dir else None,
                "name": getattr(a, "start_name", None)}
    return {key: value for key, value in identity.items() if value is not None}


def provider_hosts(a):
    """The agent's own provider: its model API and login hosts (--frontier adds the OpenAI and Anthropic ones,
    --llm its server), and those of the agents --with: they are providers of this run as much."""
    hosts = [host for agent in team(a) for host in PROVIDERS[agent]] + (FRONTIER if a.frontier else [])
    hosts += load_llm_module().pinned_names(a) if getattr(a, "llm", None) else []
    return list(dict.fromkeys(hosts))


def killswitch_config(a, hosts):
    """config.json's "killswitch" (runtime.cut_settings): what the supervisor needs to know of this run to cut its
    network during the run (sandbox-ai net cut). The model API as the start opened it: the addresses pinned for the
    provider's hosts (HOSTS and --llm's own pins) on 443 with their names, an --llm server by its addresses and port,
    or the host's port it listens on. An MCP server's or a probe's sandbox has no model API of its own. Behind Tor only
    the --vpn-bypass-tcp targets are the local network (everything else lies past Tor). None (said why): more host ports
    or API addresses than the kill switch tells apart (runtime.CUT_PORTS, CUT_API_LIMIT); the run starts without one."""
    pinned = {**hosts, **getattr(a, "llm_pins", {})}
    names = [] if a.mcp_socket_dir or a.probe else [name for name in provider_hosts(a) if name in pinned]
    https = sorted({str(ipaddress.ip_address(addr)) for name in names for addr in pinned[name]
                    if "/" not in str(addr)})
    tcp, loopback = [], []
    if getattr(a, "llm", None) and not (a.mcp_socket_dir or a.probe):
        parsed = load_llm_module().selection(a)[0]["parsed"]
        if parsed["loopback"]:
            loopback = [int(parsed["port"])]
        else:
            tcp = [[str(ipaddress.ip_address(addr)), int(parsed["port"])]
                   for addr in getattr(a, "llm_addresses", None) or []]
    tor = bool(getattr(a, "vpn_profile", None)) and a.vpn_profile.get("type") == "tor"
    bypass = tcp_targets(argparse.Namespace(allow_tcp=[str(v) for v in getattr(a, "vpn_bypass_tcp", None) or []])) \
        if tor else []
    # Tor's SocksPort (a profile made with --socks): the agent reaches the internet through it, past the proxy.
    socks = load_vpn_module().runtime().TOR_SOCKS_PORT if tor and (a.vpn_profile.get("tor") or {}).get("socks") \
        else None
    config = {"host_ports": host_ports(a), "api": {"https": https, "tcp": tcp, "loopback": loopback, "names": names},
              "tor": tor, "bypass": [[addr, port] for addr, port in bypass], "tor_socks": socks,
              "machine_host": machine_host() if on_machine(a) else None, "vpn_mark": getattr(a, "vpn_mark", None),
              # A start that lends its tunnel to its own MCP servers: the mark of those connections, which the cut
              # holds as the agent's own and whose TCP it ends with a reset (runtime.cut_rules).
              "lend_mark": getattr(a, "vpn_lend_mark", None)}
    try:
        RUNTIME.cut_settings(config)  # as the supervisor takes it: what it cannot tell apart, it is never handed
    except ValueError as exc:
        print(f"agent-sandbox: note: this run has no kill switch (sandbox-ai net cut): {printable(exc)}",
              file=sys.stderr)
        return None
    return config


def tool_rules_of(source):
    """SOURCE (a ruleset) has rules for the agent's own tools: a "tool" rule or default, or untrusted tool results."""
    return bool(source.get("tool") or source["defaults"].get("tool", "allow") != "allow"
                or any("agent" in entry for entry in source.get("untrusted", [])))


def hooks_active(a):
    """The agent's own tools have enforcing rules in this run (tool_rules_of a ruleset that is not an audit one):
    its hooks are installed. An audit ruleset only records: it never switches them on (nor refuses an agent without
    hooks), its tool rules are recorded when an enforcing one did (audit_tool_notes). Not in probes and MCP server
    sandboxes, where no agent runs."""
    return kits_apply(a) and any(source.get("mode") != "audit" and tool_rules_of(source)
                                 for source in load_rulesets(a))


def audit_tool_notes(a):
    """What the start says of audit rulesets whose tool rules record nothing in this run (no enforcing tool rules:
    the agent's tool hooks stay off)."""
    if not kits_apply(a) or hooks_active(a):
        return []
    return [f"note: the audit ruleset {source['name']}: its tool rules record nothing in this run - an audit ruleset "
            "never switches the agent's tool hooks on; they are on, and it is recorded, when an enforcing ruleset has "
            "tool rules (its HTTP and MCP rules are recorded as always)"
            for source in load_rulesets(a) if source.get("mode") == "audit" and tool_rules_of(source)]


def hooks_config(a):
    """config.json's "hooks" for the supervisor's answer to the hooks: the agent (its version is checked) and the
    rulesets' rules for its own tools and its tools whose results are untrusted. None without hooks."""
    if not hooks_active(a):
        return None
    return {"agent": a.agent, "rules": [
        {"name": source["name"], "version": 1, **audit_mode(source),
         "defaults": {"tool": source["defaults"]["tool"]}, "tool": source["tool"], "untrusted": [e for e in source.get("untrusted", []) if "agent" in e]}
        for source in load_rulesets(a) if tool_rules_of(source)]}


def trusted_kit_hooks(a):
    """The kits' hooks of this run that the user approved (stage_hooks in prepare_kits), in kit order."""
    return [staged for staged in getattr(a, "kit_hooks", []) if staged["trusted"] and not staged["errors"]]


def trusted_plugin_hooks(a):
    """The hooks of the run's Claude plugins that the user approved (stage_plugin_hooks in prepare_kits)."""
    return [staged for staged in getattr(a, "plugin_hooks", []) if staged["trusted"] and not staged["errors"]]


def plugin_hook_notes(a):
    """What the start says about the plugins' hooks while managed hooks are on (without them, Claude Code runs a
    plugin's hooks itself, as always): approved ones run, the others are named with the command that approves
    them."""
    if not managed_hooks(a):
        return []
    notes = []
    for item in getattr(a, "plugin_hooks", []):
        if item["errors"]:
            notes.append(f"{item['label']}: its hooks do not run: {item['errors'][0]}")
        elif not item["trusted"]:
            notes.append(f"{item['label']}: {len(item['hooks'])} hook(s) not approved: they do not run while the "
                         f"managed hooks are on; review and approve them with: sandbox-ai plugins claude trust-hooks "
                         f"{item['plugin']}")
        else:
            notes.append(f"{item['label']}: {len(item['hooks'])} approved hook(s)")
    return notes


# The agents whose hooks sandbox-ai writes into their own configuration of this start (agent_files, hermes_files):
# Qwen Code, Hermes, Pi, OpenCode 2, DSH, Letta Code, Goose. Only a start that runs the agent gets it (runs_agent).
OWN_CONFIG_HOOK_AGENTS = ("qwen", "hermes", "pi", "opencode2", "dsh", "letta", "goose")


def check_agent_hooks(a):
    """Refuse a start whose rules for the agent's own tools would not reach an agent of it: one of
    OWN_CONFIG_HOOK_AGENTS with --binary (a shell in its image gets none of its configuration, so an agent typed there
    would run without them), and an agent --with whose hooks sandbox-ai does not install (only Claude Code's,
    Codex's and OpenCode's are: companion_hook_mounts, hook_files)."""
    if not hooks_active(a):
        return
    if a.agent in OWN_CONFIG_HOOK_AGENTS and not runs_agent(a):
        raise ValueError(f"rules for the agent's own tools (a ruleset's \"tool\" rules, the mode paranoid's) hold for "
                         f"{AGENT_TITLES[a.agent]} through the configuration of a start of it, which --binary does "
                         "not get (an agent started from that program would run without them); start it without "
                         "--binary, or without such rules")
    for agent in companions(a):
        if agent in MANAGED_DIRS or agent == "opencode":
            continue  # their rule hooks beside the lead: hook_files, companion_hook_mounts
        if agent in OWN_CONFIG_HOOK_AGENTS and not runs_agent(a):
            raise ValueError(f"rules for the agent's own tools (a ruleset's \"tool\" rules, the mode paranoid's) hold "
                             f"for {AGENT_TITLES.get(agent, agent)} --with through its configuration, which a start "
                             "with --binary does not get; start without --binary, or without such rules")
        if agent not in OWN_CONFIG_HOOK_AGENTS:  # (OpenHands, nanobot: no layer a start can install hooks in)
            raise ValueError(f"rules for the agent's own tools (a ruleset's \"tool\" rules) cannot be enforced for "
                             f"{AGENT_TITLES.get(agent, agent)}, not --with {agent} either; start without such rules, "
                             f"or give it as a helper (--helper {agent}) in a start without them")


def managed_hooks(a):
    """The agent gets managed hooks this run: the rule hooks (hooks_active), approved kit hooks, or both - and Codex
    whenever a plugin of its kits brings hooks: they run only once approved (codex_plugins), so only managed hooks
    run (allow_managed_hooks_only)."""
    return hooks_active(a) or bool(trusted_kit_hooks(a)) or codex_plugin_hooks(a)


def codex_plugin_hooks(a):
    """A plugin the kits give Codex this run brings hooks (approved or not)."""
    return a.agent == "codex" and bool(getattr(a, "codex_plugins", None)) and bool(getattr(a, "plugin_hooks", None))


def hook_groups(a):
    """{EVENT: [group]} of the agent's managed hooks for Claude Code and Codex: the rule hooks first (every tool,
    before and after it runs), then each approved kit's in kit order, then the approved hooks of the run's Claude
    plugins."""
    groups = {}
    if hooks_active(a):
        groups.update(rule_groups(a.agent))
    for staged in [*trusted_kit_hooks(a), *trusted_plugin_hooks(a)]:  # the kits', then the plugins'
        for event, entries in staged["groups"].items():
            groups.setdefault(event, []).extend(
                [{**entry, "hooks": [kit_handler(a, handler) for handler in entry["hooks"]]} for entry in entries]
                if event == "PreToolUse" and hooks_active(a) else entries)
    return groups


def managed_hook_groups(a):
    """hook_groups for an agent whose hooks go into its own read-only configuration (Qwen Code's system settings,
    Hermes' managed configuration, Letta Code's start): None without managed hooks."""
    return hook_groups(a) if managed_hooks(a) else None


def module_hooks(a):
    """The run's hooks for an agent whose sandbox-ai plugin or extension loads them (Pi, OpenCode 2): {"rules": the
    rules for its own tools hold, "modules": the approved kits' modules (their paths in the sandbox, in kit order)},
    None without managed hooks."""
    if not managed_hooks(a):
        return None
    return {"rules": hooks_active(a), "modules": [f"{staged['target']}/{name}" for staged in trusted_kit_hooks(a)
                                                  for name in staged["plugins"]]}


def kit_handler(a, handler):
    """HANDLER of a kit's PreToolUse hook while rules hold, run through the wrapper (KIT_HOOK_COMMAND): a tool
    input it rewrites (updatedInput) is checked by the rules too, not only the agent's own. The wrapper keeps the
    hook's timeout; the agent waits that long plus the rules' answer."""
    timeout = handler.get("timeout", KIT_HOOK_TIMEOUT)
    return {**handler, "timeout": timeout + HOOK_AGENT_TIMEOUT,
            "command": KIT_HOOK_COMMAND.format(agent=a.agent, timeout=timeout, command=shlex.quote(handler["command"]))}


def opencode_recheck(a):
    """The rule plugin runs a second time, after the approved kits' plugins: rules hold and kit plugins run."""
    return hooks_active(a) and any(staged["plugins"] for staged in trusted_kit_hooks(a))


def rule_groups(agent):
    """{EVENT: [group]} of AGENT's rule hooks alone: every tool, before and after it runs."""
    return {event: [{"matcher": "*", "hooks": [{"type": "command", "timeout": HOOK_AGENT_TIMEOUT,
                                                "command": HOOK_COMMAND.format(agent=agent, event=event)}]}]
            for event in ("PreToolUse", "PostToolUse")}


def hook_files(a):
    """{path in the session directory: content} of the agent's hooks, as its managed configuration: Claude Code's
    managed-settings.json (allowManagedHooksOnly), Codex's requirements.toml (allow_managed_hooks_only, the hooks
    feature pinned on), both in a directory mounted read-only at MANAGED_DIRS; OpenCode's plugin, which
    OPENCODE_CONFIG_CONTENT names (with the approved kits' plugins after it). The rule hooks come first, then the
    approved kit hooks (hook_groups); nothing else runs hooks. The agents --with get the rule hooks the same way
    (the kits are the lead's)."""
    files = agent_hook_files(a, a.agent, hook_groups(a))
    if hooks_active(a):  # (the other agents --with: in their own configuration, companion_files)
        for agent in companions(a):
            if agent in MANAGED_DIRS or agent == "opencode":
                files.update(agent_hook_files(a, agent, rule_groups(agent), companion=True))
    return files


def toml_string(value):
    """VALUE as a TOML basic string. JSON's form is one, but not with ensure_ascii: it writes a character beyond
    the BMP (an emoji) as a surrogate pair, which TOML refuses, and Codex would then ignore its managed hooks."""
    text = str(value)
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError(f"a hook holds an unpaired surrogate: {printable(text)[:80]}")
    return json.dumps(text, ensure_ascii=False)


def agent_hook_files(a, agent, groups, companion=False):
    """hook_files for one AGENT of the run with the hook GROUPS (Claude Code, Codex) it gets."""
    if agent == "claude":
        settings = {"allowManagedHooksOnly": True, "hooks": groups}
        return {"claude-code/managed-settings.json": json.dumps(settings, indent=2) + "\n"}
    if agent == "codex":
        lines = ["# sandbox-ai: the agent's hooks (README, Rulesets: \"The agent's own tools\"; Kits: hooks)"]
        if companion or groups or managed_hooks(a):  # not for a run that has only the kits' plugins' requirements
            lines += ["allow_managed_hooks_only = true", "", "[features]", "hooks = true"]
        for event, entries in groups.items():
            for entry in entries:
                lines += ["", f"[[hooks.{event}]]", f"matcher = {toml_string(entry.get('matcher', '*'))}"]
                for handler in entry["hooks"]:
                    lines += ["", f"[[hooks.{event}.hooks]]", 'type = "command"',
                              f"command = {toml_string(handler['command'])}"]
                    lines += [f"timeout = {handler['timeout']}"] if "timeout" in handler else []
        if not companion:
            lines += codex_plugin_requirements(a)
        return {"codex/requirements.toml": "\n".join(lines) + "\n"}
    if agent != "opencode":  # the newer agents' hooks are part of their own configuration (agent_files)
        return {}
    files = {PurePosixPath(OPENCODE_HOOK_PLUGIN).name: OPENCODE_HOOKS_JS} if hooks_active(a) else {}
    if opencode_recheck(a) and not companion:
        files[PurePosixPath(OPENCODE_HOOK_RECHECK).name] = OPENCODE_HOOKS_JS.replace(
            "const RECHECK = false;", "const RECHECK = true;", 1)
    return files


def codex_plugin_requirements(a):
    """Lines of Codex's requirements.toml for the kits' plugins of this run (a.codex_plugins): marketplace sources
    restricted to the empty folders its configuration names for them - so Codex fetches no other marketplace, not
    even OpenAI's curated one (codex-rs/core-plugins manager.rs: maybe_start_curated_repo_sync_for_config), and loads
    no plugin of another - and no MCP server of any plugin (an empty list of allowed ones for each: they would run
    beside sandbox-ai's MCP gateway)."""
    given = getattr(a, "codex_plugins", None) or []
    if not given:
        return []
    lines = ["", "# sandbox-ai: the kits' plugins (README, Kits: plugins for Codex)", "[marketplaces]",
             "restrict_to_allowed_sources = true"]
    for name in dict.fromkeys(item["marketplace"] for item in given):
        lines += ["", f"[marketplaces.allowed_sources.{toml_string(name)}]", 'source = "local"',
                  f"path = {toml_string(codex_plugin_marketplace(name))}"]
    for item in given:
        lines += ["", f"[plugins.{toml_string(item['id'])}.mcp_servers]"]
    return lines


def codex_plugin_config(a):
    """{table: [(key, TOML)]} for Codex's managed_config.toml (the layer above -c and every config.toml): the kits'
    plugins on, each marketplace configured as an empty local folder (codex_plugin_marketplace), no plugins of the
    ChatGPT account (features.remote_plugin: they would be downloaded at run time, unpinned)."""
    given = (getattr(a, "codex_plugins", None) or []) if a.agent == "codex" else []
    if not given:
        return {}
    tables = {"features": [("remote_plugin", "false")]}
    for name in dict.fromkeys(item["marketplace"] for item in given):
        tables[f"marketplaces.{toml_string(name)}"] = [("source_type", '"local"'),
                                                       ("source", toml_string(codex_plugin_marketplace(name)))]
    for item in given:
        tables[f"plugins.{toml_string(item['id'])}"] = [("enabled", "true")]
    return tables


def hook_mount(a, session):
    """(directory in SESSION, target) of the agent's managed configuration with its hooks (Claude Code's: or the
    network note), mounted read-only; None without hooks, and for OpenCode (its plugin lies in the session directory,
    mounted anyway)."""
    if hermes_managed(a):  # Hermes' managed configuration, with or without hooks (hermes.py)
        hermes = load_hermes_module()
        return Path(session) / hermes.SESSION_DIR, hermes.MANAGED_DIR
    if agent_managed(a):  # DSH's patch, Qwen Code's system settings, Goose's, OpenHands', nanobot's, Letta Code's start, Pi's extension
        module = agent_module(a.agent)
        return Path(session) / module.SESSION_DIR, module.MANAGED_DIR
    # Claude Code's: its hooks, and the network note as its managed CLAUDE.md (note_files).
    if a.agent not in MANAGED_DIRS or not (managed_hooks(a) or a.agent == "claude" and start_note(a)
                                           or a.agent == "codex" and getattr(a, "codex_plugins", None)):
        return None
    return Path(session) / PurePosixPath(MANAGED_DIRS[a.agent]).name, MANAGED_DIRS[a.agent]


def companion_hook_mounts(a, session):
    """[(directory in SESSION, target)]: the managed configuration of each agent --with (Claude Code, Codex), with
    its rule hooks, mounted read-only like the lead's (hook_mount)."""
    if not hooks_active(a):
        return []
    return [(Path(session) / PurePosixPath(MANAGED_DIRS[agent]).name, MANAGED_DIRS[agent])
            for agent in companions(a) if agent in MANAGED_DIRS]


def mcp_relay_config(a, name):
    """What the supervisor's relay of MCP server NAME checks for this run: the rules that hold only in tainted
    runs and the tools whose results are untrusted (the server's own gateway has the rest)."""
    sources = []
    for source in load_rulesets(a):
        rules = [rule for rule in source["mcp"] if rule.get("when") and ("*" in rule["server"] or name in rule["server"])]
        untrusted = [entry for entry in source.get("untrusted", []) if "server" in entry
                     and ("*" in entry["server"] or name in entry["server"])]
        if rules or untrusted:
            sources.append({"name": source["name"], "version": 1, **audit_mode(source), "mcp": rules, "untrusted": untrusted})
    return {"server": name, "rules": sources, **({"network": True} if mcp_reaches_on_its_own(a, name) else {}),
            **({"tools_pin": mcp_tools_pin(a, name)} if kits_apply(a) and a.workspaces else {})}


def mcp_tools_pin(a, name):
    """The tool definitions of MCP server NAME pinned for this run's project (mcp_pins.py: {"pins": {tool: digest}
    or None, "definitions", "lint": the hidden-text checker's patterns, lint.pattern_data}): the run's relay asks
    about a change. An unreadable store: none (asked as a first list), never a failed start."""
    lint = load_lint_module().pattern_data()
    try:
        return load_mcp_pins_module().relay_pin(Path(a.workspaces[0]).expanduser().resolve(), name, lint)
    except (OSError, ValueError) as exc:
        LOG.info("mcp", f"the pinned tool definitions of {printable(name)} could not be read: {printable(exc)}")
        return {"pins": None, "definitions": {}, "lint": lint}


_mcp_pins_module = None


def load_mcp_pins_module():
    """mcp_pins.py: the MCP tool definitions pinned per project."""
    global _mcp_pins_module
    if _mcp_pins_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_mcp_pins", HERE / "mcp_pins.py")
        with LOADING:
            if _mcp_pins_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _mcp_pins_module = loaded
    return _mcp_pins_module


_mcp_budget_module = None


def load_mcp_budget_module():
    """mcp_budget.py: the MCP tools a start's model would see, against the budget of its class."""
    global _mcp_budget_module
    if _mcp_budget_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_mcp_budget", HERE / "mcp_budget.py")
        with LOADING:
            if _mcp_budget_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _mcp_budget_module = loaded
    return _mcp_budget_module


def mcp_tool_budget(a):
    """The MCP tools the model of an --llm start would see, against the budget of the model's class
    (mcp_budget.start_report: the count from each server's pin for the project, else its catalog's list; the
    budget; the servers to leave out), with "endpoint" (--llm's server); None without --llm, for a model of no class
    with nothing to count, or when the endpoint, the profiles or the definitions cannot be read - a warning at most,
    never a failed start."""
    if not getattr(a, "llm", None) or getattr(a, "mcp_socket_dir", None) or getattr(a, "probe", False):
        return None
    try:
        llm = load_llm_module()
        found, model = llm.selection(a)
        servers = {**load_mcp_module().definitions(getattr(a, "project_document", None)),
                   **(getattr(a, "helper_definitions", None) or {})} if a.mcp else {}
        project = a.workspaces[0] if getattr(a, "workspaces", None) else None
        report = load_mcp_budget_module().start_report(model, list(dict.fromkeys(a.mcp or [])), project, servers,
                                                       llm.profile_table())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.info("mcp", f"the MCP tools of this start are not counted: {printable(exc)}")
        return None
    return {**report, "endpoint": found["name"]} if report is not None else None


def pin_mcp_tools_after_run(a, say=None):
    """After the run: the first tool lists its relay saw of servers without a pin, pinned for the project
    (mcp_pins.pin_after_run); each said. Never fails the end of the run."""
    say = say or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    control = getattr(a, "control_dir", None)
    if not control or not a.workspaces or not kits_apply(a):
        return []
    try:
        notes = load_mcp_pins_module().pin_after_run(Path(a.workspaces[0]).expanduser().resolve(), control)
    except Exception as exc:  # noqa: BLE001 - the run's end goes on; the next run asks as for a first list
        say(f"the MCP tool definitions this run saw were not pinned: {printable(str(exc))[:300]}")
        return []
    for note in notes:
        say(printable(note))
    return notes


def mcp_reaches_on_its_own(a, name):
    """MCP server NAME reaches the network from a container of its own that its bridge starts on the host (an image
    server with network, behind a VPN or not): no sandbox and supervisor of its own that the run's kill switch (net
    cut) reaches, so the run's relay holds the cut for it (runtime.McpGateway.cut_refusal). A command server or a
    helper has its own (net.related_runs cuts it). A definition that cannot be read counts as one."""
    servers = getattr(a, "relay_definitions", None)  # read once per start (the relays of all its servers)
    if servers is None:
        try:
            servers = {**load_mcp_module().definitions(getattr(a, "project_document", None)),
                       **(getattr(a, "helper_definitions", None) or {})}
        except (OSError, ValueError):
            return True
        a.relay_definitions = servers
    definition = servers.get(name)
    if not isinstance(definition, dict) or not ("image" in definition or "rootfs" in definition):
        return False
    return definition.get("network", "none") != "none"


def proxy_rules(a, hosts):
    """proxy.json's "rules" (the rulesets with HTTP rules, --allow-http aside: it comes as http_rules) and
    "pinned" (the run's host addresses, so that a host's rules hold for its bare address too)."""
    rules = [{"name": source["name"], "version": 1, **audit_mode(source),
              "defaults": {"http": source["defaults"]["http"]}, "http": source["http"], "untrusted": [entry for entry in source.get("untrusted", []) if "host" in entry]}
             for source in load_rulesets(a)
             if source["name"] != "--allow-http" and (source["http"] or source["defaults"]["http"] != "allow"
                                                       or any("host" in entry for entry in source.get("untrusted", [])))]
    # The addresses always, also without rules now: rules reloaded during the run hold for bare addresses too.
    return {"rules": rules, "pinned": {name: list(values) for name, values in hosts.items()}}


def write_proxy_files(a, directory, hosts, log_name):
    """The proxy's private directory (mounted at PROXY_DIR, hidden from the agent): proxy.json, and for the
    push check push.json (the full rules, your own included) and privacy.py."""
    config = proxy_config(a, hosts, log_name)
    files = {"proxy.json": json.dumps(config)}
    if config["push_check"]:
        privacy = load_privacy_module()
        context = privacy_context(a)
        derived = privacy.derived_rules(context)
        workspaces = []
        try:
            for src, dest in workspace_targets(a):
                # Each workspace with its own project's rules (read-only ones too: their commits can be pushed).
                project = Path(src).expanduser().resolve().name
                rules = privacy.builtin_rules().add(derived).add(privacy.user_rules(privacy_rules_dir(a), project))
                workspaces.append({"path": dest, "rules": privacy.rules_document(rules)})
        except privacy.RulesUnavailable as exc:
            raise ValueError(f"--privacy-rules: {exc}") from None
        # `sandbox-ai workspace add` writes push-added.json beside it: these rules plus the new project's own.
        a.push_check_dir = directory
        canaries = getattr(a, "canary_plan", None)
        files["push.json"] = json.dumps({"mode": a.privacy_check, "own_hosts": privacy.private_hosts(context),
                                         "workspaces": workspaces,
                                         "derived": privacy.rules_document(privacy.Rules().add(derived)),
                                         **({"canaries": canaries.push_entry(),
                                             "canary_block": canaries.decisions["on_send"] != "report"}
                                            if canaries else {})})
        files["privacy.py"] = (HERE / "privacy.py").read_text()
    for name, content in files.items():
        (directory / name).write_text(content)
        (directory / name).chmod(0o600)
    return config


def check_traffic_limit(value, origin):
    """VALUE (MiB of one record of the LLM traffic log) as an int in LLM_TRAFFIC_LIMIT_MIB, else ValueError."""
    low, high = LLM_TRAFFIC_LIMIT_MIB
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{origin}: a number of MiB from {low} to {high}")
    return value


def budget_usd(value, origin="--budget"):
    """--budget USD (also "budget" in the defaults and the project file): a number of USD from 0 to 1000000."""
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError(f"{origin}: a number of USD")
    try:
        number = float(str(value).strip().removeprefix("$"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{origin}: {value!r} is no number of USD") from None
    if not 0 <= number <= 1_000_000:
        raise argparse.ArgumentTypeError(f"{origin}: from 0 to 1000000 USD")
    return round(number, 6)


def check_budget(a):
    """--budget (also the project file's "budget") needs the connection proxy: it prices each model request and
    refuses the one past the budget - with --no-proxy nothing would hold it. (--offline: nothing is spent.)"""
    if getattr(a, "budget", None) is not None and a.no_proxy and not a.offline:
        raise ValueError("--budget needs the connection proxy (it prices every model request and refuses the one past "
                         "the budget); drop --no-proxy")


def run_budget(a):
    """(USD or None, where it came from) of this run's --budget: the option (or the project file's "budget"), else your
    defaults' "budget"."""
    if getattr(a, "budget", None) is not None:
        return a.budget, "--budget"
    if getattr(a, "budget_default", None) is not None:
        return a.budget_default, str(defaults_file())
    return None, None


_spend_module = None


_session_login_module = None


def load_session_login_module():
    """session_login.py (a session's login: listed, imported, copied into a start, written back), loaded once and
    given this launcher."""
    global _session_login_module
    if _session_login_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_session_login", HERE / "session_login.py")
        with LOADING:
            if _session_login_module is None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module._modules["launcher"] = RunningLauncher()
                _session_login_module = module
    return _session_login_module


def load_spend_module():
    """spend.py (prices, budgets, the spend logs), loaded once."""
    global _spend_module
    if _spend_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_spend", HERE / "spend.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._modules["runtime"] = RUNTIME
        _spend_module = module
    return _spend_module


def spend_config(a):
    """proxy.json's "spend": the prices and the budgets of this run (spend.proxy_config); your damaged spend.json
    stops the start (its limits could not hold)."""
    spend = load_spend_module()
    workspaces = getattr(a, "workspaces", None) or [os.getcwd()]
    workspace = Path(getattr(a, "workspace", None) or workspaces[0])
    try:
        config = spend.proxy_config(getattr(a, "run_id", None), default_log_dir(workspace).name, str(workspace),
                                    a.agent, load_activity_module().session_name(a), getattr(a, "start_name", None),
                                    run_budget(a)[0])
    except ValueError as exc:
        raise ValueError(f"{exc} (sandbox-ai spend: fix or remove it - its limits must hold for this start)") from None
    a.spend_budgets = config["budgets"]  # (start_spend_watch)
    return config


def spend_plan(a):
    """The dry run's "spend": the run's budget, where it came from, your limits."""
    budget, origin = run_budget(a)
    try:
        limits = [load_spend_module().limit_label(entry) for entry in load_spend_module().limits()]
    except ValueError as exc:
        limits = [f"unreadable: {exc}"]
    return {"budget": budget, "origin": origin, "limits": limits, "counted": proxy_enabled(a),
            "file": "spend-TIME-RUN.jsonl" if proxy_enabled(a) else None}


def start_spend_watch(a):
    """The budget warnings of this run's proxy to you (desktop, phone: notify's "budget") and, for a day's or month's
    limit, what other runs spend of it to the proxy; None without a spend log or a budget."""
    name = getattr(a, "spend_log_name", None)
    budgets = getattr(a, "spend_budgets", None)
    if not name or not budgets or not a.log_dir:
        return None
    spend = load_spend_module()
    run = getattr(a, "start_name", None) or getattr(a, "run_id", None)

    def notify(title, text, record):
        approvals = load_approvals_module()
        if hasattr(approvals, "phone"):
            approvals.phone("budget", text, run=run)
        approvals.desktop_notification(title, text)

    def tell(others):
        if getattr(a, "control_dir", None):
            load_starts_module().module("net").ask(a.control_dir, {"op": "spend", "others": others})
    return spend.SpendWatch(Path(a.log_dir).expanduser() / name, getattr(a, "run_id", None), budgets, notify,
                            tell).start()


def check_log_days(value, origin):
    """VALUE (log_days in your defaults: the logs older than that many days are removed by sandbox-ai gc and its daily
    pass, statedirs.old_logs; 0: never) as an int from 0 to 36500, else ValueError."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 36500:
        raise ValueError(f"{origin}: log_days is a number of days (1 or more): the logs older than that are removed by "
                         "sandbox-ai gc and its daily pass; 0: never (without it: 30 days)")
    return value


def llm_traffic_mode(a):
    """off, llm or all: --llm-traffic, else "llm_traffic" in your defaults file, else llm (the --llm server's
    requests); off without the connection proxy, which is what sees them."""
    if not proxy_enabled(a):
        return "off"
    chosen = getattr(a, "llm_traffic", None) or getattr(a, "llm_traffic_default", None) or "llm"
    return chosen if chosen in RUNTIME.LLM_TRAFFIC_MODES else "llm"


def llm_traffic_config(a):
    """proxy.json's "traffic": {"mode", "record_limit" (bytes), "run", "session"}; None when nothing is recorded
    ("llm" without --llm records nothing either: no request goes to such a server)."""
    mode = llm_traffic_mode(a)
    if mode == "off" or (mode == "llm" and not a.llm):
        return None
    limit = getattr(a, "llm_traffic_limit", None) or getattr(a, "llm_traffic_limit_default", None) or \
        RUNTIME.LLM_TRAFFIC_RECORD >> 20
    return {"mode": mode, "record_limit": check_traffic_limit(limit, "--llm-traffic-limit") << 20,
            "run": getattr(a, "run_id", None), "session": load_activity_module().session_name(a)}


def llm_traffic_plan(a):
    """The dry run's "llm_traffic": what is recorded, where it came from, the limit of one record."""
    config = llm_traffic_config(a)
    origin = "--llm-traffic" if getattr(a, "llm_traffic", None) else \
        str(defaults_file()) if getattr(a, "llm_traffic_default", None) else "default"
    return {"mode": llm_traffic_mode(a), "origin": origin, "recorded": bool(config),
            "record_limit_mib": config["record_limit"] >> 20 if config else None,
            "file": "llm-traffic-TIME-RUN.jsonl" if config else None}


# The run a log of the proxy belongs to (connections-TIME-RUN.jsonl, llm-traffic-TIME-RUN.jsonl, their parts).
RUN_LOG = re.compile(r"(?:connections|llm-traffic)-\d{8}-\d{6}-(.+?)\.jsonl(?:\.\d{1,3})?(?:\.gz)?")


def live_run_log(path, live):
    """True when PATH is a log (or an older part of one) of a run in LIVE (the ids of the runs that run): its own in-run
    limit bounds it (runtime.rotate_held_log, LlmTrafficFile); the per-folder caps never count or delete it - they
    deleted the parts of a run that started earlier, even the log its proxy still wrote, which then grew unseen."""
    match = RUN_LOG.fullmatch(Path(path).name)
    return bool(live) and match is not None and match.group(1) in live


def rotate_traffic_logs(directory, limit=LLM_TRAFFIC_LOGS_LIMIT, live=None):
    """Delete the oldest runs' llm-traffic-*.jsonl (with their rotated parts, and those sandbox-ai gc compressed:
    NAME.gz) while all of them hold more than LIMIT bytes; never those of a run in LIVE (the ids of the runs that run:
    live_run_log), and one written within the hour stays (a sandbox that still runs). The names deleted."""
    def part(name):
        number = name[:-len(".gz")].rpartition(".")[2] if name.endswith(".gz") else name.rpartition(".")[2]
        return -int(number) if number.isdigit() else 0
    try:
        logs = sorted((p for p in Path(directory).glob("llm-traffic-*.jsonl*")
                       if re.fullmatch(r"llm-traffic-.+\.jsonl(?:\.\d+)?(?:\.gz)?", p.name) and p.is_file()
                       and not p.is_symlink() and not live_run_log(p, live)),
                      key=lambda p: (p.name.split(".jsonl")[0], part(p.name)))
        sizes = {p: p.stat() for p in logs}
    except OSError:
        return []
    total, removed = sum(info.st_size for info in sizes.values()), []
    recent = time.time() - 3600
    for oldest in logs:
        if total <= limit:
            break
        if sizes[oldest].st_mtime > recent:
            continue
        with contextlib.suppress(OSError):
            oldest.unlink()
            total -= sizes[oldest].st_size
            removed.append(oldest.name)
    return removed


def rotate_connection_logs(directory, limit=CONNECTION_LOGS_LIMIT, live=None):
    """Delete the oldest connections-*.jsonl (with the older parts a run rotated, NAME.N.gz, and those sandbox-ai gc
    compressed, NAME.gz) while they hold more than LIMIT bytes - never those of a run in LIVE (the ids of the runs that
    run: live_run_log), nor one written within the hour; the names deleted."""
    def order(path):  # by run (its start time first), a run's oldest part first
        match = re.fullmatch(r"(connections-.+?\.jsonl)(?:\.(\d{1,3}))?(?:\.gz)?", path.name)
        return (match.group(1), -int(match.group(2) or 0)) if match else (path.name, 0)
    try:
        logs = sorted((p for p in Path(directory).glob("connections-*.jsonl*")
                       if re.fullmatch(r"connections-.+\.jsonl(?:\.\d{1,3})?(?:\.gz)?", p.name) and p.is_file()
                       and not p.is_symlink() and not live_run_log(p, live)),
                      key=order)
        sizes = {p: p.stat() for p in logs}
    except OSError:
        return []
    total, removed = sum(info.st_size for info in sizes.values()), []
    recent = time.time() - 3600  # written within the hour: possibly a sandbox that still runs
    for oldest in logs:
        if total <= limit:
            break
        if sizes[oldest].st_mtime > recent:
            continue
        with contextlib.suppress(OSError):  # (gone meanwhile: another gc)
            oldest.unlink()
            total -= sizes[oldest].st_size
            removed.append(oldest.name)
    return removed


def live_runs():
    """The ids of the runs registered as running (starts.registered_runs: one whose state cannot be told counts as
    running), for the per-folder caps of the logs; None when they cannot be told (the caps then go by the hour rule
    alone, as before)."""
    try:
        return set(load_starts_module().registered_runs())
    except Exception as exc:  # noqa: BLE001 - a convenience of the start: never fatal
        LOG.warning("launcher", "which runs still run could not be told for the caps of the logs", exc)
        return None


def daily_compaction(a):
    """After a start that logs (not a dry run or probe): once a day the logs of runs that ended are compressed and large
    shared logs split, in a process of its own that the start never waits for (statedirs.auto: sandbox-ai gc's
    compaction, bounded in time; SANDBOX_AI_AUTO_COMPACT=0 switches it off). True if one was started; never fails a
    start."""
    if getattr(a, "dry_run", False) or getattr(a, "probe", False) or not getattr(a, "log_dir", None):
        return False
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_statedirs_for_launcher", HERE / "statedirs.py")
        statedirs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(statedirs)
        return statedirs.auto()
    except Exception as exc:  # noqa: BLE001 - a convenience: the start goes on
        LOG.warning("launcher", "the daily compaction of the logs was not started", exc)
        return False


def llm_traffic_notes(a):
    """What the start says of the LLM traffic log: only when it records the cloud providers' conversations too."""
    config = llm_traffic_config(a)
    if not config or config["mode"] != "all":
        return []
    return [f"the LLM traffic log records the model requests to your cloud provider too (--llm-traffic all): the "
            f"whole conversation, credentials redacted, up to {config['record_limit'] >> 20} MiB a request, in "
            f"llm-traffic-*-{a.run_id}.jsonl in "
            f"{printable(str(Path(a.log_dir).expanduser())) if a.log_dir else 'the log directory'} "
            "(sandbox-ai llm traffic)"]


def connection_summary(path):
    """Lines about one run's connection log: how much was logged, refused pushes, TLS that failed."""
    counts, refused, tls = {"http": 0, "other": 0, "denied": 0}, [], {}
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(record, dict):  # a damaged line: the summary at the run's end goes on
                    continue
                event = record.get("event")
                if event == "http":
                    counts["http"] += 1
                    push = record.get("push")
                    if isinstance(push, dict) and not push.get("ok"):
                        refused.append(", ".join(push.get("refs") or []) or record.get("url", "?"))
                elif event == "denied":
                    counts["denied"] += 1
                elif event == "tls-refused" and record.get("abandoned"):
                    counts["other"] += 1  # the program went away mid-handshake: it refused nothing
                elif event in ("tls-refused", "tls-upstream-failed"):
                    tls[str(record.get("host") or record.get("destination"))] = (event, record.get("error"))
                else:
                    counts["other"] += 1
    except OSError:
        return []
    lines = [f"proxy: {counts['http']} HTTP request(s), {counts['other']} other connection(s), "
             f"{counts['denied']} refused; log: {path}"]
    if refused:
        lines.append(f"proxy: refused {len(refused)} push(es) the privacy check stopped: {'; '.join(refused[:5])}")
    for host, (event, error) in sorted(tls.items())[:10]:
        lines.append(f"proxy: TLS to {host} failed ({tls_failure_text(event, error)}); "
                     f"--no-inspect {host} passes it through unopened")
    return lines


def tls_failure_text(event, error):
    """Why a TLS connection the proxy opened failed, from its record (EVENT, ERROR: the proxy's error text): the
    program refused the session's CA, the server's certificate was not trusted (and why: an issuer missing from the
    chain the server sent, self-signed, expired, another name), or the connection failed before any certificate (a
    reset, a timeout) - not "the server certificate is not trusted" for every one of them."""
    if event == "tls-refused":
        return "the program rejected the session CA"
    text = printable(str(error or "")).strip()
    reason = re.sub(r"^\[SSL: [A-Z_]+\] |\s*\(_ssl\.c:\d+\)$", "", text)[:200]
    if not text or "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return "the server certificate is not trusted" + (f": {reason.replace('certificate verify failed: ', '')}"
                                                          if reason else "")
    return f"the connection to the server failed before its certificate was checked: {reason}"


def launcher_script():
    """The file that runs this launcher as a process of its own (python3 -I SCRIPT AGENT ...): launcher/agent_sandbox.py,
    which runs this file from its cached bytecode under the same name (a script Python runs itself is compiled anew:
    55 ms of every start), this file in a copy without it (main.py's executed_script)."""
    stub = HERE / "launcher" / "agent_sandbox.py"
    return stub if stub.is_file() else HERE / "agent_sandbox.py"


def runs_dir():
    """Where running sandboxes register (for sandbox-ai net)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "sandbox-ai" if runtime and Path(runtime).is_dir() else \
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"
    return base / "runs"


def app_tab():
    """The desktop app's terminal tab this process runs in (SANDBOX_AI_APP_TAB, as sessions.launch checks it), or
    None."""
    tab = os.environ.get("SANDBOX_AI_APP_TAB", "")
    return tab if re.fullmatch(r"[A-Za-z0-9-]{8,64}", tab) else None


def register_run(a, run, session):
    """Directory of this run in runs_dir(): run.json for `sandbox-ai net`, and the supervisor's control
    socket (the directory is mounted at CONTROL_DIR, hidden from the agent)."""
    directory = runs_dir() / run
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        # Left by a run that was killed (sessions and MCP servers keep their name): reuse it unless that
        # run is still alive.
        try:
            earlier = json.loads((directory / "run.json").read_text())
            pid = int(earlier.get("pid"))
            os.kill(pid, 0)
            # A PID given out again (another start time) is no earlier run of this name that still lives.
            recorded, now = earlier.get("pid_start"), process_start(pid)
            alive = pid != os.getpid() and not (isinstance(recorded, int) and now is not None and now != recorded)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            alive = False
        if alive:
            raise ValueError(f"A sandbox named {run} is already running (see sandbox-ai net list)") from None
        shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
    # Held as long as this launcher runs: the sandbox's own supervisor cuts its network once it is free (or gone).
    a.supervisor_lock = hold_supervisor_lock(directory)
    a.runtime_mode = MODES.runtime_record(launcher_namespace(), a)
    (directory / "run.json").write_text(json.dumps({
        "run": run, "agent": a.agent, "backend": a.backend, "workspace": a.workspaces[0], "pid": os.getpid(),
        # Remote folders (sshfs://) mounted on this host for the run: their sources by mount point (workspace list,
        # the app and the web show the source); `workspace add sshfs://...` adds its own to remotefs' record.
        "remote": [{key: entry.get(key) for key in ("source", "point", "mode", "sandbox")}
                   for entry in getattr(a, "remote_mounts", None) or []],
        # The sandbox-ai that runs it: the app tells a run started before a change of behaviour (one prompt per
        # question since 0.1.86) by it.
        "version": __version__,
        # The PID's start time: `sandbox-ai net stop` signals this process only, never a later one of that PID.
        "pid_start": process_start(os.getpid()),
        # Its PID namespace: only a process in the same one may conclude from a missing PID that this run ended
        # (liveness.py; from inside a sandbox the host's PIDs are not visible).
        "pid_ns": os.readlink("/proc/self/ns/pid") if os.path.exists("/proc/self/ns/pid") else None,
        "session": a.session or (a.pod_name if a.tmux_socket_dir else None), "session_dir": str(session),
        # The proxy's temporary folder: removed with the run's directory when its launcher was killed (liveness.discard).
        "proxy_dir": str(a.proxy_dir) if getattr(a, "proxy_dir", None) else None,
        # sandbox-ai list, rename and the other commands find a run by its name; "start" links it to its
        # start directory (sessions.py), "pod" is what pause and resume freeze with Podman.
        "name": run_name(a), "start": getattr(a, "start_key", None) or a.start_id,
        "kind": "mcp" if a.mcp_socket_dir else "agent",
        "pod": pod_name(a, run) if a.backend == "podman" else None, "podman_url": a.podman_url,
        # --vpn: its container in the pod; names allowed during the run are looked up through it (vpn.resolver_for_run).
        "vpn_container": load_vpn_module().container_name(a.vpn_pod) if getattr(a, "vpn_pod", None) else None,
        # `sandbox-ai net allow --api-key` refuses a host the proxy does not open (as the start does).
        "no_inspect": sorted({value.lower() for value in a.no_inspect}),
        # `sandbox-ai vpn rotate` refuses a profile in use.
        "vpn_profile": a.vpn_profile["name"] if getattr(a, "vpn_profile", None) else None,
        # A Tor run: `sandbox-ai net allow --allow-host-port` keeps to it (only with --vpn-bypass-host-ports), and no
        # URL of it is opened in your browser (approvals.py), which would reach it from your own address.
        "vpn_type": a.vpn_profile.get("type") if getattr(a, "vpn_profile", None) else None,
        # Its tunnel lent to its own networked MCP servers (vpn.lends: WireGuard, Mullvad, a persistent Tailscale
        # node), or the run whose tunnel this MCP server's sandbox borrows: `mcp start --for`, `mcp attach --live`.
        "vpn_lends": bool(getattr(a, "vpn_lends", False)), "vpn_lent_by": getattr(a, "vpn_lent_by", None),
        # The desktop app's terminal tab the run is in (SANDBOX_AI_APP_TAB, set in each tab it opens): the app finds
        # the run of a tab - and whether it runs behind a VPN or Tor - before a link of it is opened on this host.
        "app_tab": app_tab(),
        "vpn_bypass_host_ports": bool(getattr(a, "vpn_bypass_host_ports", False)),
        "vpn_bypass_tcp": list(getattr(a, "vpn_bypass_tcp", None) or []),
        # macOS: the container `sandbox-ai net` reaches the supervisor in (podman exec; runtime.VM_CONTROL_DIR).
        **({"container": f"{pod_name(a, run)}-agent"} if on_machine(a) else {}),
        # sandbox-ai list (and the app and the web, which group by them): the agents --with in this sandbox, and
        # whose helper this sandbox is (--helper starts it as an MCP server of the lead).
        "companions": companions(a), "helper_of": getattr(a, "helper_of", None),
        # --service: the containers of your images beside the agent (sandbox-ai list shows them).
        "services": load_services_module().record_entries(a, pod_name(a, run))
        if getattr(a, "services", None) and a.backend == "podman" else [],
        # --browser: the sandbox's browser beside the agent; the app's Browser button finds its VNC socket here.
        "browser": load_browser_module().record(a, pod_name(a, run))
        if getattr(a, "browser_container", None) else None,
        # --cairn: Cairn's board beside the agent (its address, its container, its workers).
        "cairn": load_cairn_module().record(a, pod_name(a, run)) if getattr(a, "cairn_container", None) else None,
        # --artex: ARTEX beside the agent (its container; its console's address: sandbox-ai artex console).
        "artex": load_artex_module().record(a, pod_name(a, run)) if getattr(a, "artex_container", None) else None,
        # A HOME of your own (--state-dir) stays when the run ends; sandbox-ai list says so.
        "own_home": bool(a.state_dir) and not (getattr(a, "start_key", None) or a.tmux_socket_dir or a.mcp_socket_dir),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "proxy": proxy_enabled(a),
        # `sandbox-ai rules reload` reads these rulesets again (rules.json too); the start's own stay as they are.
        "rules_sources": {"files": [str(Path(path).expanduser().absolute()) for path in a.rules],
                          "project_config": getattr(a, "project_config", None)},
        # --hide: `sandbox-ai workspace add` hides what these globs match in a directory added during the run too.
        "hide": list(dict.fromkeys(getattr(a, "hide", None) or [])),
        # sandbox-ai activity and session events find the run's timeline (activity.jsonl) here.
        "log_dir": str(Path(a.log_dir).expanduser().resolve()) if a.log_dir else None,
        # devices.jsonl keeps serial numbers in full only then.
        "log_debug": bool(getattr(a, "log_debug", False)),
        # --toolchain: what the start mounted (sandbox-ai list, report); `toolchain attach` checks what a toolchain
        # needs against sandbox-ai's own images, and only notes it for an image of yours.
        "toolchains": load_toolchains_module().plan_shown(getattr(a, "toolchain_plan", None))
        if toolchain_entries(a) else [],
        "custom_image": a.backend == "podman" and not own_image(a),
        # `sandbox-ai net allow` refuses destinations inside a chosen network of the sandbox's own.
        "subnets": [v for v in (getattr(a, "subnet", None), getattr(a, "subnet6", None)) if v],
        # `sandbox-ai net allow --git-token-*` writes logins its Git helper reads (not with --token-proxy).
        "runtime_git_tokens": not a.token_proxy,
        # `sandbox-ai git-logins give` hands a login to this run's connection proxy through its supervisor (control
        # op "git-logins"): no token enters the sandbox. Without the proxy there is only the helper's file above.
        "proxy_git_logins": bool(proxy_enabled(a)),
        # `sandbox-ai net allow --allow-host-port` relays host ports into this run (the supervisor's PortRelay) -
        # never to DSH's web UI's port (nor a service's: "services").
        "host_relay": True,
        **({"dsh_web_port": dsh_web_ui_port(a)} if dsh_web_ui_port(a) else {}),
        # The MCP servers this run reaches (--mcp, helpers included): sandbox-ai mcp list names whom each serves.
        "mcp_servers": [] if a.mcp_socket_dir else list(dict.fromkeys(a.mcp)),
        # The agent was told what its sandbox has (/sandbox/session/INTRO.md, written again by a live attach).
        "intro": intro_on(a),
        # Those it reaches in an instance of its own (--mcp-limits, --mcp-mount, --mcp-env, or NAME ran with another
        # definition): {server: NAME.HASH}; the kill switch finds their sandboxes by it.
        **({"mcp_instances": {name: mcp_instance(a, name) for name in dict.fromkeys(a.mcp)
                              if mcp_instance(a, name) != name}}
           if not a.mcp_socket_dir and any(mcp_instance(a, name) != name for name in dict.fromkeys(a.mcp)) else {}),
        # An MCP server's sandbox: the server it is (sandbox-ai net cut cuts it with the runs it serves).
        **({"mcp_name": a.mcp_socket_dir.expanduser().parent.name} if a.mcp_socket_dir else {}),
        # `sandbox-ai net cut` (the kill switch): its supervisor takes the control op "cut" (runs from 0.1.94).
        **({"net_cut": 1} if getattr(a, "killswitch", True) is not None else {}),
        # --llm NAME[:MODEL]: sandbox-ai list, the app and the web show that server's status beside the start.
        "llm": getattr(a, "llm", None) or None,
        # `sandbox-ai mcp attach NAME START --live`: its supervisor opens relays during the run (control op mcp-open),
        # and the agent's HOME (host path) is where its MCP configuration is updated for it (mcp.py LIVE_AGENTS).
        "mcp_live": not (a.mcp_socket_dir or a.probe),
        "agent_home": str(a.sandbox_home) if getattr(a, "sandbox_home", None) else None,
        # `sandbox-ai kits attach|detach` and `plugins attach|detach` (kits_live.py): the kit folders it can add to
        # while the run goes on, whether the plugin store is mounted, whether the agent's own hooks are managed ones
        # (a plugin's hooks then run only from a start), and whether `kits restart-agent` can start the agent again in
        # its tmux window (session/agent-restart.sh: a detached tmux session with the agent in it).
        **({"kits_live": {**a.kits_live, "managed_hooks": bool(managed_hooks(a)),
                          "restart": bool(a.tmux_socket_dir) and runs_agent(a) and not getattr(a, "helper_of", None)}}
           if getattr(a, "kits_live", None) else {}),
        # The security mode: `sandbox-ai net allow` and `sandbox-ai approvals` keep to it (modes.py).
        "mode": a.runtime_mode,
        # What it may reach from its start, each with its source (sandbox-ai net show, the app, the web: reach()).
        "reach": recorded_reach(a),
        # The network profiles it started with (--profile; sandbox-ai net list shows them), and whether it is offline.
        "profiles": [name for name in (getattr(a, "profile", None) or []) if isinstance(name, str)],
        "offline": bool(getattr(a, "offline", False)),
        # --ask: on, off, or allow (every question answered by the launcher; sandbox-ai list, the app and the web warn).
        "ask": (a.ask if asking(a) else "off") if proxy_enabled(a) else None,
        # --ask-timeout: how long a request waits for its answer (the app and the web count down from it).
        "ask_timeout": ask_timeout(a),
        # --allow-host *.DOMAIN: the launcher answers the lookups below them itself (approvals.watch).
        "allow_wildcards": host_wildcards(a),
        # Those below a shared service, their risk confirmed: marked as a risk (net show, sandbox-ai report).
        **({"shared_wildcards": shared_wildcards(a)} if shared_wildcards(a) else {}),
        # The sandbox's cgroup is its own (the pod's; Bubblewrap's systemd scope), not the launcher's shared one:
        # only then may `workspace remove` freeze it and count its processes.
        "own_cgroup": a.backend == "podman" or bool(limits_prefix(a)),
        # `sandbox-ai workspace add` gives the push check a workspace added during the run (push-added.json there).
        **({"push_check": {"dir": str(a.push_check_dir), "rules_dir": str(privacy_rules_dir(a) or "") or None}}
           if getattr(a, "push_check_dir", None) else {}),
        # `sandbox-ai workspace add` holds a workspace added during the run to the checks of a start (workspaces.py).
        **({} if a.mcp_socket_dir else {"workspace_checks": workspace_checks(a, session)})}))
    if on_machine(a):
        # macOS: the token a request through podman exec must carry (runtime.VM_CONTROL_DIR); this directory is
        # never mounted for the agent.
        fd = os.open(directory / RUNTIME.EXEC_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(os.urandom(32).hex())
    return directory


def hold_supervisor_lock(directory):
    """Take the exclusive lock on the run DIRECTORY's runtime.SUPERVISOR_LOCK and hold it for as long as this launcher
    runs (the descriptor is never closed; O_CLOEXEC: no child holds it): whatever ends the launcher, the kernel lets go
    of it, and the sandbox's own supervisor (runtime.watch_launcher) cuts the sandbox's network. The descriptor, or
    None where the file system refuses the lock (the run then has no such watch: launcher_watched)."""
    fd = os.open(Path(directory) / RUNTIME.SUPERVISOR_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        LOG.warning("start", f"the run's lock could not be taken ({exc.strerror or exc}): its sandbox does not notice "
                             "when this launcher ends")
        return None
    return fd


def release_supervisor_lock(a):
    """The run's lock (hold_supervisor_lock) let go of and its descriptor closed, at the end of a run whose sandbox
    did not outlive the launcher; nothing where none was taken or it was let go of already."""
    fd, a.supervisor_lock = getattr(a, "supervisor_lock", None), None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def launcher_watched(a):
    """config.json's "launcher_lock": the sandbox's supervisor watches this launcher's lock (hold_supervisor_lock) and
    cuts its network when it is free - with Podman on this host's own kernel (a lock does not cross podman machine's
    shared folders or a remote engine; Bubblewrap ends with its launcher anyway), not for a probe."""
    return a.backend == "podman" and local_engine(a) and not a.probe and \
        getattr(a, "supervisor_lock", None) is not None


def workspace_checks(a, session):
    """What `sandbox-ai workspace add` checks a workspace added during the run against, as the start checked its
    own: the workspaces (host, sandbox path, ro|rw), whether further ones are read-only, the trees no workspace may
    overlap, the host paths none may hold (host_sources and the private directories) and the stores none may lie
    in, and sandbox-ai's own folders (sandbox_ai_folders). Paths as given and resolved, so that a later lookup needs
    nothing of A."""
    def both(path):
        literal = Path(os.path.normpath(Path(path).expanduser().absolute()))
        return list(dict.fromkeys([str(literal), str(literal.resolve())]))
    trees = [Path(src) for src, _, _ in map(parse_mount, a.mount)]
    trees += [p for p in (a.state_dir and a.state_dir.expanduser().resolve(), getattr(a, "sandbox_home", None)) if p]
    held = [(path, label) for path, label in host_sources(a)]
    held += [(exposed_registry(), "The registry of exposed directories (XDG_STATE_HOME)")]
    held += [(host, "A folder an MCP image server mounts (mcp.json)")
             for host in image_mount_hosts(getattr(a, "project_document", None))]
    if getattr(a, "mcp_mount", None):  # and those this start gives its servers (--mcp-mount)
        with contextlib.suppress(ValueError):
            held += [(host, "A folder an MCP server of this start mounts (--mcp-mount)")
                     for host in load_mcp_module().start_mount_hosts(a)]
    if a.llm:
        llm = load_llm_module()
        held += [(llm.store_path(), "The LLM endpoints file (XDG_CONFIG_HOME)"),
                 (llm.ca_dir(), "The LLM endpoints' CAs (XDG_CONFIG_HOME)")]
    private = [(path, option) for path, option in [
        (a.log_dir, "--log-dir"), (a.gui_vnc, "--gui-vnc"), (a.tmux_socket_dir, "--tmux-socket-dir"),
        (a.mcp_approvals_dir, "--mcp-approvals-dir"), (session, "The run's session directory"),
        (getattr(a, "proxy_dir", None), "The proxy's directory")] if path]
    targets = enumerate(workspace_targets(a))
    return {"workspaces": [{"host": str(Path(src).expanduser().resolve()), "sandbox": dest,
                            "mode": workspace_mode(a, index)} for index, (src, dest) in targets],
            "read_only": bool(a.workspace_ro or getattr(a, "mode_other_workspaces_ro", False)),
            "trees": [p for tree in trees for p in both(tree)],
            "held": [[p, label] for path, label in held for p in both(path)],
            "private": [[p, label] for path, label in private for p in both(path)],
            "stores": [[str(path), label] for path, label in host_stores(a)],
            # sandbox-ai's own folders as this start saw them (its XDG_* values): a later add from another
            # environment (the app, the web UI) refuses them too.
            "own_folders": [str(path) for path in sandbox_ai_folders()],
            # The launcher this start runs (a checkout, say) and its package: a later add through another sandbox-ai
            # (the installed CLI, the app, the web UI) refuses them too, not only its own.
            "launcher": [str(path) for path in launcher_trees()],
            "packages": [[str(path), label] for path, label in package_paths()]}


def run_name(a):
    """The name this run shows (sandbox-ai list): its start's current name for a session's supervisor (renamed
    perhaps since it was started), else the name the start got (name_start); None for MCP servers."""
    if getattr(a, "start_id", None):
        try:
            data = load_sessions_module().load(a.start_id)
        except (OSError, ValueError):
            return a.start_id
        return data.get("name") if isinstance(data.get("name"), str) else a.start_id
    return getattr(a, "start_name", None)


def pod_name(a, run=None):
    """The Podman pod of this run (RUN, default a.run_id): the VPN's pod (vpn.py made it), the one a session or
    MCP server chose, else agent-RUN."""
    import uuid
    run = run or getattr(a, "run_id", None) or f"{a.agent}-{uuid.uuid4().hex[:12]}"
    return getattr(a, "vpn_pod", None) or a.pod_name or f"agent-{run}"


def load_starts_module():
    spec = importlib.util.spec_from_file_location("sandbox_starts", HERE / "starts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This launcher for it and its modules (sessions, net, mcp, team): they loaded agent_sandbox.py (and the runtime) a
    # second time for the names of a start, about 20 ms of each.
    module._modules.setdefault("agent_sandbox", RunningLauncher())
    return module


def project_label(a):
    """The PROJECT part of the automatic names: --project-name (the project file's "name"), else the main
    workspace's folder name, made fit for a name."""
    return a.project_name or load_starts_module().sanitise(PurePosixPath(a.workspaces[0]).name)


def name_start(a):
    """The name of this start: --session or --name as given (a --name in use is refused), else a codename
    (brisk-otter-lantern) or, with --name-style project (or "name_style" in your defaults), AGENT-PROJECT-xxxx.
    None for an MCP server and a session's supervisor (its start is named already)."""
    if a.mcp_socket_dir or a.tmux_socket_dir:
        return None
    if a.session is not None:
        return a.session  # sessions.py decides: a new session or the restart of a stopped one
    starts = load_starts_module()
    if a.name is not None:
        if not a.dry_run:
            starts.check_free(a.name)
        return a.name
    style = getattr(a, "name_style", None) or getattr(a, "name_style_default", None) or "codename"
    return starts.default_name(a.agent, project_label(a), style)


def history_event(a, event, status=None):
    """Note EVENT (start, exit) of this run in its name's history (starts.py); True if written. Not for MCP
    servers and probes; a session's start is noted by sessions.py, which knows whether it is a rebirth."""
    if a.mcp_socket_dir or a.probe or (event == "start" and a.tmux_socket_dir) or not getattr(a, "control_dir", None):
        return False
    try:
        return load_starts_module().run_event(a.control_dir, event, status)
    except Exception:  # noqa: BLE001 - the history never stops a start or hides its exit status
        return False


def kits_apply(a):
    """Kits, project memory and plugins (and with them the agent's hooks) are for agent sandboxes, not for probes
    or MCP servers; a helper (--helper) is an agent, though it serves MCP."""
    return not a.probe and (not a.mcp_socket_dir or bool(getattr(a, "helper_of", None)))


def runs_agent(a):
    """The sandbox runs the agent itself: no --binary, or a helper, whose server runs the agent for each task."""
    return a.binary is None or bool(getattr(a, "helper_of", None))


def kit_managed_paths(a):
    """Sandbox HOME paths that kits, project memory or the plugin store fill in this run."""
    if not kits_apply(a):
        return []
    kits = load_kits_module()
    paths = []
    if not a.no_default_kit or a.kit or a.kit_dir:
        paths += kits.LAYOUT[a.agent].values()
    if not a.no_memory:
        paths += [path for path, _ in kits.memory_paths(a.agent)]
    if a.agent == "claude" and not a.no_plugins:
        paths += [".claude/plugins/cache", ".claude/plugins/marketplaces"]
    if a.agent == "codex" and not a.no_plugins:
        paths += [".codex/plugins/cache"]
    return [PurePosixPath(path) for path in paths]


ENABLE_AGENTS = ("hermes", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi")


def check_enable_agents(a):
    """--AGENT-enable for an agent that is not of this start (not its lead, not --with): refused as that agent's own
    check refuses it ("--hermes-enable is for Hermes ..."), with what would let the start go on (fixes.py) - the
    agent added beside the lead, or the option left out: a choice of the human's, asked on a terminal, shown as whole
    command lines elsewhere, never taken by --fix."""
    for agent in ENABLE_AGENTS:
        if not getattr(a, f"{agent}_enable", None) or a.agent == agent or agent in companions(a):
            continue
        try:
            globals()[f"load_{agent}_module"]().check(a, hooks_active(a), companions(a))
        except ValueError as exc:
            lacking = f"--{agent}-enable sets a feature of {agent}, which is no agent of this start (the lead {a.agent})"
            raise FIXES.Missing(str(exc), [
                FIXES.fix("option", f"with-{agent}", lacking, f"add {agent} beside the lead (the team image with both "
                          "is built when it is not there)", options={"add": ["--with", agent]},
                          choice=f"{agent}-enable",
                          security=f"{agent} runs in the same sandbox, with its own login and its provider's hosts"),
                FIXES.fix("option", f"without-{agent}-enable", lacking, f"start without --{agent}-enable",
                          options={"remove": [f"--{agent}-enable"]}, choice=f"{agent}-enable")]) from None


def check_kits_there(a):
    """--kit NAME: a kit that is not in the kits folder (for this agent) is refused as kits.sources refuses it, with
    what makes it (fixes.py): an empty kit of that name for the agent - its skills are yours to add."""
    kits = load_kits_module()
    lacking = [name for name in dict.fromkeys(a.kit) if not kits.kit_path(name, a.agent).is_dir()
               and name not in kits.kit_names(kits.BUILTIN_KITS)]
    if not lacking or not kits_apply(a):
        return
    try:
        kits.sources(a)
    except ValueError as exc:
        raise FIXES.Missing(str(exc), [FIXES.fix(
            "kit", name, f"the kit {name} is not in the kits folder for {a.agent}",
            "make it, empty (add its skills later: sandbox-ai kits add-skill)",
            [["sandbox-ai", "kits", "new", name, "--agent", a.agent]], duration="seconds")
            for name in lacking]) from None


def plugin_fix(a):
    """The fix (fixes.py) for the plugins the start's kits declare that the plugin store does not have: installed
    by the plugin installer's sandbox, in this terminal. Not needed - the start goes on without them, said in a
    note - so it joins what else the start lacks. None when none is missing."""
    plan = kit_plan(a)
    missing = (plan or {}).get("plugins", {}).get("missing") if isinstance((plan or {}).get("plugins"), dict) else None
    if not missing:
        return None
    return FIXES.fix("plugin", ",".join(missing), f"the plugins {', '.join(missing)} of the start's kits are not in "
                     "the plugin store", "install them (the plugin installer's sandbox, in this terminal)",
                     [load_kits_module().install_command(missing, a.agent if a.agent in load_kits_module().STORE_AGENTS
                                                         else "claude").split()],
                     duration="minutes", terminal=True, needed=False)


def kit_plan(a):
    """What the kits, the project memory and the plugin store give this run, without changing anything."""
    if not kits_apply(a):
        return None
    kits = load_kits_module()
    plan = kits.scan(a.agent, kits.sources(a))
    marketplaces, plugins, notes = kits.declarations(a.agent, plan["plugins"], check=kit_check(a)) \
        if not a.no_plugins else ({}, [], [])
    hooks = []
    for label, folder in kits.kit_hook_sources(a.agent, kits.sources(a)):
        info = kits.read_hooks(a.agent, folder)
        hooks.append({"kit": label, "path": str(folder), "hooks": len(info["hooks"]), "errors": info["errors"],
                      "trusted": kits.hooks_trusted(a.agent, info["sha256"], kits.trust_argument(label, folder))})
    result = {"sources": [{"kit": label, "path": str(path)} for label, path in kits.sources(a)],
              "entries": kits.summary(plan), "hooks": hooks,
              "memory_template": plan["memory"][0] if plan["memory"] else None,
              "memory": None if a.no_memory else str(kits.memory_store(a.workspaces[0], a.agent)),
              "notes": plan["notes"] + notes}
    if not a.no_default_kit and not kits.default_home(a.agent).is_dir():
        result["notes"].append(f"the default kit {kits.default_home(a.agent)} is created with examples on the "
                               "first start")
    if a.agent in kits.STORE_AGENTS and not a.no_plugins:
        _, installed = kits.installed("claude")
        result["plugins"] = {"marketplaces": sorted(marketplaces), "enabled": [p for p in plugins if p in installed],
                             "missing": [p for p in plugins if p not in installed]}
        if a.agent != "claude":  # each plugin's approval (codex_plugins, opencode2_plugins)
            staged = kits.stage_store_plugins(a.agent, [p for p in plugins if p in installed], installed)
            result["plugins"]["approval"] = {item["plugin"]: (
                "cannot run: " + item["errors"][0] if item["errors"] else
                "approved" if item["trusted"] and (item["declared"] or a.agent == "opencode2") else
                "not approved" if item["declared"] or a.agent == "opencode2" else "nothing to approve")
                for item in staged}
    elif a.agent == "opencode" and plugins:
        result["plugins"] = {"npm": plugins}
    return result


def ensure_mount_point(home, relative):
    """An empty directory in the sandbox HOME for a read-only mount; never through a symlink."""
    path = check_home_path(home, home / relative)
    if path.exists() and not path.is_dir():
        raise ValueError(f"Sandbox path {relative} is not a directory; kits mount a directory there")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def claude_plugins(home, marketplaces, plugins, notes):
    """Read-only mounts of the plugin store and the Claude settings that enable the kits' plugins."""
    kits = load_kits_module()
    known, installed = kits.installed("claude")
    missing = [p for p in plugins if p not in installed]
    if missing:
        notes.append(f"plugins not in the plugin store yet: {', '.join(missing)}; install them with: "
                     f"{kits.install_command(missing)}")
    mounts = []
    store = kits.plugin_dir("claude")
    if store and (known or installed):
        # A store with marketplaces and no plugin yet has no cache: made empty here (the store's own folder, never
        # through a link), so the sandbox gets it mounted and `sandbox-ai plugins attach` works while it runs.
        with contextlib.suppress(FileExistsError, OSError):
            os.mkdir(store / "cache", 0o700)
        for name in ("cache", "marketplaces"):
            if (store / name).is_dir() and not (store / name).is_symlink():
                ensure_mount_point(home, f".claude/plugins/{name}")
                mounts.append((store / name, f"/root/.claude/plugins/{name}", "ro"))
        # Writable copies: Claude records state there. Updates come from `sandbox-ai plugins claude update`.
        known = {name: {**entry, "autoUpdate": False} for name, entry in known.items()}
        replace_private(json.dumps(known, indent=2).encode(), home, ".claude/plugins/known_marketplaces.json")
        replace_private(json.dumps({"version": 2, "plugins": installed}, indent=2).encode(), home,
                        ".claude/plugins/installed_plugins.json")

    def apply(document):
        before = json.dumps(document, sort_keys=True)
        enabled = document.get("enabledPlugins")
        enabled = enabled if isinstance(enabled, dict) else {}
        for plugin in installed:
            enabled[plugin] = plugin in plugins  # a plugin no kit of this run lists stays off
        extra = document.get("extraKnownMarketplaces")
        extra = extra if isinstance(extra, dict) else {}
        for name, source in marketplaces.items():
            if name in known:
                extra[name] = {"source": source}
        document["enabledPlugins"], document["extraKnownMarketplaces"] = enabled, extra
        return json.dumps(document, sort_keys=True) != before
    problem = update_json_defaults(home, ".claude/settings.json", apply)
    if problem:
        notes.append(f"plugins not enabled: .claude/settings.json: {problem}")
    return mounts


CODEX_PLUGIN_MARKETPLACES = "codex-marketplaces"  # in the session directory: one empty folder per marketplace


def codex_plugin_marketplace(name):
    """Where Codex's configuration says the marketplace NAME of a kit's plugin is: an empty read-only folder of the
    session. Codex needs a configured marketplace for each plugin while marketplace sources are restricted
    (codex-rs/core-plugins marketplace_policy.rs: policy_filtered_plugin_config); the plugins themselves come from the
    plugin store, so the folder lists none - nothing for Codex to copy or refresh from (loader.rs:
    refresh_non_curated_plugin_cache)."""
    return f"/sandbox/session/{CODEX_PLUGIN_MARKETPLACES}/{name}"


def codex_plugins(a, home, plugins, notes, staging=None):
    """The kits' plugins for Codex from sandbox-ai's plugin store: [read-only mounts] of each plugin's installed folder
    at ~/.codex/plugins/cache/MARKETPLACE/NAME/local, the run's a.codex_plugins ({"id", "marketplace", "mcp"}) for its
    managed configuration and requirements, a.plugin_hooks (the hooks of those plugins: they run only once approved).
    A plugin that brings ChatGPT apps, comes from a marketplace name Codex keeps for OpenAI, or cannot be read is not
    given (NOTES say why). Off with --telemetry strict, which turns Codex's plugin system off. STAGING (a start): an
    approved plugin with hooks is given as a copy made and checked there (kits.checked_copy), not the live store."""
    kits = load_kits_module()
    a.codex_plugins, a.plugin_hooks = [], []
    if not plugins:
        return []
    if telemetry_level(a) == "strict":
        notes.append(f"the kits' Codex plugins ({', '.join(plugins)}) are off: --telemetry strict (also the mode "
                     "paranoid) turns Codex's plugin system off (features.plugins)")
        return []
    _, installed = kits.installed("claude")
    missing = [p for p in plugins if p not in installed]
    if missing:
        notes.append(f"plugins not in the plugin store yet: {', '.join(missing)}; install them with: "
                     f"{kits.install_command(missing, 'codex')}")
    mounts = []
    for info in kits.stage_store_plugins("codex", [p for p in plugins if p in installed], installed,
                                         staging=staging):
        plugin = info["plugin"]
        name, marketplace = plugin.split("@")
        if kits.CODEX_RESERVED_MARKETPLACE.fullmatch(marketplace):
            notes.append(f"{info['label']}: not given to Codex: Codex keeps the marketplace name {marketplace} for "
                         "its own (OpenAI's, the ChatGPT account's)")
            continue
        if info["errors"]:
            notes.append(f"{info['label']}: not given to Codex: {info['errors'][0]}")
            continue
        if info["apps"]:
            notes.append(f"{info['label']}: not given to Codex: it brings ChatGPT apps (connectors), which run at "
                         "chatgpt.com, outside sandbox-ai's MCP gateway")
            continue
        target = kits.sandbox_root("codex", plugin, None)
        ensure_mount_point(home, str(PurePosixPath(target).relative_to("/root")))
        mounts.append((Path(info["host_root"]), target, "ro"))
        a.codex_plugins.append({"id": plugin, "marketplace": marketplace, "mcp": info["mcp"]})
        if info["mcp"]:
            notes.append(f"{info['label']}: its MCP servers ({', '.join(info['mcp'])}) do not run (Codex's "
                         "requirements): sandbox-ai runs MCP servers only behind its gateway; define one with "
                         "sandbox-ai mcp and give it with --mcp")
        if info["declared"]:
            a.plugin_hooks.append(info)
            if info["trusted"]:
                notes.append(f"{info['label']}: {len(info['hooks'])} approved hook(s)")
            else:
                notes.append(f"{info['label']}: {len(info['hooks'])} hook(s) not approved: they do not run; review "
                             f"and approve them with: sandbox-ai plugins codex trust-hooks {plugin}")
    return mounts


def opencode2_plugins(a, plugins, notes, staging=None):
    """The kits' plugins for OpenCode 2 from sandbox-ai's plugin store: [read-only mounts] of each approved plugin's
    installed folder at /sandbox/plugins/MARKETPLACE/NAME, and a.opencode2_plugins (those folders), which its
    configuration layer names after sandbox-ai's rule plugin. A plugin is code in OpenCode 2's own process: it runs
    only once approved (sandbox-ai plugins opencode2 trust-hooks), never when it cannot be read. STAGING (a start):
    the approved plugin is given as a copy made and checked there (kits.checked_copy), not the live store."""
    kits = load_kits_module()
    a.opencode2_plugins = []
    if not plugins:
        return []
    _, installed = kits.installed("claude")
    missing = [p for p in plugins if p not in installed]
    if missing:
        notes.append(f"plugins not in the plugin store yet: {', '.join(missing)}; install them with: "
                     f"{kits.install_command(missing, 'opencode2')}")
    mounts = []
    for info in kits.stage_store_plugins("opencode2", [p for p in plugins if p in installed], installed,
                                         staging=staging):
        if info["errors"]:
            notes.append(f"{info['label']}: not given to OpenCode 2: {info['errors'][0]}")
            continue
        if not info["trusted"]:
            notes.append(f"{info['label']}: not approved: an OpenCode 2 plugin is code that runs in OpenCode 2 "
                         "itself; review and approve it with: sandbox-ai plugins opencode2 trust-hooks "
                         f"{info['plugin']}")
            continue
        mounts.append((Path(info["host_root"]), info["sandbox_path"], "ro"))
        a.opencode2_plugins.append(info["sandbox_path"])
        notes.append(f"{info['label']}: approved; OpenCode 2 loads it after sandbox-ai's rule plugin")
    return mounts


def claude_drop_plugins(home, notes):
    """--no-plugins on a HOME of an earlier run: what claude_plugins() wrote there would still enable
    plugins whose store is no longer mounted. Other settings stay; nothing is followed as a symlink."""
    kits = load_kits_module()
    try:
        for name in ("known_marketplaces.json", "installed_plugins.json"):
            kits.remove_at(home, f".claude/plugins/{name}", tree=False)
    except (OSError, ValueError) as exc:
        notes.append(f"plugin index of an earlier run left in place: {exc}")
    if not check_home_path(home, home / ".claude/settings.json").exists():
        return  # never create the file just to leave it without plugins

    def apply(document):
        return bool([document.pop(key) for key in ("enabledPlugins", "extraKnownMarketplaces") if key in document])
    problem = update_json_defaults(home, ".claude/settings.json", apply)
    if problem:
        notes.append(f"plugins of an earlier run may stay enabled: .claude/settings.json: {problem}")


def kit_hook_notes(agent, staged):
    """What the start says about the kits' hooks: approved ones run, the others are named with the command that
    approves them after a review."""
    notes = []
    for item in staged:
        count = len(item["hooks"])
        if item["errors"]:
            more = f" (and {len(item['errors']) - 1} more)" if len(item["errors"]) > 1 else ""
            notes.append(f"{item['label']}: its hooks for {agent} do not run: {item['errors'][0]}{more}")
        elif not item["trusted"]:
            notes.append(f"{item['label']}: {count} hook(s) for {agent} not approved (new, or changed since they "
                         f"were approved): they do not run; review and approve them with: sandbox-ai kits "
                         f"trust-hooks {shlex.quote(item['kit'])} --agent {agent}")
        elif count:
            notes.append(f"{item['label']}: {count} approved hook(s) for {agent}")
    return notes


def nanobot_note_skill(staging, staged, note):
    """nanobot's network note (NOTE): an always-loaded skill (nanobot.note_skill) beside the kits' skills in STAGING,
    which are mounted read-only at its workspace's skills - nanobot puts such a skill into its system prompt every
    turn (agent/context.py: Active Skills). STAGED ([(staged directory, sandbox path)] of kits.stage) with the
    skills' mount added when the kits had no skill."""
    nanobot, kits = load_nanobot_module(), load_kits_module()
    target = staging / "skills"
    target.mkdir(mode=0o755, exist_ok=True)
    folder = target / nanobot.NOTE_SKILL
    if folder.is_symlink() or folder.exists():
        shutil.rmtree(folder) if folder.is_dir() and not folder.is_symlink() else folder.unlink()
    folder.mkdir(mode=0o755)
    skill = folder / "SKILL.md"
    skill.write_text(nanobot.note_skill(note), encoding="utf-8")
    skill.chmod(0o644)
    dest = f"/root/{kits.LAYOUT['nanobot']['skills']}"
    return staged if any(source == target for source, _ in staged) else [*staged, (target, dest)]


def prepare_kits(a, home, staging):
    """Kits, project memory and plugins for this run: (read-only mounts, notes, memory state)."""
    mounts, notes, memory = [], [], None
    if not kits_apply(a):
        return mounts, notes, memory
    kits = load_kits_module()
    if a.agent == "claude" and kits.MARKETPLACE.is_dir():
        mounts.append((kits.MARKETPLACE, kits.MARKETPLACE_TARGET, "ro"))
    if not a.no_default_kit and kits.ensure_default_home(a.agent):
        notes.append(f"created the default kit {kits.default_home(a.agent)} with examples")
    plan = kits.scan(a.agent, kits.sources(a))
    notes += plan["notes"]
    check = kit_check(a)
    staging.mkdir(mode=0o755)
    # Every kind of the agent's kit folders is mounted (an empty one where the kits have none) unless the start gives
    # no kit at all: `sandbox-ai kits attach` copies into them while the sandbox runs (kits_live.py).
    every_kind = not a.no_default_kit or bool(a.kit) or bool(a.kit_dir)
    staged, more = kits.stage(a.agent, plan, staging, check, every_kind=every_kind)
    if a.agent == "nanobot" and start_note(a):
        staged = nanobot_note_skill(staging, staged, start_note(a))
    notes += more + kit_source_notes(plan, a)
    a.kit_hooks = kits.stage_hooks(a.agent, kits.sources(a), staging / "hooks", KIT_HOOKS_TARGET)
    notes += kit_hook_notes(a.agent, a.kit_hooks)
    for source, dest in staged:
        ensure_mount_point(home, str(PurePosixPath(dest).relative_to("/root")))
        mounts.append((source, dest, "ro"))
    # run.json "kits_live": where `sandbox-ai kits attach` puts what it gives this sandbox while it runs (the staged
    # folders on this host, each mounted read-only at its place in the sandbox's HOME).
    a.kits_live = {"agent": a.agent, "staging": str(staging),
                   "kinds": {Path(source).name: dest for source, dest in staged if Path(source).parent == staging},
                   "plugin_store": False}
    if not a.no_memory:
        manifest, seeded, store, more = kits.memory_prepare(a.agent, a.workspaces[0], home, plan["memory"],
                                                            check, persistent=bool(a.state_dir))
        notes += [f"memory: {note}" for note in more]
        memory = manifest
        if seeded:
            notes.append(f"new project memory {store}, started from the {kits.MEMORY_FILE[a.agent][0]} of {seeded}")
    if not a.no_plugins:
        marketplaces, plugins, more = kits.declarations(a.agent, plan["plugins"], check=check)
        notes += more
        if a.agent == "claude":
            store_mounts = claude_plugins(home, marketplaces, plugins, notes)
            mounts += store_mounts
            # `sandbox-ai plugins attach`: a plugin installed into the store now is in the running sandbox at once.
            a.kits_live["plugin_store"] = any(dest == "/root/.claude/plugins/cache" for _, dest, _ in store_mounts)
            # The approved ones are copied for the run where managed hooks run them (without managed hooks Claude
            # Code runs a plugin's hooks itself, as always: nothing is copied for that).
            a.plugin_hooks = kits.stage_plugin_hooks(plugins, kits.installed("claude")[1],
                                                     staging=staging / "plugin-hooks" if managed_hooks(a) else None,
                                                     target_root=PLUGIN_HOOKS_TARGET)
            notes += plugin_hook_notes(a)
        elif a.agent == "codex":
            mounts += codex_plugins(a, home, plugins, notes, staging=staging / "plugins")
        elif a.agent == "opencode2":
            mounts += opencode2_plugins(a, plugins, notes, staging=staging / "plugins")
        elif a.agent == "opencode" and plugins:
            a.opencode_plugins = plugins
            if not a.egress_all_allow and "registry.npmjs.org" not in allowed_hosts(a):
                notes.append("OpenCode installs its plugins from npm: allow it with --allow-host registry.npmjs.org")
    elif a.agent == "claude" and not plugin_store_run(a):
        # The plugin installer's HOME is the store: its index is what that run is there to write.
        claude_drop_plugins(home, notes)
    return mounts, notes, memory


def save_memory(a, home, manifest):
    """After the run: what the agent changed in its memory goes back to the project's memory."""
    kits = load_kits_module()
    changed, notes = kits.memory_save(a.agent, a.workspaces[0], home, manifest)
    for note in notes:
        print(f"agent-sandbox: memory: {printable(note)}", file=sys.stderr)
    if changed:
        print(f"agent-sandbox: memory: {changed} file(s) kept in {kits.memory_store(a.workspaces[0], a.agent)}",
              file=sys.stderr)


def claude_credential_pair(a, home):
    """(host, sandbox) credential paths when Claude auth is shared with the host."""
    if "claude" not in team(a) or (a.agent == "claude" and auth_mode(a) not in ("auto", "copy")):
        return None
    if a.agent != "claude" and companion_auth(a, "claude") != "auto":
        return None
    return auth_sources("claude")[0][0], home / AUTH_TARGETS["claude"]


def opencode_auth_from_codex(source):
    try:
        tokens = json.loads(Path(source).expanduser().read_text())["tokens"]
        access, refresh, account = (tokens[key] for key in ("access_token", "refresh_token", "account_id"))
        if not all(isinstance(value, str) and value for value in (access, refresh, account)):
            raise ValueError()
        payload = access.split(".")[1]
        expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
            raise ValueError()
    except (KeyError, TypeError, IndexError, ValueError, RecursionError):
        raise ValueError("Codex auth must contain ChatGPT access/refresh tokens, account_id and a JWT expiry") from None
    return {"openai": {"type": "oauth", "access": access, "refresh": refresh,
                       "expires": expires * 1000, "accountId": account}}


def own_environment(a):
    """The variables A's agent's own module gives a start that runs it (Hermes', DSH's, Qwen Code's, Goose's,
    OpenHands', nanobot's, Letta Code's, OpenCode 2's with its configuration layer, Pi's); {} for the others. For an
    agent --with: own_environment(companion_view(...)). No two agents give one variable different values
    (tests/test_with_all_agents.py), OpenCode and OpenCode 2 never share a sandbox (check_companions)."""
    env = {}
    if a.agent == "hermes" and runs_agent(a):
        env.update(load_hermes_module().environment(a))
    if a.agent in ("dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi") and runs_agent(a):
        env.update(agent_module(a.agent).environment(a))
    if a.agent == "opencode2" and runs_agent(a):
        # Its variables and its configuration layer (OPENCODE_CONFIG_CONTENT, above every file of its own).
        env.update(load_opencode2_module().environment(a, opencode2_content(a)))
    if a.agent == "goose" and thinking_shown(a) is False:
        env.pop("GOOSE_CLI_SHOW_THINKING", None)  # any value of it turns Goose's thinking on
    if a.agent == "goose" and start_note(a):  # the start's note in its Top Of Mind file, every turn (agent_files)
        env["GOOSE_MOIM_MESSAGE_FILE"] = load_goose_module().LOOP_FILE
    return env


def agent_environment(a):
    env = {}
    if a.agent == "claude" and a.mcp and runs_agent(a):
        # Claude Code before its per-server "timeout" (mcp_agent_args): the longest wait of the start's servers, and
        # its wait for a server to start (milliseconds).
        env.update({"MCP_TOOL_TIMEOUT": str(max(mcp_tool_timeouts(a).values()) * 1000),
                    "MCP_TIMEOUT": str(MCP_STARTUP_TIMEOUT * 1000)})
    env.update({key: os.environ[key] for key in a.env})
    env.update(secret_environment(a))
    env.update(api_key_environment(a))
    env.update(git_environment(a))
    env.update(publish_environment(a))
    if a.local_config:
        env.update({"OPENCODE_CONFIG": "/root/.config/opencode/opencode.jsonc",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1"})
    # --toolchain: its variables (JAVA_HOME ...), where you set none yourself (--env); its caches in the sandbox HOME.
    for key, value in load_toolchains_module().environment(toolchain_entries(a)).items() \
            if toolchain_entries(a) else ():
        env.setdefault(key, value)
    env.update(own_environment(a))
    for agent in managed_companions(a):  # the agents --with: their variables, as their own starts set them
        env.update(own_environment(companion_view(a, agent)))
    for agent in image_agents(a):
        # --binary in the image of an agent whose telemetry is never switchable, and the other agents of a team image
        # (each can be typed in its shell): still off.
        if not runs_agent(a) or agent not in team(a):
            env.update(pinned_telemetry_environment(agent))
    if a.llm:
        env.update(load_llm_module().agent_environment(a))
    # The telemetry opt-outs after what you pass (--env, --secret-env, --api-key, --llm): telemetry is always off.
    forced = telemetry_environment(a)
    undone = sorted(name for name, value in forced.items() if env.get(name, value) != value)
    if undone:
        print(f"agent-sandbox: {', '.join(undone)} {'is' if len(undone) == 1 else 'are'} set by sandbox-ai, not "
              "passed (telemetry is always off)", file=sys.stderr)
    env.update(forced)
    if hermes_guarded(a):  # what would take Hermes' pinned configuration away: never passed (its guard refuses it)
        dropped = [name for name in load_hermes_module().GUARD.SCOPE_ENV if env.pop(name, None) is not None]
        if dropped:
            print(f"agent-sandbox: {', '.join(dropped)} {'is' if len(dropped) == 1 else 'are'} not passed to Hermes "
                  "(telemetry is always off)", file=sys.stderr)
    # What a guarded command would refuse (a telemetry variable with a value of its own, one that takes a managed
    # configuration away): never passed, or every start of the agent in this sandbox would be a refused one.
    dropped = sorted({name for agent in telemetry_guarded(a) for name in TELEMETRY_GUARD.refused_names(agent, env)})
    if dropped:
        for name in dropped:
            env.pop(name)
        print(f"agent-sandbox: {', '.join(dropped)} {'is' if len(dropped) == 1 else 'are'} not passed (telemetry is "
              "always off)", file=sys.stderr)
    if a.dangerously_skip_permissions:
        for agent in team(a):
            env.update(DANGER_ENV[agent])
    if companions(a):  # who else is here, for the agent (and the sandbox-ai skill)
        env["SANDBOX_AI_COMPANIONS"] = " ".join(companions(a))
    if getattr(a, "services", None):  # the services beside it (--service), for the agent: NAME=localhost:PORT,...
        env["SANDBOX_AI_SERVICES"] = " ".join(
            service["name"] + "=" + ",".join(f"localhost:{port}" for port in service["ports"]) for service in a.services)
    if getattr(a, "browser_container", None):  # the sandbox's browser (--browser): whether and how it may drive it
        env.update(load_browser_module().agent_environment(a))
    if getattr(a, "cairn_container", None):  # Cairn's board (--cairn): its address
        env.update(load_cairn_module().agent_environment(a))
    if getattr(a, "artex_container", None):  # ARTEX (--artex): its API's address in the pod
        env.update(load_artex_module().agent_environment(a))
    if a.tmux_socket_dir:
        env["TMUX_TMPDIR"] = TMUX_DIR
    if nested(a):
        env.update(NESTED_ENV)
    if getattr(a, "helper_of", None) and a.agent == "opencode":
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"  # the lead shares /work: no plugins or settings from there
    content = {}
    if a.agent == "opencode" and a.mcp and runs_agent(a):
        # "timeout" (milliseconds): OpenCode's wait for the server's start and for each of its calls (mcp_tool_timeouts).
        timeouts = mcp_tool_timeouts(a)
        content["mcp"] = {name: {"type": "local", "command": ["python3", *mcp_bridge_args(name)], "enabled": True,
                                 "timeout": timeouts[name] * 1000}
                          for name in dict.fromkeys(a.mcp)}
    if a.agent == "opencode" and getattr(a, "opencode_plugins", None) and runs_agent(a):
        content["plugin"] = list(a.opencode_plugins)
    if a.agent == "opencode" and runs_agent(a) and managed_hooks(a):
        # The rule hooks' plugin first, then the approved kits' plugins (their copies in the session directory).
        content["plugin"] = [*content.get("plugin", []), *(["file://" + OPENCODE_HOOK_PLUGIN] if hooks_active(a) else []),
                             *(f"file://{staged['target']}/{name}" for staged in trusted_kit_hooks(a)
                               for name in staged["plugins"]),
                             *(["file://" + OPENCODE_HOOK_RECHECK] if opencode_recheck(a) else [])]
    if "opencode" in companions(a) and hooks_active(a):
        content["plugin"] = ["file://" + OPENCODE_HOOK_PLUGIN]  # the rules hold for it too
    if a.agent == "opencode" and note_path(a):  # one more of its instruction files: its AGENTS.md stays
        content["instructions"] = [note_path(a)]
    if "opencode" in team(a) and runs_agent(a):
        content.update(OPENCODE_TELEMETRY_CONFIG)  # after the project's and your own configuration: this wins
    if content:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content)
    if proxy_enabled(a):
        env.update({name: str(CA_DIR / "bundle.pem") for name in CA_BUNDLE_VARIABLES})
        env["NODE_EXTRA_CA_CERTS"] = str(CA_DIR / "ca.pem")
    if getattr(a, "run_id", None):
        env["SANDBOX_AI_RUN"] = a.run_id
    if tor_run(a):
        # Behind Tor only: it is told so, and where tor maps .onion names (a command MCP server that refuses private
        # targets takes exactly that network; the agent knows why .onion names resolve to 10.192.x.x).
        env.update(load_vpn_module().tor_environment())
    if host_browser(a):
        # Browser requests (OAuth logins and the like) go to you as questions.
        env["PATH"] = "/sandbox/session/bin:" + base_env()["PATH"]
        env["BROWSER"] = "/sandbox/session/bin/xdg-open"
    if a.gui:
        env["DISPLAY"] = GUI_DISPLAY
        # Qt on that display, unless you chose (--env QT_QPA_PLATFORM): a Qt toolchain (pyside6, qt) says offscreen
        # for the sandbox without a display, at a start and in every new shell (env.sh, where nothing set it).
        if "QT_QPA_PLATFORM" not in a.env:
            env["QT_QPA_PLATFORM"] = "xcb"
    if getattr(a, "audio", None) == "virtual":
        env["PULSE_SERVER"] = f"unix:{RUNTIME.AUDIO_SOCKET}"
    if a.gui_wayland:
        env.update({"WAYLAND_DISPLAY": PurePosixPath(WAYLAND_TARGET).name,
                    "XDG_RUNTIME_DIR": str(PurePosixPath(WAYLAND_TARGET).parent),
                    "MOZ_ENABLE_WAYLAND": "1", "QT_QPA_PLATFORM": "wayland;xcb",
                    "ELECTRON_OZONE_PLATFORM_HINT": "auto"})
    return env


def opencode2_content(a):
    """OpenCode 2's configuration layer for this run (opencode2.config_content): the --mcp servers as direct tools with
    their waits, -D, the model (--llm's provider/model or --model), the network note's file as an instruction."""
    opencode2 = load_opencode2_module()
    servers = opencode2.mcp_servers(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
    model = load_llm_module().opencode2_model(a) if a.llm else None
    return opencode2.config_content(a, servers, model, note_path(a),
                                    hooks=bool(module_hooks(a)), plugins=getattr(a, "opencode2_plugins", None) or ())


def check_model_effort(a):
    """--model and --effort: a model name (no spaces or options), an effort level the agent knows and - for a model of
    the table of cloud models whose levels it knows - one that model takes, else a warning (the Board and the web
    interface warn with the same words: cloud_models.effort_problem), not together with --llm's own model, and not given twice
    (as an option and in the agent's arguments)."""
    model, effort = getattr(a, "model", None), getattr(a, "effort", None)
    if model is not None:
        if not re.fullmatch(MODEL_NAME, model):
            raise ValueError(f"--model expects a model name (letters, digits, . _ : / @ + -): {model!r}")
        if a.llm:
            raise ValueError("--model and --llm: name the model in --llm NAME:MODEL instead")
    if effort is not None:
        levels = AGENT_EFFORTS.get(a.agent, ())
        if not levels:
            where = {"dsh": "a model route's reasoningEfforts in a cordis.patch.yml",
                     "letta": "its /model command, per model",
                     "opencode2": "opencode.json, a model's variants"}.get(a.agent, "opencode.json, a model's options")
            raise ValueError(f"--effort: {a.agent} sets its effort per model in its own configuration "
                             f"({where}); start without --effort")
        if effort not in levels:
            raise ValueError(f"--effort for {a.agent} is one of {', '.join(levels)}: {effort!r}")
        if model is not None:
            # A level the table says this model does not take: a warning, not a refusal - the table may lag behind
            # the agent, which lowers or refuses the level itself (the Board says the same as a warning).
            problem = CLOUD_MODELS.effort_problem(a.agent, model, effort)
            if problem:
                print(f"agent-sandbox: warning: {problem} (passed on: the agent lowers or refuses it itself)",
                      file=sys.stderr)
    given =a.agent_args[:a.agent_args.index("--")] if "--" in a.agent_args else a.agent_args
    # nanobot's -m is its one message (nanobot -m TEXT), not a model.
    model_flags = ("--model",) if a.agent == "nanobot" else ("--model", "-m")
    for option, value, flags in (("--model", model, model_flags), ("--effort", effort, ("--effort",))):
        if value is not None and any(arg in flags or arg.startswith(f"{flags[0]}=") for arg in given):
            raise ValueError(f"{option} is given twice: as the launcher's option and in the agent's arguments")


def check_show_thinking(a):
    """--show-thinking: refused for an agent without a switch for it (THINKING_NONE says what it offers), and for
    Codex not together with the same keys in the agent's own arguments."""
    value = getattr(a, "show_thinking", None)
    if value is None:
        return
    if a.agent in THINKING_NONE:
        raise ValueError(f"--show-thinking: {THINKING_NONE[a.agent]}; start without --show-thinking")
    given = a.agent_args[:a.agent_args.index("--")] if "--" in a.agent_args else a.agent_args
    if a.agent == "codex" and any(re.match(r"(?:hide_agent_reasoning|show_raw_agent_reasoning)\s*=", arg)
                                  for arg in given):
        raise ValueError("--show-thinking is given twice: as the launcher's option and as Codex's -c "
                         "hide_agent_reasoning/show_raw_agent_reasoning")


def thinking_shown(a):
    """True (shown) or False (hidden) when --show-thinking chose for this agent, else None (the agent's default)."""
    value = getattr(a, "show_thinking", None)
    if value is None or a.agent not in AGENT_THINKING or getattr(a, "helper_of", None):
        return None
    return value == "on"


def thinking_args(a):
    """The agent's own arguments for --show-thinking (Claude Code's --settings, Codex's -c keys); the others take it
    from their configuration (OpenCode's kv.json, Hermes', Goose's) or sandbox-ai's chat (DSH)."""
    shown = thinking_shown(a)
    if shown is None or a.binary is not None or a.probe:
        return []
    if a.agent == "claude":
        return ["--settings", json.dumps({"showThinkingSummaries": shown})]
    if a.agent == "codex":
        return ["-c", f"hide_agent_reasoning={'false' if shown else 'true'}",
                "-c", f"show_raw_agent_reasoning={'true' if shown else 'false'}"]
    return []


def thinking_plan(a):
    """The dry run's "show_thinking": the choice, the agent's default and its switch (or why it has none)."""
    switch = AGENT_THINKING.get(a.agent)
    return {"value": getattr(a, "show_thinking", None), "agent_default": switch[0] if switch else None,
            "switch": switch[1] if switch else None, "none": THINKING_NONE.get(a.agent),
            "arguments": thinking_args(a)}


OPENCODE_KV = ".local/state/opencode/kv.json"  # OpenCode's TUI state (XDG_STATE_HOME/opencode): thinking_mode


def opencode_thinking(home, shown):
    """OpenCode's thinking_mode ("show" or "hide") in the sandbox HOME's kv.json, the rest of it kept."""
    def apply(document):
        mode = "show" if shown else "hide"
        if document.get("thinking_mode") == mode:
            return False
        document["thinking_mode"] = mode
        return True
    problem = update_json_defaults(home, OPENCODE_KV, apply)
    if problem:
        print(f"agent-sandbox: --show-thinking: left sandbox ~/{OPENCODE_KV} unchanged ({problem})", file=sys.stderr)


def opencode2_thinking_apply(shown):
    """apply(document) for update_json_defaults: OpenCode 2's session.thinking and mini.thinking ("show" or "hide")
    in its cli.json DOCUMENT, every other key kept; whether it changed anything."""
    mode = "show" if shown else "hide"

    def apply(document):
        changed = False
        for section in ("session", "mini"):
            part = document.get(section)
            if isinstance(part, dict) and part.get("thinking") == mode:
                continue
            document[section] = {**(part if isinstance(part, dict) else {}), "thinking": mode}
            changed = True
        return changed
    return apply


def pi_thinking_apply(shown):
    """apply(document) for update_json_defaults: Pi's hideThinkingBlock (the opposite of SHOWN) in its settings.json
    DOCUMENT, every other setting kept; whether it changed anything."""
    def apply(document):
        if document.get("hideThinkingBlock") is (not shown):
            return False
        document["hideThinkingBlock"] = not shown
        return True
    return apply


# --show-thinking of the agents that keep it in a settings file of the user's (AGENT_THINKING): {agent: (the file in
# the sandbox HOME, apply(shown))}. The rest of the file stays, and what the agent's own toggle saves there later too.
# OpenCode 2 migrates OpenCode 1's tui.json and kv.json into its cli.json only while it has none.
HOME_THINKING = {"opencode2": (".config/opencode/cli.json", opencode2_thinking_apply),
                 "pi": (".pi/agent/settings.json", pi_thinking_apply)}


def home_thinking(home, agent, shown):
    """--show-thinking of AGENT (HOME_THINKING: OpenCode 2's cli.json, Pi's settings.json) in the sandbox HOME, the
    rest of the file kept; a file that cannot be read is left as it is, and the start says so."""
    relative, apply = HOME_THINKING[agent]
    problem = update_json_defaults(home, relative, apply(shown))
    if problem:
        print(f"agent-sandbox: --show-thinking: left sandbox ~/{relative} unchanged ({problem})", file=sys.stderr)


def model_effort_args(a):
    """The agent's own arguments for --model and --effort."""
    args, model, effort = [], getattr(a, "model", None), getattr(a, "effort", None)
    # DSH's model: its default-model row in the patch (dsh.model_rows); Goose's: GOOSE_MODEL (goose.environment);
    # OpenHands': its settings (openhands.llm_settings);
    # nanobot's: agents.defaults.model in sandbox-ai's settings (nanobot.managed_config);
    # Letta Code's and Pi's with --llm: its provider's model (llm.agent_arguments).
    # OpenCode 2's: its configuration layer's "model" (opencode2.config_content; its TUI has no model option).
    if model and a.agent not in ("dsh", "goose", "openhands", "nanobot", "opencode2") and not (a.agent in ("letta", "pi") and getattr(a, "llm", None)):
        args += ["--model", model]
    if effort:  # Qwen Code's: model.reasoningEffort in its system settings (qwen.managed_settings)
        if a.agent == "claude":
            args += ["--effort", effort]
        elif a.agent == "codex":
            args += ["-c", f"model_reasoning_effort={effort}"]
        elif a.agent == "hermes":
            args += ["--reasoning", effort]
        elif a.agent == "pi":
            args += ["--thinking", effort]
    return args


def agent_command(a, binary):
    args = list(a.agent_args)
    if a.agent == "codex" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        # Just before your own arguments (-c is global: before a subcommand too), after everything the launcher
        # adds: over every config.toml, telemetry is always off.
        args = [*codex_telemetry_args(telemetry_level(a)), *args]
        # The network note as a developer message of its own: over your config.toml's developer_instructions for
        # this run; your own -c developer_instructions=... after it wins.
        # (joined after your own developer_instructions of the sandbox HOME's config.toml: codex_own_instructions)
        if start_note(a):
            args = ["-c", "developer_instructions=" + toml_string(
                joined_text(getattr(a, "codex_own_instructions", None), start_note(a))), *args]
    # A helper's command is its server (python3): agent_mcp.py gives the agent its flags for each task.
    if a.dangerously_skip_permissions and not a.probe and not getattr(a, "helper_of", None):
        args = [flag for flag in DANGER_ARGS[a.agent] if flag not in args] + args
    # Codex's native sandbox cannot create its UID map inside the verified
    # outer namespaces. Use the external boundary unless explicitly overridden.
    if a.agent == "codex" and a.binary is None and not a.probe:
        options = args[:args.index("--")] if "--" in args else args
        explicit = any(value in ("--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox")
                       or value.startswith("--sandbox=") or value.startswith("-s") and len(value) > 2
                       for value in options)
        if not explicit:
            args = ["--sandbox", "danger-full-access", *args]
    if a.mcp and a.binary is None and not a.probe:
        args = [*mcp_agent_args(a), *args]
    launchers = []
    if a.llm and a.binary is None and not a.probe:
        launchers = list(load_llm_module().agent_arguments(a))
        args = [*launchers, *args]
    if a.agent == "qwen" and a.binary is None and not getattr(a, "helper_of", None) and start_note(a):
        # Qwen Code takes one --append-system-prompt: the loop guard's text (--llm) and the start's note share it.
        if launchers[:1] == ["--append-system-prompt"]:
            args[1] = joined_text(args[1], start_note(a))
        else:
            args = ["--append-system-prompt", start_note(a), *args]
    if a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        args = [*model_effort_args(a), *thinking_args(a), *args]
    if a.agent == "qwen" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        # While rules hold no extension loads: their hooks would run after the rule check (qwen.extension_args)
        args = [*load_qwen_module().extension_args(a, hooks_active(a)), *args]
    if a.agent == "dsh" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        # sandbox-ai's patch after the profile; none: the chat in the terminal (its web UI beside it with --browser,
        # on a loopback port nothing else takes)
        args = load_dsh_module().command_args(args, dsh_web_port(a))
        # The chat or the web UI through their window's script: what DSH offers said, the web UI's URL masked or
        # opened in the session browser.
        return load_dsh_module().command(a, binary, args, dsh_session_browser(a), dsh_web_port(a))
    if a.agent == "goose" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        return load_goose_module().command(a, binary, args)  # the enabled background parts first, then Goose
    if a.agent == "openhands" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        args = load_openhands_module().command_args(args)  # none: the Canvas on the sandbox's loopback
    if a.agent == "nanobot" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        args = load_nanobot_module().command_args(args)  # nanobot webui: --no-open (no browser in its container)
    if a.agent == "letta" and a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        return load_letta_module().command(a, binary, args)  # sandbox-ai's start script, then Letta Code
    if hermes_managed(a):  # Hermes' shared-metrics question answered first, in every mode (hermes.start_command)
        return load_hermes_module().start_command(binary, args)
    return [binary, *args]


AGENT_RESTART_FILE = "agent-restart.sh"  # in the session directory: /sandbox/session/agent-restart.sh, read-only
# The agents whose MCP servers are on their command line only (Codex: -c mcp_servers...): a server attached while the
# run goes on (sandbox-ai mcp attach --live) is on the command line of its restart inside the sandbox through the
# session's AGENT_LIVE_MCP_FILE (written by mcp.py, one word per line; read by the restart script). Reported live: a
# Codex session given a server while it ran was restarted with `resume --restart` - its sandbox ended (exit 143).
LIVE_MCP_ARG_AGENTS = ("codex",)
AGENT_LIVE_MCP_FILE = "agent-live-mcp.args"
AGENT_LIVE_MCP_TARGET = f"/sandbox/session/{AGENT_LIVE_MCP_FILE}"


def agent_restart_script(a, binary):
    """The shell script (or None) `sandbox-ai kits restart-agent` runs in the agent's tmux window (tmux respawn-window,
    through the sandbox's shell relay: inside the agent's confinement, never on the host) to start the agent again
    in the sandbox that goes on running - its network, its MCP servers' relays and its kits stay; its conversation
    continues (sessions.restart_args: the agent's continue flags, its options kept, a one-shot task or a first prompt
    not given again). The agent's command as the start built it (agent_command, companion_prelude) with those
    arguments, and its exit status written for the session's end as the start's window writes it. Only for a
    detached tmux session (a kept start's terminal) that runs the agent itself. For LIVE_MCP_ARG_AGENTS the words of
    the servers attached live (AGENT_LIVE_MCP_TARGET, one per line, when there is one) come right after the agent's
    program: Codex's -c options are global, before its subcommand."""
    if not a.tmux_socket_dir or not runs_agent(a) or getattr(a, "helper_of", None) or a.probe:
        return None
    again = copy.copy(a)
    again.agent_args = load_sessions_module().restart_args(
        {"agent": a.agent, "agent_args": list(a.agent_args), "options": [], "ready": True})
    command = with_companion_prelude(again, agent_command(again, binary))
    line, live = shlex.join(command), ""
    if a.agent in LIVE_MCP_ARG_AGENTS and binary in command:
        at = command.index(binary) + 1
        live = (f"set --\nif [ -f {AGENT_LIVE_MCP_TARGET} ]; then\n"
                f'  while IFS= read -r word; do set -- "$@" "$word"; done < {AGENT_LIVE_MCP_TARGET}\nfi\n')
        line = shlex.join(command[:at]) + ' "$@"' + (" " + shlex.join(command[at:]) if command[at:] else "")
    return ("# sandbox-ai: the agent started again in its tmux window (sandbox-ai kits restart-agent); its conversation "
            "continues\n" + live + line + f'\nprintf "%s %s" "$?" "$(date +%s)" > {RUNTIME.AGENT_EXIT}\n')


def toolchain_mounts(a, session):
    """[(host, sandbox, "ro")]: the session's folder toolchains/ at /opt/toolchains (every start has it: the wrappers
    of the toolchains' commands, their variables, and the mount point `sandbox-ai toolchain attach` mounts one on
    while the sandbox runs), the store's tree of each --toolchain on it, and - with Podman - the file that puts the
    wrappers' folder back on a login shell's PATH (the image's /etc/profile resets it). [] for a session directory
    without the folder (prepare_toolchains made none)."""
    module = load_toolchains_module()
    folder = module.session_folder(session)
    if not folder.is_dir():
        return []
    found = [(folder, module.MOUNT, "ro")]
    found += [(source, target, "ro") for source, target in module.start_mounts(toolchain_entries(a))]
    if a.backend == "podman":
        found.append((folder / "profile.sh", module.PROFILE_TARGET, "ro"))
    return found


def prepare_toolchains(a, session):
    """The session's folder toolchains/ (toolchains.write_session): the wrappers and variables of the start's
    toolchains, each mount point. The notes on commands two toolchains name."""
    return load_toolchains_module().write_session(load_toolchains_module().session_folder(session),
                                                  toolchain_entries(a))


def mount_option(src, dest, mode, nosuid=True):
    if any(c in str(src) + dest for c in (",", "\n", "\r")):
        raise ValueError("Podman bind paths cannot contain commas or newlines")
    # nodev: device files inside a mounted tree stay unusable (devices only through --device). nosuid: a setuid or
    # setgid file, or one with file capabilities, in a folder of yours gives nothing inside the sandbox - whoever
    # made it (the agent in an earlier run, one of its IDs with --nested-containers, you). The agent's processes
    # cannot gain a privilege anyway (no_new_privs); the supervisor's side is not run from these folders. Every
    # start has it since 0.1.121 (runtime.MOUNT_FLAGS: the guard looks at the flags inside Firejail).
    return ["--mount", f"type=bind,src={src},dst={dest},{'ro' if mode == 'ro' else 'rw'},nodev"
                       + (",nosuid" if nosuid else "")]


SYSCALL_FILTER_ENV = "SANDBOX_AI_SYSCALL_FILTER"  # off: Firejail's filter alone, as before 0.1.121
# What is left on the disk behind a folder a run writes, or behind its records, before it is said (the modes up to
# medium) or the run is ended (high, paranoid): bytes, and files that can still be made. (runtime.watch_disk)
DISK_RESERVE = {"warn": (256 << 20, 2000), "stop": (512 << 20, 5000)}


def strict_mode(a):
    return getattr(a, "mode", None) in MODES.STRICT


def lsm_value_ok(value):
    """The policy's "lsm": one of {"apparmor": PROFILE} and {"selinux": TYPE}, the name plain (it becomes part of
    an option of Podman)."""
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in ("apparmor", "selinux") and \
        isinstance(next(iter(value.values())), str) and \
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", next(iter(value.values()))) is not None and \
        next(iter(value.values())) != "unconfined"


def lsm_options(a):
    """Podman's options for the container's confinement by the host's security module. Without the administrator's
    policy none is applied (the supervisor mounts, makes namespaces and loads the firewall, which the engines' ready
    profiles refuse, and rootless Podman can load none of its own): AppArmor unconfined, SELinux labels off. The
    policy's "lsm" names the one the administrator made for sandboxes: the container is started in it, and its
    supervisor starts nothing when it finds itself outside (runtime.lsm_problem)."""
    wanted = getattr(a, "policy_lsm", None) or {}
    return ["--security-opt=apparmor=" + wanted.get("apparmor", "unconfined"),
            "--security-opt=label=" + (f"type:{wanted['selinux']}" if "selinux" in wanted else "disable")]


def syscall_filter(a, environ=None):
    """"on" or "off": sandbox-ai's own system call filters for this start (runtime.own_filter: io_uring, new
    namespaces, socket families, calls a newer kernel adds). On, unless you switch them off for a program that
    needs what they refuse (SANDBOX_AI_SYSCALL_FILTER=off: Firejail's filter alone, as before 0.1.121) - which
    the security modes high and paranoid refuse."""
    value = (os.environ if environ is None else environ).get(SYSCALL_FILTER_ENV, "").strip().lower() or "on"
    if value not in RUNTIME.SYSCALL_FILTERS:
        raise ValueError(f"{SYSCALL_FILTER_ENV} is one of {', '.join(RUNTIME.SYSCALL_FILTERS)}, not {value!r}")
    if value == "off" and strict_mode(a):
        raise ValueError(f"{SYSCALL_FILTER_ENV}=off widens the security mode {a.mode}: the agent would reach io_uring, "
                         "new namespaces and every socket family of your host's kernel; start without it, or in "
                         "the mode medium")
    return value


def host_folders(a):
    """{sandbox path: flags} of the host folders this start gives the agent (its HOME, the workspaces, --mount):
    what each must be mounted with, for the guard to check inside Firejail (runtime.check_mounts)."""
    targets = ["/root", *(dest for _, dest in workspace_targets(a)), *(parse_mount(value)[1] for value in a.mount)]
    return {dest: list(RUNTIME.MOUNT_FLAGS) for dest in dict.fromkeys(targets)}


def disk_watch(a):
    """What the supervisor watches the disks for (runtime.watch_disk): the folders the agent writes (its HOME, the
    writable workspaces and mounts) and the run's own records (log and control directory), the reserve, and what
    happens below it - said in the modes up to medium, the run ended in high and paranoid."""
    paths = ["/root", *(dest for index, (_, dest) in enumerate(workspace_targets(a))
                        if workspace_mode(a, index) == "rw"),
             *(parse_mount(value)[1] for value in a.mount if parse_mount(value)[2] == "rw"),
             str(LOG_DIR), str(CONTROL_DIR)]
    act = "stop" if strict_mode(a) else "warn"
    reserve, files = DISK_RESERVE[act]
    return {"paths": list(dict.fromkeys(paths)), "reserve": reserve, "files": files, "act": act}


def isolation_config(a):
    """The session configuration's "isolation" (runtime.isolation_settings): what the supervisor and the guard hold
    the run's isolation to. "strict": a mode in which nothing starts without each part."""
    wanted = getattr(a, "policy_lsm", None)
    return {"strict": strict_mode(a), "syscall_filter": syscall_filter(a), "mounts": host_folders(a),
            "disk": disk_watch(a), **({"lsm": dict(wanted)} if wanted else {})}


def host_lsm(security=Path("/sys/kernel/security"), selinux=Path("/sys/fs/selinux/enforce")):
    """The security modules of this host that confine programs: ["apparmor"], ["selinux"], both or none."""
    try:
        active = set(security.joinpath("lsm").read_text().strip().split(","))
    except OSError:
        active = set()
    found = [name for name in ("apparmor", "selinux") if name in active]
    if "selinux" not in found and selinux.exists():
        found.append("selinux")
    return found


def isolation_plan(a):
    """The dry run's account of the run's isolation: the filters by name with their SHA-256 on this machine, what
    the host folders are mounted with, the disk watch, and the container's confinement."""
    filters = {}
    if syscall_filter(a) == "on" and os.uname().machine in RUNTIME.SECCOMP_MACHINES:
        for name, policy in (("container", RUNTIME.seccomp_policy("container")),
                             ("sandbox", RUNTIME.seccomp_policy("sandbox", nested=nested(a)))):
            account = RUNTIME.seccomp_account(policy)
            filters[name] = {key: account[key] for key in ("sha256", "instructions", "new_namespaces",
                                                           "socket_families", "netlink_protocols", "unknown_calls")}
    return {"strict": strict_mode(a), "syscall_filter": syscall_filter(a), "filters": filters,
            "firejail": ["--seccomp", "--caps.drop=all", "--nonewprivs",
                         *RUNTIME.firejail_narrowing("--restrict-namespaces --protocol=", nested(a),
                                                     {"syscall_filter": syscall_filter(a)},
                                                     BWRAP_FIREJAIL_HELPERS if a.backend == "bwrap" else None)],
            "host_folders": host_folders(a), "disk": disk_watch(a),
            "lsm": getattr(a, "policy_lsm", None) or "unconfined",
            "manifest": f"{RUNTIME.ISOLATION_FILE} in the run's log directory (what the supervisor found at the start)"}


# With bwrap Firejail runs the host's own helpers, which belong to a root the sandbox's user namespace does not map
# (runtime.firejail_helpers_unusable): its --protocol and --restrict-namespaces are left out there.
BWRAP_FIREJAIL_HELPERS = "bwrap: Firejail's helpers belong to the host's root, whom the sandbox does not map"


def isolation_notes(a):
    """What a start says about its isolation: that you switched sandbox-ai's own filters off, and - in the modes
    high and paranoid, which are chosen for what they hold - what this host does not give them."""
    notes = []
    if syscall_filter(a) == "off":
        notes.append(f"warning: {SYSCALL_FILTER_ENV}=off: sandbox-ai's own system call filters are not installed - "
                     "the agent reaches io_uring, new namespaces and every socket family of this host's kernel "
                     "(Firejail's filter alone, as before 0.1.121)")
    if strict_mode(a) and not getattr(a, "policy_lsm", None) and local_engine(a) and sys.platform != "darwin":
        modules = " or ".join({"apparmor": "AppArmor", "selinux": "SELinux"}[name] for name in host_lsm())
        notes.append(f"note: security mode {a.mode}: the sandbox is not confined by {modules or 'AppArmor or SELinux'}"
                     " (rootless Podman loads no profile of its own; the administrator's policy key \"lsm\" names "
                     "one, see sandbox-ai doctor); its namespaces, its filters and its empty capability set hold "
                     "without it")
    if strict_mode(a):
        notes.append(f"note: security mode {a.mode}: nothing but the disk limits what the run writes (a quota is "
                     f"the host's to set); it is ended when less than {DISK_RESERVE['stop'][0] >> 20} MiB are free "
                     "behind a folder it writes or behind its records")
    return notes


def pod_commands(a, name, session, home):
    create = podman_command(a, "pod", "create", "--name", name,
              "--infra-image", a.image, "--infra-command", "/usr/bin/sleep infinity",
              # Rootless: the user namespace of rootless Podman itself, root in it = the host user, the
              # rest = the delegated subuids (what keep-id:uid=0,gid=0 means; Podman before 5.2 writes a
              # zero-length gid_map line for that and fails, e.g. 4.9 on Ubuntu 24.04).
              "--userns=host", "--share=net,ipc,uts",
              # ping without CAP_NET_RAW (ICMP datagram sockets) for the sandbox's root group, as Podman
              # sets for single containers; a pod's network namespace does not get it on Podman 4.
              "--sysctl=net.ipv4.ping_group_range=0 0",
              "--network", pod_network(a))
    if a.gui or a.gui_wayland or getattr(a, "browser_container", None):
        # Chromium-based apps exhaust Podman's default 64 MB /dev/shm.
        create += ["--shm-size=1g"]
    owner = pod_owner(a)
    if owner:
        create += ["--label", f"{SESSION_LABEL}={owner}"]
    # sandbox-ai's own pod (liveness.py): `sandbox-ai gc` removes it once it is left over, never anyone else's.
    create += ["--label", f"{RUN_LABEL}={name[len('agent-'):] if name.startswith('agent-') else name}"]
    if a.ip:
        create += ["--ip", a.ip]
    if a.ip6:
        create += ["--ip6", a.ip6]
    run = podman_command(a, "run", "--rm", "--pull=never", "--pod", name, "--user=0:0",
           "--read-only", "--cap-drop=ALL", "--security-opt=seccomp=unconfined", *lsm_options(a),
           "--tmpfs=/run:rw,nosuid,nodev", f"--tmpfs=/tmp:rw,nosuid,nodev{tmp_mount_size(a)}",
           f"--tmpfs=/var/tmp:rw,nosuid,nodev{tmp_mount_size(a)}",
           f"--tmpfs={WORKSPACES_DIR}:{WORKSPACES_TMPFS}", "--workdir=/work", "--interactive")
    limits = enforced_limits(a)[0]
    if "memory" in limits:
        run += ["--memory", limits["memory"]]
    if "cpus" in limits:
        run += ["--cpus", limits["cpus"]]
    if "pids" in limits:
        run += ["--pids-limit", str(limits["pids"])]
    # The bootstrap's DNS logging stub binds port 53; Firejail drops it again.
    if a.pod_name or on_machine(a):
        # Named so that `sandbox-ai attach` can exec into it (macOS: also `sandbox-ai net`, run.json "container").
        run += ["--name", f"{name}-agent"]
    for cap in CAPS + (["NET_BIND_SERVICE"] if a.log_dir else []):
        run += ["--cap-add", cap]
    if nested(a):
        # The supervisor maps the sandbox's root into the agent's own user namespace (runtime.nested_setup): the
        # kernel asks CAP_SETFCAP for that. Firejail drops it with all the others before the agent runs.
        run += ["--cap-add", "SETFCAP"]
        for path in nested_devices(a):
            run += ["--device", path]
    if sys.stdin.isatty() and sys.stdout.isatty():
        run += ["--tty"]
    bindings = [(HERE / "runtime.py", "/sandbox/runtime.py", "ro"),
                # The Public Suffix List beside it: the supervisor checks host wildcards against it (runtime.py).
                *([(HERE / "public_suffix_list.dat", "/sandbox/public_suffix_list.dat", "ro")]
                  if (HERE / "public_suffix_list.dat").is_file() else []),
                (session, "/sandbox/session", "ro"), (home, "/root", "rw"),
                *((Path(src).expanduser().resolve(), dest, workspace_mode(a, index))
                  for index, (src, dest) in enumerate(workspace_targets(a))),
                *hidden_mounts(a, session),  # --hide: over what it matches in the workspaces, after them
                (session / "hosts", "/etc/hosts", "ro"),
                (session / "resolv.conf", "/etc/resolv.conf", "ro"),
                (session / "nsswitch.conf", "/etc/nsswitch.conf", "ro")]
    bindings += toolchain_mounts(a, session)
    bindings += [parse_mount(value) for value in a.mount]
    # Kits after /root, so that they lie on top of the sandbox HOME.
    bindings += [(src, dest, mode) for src, dest, mode in getattr(a, "kit_mounts", [])]
    if a.log_dir:
        bindings.append((a.log_dir.expanduser().resolve(), str(LOG_DIR), "rw"))
    if a.gui_vnc:
        bindings.append((a.gui_vnc.expanduser().resolve(), str(VNC_DIR), "rw"))
    if a.tmux_socket_dir and on_machine(a):
        # macOS: the terminal relay's socket stays in the VM (a socket does not cross its shared folders); the host
        # attaches through podman exec (sessions.attach).
        run += [f"--tmpfs={TMUX_DIR}:rw,nosuid,nodev,mode=0700"]
    elif a.tmux_socket_dir:
        bindings.append((a.tmux_socket_dir.expanduser().resolve(), TMUX_DIR, "rw"))
    if a.mcp_socket_dir and on_machine(a):
        # macOS: an MCP server's socket in a Podman volume in the VM, where its agents' sandboxes reach it (a socket
        # does not cross the VM's shared folders).
        run += ["--mount", f"type=volume,src={mcp_volume(a.mcp_socket_dir)},dst={MCP_SERVE_DIR}"]
    elif a.mcp_socket_dir:
        bindings.append((a.mcp_socket_dir.expanduser().resolve(), MCP_SERVE_DIR, "rw"))
    if a.mcp_approvals_dir:
        bindings.append((a.mcp_approvals_dir.expanduser().resolve(), MCP_APPROVALS_DIR, "ro"))
    for name in dict.fromkeys(a.mcp):  # reached through the supervisor's relay (MCP_AGENT_DIR), never directly
        if on_machine(a):
            run += ["--mount", f"type=volume,src={mcp_volume(mcp_socket_dir(mcp_instance(a, name)))},"
                               f"dst={MCP_UPSTREAM_DIR}/{name},ro"]
        else:
            bindings.append((mcp_socket_dir(mcp_instance(a, name)), f"{MCP_UPSTREAM_DIR}/{name}", "ro"))
    if hook_mount(a, session):  # the agent's managed configuration: its hooks, which it cannot change
        bindings.append((*hook_mount(a, session), "ro"))
    if hermes_guarded(a):  # Hermes' telemetry guard in its site-packages: never switchable, not even in the sandbox
        bindings += [(source, target, "ro") for source, target in load_hermes_module().guard_mounts(session)]
    for source, target in companion_hook_mounts(a, session):
        bindings.append((source, target, "ro"))
    for source, target in companion_managed_mounts(a, session):  # Hermes', Qwen Code's ... --with: read-only too
        bindings.append((source, target, "ro"))
    # The guard of the agents' commands and their managed configuration: telemetry never switchable in the sandbox.
    bindings += [(source, target, "ro") for source, target in telemetry_guard_mounts(a, session)]
    if a.gui_wayland:
        bindings.append((wayland_socket(), WAYLAND_TARGET, "rw"))
    if getattr(a, "proxy_dir", None):
        bindings.append((a.proxy_dir, str(PROXY_DIR), "ro"))
        if package_guard_mount(a):  # the package guard's lists, for the proxy alone (hidden from the agent)
            bindings.append((package_guard_mount(a), str(PACKAGES_DIR), "ro"))
    if nested(a):
        bindings.append((session / "containers" / "containers.conf", NESTED_CONF_DROPIN, "ro"))
    if getattr(a, "control_dir", None):
        bindings.append((a.control_dir, str(CONTROL_DIR), "rw"))
    if getattr(a, "browser_dir", None) and on_machine(a):
        # macOS: the supervisor serves the browser socket in the VM (runtime.browser_relay), the host reads requests
        # from the control directory.
        run += [f"--tmpfs={BROWSER_DIR}:rw,nosuid,nodev,mode=0755"]
    elif getattr(a, "browser_dir", None):
        bindings.append((a.browser_dir, str(BROWSER_DIR), "ro"))
    for src, dest, mode in bindings:
        run += mount_option(src, dest, mode)
    run += load_cairn_module().agent_mounts(a, launcher_namespace())  # --cairn: the relay and its socket's folder
    for path, kind in devices(a):
        # A directory as a bind mount, not --device: Podman would copy only the devices present now.
        run += ["--mount", f"type=bind,src={path},dst={path},rw,dev,nosuid"] \
            if kind == "dir" else ["--device", path]
    for key, value in base_env().items():
        run += ["--env", f"{key}={value}"]
    run += [f"--entrypoint={RUNTIME.SUPERVISOR_PYTHON[0]}", a.image, *RUNTIME.SUPERVISOR_PYTHON[1:],
            "/sandbox/runtime.py", "--internal-bootstrap"]
    return create, run


# --nested-containers: the agent runs containers itself as root of a user namespace of its own, which the supervisor
# makes before Firejail starts (runtime.nested_setup). The devices the engines inside need, where this host has them
# (without /dev/fuse the images are stored by copying, "vfs"):
NESTED_DEVICES = ("/dev/fuse", "/dev/net/tun")
NESTED_ENV = {"CONTAINERS_STORAGE_CONF": "/sandbox/session/containers/storage.conf",
              # A build's RUN steps in a chroot of their own (no further namespaces of their own to set up).
              "BUILDAH_ISOLATION": "chroot"}
# The engines' configuration for the sandbox: a drop-in on top of the image's own containers.conf (CONTAINERS_CONF
# would replace it, and with it the distribution's choice of network backend and helper paths: Podman then looked
# for programs the image does not have and failed with "no such file or directory").
NESTED_CONF_DROPIN = "/etc/containers/containers.conf.d/90-sandbox-ai-nested.conf"
# Where the images and containers inside are kept: the sandbox's /tmp, a tmpfs (--tmp-size) that goes with the run,
# so that nothing owned by your subordinate IDs is left in a HOME or a workspace. (Not /var/tmp: inside Firejail that
# is a tmpfs of Firejail's own, noexec.)
NESTED_GRAPHROOT = "/tmp/containers/storage"


def nested(a):
    return bool(getattr(a, "nested_containers", False))


def nested_reserved_ids():
    """IDs that nothing in the agent's own user namespace maps to: the connection proxy's (the redirect to the proxy
    skips its connections), PulseAudio's and tor's (the Tor kill switch lets tor's connections out)."""
    return sorted({int(RUNTIME.PROXY_UID), int(RUNTIME.AUDIO_UID), int(load_vpn_module().runtime().TOR_UID)})


def nested_devices(a):
    """NESTED_DEVICES this host has (on macOS and with --podman-url the engine's machine is asked, by Podman)."""
    return [path for path in NESTED_DEVICES if not local_engine(a) or os.path.exists(path)]


def nested_storage(a):
    return "overlay" if "/dev/fuse" in nested_devices(a) else "vfs"





def check_nested(a):
    """--nested-containers: Podman only, never behind Tor, and /dev/net/tun for the containers' networks."""
    if not nested(a):
        return
    if a.backend != "podman":
        raise ValueError("--nested-containers needs the Podman backend (the agent's user namespace is made in the "
                         "pod's container)")
    if a.podman_url:
        raise ValueError("--nested-containers with --podman-url: the devices it needs are checked on this host; "
                         "start it on the machine of that Podman service")
    if tor_run(a):
        raise ValueError(f"--nested-containers is refused behind Tor (--vpn {a.vpn}): the kill switch would still "
                         "hold (tor's user is never mapped for the agent), but the namespaces and system calls it "
                         "opens reach far more of the kernel, and one kernel bug would show your address; start "
                         "without one of them")
    if local_engine(a) and "/dev/net/tun" not in nested_devices(a):
        raise ValueError("--nested-containers needs /dev/net/tun on this host (the containers' networks: slirp4netns, "
                         "pasta): sudo modprobe tun")


def nested_files(a):
    """{name: text} in the session directory (NESTED_ENV): the engines' configuration inside the sandbox."""
    driver = nested_storage(a)
    containers = "\n".join([
        "# sandbox-ai --nested-containers: Podman runs as root of the agent's own user namespace. The containers",
        "# share the agent's network namespace (their connections meet the sandbox's firewall and connection proxy",
        "# like the agent's own); --network slirp4netns or pasta run there too. No cgroups of their own: the",
        "# sandbox's limits hold for all of them. Each container has an IPC namespace of its own, made in the",
        "# agent's (its user namespace's, where its root may mount mqueue; the sandbox's own is the pod's: crun EPERM).",
        "[containers]", 'netns = "host"', 'utsns = "host"', 'ipcns = "private"', 'cgroupns = "host"',
        'cgroups = "disabled"', 'log_driver = "k8s-file"', "pids_limit = 0", "default_sysctls = []",
        'apparmor_profile = "unconfined"', "label = false", "keyring = false",
        "[engine]", 'cgroup_manager = "cgroupfs"', 'events_logger = "file"', ""])
    storage = ["[storage]", f'driver = "{driver}"', 'runroot = "/run/containers/storage"',
               f'graphroot = "{NESTED_GRAPHROOT}"']
    if driver == "overlay":
        storage += ["[storage.options.overlay]", 'mount_program = "/usr/bin/fuse-overlayfs"', 'mountopt = "nodev"']
    return {"containers/containers.conf": containers, "containers/storage.conf": "\n".join(storage) + "\n"}


def nested_writable(a):
    """The sandbox paths of the host folders the agent may write (the writable workspaces, rw --mount, its HOME and,
    for a session, the tmux socket's folder): where the supervisor gives back, at the end of a nested run, what one
    of the agent's other IDs left there (runtime.give_back)."""
    paths = [dest for index, (_, dest) in enumerate(workspace_targets(a)) if workspace_mode(a, index) == "rw"]
    paths += [parse_mount(value)[1] for value in a.mount if parse_mount(value)[2] == "rw"]
    paths.append("/root")
    if a.tmux_socket_dir and not on_machine(a):
        paths.append(TMUX_DIR)
    return list(dict.fromkeys(paths))


def nested_plan(a):
    """The dry run's account of --nested-containers, or None."""
    if not nested(a):
        return None
    return {"user_namespace": "the agent is root of a user namespace of its own (its capabilities count only there)",
            "reserved_ids": nested_reserved_ids(), "capabilities_added": ["SETFCAP"], "devices": nested_devices(a),
            "seccomp": "Firejail's default filter but " + ", ".join(
                name.lstrip("!") for name in RUNTIME.NESTED_SECCOMP.partition("=")[2].split(",")),
            "storage": {"driver": nested_storage(a), "graphroot": NESTED_GRAPHROOT},
            "network": "the agent's own: its firewall and connection proxy", "environment": dict(NESTED_ENV),
            "ipc": "a namespace of the agent's own user namespace (System V IPC and mqueue shared with nothing "
                   "outside; with --gui no MIT-SHM)",
            "containers_conf": NESTED_CONF_DROPIN,
            "host_folders": "every bind mount nosuid and nodev (a setuid file made there gives nothing inside)",
            "given_back": {"paths": nested_writable(a),
                           "what": "files and folders the run left owned by another of the sandbox's IDs: given "
                                   "back to you (your user and group, setuid and setgid bits cleared) when it ends; "
                                   "after a run that was killed: podman unshare chown -R 0:0 PATH"}}


USERNS_RESTRICTION = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")  # Ubuntu 23.10 and later


def nested_notes(a):
    if not nested(a):
        return []
    notes = ["--nested-containers: the agent may run containers (podman, buildah, unshare -rn); it is root of a user "
             "namespace of its own, which reaches more of your host's kernel; its containers' connections pass the "
             "sandbox's firewall and connection proxy; their images live in /tmp (--tmp-size) and go with the run",
             "--nested-containers: it can leave files owned by your subordinate IDs (setuid ones included) in the "
             "workspaces and the folders it may write; they are mounted nosuid here, and at the end of the run "
             "everything left so is given back to you (after a run that was killed: podman unshare chown -R 0:0 "
             "PATH)"]
    try:
        restricted = local_engine(a) and USERNS_RESTRICTION.read_text().strip() == "1"
    except OSError:
        restricted = False
    if restricted:
        notes.append("--nested-containers: this host's AppArmor takes the capabilities of user namespaces that "
                     "unconfined programs make (kernel.apparmor_restrict_unprivileged_userns=1): unshare -r and "
                     "rootless engines inside fail; sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 "
                     "lifts it")
    return notes


SIZE_UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def size_bytes(value):
    """Bytes of a size such as 4g, 512m or 1048576."""
    value = str(value).strip().lower()
    return int(value[:-1]) * SIZE_UNITS[value[-1]] if value[-1:] in SIZE_UNITS else int(value)


MEMINFO = Path("/proc/meminfo")


def host_memory(path=None):
    """Bytes of this host's memory (MemTotal), or None where it cannot be read."""
    try:
        for line in Path(path or MEMINFO).read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def tmp_ceiling(a):
    """(the most --tmp-size may be, what it is): /tmp and /var/tmp each get it, and both are memory, so together they
    stay within the memory limit (half of it each); with --memory none within half of this host's memory (a quarter
    each). (None, None) where nothing bounds it (another engine's machine)."""
    memory = resource_limits(a).get("memory")
    if memory:
        return size_bytes(memory) // 2, f"half the memory limit {memory}"
    total = host_memory() if local_engine(a) else None
    if total:
        return total // 4, f"a quarter of this host's memory ({total >> 20} MiB; --memory none)"
    return None, None


def check_tmp_size(a):
    """--tmp-size SIZE: Podman only, a size with k, m or g or a number of bytes, at least 16m, at most tmp_ceiling."""
    value = getattr(a, "tmp_size", None)
    if value is None:
        return
    if not re.fullmatch(r"[1-9][0-9]{0,12}[kmg]?", str(value).strip().lower()):
        raise ValueError(f"--tmp-size expects a size such as 6g or 512m, or a number of bytes: {value!r}")
    if a.backend != "podman":
        raise ValueError("--tmp-size needs the Podman backend (Bubblewrap's /tmp has no size of its own)")
    if size_bytes(value) < 16 << 20:
        raise ValueError(f"--tmp-size is at least 16m: {value!r}")
    ceiling, what = tmp_ceiling(a)
    if ceiling is not None and size_bytes(value) > ceiling:
        raise ValueError(f"--tmp-size {value} is more than {what}: /tmp and /var/tmp each get it, and files there "
                         "are memory that counts against the limit; raise --memory as well (twice the size)")


def tmp_mount_size(a):
    """",size=N" for /tmp and /var/tmp: --tmp-size, else half the memory limit (tmpfs_size); with --memory none a
    quarter of this host's memory each (the kernel's default, half of it each, would let both take all of it)."""
    if getattr(a, "tmp_size", None):
        return f",size={size_bytes(a.tmp_size)}"
    if "memory" not in resource_limits(a):
        ceiling = tmp_ceiling(a)[0]
        return f",size={ceiling}" if ceiling else ""
    return tmpfs_size(enforced_limits(a)[0])


_procinfo = None


def procinfo():
    """procinfo.py: start times, command lines, owners and children of this host's processes (/proc on Linux,
    sysctl on macOS)."""
    global _procinfo
    if _procinfo is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_procinfo", HERE / "procinfo.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _procinfo is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _procinfo = loaded
    return _procinfo


def process_start(pid, proc=Path("/proc")):
    """Start time of PID (Linux: clock ticks after boot, field 22 of /proc/PID/stat; macOS: from sysctl), or None
    if it is gone. Recorded with a supervisor's PID, so a later check can tell a reused PID apart."""
    return procinfo().start_time(pid, proc)


def process_cmdline(pid, start, proc=Path("/proc")):
    """Arguments of the process recorded as PID with start time START; [] when it is gone or the PID
    now belongs to another process. The caller still matches the arguments: records of older
    versions have no START (None) and rely on that check alone."""
    if not pid:
        return []
    cmdline = procinfo().cmdline(pid, proc)
    if cmdline is None:
        return []
    # Compared after reading the arguments: a match means they belonged to the recorded process.
    if start is not None and process_start(pid, proc) != start:
        return []
    return cmdline


def pod_owner(a):
    """Label value of a named session or MCP pod: its private socket directory, unique per state root."""
    directory = a.tmux_socket_dir or a.mcp_socket_dir
    return str(directory.expanduser().resolve()) if a.pod_name and directory else None


def remove_stale_pod(a, pod, owner):
    """Remove POD left behind by a supervisor that died without cleanup; True if one was removed.

    Only a pod labelled with OWNER is removed: one of the same name under another state root
    (or from an older version without the label) may still be in use."""
    try:
        found = subprocess.run(podman_command(a, "pod", "inspect", "--format", "{{json .Labels}}", pod),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("pod", f"could not ask Podman about a pod {pod} left behind (starting the pod reports it)", exc)
        return False  # Podman unusable: starting the pod reports it
    if found.returncode != 0:
        LOG.debug("pod", f"no pod {pod} left behind")
        return False  # no such pod
    try:
        labels = json.loads(found.stdout.strip() or "null") or {}
    except (ValueError, RecursionError):
        labels = {}
    if not isinstance(labels, dict) or labels.get(SESSION_LABEL) != str(owner):
        LOG.warning("pod", f"the pod {pod} exists and is not this session's (its owner label: "
                           f"{labels.get(SESSION_LABEL) if isinstance(labels, dict) else None}): the start is refused")
        raise ValueError(f"A Podman pod named {pod} already exists and was not created here. "
                         f"If it is left over from an earlier run, remove it with: podman pod rm --force {pod}")
    try:
        logged_run("pod", f"remove the pod {pod} a supervisor left behind",
                   podman_command(a, "pod", "rm", "--force", pod), check=True, stdout=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        # In words, not "Command '['podman', 'pod', 'rm', ...]' returned non-zero exit status 125"
        raise ValueError(f"Podman did not remove the pod {pod} an earlier run of this start left behind (exit status "
                         f"{exc.returncode}; what Podman said is above): remove it yourself with: podman pod rm "
                         f"--force {pod}, then start again") from None
    LOG.info("pod", f"removed the pod {pod} that an earlier supervisor of this session left behind")
    return True


# --- resource watch (memory, CPU, processes) ------------------------------------------------------------------
#
# The kernel enforces --memory: when the sandbox's cgroup is full, its OOM killer ends a process, and if that is the
# supervisor (PID 1 of the sandbox) the whole run ends. The launcher watches the cgroup from the host (the sandbox
# cannot see it): at MEMORY_WARN it tells the human and the agent (the session directory's memory.json, which the
# agent reads), at MEMORY_ACT it ends the largest process that is neither sandbox-ai's nor the agent's (a test run,
# a build), before the kernel picks one. Files in the sandbox's /tmp and HOME (tmpfs) count as memory too.
# Beside memory it watches the CPU (cpu.stat: the CPUs used, and how often --cpus held the sandbox back) and the
# processes (pids.current against --pids): a sandbox held at its CPU limit for CPU_THROTTLE_SUSTAIN, or near its
# process limit, is told about the same way (nothing is ended for that). The session directory's resources.json
# gives the agent all three; the log directory's memory.jsonl keeps the events and a sample a minute.

MEMORY_WARN, MEMORY_ACT, MEMORY_INTERVAL, MEMORY_GRACE = 0.85, 0.95, 2.0, 3.0
# memory.high, set by the watch at this share of the limit: above it the kernel reclaims and slows the sandbox's
# allocations down instead of letting it reach memory.max, where its OOM killer ends a process (a burst between two
# looks of the watch). A sandbox held there stalls rather than dies, so the watch also relieves it when its
# processes are stalled by memory (memory.pressure "full avg10", in percent) at MEMORY_STALL or more.
MEMORY_HIGH, MEMORY_STALL = 0.92, 10.0
MEMORY_STALL_PAUSE = 10.0  # seconds after a relief before a stall alone ends another process
MEMORY_EVENTS = 200  # records kept in memory for the summary after the run (memory.jsonl keeps all)
# The supervisor itself (named kthreadd for Firejail): its resident memory recorded from SUPERVISOR_RECORD (then at
# each doubling), the human told once at SUPERVISOR_WARN; its own records (runtime.py MemoryGuard) are in the run's
# control directory and are copied to the memory log when the run ends.
SUPERVISOR_NAME, SUPERVISOR_RECORD, SUPERVISOR_WARN = "kthreadd", 512 << 20, 1 << 30
# CPU: held back (throttled) in at least CPU_THROTTLE_RATIO of the scheduler's periods for CPU_THROTTLE_SUSTAIN
# seconds is told (again at most every CPU_RENOTIFY seconds; calm again below CPU_THROTTLE_CALM); processes: at
# PIDS_WARN of --pids (cleared below PIDS_CLEAR). A sample of all three goes to the log every RESOURCE_SAMPLE
# seconds, the shared files are written every RESOURCE_SHARE seconds (and at once when a warning comes or goes).
CPU_THROTTLE_RATIO, CPU_THROTTLE_CALM, CPU_THROTTLE_SUSTAIN, CPU_RENOTIFY = 0.5, 0.2, 60.0, 600.0
PIDS_WARN, PIDS_CLEAR = 0.9, 0.8
RESOURCE_SAMPLE, RESOURCE_SHARE = 60.0, 10.0
CGROUP_ROOT = Path("/sys/fs/cgroup")
# Never ended by the watch: sandbox-ai's own processes (their command lines name an --internal- entry point too).
MEMORY_PROTECTED = {"kthreadd", "firejail", "tmux: server", "tmux: client", "conmon", "catatonit", "pasta",
                    "pasta.avx2", "slirp4netns", "bwrap", "Xvfb", "x11vnc"}
AGENT_PROGRAMS = {"claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"}


def cgroup_limited(directory):
    """True if the cgroup DIRECTORY carries a limit of its own: memory.max, cpu.max (a quota) or pids.max."""
    for name in ("memory.max", "cpu.max", "pids.max"):
        try:
            value = (Path(directory) / name).read_text().split()
        except OSError:
            continue
        if value and value[0] != "max":
            return True
    return False


def cgroup_dir(pid, proc=Path("/proc"), root=CGROUP_ROOT):
    """The cgroup v2 directory of PID that carries a limit (memory, CPU or processes: its own, or its parent's when
    the process sits in a leaf without one), or None (no cgroup v2, no limit, gone)."""
    try:
        text = (Path(proc) / str(int(pid)) / "cgroup").read_text()
    except (OSError, ValueError):
        return None
    line = next((entry for entry in text.splitlines() if entry.startswith("0::")), None)
    if line is None:
        return None
    path = Path(root) / line[3:].strip().lstrip("/")
    top = MemoryWatch.sandbox_top(path)
    for candidate in (path, path.parent) if path != top else (path,):  # never above the sandbox's own cgroup
        if cgroup_limited(candidate):
            return candidate
    return None


def cgroup_pairs(path):
    """{key: int} of a cgroup file of "key value" lines (memory.stat, cpu.stat, ...); {} if it cannot be read."""
    found = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return found
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            found[key] = int(value)
    return found


def memory_state(cgroup, top=None):
    """{"current", "limit", "oom_kill", "anon", "shmem"} (bytes, kills) of CGROUP; shmem is what tmpfs files hold.
    The limit is the smallest memory.max on the path from CGROUP up to the sandbox's own top cgroup TOP (default:
    MemoryWatch.sandbox_top; one set on a parent, a pod's, holds CGROUP as well), never above it, with that
    cgroup's use as "current"; read again at every look. Without a limit on the path (a run limited only in CPUs or
    processes, or `sandbox-ai limits --memory none`): limit 0 and CGROUP's own use. ValueError when CGROUP's use
    cannot be read (it went)."""
    cgroup = Path(cgroup)
    top = Path(top) if top is not None else MemoryWatch.sandbox_top(cgroup)
    if cgroup != top and top not in cgroup.parents:
        top = cgroup  # (a TOP that does not hold CGROUP: CGROUP alone)
    levels, directory = [], cgroup
    for _ in range(64):
        try:  # the root of the cgroup tree has no memory.max: the walk ends there
            value = (directory / "memory.max").read_text().strip()
        except OSError:
            break
        if value != "max":
            try:
                current = int((directory / "memory.current").read_text().strip())
            except OSError:
                if directory == cgroup:
                    raise
                current = None
            if current is not None:
                levels.append((current, int(value)))
        if directory == top or directory.parent == directory:
            break
        directory = directory.parent
    if levels:
        current, limit = min(levels, key=lambda level: level[1])  # the smallest; of equal ones the nearest
    else:
        try:
            current, limit = int((cgroup / "memory.current").read_text().strip()), 0
        except OSError:
            raise ValueError(f"the memory use of {cgroup} cannot be read") from None
    stat, events = cgroup_pairs(cgroup / "memory.stat"), cgroup_pairs(cgroup / "memory.events")
    return {"current": current, "limit": limit, "oom_kill": events.get("oom_kill", 0), "anon": stat.get("anon", 0),
            "shmem": stat.get("shmem", 0)}


def cpu_state(cgroup):
    """{"usage_usec", "nr_periods", "nr_throttled", "throttled_usec", "limit"} of CGROUP (cpu.stat, cpu.max);
    limit: the CPUs its quota allows (0.0: none). None without cpu.stat."""
    stat = cgroup_pairs(Path(cgroup) / "cpu.stat")
    if "usage_usec" not in stat:
        return None
    limit = 0.0
    with contextlib.suppress(OSError, ValueError, ZeroDivisionError):
        quota, period = (Path(cgroup) / "cpu.max").read_text().split()[:2]
        limit = 0.0 if quota == "max" else round(int(quota) / int(period), 2)
    return {"usage_usec": stat["usage_usec"], "nr_periods": stat.get("nr_periods", 0),
            "nr_throttled": stat.get("nr_throttled", 0), "throttled_usec": stat.get("throttled_usec", 0),
            "limit": limit}


def pids_state(cgroup):
    """{"current", "limit"} of CGROUP (pids.current, pids.max; limit 0: none), or None without them."""
    try:
        current = int((Path(cgroup) / "pids.current").read_text().strip())
    except (OSError, ValueError):
        return None
    limit = 0
    with contextlib.suppress(OSError, ValueError):
        text = (Path(cgroup) / "pids.max").read_text().strip()
        limit = 0 if text == "max" else int(text)
    return {"current": current, "limit": limit}


def cpu_usage(before, after, seconds, host_cpus=None):
    """{"cpus", "percent", "limit", "host_cpus", "throttled_percent", "throttled_usec"} between two cpu_state()s
    SECONDS apart: the CPUs used, as a percentage of the limit (or of the host's CPUs without one), and the share of
    the scheduler's periods in which the limit held the sandbox back. None without both."""
    if not before or not after or seconds <= 0:
        return None
    host_cpus = host_cpus or os.cpu_count() or 1
    cpus = max(0, after["usage_usec"] - before["usage_usec"]) / (seconds * 1e6)
    periods = after["nr_periods"] - before["nr_periods"]
    throttled = after["nr_throttled"] - before["nr_throttled"]
    of = after["limit"] or host_cpus
    return {"cpus": round(cpus, 2), "percent": round(100 * cpus / of, 1), "limit": after["limit"],
            "host_cpus": host_cpus, "throttled_percent": round(100 * throttled / periods, 1) if periods > 0 else 0.0,
            "throttled_usec": after["throttled_usec"]}


def memory_stall(cgroup):
    """The share of the last 10 s in which all of CGROUP's processes were stalled waiting for memory
    (memory.pressure "full avg10", percent), or None without pressure information (no PSI)."""
    try:
        text = (Path(cgroup) / "memory.pressure").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("full "):
            fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
            try:
                return float(fields.get("avg10", ""))
            except ValueError:
                return None
    return None


def cgroup_processes(cgroup):
    """The PIDs of CGROUP and every cgroup below it."""
    pids = []
    for directory, _, _ in os.walk(cgroup):
        try:
            pids += [int(line) for line in (Path(directory) / "cgroup.procs").read_text().split() if line.isdigit()]
        except OSError:
            continue
    return pids


def memory_victim(pids, proc=Path("/proc"), protected=()):
    """(pid, name, rss bytes) of the process to end when the sandbox is about to run out of memory: the one with the
    most resident memory among PIDS that is not sandbox-ai's own (PROTECTED pids, MEMORY_PROTECTED names, an
    --internal- entry point) nor the agent itself; None if there is none."""
    best = None
    for pid in pids:
        if pid in protected:
            continue
        try:
            status = (Path(proc) / str(pid) / "status").read_text()
            argv = (Path(proc) / str(pid) / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        name = fields.get("Name", "").strip()
        rss = int(fields.get("VmRSS", "0 kB").split()[0]) * 1024 if fields.get("VmRSS") else 0
        programs = {os.path.basename(part.decode("utf-8", "replace")) for part in argv[:2] if part}
        if name in MEMORY_PROTECTED or any(b"--internal-" in part for part in argv) or programs & AGENT_PROGRAMS:
            continue
        if rss and (best is None or rss > best[2]):
            best = (pid, name, rss)
    return best


def gib(value):
    return f"{value / (1 << 30):.1f} GiB"


class ResourceWatch:
    """Watches the sandbox's memory, CPU and processes from the host while it runs (see above). LOCATE() gives
    (cgroup, protected pids) once the sandbox's processes exist, or None; SESSION is the session directory
    (memory.json and resources.json for the agent), LOG a file for memory.jsonl records (or None), NOTIFY(title,
    text) tells the human; HOST_CPUS: the engine's CPUs (the measure of the CPU use without a limit)."""

    @staticmethod
    def sandbox_top(cgroup):
        """The sandbox's own top cgroup for CGROUP (one of its processes' or the one with its limit): with Podman
        the pod's (the parent of the container's libpod-<id>[.scope], when that parent is a pod's:
        *libpod_pod_<id>.slice), else the container's own; with Bubblewrap the run's systemd-run scope (run-*.scope)
        it lies in; else CGROUP itself. Nothing above it is the sandbox's: the user's slice holds the desktop
        session, whose use is not the sandbox's to report (memory.json) or to act on (cgroup_dir, memory_state)."""
        path = Path(cgroup)
        for candidate in (path, *path.parents):
            if candidate.name.startswith("libpod-") and not candidate.name.startswith("libpod-conmon-"):
                return candidate.parent if "libpod_pod_" in candidate.parent.name else candidate
        for candidate in (path, *path.parents):
            if candidate.name.startswith("run-") and candidate.name.endswith(".scope"):
                return candidate
        return path

    def __init__(self, locate, name, session=None, log=None, notify=None, warn=MEMORY_WARN, act=MEMORY_ACT,
                 interval=MEMORY_INTERVAL, grace=MEMORY_GRACE, clock=time.monotonic, kill=os.kill, proc=Path("/proc"),
                 control=None, safeguard=None, host_cpus=None):
        self.locate, self.name, self.session, self.log, self.notify = locate, name, session, log, notify
        # SAFEGUARD(kind, what, why, fix, process=...): the run's safeguard records (safeguards.py Watch.report).
        self.safeguard, self.oom_told = safeguard, None
        # CONTROL() gives the run's control directory (or None), where the supervisor writes its own records.
        self.control = control or (lambda: None)
        self.supervisor, self.supervisor_peak, self.supervisor_next, self.supervisor_warned = None, 0, \
            SUPERVISOR_RECORD, False
        self.warn, self.act, self.interval, self.grace, self.clock, self.kill, self.proc = \
            warn, act, interval, grace, clock, kill, Path(proc)
        self.cgroup, self.protected, self.first_oom = None, set(), None
        self.high_for = None  # the limit memory.high was set for (it follows `sandbox-ai limits --memory`)
        self.high_set = False  # memory.high was written for high_for (else the write failed, recorded once)
        self.ended_at = None  # when the watch last ended a process
        self.warned, self.peak, self.ended, self.ending, self.events = False, 0, [], {}, []
        self.stopping, self.thread, self.written = threading.Event(), None, None
        # CPU and processes: the last cpu.stat (and when), what was measured since, since when the limit holds the
        # sandbox back, whether and when that was told; the processes' state and whether that was told.
        self.host_cpus = host_cpus or os.cpu_count() or 1
        self.cpu_last, self.cpu, self.throttled_since, self.cpu_warned, self.cpu_told = None, None, None, False, None
        self.cpu_peak, self.pids, self.pids_warned, self.pids_peak, self.sampled = 0.0, None, False, 0, None

    def record(self, event, **fields):
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "run": self.name, **fields}
        self.events.append(entry)
        if len(self.events) > MEMORY_EVENTS:  # a long run under pressure: the first and the latest are told
            del self.events[MEMORY_EVENTS // 2]
        if self.log is not None:
            with contextlib.suppress(OSError):
                with open(self.log, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry) + "\n")

    def inform(self, title, text):
        """A desktop notification to the human (NOTIFY), never an error of the watch."""
        if self.notify is not None:
            with contextlib.suppress(Exception):
                self.notify(title, text)

    def resources(self, state, warning):
        """What resources.json holds: memory, CPU and processes, the warnings and what to do (no names, no PIDs)."""
        warnings, advice = [], []
        if warning:
            warnings.append("memory")
            advice.append("memory is nearly full: run fewer things at once (tests, builds), remove large files from "
                          "/tmp and HOME (they count as memory)")
        if self.cpu_warned:
            warnings.append("cpu")
            advice.append("the CPU limit holds the sandbox back: run fewer jobs in parallel (make -j, test workers), "
                          "or ask the human for more CPUs (sandbox-ai limits RUN --cpus N)")
        if self.pids_warned:
            warnings.append("pids")
            advice.append("processes and threads are nearly at the limit, new ones will fail: end idle ones, use "
                          "fewer workers")
        cpu, pids = self.cpu or {}, self.pids or {}
        return {"memory": {"used_bytes": state["current"], "limit_bytes": state["limit"] or None,
                           "percent": round(100 * state["current"] / state["limit"], 1) if state["limit"] else None,
                           "tmpfs_bytes": state["shmem"]},
                "cpu": {"used_cpus": cpu.get("cpus"), "limit_cpus": cpu.get("limit") or None,
                        "host_cpus": self.host_cpus, "percent": cpu.get("percent"),
                        "throttled_percent": cpu.get("throttled_percent")},
                "pids": {"current": pids.get("current"), "limit": pids.get("limit") or None,
                         "percent": round(100 * pids["current"] / pids["limit"], 1) if pids.get("limit") else None},
                "warnings": warnings, "advice": advice,
                "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def share(self, state, warning):
        """The session directory's memory.json (memory alone, as before; only with a memory limit) and
        resources.json (memory, CPU, processes): what the agent can read about its resources (no names, no PIDs)."""
        if self.session is None:
            return
        files = {"resources.json": self.resources(state, warning)}
        if state["limit"]:
            files["memory.json"] = {
                "used_bytes": state["current"], "limit_bytes": state["limit"],
                "percent": round(100 * state["current"] / state["limit"], 1), "tmpfs_bytes": state["shmem"],
                "warning": warning, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "advice": ("memory is nearly full: run fewer things at once (tests, builds), remove large files from "
                           "/tmp and HOME (they count as memory)") if warning else ""}
        for name, data in files.items():
            with contextlib.suppress(OSError):
                target = Path(self.session) / name
                temporary = target.with_name(f".{name}.{os.getpid()}")
                temporary.write_text(json.dumps(data))
                os.replace(temporary, target)

    def watch_cpu(self, now):
        """One look at the CPU: what was used since the last look, and whether the limit holds the sandbox back for
        long (told once per such stretch, at most every CPU_RENOTIFY). True if what the agent reads changed."""
        sample = cpu_state(self.cgroup)
        if sample is None:
            return False
        last, self.cpu_last = self.cpu_last, (now, sample)
        usage = cpu_usage(last[1], sample, now - last[0], self.host_cpus) if last else None
        if usage is None:
            return False
        self.cpu = usage
        self.cpu_peak = max(self.cpu_peak, usage["cpus"])
        ratio = usage["throttled_percent"] / 100
        if ratio >= CPU_THROTTLE_RATIO and usage["limit"]:
            self.throttled_since = now if self.throttled_since is None else self.throttled_since
            held = now - self.throttled_since
            if held >= CPU_THROTTLE_SUSTAIN and not self.cpu_warned:
                self.cpu_warned = True
                self.record("cpu-throttled", cpus=usage["cpus"], limit=usage["limit"],
                            throttled_percent=usage["throttled_percent"], seconds=round(held))
                if self.cpu_told is None or now - self.cpu_told >= CPU_RENOTIFY:
                    self.cpu_told = now
                    self.inform("sandbox-ai: CPU limit reached",
                                f"{self.name} has been held at its CPU limit ({usage['limit']:g} CPUs) for "
                                f"{round(held)} s: it runs slower. More CPUs: sandbox-ai limits {self.name} --cpus N")
                return True
        elif ratio < CPU_THROTTLE_CALM:
            self.throttled_since = None
            if self.cpu_warned:
                self.cpu_warned = False
                return True
        return False

    def watch_pids(self):
        """One look at the processes against --pids (told once, again after it fell below PIDS_CLEAR). True if
        what the agent reads changed."""
        self.pids = pids_state(self.cgroup)
        if not self.pids:
            return False
        self.pids_peak = max(self.pids_peak, self.pids["current"])
        if not self.pids["limit"]:
            return False
        ratio = self.pids["current"] / self.pids["limit"]
        if ratio >= PIDS_WARN and not self.pids_warned:
            self.pids_warned = True
            self.record("pids-warning", current=self.pids["current"], limit=self.pids["limit"])
            self.inform("sandbox-ai: processes nearly at the limit",
                        f"{self.name} has {self.pids['current']} of {self.pids['limit']} processes and threads: at "
                        f"the limit new ones fail. More: sandbox-ai limits {self.name} --pids N")
            return True
        if ratio < PIDS_CLEAR and self.pids_warned:
            self.pids_warned = False
            return True
        return False

    def sample(self, state, now):
        """A record of memory, CPU and processes every RESOURCE_SAMPLE seconds (from the first look on)."""
        if self.sampled is None:
            self.sampled = now
            return
        if now - self.sampled < RESOURCE_SAMPLE:
            return
        self.sampled = now
        cpu, pids = self.cpu or {}, self.pids or {}
        self.record("sample", used=state["current"], limit=state["limit"], tmpfs=state["shmem"],
                    cpus=cpu.get("cpus"), cpu_limit=cpu.get("limit"), throttled_percent=cpu.get("throttled_percent"),
                    pids=pids.get("current"), pids_limit=pids.get("limit"))

    def tick(self):
        """One look at the cgroup; returns its memory state (or None before the sandbox's processes exist)."""
        if self.cgroup is None:
            found = self.locate()
            if not found:
                return None
            self.cgroup, self.protected = Path(found[0]), set(found[1])
        try:
            state = memory_state(self.cgroup)
        except (OSError, ValueError):
            self.unthrottle()  # the cgroup went
            return None
        now = self.clock()
        if not state["limit"]:
            self.unthrottle()  # no memory limit any more (`sandbox-ai limits --memory none`)
        if self.first_oom is None:
            self.first_oom = self.oom_told = state["oom_kill"]
        elif state["oom_kill"] > (self.oom_told or 0):
            self.tell_oom(state["oom_kill"])
        self.peak = max(self.peak, state["current"])
        ratio = state["current"] / state["limit"] if state["limit"] else 0
        changed = False
        if ratio >= self.warn and not self.warned:
            self.warned = changed = True
            text = (f"{self.name} uses {ratio:.0%} of its memory ({gib(state['current'])} of {gib(state['limit'])}, "
                    f"files in its /tmp and HOME: {gib(state['shmem'])})")
            self.record("memory-warning", used=state["current"], limit=state["limit"], tmpfs=state["shmem"])
            self.inform("sandbox-ai: memory nearly full", text)
        elif ratio < self.warn - 0.05 and self.warned:
            self.warned, changed = False, True
        with contextlib.suppress(OSError, ValueError):
            changed = self.watch_cpu(now) or changed
        with contextlib.suppress(OSError, ValueError):
            changed = self.watch_pids() or changed
        if changed:
            self.share(state, self.warned)
        elif self.written is None or self.clock() - self.written >= RESOURCE_SHARE:
            self.share(state, self.warned)
            self.written = self.clock()
            self.watch_supervisor(state)
        self.sample(state, now)
        self.throttle(state)
        stall = memory_stall(self.cgroup) if ratio >= MEMORY_HIGH - 0.02 else None
        # PSI's avg10 decays over ten seconds: after a relief, a stall alone does not end the next process yet.
        recent = self.ended_at is not None and self.clock() - self.ended_at < MEMORY_STALL_PAUSE
        if ratio >= self.act or (stall is not None and stall >= MEMORY_STALL and not recent):
            if ratio < self.act:
                self.record("memory-stall", used=state["current"], limit=state["limit"], stall=stall)
            self.relieve(state)
        return state

    def tell(self, kind, what, why, fix, process=None):
        if self.safeguard is not None:
            with contextlib.suppress(Exception):
                self.safeguard(kind, what, why, fix, process=process, target=kind)

    def tell_oom(self, count):
        """The kernel's out-of-memory killer ended processes since the last look (COUNT: its total)."""
        ended, self.oom_told = count - (self.oom_told or 0), count
        self.tell("oom", f"the kernel ended {ended} process{'es' if ended != 1 else ''} of {self.name}",
                  f"the sandbox ran out of memory (peak {gib(self.peak)})",
                  f"start with a larger --memory (e.g. --memory 16g), or now: sandbox-ai limits {self.name} --memory "
                  "16g; or run fewer things at once")
    def unthrottle(self):
        """The limit was lifted: memory.high goes with it (else the sandbox stayed slowed at 92 % of the old one)."""
        if self.high_for is None:
            return
        with contextlib.suppress(OSError):
            if (Path(self.cgroup) / "memory.max").read_text().strip() == "max":
                (Path(self.cgroup) / "memory.high").write_text("max\n")
                self.high_for, self.high_set = None, False

    def throttle(self, state):
        """memory.high at MEMORY_HIGH of the current limit (again when the limit changed, and when something set it
        back to max: systemd does, when it applies a unit's properties again); best effort: where the cgroup file
        cannot be written, the watch alone acts, as before."""
        if not state["limit"]:
            return
        if self.high_for == state["limit"]:
            if not self.high_set:
                return  # the write failed for this limit: recorded once
            try:  # read at every look (the kernel rounds what was written to pages: only "max" is a reset)
                if (Path(self.cgroup) / "memory.high").read_text().strip() != "max":
                    return
            except OSError:
                return
            self.record("memory-high-reset", limit=state["limit"])
        high = int(state["limit"] * MEMORY_HIGH)
        try:
            (Path(self.cgroup) / "memory.high").write_text(f"{high}\n")
            self.high_set = True
        except OSError as exc:  # recorded: then only the watch keeps the kernel's OOM killer away
            self.record("memory-high", error=exc.strerror or str(exc), limit=state["limit"])
            self.high_set = False
        self.high_for = state["limit"]

    def supervisor_rss(self):
        """(pid, rss bytes) of the sandbox's supervisor (the largest process named SUPERVISOR_NAME), or None."""
        best = None
        for pid in cgroup_processes(self.cgroup):
            try:
                if (self.proc / str(pid) / "comm").read_text().strip() != SUPERVISOR_NAME:
                    continue
                rss = next(int(line.split()[1]) * 1024 for line in (self.proc / str(pid) / "status").read_text()
                           .splitlines() if line.startswith("VmRSS:"))
            except (OSError, ValueError, IndexError, StopIteration):
                continue
            if best is None or rss > best[1]:
                best = (pid, rss)
        return best

    def watch_supervisor(self, state):
        found = self.supervisor_rss()
        if found is None:
            return
        self.supervisor = found
        rss = found[1]
        self.supervisor_peak = max(self.supervisor_peak, rss)
        if rss >= self.supervisor_next:
            while self.supervisor_next <= rss:
                self.supervisor_next *= 2
            self.record("supervisor-memory", rss=rss, used=state["current"], limit=state["limit"])
        if rss >= SUPERVISOR_WARN and not self.supervisor_warned:
            self.supervisor_warned = True
            self.inform("sandbox-ai: supervisor memory",
                        f"sandbox-ai's supervisor of {self.name} holds {gib(rss)} (it should stay far below); "
                        "if the run ends by memory, its records are in memory.jsonl: please report them")

    def supervisor_records(self):
        """The supervisor's own records (runtime.py MemoryGuard) from the control directory, copied into the memory
        log; their number."""
        directory = self.control()
        if directory is None:
            return 0
        try:
            lines = (Path(directory) / "supervisor-memory.jsonl").read_text(encoding="utf-8").splitlines()[:50]
        except OSError:
            return 0
        count = 0
        for line in lines:
            try:
                data = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict):
                # Written by the supervisor, which runs beside the agent: only plain fields, the record's own kind.
                data.pop("event", None)
                data.pop("run", None)
                self.record("supervisor-detail", **{key: value for key, value in data.items()
                                                    if isinstance(key, str) and key.isidentifier() and key != "self"})
                count += 1
        return count

    def relieve(self, state):
        now = self.clock()
        for pid, (since, handle) in list(self.ending.items()):
            if now - since >= self.grace:
                # Through the pidfd opened at SIGTERM: a PID reused meanwhile (by any process of yours) is never hit.
                with contextlib.suppress(OSError):
                    if handle is not None:
                        signal.pidfd_send_signal(handle, signal.SIGKILL)
                    elif pid in cgroup_processes(self.cgroup):
                        self.kill(pid, signal.SIGKILL)  # no pidfd here: only while it is still the sandbox's
                if handle is not None:
                    with contextlib.suppress(OSError):
                        os.close(handle)
                del self.ending[pid]
        victim = memory_victim(cgroup_processes(self.cgroup), self.proc, self.protected | set(self.ending))
        if victim is None:
            return
        pid, name, rss = victim
        handle = None
        if self.kill is os.kill and hasattr(os, "pidfd_open"):
            with contextlib.suppress(OSError):
                handle = os.pidfd_open(pid)
            # Opened after the look at the cgroup: still the sandbox's process (not a PID given out again)?
            if handle is not None and pid not in cgroup_processes(self.cgroup):
                os.close(handle)
                return
        try:
            if handle is not None:
                signal.pidfd_send_signal(handle, signal.SIGTERM)
            else:
                self.kill(pid, signal.SIGTERM)
        except OSError:
            if handle is not None:
                os.close(handle)
            return
        self.ending[pid] = (now, handle)
        self.ended_at = now
        self.ended.append({"pid": pid, "name": name, "rss": rss})
        self.record("memory-relief", pid=pid, process=name, rss=rss, used=state["current"], limit=state["limit"])
        self.tell("memory", f"ended {name} (PID {pid}, {gib(rss)})",
                  f"{self.name} used {state['current'] / state['limit']:.0%} of its memory ({gib(state['current'])} of "
                  f"{gib(state['limit'])}; files in /tmp and HOME count too): the largest process was ended so that "
                  "the sandbox goes on", f"start with a larger --memory (e.g. --memory 16g), or now: sandbox-ai limits "
                  f"{self.name} --memory 16g; or run fewer things at once", process=f"{name} (pid {pid})")

    def watch_memory(self):
        while not self.stopping.wait(self.interval):
            with contextlib.suppress(Exception):  # a broken look never ends the launcher's watch of the run
                self.tick()

    def start(self):
        self.thread = threading.Thread(target=self.watch_memory, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """Stop watching; the lines that tell the human what happened (none when nothing did)."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(self.interval + 1)
        lines = []
        oom = 0
        if self.cgroup is not None:
            with contextlib.suppress(OSError, ValueError):
                total = memory_state(self.cgroup)["oom_kill"]
                oom = total - (self.first_oom or 0)
                if self.first_oom is not None and total > (self.oom_told or 0):
                    self.tell_oom(total)
        for entry in self.ended:
            lines.append(f"memory nearly full: ended {printable(entry['name'])} (PID {entry['pid']}, {gib(entry['rss'])}) "
                         "so that the sandbox could go on")
        details = self.supervisor_records()
        if oom > 0:
            lines.append(f"the sandbox ran out of memory: the kernel ended {oom} process{'es' if oom != 1 else ''} "
                         f"(peak {gib(self.peak)}); start with --memory 16g, or run fewer things at once")
        if self.supervisor_peak >= SUPERVISOR_RECORD:
            where = f" (details: {self.log})" if self.log is not None and details else ""
            lines.append(f"sandbox-ai's supervisor held up to {gib(self.supervisor_peak)}{where}; that is more than it "
                         "should: please report it")
        held = [entry for entry in self.events if entry["event"] == "cpu-throttled"]
        if held:
            lines.append(f"the CPU limit ({held[-1]['limit']:g} CPUs) held the sandbox back {len(held)} "
                         f"time{'s' if len(held) != 1 else ''} for a minute or more; start with a higher --cpus, or "
                         f"change it while it runs: sandbox-ai limits {self.name} --cpus N")
        if any(entry["event"] == "pids-warning" for entry in self.events):
            lines.append(f"processes and threads came near the limit (peak {self.pids_peak}); start with a higher "
                         "--pids if something failed to start a process")
        return lines


MemoryWatch = ResourceWatch  # its older name


def tmpfs_size(limits):
    """",size=N" for the sandbox's /tmp and /var/tmp: half the memory limit (files there are memory, and without a
    size a tmpfs may take half the host's RAM, more than the limit: the run would end by OOM instead of a full
    disk); "" without a limit."""
    memory = limits.get("memory")
    if not memory:
        return ""
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
    size = int(memory[:-1]) * units[memory[-1]] if memory[-1] in units else int(memory)
    return f",size={max(size // 2, 64 << 20)}"


def podman_agent_pid(a, pod):
    """The host PID of the agent container's first process (the supervisor) in POD, or None while it starts."""
    try:
        listed = subprocess.run(podman_command(a, "ps", "--filter", f"pod={pod}", "--format",
                                               "{{.ID}} {{.IsInfra}} {{.Names}}"),
                                capture_output=True, text=True, timeout=10, check=False).stdout.split("\n")
        rows = [line.split() for line in listed if line.strip()]
        # Not the infra container, and not a service beside the agent (--service: POD-service-NAME).
        ids = [row[0] for row in rows if len(row) >= 2 and row[1] == "false"
               and not (len(row) > 2 and row[2].startswith(f"{pod}-service-"))]
        if not ids:
            return None
        pid = subprocess.run(podman_command(a, "inspect", "--format", "{{.State.Pid}}", ids[0]),
                             capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        return int(pid) if pid.isdigit() and int(pid) > 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def start_memory_watch(a, session, safeguard=None):
    """The resource watch of this run, or None: only where the launcher sees the sandbox's cgroup (a local engine on
    Linux) and the run has a limit there (memory, CPUs or processes). SAFEGUARD: the safeguard watch's report
    (processes it ends, OOM kills)."""
    if not local_engine(a) or not set(enforced_limits(a)[0]) & {"memory", "cpus", "pids"}:
        return None

    def locate():
        pid = podman_agent_pid(a, pod_name(a)) if a.backend == "podman" else getattr(a, "sandbox_pid", None)
        cgroup = cgroup_dir(pid) if pid else None
        return (cgroup, {pid}) if cgroup else None
    log = Path(a.log_dir).expanduser() / "memory.jsonl" if a.log_dir else None

    def notify(title, text):
        approvals = load_approvals_module()
        if hasattr(approvals, "phone"):  # the phone channels (sandbox-ai notify): a limit reached
            approvals.phone("limits", title.removeprefix("sandbox-ai: "), run=getattr(a, "start_name", None)
                            or getattr(a, "run_id", None))
        approvals.desktop_notification(title, text)
    return ResourceWatch(locate, getattr(a, "run_id", None) or pod_name(a), session=session, log=log,
                         notify=notify, control=lambda: getattr(a, "control_dir", None), safeguard=safeguard,
                         host_cpus=engine_cpus(a)).start()


_monitor_module = None


def load_monitor_module():
    """monitor.py (the system monitor: the run's collector, `top`, `stats usage`), loaded once."""
    global _monitor_module
    if _monitor_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_monitor", HERE / "monitor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _monitor_module = module
    return _monitor_module


_checkpoints_module = None


_updates_module = None


def load_updates_module():
    """updates.py (the update mode: off, notify, download, auto; its timer), loaded once."""
    global _updates_module
    if _updates_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_updates", HERE / "updates.py")
        with LOADING:
            if _updates_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _updates_module = loaded
    return _updates_module


def load_checkpoints_module():
    """checkpoints.py (the workspace checkpoints: taken at the start, every few minutes, restored), loaded once."""
    global _checkpoints_module
    if _checkpoints_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_checkpoints", HERE / "checkpoints.py")
        with LOADING:
            if _checkpoints_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _checkpoints_module = loaded
    return _checkpoints_module


def checkpoint_plan(a):
    """What this start's checkpoints hold, or None (none: --no-checkpoint without --checkpoint-every, a probe, an MCP
    server's or helper's sandbox, nothing writable): {"project", "roots": [{"host", "sandbox", "kind"}], "start":
    whether one is taken as it begins, "every": minutes (0: none), "exclude", "store": where they are kept - in
    sandbox-ai's own state, which no sandbox gets}. Remote folders (sshfs, NFS, SMB) are not held (named in
    "not_held")."""
    if a.probe or getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None):
        return None
    C = load_checkpoints_module()
    found = C.settings(getattr(a, "checkpoint_defaults", None) or {})
    every = a.checkpoint_every if a.checkpoint_every is not None else found["checkpoint_every"]
    at_start = found["checkpoint_start"] and not a.no_checkpoint
    if not at_start and not every:
        return None
    remote = {str(Path(entry["point"]).resolve()) for entry in getattr(a, "remote_mounts", None) or []
              if isinstance(entry, dict) and entry.get("point")}
    roots, not_held = [], []
    for index, (src, dest) in enumerate(workspace_targets(a)):
        host = str(Path(src).expanduser().resolve())
        if workspace_mode(a, index) != "rw":
            continue
        (not_held if host in remote else roots).append({"host": host, "sandbox": dest, "kind": "workspace"})
    mounts = a.checkpoint_mounts if a.checkpoint_mounts is not None else found["checkpoint_mounts"]
    for src, dest, mode in map(parse_mount, a.mount) if mounts else []:
        host = str(Path(src).expanduser().resolve())
        if mode == "rw":
            (not_held if host in remote else roots).append({"host": host, "sandbox": dest, "kind": "mount"})
    if not roots:
        return None
    project = str(Path(a.workspaces[0]).expanduser().resolve())
    return {"project": project, "roots": roots, "start": at_start, "every": every,
            "exclude": found["checkpoint_exclude"], "store": str(C.store_root() / C.project_key(project)),
            "not_held": [root["host"] for root in not_held]}


def start_checkpoint(a):
    """The start's checkpoint of its workspaces (checkpoint_plan), begun in the background while the start prepares
    everything else (checkpoint_ready waits for it before the agent runs). The pending checkpoint, or None."""
    a.checkpoint_plan = plan = checkpoint_plan(a)
    if plan is None or not plan["start"]:
        return None
    a.checkpoint_pending = load_checkpoints_module().Pending(
        plan["project"], plan["roots"], kind="start", run=a.run_id, start=checkpoint_key(a), name=run_name(a),
        agent=a.agent, exclude=plan["exclude"])
    return a.checkpoint_pending


def checkpoint_key(a):
    """The start the checkpoints of this run belong to: its kept start's id (a session's, a kept foreground start's),
    else the run's id."""
    return getattr(a, "start_key", None) or getattr(a, "start_id", None) or a.run_id


def checkpoint_ready(a, cleanup=None):
    """Before the agent can write (and before the canaries are planted): the start's checkpoint finished, the run's
    record kept (checkpoints.record_run: `checkpoint RUN`, `restore RUN`), the periodic checkpoints begun (stopped by
    CLEANUP). A checkpoint that failed is said and the start goes on without. Its record, or None."""
    plan = getattr(a, "checkpoint_plan", None)
    if plan is None:
        return None
    C = load_checkpoints_module()
    key, name = checkpoint_key(a), run_name(a)
    pending, meta = getattr(a, "checkpoint_pending", None), None
    if pending is not None:
        progress = getattr(a, "progress", None)
        if not pending.done() and progress is not None:
            progress.step("finishing the checkpoint of the workspaces (taken before the agent writes)")
        try:
            meta = pending.finish()
        except Exception as exc:  # noqa: BLE001 - the start goes on; what it means is said
            print(f"agent-sandbox: warning: no checkpoint of the workspaces at this start ({printable(exc)[:300]}): "
                  "sandbox-ai checkpoint restore cannot undo this run", file=sys.stderr)
        else:
            print(f"agent-sandbox: checkpoint {meta['id']} of the workspaces ({meta['files']} files, "
                  f"{meta['seconds']:.1f} s); undo what the agent does: sandbox-ai checkpoint restore "
                  f"{printable(name or key)}", file=sys.stderr)
    if meta is None and not plan["every"]:
        return None
    with contextlib.suppress(OSError, ValueError):
        C.record_run(key, {"run": a.run_id, "start": key, "name": name, "agent": a.agent,
                           "project": plan["project"], "roots": plan["roots"], "exclude": plan["exclude"],
                           "checkpoint": meta["id"] if meta else None, "every": plan["every"]})
    if plan["every"]:
        periodic = C.Periodic(plan["every"], plan["project"], plan["roots"], run=a.run_id, start=key, name=name,
                              agent=a.agent, exclude=plan["exclude"])
        a.checkpoint_periodic = periodic
        if cleanup is not None:
            cleanup.callback(periodic.stop)
    return meta


MONITOR_LOCATE_TRIES = 6  # looks for the run's cgroup before `podman stats` stands in (a cgroup this host cannot read)
# The memory watch's kinds: it tells them itself (its desktop notification, the OOM safeguard record); the monitor
# records them (its series, the run's activity) without a second notification.
MONITOR_WATCHED_KINDS = {"memory", "cpu", "pids", "oom"}


def start_monitor(a, safeguard=None, watched=False):
    """The system monitor's collector of this run (monitor.py Recorder), started; None for a probe or with recording
    off (monitor_interval 0). Where this host sees the run's cgroup (a local engine on Linux) it reads it (a
    Bubblewrap run without a scope of its own: its process tree); else, and when the cgroup is not found after
    MONITOR_LOCATE_TRIES looks, `podman stats` every monitor.REMOTE_INTERVAL seconds stands in (macOS: podman machine's
    VM; --podman-url). Its warnings go to SAFEGUARD (the run's safeguard records: the app's popups, a desktop
    notification) - those the memory watch tells itself not while it sees the run's cgroup (WATCHED: the run's
    ResourceWatch, None, or True/False) - and to the run's activity. Never fails the launch."""
    if a.probe:
        return None
    try:
        M = load_monitor_module()
        interval = M.interval_setting()
        if not interval:
            return None
        # The cgroups are read where this host has cgroup v2 (a v1 host: Podman's numbers instead).
        local = local_engine(a) and (M.CGROUP_ROOT / "cgroup.controllers").exists()
        pod = pod_name(a) if a.backend == "podman" else None
        state = {"located": None, "tries": 0, "stats_at": None, "pids": []}
        remote_every = max(interval, min(M.REMOTE_INTERVAL, 3 * interval))
        own = M.cgroup_of(os.getpid()) if local else None

        def locate():
            state["tries"] += 1
            pid = podman_agent_pid(a, pod) if pod else getattr(a, "sandbox_pid", None)
            if not pid:
                return None
            found = M.cgroup_of(pid)
            if found is not None and (own is None or found[0] != own[0]):
                return "cgroup", found, pid
            return ("tree", None, pid) if a.backend == "bwrap" else None

        def read():
            if local and state["located"] is None and state["tries"] < MONITOR_LOCATE_TRIES:
                state["located"] = locate()
            located = state["located"]
            if located is not None:
                kind, found, pid = located
                counters = M.cgroup_counters(found[0], found[1], pid) if kind == "cgroup" else M.tree_counters(pid)
                if counters is None:
                    state["located"] = None  # gone (its container made again?): looked for again, then podman stats
                    return None
                if a.gpu == "nvidia":
                    state["pids"] = M.descendants(pid) if kind == "tree" else cgroup_processes(found[0])
                return counters
            if pod is None or (local and state["tries"] < MONITOR_LOCATE_TRIES):
                return None
            now = time.time()
            if state["stats_at"] is not None and now - state["stats_at"] < remote_every - 0.5:
                return None
            state["stats_at"] = now
            return M.stats_counters(M.podman_stats(podman_command(a)), f"{pod}-")

        activity = {}

        def warn(warning, started):
            if not started:
                return
            # (a memory watch that has not found the run's cgroup tells nothing: the monitor's warning is the one)
            told = watched if isinstance(watched, bool) else getattr(watched, "cgroup", None) is not None
            if safeguard is not None and not (told and warning["kind"] in MONITOR_WATCHED_KINDS):
                fix = warning["advice"] + (f" (sandbox-ai {' '.join(warning['argv'])})" if warning.get("argv") else "")
                safeguard("resources", warning["text"], warning["title"], fix, target=warning["kind"])
            if a.log_dir:
                if "module" not in activity:
                    activity["module"] = load_activity_module()
                A = activity["module"]
                record = A.finalize(A.raw("resources", f"{warning['title']}: {warning['text']}", details={
                    "warning": warning["kind"], "level": warning["level"], "advice": warning["advice"],
                    "command": " ".join(["sandbox-ai", *warning["argv"]]) if warning.get("argv") else None,
                    "values": warning["values"]}), a.run_id, A.session_name(a), a.agent,
                    lambda text, limit=None: str(text)[:limit or 2000])
                A.append(Path(a.log_dir).expanduser() / A.ACTIVITY_FILE, [record])
        header = {"run": a.run_id, "name": run_name(a), "agent": a.agent, "backend": a.backend,
                  "workspace": a.workspaces[0] if a.workspaces else None,
                  "kind": "mcp" if a.mcp_socket_dir else "agent",
                  "mcp_name": a.mcp_socket_dir.expanduser().parent.name if a.mcp_socket_dir else None,
                  "mcp_servers": [] if a.mcp_socket_dir else list(dict.fromkeys(a.mcp)),
                  "helper_of": getattr(a, "helper_of", None),
                  "kept": getattr(a, "session", None) is not None,  # (the idle warning: stopped, it is kept)
                  "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        directory = Path(a.log_dir).expanduser() if a.log_dir else M.monitor_root() / "runs"
        host = M.host_counters if local_engine(a) else M.host_counters_mac if sys.platform == "darwin" and \
            not a.podman_url else None
        with contextlib.suppress(Exception):  # the runs without a log folder: bounded by age and size
            M.prune(days=load_starts_module().log_days_setting()[0])
        gpu = (lambda: M.gpu_counters(state["pids"])) if a.gpu == "nvidia" else None
        return M.Recorder(header, read, directory, interval=interval, warn=warn, host=host, gpu=gpu,
                          host_cpus=engine_cpus(a)).start()
    except Exception as exc:  # noqa: BLE001 - the run goes on without its monitor
        print(f"agent-sandbox: monitor: not recorded: {printable(str(exc))[:300]}", file=sys.stderr)
        return None


POD_REMOVE_SECONDS = 120  # the removal of a run's pod at its end, at most
AT_ONCE_SECONDS = 10  # a sandbox that fails within them ended "at once" (the agent did not start, a bad argument)


def engine_bind_note(a, engine):
    """What to say when ENGINE (Podman, Bubblewrap) failed at once while the start A gives the sandbox the kernel's or
    the devices' tree or a file system of the kernel's own (check_kernel_mounts: you chose it, it was given with a
    warning): the engine's own message (above it) says why, and such a tree cannot be given this way then - the one
    refusal there is, the engine's. None when no such tree was given."""
    given = [Path(os.path.expanduser(str(path))) for path in [*a.workspaces, *([a.state_dir] if a.state_dir else [])]]
    for value in a.mount:
        with contextlib.suppress(ValueError):
            given.append(Path(parse_mount(value)[0]))
    trees = []
    for path in given:
        with contextlib.suppress(OSError, RuntimeError):
            if check_kernel_mounts(path.resolve(), "", warn=lambda _text: None):
                trees.append(str(path))
    if not trees:
        return None
    return (f"{engine} failed at once, and this start gives the sandbox {', '.join(trees)}, the kernel's or the "
            f"devices' tree: when {engine}'s message above is about that mount, {engine} cannot bind it - it cannot "
            "be given to a sandbox this way (--device gives a single device)")


def sandbox_ended(name, status, seconds):
    """(level, area, message) of the line saying that the sandbox NAME ended with STATUS after SECONDS: INFO when it
    ended well or by a signal (stop, Ctrl-C), else WARNING - and "at once" says where its reason is."""
    text = f"the sandbox of {name} ended with exit status {status} after {seconds:.0f} s"
    if not isinstance(status, int):
        return "INFO", "pod", text
    signalled = status < 0 or status in (128 + signal.SIGINT, 128 + signal.SIGTERM, 128 + signal.SIGHUP)
    if status == 0:
        return "INFO", "pod", text
    if signalled:
        number = -status if status < 0 else status - 128
        with contextlib.suppress(ValueError):
            return "INFO", "pod", f"{text} (ended by {signal.Signals(number).name}: stopped, Ctrl-C or the host)"
        return "INFO", "pod", text
    return "WARNING", "pod", text + (" - at once: the agent did not start or ended on its first step; its own "
                                     "message is on its terminal (a session's: supervisor.log)"
                                     if seconds < AT_ONCE_SECONDS else "")


def phone_run_end(a, status, seconds, error=None):
    """The end of this run to the phone channels of `sandbox-ai notify` (sent from the host): "run-ended" when it ended
    well or was stopped (a signal, Ctrl-C), "run-failed" for another exit status or an ERROR that ended it; what is
    held for a summary goes out now, and the messages get up to 5 seconds. Not for a probe, an MCP server's or a
    helper's sandbox, nor for a scheduled run (SANDBOX_AI_SCHEDULE: `sandbox-ai schedule` tells its end itself, with
    its summary). The event, or None (none sent); never an error."""
    if getattr(a, "probe", False) or getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None) or \
            os.environ.get("SANDBOX_AI_SCHEDULE"):
        return None
    try:
        approvals = load_approvals_module()
        if not hasattr(approvals, "phone"):
            return None
        if status is None:
            stopped = isinstance(error, (KeyboardInterrupt, SystemExit)) and \
                getattr(error, "code", 0) in (0, None, 130, 143)
            event = "run-ended" if stopped else "run-failed"
            what = "the run was stopped" if stopped else "the run failed"
        else:
            event = "run-ended" if sandbox_ended("", status, seconds)[0] == "INFO" else "run-failed"
            what = f"the run ended with exit status {status} after {round(seconds / 60)} min"
        approvals.phone(event, what, None, run=getattr(a, "start_name", None) or getattr(a, "run_id", None))
        approvals.phone_module().finish(5)
        return event
    except Exception as exc:  # noqa: BLE001 - the run's end goes on
        LOG.debug("notify", "the end of the run was not told to the phone channels", exc)
        return None


def run_pod(a, session, home):
    name = pod_name(a)
    create, run = pod_commands(a, name, session, home)
    # Check local image first, before creating an owned pod. Never mutate a
    # pre-existing pod, use host networking, or implicitly fetch images.
    try:
        logged_run("pod", f"is the image {a.image} there", podman_command(a, "image", "exists", a.image), check=True)
    except subprocess.CalledProcessError as exc:
        # (an MCP server's sandbox, a probe: no image check before - only "exit status 1" was logged)
        LOG.warning("pod", f"the start is refused: the image {a.image} of {a.agent} is not on this host"
                    if exc.returncode == 1 else f"the start is refused: Podman could not say whether the image "
                                                f"{a.image} is there (exit status {exc.returncode})")
        # In words, not "Command '['podman', 'image', 'exists', ...]' returned non-zero exit status 1"
        raise ValueError(f"the image {a.image} is not on this host: {image_fix(a, a.agent, a.image)}"
                         if exc.returncode == 1 else f"Podman could not say whether the image {a.image} is there "
                         f"(podman image exists: exit status {exc.returncode}); check the host with: sandbox-ai "
                         "doctor") from None
    # Owned unless podman itself refuses the creation (e.g. the name is taken by
    # someone else's pod). An interruption during or right after creation still
    # removes the pod, with no window between creating and owning it.
    owned = True
    try:
        try:
            if not getattr(a, "vpn_pod", None):  # vpn.py created it, with the tunnel in it
                # Its error output is taken (and said on standard error as before), so launcher.log has Podman's
                # reason too, not only "exit status 125".
                made = logged_run("pod", f"create the pod {name}", create, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
                said = getattr(made, "stderr", None)
                if isinstance(said, str) and said:
                    sys.stderr.write(said)
                    sys.stderr.flush()
                status = getattr(made, "returncode", 0)
                if isinstance(status, int) and status != 0:
                    raise subprocess.CalledProcessError(status, create, stderr=said)
        except subprocess.CalledProcessError as exc:
            # (its error output is in the line above; what helps, as the start's first Podman command says it)
            hint = podman_failure(a, exc).rpartition("-> ")[2]
            LOG.warning("pod", f"Podman did not create the pod {name} (exit status {exc.returncode}) -> {hint}")
            owned = False
            # In words, never Python's "Command '[...]' returned non-zero exit status 125" (Podman's own reason is
            # said just above, as it wrote it: not again here)
            said = any(line.strip() and not PODMAN_WARNING.match(line.strip())
                       for line in (exc.stderr or "").splitlines())
            raise ValueError(f"Podman did not create the pod {name} (exit status {exc.returncode}"
                             + ("; its reason is above" if said else "") + f")\n  -> {hint}") from None
        a.running_pod = name  # a canary's pause (canary.py) freezes the pod
        LOG.info("pod", f"the pod {name} is created; its sandbox starts")
        LOG.debug("pod", f"its command: {LOG.command_line(run)}")
        begun = time.monotonic()
        child = subprocess.Popen(run)
        LOG.debug("pod", f"podman run of {name} has PID {getattr(child, 'pid', None)}")
        if load_services_module().members(a) and not a.probe:
            # The services (and the browser) start once the supervisor's firewall and proxy are up (it says so in the
            # run's directory).
            threading.Thread(target=load_services_module().launch, daemon=True,
                             args=(a, name, session, lambda: child.poll() is None, launcher_namespace())).start()
            if getattr(a, "artex_container", None):
                # --artex: its console on this computer's 127.0.0.1 once it started, as long as the run lives.
                threading.Thread(target=load_artex_module().forward, daemon=True,
                                 args=(a, name, lambda: child.poll() is None, launcher_namespace())).start()
        status = wait_child(child)
        LOG.log(*sandbox_ended(name, status, time.monotonic() - begun))
        note = engine_bind_note(a, "Podman") if status == 125 and time.monotonic() - begun < AT_ONCE_SECONDS \
            else None
        if note:  # (Podman's own message is above it)
            print(f"agent-sandbox: error: {printable(note)}", file=sys.stderr)
        return status
    finally:
        if owned and load_services_module().members(a):
            try:  # their output, before the pod (and they) go
                for path in load_services_module().collect_logs(a, name, launcher_namespace()):
                    print(f"agent-sandbox: a service's output: {printable(str(path))}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - the pod is removed in any case
                print(f"agent-sandbox: the services' output could not be kept: {printable(str(exc))}", file=sys.stderr)
        if owned:
            # --time 0: the infra container's sleep ignores SIGTERM; waiting for it only costs 10 seconds.
            with signals_deferred():
                # With a time limit: Ctrl-C and SIGTERM are held here (and in podman, which inherits the mask), so a
                # podman that hangs (a stuck lock of its own) would hold the launcher until a kill -9.
                timed_out = []

                def remove(*extra):
                    try:
                        return subprocess.run(podman_command(a, "pod", "rm", "--force", *extra, name), check=False,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                              timeout=POD_REMOVE_SECONDS)
                    except subprocess.TimeoutExpired:
                        LOG.warning("pod", f"podman pod rm of {name} gave no answer within {POD_REMOVE_SECONDS} s")
                        print(f"agent-sandbox: podman did not remove the pod {name} within {POD_REMOVE_SECONDS} s; "
                              f"remove it with: podman pod rm --force {name}", file=sys.stderr)
                        timed_out.append(True)
                        return None
                removed = remove("--time", "0")
                said = str(getattr(removed, "stderr", "") or "").strip()
                LOG.info("pod", f"removed the pod {name}" if getattr(removed, "returncode", None) == 0 else
                         f"podman pod rm --force --time 0 {name}: exit status "
                         f"{getattr(removed, 'returncode', None)}: {LOG.tail(said)}")
                def failed(done):
                    return isinstance(getattr(done, "returncode", None), int) and done.returncode != 0
                if failed(removed) and "no such pod" not in said.lower():
                    # Once more without --time (Podman before 4.0 knows none); a pod still left is said, not hidden.
                    removed = remove()
                    said = str(getattr(removed, "stderr", "") or "").strip()
                    if failed(removed) and "no such pod" not in said.lower():
                        print(f"agent-sandbox: could not remove the pod {name} "
                              f"({printable(said.splitlines()[-1] if said else '?')}); remove it with: podman pod rm "
                              f"--force {name}", file=sys.stderr)
                if timed_out or (failed(removed) and "no such pod" not in said.lower()):
                    # Its sandbox may run on without this launcher: never without its kill switch (fail closed).
                    why = f"podman pod rm gave no answer within {POD_REMOVE_SECONDS} s" if timed_out and \
                        removed is None else said.splitlines()[-1] if said else \
                        f"podman pod rm failed with exit status {getattr(removed, 'returncode', None)}"
                    try:
                        sandbox_outlived(a, name, why)
                    except Exception as exc:  # noqa: BLE001 - never instead of the run's own end; said loud
                        LOG.error("pod", f"the pod {name} may run on without its supervisor ({why}); what to do "
                                         "about it failed", exc)
                        print(f"agent-sandbox: error: the pod {name} may run on without its supervisor "
                              f"({printable(why)}): end it now: podman pod rm --force {name}", file=sys.stderr)


OUTLIVED_CUT = "all"  # the kill switch's mode for a sandbox that outlives its launcher: nothing leaves it
OUTLIVED_BY = "supervisor-gone"  # on whose behalf (runtime.SUPERVISOR_GONE_BY: the sandbox's own watch cuts so too)
OUTLIVED_KILL_SECONDS = 30


def sandbox_outlived(a, pod, why):
    """Podman could not remove POD at the end of this run (WHY: what it said, a full disk say): its sandbox may run on
    while this launcher - its supervisor on the host - ends, and with it went the run's directory (its control socket:
    approvals, net allow, the kill switch) and every watch of the run. Fails closed instead: when Podman still lists a
    container of POD running (or does not answer), its network is cut through its control socket (the kill switch
    OUTLIVED_CUT, on behalf of OUTLIVED_BY); when that is not confirmed, the pod is killed. A sandbox that still runs
    then keeps its run directory and its folders (unless_outlived; liveness.discard removes them once it has ended),
    gets the reason recorded there (liveness.SUPERVISOR_END, which sandbox-ai list and doctor say), and the human is
    told: launcher.log, this terminal (a session's: supervisor.log) and a desktop notification. Its end is not
    recorded (it has not ended): list shows it orphaned. The record ({"why", "cut", ...}), or None when nothing of it
    runs any more."""
    starts = load_starts_module()
    live = starts.module("liveness")
    if live.pod_live(pod, a.podman_url, fresh=True) is False:
        return None
    directory = getattr(a, "control_dir", None)
    cut, error = None, None
    if directory is not None and (Path(directory) / "run.json").is_file():
        net = starts.module("net")
        try:
            record = {**json.loads((Path(directory) / "run.json").read_text()), "directory": str(directory)}
            cut = net.cut_label((net.cut_one(record, OUTLIVED_CUT, OUTLIVED_BY) or {}).get("cut")) or None
        except Exception as exc:  # noqa: BLE001 - whatever stops the cut: the pod is ended instead
            error = printable(str(exc))[:300] or type(exc).__name__
    else:
        error = "the run has no control directory"
    if cut is None:
        LOG.warning("pod", f"the pod {pod} runs on and its network could not be cut ({error}): kill it")
        try:
            subprocess.run(podman_command(a, "pod", "kill", pod), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           check=False, timeout=OUTLIVED_KILL_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("pod", f"podman pod kill {pod}: {LOG.exception_text(exc)}")
        if live.pod_live(pod, a.podman_url, fresh=True) is False:
            LOG.warning("pod", f"killed the pod {pod}, which Podman could not remove ({why}); sandbox-ai gc removes it")
            print(f"agent-sandbox: warning: Podman could not remove the pod {pod} ({printable(why)}): it was killed "
                  "instead; sandbox-ai gc removes it", file=sys.stderr)
            return None
    name = printable(current_run_name(a) if directory is not None else a.run_id) or a.run_id
    reason = f"Podman could not remove its pod {pod} at the end of the run ({why})"
    a.sandbox_outlived = {"why": reason, "cut": cut, "cut_error": error, "pod": pod}
    if directory is not None:
        live.record_supervisor_end(directory, reason, "launcher", cut=cut, cut_error=error, pod=pod, run=a.run_id)
    state = (f"its network is cut (kill switch {cut}): nothing leaves it" if cut else
             f"its network could not be cut ({error}) and Podman did not end it either - end it now: podman pod rm "
             f"--force {pod}")
    text = (f"the sandbox {name} runs on without its supervisor: {printable(reason)}; {state}. Save your work in it, "
            f"then: sandbox-ai stop {name}, then: sandbox-ai resume {name}")
    LOG.error("pod", text)
    print(f"agent-sandbox: error: {text}", file=sys.stderr)
    with contextlib.suppress(Exception):
        approvals = load_approvals_module()
        if hasattr(approvals, "phone"):  # the phone channels (sandbox-ai notify): an alarm
            approvals.phone("alarms", "the sandbox runs on without its supervisor",
                            "network cut" if cut else "its network could not be cut", run=name)
        approvals.desktop_notification(f"sandbox-ai: {name} runs without its supervisor", text, alarm=True)
    return a.sandbox_outlived


def unless_outlived(a, function, *args):
    """FUNCTION(*ARGS) - a step of the run's cleanup that removes what is bound into its sandbox - unless the sandbox
    outlives this launcher (sandbox_outlived): its agent still works in it, and its run directory is how `sandbox-ai
    stop` finds it. Left for liveness.discard, which removes them once the run has ended for certain."""
    if getattr(a, "sandbox_outlived", None):
        return None
    return function(*args)


def remove_tree(path):
    """Remove the folder PATH as tempfile.TemporaryDirectory does: also entries that were made read-only (a kit's
    files, the agent's own); what cannot be removed stays (never an error of the run's end). Only below PATH is a
    mode changed."""
    top = os.path.abspath(path)
    if not os.path.isdir(top) or os.path.islink(top):
        return

    def writable(function, failed, _info):
        with contextlib.suppress(OSError):
            for folder in (os.path.dirname(failed), failed):
                if (folder == top or folder.startswith(top + os.sep)) and os.path.isdir(folder) and \
                        not os.path.islink(folder):
                    os.chmod(folder, 0o700)
            if os.path.isdir(failed) and not os.path.islink(failed):
                shutil.rmtree(failed, ignore_errors=True)  # (once: a folder that cannot go never loops)
            else:
                function(failed)
    shutil.rmtree(top, onerror=writable)


def bwrap_command(a, session, home):
    cmd = ["bwrap", "--unshare-all", "--unshare-user", "--uid", "0", "--gid", "0", "--cap-add", "ALL",
           "--as-pid-1", "--die-with-parent", "--clearenv", "--hostname", "agent-sandbox",
           "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        p = Path(path)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), path]
        elif p.exists():
            cmd += ["--ro-bind", path, path]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--dir", "/var/tmp", "--dir", "/etc", "--tmpfs", WORKSPACES_DIR]
    if a.gui or a.gui_wayland:
        cmd += ["--tmpfs", "/dev/shm"]
    for path in ("/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/firejail", "/etc/localtime", "/etc/ld.so.cache", "/etc/login.defs"):
        if Path(path).exists():
            cmd += ["--ro-bind", path, path]
    for name in ("hosts", "resolv.conf", "nsswitch.conf", "passwd", "group"):
        cmd += ["--ro-bind", str(session / name), "/etc/" + name]
    cmd += ["--ro-bind", str(HERE / "runtime.py"), "/sandbox/runtime.py",
            "--ro-bind-try", str(HERE / "public_suffix_list.dat"), "/sandbox/public_suffix_list.dat",  # runtime.py's
            "--ro-bind", str(session), "/sandbox/session", "--bind", str(home), "/root"]
    for index, (src, dest) in enumerate(workspace_targets(a)):
        cmd += ["--ro-bind" if workspace_mode(a, index) == "ro" else "--bind", str(Path(src).expanduser().resolve()),
                dest]
    for src, dest, _ in hidden_mounts(a, session):  # --hide: over what it matches in the workspaces, after them
        cmd += ["--ro-bind", str(src), dest]
    for src, dest, _ in toolchain_mounts(a, session):  # /opt/toolchains and each --toolchain on it (nosuid, nodev)
        cmd += ["--ro-bind", str(src), dest]
    for value in a.mount:
        src, dest, mode = parse_mount(value)
        cmd += ["--ro-bind" if mode == "ro" else "--bind", src, dest]
    for src, dest, mode in getattr(a, "kit_mounts", []):
        cmd += ["--ro-bind" if mode == "ro" else "--bind", str(src), dest]
    if a.log_dir:
        cmd += ["--bind", str(a.log_dir.expanduser().resolve()), str(LOG_DIR)]
    if a.gui_vnc:
        cmd += ["--bind", str(a.gui_vnc.expanduser().resolve()), str(VNC_DIR)]
    if a.tmux_socket_dir:
        cmd += ["--bind", str(a.tmux_socket_dir.expanduser().resolve()), TMUX_DIR]
    if a.mcp_socket_dir:
        cmd += ["--bind", str(a.mcp_socket_dir.expanduser().resolve()), MCP_SERVE_DIR]
    if a.mcp_approvals_dir:
        cmd += ["--ro-bind", str(a.mcp_approvals_dir.expanduser().resolve()), MCP_APPROVALS_DIR]
    for name in dict.fromkeys(a.mcp):  # reached through the supervisor's relay (MCP_AGENT_DIR), never directly
        cmd += ["--ro-bind", str(mcp_socket_dir(mcp_instance(a, name))), f"{MCP_UPSTREAM_DIR}/{name}"]
    if hook_mount(a, session):  # the agent's managed configuration: its hooks, which it cannot change
        source, target = hook_mount(a, session)
        cmd += ["--ro-bind", str(source), target]
    for source, target in companion_hook_mounts(a, session):
        cmd += ["--ro-bind", str(source), target]
    for source, target in companion_managed_mounts(a, session):  # Hermes', Qwen Code's ... --with: read-only too
        cmd += ["--ro-bind", str(source), target]
    for source, target in telemetry_guard_mounts(a, session):  # the agents' managed configuration (telemetry off)
        cmd += ["--ro-bind", str(source), target]
    if a.gui_wayland:
        runtime = str(PurePosixPath(WAYLAND_TARGET).parent)
        cmd += ["--perms", "0700", "--dir", runtime, "--bind", str(wayland_socket()), WAYLAND_TARGET]
    if getattr(a, "proxy_dir", None):
        cmd += ["--ro-bind", str(a.proxy_dir), str(PROXY_DIR)]
        if package_guard_mount(a):
            cmd += ["--ro-bind", str(package_guard_mount(a)), str(PACKAGES_DIR)]
    if getattr(a, "control_dir", None):
        cmd += ["--bind", str(a.control_dir), str(CONTROL_DIR)]
    if getattr(a, "browser_dir", None):
        cmd += ["--ro-bind", str(a.browser_dir), str(BROWSER_DIR)]
    for path, kind in devices(a):
        if kind == "cdi":
            raise ValueError("CDI devices need the Podman backend")
        cmd += ["--dev-bind", path, path]
    for directory in a.runtime:
        path = Path(directory).expanduser().resolve(strict=True)
        if not path.is_dir() or not any(base in path.parents for base in
                                        map(Path, ("/usr", "/opt", "/home", "/mnt", "/srv"))):
            raise ValueError("Invalid --runtime directory")
        cmd += ["--ro-bind", str(path), str(path)]
    for key, value in base_env().items():
        cmd += ["--setenv", key, value]
    return cmd + ["--chdir", "/work"]


TIOCSTI_SYSCTL = Path("/proc/sys/dev/tty/legacy_tiocsti")


def has_controlling_terminal():
    """bwrap keeps the caller's controlling terminal (reachable as /dev/tty) even with 0, 1 and 2 redirected."""
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY))
        return True
    except OSError:
        return False


def check_tiocsti():
    """bwrap keeps the caller's terminal; TIOCSTI on it would type commands into the host shell."""
    try:
        enabled = TIOCSTI_SYSCTL.read_text().strip() != "0"
    except OSError:
        enabled = True  # Kernels before 6.2 always allow TIOCSTI.
    if enabled:
        raise ValueError("This kernel lets programs inject input into a terminal (TIOCSTI); with bwrap the agent "
                         "could type commands into your shell. Use --backend podman, or run "
                         "`sysctl dev.tty.legacy_tiocsti=0` (kernel 6.2 or newer)")


def read_ready(fd, process, timeout=15):
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.1)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError(f"Sandbox setup exited early (status {process.poll()})")
            data += chunk
            try:
                return json.loads(data)
            except (json.JSONDecodeError, RecursionError):
                if data == b"1":
                    return True
        if process.poll() is not None:
            raise RuntimeError(f"Sandbox setup failed with status {process.returncode}")
    raise RuntimeError("Timed out waiting for namespace/network setup")


def subordinate_range(kind):
    username = pwd.getpwuid(os.getuid()).pw_name
    for line in Path("/etc/sub" + kind).read_text().splitlines():
        fields = line.split(":")
        if len(fields) != 3 or fields[0] not in (username, str(os.getuid())):
            continue
        start, count = int(fields[1]), int(fields[2])
        if start > 0 and count >= 65536:
            return start, count
    raise ValueError(f"Bubblewrap + Firejail needs a delegated range of at least 65536 IDs in /etc/sub{kind}")


def configure_user_maps(pid):
    # Unlike bwrap's single-ID self-mapping (which permanently denies
    # setgroups), newgidmap permits setgroups when a delegated subgid range
    # is included. Firejail's drop_privs() requires this even as namespace root.
    for kind, real in [("uid", os.getuid()), ("gid", os.getgid())]:
        start, count = subordinate_range(kind)
        subprocess.run(["new" + kind + "map", str(pid), "0", str(real), "1",
                        "1", str(start), str(count)], check=True)
    if Path(f"/proc/{pid}/setgroups").read_text().strip() != "allow":
        raise RuntimeError("User namespace denies setgroups, which Firejail requires")


def run_bwrap(a, session, home):
    cmd = bwrap_command(a, session, home)
    info_r = info_w = maps_r = maps_w = -1
    gate_r = gate_w = ready_r = ready_w = -1
    child = slirp = None
    try:
        # Allocation itself can fail. Keep every successfully allocated pipe
        # inside the cleanup scope, even before the first process exists.
        info_r, info_w = os.pipe()
        maps_r, maps_w = os.pipe()
        gate_r, gate_w = os.pipe()
        ready_r, ready_w = os.pipe()
        prefix = limits_prefix(a)
        child = subprocess.Popen(prefix + cmd + ["--info-fd", str(info_w), "--userns-block-fd", str(maps_r), "--block-fd", str(gate_r),
                                       "/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-bootstrap"],
                                 pass_fds=(info_w, maps_r, gate_r), env=user_bus_environment() if prefix else None)
        a.sandbox_pid = child.pid  # the memory watch finds the run's cgroup (systemd-run's scope) through it
        os.close(info_w)
        info_w = -1
        os.close(gate_r)
        gate_r = -1
        os.close(maps_r)
        maps_r = -1
        try:
            pid = int(read_ready(info_r, child)["child-pid"])
        except RuntimeError as exc:  # Bubblewrap's own message is above it
            note = engine_bind_note(a, "Bubblewrap")
            if note:
                raise RuntimeError(f"{exc}; {note}") from None
            raise
        a.running_pid = child.pid  # a canary's pause (canary.py) freezes this process tree
        configure_user_maps(pid)
        os.write(maps_w, b"1")
        os.close(maps_w)
        maps_w = -1
        if not a.offline:
            slirp = subprocess.Popen(slirp_command(a, pid, ready_w), pass_fds=(ready_w,),
                                     stdout=subprocess.DEVNULL)
            os.close(ready_w)
            ready_w = -1
            read_ready(ready_r, slirp)
        os.write(gate_w, b"1")
        os.close(gate_w)
        gate_w = -1
        return wait_child(child)
    finally:
        with signals_deferred():
            stop_process(child)
            stop_process(slirp)
            for fd in (info_r, info_w, maps_r, maps_w, gate_r, gate_w, ready_r, ready_w):
                if fd >= 0:
                    os.close(fd)


def load_sessions_module():
    # Launchers run with -I, so the script directory is not on sys.path.
    spec = importlib.util.spec_from_file_location("sandbox_sessions", HERE / "sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This launcher for it (a kept start's directory, sessions.begin): it loaded agent_sandbox.py and the runtime a
    # second time, about 25 ms of a start.
    module._launcher = RunningLauncher()
    return module


def load_canary_module():
    spec = importlib.util.spec_from_file_location("sandbox_canary", HERE / "canary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_safeguards_module = None


def load_safeguards_module():
    global _safeguards_module
    if _safeguards_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_safeguards", HERE / "safeguards.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _safeguards_module = module
    return _safeguards_module


def start_safeguards(a, connection_log, cfg):
    """The run's safeguard watch (safeguards.py), started: every safeguard that refuses or ends something becomes a
    record (safeguards.jsonl) and a desktop warning, grouped and rate-limited. The values of the agent's secret
    variables (--env, --secret-env, and names like the activity log's) are redacted wherever they appear. None for a
    probe; never fails the launch."""
    if a.probe:
        return None
    try:
        S = load_safeguards_module()
        env, names = cfg.get("env") or {}, set(cfg.get("secret_names") or [])
        secret_values = [value for name, value in env.items() if isinstance(value, str) and
                         (name in names or re.search(r"token|secret|passw|api_?key|apikey|auth|credential|private"
                                                     r"|cookie|session_?key", name, re.I))]
        return S.Watch(a.run_id, log_dir=a.log_dir, control_dir=getattr(a, "control_dir", None),
                       connection_log=connection_log, notify=S.desktop_sender(load_approvals_module()),
                       secrets=secret_values).start()
    except Exception as exc:  # noqa: BLE001 - the run goes on without its safeguard records
        print(f"agent-sandbox: safeguards: not recorded: {printable(str(exc))[:300]}", file=sys.stderr)
        return None


def plant_canaries(a, home, cleanup):
    """The run's canary files (canary.py), planted in HOME and the workspaces; removed (or kept with a kept HOME)
    when CLEANUP ends. None when the run has none."""
    plan = load_canary_module().plant(a, home, argparse.Namespace(**globals()))
    if plan is None:
        return None
    def finish():
        for note in plan.finish():
            print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    cleanup.callback(finish)
    for note in plan.notes:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    return plan if plan.entries else None


def load_activity_module():
    spec = importlib.util.spec_from_file_location("sandbox_activity", HERE / "activity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This launcher and its runtime for it: the run's timeline loaded the runtime a second time to redact its record.
    module._modules.update(runtime=RUNTIME, agent_sandbox=RunningLauncher())
    return module


def start_activity(a, home, log_name, cfg):
    """The timeline of this run (activity.py) while the agent works, or None: for agent sandboxes with a log
    directory. Never fails the launch."""
    if not a.log_dir or not kits_apply(a):
        return None
    try:
        return load_activity_module().start(a, home, log_name, cfg["env"], cfg["secret_names"],
                                            [{"host": str(src), "sandbox": str(dest)}
                                             for src, dest in workspace_targets(a)])
    except Exception as exc:  # noqa: BLE001 - the sandbox runs without a timeline
        print(f"agent-sandbox: activity: not recorded: {printable(exc)}", file=sys.stderr)
        return None


_devices_module = None


def load_devices_module():
    """devices.py (sysfs identity of devices, the run's device grants and their watcher), loaded once."""
    global _devices_module
    if _devices_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_devices", HERE / "devices.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _devices_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _devices_module = loaded
    return _devices_module


def start_devices(a):
    """The run's device grants in devices.json and its device rules in device-rules.json (both in the run's
    directory, for `sandbox-ai devices` and the watcher); the run record when a watcher is needed (a device on
    USB to follow through unplug and replug, or one held for a question), else None."""
    module = load_devices_module()
    rules = [{"name": source["name"], "version": 1, **audit_mode(source),
              "defaults": {"devices": source["defaults"]["devices"]}, "devices": source["devices"]} for source in load_rulesets(a)
             if source["devices"] or source["defaults"]["devices"] != "allow"]
    path = a.control_dir / "device-rules.json"
    path.write_text(json.dumps(rules))
    path.chmod(0o600)
    grants = device_grants(a) if (a.device or a.gpu) and kits_apply(a) else []
    record = {**json.loads((a.control_dir / "run.json").read_text()), "directory": str(a.control_dir)}
    if not grants:
        return None
    module.start_grants(record, grants)
    return record if any(g.get("track") or g["state"] == "held" for g in grants) else None


def start_mcp_servers(mcp, a, names, behind, progress):
    """Start the MCP servers NAMES of the start A (each behind the VPN BEHIND[name], if any): each begun in turn
    (mcp.begin_running: its sandbox or bridge started, its lock held), then waited for in the same order - their
    sandboxes come up side by side (a start with three command servers waited for three sandbox starts one after
    another). Each one begun is waited for, also when a later one fails to begin or an earlier one to come up (its
    lock let go, its network checked); then the first server's error in their order is raised."""
    waits, failed = [], None
    for number, name in enumerate(names):
        progress.step(f"starting the MCP server {printable(name)} (its image is pulled once, its sandbox started)"
                      + (f" behind the VPN {printable(behind[name])}" if behind.get(name) else ""))
        try:
            waits.append(mcp.begin_running(name, a, vpn=behind.get(name)))
        except Exception as exc:  # noqa: BLE001 - raised below, once those begun are waited for
            LOG.warning("mcp", f"the MCP server {name} could not be started for the start", exc)
            failed = (number, exc)
            break
    if len(waits) > 1:
        progress.step(f"waiting for the MCP servers {', '.join(printable(name) for name in names[:len(waits)])}")
    for number, wait in enumerate(waits):
        begun = time.monotonic()
        try:
            wait()
            LOG.info("mcp", f"the MCP server {names[number]} is up for the start (waited {time.monotonic() - begun:.1f} s)")
        except Exception as exc:  # noqa: BLE001 - the earliest server's error is raised
            if getattr(exc, "launch_logged", False):  # (mcp.py said why, in the line before)
                LOG.warning("mcp", f"the MCP server {names[number]} did not come up for the start "
                                   f"({type(exc).__name__}, said above)")
            else:
                LOG.warning("mcp", f"the MCP server {names[number]} did not come up for the start", exc)
            if failed is None or number < failed[0]:
                failed = (number, exc)
    if failed is not None:
        raise failed[1]


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("sandbox_mcp", HERE / "mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._launcher = RunningLauncher()  # (as load_sessions_module: every start asks it, for the agent's configuration)
    return module


def launcher_namespace():
    """This launcher as other modules see it (what a caller replaced in it included)."""
    return argparse.Namespace(**globals())


def load_vpn_module():
    """vpn.py, which sees this launcher as loaded (what a caller replaced in it included)."""
    spec = importlib.util.spec_from_file_location("sandbox_vpn", HERE / "vpn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.launcher = lambda: argparse.Namespace(**globals())
    return module


def terminate(signum, _frame):
    # SIGTERM/SIGHUP become a normal exit, so every cleanup in a finally block
    # runs (pod removal, temporary state, Claude write-back), even during startup.
    raise SystemExit(128 + signum)


CLEANUP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@contextlib.contextmanager
def signals_deferred():
    """Hold SIGTERM, SIGHUP and SIGINT while cleanup runs and deliver them afterwards: an interrupted
    `podman pod rm` or slirp4netns stop leaves the pod or the process behind.

    Commands run inside (podman pod rm) inherit the blocked mask (exec keeps it). That is intended:
    Ctrl-C and a terminal hangup reach the whole foreground process group, podman included, and podman
    must finish its job too; it exits when done, so nothing long-lived keeps the mask."""
    if threading.current_thread() is not threading.main_thread():
        # Python runs signal handlers in the main thread only; a mask set here would not hold them.
        yield
        return
    caught, previous = [], {}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
    try:
        for sig in CLEANUP_SIGNALS:
            if signal.getsignal(sig) not in (signal.SIG_IGN, None):
                # The mask covers this thread only: a signal sent to the process can still land on a
                # helper thread (approval notifications), and CPython then runs the handler here.
                previous[sig] = signal.signal(sig, lambda signum, _frame: caught.append(signum))
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)  # signals held by the mask arrive here
        for sig in dict.fromkeys(caught):
            signal.raise_signal(sig)


# Arguments that print something and exit instead of starting the agent's interface.
NON_INTERACTIVE_ARGS = {"-p", "--print", "--version", "-v", "-V", "--help", "-h"}


def interactive_args(agent, args):
    """True if the agent arguments start the agent's interactive interface: options (with their values)
    only. A subcommand or prompt (claude mcp list, codex exec, opencode run, a first message) or an
    option we do not know with a value may run something that exits, whose output tmux would hide."""
    sessions = load_sessions_module()
    # (an interactive session's first task - OpenCode's --prompt TEXT - takes a value as well)
    values = sessions.VALUE_OPTIONS[agent] | sessions.PROMPT_OPTIONS.get(agent, set())
    # One task and exit, also as NAME=VALUE (Hermes' --oneshot=TASK, Qwen Code's --prompt TASK: sessions.py's own).
    one_shot = {*NON_INTERACTIVE_ARGS, "-z", "--oneshot", *sessions.ONE_SHOT_OPTIONS.get(agent, ())}
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-") or arg == "-" or arg in NON_INTERACTIVE_ARGS or arg.split("=", 1)[0] in one_shot:
            return False
        index += 2 if arg in values else 1
    return True


def resolve_keep(a):
    """Whether this start keeps its HOME (a.keep_home), and a note when an option is ignored: --session always
    does; else --keep/--no-keep or the project file's "keep" as chosen, by default yes. Never for a session's
    own supervisor or an MCP server (their HOME is chosen for them), probes or a HOME of your own (--state-dir,
    which stays anyway); the administrator's policy forbid_keep and the security mode paranoid discard every
    HOME (paranoid refuses --keep and --session, and a rebirth of a kept HOME)."""
    if a.tmux_socket_dir or a.mcp_socket_dir or a.probe:
        a.keep_home = False
        if getattr(a, "mode", None) == "paranoid" and kept_start_dir(a) is not None:
            # A rebirth of a kept start whose mode is paranoid now (its options, project file or defaults).
            raise ValueError("the security mode paranoid keeps no HOME beyond a run, so this start's kept HOME "
                             "cannot be used again; start anew (its HOME is discarded at the end), or choose a laxer "
                             "mode (--mode high keeps HOMEs)")
        return None
    if getattr(a, "mode", None) == "paranoid":
        a.keep_home = False  # every start discards its HOME (an explicit --keep was refused by modes.check)
        return None
    if a.state_dir:
        a.keep_home = False
        return "--keep has no effect with --state-dir: that HOME is yours and stays anyway" if a.keep else None
    if getattr(a, "forbid_keep", False):
        a.keep_home = False
        return ("the administrator's policy discards the HOME when the start ends (forbid_keep); --keep is ignored"
                if a.keep else None)
    if a.session is not None:
        a.keep_home = True
        return "--session keeps its HOME; --no-keep is ignored" if a.keep is False else None
    a.keep_home = a.keep is not False
    return None


def plan_kept_home(a):
    """A kept start in the foreground (a task, no terminal, --no-tmux) gets its start directory's home/ as
    --state-dir, chosen before the checks so that they cover it like any --state-dir (sessions.begin makes it)."""
    if not a.keep_home or detachable(a) or a.dry_run or a.state_dir:
        return False
    if not re.fullmatch(SESSION_NAME, a.start_name or ""):
        return False  # validate() refuses the name
    sessions = load_sessions_module()
    a.start_key = sessions.new_key(a.start_name)
    a.state_dir = sessions.session_dir(a.start_key) / "home"
    a.foreground_start = True
    return True


def expire_kept(a, defaults):
    """With "keep_days" in your defaults file: remove the stopped kept starts unused for longer (starts.gc);
    the names of those removed. Not for plans, probes and the starts of sessions and MCP servers."""
    days = defaults.get("keep_days")
    if not days or a.dry_run or a.probe or a.tmux_socket_dir or a.mcp_socket_dir:
        return []
    try:
        return [entry["name"] for entry in load_starts_module().gc(days, expired_only=True)["removed"]]
    except Exception as exc:  # noqa: BLE001 - an expiry that fails must not stop a start
        print(f"agent-sandbox: kept starts not expired: {printable(exc)}", file=sys.stderr)
        return []


def expiry_notice(a, defaults):
    """Without keep_days in your defaults: while the default expiry's grace runs (starts.expiry_notice), say once a day
    which kept starts sandbox-ai gc and its daily pass remove from when on, and how to keep them. Not for plans,
    probes and the starts of sessions and MCP servers; never fatal."""
    if "keep_days" in defaults or a.dry_run or a.probe or a.tmux_socket_dir or a.mcp_socket_dir:
        return None
    try:
        text = load_starts_module().expiry_notice()
    except Exception:  # noqa: BLE001 - a notice that fails must not stop a start
        return None
    if text:
        print(f"agent-sandbox: note: {printable(text)}", file=sys.stderr)
    return text


def log_days_notice(a, defaults):
    """Without log_days in your defaults: while the default age limit of the logs waits out its grace
    (starts.log_days_notice), say once a day from when sandbox-ai gc and its daily pass remove the logs older than 30
    days, and how to keep them. Not for plans, probes and the starts of sessions and MCP servers; never fatal."""
    if "log_days" in defaults or a.dry_run or a.probe or a.tmux_socket_dir or a.mcp_socket_dir:
        return None
    try:
        text = load_starts_module().log_days_notice()
    except Exception:  # noqa: BLE001 - a notice that fails must not stop a start
        return None
    if text:
        print(f"agent-sandbox: note: {printable(text)}", file=sys.stderr)
    return text


def kept_start_dir(a):
    """The directory of the start whose kept HOME this run uses (a session's or a task's), or None."""
    key = getattr(a, "start_key", None) or a.start_id
    if not key or not a.state_dir:
        return None
    directory = load_sessions_module().session_dir(key)
    try:
        same = Path(a.state_dir).expanduser().resolve() == (directory / "home").resolve()
    except OSError:
        return None
    return directory if same else None


_lint_module = None


def load_lint_module():
    """lint.py: the hidden-text checker (invisible and bidi characters, instruction phrasing aimed at the model)."""
    global _lint_module
    if _lint_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_lint", HERE / "lint.py")
        with LOADING:
            if _lint_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _lint_module = loaded
    return _lint_module


def check_instruction_files(a, report=None, say=None):
    """Before the agent reads them: the workspaces' agent instruction files (CLAUDE.md, AGENTS.md, .cursorrules,
    .github/copilot-instructions.md, .claude/skills ...) through the hidden-text checker (lint.py). Each workspace with
    findings: a warning (SAY, default: standard error) and a safeguard record of kind hidden-text (REPORT: the
    safeguard watch's report). Nothing is changed or refused: the human decides. The findings ([(sandbox path,
    finding)]); never fails the start."""
    if not kits_apply(a):
        return []
    say = say or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    lint, found = load_lint_module(), []
    for source, target in workspace_targets(a):
        try:
            findings = lint.check_instructions(Path(source).expanduser())
        except Exception as exc:  # noqa: BLE001 - the start goes on; say that nothing was checked
            say(f"warning: the agent instruction files in {printable(target)} could not be checked: "
                f"{printable(str(exc))[:200]}")
            continue
        if not findings:
            continue
        found += [(target, item) for item in findings]
        files = sorted({item["file"] for item in findings})
        say(f"warning: hidden text in the agent instruction files of {printable(target)} "
            f"({lint.summary(findings)}): the agent reads these files on its own - look at them before you trust "
            f"what it does with them (the escapes show what is invisible):")
        for line in lint.lines(findings, limit=8):
            say(f"  {target}/{printable(line)}")
        if report is not None:
            with contextlib.suppress(Exception):
                report("hidden-text", f"{', '.join(files[:4])}{' ...' if len(files) > 4 else ''} in {target}",
                       f"{lint.summary(findings)}; first: {lint.line_of(findings[0])}",
                       f"look at the files in {source} (the record's details show the escapes); nothing was changed "
                       "and the start went on", target=target,
                       details={"findings": len(findings), "files": len(files), "first": lint.line_of(findings[0])})
    return found


_hide_module = None


def load_hide_module():
    """hide.py: --hide GLOB, the workspace files kept out of the agent's sight."""
    global _hide_module
    if _hide_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_hide", HERE / "hide.py")
        with LOADING:
            if _hide_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _hide_module = loaded
    return _hide_module


def hide_glob(value):
    """--hide VALUE checked (hide.check_glob), for argparse."""
    try:
        return load_hide_module().check_glob(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


HIDDEN_DIR = "hidden"  # in the session folder: the empty file and folder mounted over what --hide matches


def plan_hidden(a, session=None, say=None):
    """--hide: what the globs match in each workspace now (hide.plan; before the canaries are planted, so a canary
    .env is never hidden), said (SAY, default: standard error); with SESSION the empty read-only file and folder that
    are mounted over them are made there. Matches whose path a Podman bind cannot carry (a comma, a line break) are
    left out, said. Sets a.hide_plan; returns it ({"hidden", "notes"})."""
    say = say or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    globs = list(dict.fromkeys(getattr(a, "hide", None) or []))
    if not globs or not kits_apply(a):
        a.hide_plan = {"hidden": [], "notes": []}
        return a.hide_plan
    plan = load_hide_module().plan([(Path(src).expanduser().resolve(), dest) for src, dest in workspace_targets(a)],
                                   globs)
    kept = []
    for item in plan["hidden"]:
        if any(c in item["sandbox"] for c in (",", "\n", "\r")):
            plan["notes"].append(f"{printable(item['sandbox'])}: not hidden (a comma or line break in its path)")
        else:
            kept.append(item)
    plan["hidden"] = kept
    if session is not None and kept:
        directory = Path(session) / HIDDEN_DIR
        (directory / "dir").mkdir(mode=0o755, parents=True, exist_ok=True)
        (directory / "file").write_bytes(b"")
        (directory / "file").chmod(0o444)
        (directory / "dir").chmod(0o555)
    a.hide_plan = plan
    if kept:
        shown = [item["sandbox"] for item in kept]
        say(f"hidden from the agent (--hide): {', '.join(printable(path) for path in shown[:10])}"
            + (f" and {len(shown) - 10} more" if len(shown) > 10 else "")
            + " (an empty read-only file or folder in their place; files made later are not hidden)")
    else:
        say(f"--hide {', '.join(printable(glob) for glob in globs)}: nothing matches in the workspaces now")
    for note in plan["notes"]:
        say(f"warning: {printable(note)}")
    return plan


def hidden_mounts(a, session):
    """[(source, target, "ro")]: the empty file or folder of SESSION over each path a.hide_plan hides, mounted after
    the workspaces (Podman and Bubblewrap alike)."""
    directory = Path(session) / HIDDEN_DIR
    return [(directory / ("dir" if item["kind"] == "dir" else "file"), item["sandbox"], "ro")
            for item in (getattr(a, "hide_plan", None) or {}).get("hidden", [])]


_workspace_review_module = None


def load_workspace_review_module():
    """workspace_review.py: what the agent changed in the workspaces that the host runs or trusts."""
    global _workspace_review_module
    if _workspace_review_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_workspace_review", HERE / "workspace_review.py")
        with LOADING:
            if _workspace_review_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _workspace_review_module = loaded
    return _workspace_review_module


def record_workspace_review(a):
    """Right before the agent runs: the files of each workspace the host runs or trusts (Git hooks and configuration,
    editor tasks, agent settings, build scripts, CI workflows, instruction files), recorded on the host
    (workspace_review.record); None for a probe or an MCP server's sandbox, or when it fails (said, never fatal)."""
    if not kits_apply(a):
        return None
    try:
        return load_workspace_review_module().record([Path(src).expanduser().resolve()
                                                      for src, _ in workspace_targets(a)])
    except Exception as exc:  # noqa: BLE001 - the run goes on without a review
        print(f"agent-sandbox: warning: the workspace files the host trusts were not recorded ({printable(str(exc))}); "
              "there is no review of them after this run", file=sys.stderr)
        return None


def review_workspaces_after_run(a, manifest, report=None, say=None):
    """After the run: every change to the files record_workspace_review recorded, said with its diff (SAY, default:
    standard error), as one safeguard record of kind workspace-review (REPORT: the safeguard watch's) and a line of
    the log directory's workspace-review.jsonl (sandbox-ai workspace review). Nothing is reverted. The changes."""
    if not manifest:
        return []
    say = say or (lambda line: print(f"agent-sandbox: {line}", file=sys.stderr))
    module = load_workspace_review_module()
    mapping = {str(Path(src).expanduser().resolve()): dest for src, dest in workspace_targets(a)}
    try:
        changes = module.compare(manifest, mapping)
    except Exception as exc:  # noqa: BLE001 - said as a change would be: look yourself
        say(f"warning: the workspace files the host trusts could not be compared ({printable(str(exc))}); check "
            ".git/hooks, .git/config, .vscode, .claude and the like before you run anything from the workspace")
        return []
    if not changes:
        return []
    say(f"warning: the agent changed {len(changes)} file(s) of the workspace that your host runs or trusts - look "
        "at them before your next commit, IDE start or install (nothing was reverted):")
    for line in module.summary_lines(changes):
        say(f"  {printable(line)}")
    where = None
    if a.log_dir:
        try:
            where = module.save(Path(a.log_dir).expanduser(), a.run_id, changes)
        except OSError as exc:
            say(f"warning: the workspace review was not saved: {printable(str(exc))}")
    try:  # the before-state for Restore: in sandbox-ai's state folder, which no sandbox reaches (never the log folder)
        module.keep_before(Path(a.log_dir).expanduser() if a.log_dir else
                           default_log_dir(Path(workspace_targets(a)[0][0]).expanduser().resolve()), a.run_id, changes)
    except (OSError, ValueError, IndexError) as exc:
        say(f"warning: the files' state at the start was not kept: a restore needs the start's checkpoint "
            f"({printable(str(exc))})")
    if report is not None:
        paths = [f"{item['sandbox']}/{item['path']}" for item in changes]
        with contextlib.suppress(Exception):
            report("workspace-review", f"{', '.join(paths[:4])}{' ...' if len(paths) > 4 else ''}",
                   f"{len(changes)} file(s) the host runs or trusts changed during the run: {changes[0]['why']}",
                   "look at the diffs: sandbox-ai workspace review" + (f" --log-dir {where.parent}" if where else "")
                   + "; restore a file from the start's checkpoint: sandbox-ai checkpoint restore",
                   target=changes[0]["sandbox"], details={"changes": len(changes), "first": paths[0][:200]})
    return changes


def record_home_manifest(a, home):
    """Right before the sandbox starts: record what the start files of a kept HOME are now (home_review.py), in
    the start's directory, where the agent cannot reach it; the next rebirth names what changed since. True if
    recorded; a failure is reported, never fatal."""
    directory = kept_start_dir(a)
    if directory is None:
        return False
    try:
        spec = importlib.util.spec_from_file_location("sandbox_home_review", HERE / "home_review.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.record(home, directory, run=a.run_id)
        return True
    except Exception as exc:  # noqa: BLE001 - the start goes on; the next rebirth has no manifest to compare
        print(f"agent-sandbox: the start files of the HOME were not recorded: {printable(exc)}", file=sys.stderr)
        return False


def detachable(a):
    """This start runs as a detachable session: interactive in tmux (resolve_tmux), not a probe, MCP server or
    a session's own supervisor. Closing the terminal then detaches instead of ending the agent."""
    return a.session is not None or (bool(a.tmux) and not (a.probe or a.tmux_socket_dir or a.mcp_socket_dir))


_progress_module = None


def load_progress_module():
    """progress.py: the lines a start says while it prepares (and podman's build and pull output, bounded)."""
    global _progress_module
    if _progress_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_progress", HERE / "progress.py")
        with LOADING:
            if _progress_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _progress_module = loaded
    return _progress_module


def resolve_tmux(a):
    """--tmux/--no-tmux as given; by default tmux for every interactive start from a terminal, but not
    for probes, sessions (tmux already), MCP servers, other binaries, commands that print and exit, or
    bwrap without a host tmux."""
    if a.tmux is None:
        a.tmux = bool(not (a.probe or a.session is not None or a.tmux_socket_dir or a.mcp_socket_dir
                           or a.binary) and sys.stdin.isatty() and sys.stdout.isatty()
                      and interactive_args(a.agent, a.agent_args) and (a.backend == "podman" or shutil.which("tmux")))
    return a.tmux


USAGE_ERROR = re.compile(r"^sandbox-ai(?: [\w-]+)?: error: ")


def marked(text, stream, error=False):
    """TEXT as the launcher says it on STREAM: on a terminal its messages marked ("agent-sandbox: warning: ..."
    yellow, an error red: textout.message; ERROR: TEXT is what ended the start), else - a pipe, a log file, NO_COLOR,
    TERM=dumb, --no-color - as it is. textout.py is loaded only for a terminal."""
    if not isinstance(text, str) or not text or os.environ.get("NO_COLOR") or \
            not (os.environ.get("FORCE_COLOR") or getattr(stream, "isatty", lambda: False)()):
        return text
    try:
        return load_helpstyle().textout().message(text, stream=stream, error=error)
    except (OSError, ImportError, AttributeError, ValueError):  # (no textout.py beside it: the text as it is)
        return text


class SteadyStream:
    """The launcher's standard output or error while it runs: a write that fails - the terminal closed (EIO after
    its hangup), a session's supervisor.log on a full disk (ENOSPC, EDQUOT) - loses that text instead of raising.
    The clean-up at a run's end says what it does: one such line that raised skipped the rest of its step and every
    step after it in that block (the login written back, the project memory, the privacy check, the activity
    record, the run's exit in its history). Everything else is the stream's own. TEE (standard error): each whole
    line the launcher says ("agent-sandbox: ...") goes to launcher.log too (LOG), so a start in the foreground leaves
    what it said there as a session's supervisor.log does."""

    def __init__(self, stream, tee=False):
        self._stream = stream
        self._tee, self._pending, self._lock = tee, "", threading.Lock()

    def write(self, text):
        shown = text
        if self._tee and isinstance(text, str):
            if not self._pending:  # (a text that starts a line: on a terminal, a message of the launcher marked)
                shown = marked(text, self._stream)
            self._said(text)
        try:
            self._stream.write(shown)
            return len(text)
        except OSError:
            return len(text)

    def _said(self, text):
        with self._lock:
            *lines, self._pending = (self._pending + text).split("\n")
            self._pending = self._pending[-4000:]
        if LOG.relaying():
            return  # a session's supervisor's lines, said here too: it logged them itself
        for line in lines:
            if line.startswith("agent-sandbox: ") and not line.startswith("agent-sandbox: debug: "):
                said = line[len("agent-sandbox: "):]
                LOG.log("WARNING" if said.startswith(("warning:", "error:")) else "INFO", "said", said)
            elif USAGE_ERROR.match(line):  # argparse's refusal of an option (only "ended (exit 2)" was logged)
                LOG.warning("said", line)

    def flush(self):
        with contextlib.suppress(OSError):
            self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextlib.contextmanager
def steady_output():
    """sys.stdout and sys.stderr as SteadyStream while the launcher runs (restored afterwards)."""
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = (stream if stream is None or isinstance(stream, SteadyStream) else
                              SteadyStream(stream, tee=stream is saved[1]) for stream in saved)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved


def start_summary(a):
    """What was decided for the start A, on one line of the launcher's log: the agent, backend, image (its variant),
    mode, workspace, configuration, MCP servers, VPN, how it runs, its run ID."""
    parts = [f"agent {a.agent}", f"backend {a.backend}"]
    if a.backend == "podman":
        parts.append(f"image {a.image or 'none'}" + (f" (variant {a.image_variant})"
                                                    if getattr(a, "image_variant", None) else ""))
    parts += [f"mode {getattr(a, 'mode', None) or 'default'}" + (f" (from {a.mode_origin})"
                                                                  if getattr(a, "mode_origin", None) else ""),
              f"workspace {a.workspaces[0] if a.workspaces else None}",
              f"configuration {getattr(a, 'project_config', None) or 'none'}"]
    if a.mcp:
        parts.append("MCP servers " + ", ".join(dict.fromkeys(a.mcp)))
    if a.vpn:
        parts.append(f"VPN {a.vpn}")
    parts.append("dry run" if a.dry_run else "probe" if a.probe else "an MCP server's sandbox"
                 if getattr(a, "mcp_socket_dir", None) else "the supervisor of a session (its terminal: tmux)"
                 if getattr(a, "tmux_socket_dir", None) else "session" if detachable(a) else "in this terminal")
    # (a session's supervisor and an MCP server's sandbox have no start name of their own: the session's, the pod's)
    name = getattr(a, "start_name", None) or getattr(a, "start_id", None) or getattr(a, "pod_name", None)
    parts.append(f"name {name}, run {a.run_id}")
    return printable(", ".join(str(part) for part in parts))


def main(argv):
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    LOG.to_stderr()  # SANDBOX_AI_DEBUG=1: the debug lines on standard error too (a session's supervisor.log)
    started, status = time.monotonic(), None
    LOG.info("start", f"asked: {LOG.command_line(argv)}")
    if LOG.debugging():
        print(f"agent-sandbox: debug: SANDBOX_AI_DEBUG is on; the launcher's log: {LOG.path()}", file=sys.stderr)
    try:
        with steady_output():
            status = launch(argv)
            return status
    except SystemExit as exc:
        LOG.log("INFO" if exc.code in (0, None) else "WARNING", "start",
                f"ended (exit {exc.code}) after {time.monotonic() - started:.1f} s")
        raise
    except KeyboardInterrupt:
        LOG.info("start", f"interrupted after {time.monotonic() - started:.1f} s")
        raise
    except Exception as exc:
        # What refused the start (an option, a missing image, a Podman failure ...): its message, as the terminal
        # says it, and where it came from.
        LOG.error("start", f"refused or failed after {time.monotonic() - started:.1f} s", exc)
        LOG.debug("start", "where: " + " <- ".join(
            f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
            for frame in reversed(__import__("traceback").extract_tb(exc.__traceback__)[-6:])))
        raise
    finally:
        forget_planned_remote()  # mount points of remote folders this start planned and never mounted
        if status is not None:
            LOG.log("INFO" if not isinstance(status, int) or status <= 0 or sandbox_ended("", status, 0)[0] == "INFO"
                    else "WARNING", "start",
                    f"ended with exit status {status} after {time.monotonic() - started:.1f} s")
        LOG.flush()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


_hermes_module = None


def load_hermes_module():
    """hermes.py: Hermes' features, managed configuration and plan; loaded once (the parser's help reads it)."""
    global _hermes_module
    if _hermes_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_hermes", HERE / "hermes.py")
        with LOADING:
            if _hermes_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _hermes_module = loaded
    return _hermes_module


def hermes_managed(a):
    """This sandbox runs Hermes (its own start or a helper's server): it gets Hermes' managed configuration."""
    return a.agent == "hermes" and runs_agent(a) and not a.probe


def hermes_guarded(a):
    """This sandbox has Hermes, where `hermes` can be typed (its own start, a helper's server, a shell with --binary,
    a start with --with hermes or in a team image with its layer): Hermes' telemetry guard goes read-only into its
    site-packages (hermes.guard_files, guard_mounts)."""
    return not a.probe and (a.agent == "hermes" or "hermes" in image_agents(a))


def telemetry_guarded(a):
    """The agents whose own command this sandbox has (telemetry_guard.AGENTS) - in its own start, a helper's server or
    a shell with --binary, where it can be typed: the start's agent and its --with, and in the team image (Cairn's
    too) all three of Claude Code, Codex and OpenCode. Their command is guarded and their managed configuration pins
    telemetry off (telemetry_guard_files, telemetry_guard_mounts). None in a probe."""
    if getattr(a, "probe", False):
        return []
    return [agent for agent in image_agents(a) if agent in TELEMETRY_GUARD.AGENTS]


def telemetry_guard_files(a, files):
    """{path in the session directory: content} for the agents telemetry_guarded: the guard of their commands (its
    Python and its JavaScript, over the image's copies; not with bwrap, which runs the host's agents) and their
    managed configuration (TELEMETRY_MANAGED) - Claude Code's managed settings are those of FILES (its hooks) with the
    opt-out variables as "env", Codex's managed_config.toml its telemetry keys, OpenCode's its share, update and
    OpenTelemetry switches."""
    agents, found = telemetry_guarded(a), {}
    if agents and a.backend != "bwrap":
        found[f"{TELEMETRY_GUARD_DIR}/{PurePosixPath(TELEMETRY_GUARD.GUARD_PY).name}"] = \
            (HERE / "telemetry_guard.py").read_text()
        found[f"{TELEMETRY_GUARD_DIR}/{PurePosixPath(TELEMETRY_GUARD.GUARD_JS).name}"] = TELEMETRY_GUARD.javascript()
        for agent in agents:  # OpenHands and nanobot: the .pth of their venv's site-packages (an older image's too)
            if TELEMETRY_GUARD.KINDS[agent] == "python":
                found[f"{TELEMETRY_GUARD_DIR}/{agent}.pth"] = TELEMETRY_GUARD.pth_line(agent)
    level = telemetry_level(a)
    for agent in agents:
        if agent not in TELEMETRY_MANAGED:
            continue
        target, name = TELEMETRY_MANAGED[agent]
        path = f"{PurePosixPath(target).name}/{name}"
        if agent == "claude":
            settings = json.loads(files[path]) if path in files else {}
            settings["env"] = {**settings.get("env", {}), **TELEMETRY_ENV["claude"]["off"],
                               **(TELEMETRY_ENV["claude"]["strict"] if level == "strict" else {})}
            found[path] = json.dumps(settings, indent=2) + "\n"
        elif agent == "codex":
            keys = codex_telemetry_keys(level)
            lines = ["# sandbox-ai: Codex's telemetry is always off (its managed configuration, above -c and every "
                     "config.toml)", *(f"{key} = {literal}" for table, key, _, literal in keys if table is None)]
            tables = {}
            for table, key, _, literal in keys:
                if table is not None:
                    tables.setdefault(table, []).append((key, literal))
            for table, pairs in codex_plugin_config(a).items():  # the kits' plugins (codex_plugins)
                tables.setdefault(table, []).extend(pairs)
            for table, pairs in tables.items():
                lines += ["", f"[{table}]", *(f"{key} = {literal}" for key, literal in pairs)]
            found[path] = "\n".join(lines) + "\n"
        else:
            found[path] = json.dumps(OPENCODE_MANAGED_CONFIG, indent=2) + "\n"
    return found


def telemetry_guard_mounts(a, session):
    """[(file or directory in SESSION, target)] of telemetry_guard_files, each mounted read-only: the guard's files
    over the image's copies (not with bwrap), each managed configuration's directory where the agent's hooks do not
    mount it already (hook_mount, companion_hook_mounts)."""
    agents = telemetry_guarded(a)
    if not agents:
        return []
    mounts = [] if a.backend == "bwrap" else [
        (Path(session) / TELEMETRY_GUARD_DIR / PurePosixPath(target).name, target)
        for target in (TELEMETRY_GUARD.GUARD_PY, TELEMETRY_GUARD.GUARD_JS)] + [
        (Path(session) / TELEMETRY_GUARD_DIR / f"{agent}.pth",
         f"{TELEMETRY_GUARD.SITE_PACKAGES[agent]}/{TELEMETRY_GUARD.PTH}")
        for agent in agents if TELEMETRY_GUARD.KINDS[agent] == "python"]
    hooks = hook_mount(a, session)
    taken = {target for _, target in [*([hooks] if hooks else []), *companion_hook_mounts(a, session),
                                      *companion_managed_mounts(a, session)]}
    for agent in agents:
        if agent in TELEMETRY_MANAGED and TELEMETRY_MANAGED[agent][0] not in taken:
            target = TELEMETRY_MANAGED[agent][0]
            mounts.append((Path(session) / PurePosixPath(target).name, target))
    return mounts


def hermes_files(a):
    """{path in the session directory: content} of Hermes' managed configuration for this run (hermes.py): the
    sandbox's defaults and switches, the features, the --mcp servers through the launcher's bridge, --llm's model with
    the loop guard."""
    hermes = load_hermes_module()
    mcp = hermes.mcp_servers(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
    model, loop_text = None, None
    if a.llm:
        llm = load_llm_module()
        model, loop_text = llm.hermes_config(a), llm.loop_text(llm.loop_guard_of(a, llm.selection(a)[0]))
    note = start_note(a)
    config = hermes.managed_config(a, telemetry_level(a), mcp, model, note=bool(note), hooks=managed_hook_groups(a))
    return hermes.files(a, config, loop_text, note)


_dsh_module = None
_qwen_module = None
_goose_module = None
_openhands_module = None
_nanobot_module = None
_letta_module = None
_opencode2_module = None
_pi_module = None
# Agents the team image (--with) does not hold: none since 0.1.98 - the others are its layers (team_image.py).
NOT_IN_TEAM = ()


def load_dsh_module():
    """dsh.py: DeepSeek Harness' patch, telemetry and plan; loaded once."""
    global _dsh_module
    if _dsh_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_dsh", HERE / "dsh.py")
        with LOADING:
            if _dsh_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _dsh_module = loaded
    return _dsh_module


def load_qwen_module():
    """qwen.py: Qwen Code's features, system settings and plan; loaded once (the parser's help reads it)."""
    global _qwen_module
    if _qwen_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_qwen", HERE / "qwen.py")
        with LOADING:
            if _qwen_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _qwen_module = loaded
    return _qwen_module


def load_goose_module():
    """goose.py: Goose's features, system configuration, variables and plan; loaded once (the parser's help reads
    it)."""
    global _goose_module
    if _goose_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_goose", HERE / "goose.py")
        with LOADING:
            if _goose_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _goose_module = loaded
    return _goose_module


def load_openhands_module():
    """openhands.py: OpenHands' features, settings, variables and plan; loaded once (the parser's help reads it)."""
    global _openhands_module
    if _openhands_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_openhands", HERE / "openhands.py")
        with LOADING:
            if _openhands_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _openhands_module = loaded
    return _openhands_module


def load_nanobot_module():
    """nanobot.py: nanobot's features, sandbox-ai's settings for it, variables and plan; loaded once (the parser's
    help reads it)."""
    global _nanobot_module
    if _nanobot_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_nanobot", HERE / "nanobot.py")
        with LOADING:
            if _nanobot_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _nanobot_module = loaded
    return _nanobot_module


def load_letta_module():
    """letta.py: Letta Code's features, variables, start script and plan; loaded once (the parser's help reads it)."""
    global _letta_module
    if _letta_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_letta", HERE / "letta.py")
        with LOADING:
            if _letta_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _letta_module = loaded
    return _letta_module


def load_opencode2_module():
    """opencode2.py: OpenCode 2's features, variables, configuration layer and plan; loaded once (the parser's help
    reads it)."""
    global _opencode2_module
    if _opencode2_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_opencode2", HERE / "opencode2.py")
        with LOADING:
            if _opencode2_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _opencode2_module = loaded
    return _opencode2_module


def load_pi_module():
    """pi.py: Pi's features, variables, extension and plan; loaded once (the parser's help reads it)."""
    global _pi_module
    if _pi_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_pi", HERE / "pi.py")
        with LOADING:
            if _pi_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _pi_module = loaded
    return _pi_module


def agent_module(agent):
    """The module of a reviewed agent with read-only configuration of its own (dsh.py, qwen.py, goose.py,
    openhands.py, nanobot.py, letta.py, pi.py), else None."""
    return {"dsh": load_dsh_module, "qwen": load_qwen_module, "goose": load_goose_module,
            "openhands": load_openhands_module, "nanobot": load_nanobot_module,
            "letta": load_letta_module, "opencode2": load_opencode2_module, "pi": load_pi_module}.get(agent, lambda: None)()


def agent_managed(a):
    """This sandbox runs DSH, Qwen Code, Goose, OpenHands, nanobot, Letta Code, OpenCode 2 or Pi (its own start or a
    helper's server): it gets their read-only configuration (DSH's patch at /etc/dsh, Qwen Code's system settings at
    /etc/qwen-code, Goose's system configuration at /etc/goose, the settings OpenHands' start applies at /etc/openhands,
    sandbox-ai's settings for nanobot at /etc/nanobot, Letta Code's start script at /etc/letta, OpenCode 2's layer and
    --llm file at /etc/opencode2, sandbox-ai's extension for Pi at /etc/pi)."""
    return a.agent in ("dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi") and runs_agent(a) \
        and not a.probe


def agent_files(a):
    """{path in the session directory: content} of DSH's patch or Qwen Code's system settings for this run: the
    sandbox's defaults, the features, --model/--effort, the --mcp servers (DSH: patch rows; Qwen Code takes them as
    --mcp-config), --llm's model with the loop guard, the telemetry pins last."""
    llm = load_llm_module() if a.llm else None
    if a.agent == "dsh":
        dsh = load_dsh_module()
        mcp = dsh.mcp_rows(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
        return dsh.files(a, dsh.patch(a, mcp, llm.dsh_config(a) if llm else None, start_note(a),
                                      hooks=hooks_active(a)))
    if a.agent == "goose":
        goose = load_goose_module()
        mcp = goose.extensions(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
        # Its Top Of Mind file holds the loop guard's text (--llm) and the network note.
        return goose.files(a, goose.managed_config(a, mcp),
                           joined_text(llm.goose_loop_text(a) if llm else None, start_note(a)),
                           managed_hook_groups(a))
    if a.agent == "openhands":
        openhands = load_openhands_module()
        mcp = openhands.mcp_servers(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
        keys = api_key_environment(a)
        provider = openhands.api_provider(a)
        loop = llm.openhands_loop(a) if llm else None
        if start_note(a):  # the start's note after the loop guard's text, in its system message's suffix
            loop = {**(loop or {}), "instructions": joined_text((loop or {}).get("instructions"), start_note(a))}
        return openhands.files(a, openhands.managed_settings(
            a, mcp, llm.openhands_llm(a) if llm else None, keys.get(provider[1]) if provider else None, loop))
    if a.agent == "nanobot":
        nanobot = load_nanobot_module()
        mcp = nanobot.mcp_servers(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) if a.mcp else None
        return nanobot.files(a, nanobot.managed_config(a, mcp, llm.nanobot_config(a) if llm else None,
                                                       api_key_environment(a)))
    if a.agent == "letta":
        letta = load_letta_module()
        servers = letta.mcp_servers(a.mcp, mcp_bridge_args) if a.mcp and not getattr(a, "helper_of", None) else None
        return letta.files(a, letta.start_config(a, servers, llm.letta_model(a) if llm else None,
                                                 note_path(a), managed_hook_groups(a)))
    if a.agent == "opencode2":
        # Its configuration layer to read, and with --llm the provider file and the loop guard's text (read-only).
        return load_opencode2_module().files(a, opencode2_content(a), llm.opencode2_files(a) if llm else None,
                                             module_hooks(a))
    if a.agent == "pi":
        pi = load_pi_module()
        servers = pi.mcp_servers(a.mcp, mcp_bridge_args, mcp_tool_timeouts(a)) \
            if a.mcp and not getattr(a, "helper_of", None) else None
        config = pi.extension_config(a, servers, llm.pi_llm(a) if llm else None, module_hooks(a))
        if start_note(a):  # the start's note after the loop guard's text: a section of Pi's system prompt
            config["instructions"] = joined_text(config.get("instructions"), start_note(a))
        return pi.files(a, config)
    qwen = load_qwen_module()
    return qwen.files(a, qwen.managed_settings(a, telemetry_level(a), llm.qwen_settings(a) if llm else None,
                                               managed_hook_groups(a)))


def openhands_canvas_note(a):
    """OpenHands' UI is a web page on the sandbox's loopback: without the sandbox's own browser nothing shows it."""
    if a.agent == "openhands":
        note = load_openhands_module().canvas_note(a)
        if note:
            print(f"agent-sandbox: note: {note}", file=sys.stderr)


def preset_bridge_notes(a):
    """What the start says of the bridge its preset proposes (a.preset_bridge): an agent that would not see it; and,
    for a start in the foreground of this terminal (no session to ask about it once it is up), the command to make
    it once it runs. A session asks once it is up (propose_preset_bridge)."""
    proposal = getattr(a, "preset_bridge", None)
    if not proposal or a.dry_run or a.probe:
        return []
    presets = load_presets_module()
    found = [f"note: {why}" for why in [presets.bridge_agent_note(proposal, a.agent)] if why]
    if not detachable(a):
        found.append(f"note: the preset {proposal['preset']} proposes a bridge, made only once you confirm it; this "
                     "start runs in the foreground of this terminal, so it is not asked here - once it runs, in "
                     f"another terminal: {presets.bridge_command(proposal, a.start_name)}")
    return found


def propose_preset_bridge(a, name):
    """The bridge a.preset_bridge, proposed once the new session NAME is up (sessions.start): asked on a terminal and
    made by bridge add's own code on the human's yes, said as a command otherwise. Never fails the start."""
    try:
        load_presets_module().propose_bridge(a.preset_bridge, name)
    except Exception as exc:  # noqa: BLE001 - the session runs; the bridge can be made by hand
        print(f"agent-sandbox: the preset's bridge was not proposed: {printable(exc)}", file=sys.stderr)


_presets_module = None


def load_presets_module():
    global _presets_module
    if _presets_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_presets", HERE / "presets.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _presets_module is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded.EXPOSED_BEFORE = exposed_before  # where sandboxes wrote: no preset directory there
                _presets_module = loaded
    return _presets_module


FIX_ROUNDS = 4  # how often one start is tried again after its fixes (each round fixes what the last one found)


def launch(argv, floor_by=None, preset_bridge=None, preset_catalogs=None):
    """The start ARGV (launch_once), with what it lacks offered instead of only refused (fixes.py): a refusal that
    carries fixes - its image not built, an MCP server not added, a toolchain not in the store, an identity - is
    put together with everything else the start lacks (missing_ahead) into one plan; on a terminal the human is
    asked (your default fix_questions: all at once, or each), --fix and --fix-only do it without asking, and the
    start is tried again when everything it needs was done. Without a terminal and without --fix, declined, or
    with only what sandbox-ai cannot do: the refusal as before, ending with the commands that fix it. --fix-plan
    prints the plan as JSON and starts nothing. A fix is never tried twice. PRESET_CATALOGS: launch_once's."""
    attempted, state = [], {}
    for _round in range(FIX_ROUNDS + 1):
        state.clear()
        try:
            return launch_once(argv, floor_by, preset_bridge, state, preset_catalogs)
        except ValueError as exc:
            a = state.get("a")
            if not getattr(exc, "fixes", None) and a is not None:
                # A refusal of a module that names its own fix (a secret not kept: "keep one with: sandbox-ai
                # secrets set NAME") carries it here (fixes.recognised): offered like every other.
                with contextlib.suppress(AttributeError, TypeError):
                    exc.fixes = FIXES.recognised(str(exc))
            if not getattr(exc, "fixes", None) or a is None:
                raise
            if a.dry_run and not getattr(a, "fix_plan", False):
                # Nothing is made, asked or looked up in a dry run, and its refusal is as it was: the app's and the
                # web interface's checks read it, often (--fix-plan is the plan for them).
                raise
            message = str(exc)
            mode = getattr(a, "mode", None) or MODES.NAMES[2]
            if internal_start(a):
                # A session's supervisor, an MCP server's own sandbox: nobody to ask, nothing fixed by itself - the
                # refusal with its commands (the start that made it relays what it says).
                raise ValueError(FIXES.refusal(message, FIXES.judge(exc.fixes, mode), flag=None)) from None
            # A name no start takes is said first: nothing is fixed (a build of minutes) for a start refused then.
            for name in getattr(a, "mcp", None) or []:
                check_mcp_name(name)
            # (the start's options in their long forms: a fix that changes them - add --with claude - names them so)
            argv = canonical_options(list(argv))
            fixes = FIXES.with_starts(start_fixes(a, exc.fixes, getattr(exc, "checked", None)),
                                      FIXES.without_options(argv))
            questions = state.get("defaults", {}).get("fix_questions") or FIXES.QUESTIONS[0]
            if getattr(a, "fix_plan", False):
                print(json.dumps(FIXES.plan_json(fixes, mode, questions), indent=1))
                return 0
            LOG.info("fixes", "the start lacks: " + "; ".join(
                f"{item['id']} ({item['verdict']})" for item in FIXES.judge(fixes, mode)))
            done = FIXES.resolve(message, fixes, mode=mode, questions=questions, fix_all=bool(a.fix),
                                 only=list(a.fix_only or []), attempted=attempted, printable=printable)
            attempted += done
            LOG.info("fixes", f"done: {', '.join(done)}; the start is tried again")
            forget_planned_remote()
            argv = confirmed_argv(a, argv)  # (a wildcard confirmed on the way is not asked about again)
            changed = [item for item in fixes if item["id"] in done and item.get("options")]
            if changed:  # a way to go on that was chosen: the start's own options, as the session keeps them too
                argv = FIXES.start_options(argv, changed)
                print("agent-sandbox: going on as: " + printable(FIXES.command_text(["sandbox-ai", *argv])),
                      file=sys.stderr)
            else:
                print("agent-sandbox: what the start lacked is there now: it goes on", file=sys.stderr)
    raise ValueError("the start still lacks something after its fixes ran; see what it said above")


KIND_ORDER = ("option", "tool", "image", "variant", "service", "toolchain", "identity", "vpn", "llm", "mcp-image",
              "mcp", "mcp-update", "kit", "host", "secret", "publish", "plugin", "device", "login")


def check_mcp_name(name):
    """ValueError for an --mcp NAME no start takes; a catalog's PREFIX:NAME (what `mcp add` takes) is told the name
    a start takes and the add."""
    if re.fullmatch(MCP_NAME, str(name)):
        return
    prefix, _, bare = str(name).partition(":")
    hint = (f" - a start takes the server's name: --mcp {bare} (added with: sandbox-ai mcp add {name})"
            if prefix and re.fullmatch(MCP_NAME, prefix) and re.fullmatch(MCP_NAME, bare) else "")
    raise ValueError(f"--mcp NAME: 1-32 letters, digits, '_' or '-': {name!r}{hint}")


def start_fixes(a, known, checked=None):
    """Everything the start A lacks, as one plan: KNOWN (the fixes of the refusal that stopped it) and what the
    checks after it would have refused too (missing_ahead; CHECKED: the check that refused, not run again), each
    once, in the order they are done - what others need first (an image before a login in it)."""
    fixes = FIXES.unique([*known, *missing_ahead(a, {item["id"] for item in known}, checked)])
    return sorted(fixes, key=lambda item: KIND_ORDER.index(item["kind"]) if item["kind"] in KIND_ORDER
                  else len(KIND_ORDER))


def missing_ahead(a, known=(), checked=None):
    """The fixes of what the start A lacks beyond the refusal that stopped it: the launcher's own checks, each run
    on A now and asked only for the fixes its refusal carries (a start refuses the first thing it finds; the plan
    lists everything at once). A check that fails otherwise says so when the start is tried again; nothing is made
    here (a.fix_collect: no image is built on the way). The login of the start's agent joins it when the host has
    none (not needed: the start goes on without it)."""
    found = []
    a.fix_collect = True

    def mcp_servers():
        if checked == "mcp":  # (the refusal that stopped the start is this check's own: it said all it finds)
            return
        if not getattr(a, "helper_definitions", None):
            add_helpers(a)
        check_mcp_names(a)
    for check in (plan_toolchains, check_identity, check_image_variant, check_kits_there, check_enable_agents,
                  check_cairn, mcp_servers,
                  lambda _a: load_vpn_module().load_profile(a.vpn) if getattr(a, "vpn", None) else None,
                  lambda _a: load_llm_module().endpoint(a.llm) if getattr(a, "llm", None) else None,
                  check_services, check_publish):
        try:
            check(a) if check is not mcp_servers else check()
        except ValueError as exc:
            fixes = getattr(exc, "fixes", None) or FIXES.recognised(str(exc))
            found += [item for item in fixes if item["id"] not in known]
            if not fixes:
                LOG.debug("fixes", f"looking ahead: {str(exc).splitlines()[0][:300]}")
        except Exception as exc:  # noqa: BLE001 - only for the plan: the start itself says what is wrong
            LOG.debug("fixes", "looking ahead for what the start lacks failed", exc)
    return found + late_fixes(a, known) + optional_fixes(a)


def late_fixes(a, known=()):
    """The fixes of what a start finds only once it is checked and about to run - the services' images in Podman's
    store and their secrets (services.ready_fixes) - looked at now: listed with the rest, and by --fix-plan, whose
    dry run gets that far never."""
    found = []
    if getattr(a, "services", None) and not getattr(a, "probe", False):
        with contextlib.suppress(Exception):
            found += [item for item in load_services_module().ready_fixes(a, launcher_namespace())
                      if item["id"] not in known]
    for value in getattr(a, "secret_env", None) or []:
        # --secret-env NAME[=SECRET]: read when the sandbox is made (a session's supervisor: no terminal to ask on)
        name, _, secret = str(value).partition("=")
        secret = secret or name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", secret) or f"secret:{secret}" in known:
            continue
        try:
            kept = load_secret_store_module().get(secret, ask=False) is not None
        except Exception:  # noqa: BLE001 - locked, or not readable: the start says why when it reads it
            kept = True
        if not kept and all(item["id"] != f"secret:{secret}" for item in found):
            found.append(FIXES.secret_fix(secret, f"--secret-env {value}"))
    return found


def llm_fix(a):
    """The fix (fixes.py) for the --llm server of a start that did not answer (llm.unreachable_fix): not needed, said
    with its command; never asked for by a dry run (no request is made for one)."""
    if getattr(a, "dry_run", False) or not getattr(a, "llm", None):
        return None
    return load_llm_module().unreachable_fix(a)


UDEV_RULES = "/etc/udev/rules.d/70-sandbox-ai.rules"


def udev_fix(path):
    """The fix (fixes.py) for the device PATH your user cannot open directly (the sandbox keeps no supplementary
    groups): a udev rule that gives your user the device (TAG+="uaccess", by its USB IDs), udev told and the
    device triggered - root's commands, shown, never run. None where no rule can be named (no USB IDs)."""
    rule = udev_hint(path)
    if rule is None:
        return None
    return FIXES.fix("device", str(path), f"your user cannot open {path} directly, so the agent cannot either",
                     "give your user the device with a udev rule, then replug it",
                     [["sudo", "sh", "-c", f"echo {shlex.quote(rule)} >> {UDEV_RULES}"],
                      ["sudo", "udevadm", "control", "--reload"], ["sudo", "udevadm", "trigger"]],
                     needed=False, manual="it needs root: sandbox-ai never runs sudo for you")


def device_fixes(a):
    """The fixes (fixes.py) for the --device nodes of the start A your user cannot open (udev_fix): not needed - the
    start goes on, the agent cannot open them until the rule is there."""
    found = []
    for path, kind in devices(a):
        if kind in ("cdi", "dir"):
            continue
        with contextlib.suppress(OSError):
            if not device_access(path):
                item = udev_fix(path)
                if item is not None:
                    found.append(item)
    return found


def mcp_newer(a):
    """[(name, newer)] of the --mcp servers of the start A (of your mcp.json, from a catalog) whose catalog pins a
    newer image than they run, as that catalog was last read here (mcp.newer_pin: nothing downloaded)."""
    names = [name for name in dict.fromkeys(getattr(a, "mcp", None) or []) if re.fullmatch(MCP_NAME, str(name))]
    if not names:
        return []
    mcp = load_mcp_module()
    try:
        mine = mcp.read_config()[1]
        servers = mcp.validated({name: mine[name] for name in names if name in mine}, mcp.config_file())
    except (OSError, ValueError):
        return []
    project = getattr(a, "project_document", None)
    named = (project.get("mcp_servers") or {}) if isinstance(project, dict) else {}
    found = []
    for name, definition in servers.items():
        if name in named:  # (a project file's server of the name is the one the start runs)
            continue
        newer = mcp.newer_pin(definition)
        if newer:
            found.append((name, newer))
    return found


def mcp_update_notes(a):
    """What a start says of its MCP servers whose catalog pins a newer image (mcp_newer): the command that takes it."""
    return [load_mcp_module().newer_text(name, newer) for name, newer in mcp_newer(a)]


def mcp_update_fixes(a):
    """The fixes (fixes.py) of the start A's MCP servers whose catalog pins a newer image (mcp_newer): `mcp update
    NAME --yes` - the catalog read again and its signature checked, the image pulled by its digest, its folders,
    variables, limits and network kept. Not needed: the start goes on with the image it has. One of an unsigned
    catalog is unsigned (refused in the modes high and paranoid: fixes.judge)."""
    found = []
    for name, newer in mcp_newer(a):
        parts = [f"{key} {load_mcp_module().short_pin(newer[key][0])}" for key in ("image", "rootfs") if key in newer]
        found.append(FIXES.fix("mcp-update", name, f"the MCP server {name} runs an older " + " and ".join(parts)
                               + f" than its {'signed ' if newer['signed'] else 'unsigned '}catalog pins",
                               "take the newer image (its signature checked again; its folders, variables, limits "
                               "and network stay)",
                               [["sandbox-ai", "mcp", "update", name, "--yes",
                                 *([] if newer["signed"] else ["--unsigned"])]],
                               duration="a download", unsigned=not newer["signed"], needed=False))
    return found


def optional_fixes(a):
    """The fixes of what the start A goes on without, offered with what it needs (never a reason to stop): a Git
    login left out because its secret is no access token, the plugins its kits declare, the agent's login, a newer
    image of its MCP servers."""
    found = []
    for check in (git_login_fixes, plugin_fix, login_fix, device_fixes, llm_fix, mcp_update_fixes):
        with contextlib.suppress(Exception):
            items = check(a)
            found += items if isinstance(items, list) else [items] if items is not None else []
    return found


GIT_LOGIN_SECRET = re.compile(r"left out: (?:the secret (?P<bad>[A-Za-z0-9_.-]+) is no access token - (?P<why>.*) "
                              r"\(enter it again|no secret (?P<none>[A-Za-z0-9_.-]+) \()")


def git_login_fixes(a):
    """The fixes (fixes.py) for the Git logins this start leaves out because their secret is no access token, or is
    not there (registry_git_tokens' notes): the secret entered again, in this terminal. Not needed: the start goes
    on without that login (a push there is refused)."""
    found = []
    for note in registry_git_tokens(a)[1]:
        match = GIT_LOGIN_SECRET.search(note)
        if not match:
            continue
        name = match["bad"] or match["none"]
        what = (f"the secret {name} is no access token ({match['why']}): its Git login is left out" if match["bad"]
                else f"there is no secret {name}: its Git login is left out")
        found.append(FIXES.fix("secret", name, what, "enter it again, in this terminal (a line break or a blank "
                               "pasted with it is the usual cause)" if match["bad"] else
                               "keep it, in this terminal", [["sandbox-ai", "secrets", "set", name]],
                               terminal=True, needed=False))
    return found


def launch_once(argv, floor_by=None, preset_bridge=None, state=None, preset_catalogs=None):
    # STATE (launch's): gets "a", the start's options as far as they were read, and "defaults", your defaults.
    # FLOOR_BY: the floor that chose the mode of this start (the launch below), for MODES.check (--ask allow).
    # --preset NAME: its options right after the agent (the command line wins), its first task as the agent's
    # arguments; then everything is checked as if typed. PRESET_BRIDGE: the bridge the preset proposes (the launch
    # below: its options are the preset's already), made only once the start runs and the human confirms it.
    # PRESET_CATALOGS: {server: catalog prefix} the preset chose (the launch below: its options are the preset's).
    import uuid
    preset_notes, bridges, preset_warnings, catalogs = [], [], [], {}
    argv, preset_mode = load_presets_module().apply(canonical_options(list(argv)), canonical=canonical_options,
                                                    with_mode=True, notes=preset_notes,
                                                    credential=copies_credential, private=private_source,
                                                    code_env=lambda options, given: load_mcp_module().code_env_in(
                                                        options, given), bridges=bridges,
                                                    warnings=preset_warnings, catalogs=catalogs)
    preset_bridge = bridges[0] if bridges else preset_bridge
    preset_catalogs = catalogs or preset_catalogs or {}
    for note in preset_notes:
        print(f"agent-sandbox: note: {printable(note)}", file=sys.stderr)
    # A preset of an extra directory: in a built-in one's place, the secrets of yours it hands on and where to.
    for warning in preset_warnings:
        print(f"agent-sandbox: warning: {printable(warning)}", file=sys.stderr)
    argv = canonical_options(argv)
    if preset_mode:
        MODES.check_name(preset_mode, "the preset's --mode")
    a = parse_args(argv)
    config_path, document = load_project_config(a)
    merged = merge_config(argv, a, document) if document else argv
    if document:
        a = parse_args(merged)
    if a.fix_plan:
        a.dry_run = True  # (--fix-plan: the checks of a dry run, then what they lack as JSON; nothing is made)
    user_defaults, policy = load_user_defaults(a), load_policy()
    if state is not None:
        state.update(a=a, defaults=user_defaults)
    # The security mode's defaults join the options (modes.py); medium, the default, adds none.
    a = MODES.apply(launcher_namespace(), merged, a, document, config_path, user_defaults, policy)
    if a.fix_plan:
        a.dry_run = True
    # The catalogs the preset chose for its servers (mcp:binwalk): what a missing one is added from (mcp_fixes).
    a.preset_catalogs = dict(preset_catalogs)
    if state is not None:
        state["a"] = a
    floors = [m for m in (preset_mode, getattr(a, "mode_at_least", None)) if m]
    floor = max(floors, key=MODES.rank) if floors else None
    if floor and a.mode_origin != "--mode" and MODES.rank(floor) > MODES.rank(a.mode):
        # A floor (the preset's mode, --mode-at-least): stricter than the project's or your defaults', so it is
        # taken (as if typed). Never lower: a stricter mode they chose stays.
        rest, skip = [], False
        for token in argv[1:]:  # the floor is applied: --mode-at-least goes (it would only say the same again)
            if skip:
                skip = False
            elif token == "--mode-at-least":
                skip = True
            elif not token.startswith("--mode-at-least="):
                rest.append(token)
        return launch([argv[0], "--mode", floor, *rest],
                      floor_by=f"the preset's mode {floor}" if preset_mode == floor else f"--mode-at-least {floor}",
                      preset_bridge=preset_bridge, preset_catalogs=preset_catalogs)
    a.mode_floor = floor_by  # a mode a floor chose is not the human's own choice (MODES.check: --ask allow)
    a.preset_bridge = preset_bridge  # (never from a project file, a kept start or a session's saved options)
    # The checked document is passed on; nothing re-reads the file later.
    a.project_config, a.project_document = (str(config_path) if config_path else None), document
    # What the start's own options name (a preset's and the app's included): reach() tells them from the file's.
    a.start_tokens = list(argv)
    if not a.dry_run and not a.probe and not a.mcp_socket_dir and a.session is None:
        # Which folder the agent works in, and whose configuration applies: the mix-up this prevents is giving
        # --workspace another checkout and getting that one as /work. (A session's supervisor says it, relayed to
        # the terminal that starts it: said once.)
        where = f"agent-sandbox: /work is {printable(a.workspaces[0])}"
        if getattr(a, "cwd_project", None):
            where += " (the project of this directory; what --workspace named follows it)"
        where += f"; configuration: {printable(a.project_config) if a.project_config else 'none'}"
        print(where, file=sys.stderr)
    apply_subnet_defaults(a, user_defaults, str(defaults_file()))
    a.canary_default = user_defaults.get("canaries")  # canary.categories(): after the options and the project file
    a.telemetry_default = user_defaults.get("telemetry")  # telemetry_level(): after the option
    # llm_traffic_mode(), llm_traffic_limit(): after the options
    a.llm_traffic_default, a.llm_traffic_limit_default = (user_defaults.get("llm_traffic"),
                                                          user_defaults.get("llm_traffic_limit"))
    a.name_style_default = user_defaults.get("name_style")  # name_start(): after --name-style
    a.intro_default = user_defaults.get("agent_intro")  # intro_on(): after --intro and --no-intro
    a.budget_default = user_defaults.get("budget")  # run_budget(): after --budget (and the project file's)
    a.file_reads = user_defaults.get("file_reads") or "folders"  # the file log's reads (runtime.FileReads)
    a.connection_bodies = user_defaults.get("connection_bodies") or "text"  # runtime.binary_answer
    # the workspace checkpoints (checkpoint_plan): after the options
    a.checkpoint_defaults = {key: user_defaults[key] for key in CHECKPOINT_KEYS if key in user_defaults}
    if getattr(a, "ask_timeout", None) is None:  # the option (also the project file's) first, then your default
        a.ask_timeout = user_defaults.get("ask_timeout") or ASK_TIMEOUT_DEFAULT
    a.package_min_age_default = user_defaults.get("package_min_age_days")  # package_guard_config(): your own floor
    # The administrator's policy binds every way of choosing an option, the project file's and the mode's included.
    check_policy(a, policy)
    # Options may narrow the mode, never widen it; then what it enforces beyond options (approval scope ...).
    MODES.check(launcher_namespace(), a, policy)
    # --toolchain: what it mounts, and its registries (after the mode's check: in high and paranoid they stay closed
    # instead of being refused as --allow-host would be; before validate(), which checks the hosts as any others).
    toolchain_notes = plan_toolchains(a)
    # A wildcard below a shared service given with --allow-host: confirmed by --yes or on a terminal (else validate()
    # refuses it, naming --allow-shared-wildcard); after the policy and the mode, which refuse it without a question.
    confirm_shared_wildcards(a)
    # What you approved "always" for this project joins the options before they are checked.
    remembered = apply_remembered_approvals(a) if not a.offline and a.max_approval_scope == "always" else []
    for name in expire_kept(a, user_defaults):
        print(f"agent-sandbox: removed the kept start {printable(name)}: unused for more than "
              f"{user_defaults['keep_days']} days (keep_days in {defaults_file()})", file=sys.stderr)
    expiry_notice(a, user_defaults)
    log_days_notice(a, user_defaults)
    resolve_tmux(a)
    a.start_name = name_start(a)
    notes = [note for note in [resolve_keep(a)] if note]
    plan_kept_home(a)
    check_image_variant(a)  # before validate: its image checks see the variant's image (or none)
    # Remote folders (sshfs://): a mount point of sandbox-ai's own for each, in their place - checked as any folder.
    plan_remote_mounts(a)
    validate(a)
    # The notes about the run itself: a session's (or a tmux start's) supervisor says them, and its terminal shows
    # what it says (sessions.wait_ready) - said here too, they came twice.
    relayed = detachable(a) and not a.dry_run
    if not relayed:
        openhands_canvas_note(a)
    if not relayed and a.agent == "dsh" and a.binary is None and not a.probe and not getattr(a, "helper_of", None) \
            and load_dsh_module().note(a, dsh_session_browser(a)):
        # The chat in the terminal (sandbox-ai's) or the web UI: what it is and how to reach the other (its window
        # says it again).
        print(f"agent-sandbox: note: {load_dsh_module().note(a, dsh_session_browser(a))}", file=sys.stderr)
    if not a.dry_run:
        remember_exposed(a)
    for note in remembered + notes + ask_allow_notes(a) + preset_bridge_notes(a) + \
            ([] if detachable(a) and not a.dry_run else toolchain_notes):
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    a.run_id = getattr(a, "run_id", None) or (a.pod_name[len("agent-"):] if a.pod_name
                                              else f"{a.agent}-{uuid.uuid4().hex[:8]}")
    LOG.info("start", f"checked: {start_summary(a)}")
    if a.fix_plan:
        # --fix-plan of a start whose checks found nothing it needs: what it finds only when it runs (a service's
        # image gone, its secret), and what it could still use (a login), as JSON; nothing made.
        print(json.dumps(FIXES.plan_json(late_fixes(a) + optional_fixes(a), a.mode,
                                         user_defaults.get("fix_questions") or FIXES.QUESTIONS[0]), indent=1))
        return 0
    late = late_fixes(a) if not a.dry_run and not a.probe else []
    if late:
        # (what prepare() below would refuse one at a time - a service's image gone, its secret - offered at once)
        raise FIXES.Missing("Not started: " + "; ".join(item["missing"] for item in late), late)
    check_login(a)
    if getattr(a, "llm", None) and not relayed and not a.probe:
        # Its LLM server asked now (as the start asks it anyway): one that does not answer is said with what makes it
        # answer - the start goes on (fixes.py: not needed; the agent's requests fail until it answers).
        with contextlib.suppress(Exception):
            item = llm_fix(a)
            if item is not None:
                print(f"agent-sandbox: note: {printable(item['missing'])}: {printable(item['why'])}; then: "
                      + FIXES.command_text(item["commands"][0]), file=sys.stderr)
    for note in git_credentials_file(a)[1] + ([a.llm_vpn_note] if getattr(a, "llm_vpn_note", None) else []) + \
            registry_git_tokens(a)[1]:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in [] if relayed else device_notes(a) + nested_notes(a) + isolation_notes(a):
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    # Your own images (services, a variant's base): unchecked, said at every start.
    for note in [] if relayed else ([a.image_base["warning"]] if getattr(a, "image_base", None) else []) + \
            (load_services_module().notes(a) if getattr(a, "services", None) else []):
        print(f"agent-sandbox: warning: {printable(note)}", file=sys.stderr)
    for note in load_browser_module().notes(a) if getattr(a, "browser_container", None) and not relayed else []:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in load_cairn_module().notes(a) if getattr(a, "cairn_container", None) and not relayed else []:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in load_artex_module().notes(a) if getattr(a, "artex_container", None) and not relayed else []:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in [] if relayed else enforced_limits(a)[1]:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    if a.backend == "bwrap" and resource_limits(a) and not limits_prefix(a) and not relayed:
        print("agent-sandbox: the resource limits are not enforced with bwrap here (no systemd user manager)",
              file=sys.stderr)
    if (a.tmux and a.session is None and not (a.probe or a.tmux_socket_dir or a.mcp_socket_dir or a.dry_run)
            and not sys.stdin.isatty()):
        raise ValueError("--tmux needs an interactive terminal")  # a session may start detached, without one
    if getattr(a, "services", None) and not a.probe and not a.dry_run:
        # Each service's image there with its recorded ID (never pulled now), its secrets and variables at hand:
        # before a session detaches too, so that what is missing is said here.
        try:
            load_services_module().prepare(a, launcher_namespace())
        except ValueError as exc:
            if FIXES.hostinit().MISSING not in str(exc):
                raise
            raise FIXES.Missing(str(exc), [FIXES.init_fix("a service of this start")]) from None
    if getattr(a, "browser_container", None) and not a.probe and not a.dry_run:
        load_browser_module().prepare(a, launcher_namespace())  # its image there (sandbox-ai build browser)
    if getattr(a, "cairn_container", None) and not a.probe and not a.dry_run:
        load_cairn_module().prepare(a, launcher_namespace())  # its image there (sandbox-ai build cairn)
    if getattr(a, "artex_container", None) and not a.probe and not a.dry_run:
        load_artex_module().prepare(a, launcher_namespace())  # its image there (sandbox-ai build artex)
    if detachable(a) and not a.dry_run:
        if a.backend == "bwrap" and not shutil.which("tmux"):
            raise FIXES.Missing("Required tool missing: tmux", [FIXES.tool_fix(["tmux"])])
        LOG.info("start", f"{printable(a.start_name)}: started as a session (a background supervisor runs it; its "
                          "output: its supervisor.log, named below)")
        # A background supervisor runs the sandbox; this terminal attaches (and may detach). It mounts the remote
        # folders itself: here their connections are opened (a password is asked in this terminal, never there).
        if restore_remote_options(a):
            open_remote_connections(a)
        if a.llm and getattr(a, "use_case", None):
            # the model --use-case picked is named in the session's options: its later runs keep that model
            argv = load_llm_module().pinned_argv(a, argv)
        # A wildcard below a shared service confirmed here (its question, --yes): kept as that confirmation.
        # (--fix and --fix-only are this start's: a session's later runs fix nothing unasked)
        return load_sessions_module().start(a, FIXES.without_options(confirmed_argv(a, argv)))
    if a.dry_run:
        print(json.dumps({"agent": a.agent, "backend": a.backend, "mode": MODES.plan(a),
                          "workspace": str(Path(a.workspace).resolve()),
                          "workspaces": [{"host": src, "sandbox": dest} for src, dest in workspace_targets(a)],
                          "workspace_mode": "ro" if a.workspace_ro else "rw", "mounts": a.mount,
                          # --hide: what an empty read-only file or folder would be mounted over now
                          "hidden": plan_hidden(a, say=lambda line: None)["hidden"] if a.hide else [],
                          # sshfs://: mounted on this host at these points when it starts (none now)
                          "remote": [{key: entry.get(key) for key in ("source", "point", "mode", "sandbox")}
                                     for entry in getattr(a, "remote_mounts", None) or []],
                          # the workspace checkpoints, kept on the host in sandbox-ai's state (never mounted)
                          "checkpoint": checkpoint_plan(a),
                          "devices": [{"path": path, "kind": kind} for path, kind in devices(a)], "gpu": a.gpu,
                          # --toolchain: mounted read-only at /opt/toolchains/NAME; the registries opened with them
                          "toolchains": load_toolchains_module().plan_shown(a.toolchain_plan)
                          if a.toolchain_plan else [],
                          "toolchain_hosts": {"opened": [*a.toolchain_plan["hosts"],
                                                         *(f"--allow-{p}" for p in a.toolchain_plan["presets"])],
                                              "closed": a.toolchain_plan["closed"]} if a.toolchain_plan else None,
                          "device_grants": [{**{key: grant[key] for key in ("value", "by", "state", "decision",
                                                                            "rule", "ruleset", "track")},
                                             "nodes": list(grant["nodes"] or grant.get("pending") or [])}
                                            for grant in device_grants(a)],
                          "limits": enforced_limits(a)[0],
                          "tmp_size": int(tmp_mount_size(a).partition("=")[2] or 0) or None
                          if a.backend == "podman" else None,
                          "nested_containers": nested_plan(a),
                          # sandbox-ai's own system call filters, the host folders' flags, the disk watch, the LSM
                          "isolation": isolation_plan(a),
                          # --service: containers of your images in the pod; a variant on a base image of yours
                          "services": load_services_module().plan(a, launcher_namespace())
                          if getattr(a, "services", None) else [],
                          # --browser: its container, network, mounts, CA, display socket, DevTools (agent, both)
                          "browser": load_browser_module().plan(a, launcher_namespace())
                          if getattr(a, "browser_container", None) else None,
                          # --cairn: its container, its workers through the relay, the dispatcher's configuration
                          "cairn": load_cairn_module().plan(a, launcher_namespace())
                          if getattr(a, "cairn_container", None) else None,
                          # --artex: its container, its listeners, the console's forward, its LLM, its data
                          "artex": load_artex_module().plan(a, launcher_namespace())
                          if getattr(a, "artex_container", None) else None,
                          "image_base": getattr(a, "image_base", None),
                          "allow": a.allow, "https_hosts": allowed_hosts(a), "host_wildcards": host_wildcards(a),
                          "shared_wildcards": shared_wildcards(a),
                          "https_networks": host_networks(allowed_hosts(a)), "offline": a.offline,
                          "dangerously_skip_permissions": a.dangerously_skip_permissions,
                          "egress_all_allow": a.egress_all_allow, "nameservers": nameservers(a),
                          "log_dir": str(a.log_dir.expanduser().resolve()) if a.log_dir else None,
                          "gui": gui_config(a), "audio": getattr(a, "audio", None), "gui_wayland": a.gui_wayland,
                          "tmux": a.tmux, "session": a.session,
                          # the bridge its preset proposes (made only once it runs, after you confirm it)
                          "preset_bridge": getattr(a, "preset_bridge", None),
                          "name": a.start_name, "project_name": project_label(a), "detachable": detachable(a),
                          "keep": a.keep_home,
                          "allow_tcp": [f"{addr}:{port}" for addr, port in tcp_targets(a)],
                          "host_ports": host_ports(a),
                          "network_profiles": a.profile,
                          # what it may reach, each with its source (sandbox-ai net show lists it while it runs)
                          "reach": recorded_reach(a),
                          "git_identity": a.git_identity,
                          "privacy_check": a.privacy_check,
                          "ask": (a.ask if asking(a) else "off") if proxy_enabled(a) else None,
                          "proxy": {"enabled": proxy_enabled(a), "no_inspect": a.no_inspect,
                                    "log_dir": str(Path(a.log_dir).expanduser()) if proxy_enabled(a) else None,
                                    # binary answers by digest ("text") or whole ("all"): defaults connection_bodies
                                    "connection_bodies": getattr(a, "connection_bodies", None) or "text"},
                          # files.jsonl (an explicit --log-dir): reads summed per folder, each file, none (file_reads)
                          "file_log": {"watched": bool(a.log_explicit),
                                       "reads": getattr(a, "file_reads", None) or "folders"},
                          "privacy_rules": str(privacy_rules_dir(a)) if privacy_rules_dir(a) else None,
                          "log_debug": a.log_debug,
                          "canaries": load_canary_module().Plan(a, "/root", argparse.Namespace(**globals())).plan(),
                          "telemetry": telemetry_plan(a),
                          "package_guard": package_guard_config(a) if package_guard(a) else None,
                          "secret_guard": secret_guard_plan(a),
                          "llm_traffic": llm_traffic_plan(a),
                          "spend": spend_plan(a),
                          "rulesets": [{"name": source["name"], "defaults": source["defaults"],
                                        "http": [rule["id"] for rule in source["http"]],
                                        "mcp": [rule["id"] for rule in source["mcp"]],
                                        "devices": [rule["id"] for rule in source["devices"]],
                                        "tool": [rule["id"] for rule in source["tool"]]}
                                       for source in load_rulesets(a)],
                          "git_credentials": [{key: c[key] for key in ("url", "user", "env", "file", "secret",
                                                                       "credentials", "line") if key in c}
                                              for c, _ in git_tokens(a)],
                          # sandbox-ai git-logins the proxy adds (servers and users; the tokens never)
                          "git_logins": [{"url": c["url"], "user": c["user"]} for c, _ in registry_git_tokens(a)[0]],
                          "mcp": mcp_plan(a),
                          # --llm: the MCP tools its model would see against its class's budget (mcp_budget.py)
                          "mcp_tools": getattr(a, "mcp_tools_report", None),
                          "project_config": a.project_config,
                          "kits": kit_plan(a),
                          "network": a.network, "ip": a.ip, "ip6": a.ip6, "image": a.image,
                          "sandbox_network": sandbox_network(a),
                          "vpn": load_vpn_module().plan(a) if a.vpn else None,
                          "network_note": {"text": network_note(a), "file": load_vpn_module().NOTE_PATH,
                                           "agent_reads_it": note_place(a)} if network_note(a) else None,
                          # the agent's intro (intro.py): what its sandbox has, and where it reads the line naming it
                          "intro": intro_plan(a),
                          "podman_url": a.podman_url,
                          "local_config": str(a.local_config) if a.local_config else None,
                          "llm": load_llm_module().plan(a) if a.llm else None,
                          "copy_auth": a.copy_auth, "auth_file": str(a.auth_file) if a.auth_file else None,
                          "auth_mode": auth_mode(a), "auth_copies": auth_plan(a),
                          "companions": companions(a),
                          "import_codex_auth": str(a.import_codex_auth) if a.import_codex_auth else None,
                          "copies": a.copy, "env_names": a.env, "secret_env": a.secret_env,
                          "api_keys": [{"host": host, "variable": name, "secret": secret}
                                       for host, name, secret in api_key_values(a)],
                          "publish": load_publish_logins_module().plan(publish_kinds(a), publish_logins_of(a)),
                          "identity": identity_plan(a),
                          "model": getattr(a, "model", None), "effort": getattr(a, "effort", None),
                          "show_thinking": thinking_plan(a),
                          # Each of the run's agents with a module of its own, the lead's or one --with.
                          "hermes": load_hermes_module().plan(agent_view(a, "hermes"))
                          if "hermes" in team(a) else None,
                          "dsh": load_dsh_module().plan(agent_view(a, "dsh"), dsh_session_browser(a), dsh_web_port(a))
                          if "dsh" in team(a) else None,
                          **{agent: agent_module(agent).plan(agent_view(a, agent)) if agent in team(a) else None
                             for agent in ("qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi")},
                          "agent_args": a.agent_args,
                          "firejail_required": True}, indent=2))
        return 0
    if os.geteuid() == 0:
        raise ValueError("Run as an ordinary host user; both backends use a rootless user namespace")
    required = ["podman"] if a.backend == "podman" else ["bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap"] + ([] if a.offline else ["slirp4netns"])
    if (a.tmux or a.tmux_socket_dir) and not a.probe and a.backend == "bwrap":
        required.append("tmux")
    lacking = [tool for tool in required if not shutil.which(tool)]
    if lacking:
        # A tool of the host: its distribution's package command is said with the refusal (never run: it needs root).
        raise FIXES.Missing(f"Required tool missing: {lacking[0]}", [FIXES.tool_fix(lacking)])
    if a.backend == "bwrap" and not a.dry_run and (any(os.isatty(fd) for fd in (0, 1, 2))
                                                   or has_controlling_terminal()):
        check_tiocsti()
    binary = a.binary or a.agent
    if a.backend == "bwrap" and not a.probe:
        found = shutil.which(binary)
        if not found:
            raise FIXES.Missing(f"Agent executable missing: {binary}", bwrap_agent_fixes(a, binary))
        binary = str(Path(found).resolve(strict=True))
        if not binary.startswith("/usr/"):
            a.runtime.append(str(Path(binary).parent))
        # The agents --with, and the agent a helper's server runs for each task: the host's, like the lead's.
        for agent in [*companions(a), *([a.agent] if getattr(a, "helper_of", None) else [])]:
            found = shutil.which(agent)
            if not found:
                raise FIXES.Missing(f"Agent executable missing: {agent} "
                                    f"({'the helper' if agent == a.agent else '--with ' + agent})",
                                    bwrap_agent_fixes(a, agent))
            where = str(Path(found).resolve(strict=True).parent)
            if not where.startswith("/usr/") and where not in a.runtime:
                a.runtime.append(where)
    own = own_folders(a)
    for workspace in a.workspaces:
        prepare_workspace(workspace, warn=Path(os.path.normpath(Path(workspace).expanduser().absolute())).resolve()
                          not in own)
    # The workspaces' checkpoint, taken on the host while the start prepares the rest (finished before the agent
    # runs: checkpoint_ready). Remote folders are mounted later: they are not held.
    start_checkpoint(a)
    for private in (a.log_dir, a.gui_vnc, a.tmux_socket_dir, a.mcp_socket_dir, a.mcp_approvals_dir):
        if private:
            private.expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)
            private.expanduser().chmod(0o700)
    if a.gui_wayland:
        print("agent-sandbox: warning: --gui-wayland gives the agent access to your Wayland compositor "
              "(its windows appear on your desktop).", file=sys.stderr)
    # What the start does before its agent appears, said as it happens (progress.py; in a session the supervisor's
    # lines are said in its terminal by sessions.wait_ready): each step, how long a slow one took, "ready in N s".
    a.progress = progress = load_progress_module().Progress(enabled=not (a.probe or getattr(a, "mcp_socket_dir", None)))
    hosts = None
    remembered = [name for name in getattr(a, "remembered_hosts", []) if name in allowed_hosts(a)]
    wanted = [name for name in allowed_hosts(a) if name not in remembered]
    resolve = (lambda names: resolve_pod_hosts(a, names)) if a.backend == "podman" else resolve_hosts
    # Podman: each lookup is a container of its own - one for all the names (looked_up_together), and while the MCP
    # servers' sandboxes come up, begun before them. (With --vpn the names are resolved through the tunnel, once it
    # is up, below.)
    pod_lookup = not a.vpn and a.backend == "podman"
    groups = (wanted, remembered, load_llm_module().pinned_names(a) if a.llm else []) if pod_lookup else ()
    ahead = in_background(lambda: looked_up_together(resolve, *groups, ahead=True),
                          what="looking up the allowed host names while the MCP servers start") \
        if pod_lookup and a.mcp and not a.probe else None
    lent_servers = []
    if a.mcp and not a.probe:
        # Each MCP server runs in its own sandbox and outlives this one. With --vpn, those that reach the network
        # run behind this start's VPN too (their own VPN where they have one; with Tor, Tor only): a server outside
        # it reached the internet from your own address.
        try:
            mcp = load_mcp_module()
            names = list(dict.fromkeys(a.mcp))
            behind = mcp.vpn_for_servers(a, names)
            # Those behind this start's tunnel of a profile that runs once at a time (WireGuard, Mullvad, a persistent
            # Tailscale node) borrow it - one key, one tunnel - and come up once it is up (below).
            lent_servers = [name for name in names if mcp.lent_of(behind.get(name))[0]]
            start_mcp_servers(mcp, a, [name for name in names if name not in lent_servers], behind, progress)
        finally:
            looked_up = looked_up_ahead(ahead, resolve)  # waited for also when a server failed: no lookup outlives it
    if not a.vpn:
        if ahead is not None:
            resolve = looked_up
        elif pod_lookup:
            resolve = looked_up_together(resolve, *groups)
        hosts = resolve(wanted)
        hosts.update(apply_remembered_pins(a, remembered, resolve))
        settle_network(a, hosts, resolve)
    with contextlib.ExitStack() as cleanup:
        # The run's folder (config, kits, and the HOME of a run without one of its own): removed at the end - unless
        # the sandbox outlives this launcher (sandbox_outlived), whose agent still works in it; liveness.discard
        # removes it once that run has ended for certain (run.json "session_dir").
        session = Path(tempfile.mkdtemp(prefix="agent-sandbox-"))
        cleanup.callback(unless_outlived, a, remove_tree, session)
        # The remote folders (sshfs://) on this host now: the start's, and those `workspace add` mounts during the
        # run, unmounted last - after the pod (this callback is the first).
        cleanup.callback(unless_outlived, a, release_remote, a)
        if getattr(a, "remote_mounts", None):
            progress.step("mounting the remote folders (sshfs, NFS, SMB) on this host")
            mount_remote(a)
        if getattr(a, "foreground_start", False):
            # The kept start's directory now: everything before could still refuse the start.
            load_sessions_module().begin(a, argv)
            cleanup.callback(load_sessions_module().discard_unstarted, a)
        home = a.state_dir.expanduser().absolute() if a.state_dir else session / "home"
        if home.is_symlink():
            raise ValueError("--state-dir must not be a symlink")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if home.stat().st_uid != os.getuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
            raise ValueError("--state-dir must be owned by you and have mode 0700")
        if a.state_dir:
            cleanup.enter_context(state_dir_lock(home))  # closed (O_CLOEXEC) in every child
        if a.vpn:  # the pod with the tunnel first; the agent's names are resolved through it
            progress.step(f"bringing up the VPN {printable(a.vpn)} (its container, its tunnel and kill switch)")
            names = allowed_hosts(a)
            extra = [name for name in (load_llm_module().pinned_names(a) if a.llm else []) if name not in names]
            # Servers that borrow this start's tunnel now: without its lender they would reach nothing, so the start
            # is refused then; a start without such servers goes on without one (vpn.not_lent).
            a.vpn_lend_required = bool(lent_servers)
            found = load_vpn_module().agent_up(a, session, home, names + extra, cleanup)
            hosts = {name: found[name] for name in names if name not in remembered}
            hosts.update(apply_remembered_pins(a, remembered, lambda wanted: {n: found[n] for n in wanted if n in found}))
            settle_network(a, hosts, lambda wanted: {name: found[name] for name in wanted})
            if getattr(a, "vpn_lends", False):
                # The servers that borrow this run's tunnel (these, and any attached during the run) serve it alone:
                # stopped when it ends, before its tunnel goes (callbacks run last first) - without it they reach
                # nothing anyway (their kill switch).
                cleanup.callback(load_mcp_module().stop_lent, a.run_id)
            if lent_servers:
                start_mcp_servers(load_mcp_module(), a, lent_servers, behind, progress)
        for source, dest in prepare_home(a, home):
            print(f"agent-sandbox: auth copied: {Path(source).expanduser()} -> /root/{dest}", file=sys.stderr)
        if a.kit or not getattr(a, "no_default_kit", False):
            progress.step("preparing the kits and plugins")
        a.kit_mounts, notes, memory = prepare_kits(a, home, session / "kits")
        for note in notes:
            print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
        checkpoint_ready(a, cleanup)  # the workspaces' checkpoint complete before anything writes into them
        # The files of the workspaces the host runs or trusts, as they are before the agent: compared after the run.
        workspace_manifest = record_workspace_review(a)
        plan_hidden(a, session)  # --hide: before the canaries, so that a canary's .env is never one of them
        # /opt/toolchains of this run: the wrappers and variables of its --toolchain, the mount points (and the place
        # `sandbox-ai toolchain attach` puts one while it runs).
        for note in prepare_toolchains(a, session):
            print(f"agent-sandbox: note: toolchains: {printable(note)}", file=sys.stderr)
        a.canary_plan = plant_canaries(a, home, cleanup)  # after the auth copies and kits: never over them
        pair = claude_credential_pair(a, home)
        writeback = claude_writeback_state(pair) if pair and not a.probe else None
        stored = [state for state in (store_writeback_state(a, home, agent) for agent in team(a)) if state]
        if a.agent == "claude" and auth_mode(a) != "none" and (home / AUTH_TARGETS["claude"]).exists():
            print(f"agent-sandbox: {claude_token_status(home / AUTH_TARGETS['claude'])}", file=sys.stderr)
        for port in host_ports(a):
            host = "the Podman machine at --podman-url" if a.podman_url else \
                "this Mac (relayed through podman machine)" if on_machine(a) else "the host"
            print(f"agent-sandbox: 127.0.0.1:{port} in the sandbox reaches port {port} on the loopback of {host}; "
                  "the sandbox cannot listen on that port itself", file=sys.stderr)
        log_name, traffic_name = None, None
        if proxy_enabled(a):
            running = live_runs()  # (their logs are bounded by their own in-run limits: never deleted here)
            for name in rotate_connection_logs(a.log_dir.expanduser(), live=running):
                print(f"agent-sandbox: proxy: deleted the old connection log {name} (logs over "
                      f"{CONNECTION_LOGS_LIMIT >> 30} GiB)", file=sys.stderr)
            log_name = f"connections-{time.strftime('%Y%m%d-%H%M%S')}-{a.run_id}.jsonl"
            a.spend_log_name = "spend-" + log_name[len("connections-"):]
            if llm_traffic_config(a):
                traffic_name = "llm-traffic-" + log_name[len("connections-"):]
                for name in rotate_traffic_logs(a.log_dir.expanduser(), live=running):
                    print(f"agent-sandbox: proxy: deleted the old LLM traffic log {name} (logs over "
                          f"{LLM_TRAFFIC_LOGS_LIMIT >> 30} GiB)", file=sys.stderr)
                for line in llm_traffic_notes(a):
                    print(f"agent-sandbox: {line}", file=sys.stderr)
            for line in package_guard_notes(a):
                print(f"agent-sandbox: {line}", file=sys.stderr)
            for line in secret_guard_notes(a):
                print(f"agent-sandbox: {line}", file=sys.stderr)
            for line in no_inspect_notes(a):
                print(f"agent-sandbox: {line}", file=sys.stderr)
            daily_compaction(a)
            a.proxy_dir = Path(tempfile.mkdtemp(prefix="agent-sandbox-proxy-"))
            cleanup.callback(unless_outlived, a, shutil.rmtree, a.proxy_dir, True)
            write_proxy_files(a, a.proxy_dir, hosts, log_name)
        a.sandbox_home = home  # run.json: a workspace added later must not overlap it
        # The kill switch (sandbox-ai net cut): what it needs to know of this run; None: it has none (said why).
        a.killswitch = killswitch_config(a, hosts)
        a.control_dir = register_run(a, a.run_id, session)
        if toolchain_entries(a):  # the run's registry (toolchain list --run, detach) and its toolchains.jsonl
            load_toolchains_module().record_start(a.control_dir, a.toolchain_plan, a.log_dir, a.run_id)
        if a.killswitch is not None:
            # The cuts of the runs this one acts for, when they are cut now: they hold before its agent runs.
            a.killswitch["cuts"] = load_starts_module().module("net").joined_cuts(a.run_id, a.control_dir)
        # The run's directory goes at the end - unless the sandbox outlives this launcher (sandbox_outlived): its
        # control socket is in it (approvals, net allow, the kill switch) and `sandbox-ai stop` finds the run by it.
        # Last of all (callbacks run last first), once the run's directory is gone: its lock is let go of. A launcher
        # that goes on (a team's, the tests' in one process) holds no descriptor per run it ended.
        cleanup.callback(unless_outlived, a, release_supervisor_lock, a)
        cleanup.callback(unless_outlived, a, shutil.rmtree, a.control_dir, True)
        # Before the run's directory goes (callbacks run last first): what `mcp attach --live` wrote into the agent's
        # configuration is taken out again (a later start names the servers it keeps on its command line).
        cleanup.callback(unless_outlived, a, lambda directory=a.control_dir: load_mcp_module().forget_live_config(
            directory))
        if a.canary_plan:
            a.canary_plan.arm(a.control_dir, a.log_dir)
            cleanup.callback(a.canary_plan.drain)  # before the control directory goes: what is left is read
        device_watch = start_devices(a)
        history_event(a, "start")
        # The sandbox's browser socket: URLs to your browser (never behind Tor: it would reach them from your own
        # address), DSH's web UI to the session browser (dsh_session_browser, behind Tor too).
        if browser_requests(a):
            a.browser_dir = a.control_dir.with_name(a.control_dir.name + ".browser")
            a.browser_dir.mkdir(mode=0o700, exist_ok=True)
            cleanup.callback(unless_outlived, a, shutil.rmtree, a.browser_dir, True)
        if host_browser(a):
            write_browser_shims(session / "bin")
        mode = a.runtime_mode
        cfg = {"command": with_start_prelude(a, agent_command(a, binary)), "env": agent_environment(a),
               "probe": a.probe,
               # The run's isolation: sandbox-ai's own filters, the host folders' flags, the disk watch.
               "isolation": isolation_config(a),
               # Who this sandbox is, for the process attribution in its logs and questions.
               "sandbox": sandbox_identity(a), "log_debug": a.log_debug,
               # The supervisor relays each MCP server for this run: the rules for tainted runs, untrusted tools.
               "mcp_relay": {name: mcp_relay_config(a, name) for name in dict.fromkeys(a.mcp)},
               # The rules for the agent's own tools, answered to its hooks (None: no hooks this run).
               "hooks": hooks_config(a),
               # The agent whose managed hooks (rule hooks, approved kit hooks) must hold: its version is checked.
               "hook_agent": a.agent if managed_hooks(a) else None,
               # The kits give Codex plugins: its version must be one whose plugin system was reviewed.
               "codex_plugins": bool(getattr(a, "codex_plugins", None)),
               # A helper's command is its server (python3): the agent it runs is checked by its name.
               "hook_binary": a.agent if getattr(a, "helper_of", None) else None,
               "companion_hook_agents": list(companions(a)) if hooks_active(a) else [],
               # Values the supervisor's process log redacts by value (--env and --secret-env).
               # Not the telemetry opt-outs set over them (their values are sandbox-ai's: "1", "none").
               "secret_names": sorted((set(a.env) | {v.partition("=")[0] for v in a.secret_env})
                                      - set(telemetry_environment(a))),
               "tmux": "detached" if a.tmux_socket_dir else a.tmux,
               "proxy": {"log": log_name, **({"traffic": traffic_name} if traffic_name else {}),
                         **({"spend": a.spend_log_name} if getattr(a, "spend_log_name", None) else {})}
               if log_name else None,
               "ask": asking(a), "ask_quiet": list(ASK_QUIET), "ask_timeout": ask_timeout(a),
               "ask_max_scope": getattr(a, "max_approval_scope", "always"),
               # A strict security mode, which the supervisor holds run-time changes to (modes.py).
               **({"mode": mode} if mode["refuses"] or mode["approval_scope"] != "always" else {}),
               "ask_blocked": getattr(a, "blocked_approvals", {"hosts": [], "tcp": []}),
               "policy": getattr(a, "policy", {}),  # the supervisor holds run-time changes to it too
               # The kill switch (sandbox-ai net cut): the model API, the host's ports, Tor; the firewall's cut chain.
               **({"killswitch": a.killswitch} if a.killswitch is not None else {}),
               # This launcher's lock: once it is free (the launcher gone), the network is cut (watch_launcher).
               **({"launcher_lock": True} if launcher_watched(a) else {}),
               "pinned": [*hosts, *a.llm_pins], "wildcards": host_wildcards(a),
               "shared_wildcards": sorted(shared_wildcards(a)), "watch": [*(dest for _, dest in workspace_targets(a)), "/root", "/tmp",
                         *(parse_mount(m)[1] for m in a.mount)] if a.log_explicit else [],
               "file_reads": getattr(a, "file_reads", None) or "folders",
               # macOS: the sandbox runs in podman machine's VM (runtime.VM_CONTROL_DIR, PortRelay's upstream).
               **({"machine": {"host": machine_host(), "browser": browser_requests(a)}} if on_machine(a) else {}),
               # --nested-containers: the agent's own user namespace, without these IDs (runtime.nested_setup).
               **({"nested": {"reserved": nested_reserved_ids(), "writable": nested_writable(a)}}
                  if nested(a) else {}),
               # --cairn: the relay to the session's agents for Cairn's workers (cairn_relay.serve, inner_guard).
               **({"cairn": load_cairn_module().relay_config(a, launcher_namespace())}
                  if getattr(a, "cairn_container", None) and not a.probe else {}),
               # --service: what the supervisor waits for before the agent runs (runtime.await_services).
               **({"services": load_services_module().supervisor_config(a)}
                  if load_services_module().members(a) and not a.probe else {})}
        resolv, nsswitch = resolver_files(a)
        files = {"config.json": json.dumps(cfg), "hosts": hosts_file({**hosts, **a.llm_pins}, own_host_names(a)),
                 "resolv.conf": resolv, "nsswitch.conf": nsswitch,
                 "passwd": "root:x:0:0:Sandbox:/root:/bin/bash\n", "group": "root:x:0:\n",
                 "firewall.nft": firewall_rules(a.allow, [addr for values in hosts.values() for addr in values]
                                                + host_networks(hosts), log=bool(a.log_dir),
                                                dns=nameservers(a), allow_all=a.egress_all_allow,
                                                tcp=tcp_targets(a),
                                                proxy={"port": PROXY_PORT, "loopback": host_ports(a)}
                                                if log_name else None, vpn_mark=getattr(a, "vpn_mark", None),
                                                vpn_rules=getattr(a, "vpn_firewall", ()),
                                                check_mark=getattr(a, "vpn_check_mark", None),
                                                lend_mark=getattr(a, "vpn_lend_mark", None),
                                                machine_host=machine_host() if on_machine(a) else None)}
        if gui_config(a):
            files["gui.json"] = json.dumps(gui_config(a))
        if getattr(a, "browser_container", None) and not a.probe:
            # --browser: its runtime's configuration, its VNC socket's folder (beside the run's directory, only
            # yours) and its profile's folder; the folder of the socket goes with the run.
            browser = load_browser_module()
            files[browser.CONFIG_FILE] = json.dumps(browser.config(a))
            cleanup.callback(unless_outlived, a, shutil.rmtree, browser.prepare_folders(a, launcher_namespace()), True)
            profile = browser.profile_dir(a, launcher_namespace())
            if profile is not None:  # a kept profile: one browser at a time (each clears the other's lock)
                cleanup.enter_context(state_dir_lock(profile, "--browser-profile"))
        if getattr(a, "cairn_container", None) and not a.probe:
            # --cairn: its runtime's configuration, the relay's folder (beside the run's directory, only yours; on
            # macOS a Podman volume of the run) and a kept board's folder; the relay's goes with the run.
            cairn = load_cairn_module()
            files[cairn.CONFIG_FILE] = json.dumps(cairn.config(a))
            relay = cairn.prepare_folders(a, launcher_namespace())
            if relay is not None:
                cleanup.callback(unless_outlived, a, shutil.rmtree, relay, True)
            else:
                cleanup.callback(remove_volume, a, cairn.relay_volume(a))
        if getattr(a, "artex_container", None) and not a.probe:
            # --artex: its runtime's configuration (no secret: its LLM's placeholders), the run's ARTEX folder
            # (beside the run's directory, only yours: the console's address and this start's admin password) and
            # the password itself, which reaches its container only through podman's environment.
            artex = load_artex_module()
            files[artex.CONFIG_FILE] = json.dumps(artex.config(a, launcher_namespace()))
            cleanup.callback(unless_outlived, a, shutil.rmtree, artex.prepare_folders(a, launcher_namespace()), True)
        if getattr(a, "audio", None) == "virtual":
            files["audio.json"] = json.dumps({"kind": "virtual"})
        if nested(a):
            files.update(nested_files(a))
        npmrc = load_publish_logins_module().npmrc(publish_kinds(a), publish_logins_of(a)) \
            if getattr(a, "publish", None) else None
        if npmrc:
            files["npmrc"] = npmrc
        if managed_hooks(a) or getattr(a, "codex_plugins", None):
            files.update(hook_files(a))  # Codex's requirements.toml: its hooks, the kits' plugins' requirements
        # the network note (network.md) and the intro (INTRO.md, agent-note.md), Claude Code's managed CLAUDE.md
        files.update(note_files(a))
        if intro_on(a):  # what INTRO.md was made from, on the host: a live attach writes it again (intro.refresh)
            load_intro_module().write_state(a.control_dir, load_intro_module().resources(a, launcher_namespace()))
        if hermes_managed(a):
            files.update(hermes_files(a))
        if hermes_guarded(a):  # Hermes' telemetry guard, in every process of its Python (hermes_guard.py)
            files.update(load_hermes_module().guard_files())
        if agent_managed(a):
            files.update(agent_files(a))
        files.update(companion_files(a))  # the agents --with: their managed configuration, as in their own images
        # The agents' commands guarded, their managed configuration pinned (telemetry_guard.py): telemetry is always
        # off, also for an agent started from the sandbox's shell.
        files.update(telemetry_guard_files(a, files))
        restart_script = agent_restart_script(a, binary)
        if restart_script:  # `sandbox-ai kits restart-agent`: the agent again in its tmux window, its conversation on
            files[AGENT_RESTART_FILE] = restart_script
        # What an earlier run attached live (a launcher killed on the way): this start names its servers itself.
        with contextlib.suppress(FileNotFoundError):
            (session / AGENT_LIVE_MCP_FILE).unlink()
        for name, content in files.items():
            path = session / name
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)  # hermes/skills/NAME/SKILL.md
            path.write_text(content)
            path.chmod(0o600 if name == "config.json" else 0o644)
        for item in getattr(a, "codex_plugins", None) or []:  # the empty marketplace folders Codex is told of
            (session / CODEX_PLUGIN_MARKETPLACES / item["marketplace"]).mkdir(mode=0o755, parents=True, exist_ok=True)
        privacy_after_run = prepare_privacy(a, session)
        watching = threading.Event()
        if device_watch:
            threading.Thread(target=load_devices_module().watch_devices, args=(device_watch, watching),
                             daemon=True).start()
        if asking(a) or (device_watch and devices(a, held=True)):
            # The agent's terminal is busy with the agent: questions are announced on the desktop.
            approvals = load_approvals_module()
            (a.control_dir / "approvals").mkdir(mode=0o700, exist_ok=True)
            # --ask allow: answered here at once, nobody is notified (a canary's alarm still is).
            threading.Thread(target=approvals.watch, args=(a.control_dir / "approvals", watching),
                             kwargs={"auto": True, "interval": 0.2} if asks_nobody(a) else {}, daemon=True).start()
        # DSH's web UI goes to the session browser (session_browser_opener), the rest to you (host_browser).
        sending = {"auto": asks_nobody(a), **({"session": session_browser_opener(a), "host": host_browser(a)}
                                               if dsh_session_browser(a) else {})}
        if browser_requests(a):
            approvals = load_approvals_module()
        if browser_requests(a) and on_machine(a):
            threading.Thread(target=approvals.serve_browser_files,
                             args=(a.control_dir / RUNTIME.BROWSER_REQUESTS, a.control_dir / "approvals", watching),
                             kwargs=sending, daemon=True).start()
        elif browser_requests(a):
            threading.Thread(target=approvals.serve_browser, args=(a.browser_dir / "open.sock",
                                                                   a.control_dir / "approvals", watching),
                             kwargs={**sending, "listen": RUNTIME.listen_unix},  # (not a runtime of its own)
                             daemon=True).start()
        if writeback and writeback["host_login"]:
            # Refreshes reach the source (and other sandboxes' refreshes this one) during the run, not at its end.
            login_watch = threading.Thread(target=watch_claude_login, args=(home, pair, writeback, watching),
                                           daemon=True)
            login_watch.start()
        else:
            login_watch = None
        record_home_manifest(a, home)
        tools = tool_dirs_state(a)
        activity, status = start_activity(a, home, log_name, cfg), None
        # Every safeguard that refuses or ends something: a record and a desktop warning (safeguards.py).
        safeguard_watch = start_safeguards(a, log_name, cfg)
        # What the agent reads first on its own: hidden text in the workspaces' instruction files, said before it does.
        check_instruction_files(a, safeguard_watch.report if safeguard_watch else None)
        # Not "memory": that name holds the project memory (save_memory below).
        memory_watch = start_memory_watch(a, session, safeguard_watch.report if safeguard_watch else None)
        # The system monitor: what the run uses over time, its warnings (sandbox-ai top, stats usage; the app).
        monitor = start_monitor(a, safeguard_watch.report if safeguard_watch else None, memory_watch)
        # The budgets (sandbox-ai spend): the proxy's warnings to you, the other runs' spending to the proxy.
        try:
            spend_watch = start_spend_watch(a)
        except Exception as exc:  # noqa: BLE001 - the proxy holds the budgets anyway; only the warnings are missing
            print(f"agent-sandbox: spend: no budget warnings this run: {printable(str(exc))[:300]}", file=sys.stderr)
            spend_watch = None
        begun = None
        try:
            a.progress.step("starting the sandbox: its firewall, connection proxy and the agent")
            if not a.tmux_socket_dir:  # (a session's terminal says it once its sandbox is up: sessions.wait_ready)
                a.progress.ready("prepared")
            if not (a.tmux_socket_dir or a.mcp_socket_dir) and sys.stderr.isatty():
                # The agent has this terminal now: SANDBOX_AI_DEBUG's lines go to launcher.log only (a session's and
                # an MCP server's standard error is their supervisor.log: they stay there too).
                LOG.to_stderr(False)
            begun = time.monotonic()
            status = run_pod(a, session, home) if a.backend == "podman" else run_bwrap(a, session, home)
            return status
        finally:
            watching.set()
            phone_run_end(a, status, time.monotonic() - begun if begun is not None else 0, sys.exc_info()[1])
            # Each step on its own: whatever the agent left behind must never skip the login write-back, the
            # memory record or the privacy check after them.
            try:
                for line in memory_watch.stop() if memory_watch is not None else []:
                    print(f"agent-sandbox: {printable(line)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"agent-sandbox: memory watch: {printable(str(exc))}", file=sys.stderr)
            try:
                if spend_watch is not None:
                    spend_watch.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"agent-sandbox: spend: {printable(str(exc))[:300]}", file=sys.stderr)
            try:
                if monitor is not None:
                    monitor.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"agent-sandbox: monitor: {printable(str(exc))[:300]}", file=sys.stderr)
            try:  # (before the safeguard watch stops: its record is one of the run's)
                review_workspaces_after_run(a, workspace_manifest,
                                            safeguard_watch.report if safeguard_watch is not None else None)
            except Exception as exc:  # noqa: BLE001 - the agent's exit status matters more
                print(f"agent-sandbox: workspace review: {printable(str(exc))[:300]}", file=sys.stderr)
            try:
                for line in safeguard_watch.stop() if safeguard_watch is not None else []:
                    print(f"agent-sandbox: {printable(line)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - the agent's exit status matters more
                print(f"agent-sandbox: safeguards: the last read failed: {printable(str(exc))[:300]}", file=sys.stderr)
            try:
                for note in tool_dirs_changed(tools):
                    print(f"agent-sandbox: warning: {printable(note)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - the check could not finish: say so, as a change would be
                print(f"agent-sandbox: warning: the PATH directories in the workspaces could not be checked "
                      f"({printable(str(exc))}); check them before running anything from this shell", file=sys.stderr)
            # Each login on its own: one that fails never keeps another's refresh from the store, and the
            # agent's own exit status survives any of them.
            try:
                if login_watch is not None:
                    login_watch.join(CLAUDE_LOCK_TIMEOUT + 5)
                for note in claude_sync_notes(writeback):
                    print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
                message = claude_writeback(home, pair, writeback) if writeback else None
                if message:
                    print(f"agent-sandbox: {message}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - whatever the agent left, keep its exit status
                print(f"agent-sandbox: could not write back the Claude login: {exc}", file=sys.stderr)
            try:  # --auth-from-session: a refresh back into the source session's kept HOME, never a running one's
                message = load_session_login_module().write_back(a, home) if getattr(a, "session_login", None) \
                    else None
                if message:
                    print(f"agent-sandbox: {printable(message)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - as below: the agent's exit status survives it
                print(f"agent-sandbox: could not write back the login of the session it came from: "
                      f"{printable(str(exc))}", file=sys.stderr)
            for state in stored:  # the lead's and the --with agents' logins from the store
                try:
                    message = store_writeback(home, a, state)
                    if message:
                        print(f"agent-sandbox: {message}", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001 - as above
                    print(f"agent-sandbox: could not write back the {state.get('agent') or a.agent} login to the "
                          f"login store: {exc}", file=sys.stderr)
            if memory is not None:
                try:
                    save_memory(a, home, memory)
                except Exception as exc:  # noqa: BLE001 - as above
                    print(f"agent-sandbox: memory: could not keep the changes of this run: {printable(exc)}",
                          file=sys.stderr)
            if privacy_after_run is not None:
                try:
                    privacy_after_run()
                except Exception as exc:  # noqa: BLE001 - as above
                    print(f"agent-sandbox: privacy: the check after the run failed: {printable(exc)}",
                          file=sys.stderr)
            pin_mcp_tools_after_run(a)
            if log_name:
                for line in connection_summary(a.log_dir.expanduser() / log_name):
                    print(f"agent-sandbox: {printable(line)}", file=sys.stderr)
            if activity is not None:  # last (a signal here skips no other cleanup); before the HOME goes
                activity.finish(status)
            ended = sys.exc_info()[1]
            if not getattr(a, "sandbox_outlived", None):  # (one that runs on has not ended: list shows it orphaned)
                history_event(a, "exit", status if status is not None else
                              ended.code if isinstance(ended, SystemExit) and isinstance(ended.code, int) else None)
            if getattr(a, "foreground_start", False):
                name = printable(current_run_name(a))
                print(f"agent-sandbox: {name} keeps its HOME; continue it with: sandbox-ai resume {name} (or remove "
                      f"it: sandbox-ai delete {name} --yes)", file=sys.stderr)


def full_text(exc):
    """What a start (or a command on starts) that ended on a full file system (EXC: an OSError ENOSPC or EDQUOT naming
    its file) says: what
    could not be written, which file system that is (mostly XDG_RUNTIME_DIR, a small tmpfs, or the state folder) with
    its free space and inodes, and the fix - not "[Errno 28] No space left on device: PATH". "" for anything else."""
    if not isinstance(exc, OSError) or exc.errno not in (errno.ENOSPC, errno.EDQUOT) or not exc.filename:
        return ""
    path = printable(os.fsdecode(exc.filename))
    reason = exc.strerror or "No space left on device"
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_statedirs_for_full", HERE / "statedirs.py")
        statedirs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(statedirs)
        found = statedirs.space(os.fsdecode(exc.filename))
        where = printable(statedirs.space_text(found)) if found else ""
    except Exception:  # noqa: BLE001 - the words only: the start has failed anyway
        where = ""
    return (f"{path} could not be written ({reason}): a file system sandbox-ai writes is full"
            + (f" - {where}" if where else "") + "; free space there (sandbox-ai gc --check shows what sandbox-ai "
            "can free), then try again")


def current_run_name(a):
    """The name of this run's start as it is now: `sandbox-ai rename` while it ran changed it in run.json (the name
    it started with no longer finds it)."""
    try:
        name = json.loads((Path(a.control_dir) / "run.json").read_text()).get("name")
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        name = None
    return name if isinstance(name, str) and name else a.start_name


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
        # A child killed by signal N reports -N; exit like a shell would (128 + N).
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(marked(f"agent-sandbox: {full_text(exc) or exc}", sys.stderr, error=True), file=sys.stderr)
        sys.exit(1)
