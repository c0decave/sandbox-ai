#!/usr/bin/env python3
"""Rootless agent launchers; standard library only. See SANDBOX.md."""
import argparse
import base64
import contextlib
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
import termios
import threading
import time
import urllib.parse
import uuid

HERE = Path(__file__).resolve().parent


def load_runtime():
    """The in-sandbox runtime (runtime.py): loaded by path, because launchers run with python -I."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_runtime", HERE / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = load_runtime()


def load_modes():
    """The security modes (modes.py): a table and small checks, loaded by path like the runtime."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_modes", HERE / "modes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODES = load_modes()
# Shared constants and helpers, and the runtime functions under their former names.
LOG_DIR = RUNTIME.LOG_DIR
VNC_DIR = RUNTIME.VNC_DIR
TMUX_DIR = RUNTIME.TMUX_DIR
ATTACH_SOCKET = RUNTIME.ATTACH_SOCKET
GUI_CONFIG = RUNTIME.GUI_CONFIG
DROP_ALL = RUNTIME.DROP_ALL
INOTIFY_EVENTS = RUNTIME.INOTIFY_EVENTS
x_socket = RUNTIME.x_socket
listen_unix = RUNTIME.listen_unix
read_frames = RUNTIME.read_frames
attach_relay = RUNTIME.attach_relay
relay_terminal = RUNTIME.relay_terminal
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
PROVIDERS = {
    "codex": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    # console.anthropic.com serves OAuth token refresh for many Claude Code versions.
    "claude": ["api.anthropic.com", "console.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"],
    "opencode": ["opencode.ai"],
}
# --frontier: OpenAI and Anthropic endpoints for any agent (e.g. OpenCode).
FRONTIER = PROVIDERS["codex"] + PROVIDERS["claude"]
# --allow-<preset>: common destinations, TCP/443 via pinned DNS like --allow-host.
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
               "claude": ["--dangerously-skip-permissions"], "opencode": []}
# --model and --effort: the names each agent knows for sure (any other model name is passed on as it is) and the
# effort levels it takes; OpenCode sets the effort per model in its own configuration.
AGENT_MODELS = {"claude": ("fable", "opus", "sonnet", "haiku"), "codex": (), "opencode": ()}
AGENT_EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
                 "codex": ("minimal", "low", "medium", "high", "xhigh"), "opencode": ()}
MODEL_NAME = r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}"
DANGER_ENV = {"codex": {}, "claude": {"IS_SANDBOX": "1"},
              "opencode": {"OPENCODE_PERMISSION": '{"*":"allow"}'}}
# --telemetry: what the agents report to their makers. off (the default): no usage statistics, error reports,
# feedback uploads, session sharing or update checks; strict: also every other call that is not the model's;
# on: the agents' own settings. Names checked in the sources: Claude Code 2.1.317 (the binary), Codex rust-v0.166.0
# (codex-rs/config: analytics, feedback, otel, check_for_update_on_startup, features), OpenCode v1.19.5
# (packages/core/src/flag/flag.ts, effect/runtime-flags.ts; "share" and "autoupdate" in its config).
TELEMETRY_LEVELS = ("off", "strict", "on")
# Tools an agent runs that report usage unless told not to: the common DO_NOT_TRACK and the tools' own switches.
TELEMETRY_TOOLS = {"DO_NOT_TRACK": "1", "OPENSPEC_TELEMETRY": "0", "HF_HUB_DISABLE_TELEMETRY": "1",
                   "NEXT_TELEMETRY_DISABLED": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "GATSBY_TELEMETRY_DISABLED": "1",
                   "ASTRO_TELEMETRY_DISABLED": "1"}
TELEMETRY_ENV = {
    "claude": {"off": {**TELEMETRY_TOOLS, "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_BUG_COMMAND": "1",
                       "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1", "DISABLE_AUTOUPDATER": "1"},
               # Also plugin and marketplace downloads, release notes and the like.
               "strict": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}},
    # OpenCode sends no usage statistics (OTLP only with an endpoint of yours); it checks for updates.
    "opencode": {"off": {**TELEMETRY_TOOLS, "OPENCODE_DISABLE_AUTOUPDATE": "1"},
                 "strict": {"OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}},
    "codex": {"off": dict(TELEMETRY_TOOLS), "strict": {}},  # its own switches: codex_telemetry_defaults()
}
OPENCODE_TELEMETRY_CONFIG = {"share": "disabled", "autoupdate": False}  # in OPENCODE_CONFIG_CONTENT
# Telemetry sent to hosts the agent needs anyway, refused by the proxy as well (the ruleset "telemetry"): Claude
# Code's event log on its API host, Codex's analytics on ChatGPT's (codex-rs/analytics/src/client.rs).
TELEMETRY_PATHS = {"claude": [("api.anthropic.com", "/api/event_logging/**")],
                   "codex": [("chatgpt.com", "/backend-api/codex/analytics-events/**")]}
# (table, key, value, TOML) set in the sandbox's ~/.codex/config.toml where the file does not set them itself.
CODEX_TELEMETRY = {
    "off": [(None, "check_for_update_on_startup", False, "false"), ("analytics", "enabled", False, "false"),
            ("feedback", "enabled", False, "false"), ("otel", "metrics_exporter", "none", '"none"')],
    "strict": [("features", "plugins", False, "false")],  # the plugin sync with chatgpt.com
}
# Written into a sandbox HOME's ~/.claude/settings.json where not already set.
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_DEFAULT_EFFORT = "high"
AUTH_TARGETS = {"codex": ".codex/auth.json", "claude": ".claude/.credentials.json",
                "opencode": ".local/share/opencode/auth.json"}
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
DEFAULTS_KEYS = {"version", "subnet", "subnet6", "mode", "canaries", "keep_days", "telemetry", "preset_dirs"}
SESSION_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"
# The directory of a start: its first name, with a suffix when that directory is taken (sessions.new_key).
START_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}"
__version__ = "0.1.78"
CONFIG_NAME = ".sandbox-ai.json"
RULES_NAME = "rules.json"  # ~/.config/sandbox-ai/rules.json: your own rules, for every start
# Label on session pods: the session's tmux directory, so a stale pod can be told apart from someone else's.
SESSION_LABEL = "sandbox-ai.session"
CONFIG_KEYS = {"version", "agent", "options", "agent_args", "workspaces", "description", "mcp_servers", "rules",
               "log_debug", "mode", "canaries", "name", "keep"}
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
                    "--workspace",
                    "--session", "--dry-run", "--mcp-approvals-dir", "--rules", "--mode", "--name", "--project-name",
                    "--start-id", "--")
# Mutually exclusive auth choices: one given on the command line replaces the file's.
AUTH_OPTIONS = {"--copy-auth": False, "--no-auth": False, "--auth-file": True, "--import-codex-auth": True}
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
    "--version": "-V", "--backend": "-b", "--workspace": "-w", "--workspace-ro": "-W", "--mount": "-m",
    "--device": "-d", "--gpu": "-G", "--profile": "-p", "--mode-at-least": "-ml", "--preset": "-ps", "--preset-value": "-pv", "--allow": "-a", "--allow-host": "-A",
    "--allow-pypi": "-y", "--allow-github": "-g", "--allow-gitlab": "-gl", "--allow-kernel-org": "-ak",
    "--allow-duckduckgo": "-dd", "--allow-npm": "-an", "--allow-tcp": "-t", "--allow-host-port": "-P", "--git-token-env": "-ge",
    "--git-token-file": "-gf", "--git-credentials": "-gc", "--git-identity": "-I", "--identity": "-id", "--privacy-check": "-pc",
    "--privacy-rules": "-pr", "--no-proxy": "-X", "--no-inspect": "-x", "--allow-http": "-ah", "--no-provider-defaults": "-nd",
    "--frontier": "-f", "--dangerously-skip-permissions": "-D", "--local-config": "-L", "--offline": "-o",
    "--egress-all-allow": "-E", "--dns": "-R", "--nameserver": "-ns", "--subnet": "-sn", "--subnet6": "-s6",
    "--network": "-nw", "--ip": "-ip",
    "--ip6": "-i6", "--image": "-i", "--podman-url": "-pu", "--binary": "-B", "--runtime": "-rt",
    "--copy-auth": "-ca", "--no-auth": "-na", "--auth-file": "-af", "--import-codex-auth": "-ic",
    "--image-variant": "-iv",
    "--copy": "-cp", "--state-dir": "-S", "--kit": "-k", "--kit-dir": "-K", "--no-default-kit": "-nk",
    "--no-memory": "-nm", "--no-plugins": "-np", "--env": "-e", "--log-dir": "-l", "--gui": "-gu",
    "--gui-screen": "-gs", "--audio": "-au", "--gui-vnc": "-gv", "--gui-wayland": "-gw", "--tmux": "-T", "--no-tmux": "-nt",
    "--mcp": "-M", "--helper": "-hl", "--with": "-wa", "--session": "-s", "--name": "-N", "--project-name": "-pn", "--keep": "-kp",
    "--no-keep": "-nkp", "--config": "-c", "--no-config": "-nc", "--dry-run": "-n",
    "--probe": "-pb", "--memory": "-me", "--cpus": "-cu", "--pids": "-pi", "--ask": "-Q",
    "--secret-env": "-se", "--api-key": "-ky", "--model": "-mm", "--effort": "-ef", "--publish": "-pl", "--git-token-secret": "-gk", "--token-proxy": "-tp", "--git-logins": "-gn", "--rules": "-ru",
    "--log-debug": "-ld", "--llm": "-lm", "--vpn": "-vp", "--vpn-check": "-vc", "--mode": "-mo",
    "--canaries": "-cn", "--no-canaries": "-nn", "--telemetry": "-tm",
}
HELP_EPILOG = """\
Agent arguments follow --. Examples (more in QUICKSTART.md):
  sandbox-ai claude                                the current directory as /work; detach: Ctrl-b d
  sandbox-ai claude -w ~/src/app -p development -D PyPI, GitHub, both APIs; no prompts
  sandbox-ai claude -n                             show the plan, start nothing
  sandbox-ai claude -N web                         named web: sandbox-ai attach|stop|resume web
  sandbox-ai claude --no-keep -- -p "task"         a task whose HOME goes when it ends

Every option has a short form (shown above). One-letter forms can be bundled
(-Dn = --dangerously-skip-permissions --dry-run) and take a value directly
(-w~/src/app); two-letter forms such as -gl are options of their own.

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
"""


HELP_GROUPS = [  # --help: options by topic, in this order
    ("Presets", ["--preset", "--preset-value"]),
    ("Security mode", ["--mode", "--mode-at-least"]),
    ("Workspace and files", ["--workspace", "--workspace-ro", "--mount", "--copy", "--state-dir", "--config",
                             "--no-config"]),
    ("Network", ["--profile", "--allow-host", "--allow-pypi", "--allow-github", "--allow-gitlab",
                 "--allow-kernel-org", "--allow-duckduckgo", "--allow-npm", "--frontier", "--allow", "--allow-tcp",
                 "--allow-host-port", "--no-provider-defaults", "--dns", "--nameserver", "--offline",
                 "--egress-all-allow", "--subnet", "--subnet6", "--network", "--ip", "--ip6", "--vpn",
                 "--vpn-check"]),
    ("Connection proxy, approvals and privacy", ["--ask", "--rules", "--allow-http", "--privacy-check",
                                                 "--privacy-rules", "--no-inspect", "--no-proxy", "--canaries",
                                                 "--no-canaries", "--telemetry"]),
    ("Git", ["--identity", "--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret", "--git-logins", "--token-proxy",
             "--git-credentials"]),
    ("Agent, login and secrets", ["--model", "--effort", "--dangerously-skip-permissions", "--copy-auth", "--no-auth", "--auth-file",
                                  "--import-codex-auth", "--env", "--secret-env", "--api-key", "--publish", "--llm", "--local-config",
                                  "--mcp", "--helper", "--with"]),
    ("Kits, memory and plugins", ["--kit", "--kit-dir", "--no-default-kit", "--no-memory", "--no-plugins"]),
    ("Sessions, terminal and display", ["--audio", "--name", "--project-name", "--keep", "--no-keep", "--session", "--tmux",
                                        "--no-tmux", "--gui", "--gui-screen", "--gui-vnc", "--gui-wayland"]),
    ("Hardware and limits", ["--device", "--gpu", "--memory", "--cpus", "--pids"]),
    ("Engine", ["--backend", "--image", "--image-variant", "--podman-url", "--binary", "--runtime"]),
    ("Logs and checks", ["--log-dir", "--log-debug", "--dry-run", "--probe", "--version", "--help"]),
]


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Each option on one line with its short form and its value once: "-w, --workspace DIR[,DIR...]"."""

    def __init__(self, prog):
        width = min(shutil.get_terminal_size((100, 24)).columns, 110)
        super().__init__(prog, max_help_position=38, width=width)

    def _format_action_invocation(self, action):
        if not action.option_strings:
            return super()._format_action_invocation(action)
        names = ", ".join(action.option_strings)
        if action.nargs == 0:
            return names
        return f"{names} {self._format_args(action, self._get_default_metavar_for_optional(action))}"


def group_help(p):
    """Sort the options into HELP_GROUPS for --help (parsing is unchanged); options not listed come last."""
    listed = {name for _, names in HELP_GROUPS for name in names}
    by_name = {action.option_strings[-1]: action for action in p._actions if action.option_strings}
    general = p._optionals
    for title, names in HELP_GROUPS:
        group = p.add_argument_group(title)
        for name in names:
            action = by_name.get(name)
            if action is not None and action in general._group_actions:
                general._group_actions.remove(action)
                group._group_actions.append(action)
    general.title = "Other options" if any(a.option_strings and a.option_strings[-1] not in listed and
                                           a.help != argparse.SUPPRESS for a in general._group_actions) else None
    p._action_groups.remove(general)
    p._action_groups.append(general)


def build_parser():
    """The launcher's option parser (also used by the wizard to split option lists)."""
    # No prefix matching: abbreviations must not reach internal or forbidden options.
    p = argparse.ArgumentParser(prog="sandbox-ai", usage="sandbox-ai claude|codex|opencode [options] [-- agent arguments]\n"
                                "       claude-sandbox|codex-sandbox|opencode-sandbox (deprecated) [options] [-- agent arguments]",
                                description="Agent in rootless Podman/Bubblewrap + verified Firejail.",
                                epilog=HELP_EPILOG, formatter_class=HelpFormatter, allow_abbrev=False)
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
                        "comma-separated, appear at /workspaces/<name>")
    p.add_argument("--workspace-ro", action="store_true", help="Mount every workspace read-only")
    p.add_argument("--mount", action="append", default=[], metavar="SRC:DEST:ro|rw", help="Repeatable; DEST under /work, /mnt or /opt")
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
    p.add_argument("--preset", metavar="NAME",
                   help="Start from the preset NAME (sandbox-ai presets list): its options, MCP servers, ruleset and "
                        "first task; the command line wins over it")
    p.add_argument("--preset-value", action="append", default=[], metavar="KEY=VALUE",
                   help="A value the preset asks for (sandbox-ai presets show NAME); repeatable")
    p.add_argument("--profile", action="append", default=[], choices=PROFILES, metavar="NAME",
                   help="Network profile: " + "; ".join(f"{name}: {value['help']}" for name, value in PROFILES.items())
                        + "; repeatable")
    p.add_argument("--allow", action="append", default=[], metavar="IP_OR_CIDR", help="Allow destination on all IP protocols/ports; repeatable")
    # Lower case: host names are case-insensitive, EXAMPLE.org and example.org are one host (one pin, one entry).
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST", type=str.lower,
                   help="Pin hostname and allow TCP/443; repeatable")
    for preset, hosts in PRESETS.items():
        p.add_argument(f"--allow-{preset}", action="append_const", const=preset, dest="presets", default=[],
                       help=f"Allow {', '.join(hosts)} (TCP/443)")
    p.add_argument("--allow-tcp", action="append", default=[], metavar="IP:PORT",
                   help="Allow one TCP port on one address, e.g. a local LLM server; IPv6 as [ADDR]:PORT; repeatable")
    p.add_argument("--allow-host-port", action="append", default=[], metavar="PORT",
                   help="Podman: forward 127.0.0.1:PORT in the sandbox to the same TCP port on the host's loopback, "
                        "e.g. a local Git server; repeatable")
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
    p.add_argument("--ask", choices=["on", "off"], default="on",
                   help="With the proxy, ask (sandbox-ai approvals, the app, a desktop notification) before a "
                        "connection to a destination the options do not allow, a lookup of a name they do not pin, "
                        "or a push with privacy warnings; unanswered questions are refused after 60 s (default: on)")
    p.add_argument("--allow-http", action="append", default=[], metavar="'HOST METHODS PATH'",
                   help="Allow only matching requests to HOST (or *.DOMAIN): METHODS such as GET,HEAD or *, PATH a "
                        "pattern where * matches within one path segment and ** across them, e.g. "
                        "'api.github.com GET,HEAD /repos/**'; once a host has rules, every other request to it is "
                        "refused; repeatable")
    p.add_argument("--rules", action="append", default=[], type=Path, metavar="FILE",
                   help="Allow, ask or deny per HTTP request (host, method, path, query, headers, JSON body) and per "
                        "MCP tool call (tool, arguments): a JSON ruleset outside every workspace, applied with "
                        f"~/.config/sandbox-ai/{RULES_NAME} and the administrator's and the project's rules; each can "
                        "only narrow what the others allow; repeatable")
    p.add_argument("--canaries", metavar="CATEGORIES",
                   help="Decoy files with tokens made for the run, which no service accepts: credentials (fake "
                        "~/.aws/credentials, gcloud, SSH key, gh, ~/.netrc, ~/.git-credentials) and home "
                        "(a password list, ~/.kube/config), the default; workspace (a .env in each workspace, kept out of Git and "
                        "removed at the end); comma-separated, or all. A read is reported, a token leaving "
                        "(proxy, DNS, push) is refused and stops the sandbox; rulesets' \"canary\" section decides "
                        "(sandbox-ai canaries list RUN)")
    p.add_argument("--no-canaries", action="store_const", const="none", dest="canaries",
                   help="No canary files")
    p.add_argument("--telemetry", choices=TELEMETRY_LEVELS,
                   help="What Claude Code, Codex and OpenCode report to their makers: off (the default: no usage "
                        "statistics, error reports, feedback uploads, session sharing or update checks), strict "
                        "(also no other call that is not the model's: plugin downloads, model lists, language "
                        "servers; the default of --mode paranoid), on (the agents' own settings); "
                        "\"telemetry\" in ~/.config/sandbox-ai/defaults.json")
    p.add_argument("--no-inspect", action="append", default=[], metavar="HOST",
                   help="Pass TLS to HOST (or *.DOMAIN) through unopened, for programs that do not accept the "
                        "session CA; only the name is logged, and pushes there are not checked; repeatable")
    p.add_argument("--no-provider-defaults", action="store_true",
                   help="Do not open the agent's own provider hosts; only what other options allow")
    p.add_argument("--frontier", action="store_true", help="Also allow OpenAI and Anthropic API/auth hosts (TCP/443); everything else stays denied")
    p.add_argument("--dangerously-skip-permissions", action="store_true",
                   help="Start the agent without approval prompts; the outer sandbox and egress firewall stay in force")
    p.add_argument("--llm", metavar="NAME[:MODEL]",
                   help="Use the LLM server NAME of sandbox-ai llm (Ollama, LM Studio, llama.cpp, vLLM or another "
                        "OpenAI-compatible one, local or remote) with MODEL (default: the endpoint's model): opens "
                        "exactly its address and port and configures the agent for it; its key is added by the "
                        "connection proxy, the agent sees only a placeholder. With --no-provider-defaults the agent "
                        "reaches nothing else")
    p.add_argument("--local-config", type=Path, help="OpenCode local-only config: copy privately and disable default cloud access")
    p.add_argument("--offline", action="store_true", help="No outside network, including provider defaults")
    p.add_argument("--egress-all-allow", action="store_true",
                   help="Allow all outbound traffic (implies --dns); inbound stays closed")
    p.add_argument("--dns", action="store_true",
                   help=f"Real DNS via {' and '.join(DEFAULT_NAMESERVERS)} (UDP/TCP 53 to them only)")
    p.add_argument("--nameserver", action="append", default=[], metavar="IP",
                   help="Use this resolver instead of the defaults; repeatable, implies --dns")
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
    p.add_argument("--image", help="Prebuilt Podman image (default: localhost/<agent>-sandbox:latest); never pulled implicitly")
    p.add_argument("--image-variant", metavar="NAME",
                   help="Your image variant NAME of this agent (sandbox-ai images new NAME --from AGENT): "
                        "localhost/<agent>-sandbox-NAME:latest")
    p.add_argument("--podman-url", metavar="URL", help="Podman service URL, e.g. unix:///example/podman.sock")
    p.add_argument("--binary", metavar="PATH", help="Agent executable (host path with bwrap; image path with podman)")
    p.add_argument("--runtime", action="append", default=[], metavar="DIR", help="bwrap: expose runtime directory read-only at same path")
    p.add_argument("--copy-auth", action="store_true",
                   help="Copy this agent's JSON credential files; they must exist on the host. A login already "
                        "in a reused sandbox HOME (session, --state-dir) is kept. Without any auth option, the "
                        "host's files are copied automatically when present and the sandbox has none yet")
    p.add_argument("--no-auth", action="store_true", help="Disable automatic copying of host credentials")
    p.add_argument("--auth-file", type=Path, help="Copy this JSON file to agent's standard auth path")
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
    p.add_argument("--env", action="append", default=[], metavar="NAME", help="Pass named host variable to agent only; values never printed")
    p.add_argument("--secret-env", action="append", default=[], metavar="NAME[=SECRET]",
                   help="Give the agent the environment variable NAME with the value of the secret SECRET (default: "
                        "NAME) from sandbox-ai secrets; values never printed; repeatable")
    p.add_argument("--model", metavar="MODEL",
                   help="The agent's model: Claude Code's alias (fable, opus, sonnet, haiku) or full name, Codex's "
                        "model name, OpenCode's provider/model (default: the agent's own; with --llm the model comes "
                        "from --llm NAME:MODEL)")
    p.add_argument("--effort", metavar="LEVEL",
                   help="How hard the agent thinks: Claude Code low, medium, high, xhigh, max; Codex minimal, low, "
                        "medium, high, xhigh (model_reasoning_effort); OpenCode sets it per model in its configuration")
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
    p.add_argument("--tmux", action="store_const", const=True, default=None,
                   help="Run in tmux inside the sandbox: window 'agent' plus window 'shell' (bash). The default "
                        "whenever stdin and stdout are a terminal and the agent runs interactively; --tmux insists "
                        "(and fails without a terminal)")
    p.add_argument("--no-tmux", action="store_const", const=False, dest="tmux", help="Run the agent without tmux")
    p.add_argument("--mcp", action="append", default=[], metavar="NAME",
                   help="Connect a local MCP server running in its own sandbox; started if not running "
                        "(see sandbox-ai mcp list; built in: demo)")
    p.add_argument("--helper", action="append", default=[], choices=sorted(AUTH_TARGETS), metavar="AGENT",
                   help="Another agent (claude, codex, opencode) as this agent's helper: it runs as an MCP server in "
                        "a sandbox of its own (its login, its provider's hosts, kits, rules and hooks) on this run's "
                        "main workspace, and this agent gives it tasks with its tool ask")
    p.add_argument("--session", metavar="NAME",
                   help="A kept, named session (the older form of --name NAME): a stopped one of that name comes back; manage "
                        "with sandbox-ai list|attach|pause|resume|stop|delete")
    p.add_argument("--name", metavar="NAME",
                   help="Name of this start (default: AGENT-PROJECT-xxxx); sandbox-ai list shows it, rename changes "
                        "it, and the other commands take it")
    p.add_argument("--project-name", metavar="NAME",
                   help="The PROJECT part of the automatic name (default: the main workspace's folder name; the "
                        "project file's \"name\")")
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
    p.add_argument("--with", dest="companions", action="append", default=[], choices=sorted(AUTH_TARGETS),
                   metavar="AGENT",
                   help="Another agent (claude, codex, opencode) in the same sandbox, for this agent to call from "
                        "the shell (claude -p, codex exec, opencode run): its login, its provider's hosts and its "
                        "managed hooks join this run's; needs the team image (sandbox-ai build team)")
    p.add_argument("--mcp-approvals-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--config", type=Path, metavar="FILE",
                   help=f"Project configuration (default: {CONFIG_NAME} in the main workspace, if trusted)")
    p.add_argument("--no-config", action="store_true", help=f"Ignore {CONFIG_NAME}")
    p.add_argument("--dry-run", action="store_true", help="Print plan; no DNS, auth reads, copies, pods or processes")
    p.add_argument("--probe", action="store_true", help="Verify both sandbox layers and firewall, then exit without agent")
    group_help(p)
    return p


def long_option(action):
    """The long name of an argparse action (its first --option)."""
    return next((name for name in action.option_strings if name.startswith("--")), action.option_strings[0])


def canonical_options(tokens):
    """TOKENS (options as on the command line, up to an optional --) with every short form written as its
    long option, a value attached to a one-letter form (-w/path) split off and bundled flags (-Dn) taken
    apart. Code that looks for options by name (project files, MCP definitions, merging) sees only
    long names; argparse reads both forms the same way."""
    actions = build_parser()._option_string_actions
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
            result.append(token)
            name = token.split("=", 1)[0]
            value_next = name in actions and actions[name].nargs != 0 and "=" not in token
            continue
        action = actions.get(token)
        if action is not None:  # an exact short form: -w, -gl
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
    return result


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
               "min_mode", "require_canaries", "forbid_keep"}
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
    if not isinstance(policy.get("forbid_keep", False), bool):
        raise ValueError(f"Policy {path}: forbid_keep is true or false (every start discards its HOME at the end)")
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
    known = build_parser()._option_string_actions
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
    actions = build_parser()._option_string_actions
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
            used = value not in (f"localhost/{a.agent}-sandbox:latest", *([TEAM_IMAGE] if companions(a) else []))
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


def check_image_variant(a):
    """--image-variant names a variant of this agent from sandbox-ai images (it fixes the image's name only:
    nothing is read from the variant at the start), for Podman, and replaces --image."""
    if not a.image_variant:
        return
    spec = importlib.util.spec_from_file_location("images_for_launcher", Path(__file__).resolve().parent / "images.py")
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    wanted = images.variant_image(a.agent, a.image_variant)
    if a.image != wanted:
        raise ValueError("--image and --image-variant exclude each other")
    if a.backend != "podman":
        raise ValueError("--image-variant applies only to the Podman backend")


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
            if isinstance(mount, dict) and isinstance(mount.get("host"), str):
                with contextlib.suppress(OSError, RuntimeError):
                    hosts.append(Path(os.path.expanduser(mount["host"])).resolve())
    return hosts


def planted_symlink(source, trees):
    """The first symlink on the way to SOURCE (itself or a parent) that lies in one of TREES, where a sandbox
    could have created it; None. Links elsewhere (~/src -> /data/src, /home -> /var/home) are yours."""
    for path in reversed((source, *source.parents)):
        try:
            if not path.is_symlink():
                continue
            holder = path.parent.resolve()
        except OSError:
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


def own_home(path):
    """PATH (resolved) is or lies in a HOME or work folder sandbox-ai itself keeps for a sandbox, inside ~/.local
    but private to that sandbox: a session's or MCP server's home, a login's staging home and work folder, an MCP
    server's default work folder, a team's board, the plugin installer's store, the wizard's persistent HOME of a
    project. Their siblings (session.json, logs, the registry) are not."""
    state = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai").expanduser()
    data = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai").expanduser()
    # The work folders are the workspaces of sandbox-ai's own sandboxes: a login's (empty), an MCP server's
    # without one of its own, a team board's (team.py). Refusing them refused every login and those servers.
    patterns = [(state.resolve() / "sessions", ("*", "home")), (state.resolve() / "mcp", ("*", "home")),
                (state.resolve() / "mcp", ("*", "work")), (state.resolve() / "teams", ("*", "board")),
                (state.resolve() / "home", ("*",)), (data.resolve() / "auth", ("*", "home")),
                (data.resolve() / "auth", ("*", "work")), (data.resolve() / "plugins", ("*",))]
    for base, shape in patterns:
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            continue
        if len(parts) >= len(shape) and all(want == "*" or want == got for want, got in zip(shape, parts)):
            return True
    return False


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


def check_package_location(path, option, packages=None):
    """Refuse an agent-writable host tree PATH (resolved) that is, holds or lies in the package this launcher runs
    from or its copy: the agent could rewrite it, and the next start would run that on the host."""
    for package, label in package_paths() if packages is None else packages:
        inside = path == package or package in path.parents
        if inside or path in package.parents:
            move = "" if label.startswith("the copy") else \
                f" Move it out, e.g. mv {shlex.quote(str(package))} ~/.local/bin/, and start it from there."
            raise ValueError(f"{option} {path} {'lies in' if inside and path != package else 'contains'} {package}, "
                             f"{label}: the agent could rewrite it and run code on the host at the next start.{move}")


def check_private_location(path, option):
    """Refuse an agent-writable host tree PATH (resolved) in or over a private location."""
    if own_home(path):
        return
    fixed, program_dirs = private_locations()
    for private in fixed + program_dirs:
        inside = path == private or private in path.parents
        if inside or (path in private.parents and not project_tool_dir(private, path)):
            raise ValueError(f"{option} {path} {'lies in' if inside else 'contains'} {private}: settings, keys, "
                             "autostart entries or programs on your PATH there run on the host. Use a project "
                             "directory.")


def parse_args(argv):
    # The separator belongs to this wrapper; everything afterwards is opaque.
    tail = []
    if "--" in argv:
        pos = argv.index("--")
        argv, tail = argv[:pos], argv[pos + 1:]
    a = build_parser().parse_args(argv)
    # Commas are invalid in workspace paths, so they can separate entries.
    a.workspaces = [part for value in (a.workspace or [logical_cwd()]) for part in value.split(",")]
    a.subnet_origin = {}  # where a --subnet/--subnet6 came from when no option set it (defaults, policy)
    a.workspace = a.workspaces[0]
    a.gui = a.gui or bool(a.gui_vnc)
    for name in a.profile:
        profile = PROFILES[name]
        a.presets += [preset for preset in profile.get("presets", []) if preset not in a.presets]
        for flag in ("frontier", "dns", "egress_all_allow", "offline"):
            if profile.get(flag):
                setattr(a, flag, True)
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
    for host, _, _ in api_key_values(a, check=False):
        if host not in a.allow_host:
            a.allow_host.append(host)  # validate() reports a wrong --api-key
    for host in publish_hosts(a):
        if host not in a.allow_host:
            a.allow_host.append(host)
    if a.llm:
        llm = load_llm_module()
        llm.network_options(a, loopback_reachable(a))
        a.llm_vpn_note = llm.apply_vpn(a)
    if a.image is None and companions(a) and not a.image_variant:
        a.image = TEAM_IMAGE  # every agent of the run in one image
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


def host_ports(a):
    """Sorted unique ports from --allow-host-port."""
    ports = set()
    for value in a.allow_host_port:
        if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
            raise ValueError(f"--allow-host-port expects a TCP port 1-65535: {value!r}")
        ports.add(int(value))
    web = web_service_port()
    if web in ports:
        raise ValueError(f"--allow-host-port {web}: the web interface (sandbox-ai web) listens there; no sandbox may "
                         "reach it")
    return sorted(ports)


def web_service_port():
    """The port a running `sandbox-ai web` listens on (its listen.json, while its PID lives), else None."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "web" / "listen.json"
    try:
        record = json.loads(path.read_text())
        port, pid = int(record["port"]), int(record["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if 1 <= port <= 65535 else None


def pod_network(a):
    """Podman --network value; host ports are pasta's namespace-to-host forwards (-T), a --subnet/--subnet6 its
    address (-a), IPv4 prefix (-n; IPv6 is a /64) and gateway (-g), laid out as slirp4netns would."""
    if a.offline:
        return "none"
    if a.network:
        return a.network
    ports = host_ports(a)
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
    if "telemetry" in document and document["telemetry"] not in TELEMETRY_LEVELS:
        raise ValueError(f"{path}: telemetry is one of {', '.join(TELEMETRY_LEVELS)}")
    if "preset_dirs" in document:
        # Extra preset directories (presets.py): their shape here; where they lie when one is used (presets.py and
        # check_host_sources: never where an agent writes).
        load_presets_module().check_dirs(document["preset_dirs"], f"{path}: preset_dirs")
    days = document.get("keep_days", 1)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 36500:
        raise ValueError(f"{path}: keep_days is a number of days (1 or more): kept starts unused for longer expire")
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
    """A token from a private file (see read_private_file); the token is never part of an error message."""
    path = Path(path).expanduser()
    data = read_private_file(path, option, 4096)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        token = ""
    if len(data) > 4096 or not re.fullmatch(pattern, token):
        raise ValueError(f"{option}: {path} does not hold a token (one line of letters, digits and _.~+/=-)")
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
        if not re.fullmatch(GIT_TOKEN, token):
            raise ValueError(f"the secret {credential['secret']} does not look like an access token")
        return token
    name = credential["env"]
    if name not in os.environ:
        raise ValueError(f"Host variable is not set: {name}")
    if not re.fullmatch(GIT_TOKEN, os.environ[name]):
        raise ValueError(f"{name} does not look like an access token "
                         "(one line of letters, digits and _.~+/=-)")
    return os.environ[name]


# Git logins added during the run (`sandbox-ai net allow --git-token-*`): the host writes them into the run's
# session directory, which the sandbox sees read-only and which is removed when the run ends; this helper,
# configured at every start, reads them when Git asks.
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
        _git_logins_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_git_logins_module)
        _git_logins_module._launcher = RunningLauncher()
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
        _identities_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_identities_module)
        _identities_module._modules["agent_sandbox"] = RunningLauncher()
        _identities_module._modules["git_logins"] = load_git_logins_module()
        _identities_module._modules["publish_logins"] = load_publish_logins_module()
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
        cached = a._identity = (name, module.get(name))
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


def trusted_configs():
    try:
        return json.loads(config_store().read_text())
    except (OSError, ValueError):
        return {}


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


def trust_config(path, digest=None):
    """Record a project configuration as trusted: the hash of the bytes reviewed, or of the file now."""
    key = config_key(path)
    trusted = trusted_configs()
    trusted[str(key)] = digest or hashlib.sha256(read_config_bytes(key)).hexdigest()
    save_trusted(trusted)


def forget_config(path):
    """Remove a configuration from the trusted list; True if it was there."""
    trusted = trusted_configs()
    if trusted.pop(str(config_key(path)), None) is None:
        return False
    save_trusted(trusted)
    return True


def save_trusted(trusted):
    store = config_store()
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".trusted-", dir=store.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(trusted, stream, indent=2)
    os.replace(temporary, store)


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
        raise ValueError(f"{path} is for {document.get('agent')}; use ./{document.get('agent')}-sandbox or --no-config")
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
        if value == "--telemetry=on" or value == "--telemetry" and options[index + 1:index + 2] == ["on"]:
            raise ValueError(f"{path}: --telemetry on is not allowed in a project configuration: only your command "
                             "line or ~/.config/sandbox-ai/defaults.json turns the agents' telemetry on")
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
    if "name" in document and not (isinstance(document["name"], str) and re.fullmatch(SESSION_NAME, document["name"])):
        raise ValueError(f"{path}: name (the PROJECT part of the automatic names) is 1-40 letters, digits, '_', '.' "
                         "or '-', starting with a letter or digit")
    return path, document


# Options with a host path; the function returns the path part of the value.
PATH_OPTIONS = {"--mount": lambda v: v.rsplit(":", 2)[0], "--copy": lambda v: v.rsplit(":", 1)[0],
                "--git-token-file": lambda v: v.partition("=")[2], "--local-config": str, "--auth-file": str,
                "--import-codex-auth": str, "--state-dir": str, "--log-dir": str, "--gui-vnc": str,
                "--git-credentials": str, "--runtime": str, "--workspace": str, "--kit-dir": str,
                "--privacy-rules": str, "--rules": str}


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
    actions = build_parser()._option_string_actions
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
    workspaces = a.workspaces + [w for w in document.get("workspaces", []) if w not in a.workspaces]
    file_options = document.get("options", []) + (["--log-debug"] if document.get("log_debug") is True else []) + \
        (["--project-name", document["name"]] if isinstance(document.get("name"), str) else []) + \
        ({True: ["--keep"], False: ["--no-keep"]}.get(document.get("keep")) if "keep" in document else [])
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
WORKSPACES_TMPFS = "rw,nosuid,nodev,noexec,size=1m,nr_inodes=4096,mode=0755"


def workspace_targets(a):
    """[(host path, sandbox path)]: the first workspace is always /work."""
    targets, used = [(a.workspaces[0], "/work")], set()
    for value in a.workspaces[1:]:
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
    return {"display": GUI_DISPLAY, "screen": a.gui_screen, "vnc": bool(a.gui_vnc)}


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


def check_private_dir(a, path, option):
    """Reject a host directory the agent could reach through another mount."""
    path = Path(path).expanduser().resolve()
    exposed = [Path(w).expanduser().resolve() for w in a.workspaces] + [Path(parse_mount(m)[0]) for m in a.mount]
    exposed += [a.state_dir.expanduser().resolve()] if a.state_dir else []
    if any(path == other or other in path.parents or path in other.parents for other in exposed):
        raise ValueError(f"{option} must lie outside the workspace, mounts and --state-dir")
    if any(c in str(path) for c in (",", "\n", "\r")):
        raise ValueError(f"{option} cannot contain commas or control characters")


def exposed_trees(a):
    """Host trees the agent can write: workspaces, rw mounts and the persistent HOME."""
    trees = [Path(w).expanduser().resolve() for w in a.workspaces]
    trees += [Path(src) for src, _, mode in map(parse_mount, a.mount) if mode == "rw"]
    trees += [a.state_dir.expanduser().resolve()] if a.state_dir else []
    return trees


def check_host_source(a, path, option, trees=None):
    """Refuse a host file or directory the agent could swap for a symlink before we read it."""
    literal = Path(os.path.normpath(Path(path).expanduser().absolute()))
    for candidate in dict.fromkeys([literal, literal.resolve()]):
        for tree in exposed_trees(a) if trees is None else trees:
            if candidate == tree or tree in candidate.parents:
                raise ValueError(f"{option} {path} lies inside {tree}, which the agent can change; "
                                 "keep it outside workspaces, rw mounts and --state-dir")


HELPER_PREFIX = "helper-"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # Git's empty tree: no .gitattributes for a helper
LONG_TOOL_TIMEOUT = 2000  # seconds Codex waits for a helper's or a team board's call (a helper's task: 1800 at most)


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
    for agent in dict.fromkeys(a.helper):
        name = helper_name(agent, a.workspaces[0])
        # What narrows the lead holds for its helper too: a read-only workspace, the security mode, no login.
        a.helper_definitions[name] = {
            "helper": agent, "workspace": a.workspaces[0], "workspace_ro": bool(a.workspace_ro),
            "mode": getattr(a, "mode", None) or "medium", "no_auth": bool(a.no_auth),
            "skip_permissions": bool(a.dangerously_skip_permissions), "telemetry": telemetry_level(a),
            "rules": [str(Path(path).expanduser().absolute()) for path in a.rules],
            "project_rules": document.get("rules") if isinstance(document, dict) else None,
            "description": f"{agent} as a helper on {a.workspaces[0]}"}
        if name not in a.mcp:
            a.mcp.append(name)


def mcp_socket_dir(name):
    """Host directory holding the Unix socket of MCP server NAME."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "mcp" / name / "sock"


def mcp_bridge_args(name):
    return ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge", f"{MCP_AGENT_DIR}/{name}/server.sock"]


def mcp_agent_args(a):
    """Agent CLI arguments registering every --mcp server as a stdio command."""
    names = list(dict.fromkeys(a.mcp))
    if a.agent == "claude" and names:
        servers = {name: {"command": "python3", "args": mcp_bridge_args(name)} for name in names}
        return ["--mcp-config", json.dumps({"mcpServers": servers})]
    if a.agent == "codex":
        # A helper's task and a team member's wait take longer than Codex's default of 60 s for an MCP call.
        return [value for name in names for value in
                ("-c", f'mcp_servers.{name}.command="python3"', "-c", f"mcp_servers.{name}.args={json.dumps(mcp_bridge_args(name))}",
                 *(("-c", f"mcp_servers.{name}.tool_timeout_sec={LONG_TOOL_TIMEOUT}")
                   if name.startswith((HELPER_PREFIX, "team-")) else ()))]
    return []


def attach_frame(kind, payload):
    """Host-to-sandbox terminal frame: b"d" input bytes or b"r" (rows, columns)."""
    return kind + struct.pack("!I", len(payload)) + payload


def attach_client(socket_path, stdin=0, stdout=1):
    """Host side of an attach: a dumb byte pipe. Nothing from the sandbox is executed."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The directory is writable from the sandbox: a symlink there could
        # point at another socket of the host user and receive our keystrokes.
        fd = os.open(socket_path, os.O_PATH | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError(errno.EPERM, "not a socket owned by you")
            client.connect(f"/proc/self/fd/{fd}")
        finally:
            os.close(fd)
    except OSError as exc:
        client.close()
        raise RuntimeError(f"Session terminal relay not reachable: {socket_path}: {exc}") from None
    interactive = os.isatty(stdin)
    def send_size():
        size = os.get_terminal_size(stdin) if interactive else os.terminal_size((80, 24))
        client.sendall(attach_frame(b"r", struct.pack("!HH", size.lines, size.columns)))
    send_size()
    # The SIGWINCH handler only wakes the loop: a frame sent from the handler could land
    # inside a partially sent input frame and corrupt the stream.
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    def resized(*_):
        try:
            os.write(wake_write, b"r")
        except BlockingIOError:
            pass
    saved = termios.tcgetattr(stdin) if interactive else None
    previous = signal.signal(signal.SIGWINCH, resized) if interactive else None
    if interactive:
        import tty
        tty.setraw(stdin)
    sources = [stdin, client, wake_read]
    try:
        while True:
            ready, _, _ = select.select(sources, [], [])
            if wake_read in ready:
                os.read(wake_read, 4096)
                send_size()
            if stdin in ready:
                data = os.read(stdin, 65536)
                if data:
                    client.sendall(attach_frame(b"d", data))
                else:
                    sources = [client, wake_read]
            if client in ready:
                data = client.recv(65536)
                if not data:
                    break
                view = memoryview(data)
                while view:
                    view = view[os.write(stdout, view):]
    except ConnectionResetError:
        pass
    finally:
        if interactive:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous)
            # Drop terminal answers to queries the sandbox sent just before it closed,
            # so they do not reach the host shell as typed input.
            termios.tcflush(stdin, termios.TCIFLUSH)
        os.close(wake_read)
        os.close(wake_write)
        client.close()
    return 0


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
    try:
        source, dest, mode = value.rsplit(":", 2)
    except ValueError:
        raise ValueError("Mount syntax: SRC:DEST:ro|rw") from None
    try:
        source = Path(source).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Mount source unavailable: {exc}") from None
    if mode not in ("ro", "rw") or not (source.is_dir() or source.is_file()):
        raise ValueError("Mount must be a regular file/directory with ro or rw mode")
    if any(source == base or base in source.parents for base in (DEV, Path("/proc"), Path("/sys"))):
        raise ValueError(f"--mount {source}: device and kernel trees are not mounted; use --device for devices")
    # Socket files are never accepted as individual mounts. Directory contents
    # remain the operator's responsibility (see documentation).
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
        if verdict["decision"] == "ask" and a.ask != "on" and not a.dry_run:
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
    if a.offline and (a.allow or a.allow_host or a.allow_tcp or a.allow_host_port or a.presets or a.frontier
                      or a.network or a.ip or a.ip6 or a.dns or a.nameserver or a.egress_all_allow
                      or a.subnet or a.subnet6):
        raise ValueError("--offline conflicts with network permissions/options")
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
        ipaddress.ip_network(value, strict=False)
    for host in a.allow_host:
        if host.startswith("*."):
            try:
                RUNTIME.wildcard_suffix(host)
            except ValueError as exc:
                raise ValueError(f"--allow-host {host}: {exc}") from None
            if not asking(a) and not a.egress_all_allow:
                raise ValueError(f"--allow-host {host} needs the sandbox's questions (not with --ask off): each name "
                                 "below it is opened as its lookup is answered")
            if nameservers(a) and not a.egress_all_allow:
                raise ValueError(f"--allow-host {host} with --dns or --nameserver: lookups would go past the sandbox's "
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
    check_subnets(a)
    if a.backend == "podman" and a.runtime:
        raise ValueError("--runtime applies only to bwrap; install runtimes in the image")
    if sum(bool(value) for value in (a.copy_auth, a.auth_file, a.import_codex_auth, a.no_auth)) > 1:
        raise ValueError("Choose one of --copy-auth, --auth-file, --import-codex-auth or --no-auth")
    auth_targets = set(AUTH_TARGETS.values()) | {".claude.json"}
    if a.no_auth and any(":" in v and str(PurePosixPath(v.rsplit(":", 1)[1])) in auth_targets for v in a.copy):
        raise ValueError("--no-auth conflicts with --copy of a credential file")
    if a.import_codex_auth and a.agent != "opencode":
        raise ValueError("--import-codex-auth is supported only for OpenCode")
    for value in a.secret_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:=[A-Za-z_][A-Za-z0-9_]{0,63})?", value):
            raise ValueError(f"--secret-env expects NAME or NAME=SECRET (letters, digits, _): {value!r}")
    check_api_keys(a)
    check_model_effort(a)
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
        if not re.fullmatch(MCP_NAME, name):
            raise ValueError(f"--mcp NAME: 1-32 letters, digits, '_' or '-': {name!r}")
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
            credential = git_token_credential(value, kind)
            if not credential["url"].startswith("https://") and not credential["loopback"]:
                raise ValueError(f"--token-proxy: {credential['url']} is plain http; the proxy adds tokens only to "
                                 "HTTPS servers whose certificate it checked, or to a server on this host's loopback")
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
        resolved.append(workspace)
    for index, first in enumerate(resolved):
        for second in resolved[index + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError(f"Workspaces must not overlap: {first} and {second}")
    for workspace in resolved:
        check_workspace_scope(workspace)
    a.workspaces = [str(path) for path in resolved]
    a.workspace = a.workspaces[0]
    workspace = resolved[0]
    add_helpers(a)
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
    a.canary_categories = load_canary_module().categories(a)
    a.log_explicit = bool(a.log_dir)
    if a.log_dir is None and proxy_enabled(a):
        # The connection log is always kept; a workspace containing this directory needs --log-dir elsewhere.
        a.log_dir = default_log_dir(workspace)
        check_private_dir(a, a.log_dir, "The default log directory (set --log-dir)")


def default_log_dir(project):
    """Log directory the wizard uses for PROJECT: per project, also for equal directory names."""
    project = Path(project)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return state / "sandbox-ai" / "logs" / f"{project.name or 'root'}-{digest}"


def check_workspace_scope(workspace):
    """Refuse workspaces that would hand the agent the launcher itself or the host user's world."""
    workspace = Path(workspace)
    if workspace == HERE or workspace in HERE.parents:
        raise ValueError(f"Workspace {workspace} contains sandbox-ai's own launcher ({HERE}); the agent could "
                         "rewrite it and run code on the host at the next start. Install sandbox-ai "
                         "(./install.sh --from .) and start it from there.")
    risky = {Path("/"): "the root directory", Path.home().resolve(): "your HOME directory",
             Path(tempfile.gettempdir()).resolve(): "the temporary directory"}
    if os.environ.get("XDG_RUNTIME_DIR"):
        risky[Path(os.environ["XDG_RUNTIME_DIR"]).resolve()] = "XDG_RUNTIME_DIR"
    for path, label in risky.items():
        if workspace == path or workspace in path.parents:
            raise ValueError(f"Workspace {workspace} is or contains {label}; the agent could change your shell "
                             "start files, credentials and sandbox-ai's trust store. Use a project directory.")
    # The kernel's and the devices' trees (also read-only: /proc/PID/root, /sys/fs/cgroup, /dev/shm), as --mount.
    for tree in (Path("/proc"), Path("/sys"), DEV):
        if workspace == tree or tree in workspace.parents or workspace in tree.parents:
            raise ValueError(f"Workspace {workspace} {'lies in' if tree in workspace.parents else 'is or holds'} "
                             f"{tree}: the kernel's and the devices' trees are never a workspace; use --device for "
                             "devices.")
    check_private_location(workspace, "Workspace")
    check_package_location(workspace, "Workspace")


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
    """[(path, label)] of the stores no agent-writable tree may lie in either (every sandbox reads them)."""
    if not kits_apply(a):
        return []
    kits = load_kits_module()
    return [(path, label) for path, label, used in [
        (kits.default_home(a.agent), "the default kit", not a.no_default_kit),
        (kits.kits_dir(), "the kits directory", bool(a.kit)),
        (kits.state_root() / "memory", "the project memory", not a.no_memory),
        (kits.plugin_store("claude"), "the plugin store", a.agent == "claude" and not a.no_plugins)] if used]


def check_stores(trees, stores):
    """A workspace, rw mount or --state-dir inside a store or kit would let the agent change what every sandbox
    gets (or what its own next run reads back)."""
    for tree in trees:
        for path, label in stores:
            path = Path(os.path.normpath(Path(path).expanduser().absolute()))
            if tree == path or path in tree.parents or tree == path.resolve() or path.resolve() in tree.parents:
                raise ValueError(f"{tree} lies inside {label} ({path}), which the agent would then change "
                                 "for other sandboxes; choose another directory")


def check_host_sources(a):
    """Every host path the launcher reads, mounts or writes back to lies outside agent-writable trees."""
    for path, label in host_sources(a):
        check_host_source(a, path, label)
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
    for value, src, _, mode in mounts:
        check_planted(value.rsplit(":", 2)[0], "--mount", before)
        if mode == "rw":
            check_private_location(Path(src), "--mount")
            check_package_location(Path(src).resolve(), "--mount")
    if a.state_dir:
        check_planted(a.state_dir, "--state-dir", before)
        check_private_location(a.state_dir.expanduser().resolve(), "--state-dir")
        check_package_location(a.state_dir.expanduser().resolve(), "--state-dir")
    if a.log_dir:
        check_planted(a.log_dir, "--log-dir", before)
    for value in a.kit_dir:
        check_planted(value, "--kit-dir", before)
    # An MCP image server mounts its folders again for every connection: the agent must not be able to swap
    # one of them for a link, through a workspace or rw mount that holds it.
    for host in image_mount_hosts(getattr(a, "project_document", None)):
        for tree in exposed_trees(a):
            if tree in host.parents:
                raise ValueError(f"{tree} contains {host}, a folder an MCP image server mounts (see "
                                 f"{Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')}/sandbox-ai/"
                                 "mcp.json); the agent could replace it with a link. Use a workspace beside it, "
                                 "or the folder itself.")


def prepare_workspace(path):
    workspace = Path(path)
    try:
        workspace.mkdir(parents=True)
    except FileExistsError:
        if not workspace.is_dir():
            raise ValueError(f"Workspace path exists but is not a directory: {workspace}") from None
        print(f"agent-sandbox: warning: workspace exists already; using it: {workspace}", file=sys.stderr)


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


def hosts_file(hosts):
    return "127.0.0.1 localhost\n::1 localhost\n" + "".join(
        f"{addr} {name}\n" for name, addresses in hosts.items() for addr in addresses)


def podman_command(a, *args):
    return ["podman", *(["--remote", "--url", a.podman_url] if a.podman_url else []), *args]


PODMAN_HINTS = (
    # (a pattern in Podman's message, lower case; what helps), the first that matches. The message is read without
    # Podman's WARN lines (without a user session four of them come first, whatever the error).
    (r"image not known|no such image|image not found", "the image is not built on this host: sandbox-ai build {agent}"),
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
                         "--entrypoint=/usr/bin/python3", a.image, "-I", "-c", script, json.dumps(hosts))
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


def firewall_rules(networks, addresses, log=False, dns=(), allow_all=False, tcp=(), proxy=None, vpn_mark=None,
                   vpn_rules=()):
    """The sandbox's nftables table. PROXY ({"port", "loopback"}) redirects every TCP connection of the agent
    to the connection proxy, except the proxy's own (PROXY_UID) and those to loopback ports other than
    LOOPBACK (the host's forwarded ports). The chain "runtime" holds what `sandbox-ai net` allows later.
    VPN_MARK (an nft match, see vpn.py) lets the VPN's own packets pass, unredirected: only a process with
    CAP_NET_ADMIN can mark one, and the VPN's kill switch limits where they go. VPN_RULES (vpn.py: Tor's
    loopback ports) open the output chain, before loopback is accepted."""
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
             " chain output { type filter hook output priority 0; policy drop;",
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


def companions(a):
    """The agents --with, without the lead itself, each once."""
    return [agent for agent in dict.fromkeys(getattr(a, "companions", None) or []) if agent != a.agent]


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
        if companion_auth(a, agent) != "auto":
            continue
        for source, dest in auth_sources(agent):
            target = home / dest
            if target.is_symlink():
                raise ValueError(f"Auth destination is a symlink: {dest}")
            if source.is_file() and not target.exists():
                copy_auth_file(source, home, dest)
                copied.append((source, dest))
    return copied


def telemetry_level(a):
    """off, strict or on: --telemetry, else "telemetry" in your defaults file, else off; never below the security
    mode's floor (strict for paranoid; modes.py refuses an option below it)."""
    chosen = getattr(a, "telemetry", None) or getattr(a, "telemetry_default", None) or "off"
    floor = MODES.MODES.get(getattr(a, "mode", None) or "medium", {}).get("telemetry")
    order = ("on", "off", "strict")
    return floor if floor and order.index(floor) > order.index(chosen) else chosen


def telemetry_environment(a):
    """The variables that keep the run's agents (the lead and the agents --with) from reporting to their makers."""
    level, env = telemetry_level(a), {}
    if level == "on" or not runs_agent(a):
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
            "opencode_config": OPENCODE_TELEMETRY_CONFIG if level != "on" and "opencode" in agents else None,
            "codex_config": [".".join(filter(None, (table, key))) for table, key, _, _ in codex_telemetry_keys(level)]
            if "codex" in agents else []}


def telemetry_rules(a):
    """The ruleset "telemetry" (--telemetry off or strict, with the proxy): the agents' telemetry to the hosts
    they need anyway is refused even if an agent ignores its switches. Not for hosts passed unopened
    (--no-inspect), whose paths the proxy cannot see."""
    if telemetry_level(a) == "on" or not runs_agent(a) or not proxy_enabled(a) or a.offline:
        return None
    rules = [{"id": f"telemetry-{agent}-{number}", "host": host, "path": path, "decision": "deny",
              "reason": f"--telemetry {telemetry_level(a)}: {agent}'s telemetry"}
             for agent in team(a) for number, (host, path) in enumerate(TELEMETRY_PATHS.get(agent, []), 1)
             if not any(host_matches_any(host, value) for value in a.no_inspect)]
    return {"version": 1, "description": "the agents' telemetry (--telemetry)", "http": rules} if rules else None


def codex_telemetry_keys(level):
    return [] if level == "on" else CODEX_TELEMETRY["off"] + (CODEX_TELEMETRY["strict"] if level == "strict" else [])


TOML_TABLE_HEADER = r"^[ \t]*\[[ \t]*{name}[ \t]*\][ \t]*(?:#[^\n]*)?$"


def codex_config_with(text, keys, toml):
    """TEXT (~/.codex/config.toml) with each (table, key, value, TOML) of KEYS the file does not set itself, and
    [(name, value)] of those it sets otherwise (they stay: the file is yours). Top-level keys go first; a key of a
    table the file has goes below its [table] header, a table it lacks at the end. The result must parse to the
    same document plus exactly these keys, else ValueError: nothing is written that Codex could read otherwise."""
    original, document = text, toml.loads(text)
    top, tables, below, kept = [], {}, {}, []
    for table, key, value, literal in keys:
        holder = document if table is None else document.get(table)
        if holder is not None and not isinstance(holder, dict):
            raise ValueError(f"{table} is not a table")
        if holder is not None and key in holder:
            if holder[key] != value:
                kept.append((".".join(filter(None, (table, key))), holder[key]))
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
        holder.setdefault(key, value)
    if toml.loads(text) != expected:
        raise ValueError("the changed file would read differently")
    return text, kept


def codex_telemetry_defaults(home, level):
    """--telemetry off/strict for Codex: ~/.codex/config.toml in the sandbox HOME turns off its analytics, feedback
    uploads, metrics (statsig) and update check (strict: the plugin sync too) where the file does not choose itself.
    The file of a --state-dir or session stays yours: what it sets is kept, and said."""
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
        print(f"agent-sandbox: Codex's telemetry is left as its ~/.codex/config.toml says ({printable(str(exc))}); "
              "set analytics.enabled = false, feedback.enabled = false, otel.metrics_exporter = \"none\" and "
              "check_for_update_on_startup = false there", file=sys.stderr)
        return
    for name, value in kept:
        print(f"agent-sandbox: Codex: {name} = {printable(json.dumps(value))} in ~/.codex/config.toml stays "
              "(--telemetry does not override your own file)", file=sys.stderr)
    if content != text:
        replace_private(content.encode(), home, ".codex/config.toml")


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
    if a.copy_auth:
        sources = auth_sources(a.agent)
        if not sources[0][0].is_file():
            raise ValueError(f"Auth JSON missing: {sources[0][0]}; log in with `sandbox-ai login {a.agent}` "
                             "or on the host, or use --auth-file")
        # A login already in a reused HOME is kept (Claude's is refreshed by the newest-wins sync below).
        copies.extend((src, dest, True) for src, dest in sources if src.is_file() and not (home / dest).exists())
    if a.auth_file:
        copies.append((a.auth_file, AUTH_TARGETS[a.agent], False))
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
    if auth_mode(a) == "auto":
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
    if "codex" in team(a) and not getattr(a, "helper_of", None):
        codex_trusted_workspaces(a, home)  # a helper's /work is the lead's too: never trusted for its settings
    if "codex" in companions(a):
        codex_companion_defaults(home)
    if "codex" in team(a) and runs_agent(a) and telemetry_level(a) != "on":
        codex_telemetry_defaults(home, telemetry_level(a))
    if "opencode" in team(a):
        catalog = opencode_catalog()
        target = home / ".cache/opencode/models.json"
        if catalog.is_file() and not target.exists():
            copy_private(catalog, home, ".cache/opencode/models.json")
    if a.llm:
        load_llm_module().prepare_home(a, home, replace_private, update_json_defaults, CLAUDE_STATE_LIMIT)
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
def state_dir_lock(home):
    """Exclusive lock on a --state-dir for the whole run: two runs on one HOME would overwrite each
    other's logins and settings. The agent can write the HOME, so the file is never followed as a
    symlink, never waited on as a FIFO, and the PID goes only into a private file of ours."""
    try:
        fd = os.open(home / STATE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600)
    except OSError as exc:
        raise ValueError(f"--state-dir {home}: cannot open its lock file {STATE_LOCK}: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        # A hard link would make us overwrite another file with the PID.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError(f"--state-dir {home}: {STATE_LOCK} is not a private regular file; remove it")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 16, 0).decode(errors="replace").strip()
            holder = f" (PID {holder})" if holder.isdigit() else ""
            raise ValueError(f"--state-dir {home} is in use by another run{holder}; wait for it to end or "
                             "choose another directory") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield fd
    finally:
        os.close(fd)


def update_json_defaults(home, relative, apply, limit=CONFIG_LIMIT):
    """Let apply() add missing keys to a sandbox-HOME JSON object; existing values always win.

    apply(document) returns whether it changed anything; an unchanged file is
    never rewritten, so a Claude already running on a shared HOME keeps its
    writes. Returns None when done, else why the file was left untouched."""
    path = check_home_path(home, home / relative)
    exists = path.exists()
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
            os.fchmod(stream.fileno(), 0o600)
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


def sync_claude_credentials(source, target, sandbox=None, newer_only=True):
    """Anthropic rotates refresh tokens: whichever side refreshed last holds the
    only valid pair. Copy it over the older side; never downgrade.

    sandbox: "source" or "target", the side the agent could have tampered with.
    newer_only=False lets SOURCE win whenever its tokens differ: the host at
    start, because the sandbox's expiresAt is the agent's to choose."""
    new = claude_oauth(source, follow=sandbox != "source")
    if new is None or Path(target).exists() and Path(source).resolve() == Path(target).resolve():
        return False
    if sandbox == "source" and not 0 < new["expiresAt"] / 1000 <= time.time() + OAUTH_MAX_LIFETIME:
        return False
    old = claude_oauth(target, follow=sandbox != "target")
    if old is not None and (old["expiresAt"] >= new["expiresAt"] if newer_only else
                            all(old.get(key) == new.get(key) for key in OAUTH_TOKEN_FIELDS)):
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
    still hold what it last shared with the sandbox, and the sandbox tokens must differ from that.
    A login the source got meanwhile (the host's own, another sandbox's refresh) always wins."""
    check_home_path(home, pair[1])
    return claude_login_sync(home, pair, before)


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


def claude_login_sync(home, pair, state):
    """Compare a sandbox's Claude login with its source (host or login store) once, under claude_login_lock;
    a status line or None. STATE (claude_writeback_state) holds what the two last shared and follows each sync.

    Refresh tokens rotate, so whichever side refreshed last holds the only valid pair, and it must reach the
    other side before that one refreshes with the used-up token:
    - the sandbox refreshed and the source still holds what they shared: the new tokens go to the source now
      (only a plausible pair newer than the source's; never a login into a source that had none);
    - the source holds another login than the sandbox (another sandbox's refresh, the host's own Claude, a new
      `sandbox-ai login`) and the sandbox did not refresh since: that login goes into the sandbox, where Claude
      reads its file again before refreshing and takes the newer pair instead of sending the used one;
    - both changed: the source wins.
    The sandbox side is read and replaced without following links; tokens never appear in a status line."""
    host, sandbox = pair
    if state.get("host") is None or not state.get("host_login"):
        return None  # Never create a login in the source, not even in an existing file.
    relative = str(Path(sandbox).relative_to(home))
    store = auth_store() in Path(host).parents
    where = "sandbox-ai's login store" if store else "the host"
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
        if changed and oauth_token_digest(source) == state["host"]:
            # The agent chooses the sandbox's expiresAt: only a plausible pair newer than the source's goes back.
            if not source["expiresAt"] < box["expiresAt"] <= (time.time() + OAUTH_MAX_LIFETIME) * 1000:
                state["tokens"], state["refused"] = tokens, oauth_token_digest(box)
                return None
            if Path(host).is_symlink():
                raise ValueError(f"Credential target is a symlink: {host}")
            write_private_file(host, merge_claude_login(Path(host).read_bytes(), box))
            state["host"], state["tokens"] = oauth_token_digest(claude_oauth(host)), tokens
            remember_sync("claude", home, state["host"])
            return f"refreshed Claude credentials written back to {where}"
        if box is None or oauth_token_digest(box) in (oauth_token_digest(source), state.get("refused")):
            return None
        replace_home_file(home, relative, merge_claude_login(data, source))
        state["host"], state["tokens"] = oauth_token_digest(source), oauth_fields(source, OAUTH_TOKEN_FIELDS)
        remember_sync("claude", home, state["host"])
        if changed:
            return f"the Claude login {'in' if store else 'on'} {where} changed during the run; kept that one"
        return f"took over the newer Claude login from {where} (refreshed elsewhere)"


def watch_claude_login(home, pair, state, stop, interval=None):
    """For the whole run, until STOP is set: whenever the sandbox's Claude login or its source changed (an
    lstat every INTERVAL seconds, by default CLAUDE_SYNC_INTERVAL), sync them (claude_login_sync), so that a
    refresh on either side reaches the other at once and not only when the run ends. Status lines are collected
    in STATE["notes"] and printed after the run, since the agent's terminal is busy meanwhile. Never raises."""
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
        if current == seen:
            continue
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
    except (OSError, ValueError):
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
    return sync_claude_credentials(sandbox, source, sandbox="source")


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


def check_login(a):
    """Before the start: say when the login sandboxes would get is missing or expired, and in a terminal
    offer to log in now inside a sandbox (sandbox-ai login), stored for all later sandboxes."""
    if (auth_mode(a) not in ("auto", "copy") or a.probe or a.dry_run or a.local_config or a.tmux_socket_dir
            or a.mcp_socket_dir):
        return
    home = (Path(a.state_dir).expanduser() if a.state_dir else
            load_sessions_module().session_dir(a.session) / "home" if a.session is not None else None)
    if home and (home / AUTH_TARGETS[a.agent]).exists():
        return  # a persistent HOME keeps its own login
    where, problem = login_state(a.agent)
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
    print(f"agent-sandbox: {message}; log in with `sandbox-ai login {a.agent}` (kept for all sandboxes) "
          "or on the host", file=sys.stderr)


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


def load_privacy_module():
    spec = importlib.util.spec_from_file_location("sandbox_privacy", HERE / "privacy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    entries = data.get("workspaces") if isinstance(data, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("mode") == "rw" and isinstance(entry.get("host"), str) \
                and entry["host"].startswith("/"):
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
        except (OSError, ValueError, subprocess.TimeoutExpired):
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
    if sys.platform == "darwin" and not a.podman_url:
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
    """{NAME: value} of --secret-env NAME[=SECRET], read from sandbox-ai secrets; values never printed."""
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
        result[name] = token
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
        if host in (h.lower() for h in a.no_inspect):
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
        _publish_logins_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_publish_logins_module)
        _publish_logins_module._launcher = RunningLauncher()
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
        if logins is None:
            raise
        missing = next(kind for kind in kinds if kind not in {entry["kind"] for entry in logins})
        raise ValueError(f"--publish {missing}: the identity {a.identity} has no login for it; add one with: "
                         f"sandbox-ai identities publish {a.identity} {missing} --secret NAME") from exc
    if a.no_proxy:
        raise ValueError("--publish needs the connection proxy (it adds the tokens on the wire); drop --no-proxy")
    if getattr(a, "mcp_socket_dir", None):
        raise ValueError("--publish is for an agent's sandbox, not an MCP server's")
    inspected_off = {h.lower() for h in a.no_inspect}
    for entry in found:
        for host in entry["hosts"]:
            if host in inspected_off:
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
    """Approvals are asked for: the proxy runs and --ask is on (not in probes)."""
    return proxy_enabled(a) and a.ask == "on" and not a.probe


def apply_remembered_approvals(a):
    """Add what was approved "always" for this project (sandbox-ai approvals) to the options; the notes."""
    approvals = load_approvals_module()
    # The agent must not be able to write what widens its next start: such a file is not read.
    try:
        check_host_source(a, approvals.approved_file(), "Remembered approvals (XDG_CONFIG_HOME)")
    except ValueError as exc:
        return [f"{exc}; not read"]
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
    files += [(path, f"--rules {Path(path).name}") for path in a.rules]
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
            sources.append({"name": source["name"], "version": 1, "defaults": {"mcp": source["defaults"]["mcp"]},
                            "mcp": rules})
    if not sources:
        return None
    config = {"server": name, "rules": sources}
    if approvals and RUNTIME.Ruleset(sources).mcp_asks():
        config["approvals"] = str(approvals)
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
            "no_inspect": [value.lower() for value in a.no_inspect], "allow": proxy_allow(a, hosts),
            "ssh": {"block": a.privacy_check != "off", "allowed": explicit, "allowed_tcp": explicit_tcp,
                    "public_git": public_git},
            "push_check": a.privacy_check != "off",
            "debug": a.log_debug,
            # The agent's own provider: never a source of untrusted content, never held by a rule for tainted runs.
            "providers": provider_hosts(a),
            # --token-proxy: the tokens live here, in the proxy's directory, which the agent cannot read.
            # sandbox-ai git-logins: the same, for every run with the proxy; the start's options win for their server.
            "git_tokens": [{"host": credential["host"], "port": credential["port"], "user": credential["user"],
                            "loopback": credential["loopback"], "token": token}
                           for credential, token in proxied_git_tokens(a)],
            # --llm: the endpoint's key and header secrets, added on the wire; its own CA.
            "llm": load_llm_module().proxy_entry(a) if a.llm else [],
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
            **proxy_rules(a, hosts)}


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


def hooks_active(a):
    """The agent's own tools have rules in this run (a "tool" rule or default, or a tool whose results are
    untrusted): its hooks are installed. Not in probes and MCP server sandboxes, where no agent runs."""
    return kits_apply(a) and any(
        source.get("tool") or source["defaults"].get("tool", "allow") != "allow"
        or any("agent" in entry for entry in source.get("untrusted", [])) for source in load_rulesets(a))


def hooks_config(a):
    """config.json's "hooks" for the supervisor's answer to the hooks: the agent (its version is checked) and the
    rulesets' rules for its own tools and its tools whose results are untrusted. None without hooks."""
    if not hooks_active(a):
        return None
    return {"agent": a.agent, "rules": [
        {"name": source["name"], "version": 1, "defaults": {"tool": source["defaults"]["tool"]},
         "tool": source["tool"], "untrusted": [e for e in source.get("untrusted", []) if "agent" in e]}
        for source in load_rulesets(a) if source.get("tool") or source["defaults"]["tool"] != "allow"
        or any("agent" in e for e in source.get("untrusted", []))]}


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


def managed_hooks(a):
    """The agent gets managed hooks this run: the rule hooks (hooks_active), approved kit hooks, or both."""
    return hooks_active(a) or bool(trusted_kit_hooks(a))


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
    if hooks_active(a):
        for agent in companions(a):
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
        lines = ["# sandbox-ai: the agent's hooks (README, Rulesets: \"The agent's own tools\"; Kits: hooks)",
                 "allow_managed_hooks_only = true", "", "[features]", "hooks = true"]
        for event, entries in groups.items():
            for entry in entries:
                lines += ["", f"[[hooks.{event}]]", f"matcher = {toml_string(entry.get('matcher', '*'))}"]
                for handler in entry["hooks"]:
                    lines += ["", f"[[hooks.{event}.hooks]]", 'type = "command"',
                              f"command = {toml_string(handler['command'])}"]
                    lines += [f"timeout = {handler['timeout']}"] if "timeout" in handler else []
        return {"codex/requirements.toml": "\n".join(lines) + "\n"}
    files = {PurePosixPath(OPENCODE_HOOK_PLUGIN).name: OPENCODE_HOOKS_JS} if hooks_active(a) else {}
    if opencode_recheck(a) and not companion:
        files[PurePosixPath(OPENCODE_HOOK_RECHECK).name] = OPENCODE_HOOKS_JS.replace(
            "const RECHECK = false;", "const RECHECK = true;", 1)
    return files


def hook_mount(a, session):
    """(directory in SESSION, target) of the agent's managed configuration with its hooks, mounted read-only;
    None without hooks, and for OpenCode (its plugin lies in the session directory, mounted anyway)."""
    if a.agent not in MANAGED_DIRS or not managed_hooks(a):
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
            sources.append({"name": source["name"], "version": 1, "mcp": rules, "untrusted": untrusted})
    return {"server": name, "rules": sources}


def proxy_rules(a, hosts):
    """proxy.json's "rules" (the rulesets with HTTP rules, --allow-http aside: it comes as http_rules) and
    "pinned" (the run's host addresses, so that a host's rules hold for its bare address too)."""
    rules = [{"name": source["name"], "version": 1, "defaults": {"http": source["defaults"]["http"]},
              "http": source["http"], "untrusted": [entry for entry in source.get("untrusted", []) if "host" in entry]}
             for source in load_rulesets(a)
             if source["name"] != "--allow-http" and (source["http"] or source["defaults"]["http"] != "allow"
                                                       or any("host" in entry for entry in source.get("untrusted", [])))]
    return {"rules": rules, "pinned": {name: list(values) for name, values in hosts.items()}} if rules else {}


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


def rotate_connection_logs(directory, limit=CONNECTION_LOGS_LIMIT):
    """Delete the oldest connections-*.jsonl while they hold more than LIMIT bytes; the names deleted."""
    try:
        logs = sorted((p for p in Path(directory).glob("connections-*.jsonl") if p.is_file() and not p.is_symlink()),
                      key=lambda p: p.name)
    except OSError:
        return []
    total, removed = sum(p.stat().st_size for p in logs), []
    recent = time.time() - 3600  # written within the hour: possibly a sandbox that still runs
    for oldest in logs:
        if total <= limit:
            break
        if oldest.stat().st_mtime > recent:
            continue
        total -= oldest.stat().st_size
        oldest.unlink()
        removed.append(oldest.name)
    return removed


def connection_summary(path):
    """Lines about one run's connection log: how much was logged, refused pushes, TLS that failed."""
    counts, refused, tls = {"http": 0, "other": 0, "denied": 0}, [], {}
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                event = record.get("event")
                if event == "http":
                    counts["http"] += 1
                    push = record.get("push")
                    if isinstance(push, dict) and not push.get("ok"):
                        refused.append(", ".join(push.get("refs") or []) or record.get("url", "?"))
                elif event == "denied":
                    counts["denied"] += 1
                elif event in ("tls-refused", "tls-upstream-failed"):
                    tls[str(record.get("host") or record.get("destination"))] = event
                else:
                    counts["other"] += 1
    except OSError:
        return []
    lines = [f"proxy: {counts['http']} HTTP request(s), {counts['other']} other connection(s), "
             f"{counts['denied']} refused; log: {path}"]
    if refused:
        lines.append(f"proxy: refused {len(refused)} push(es) the privacy check stopped: {'; '.join(refused[:5])}")
    for host, event in sorted(tls.items())[:10]:
        lines.append(f"proxy: TLS to {host} failed ({'the program rejected the session CA' if event == 'tls-refused' else 'the server certificate is not trusted'}); "
                     f"--no-inspect {host} passes it through unopened")
    return lines


def runs_dir():
    """Where running sandboxes register (for sandbox-ai net)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "sandbox-ai" if runtime and Path(runtime).is_dir() else \
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai"
    return base / "runs"


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
        except (OSError, ValueError, TypeError, AttributeError):
            alive = False
        if alive:
            raise ValueError(f"A sandbox named {run} is already running (see sandbox-ai net list)") from None
        shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
    a.runtime_mode = MODES.runtime_record(launcher_namespace(), a)
    (directory / "run.json").write_text(json.dumps({
        "run": run, "agent": a.agent, "backend": a.backend, "workspace": a.workspaces[0], "pid": os.getpid(),
        # The PID's start time: `sandbox-ai net stop` signals this process only, never a later one of that PID.
        "pid_start": process_start(os.getpid()),
        "session": a.session or (a.pod_name if a.tmux_socket_dir else None), "session_dir": str(session),
        # sandbox-ai list, rename and the other commands find a run by its name; "start" links it to its
        # start directory (sessions.py), "pod" is what pause and resume freeze with Podman.
        "name": run_name(a), "start": getattr(a, "start_key", None) or a.start_id,
        "kind": "mcp" if a.mcp_socket_dir else "agent",
        "pod": pod_name(a, run) if a.backend == "podman" else None, "podman_url": a.podman_url,
        # A HOME of your own (--state-dir) stays when the run ends; sandbox-ai list says so.
        "own_home": bool(a.state_dir) and not (getattr(a, "start_key", None) or a.tmux_socket_dir or a.mcp_socket_dir),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "proxy": proxy_enabled(a),
        # sandbox-ai activity and session events find the run's timeline (activity.jsonl) here.
        "log_dir": str(Path(a.log_dir).expanduser().resolve()) if a.log_dir else None,
        # devices.jsonl keeps serial numbers in full only then.
        "log_debug": bool(getattr(a, "log_debug", False)),
        # `sandbox-ai net allow` refuses destinations inside a chosen network of the sandbox's own.
        "subnets": [v for v in (getattr(a, "subnet", None), getattr(a, "subnet6", None)) if v],
        # `sandbox-ai net allow --git-token-*` writes logins its Git helper reads (not with --token-proxy).
        "runtime_git_tokens": not a.token_proxy,
        # `sandbox-ai net allow --allow-host-port` relays host ports into this run (the supervisor's PortRelay).
        "host_relay": True,
        # The MCP servers this run reaches (--mcp, helpers included): sandbox-ai mcp list names whom each serves.
        "mcp_servers": [] if a.mcp_socket_dir else list(dict.fromkeys(a.mcp)),
        # The security mode: `sandbox-ai net allow` and `sandbox-ai approvals` keep to it (modes.py).
        "mode": a.runtime_mode,
        # --allow-host *.DOMAIN: the launcher answers the lookups below them itself (approvals.watch).
        "allow_wildcards": host_wildcards(a),
        # The sandbox's cgroup is its own (the pod's; Bubblewrap's systemd scope), not the launcher's shared one:
        # only then may `workspace remove` freeze it and count its processes.
        "own_cgroup": a.backend == "podman" or bool(limits_prefix(a)),
        # `sandbox-ai workspace add` gives the push check a workspace added during the run (push-added.json there).
        **({"push_check": {"dir": str(a.push_check_dir), "rules_dir": str(privacy_rules_dir(a) or "") or None}}
           if getattr(a, "push_check_dir", None) else {}),
        # `sandbox-ai workspace add` holds a workspace added during the run to the checks of a start (workspaces.py).
        **({} if a.mcp_socket_dir else {"workspace_checks": workspace_checks(a, session)})}))
    return directory


def workspace_checks(a, session):
    """What `sandbox-ai workspace add` checks a workspace added during the run against, as the start checked its
    own: the workspaces (host, sandbox path, ro|rw), whether further ones are read-only, the trees no workspace may
    overlap, the host paths none may hold (host_sources and the private directories) and the stores none may lie
    in. Paths as given and resolved, so that a later lookup needs nothing of A."""
    def both(path):
        literal = Path(os.path.normpath(Path(path).expanduser().absolute()))
        return list(dict.fromkeys([str(literal), str(literal.resolve())]))
    trees = [Path(src) for src, _, _ in map(parse_mount, a.mount)]
    trees += [p for p in (a.state_dir and a.state_dir.expanduser().resolve(), getattr(a, "sandbox_home", None)) if p]
    held = [(path, label) for path, label in host_sources(a)]
    held += [(exposed_registry(), "The registry of exposed directories (XDG_STATE_HOME)")]
    held += [(host, "A folder an MCP image server mounts (mcp.json)")
             for host in image_mount_hosts(getattr(a, "project_document", None))]
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
            "stores": [[str(path), label] for path, label in host_stores(a)]}


def run_name(a):
    """The name this run shows (sandbox-ai list): its start's current name for a session's supervisor (renamed
    perhaps since it was started), else the name the start got (name_start); None for MCP servers."""
    if a.start_id:
        try:
            data = load_sessions_module().load(a.start_id)
        except (OSError, ValueError):
            return a.start_id
        return data.get("name") if isinstance(data.get("name"), str) else a.start_id
    return getattr(a, "start_name", None)


def pod_name(a, run=None):
    """The Podman pod of this run (RUN, default a.run_id): the VPN's pod (vpn.py made it), the one a session or
    MCP server chose, else agent-RUN."""
    run = run or getattr(a, "run_id", None) or f"{a.agent}-{uuid.uuid4().hex[:12]}"
    return getattr(a, "vpn_pod", None) or a.pod_name or f"agent-{run}"


def load_starts_module():
    spec = importlib.util.spec_from_file_location("sandbox_starts", HERE / "starts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_label(a):
    """The PROJECT part of the automatic names: --project-name (the project file's "name"), else the main
    workspace's folder name, made fit for a name."""
    return a.project_name or load_starts_module().sanitise(PurePosixPath(a.workspaces[0]).name)


def name_start(a):
    """The name of this start: --session or --name as given (a --name in use is refused), else
    AGENT-PROJECT-xxxx. None for an MCP server and a session's supervisor (its start is named already)."""
    if a.mcp_socket_dir or a.tmux_socket_dir:
        return None
    if a.session is not None:
        return a.session  # sessions.py decides: a new session or the restart of a stopped one
    starts = load_starts_module()
    if a.name is not None:
        if not a.dry_run:
            starts.check_free(a.name)
        return a.name
    return starts.auto_name(a.agent, project_label(a))


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
    return [PurePosixPath(path) for path in paths]


def kit_plan(a):
    """What the kits, the project memory and the plugin store give this run, without changing anything."""
    if not kits_apply(a):
        return None
    kits = load_kits_module()
    plan = kits.scan(a.agent, kits.sources(a))
    marketplaces, plugins, notes = kits.declarations(a.agent, plan["plugins"]) if not a.no_plugins else ({}, [], [])
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
    if a.agent == "claude" and not a.no_plugins:
        _, installed = kits.installed("claude")
        result["plugins"] = {"marketplaces": sorted(marketplaces), "enabled": [p for p in plugins if p in installed],
                             "missing": [p for p in plugins if p not in installed]}
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
    trees = exposed_trees(a)
    staging.mkdir(mode=0o755)
    staged, more = kits.stage(a.agent, plan, staging,
                              lambda path, label: check_host_source(a, path, f"Kit file ({label})", trees))
    notes += more
    a.kit_hooks = kits.stage_hooks(a.agent, kits.sources(a), staging / "hooks", KIT_HOOKS_TARGET)
    notes += kit_hook_notes(a.agent, a.kit_hooks)
    for source, dest in staged:
        ensure_mount_point(home, str(PurePosixPath(dest).relative_to("/root")))
        mounts.append((source, dest, "ro"))
    check = lambda path, label: check_host_source(a, path, f"Kit file ({label})", trees)  # noqa: E731
    for label, path in plan["plugins"]:
        check(path, label)
        check(path.resolve(), label)
    if not a.no_memory:
        manifest, seeded, store, more = kits.memory_prepare(a.agent, a.workspaces[0], home, plan["memory"],
                                                            check, persistent=bool(a.state_dir))
        notes += [f"memory: {note}" for note in more]
        memory = manifest
        if seeded:
            notes.append(f"new project memory {store}, started from the {kits.MEMORY_FILE[a.agent][0]} of {seeded}")
    if not a.no_plugins:
        marketplaces, plugins, more = kits.declarations(a.agent, plan["plugins"])
        notes += more
        if a.agent == "claude":
            mounts += claude_plugins(home, marketplaces, plugins, notes)
            a.plugin_hooks = kits.stage_plugin_hooks(plugins, kits.installed("claude")[1])
            notes += plugin_hook_notes(a)
        elif a.agent == "opencode" and plugins:
            a.opencode_plugins = plugins
            if not a.egress_all_allow and "registry.npmjs.org" not in allowed_hosts(a):
                notes.append("OpenCode installs its plugins from npm: allow it with --allow-host registry.npmjs.org")
    elif a.agent == "claude":
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
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ValueError("Codex auth must contain ChatGPT access/refresh tokens, account_id and a JWT expiry") from None
    return {"openai": {"type": "oauth", "access": access, "refresh": refresh,
                       "expires": expires * 1000, "accountId": account}}


def agent_environment(a):
    env = telemetry_environment(a)  # first: what you pass yourself (--env, --secret-env, --llm) wins
    env.update({key: os.environ[key] for key in a.env})
    env.update(secret_environment(a))
    env.update(api_key_environment(a))
    env.update(git_environment(a))
    env.update(publish_environment(a))
    if a.local_config:
        env.update({"OPENCODE_CONFIG": "/root/.config/opencode/opencode.jsonc",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1"})
    if a.llm:
        env.update(load_llm_module().agent_environment(a))
    if a.dangerously_skip_permissions:
        for agent in team(a):
            env.update(DANGER_ENV[agent])
    if companions(a):  # who else is here, for the agent (and the sandbox-ai skill)
        env["SANDBOX_AI_COMPANIONS"] = " ".join(companions(a))
    if a.tmux_socket_dir:
        env["TMUX_TMPDIR"] = TMUX_DIR
    if getattr(a, "helper_of", None) and a.agent == "opencode":
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"  # the lead shares /work: no plugins or settings from there
    content = {}
    if a.agent == "opencode" and a.mcp and runs_agent(a):
        content["mcp"] = {name: {"type": "local", "command": ["python3", *mcp_bridge_args(name)], "enabled": True}
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
    if "opencode" in team(a) and runs_agent(a) and telemetry_level(a) != "on":
        content.update(OPENCODE_TELEMETRY_CONFIG)  # after the project's and your own configuration: this wins
    if content:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content)
    if proxy_enabled(a):
        env.update({name: str(CA_DIR / "bundle.pem") for name in CA_BUNDLE_VARIABLES})
        env["NODE_EXTRA_CA_CERTS"] = str(CA_DIR / "ca.pem")
    if getattr(a, "run_id", None):
        env["SANDBOX_AI_RUN"] = a.run_id
    if asking(a):
        # Browser requests (OAuth logins and the like) go to you as questions.
        env["PATH"] = "/sandbox/session/bin:" + base_env()["PATH"]
        env["BROWSER"] = "/sandbox/session/bin/xdg-open"
    if a.gui:
        env["DISPLAY"] = GUI_DISPLAY
    if getattr(a, "audio", None) == "virtual":
        env["PULSE_SERVER"] = f"unix:{RUNTIME.AUDIO_SOCKET}"
    if a.gui_wayland:
        env.update({"WAYLAND_DISPLAY": PurePosixPath(WAYLAND_TARGET).name,
                    "XDG_RUNTIME_DIR": str(PurePosixPath(WAYLAND_TARGET).parent),
                    "MOZ_ENABLE_WAYLAND": "1", "QT_QPA_PLATFORM": "wayland;xcb",
                    "ELECTRON_OZONE_PLATFORM_HINT": "auto"})
    return env


def check_model_effort(a):
    """--model and --effort: a model name (no spaces or options), an effort level the agent knows, not together with
    --llm's own model, and not given twice (as an option and in the agent's arguments)."""
    model, effort = getattr(a, "model", None), getattr(a, "effort", None)
    if model is not None:
        if not re.fullmatch(MODEL_NAME, model):
            raise ValueError(f"--model expects a model name (letters, digits, . _ : / @ + -): {model!r}")
        if a.llm:
            raise ValueError("--model and --llm: name the model in --llm NAME:MODEL instead")
    if effort is not None:
        levels = AGENT_EFFORTS.get(a.agent, ())
        if not levels:
            raise ValueError(f"--effort: {a.agent} sets its effort per model in its own configuration "
                             "(opencode.json, a model's options); start without --effort")
        if effort not in levels:
            raise ValueError(f"--effort for {a.agent} is one of {', '.join(levels)}: {effort!r}")
    given = a.agent_args[:a.agent_args.index("--")] if "--" in a.agent_args else a.agent_args
    for option, value, flags in (("--model", model, ("--model", "-m")), ("--effort", effort, ("--effort",))):
        if value is not None and any(arg in flags or arg.startswith(f"{flags[0]}=") for arg in given):
            raise ValueError(f"{option} is given twice: as the launcher's option and in the agent's arguments")


def model_effort_args(a):
    """The agent's own arguments for --model and --effort."""
    args, model, effort = [], getattr(a, "model", None), getattr(a, "effort", None)
    if model:
        args += ["--model", model]
    if effort:
        if a.agent == "claude":
            args += ["--effort", effort]
        elif a.agent == "codex":
            args += ["-c", f"model_reasoning_effort={effort}"]
    return args


def agent_command(a, binary):
    args = list(a.agent_args)
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
    if a.llm and a.binary is None and not a.probe:
        args = [*load_llm_module().agent_arguments(a), *args]
    if a.binary is None and not a.probe and not getattr(a, "helper_of", None):
        args = [*model_effort_args(a), *args]
    return [binary, *args]


def mount_option(src, dest, mode):
    if any(c in str(src) + dest for c in (",", "\n", "\r")):
        raise ValueError("Podman bind paths cannot contain commas or newlines")
    # nodev: device files inside a mounted tree stay unusable (devices only through --device).
    return ["--mount", f"type=bind,src={src},dst={dest},{'ro' if mode == 'ro' else 'rw'},nodev"]


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
    if a.gui or a.gui_wayland:
        # Chromium-based apps exhaust Podman's default 64 MB /dev/shm.
        create += ["--shm-size=1g"]
    owner = pod_owner(a)
    if owner:
        create += ["--label", f"{SESSION_LABEL}={owner}"]
    if a.ip:
        create += ["--ip", a.ip]
    if a.ip6:
        create += ["--ip6", a.ip6]
    run = podman_command(a, "run", "--rm", "--pull=never", "--pod", name, "--user=0:0",
           "--read-only", "--cap-drop=ALL", "--security-opt=seccomp=unconfined",
           "--security-opt=apparmor=unconfined", "--security-opt=label=disable",
           "--tmpfs=/run:rw,nosuid,nodev", f"--tmpfs=/tmp:rw,nosuid,nodev{tmpfs_size(enforced_limits(a)[0])}",
           f"--tmpfs=/var/tmp:rw,nosuid,nodev{tmpfs_size(enforced_limits(a)[0])}",
           f"--tmpfs={WORKSPACES_DIR}:{WORKSPACES_TMPFS}", "--workdir=/work", "--interactive")
    limits = enforced_limits(a)[0]
    if "memory" in limits:
        run += ["--memory", limits["memory"]]
    if "cpus" in limits:
        run += ["--cpus", limits["cpus"]]
    if "pids" in limits:
        run += ["--pids-limit", str(limits["pids"])]
    # The bootstrap's DNS logging stub binds port 53; Firejail drops it again.
    if a.pod_name:
        # Named so that `sandbox-session attach` can exec into it.
        run += ["--name", f"{name}-agent"]
    for cap in CAPS + (["NET_BIND_SERVICE"] if a.log_dir else []):
        run += ["--cap-add", cap]
    if sys.stdin.isatty() and sys.stdout.isatty():
        run += ["--tty"]
    bindings = [(HERE / "runtime.py", "/sandbox/runtime.py", "ro"),
                # The Public Suffix List beside it: the supervisor checks host wildcards against it (runtime.py).
                *([(HERE / "public_suffix_list.dat", "/sandbox/public_suffix_list.dat", "ro")]
                  if (HERE / "public_suffix_list.dat").is_file() else []),
                (session, "/sandbox/session", "ro"), (home, "/root", "rw"),
                *((Path(src).expanduser().resolve(), dest, workspace_mode(a, index))
                  for index, (src, dest) in enumerate(workspace_targets(a))),
                (session / "hosts", "/etc/hosts", "ro"),
                (session / "resolv.conf", "/etc/resolv.conf", "ro"),
                (session / "nsswitch.conf", "/etc/nsswitch.conf", "ro")]
    bindings += [parse_mount(value) for value in a.mount]
    # Kits after /root, so that they lie on top of the sandbox HOME.
    bindings += [(src, dest, mode) for src, dest, mode in getattr(a, "kit_mounts", [])]
    if a.log_dir:
        bindings.append((a.log_dir.expanduser().resolve(), str(LOG_DIR), "rw"))
    if a.gui_vnc:
        bindings.append((a.gui_vnc.expanduser().resolve(), str(VNC_DIR), "rw"))
    if a.tmux_socket_dir:
        bindings.append((a.tmux_socket_dir.expanduser().resolve(), TMUX_DIR, "rw"))
    if a.mcp_socket_dir:
        bindings.append((a.mcp_socket_dir.expanduser().resolve(), MCP_SERVE_DIR, "rw"))
    if a.mcp_approvals_dir:
        bindings.append((a.mcp_approvals_dir.expanduser().resolve(), MCP_APPROVALS_DIR, "ro"))
    for name in dict.fromkeys(a.mcp):  # reached through the supervisor's relay (MCP_AGENT_DIR), never directly
        bindings.append((mcp_socket_dir(name), f"{MCP_UPSTREAM_DIR}/{name}", "ro"))
    if hook_mount(a, session):  # the agent's managed configuration: its hooks, which it cannot change
        bindings.append((*hook_mount(a, session), "ro"))
    for source, target in companion_hook_mounts(a, session):
        bindings.append((source, target, "ro"))
    if a.gui_wayland:
        bindings.append((wayland_socket(), WAYLAND_TARGET, "rw"))
    if getattr(a, "proxy_dir", None):
        bindings.append((a.proxy_dir, str(PROXY_DIR), "ro"))
    if getattr(a, "control_dir", None):
        bindings.append((a.control_dir, str(CONTROL_DIR), "rw"))
    if getattr(a, "browser_dir", None):
        bindings.append((a.browser_dir, str(BROWSER_DIR), "ro"))
    for src, dest, mode in bindings:
        run += mount_option(src, dest, mode)
    for path, kind in devices(a):
        # A directory as a bind mount, not --device: Podman would copy only the devices present now.
        run += ["--mount", f"type=bind,src={path},dst={path},rw,dev"] if kind == "dir" else ["--device", path]
    for key, value in base_env().items():
        run += ["--env", f"{key}={value}"]
    run += ["--entrypoint=/usr/bin/python3", a.image, "-I", "/sandbox/runtime.py", "--internal-bootstrap"]
    return create, run


def process_start(pid, proc=Path("/proc")):
    """Start time of PID in clock ticks after boot (field 22 of /proc/PID/stat), or None if it is gone.
    Recorded with a supervisor's PID, so a later check can tell a reused PID apart."""
    try:
        # comm (field 2) may contain spaces and parentheses: the fields after it follow the last ')'.
        return int((proc / str(pid) / "stat").read_text().rpartition(")")[2].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def process_cmdline(pid, start, proc=Path("/proc")):
    """Arguments of the process recorded as PID with start time START; [] when it is gone or the PID
    now belongs to another process. The caller still matches the arguments: records of older
    versions have no START (None) and rely on that check alone."""
    if not pid:
        return []
    try:
        cmdline = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
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
    except (OSError, subprocess.TimeoutExpired):
        return False  # Podman unusable: starting the pod reports it
    if found.returncode != 0:
        return False  # no such pod
    try:
        labels = json.loads(found.stdout.strip() or "null") or {}
    except ValueError:
        labels = {}
    if not isinstance(labels, dict) or labels.get(SESSION_LABEL) != str(owner):
        raise ValueError(f"A Podman pod named {pod} already exists and was not created here. "
                         f"If it is left over from an earlier run, remove it with: podman pod rm --force {pod}")
    subprocess.run(podman_command(a, "pod", "rm", "--force", pod), check=True,
                   stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return True


# --- memory watch ----------------------------------------------------------------------------------------------
#
# The kernel enforces --memory: when the sandbox's cgroup is full, its OOM killer ends a process, and if that is the
# supervisor (PID 1 of the sandbox) the whole run ends. The launcher watches the cgroup from the host (the sandbox
# cannot see it): at MEMORY_WARN it tells the human and the agent (the session directory's memory.json, which the
# agent reads), at MEMORY_ACT it ends the largest process that is neither sandbox-ai's nor the agent's (a test run,
# a build), before the kernel picks one. Files in the sandbox's /tmp and HOME (tmpfs) count as memory too.

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
CGROUP_ROOT = Path("/sys/fs/cgroup")
# Never ended by the watch: sandbox-ai's own processes (their command lines name an --internal- entry point too).
MEMORY_PROTECTED = {"kthreadd", "firejail", "tmux: server", "tmux: client", "conmon", "catatonit", "pasta",
                    "pasta.avx2", "slirp4netns", "bwrap", "Xvfb", "x11vnc"}
AGENT_PROGRAMS = {"claude", "codex", "opencode"}


def cgroup_dir(pid, proc=Path("/proc"), root=CGROUP_ROOT):
    """The cgroup v2 directory of PID that carries a memory limit (its own, or its parent's when the process sits in
    a leaf without one), or None (no cgroup v2, no limit, gone)."""
    try:
        text = (Path(proc) / str(int(pid)) / "cgroup").read_text()
    except (OSError, ValueError):
        return None
    line = next((entry for entry in text.splitlines() if entry.startswith("0::")), None)
    if line is None:
        return None
    path = Path(root) / line[3:].strip().lstrip("/")
    for candidate in (path, path.parent):
        try:
            if (candidate / "memory.max").read_text().strip() not in ("max", ""):
                return candidate
        except OSError:
            continue
    return None


def memory_state(cgroup):
    """{"current", "limit", "oom_kill", "anon", "shmem"} (bytes, kills) of CGROUP; shmem is what tmpfs files hold.
    The limit is the smallest memory.max on the path from CGROUP up to the cgroup root (one set on a parent, a pod's,
    holds CGROUP as well), with that cgroup's use as "current"; read again at every look. ValueError when no
    cgroup on the path has a limit."""
    cgroup = Path(cgroup)
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
        if directory.parent == directory:
            break
        directory = directory.parent
    if not levels:
        raise ValueError(f"no memory limit on {cgroup} or above")
    current, limit = min(levels, key=lambda level: level[1])  # the smallest; of equal ones the nearest

    def pairs(name):
        found = {}
        for line in (cgroup / name).read_text().splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                found[key] = int(value)
        return found
    stat, events = pairs("memory.stat"), pairs("memory.events")
    return {"current": current, "limit": limit, "oom_kill": events.get("oom_kill", 0), "anon": stat.get("anon", 0),
            "shmem": stat.get("shmem", 0)}


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


class MemoryWatch:
    """Watches the sandbox's memory from the host while it runs (see above). LOCATE() gives (cgroup, protected pids)
    once the sandbox's processes exist, or None; SESSION is the session directory (memory.json for the agent), LOG
    a file for memory.jsonl records (or None), NOTIFY(title, text) tells the human."""

    def __init__(self, locate, name, session=None, log=None, notify=None, warn=MEMORY_WARN, act=MEMORY_ACT,
                 interval=MEMORY_INTERVAL, grace=MEMORY_GRACE, clock=time.monotonic, kill=os.kill, proc=Path("/proc"),
                 control=None):
        self.locate, self.name, self.session, self.log, self.notify = locate, name, session, log, notify
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
        self.stopping, self.thread, self.written = threading.Event(), None, 0.0

    def record(self, event, **fields):
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "run": self.name, **fields}
        self.events.append(entry)
        if len(self.events) > MEMORY_EVENTS:  # a long run under pressure: the first and the latest are told
            del self.events[MEMORY_EVENTS // 2]
        if self.log is not None:
            with contextlib.suppress(OSError):
                with open(self.log, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry) + "\n")

    def share(self, state, warning):
        """The session directory's memory.json: what the agent can read about its memory (no names, no PIDs)."""
        if self.session is None:
            return
        data = {"used_bytes": state["current"], "limit_bytes": state["limit"],
                "percent": round(100 * state["current"] / state["limit"], 1), "tmpfs_bytes": state["shmem"],
                "warning": warning, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "advice": ("memory is nearly full: run fewer things at once (tests, builds), remove large files from "
                           "/tmp and HOME (they count as memory)") if warning else ""}
        with contextlib.suppress(OSError):
            target = Path(self.session) / "memory.json"
            temporary = target.with_name(f".memory.json.{os.getpid()}")
            temporary.write_text(json.dumps(data))
            os.replace(temporary, target)

    def tick(self):
        """One look at the cgroup; returns its state (or None before the sandbox's processes exist)."""
        if self.cgroup is None:
            found = self.locate()
            if not found:
                return None
            self.cgroup, self.protected = Path(found[0]), set(found[1])
        try:
            state = memory_state(self.cgroup)
        except (OSError, ValueError):
            self.unthrottle()  # no limit any more (`sandbox-ai limits --memory none`), or the cgroup went
            return None
        if self.first_oom is None:
            self.first_oom = state["oom_kill"]
        self.peak = max(self.peak, state["current"])
        ratio = state["current"] / state["limit"] if state["limit"] else 0
        if ratio >= self.warn and not self.warned:
            self.warned = True
            text = (f"{self.name} uses {ratio:.0%} of its memory ({gib(state['current'])} of {gib(state['limit'])}, "
                    f"files in its /tmp and HOME: {gib(state['shmem'])})")
            self.record("memory-warning", used=state["current"], limit=state["limit"], tmpfs=state["shmem"])
            if self.notify is not None:
                with contextlib.suppress(Exception):
                    self.notify("sandbox-ai: memory nearly full", text)
            self.share(state, True)
        elif ratio < self.warn - 0.05 and self.warned:
            self.warned = False
            self.share(state, False)
        elif self.clock() - self.written >= 10:
            self.share(state, self.warned)
            self.written = self.clock()
            self.watch_supervisor(state)
        self.throttle(state)
        stall = memory_stall(self.cgroup) if ratio >= MEMORY_HIGH - 0.02 else None
        # PSI's avg10 decays over ten seconds: after a relief, a stall alone does not end the next process yet.
        recent = self.ended_at is not None and self.clock() - self.ended_at < MEMORY_STALL_PAUSE
        if ratio >= self.act or (stall is not None and stall >= MEMORY_STALL and not recent):
            if ratio < self.act:
                self.record("memory-stall", used=state["current"], limit=state["limit"], stall=stall)
            self.relieve(state)
        return state

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
            if self.notify is not None:
                with contextlib.suppress(Exception):
                    self.notify("sandbox-ai: supervisor memory",
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
            except ValueError:
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
                oom = memory_state(self.cgroup)["oom_kill"] - (self.first_oom or 0)
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
        return lines


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
        listed = subprocess.run(podman_command(a, "ps", "--filter", f"pod={pod}", "--format", "{{.ID}} {{.IsInfra}}"),
                                capture_output=True, text=True, timeout=10, check=False).stdout.split("\n")
        ids = [line.split()[0] for line in listed if line.strip() and line.split()[-1] == "false"]
        if not ids:
            return None
        pid = subprocess.run(podman_command(a, "inspect", "--format", "{{.State.Pid}}", ids[0]),
                             capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        return int(pid) if pid.isdigit() and int(pid) > 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def start_memory_watch(a, session):
    """The memory watch of this run, or None: only where the launcher sees the sandbox's cgroup (a local engine on
    Linux) and the run has a memory limit."""
    if not local_engine(a) or "memory" not in enforced_limits(a)[0]:
        return None

    def locate():
        pid = podman_agent_pid(a, pod_name(a)) if a.backend == "podman" else getattr(a, "sandbox_pid", None)
        cgroup = cgroup_dir(pid) if pid else None
        return (cgroup, {pid}) if cgroup else None
    log = Path(a.log_dir).expanduser() / "memory.jsonl" if a.log_dir else None

    def notify(title, text):
        load_approvals_module().desktop_notification(title, text)
    return MemoryWatch(locate, getattr(a, "run_id", None) or pod_name(a), session=session, log=log,
                       notify=notify, control=lambda: getattr(a, "control_dir", None)).start()


def run_pod(a, session, home):
    name = pod_name(a)
    create, run = pod_commands(a, name, session, home)
    # Check local image first, before creating an owned pod. Never mutate a
    # pre-existing pod, use host networking, or implicitly fetch images.
    subprocess.run(podman_command(a, "image", "exists", a.image), check=True)
    # Owned unless podman itself refuses the creation (e.g. the name is taken by
    # someone else's pod). An interruption during or right after creation still
    # removes the pod, with no window between creating and owning it.
    owned = True
    try:
        try:
            if not getattr(a, "vpn_pod", None):  # vpn.py created it, with the tunnel in it
                subprocess.run(create, check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            owned = False
            raise
        a.running_pod = name  # a canary's pause (canary.py) freezes the pod
        return wait_child(subprocess.Popen(run))
    finally:
        if owned:
            # --time 0: the infra container's sleep ignores SIGTERM; waiting for it only costs 10 seconds.
            with signals_deferred():
                subprocess.run(podman_command(a, "pod", "rm", "--force", "--time", "0", name), check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        cmd += ["--ro-bind", str(mcp_socket_dir(name)), f"{MCP_UPSTREAM_DIR}/{name}"]
    if hook_mount(a, session):  # the agent's managed configuration: its hooks, which it cannot change
        source, target = hook_mount(a, session)
        cmd += ["--ro-bind", str(source), target]
    for source, target in companion_hook_mounts(a, session):
        cmd += ["--ro-bind", str(source), target]
    if a.gui_wayland:
        runtime = str(PurePosixPath(WAYLAND_TARGET).parent)
        cmd += ["--perms", "0700", "--dir", runtime, "--bind", str(wayland_socket()), WAYLAND_TARGET]
    if getattr(a, "proxy_dir", None):
        cmd += ["--ro-bind", str(a.proxy_dir), str(PROXY_DIR)]
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
            except json.JSONDecodeError:
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
        pid = int(read_ready(info_r, child)["child-pid"])
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
    return module


def load_canary_module():
    spec = importlib.util.spec_from_file_location("sandbox_canary", HERE / "canary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        _devices_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_devices_module)
    return _devices_module


def start_devices(a):
    """The run's device grants in devices.json and its device rules in device-rules.json (both in the run's
    directory, for `sandbox-ai devices` and the watcher); the run record when a watcher is needed (a device on
    USB to follow through unplug and replug, or one held for a question), else None."""
    module = load_devices_module()
    rules = [{"name": source["name"], "version": 1, "defaults": {"devices": source["defaults"]["devices"]},
              "devices": source["devices"]} for source in load_rulesets(a)
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


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("sandbox_mcp", HERE / "mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    values = load_sessions_module().VALUE_OPTIONS[agent]
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-") or arg == "-" or arg in NON_INTERACTIVE_ARGS:
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


def resolve_tmux(a):
    """--tmux/--no-tmux as given; by default tmux for every interactive start from a terminal, but not
    for probes, sessions (tmux already), MCP servers, other binaries, commands that print and exit, or
    bwrap without a host tmux."""
    if a.tmux is None:
        a.tmux = bool(not (a.probe or a.session is not None or a.tmux_socket_dir or a.mcp_socket_dir
                           or a.binary) and sys.stdin.isatty() and sys.stdout.isatty()
                      and interactive_args(a.agent, a.agent_args) and (a.backend == "podman" or shutil.which("tmux"))
                      # macOS: attaching reaches the VM's socket only in stage 2 (TODO.md); --tmux still asks for it
                      and sys.platform != "darwin")
    return a.tmux


def main(argv):
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return launch(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


_presets_module = None


def load_presets_module():
    global _presets_module
    if _presets_module is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_presets", HERE / "presets.py")
        _presets_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_presets_module)
        _presets_module.EXPOSED_BEFORE = exposed_before  # where sandboxes wrote: no preset directory there
    return _presets_module


def launch(argv):
    # --preset NAME: its options right after the agent (the command line wins), its first task as the agent's
    # arguments; then everything is checked as if typed.
    argv, preset_mode = load_presets_module().apply(canonical_options(list(argv)), canonical=canonical_options,
                                                    with_mode=True)
    argv = canonical_options(argv)
    if preset_mode:
        MODES.check_name(preset_mode, "the preset's --mode")
    a = parse_args(argv)
    config_path, document = load_project_config(a)
    merged = merge_config(argv, a, document) if document else argv
    if document:
        a = parse_args(merged)
    user_defaults, policy = load_user_defaults(a), load_policy()
    # The security mode's defaults join the options (modes.py); medium, the default, adds none.
    a = MODES.apply(launcher_namespace(), merged, a, document, config_path, user_defaults, policy)
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
        return launch([argv[0], "--mode", floor, *rest])
    # The checked document is passed on; nothing re-reads the file later.
    a.project_config, a.project_document = (str(config_path) if config_path else None), document
    apply_subnet_defaults(a, user_defaults, str(defaults_file()))
    a.canary_default = user_defaults.get("canaries")  # canary.categories(): after the options and the project file
    a.telemetry_default = user_defaults.get("telemetry")  # telemetry_level(): after the option
    # The administrator's policy binds every way of choosing an option, the project file's and the mode's included.
    check_policy(a, policy)
    # Options may narrow the mode, never widen it; then what it enforces beyond options (approval scope ...).
    MODES.check(launcher_namespace(), a, policy)
    # What you approved "always" for this project joins the options before they are checked.
    remembered = apply_remembered_approvals(a) if not a.offline and a.max_approval_scope == "always" else []
    for name in expire_kept(a, user_defaults):
        print(f"agent-sandbox: removed the kept start {printable(name)}: unused for more than "
              f"{user_defaults['keep_days']} days (keep_days in {defaults_file()})", file=sys.stderr)
    resolve_tmux(a)
    a.start_name = name_start(a)
    notes = [note for note in [resolve_keep(a)] if note]
    plan_kept_home(a)
    validate(a)
    check_image_variant(a)
    if not a.dry_run:
        remember_exposed(a)
    for note in remembered + notes:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    a.run_id = (a.pod_name[len("agent-"):] if a.pod_name else f"{a.agent}-{uuid.uuid4().hex[:8]}")
    check_login(a)
    for note in git_credentials_file(a)[1] + ([a.llm_vpn_note] if getattr(a, "llm_vpn_note", None) else []) + \
            registry_git_tokens(a)[1]:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in device_notes(a):
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    for note in enforced_limits(a)[1]:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    if a.backend == "bwrap" and resource_limits(a) and not limits_prefix(a):
        print("agent-sandbox: the resource limits are not enforced with bwrap here (no systemd user manager)",
              file=sys.stderr)
    if (a.tmux and a.session is None and not (a.probe or a.tmux_socket_dir or a.mcp_socket_dir or a.dry_run)
            and not sys.stdin.isatty()):
        raise ValueError("--tmux needs an interactive terminal")  # a session may start detached, without one
    if detachable(a) and not a.dry_run:
        if a.backend == "bwrap" and not shutil.which("tmux"):
            raise ValueError("Required tool missing: tmux")
        # A background supervisor runs the sandbox; this terminal attaches (and may detach).
        return load_sessions_module().start(a, argv)
    if a.dry_run:
        print(json.dumps({"agent": a.agent, "backend": a.backend, "mode": MODES.plan(a),
                          "workspace": str(Path(a.workspace).resolve()),
                          "workspaces": [{"host": src, "sandbox": dest} for src, dest in workspace_targets(a)],
                          "workspace_mode": "ro" if a.workspace_ro else "rw", "mounts": a.mount,
                          "devices": [{"path": path, "kind": kind} for path, kind in devices(a)], "gpu": a.gpu,
                          "device_grants": [{**{key: grant[key] for key in ("value", "by", "state", "decision",
                                                                            "rule", "ruleset", "track")},
                                             "nodes": list(grant["nodes"] or grant.get("pending") or [])}
                                            for grant in device_grants(a)],
                          "limits": enforced_limits(a)[0],
                          "allow": a.allow, "https_hosts": allowed_hosts(a), "host_wildcards": host_wildcards(a),
                          "https_networks": host_networks(allowed_hosts(a)), "offline": a.offline,
                          "dangerously_skip_permissions": a.dangerously_skip_permissions,
                          "egress_all_allow": a.egress_all_allow, "nameservers": nameservers(a),
                          "log_dir": str(a.log_dir.expanduser().resolve()) if a.log_dir else None,
                          "gui": gui_config(a), "audio": getattr(a, "audio", None), "gui_wayland": a.gui_wayland,
                          "tmux": a.tmux, "session": a.session,
                          "name": a.start_name, "project_name": project_label(a), "detachable": detachable(a),
                          "keep": a.keep_home,
                          "allow_tcp": [f"{addr}:{port}" for addr, port in tcp_targets(a)],
                          "host_ports": host_ports(a),
                          "network_profiles": a.profile,
                          "git_identity": a.git_identity,
                          "privacy_check": a.privacy_check,
                          "proxy": {"enabled": proxy_enabled(a), "no_inspect": a.no_inspect,
                                    "log_dir": str(Path(a.log_dir).expanduser()) if proxy_enabled(a) else None},
                          "privacy_rules": str(privacy_rules_dir(a)) if privacy_rules_dir(a) else None,
                          "log_debug": a.log_debug,
                          "canaries": load_canary_module().Plan(a, "/root", argparse.Namespace(**globals())).plan(),
                          "telemetry": telemetry_plan(a),
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
                          "mcp": [{"name": name, "socket": str(mcp_socket_dir(name) / "server.sock")}
                                  for name in dict.fromkeys(a.mcp)],
                          "project_config": a.project_config,
                          "kits": kit_plan(a),
                          "network": a.network, "ip": a.ip, "ip6": a.ip6, "image": a.image,
                          "sandbox_network": sandbox_network(a),
                          "vpn": load_vpn_module().plan(a) if a.vpn else None,
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
                          "agent_args": a.agent_args,
                          "firejail_required": True}, indent=2))
        return 0
    if os.geteuid() == 0:
        raise ValueError("Run as an ordinary host user; both backends use a rootless user namespace")
    required = ["podman"] if a.backend == "podman" else ["bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap"] + ([] if a.offline else ["slirp4netns"])
    if (a.tmux or a.tmux_socket_dir) and not a.probe and a.backend == "bwrap":
        required.append("tmux")
    for tool in required:
        if not shutil.which(tool):
            raise ValueError(f"Required tool missing: {tool}")
    if a.backend == "bwrap" and not a.dry_run and (any(os.isatty(fd) for fd in (0, 1, 2))
                                                   or has_controlling_terminal()):
        check_tiocsti()
    binary = a.binary or a.agent
    if a.backend == "bwrap" and not a.probe:
        found = shutil.which(binary)
        if not found:
            raise ValueError(f"Agent executable missing: {binary}")
        binary = str(Path(found).resolve(strict=True))
        if not binary.startswith("/usr/"):
            a.runtime.append(str(Path(binary).parent))
        # The agents --with, and the agent a helper's server runs for each task: the host's, like the lead's.
        for agent in [*companions(a), *([a.agent] if getattr(a, "helper_of", None) else [])]:
            found = shutil.which(agent)
            if not found:
                raise ValueError(f"Agent executable missing: {agent} "
                                 f"({'the helper' if agent == a.agent else '--with ' + agent})")
            where = str(Path(found).resolve(strict=True).parent)
            if not where.startswith("/usr/") and where not in a.runtime:
                a.runtime.append(where)
    for workspace in a.workspaces:
        prepare_workspace(workspace)
    for private in (a.log_dir, a.gui_vnc, a.tmux_socket_dir, a.mcp_socket_dir, a.mcp_approvals_dir):
        if private:
            private.expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)
            private.expanduser().chmod(0o700)
    if a.gui_wayland:
        print("agent-sandbox: warning: --gui-wayland gives the agent access to your Wayland compositor "
              "(its windows appear on your desktop).", file=sys.stderr)
    if a.mcp and not a.probe:
        # Each MCP server runs in its own sandbox and outlives this one.
        mcp = load_mcp_module()
        for name in dict.fromkeys(a.mcp):
            mcp.ensure_running(name, a)
    hosts = None
    remembered = [name for name in getattr(a, "remembered_hosts", []) if name in allowed_hosts(a)]
    if not a.vpn:  # with --vpn the names are resolved through the tunnel, once it is up (below)
        resolve = (lambda names: resolve_pod_hosts(a, names)) if a.backend == "podman" else resolve_hosts
        hosts = resolve([name for name in allowed_hosts(a) if name not in remembered])
        hosts.update(apply_remembered_pins(a, remembered, resolve))
        settle_network(a, hosts, resolve)
    with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as tmp, contextlib.ExitStack() as cleanup:
        session = Path(tmp)
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
            names = allowed_hosts(a)
            extra = [name for name in (load_llm_module().pinned_names(a) if a.llm else []) if name not in names]
            found = load_vpn_module().agent_up(a, session, home, names + extra, cleanup)
            hosts = {name: found[name] for name in names if name not in remembered}
            hosts.update(apply_remembered_pins(a, remembered, lambda wanted: {n: found[n] for n in wanted if n in found}))
            settle_network(a, hosts, lambda wanted: {name: found[name] for name in wanted})
        for source, dest in prepare_home(a, home):
            print(f"agent-sandbox: auth copied: {Path(source).expanduser()} -> /root/{dest}", file=sys.stderr)
        a.kit_mounts, notes, memory = prepare_kits(a, home, session / "kits")
        for note in notes:
            print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
        a.canary_plan = plant_canaries(a, home, cleanup)  # after the auth copies and kits: never over them
        pair = claude_credential_pair(a, home)
        writeback = claude_writeback_state(pair) if pair and not a.probe else None
        stored = [state for state in (store_writeback_state(a, home, agent) for agent in team(a)) if state]
        if a.agent == "claude" and auth_mode(a) != "none" and (home / AUTH_TARGETS["claude"]).exists():
            print(f"agent-sandbox: {claude_token_status(home / AUTH_TARGETS['claude'])}", file=sys.stderr)
        for port in host_ports(a):
            host = "the Podman machine at --podman-url" if a.podman_url else "the host"
            print(f"agent-sandbox: 127.0.0.1:{port} in the sandbox reaches port {port} on the loopback of {host}; "
                  "the sandbox cannot listen on that port itself", file=sys.stderr)
        log_name = None
        if proxy_enabled(a):
            for name in rotate_connection_logs(a.log_dir.expanduser()):
                print(f"agent-sandbox: proxy: deleted the old connection log {name} (logs over "
                      f"{CONNECTION_LOGS_LIMIT >> 30} GiB)", file=sys.stderr)
            log_name = f"connections-{time.strftime('%Y%m%d-%H%M%S')}-{a.run_id}.jsonl"
            a.proxy_dir = Path(tempfile.mkdtemp(prefix="agent-sandbox-proxy-"))
            cleanup.callback(shutil.rmtree, a.proxy_dir, True)
            write_proxy_files(a, a.proxy_dir, hosts, log_name)
        a.sandbox_home = home  # run.json: a workspace added later must not overlap it
        a.control_dir = register_run(a, a.run_id, session)
        cleanup.callback(shutil.rmtree, a.control_dir, True)
        if a.canary_plan:
            a.canary_plan.arm(a.control_dir, a.log_dir)
            cleanup.callback(a.canary_plan.drain)  # before the control directory goes: what is left is read
        device_watch = start_devices(a)
        history_event(a, "start")
        if asking(a):
            a.browser_dir = a.control_dir.with_name(a.control_dir.name + ".browser")
            a.browser_dir.mkdir(mode=0o700, exist_ok=True)
            cleanup.callback(shutil.rmtree, a.browser_dir, True)
            write_browser_shims(session / "bin")
        mode = a.runtime_mode
        cfg = {"command": agent_command(a, binary), "env": agent_environment(a), "probe": a.probe,
               # Who this sandbox is, for the process attribution in its logs and questions.
               "sandbox": sandbox_identity(a), "log_debug": a.log_debug,
               # The supervisor relays each MCP server for this run: the rules for tainted runs, untrusted tools.
               "mcp_relay": {name: mcp_relay_config(a, name) for name in dict.fromkeys(a.mcp)},
               # The rules for the agent's own tools, answered to its hooks (None: no hooks this run).
               "hooks": hooks_config(a),
               # The agent whose managed hooks (rule hooks, approved kit hooks) must hold: its version is checked.
               "hook_agent": a.agent if managed_hooks(a) else None,
               # A helper's command is its server (python3): the agent it runs is checked by its name.
               "hook_binary": a.agent if getattr(a, "helper_of", None) else None,
               "companion_hook_agents": list(companions(a)) if hooks_active(a) else [],
               # Values the supervisor's process log redacts by value (--env and --secret-env).
               "secret_names": sorted(set(a.env) | {v.partition("=")[0] for v in a.secret_env}),
               "tmux": "detached" if a.tmux_socket_dir else a.tmux,
               "proxy": {"log": log_name} if log_name else None,
               "ask": asking(a), "ask_quiet": list(ASK_QUIET),
               "ask_max_scope": getattr(a, "max_approval_scope", "always"),
               # A strict security mode, which the supervisor holds run-time changes to (modes.py).
               **({"mode": mode} if mode["refuses"] or mode["approval_scope"] != "always" else {}),
               "ask_blocked": getattr(a, "blocked_approvals", {"hosts": [], "tcp": []}),
               "policy": getattr(a, "policy", {}),  # the supervisor holds run-time changes to it too
               "pinned": [*hosts, *a.llm_pins], "wildcards": host_wildcards(a), "watch": [*(dest for _, dest in workspace_targets(a)), "/root", "/tmp",
                         *(parse_mount(m)[1] for m in a.mount)] if a.log_explicit else []}
        resolv, nsswitch = resolver_files(a)
        files = {"config.json": json.dumps(cfg), "hosts": hosts_file({**hosts, **a.llm_pins}),
                 "resolv.conf": resolv, "nsswitch.conf": nsswitch,
                 "passwd": "root:x:0:0:Sandbox:/root:/bin/bash\n", "group": "root:x:0:\n",
                 "firewall.nft": firewall_rules(a.allow, [addr for values in hosts.values() for addr in values]
                                                + host_networks(hosts), log=bool(a.log_dir),
                                                dns=nameservers(a), allow_all=a.egress_all_allow,
                                                tcp=tcp_targets(a),
                                                proxy={"port": PROXY_PORT, "loopback": host_ports(a)}
                                                if log_name else None, vpn_mark=getattr(a, "vpn_mark", None),
                                                vpn_rules=getattr(a, "vpn_firewall", ()))}
        if gui_config(a):
            files["gui.json"] = json.dumps(gui_config(a))
        if getattr(a, "audio", None) == "virtual":
            files["audio.json"] = json.dumps({"kind": "virtual"})
        npmrc = load_publish_logins_module().npmrc(publish_kinds(a), publish_logins_of(a)) \
            if getattr(a, "publish", None) else None
        if npmrc:
            files["npmrc"] = npmrc
        if managed_hooks(a):
            files.update(hook_files(a))
        for name, content in files.items():
            path = session / name
            path.parent.mkdir(mode=0o755, exist_ok=True)
            path.write_text(content)
            path.chmod(0o600 if name == "config.json" else 0o644)
        privacy_after_run = prepare_privacy(a, session)
        watching = threading.Event()
        if device_watch:
            threading.Thread(target=load_devices_module().watch_devices, args=(device_watch, watching),
                             daemon=True).start()
        if asking(a) or (device_watch and devices(a, held=True)):
            # The agent's terminal is busy with the agent: questions are announced on the desktop.
            approvals = load_approvals_module()
            (a.control_dir / "approvals").mkdir(mode=0o700, exist_ok=True)
            threading.Thread(target=approvals.watch, args=(a.control_dir / "approvals", watching),
                             daemon=True).start()
        if asking(a):
            threading.Thread(target=approvals.serve_browser, args=(a.browser_dir / "open.sock",
                                                                   a.control_dir / "approvals", watching),
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
        # Not "memory": that name holds the project memory (save_memory below).
        memory_watch = start_memory_watch(a, session)
        try:
            status = run_pod(a, session, home) if a.backend == "podman" else run_bwrap(a, session, home)
            return status
        finally:
            watching.set()
            # Each step on its own: whatever the agent left behind must never skip the login write-back, the
            # memory record or the privacy check after them.
            try:
                for line in memory_watch.stop() if memory_watch is not None else []:
                    print(f"agent-sandbox: {printable(line)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"agent-sandbox: memory watch: {printable(str(exc))}", file=sys.stderr)
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
            if log_name:
                for line in connection_summary(a.log_dir.expanduser() / log_name):
                    print(f"agent-sandbox: {printable(line)}", file=sys.stderr)
            if activity is not None:  # last (a signal here skips no other cleanup); before the HOME goes
                activity.finish(status)
            ended = sys.exc_info()[1]
            history_event(a, "exit", status if status is not None else
                          ended.code if isinstance(ended, SystemExit) and isinstance(ended.code, int) else None)
            if getattr(a, "foreground_start", False):
                print(f"agent-sandbox: {printable(a.start_name)} keeps its HOME; continue it with: sandbox-ai resume "
                      f"{printable(a.start_name)} (or remove it: sandbox-ai delete {printable(a.start_name)} --yes)",
                      file=sys.stderr)


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
        # A child killed by signal N reports -N; exit like a shell would (128 + N).
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
