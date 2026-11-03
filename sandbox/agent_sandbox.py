#!/usr/bin/env python3
"""Rootless agent launchers; standard library only. See SANDBOX.md."""
import argparse
import base64
import errno
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
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
}
# Published, stable ingress ranges (TCP/443 only). They keep the API reachable
# when DNS rotates after launch. OpenAI publishes none; it stays DNS-pinned.
HOST_NETWORKS = {"api.anthropic.com": ["160.79.104.0/23", "2607:6bc0::/48"]}
# Per-agent switches for --dangerously-skip-permissions.
DANGER_ARGS = {"codex": ["--dangerously-bypass-approvals-and-sandbox"],
               "claude": ["--dangerously-skip-permissions"], "opencode": []}
DANGER_ENV = {"codex": {}, "claude": {"IS_SANDBOX": "1"},
              "opencode": {"OPENCODE_PERMISSION": '{"*":"allow"}'}}
# Written into a sandbox HOME's ~/.claude/settings.json where not already set.
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_DEFAULT_EFFORT = "high"
AUTH_TARGETS = {"codex": ".codex/auth.json", "claude": ".claude/.credentials.json",
                "opencode": ".local/share/opencode/auth.json"}
CAPS = ["SYS_ADMIN", "NET_ADMIN", "SETUID", "SETGID", "SYS_CHROOT",
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETPCAP"]
# pasta without gateway mapping or any port forwarding (-t/-u inbound, -T/-U to the host).
PASTA_OPTIONS = ("--no-map-gw", "-t", "none", "-u", "none", "-T", "none", "-U", "none")
POD_NETWORK = "pasta:" + ",".join(PASTA_OPTIONS)
SESSION_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"
__version__ = "0.1.18"
CONFIG_NAME = ".sandbox-ai.json"
# Label on session pods: the session's tmux directory, so a stale pod can be told apart from someone else's.
SESSION_LABEL = "sandbox-ai.session"
CONFIG_KEYS = {"version", "agent", "options", "agent_args", "workspaces", "description", "mcp_servers"}
MCP_NAME = r"[A-Za-z0-9_-]{1,32}"
MCP_AGENT_DIR = "/sandbox/mcp"        # agent side: one read-only socket directory per server
MCP_SERVE_DIR = "/sandbox/mcp-sock"   # server side: where the server sandbox listens
# Options a project file may not set: internal ones, config selection itself,
# workspace order (main workspace = where the file lives) and sessions.
CONFIG_FORBIDDEN = ("--config", "--no-config", "--tmux-socket-dir", "--pod-name", "--mcp-socket-dir", "--workspace",
                    "--session", "--dry-run", "--")
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


def build_parser():
    """The launcher's option parser (also used by the wizard to split option lists)."""
    # No prefix matching: abbreviations must not reach internal or forbidden options.
    p = argparse.ArgumentParser(description="Agent in rootless Podman/Bubblewrap + verified Firejail.",
                                epilog="Agent arguments follow --. Examples: see SANDBOX.md.", allow_abbrev=False)
    p.add_argument("agent", choices=AUTH_TARGETS)
    p.add_argument("--version", action="version", version=f"sandbox-ai {__version__}")
    p.add_argument("--backend", choices=["podman", "bwrap"], default="podman")
    p.add_argument("--workspace", action="append", metavar="DIR[,DIR...]",
                   help="Host directory at /work (default: current directory); further entries, repeated or "
                        "comma-separated, appear at /workspaces/<name>")
    p.add_argument("--workspace-ro", action="store_true", help="Mount every workspace read-only")
    p.add_argument("--mount", action="append", default=[], metavar="SRC:DEST:ro|rw", help="Repeatable; DEST under /work, /mnt or /opt")
    p.add_argument("--profile", action="append", default=[], choices=PROFILES, metavar="NAME",
                   help="Network profile: " + "; ".join(f"{name}: {value['help']}" for name, value in PROFILES.items())
                        + "; repeatable")
    p.add_argument("--allow", action="append", default=[], metavar="IP_OR_CIDR", help="Allow destination on all IP protocols/ports; repeatable")
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST", help="Pin hostname and allow TCP/443; repeatable")
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
                        "http://localhost:3000=GITEA_TOKEN or https://github.com=GH_PAT (user as "
                        "http://USER@HOST:PORT); loopback URLs imply --allow-host-port, github.com --allow-github "
                        "and sets GH_TOKEN, other URLs --allow-host; repeatable")
    p.add_argument("--git-token-file", action="append", default=[], metavar="URL=FILE",
                   help="Like --git-token-env, but the token is read from FILE (yours, mode 0600, outside every "
                        "workspace); repeatable")
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
    p.add_argument("--no-provider-defaults", action="store_true")
    p.add_argument("--frontier", action="store_true", help="Also allow OpenAI and Anthropic API/auth hosts (TCP/443); everything else stays denied")
    p.add_argument("--dangerously-skip-permissions", action="store_true",
                   help="Start the agent without approval prompts; the outer sandbox and egress firewall stay in force")
    p.add_argument("--local-config", type=Path, help="OpenCode local-only config: copy privately and disable default cloud access")
    p.add_argument("--offline", action="store_true", help="No outside network, including provider defaults")
    p.add_argument("--egress-all-allow", action="store_true",
                   help="Allow all outbound traffic (implies --dns); inbound stays closed")
    p.add_argument("--dns", action="store_true",
                   help=f"Real DNS via {' and '.join(DEFAULT_NAMESERVERS)} (UDP/TCP 53 to them only)")
    p.add_argument("--nameserver", action="append", default=[], metavar="IP",
                   help="Use this resolver instead of the defaults; repeatable, implies --dns")
    p.add_argument("--network", help="Existing Podman network (default: private rootless pasta)")
    p.add_argument("--ip", help="Static IPv4 address in --network")
    p.add_argument("--ip6", help="Static IPv6 address in --network")
    p.add_argument("--image", help="Prebuilt Podman image (default: localhost/<agent>-sandbox:latest); never pulled implicitly")
    p.add_argument("--podman-url", help="Podman service URL, e.g. unix:///example/podman.sock")
    p.add_argument("--binary", help="Agent executable (host path with bwrap; image path with podman)")
    p.add_argument("--runtime", action="append", default=[], metavar="DIR", help="bwrap: expose runtime directory read-only at same path")
    p.add_argument("--copy-auth", action="store_true",
                   help="Copy this agent's JSON credential files; they must exist on the host. A login already "
                        "in a reused sandbox HOME (session, --state-dir) is kept. Without any auth option, the "
                        "host's files are copied automatically when present and the sandbox has none yet")
    p.add_argument("--no-auth", action="store_true", help="Disable automatic copying of host credentials")
    p.add_argument("--auth-file", type=Path, help="Copy this JSON file to agent's standard auth path")
    p.add_argument("--import-codex-auth", type=Path, help="OpenCode only: import a Codex ChatGPT auth.json as a private OAuth copy")
    p.add_argument("--copy", action="append", default=[], metavar="SRC:HOME_RELATIVE_DEST", help="Copy individual config/credential file; repeatable")
    p.add_argument("--state-dir", type=Path, help="Private persistent sandbox home; default: deleted after exit")
    p.add_argument("--kit", action="append", default=[], metavar="NAME",
                   help="Also use the kit ~/.local/share/sandbox-ai/kits/NAME/AGENT (skills, agents, commands, "
                        "prompts, memory template, marketplaces.json); repeatable")
    p.add_argument("--kit-dir", action="append", default=[], metavar="DIR",
                   help="Also use the kit in DIR/AGENT; repeatable")
    p.add_argument("--no-default-kit", action="store_true",
                   help="Leave out the default kit ~/.local/share/sandbox-ai/home/AGENT")
    p.add_argument("--no-memory", action="store_true",
                   help="No project memory: the agent's memory file and Claude's auto memory are neither "
                        "loaded nor kept")
    p.add_argument("--no-plugins", action="store_true",
                   help="No plugins from sandbox-ai's plugin store (Claude) or the kits (OpenCode)")
    p.add_argument("--env", action="append", default=[], metavar="NAME", help="Pass named host variable to agent only; values never printed")
    p.add_argument("--log-dir", type=Path, help="Write egress-denied.jsonl and files.jsonl here (outside workspace; hidden from the agent)")
    p.add_argument("--gui", action="store_true", help=f"Virtual X display {GUI_DISPLAY} (Xvfb) for GUI apps and headed browsers")
    p.add_argument("--gui-screen", default="1920x1080x24", metavar="WxHxDEPTH", help="Virtual screen size (default: 1920x1080x24)")
    p.add_argument("--gui-vnc", type=Path, metavar="DIR",
                   help="Implies --gui; serve the display over VNC on the Unix socket DIR/vnc.sock (outside workspaces)")
    p.add_argument("--gui-wayland", action="store_true",
                   help="Expose the host's Wayland socket: windows appear on your desktop (weakens isolation)")
    p.add_argument("--tmux", action="store_const", const=True, default=None,
                   help="Run in tmux inside the sandbox: window 'agent' plus window 'shell' (bash). The default "
                        "whenever stdin and stdout are a terminal and the agent runs interactively; --tmux insists "
                        "(and fails without a terminal)")
    p.add_argument("--no-tmux", action="store_const", const=False, dest="tmux", help="Run the agent without tmux")
    p.add_argument("--mcp", action="append", default=[], metavar="NAME",
                   help="Connect a local MCP server running in its own sandbox; started if not running "
                        "(see sandbox-mcp list; built in: demo)")
    p.add_argument("--session", metavar="NAME",
                   help="Named, detachable session in tmux; manage with sandbox-session (list/attach/pause/resume/stop)")
    # Internal: set by the session supervisor, not meant for direct use.
    p.add_argument("--tmux-socket-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--pod-name", help=argparse.SUPPRESS)
    p.add_argument("--mcp-socket-dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--config", type=Path, metavar="FILE",
                   help=f"Project configuration (default: {CONFIG_NAME} in the main workspace, if trusted)")
    p.add_argument("--no-config", action="store_true", help=f"Ignore {CONFIG_NAME}")
    p.add_argument("--dry-run", action="store_true", help="Print plan; no DNS, auth reads, copies, pods or processes")
    p.add_argument("--probe", action="store_true", help="Verify both sandbox layers and firewall, then exit without agent")
    return p


def parse_args(argv):
    # The separator belongs to this wrapper; everything afterwards is opaque.
    tail = []
    if "--" in argv:
        pos = argv.index("--")
        argv, tail = argv[:pos], argv[pos + 1:]
    a = build_parser().parse_args(argv)
    # Commas are invalid in workspace paths, so they can separate entries.
    a.workspaces = [part for value in (a.workspace or [os.getcwd()]) for part in value.split(",")]
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
    if a.image is None:
        a.image = f"localhost/{a.agent}-sandbox:latest"
    a.agent_args = tail
    return a


def allowed_hosts(a):
    if a.offline:
        return []
    defaults = PROVIDERS[a.agent]
    return list(dict.fromkeys(([] if a.no_provider_defaults or a.local_config else defaults)
                              + (FRONTIER if a.frontier else [])
                              + [host for preset in a.presets for host in PRESETS[preset]] + a.allow_host))


def host_ports(a):
    """Sorted unique ports from --allow-host-port."""
    ports = set()
    for value in a.allow_host_port:
        if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
            raise ValueError(f"--allow-host-port expects a TCP port 1-65535: {value!r}")
        ports.add(int(value))
    return sorted(ports)


def pod_network(a):
    """Podman --network value; host ports are pasta's namespace-to-host forwards (-T)."""
    if a.offline:
        return "none"
    if a.network:
        return a.network
    ports = host_ports(a)
    options = list(PASTA_OPTIONS)
    if ports:
        index = options.index("-T")
        options[index:index + 2] = [part for port in ports for part in ("-T", str(port))]
    return "pasta:" + ",".join(options)


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
    """[(kind, URL=REF)] of --git-token-env (kind "env") and --git-token-file (kind "file")."""
    return [("env", value) for value in a.git_token_env] + [("file", value) for value in a.git_token_file]


def git_token_credential(value, kind="env"):
    """Parse URL=NAME (--git-token-env) or URL=FILE (--git-token-file); the token is read by git_tokens()."""
    option = f"--git-token-{kind}"
    # The URL cannot contain "=", so the first one separates it from the name or path.
    url, sep, ref = value.partition("=")
    if kind == "env" and (not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref)):
        raise ValueError(f"{option} expects URL=NAME, a variable NAME without a value: {value!r}")
    if kind == "file" and (not sep or not ref or any(ord(c) < 32 for c in ref)):
        raise ValueError(f"{option} expects URL=FILE: {value!r}")
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
        if kind == "file":
            token = read_token_file(credential["file"], GIT_TOKEN, "--git-token-file")
        else:
            name = credential["env"]
            if name not in os.environ:
                raise ValueError(f"Host variable is not set: {name}")
            if not re.fullmatch(GIT_TOKEN, os.environ[name]):
                raise ValueError(f"{name} does not look like an access token "
                                 "(one line of letters, digits and _.~+/=-)")
            token = os.environ[name]
        result.append((credential, token))
    return result + git_credentials_file(a)[0]


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
    for index, (credential, token) in enumerate(git_tokens(a)):
        variable, user = f"SANDBOX_GIT_TOKEN_{index}", f"SANDBOX_GIT_USER_{index}"
        key = f"credential.{credential['url']}.helper"
        # The empty value drops helpers from config files (e.g. one the agent planted) for this URL first.
        entries += [(key, ""), (key, git_credential_helper(user, variable))]
        env[variable], env[user] = token, credential["user"]
        if credential["url"] == "https://github.com":
            env["GH_TOKEN"] = token  # the gh CLI
    if privacy_hooks_apply(a):
        entries.append(("core.hooksPath", PRIVACY_HOOKS))
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
    trusted = trusted_configs()
    if not path.is_symlink() and not path.exists():
        if a.config:
            raise ValueError(f"Configuration not found: {path}")
        if str(path) in trusted:
            # Deleting a restrictive file must not silently drop its restrictions.
            raise ValueError(f"The trusted project configuration {path} is missing; restore it, start with "
                             f"--no-config, or run: sandbox-wizard forget {shlex.quote(str(path))}")
        return None, None
    # Hash and parse the very same bytes: no window to swap the file in between.
    data = read_config_bytes(path)
    known = trusted.get(str(path))
    if known != hashlib.sha256(data).hexdigest():
        state = "changed since it was trusted" if known else "not trusted"
        raise ValueError(f"Project configuration {path} is {state}. Review it, then run: "
                         f"sandbox-wizard trust {shlex.quote(str(path))} (or start with --no-config)")
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
    for value in document.get("options", []):
        if any(value == flag or value.startswith(flag + "=") for flag in CONFIG_FORBIDDEN):
            raise ValueError(f"{path}: option {value} is not allowed in a project configuration")
    # Relative paths would resolve against wherever the launcher is started, not the file.
    relative = relative_paths(document.get("options", [])) + [
        w for w in document.get("workspaces", []) if not os.path.isabs(os.path.expanduser(w))]
    if relative:
        raise ValueError(f"{path}: use absolute paths (or ~/...), not: {', '.join(relative)}")
    if any("," in value for value in document.get("workspaces", [])):
        raise ValueError(f"{path}: workspaces cannot contain commas")
    return path, document


# Options with a host path; the function returns the path part of the value.
PATH_OPTIONS = {"--mount": lambda v: v.rsplit(":", 2)[0], "--copy": lambda v: v.rsplit(":", 1)[0],
                "--git-token-file": lambda v: v.partition("=")[2], "--local-config": str, "--auth-file": str,
                "--import-codex-auth": str, "--state-dir": str, "--log-dir": str, "--gui-vnc": str,
                "--git-credentials": str, "--runtime": str, "--workspace": str, "--kit-dir": str,
                "--privacy-rules": str}


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
    file_options = document.get("options", [])
    if any(value.split("=", 1)[0] in AUTH_OPTIONS for value in kept):
        filtered, skip = [], False
        for value in file_options:
            name = value.split("=", 1)[0]
            if skip:
                skip = False
            elif name in AUTH_OPTIONS:
                skip = AUTH_OPTIONS[name] and "=" not in value
            else:
                filtered.append(value)
        file_options = filtered
    return [argv[0], *file_options, *kept, "--workspace", ",".join(workspaces),
            "--", *(document.get("agent_args", []) if agent_args is None else agent_args)]


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
        return [value for name in names for value in
                ("-c", f'mcp_servers.{name}.command="python3"', "-c", f"mcp_servers.{name}.args={json.dumps(mcp_bridge_args(name))}")]
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
    # Socket files are never accepted as individual mounts. Directory contents
    # remain the operator's responsibility (see documentation).
    safe_dest(dest)
    if any(c in str(source) + dest for c in (",", "\n", "\r", "\0")):
        raise ValueError("Mount paths cannot contain commas or control characters")
    return str(source), dest, mode


def validate(a):
    if a.offline and (git_token_values(a) or a.git_credentials):
        raise ValueError("Git tokens need network access to their server; drop --offline (or --profile offline)")
    if a.offline and (a.allow or a.allow_host or a.allow_tcp or a.allow_host_port or a.presets or a.frontier
                      or a.network or a.ip or a.ip6 or a.dns or a.nameserver or a.egress_all_allow):
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
    if a.backend == "podman" and a.runtime:
        raise ValueError("--runtime applies only to bwrap; install runtimes in the image")
    if sum(bool(value) for value in (a.copy_auth, a.auth_file, a.import_codex_auth, a.no_auth)) > 1:
        raise ValueError("Choose one of --copy-auth, --auth-file, --import-codex-auth or --no-auth")
    auth_targets = set(AUTH_TARGETS.values()) | {".claude.json"}
    if a.no_auth and any(":" in v and str(PurePosixPath(v.rsplit(":", 1)[1])) in auth_targets for v in a.copy):
        raise ValueError("--no-auth conflicts with --copy of a credential file")
    if a.import_codex_auth and a.agent != "opencode":
        raise ValueError("--import-codex-auth is supported only for OpenCode")
    for name in a.env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("--env expects a variable NAME, without a value")
        if name not in os.environ:
            raise ValueError(f"Host variable is not set: {name}")
        if name in ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            raise ValueError(f"Sandbox-owned variable cannot be overridden: {name}")
    for mount in a.mount:
        parse_mount(mount)
    for name in a.kit:
        load_kits_module().kit_path(name, a.agent)
    for value in a.copy:
        if ":" in value:
            dest = PurePosixPath(os.path.normpath(value.rsplit(":", 1)[1]))
            for managed in kit_managed_paths(a):
                if dest == managed or managed in dest.parents:
                    raise ValueError(f"--copy {dest}: sandbox-ai fills {managed} from the kits or the project "
                                     "memory; put the file into a kit (or start with --no-default-kit/--no-memory)")
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
    # Inside an agent-visible tree the Firejail blacklist would not hide them.
    for path, option in [(a.log_dir, "--log-dir"), (a.gui_vnc, "--gui-vnc"), (a.tmux_socket_dir, "--tmux-socket-dir"),
                         (a.mcp_socket_dir, "--mcp-socket-dir")]:
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


def check_host_sources(a):
    """Every host path the launcher reads, mounts or writes back to lies outside agent-writable trees."""
    if a.copy_auth or auth_mode(a) == "auto":
        # CODEX_HOME, CLAUDE_CONFIG_DIR or XDG_* inside a workspace would let the agent plant a
        # symlink there that we would follow; Claude's file is also the write-back target.
        for source in auth_source_candidates(a.agent):
            check_host_source(a, source, "Credential file (from CODEX_HOME, CLAUDE_CONFIG_DIR or XDG_DATA_HOME)")
    if a.agent == "opencode":
        check_host_source(a, opencode_catalog(), "OpenCode model catalog (from XDG_CACHE_HOME)")
    for path, option in [(a.local_config, "--local-config"), (a.auth_file, "--auth-file"),
                         (a.import_codex_auth, "--import-codex-auth")]:
        if path:
            check_host_source(a, path, option)
    for value in a.git_token_file:
        # A token file the agent could replace would hand it any host file as a "token".
        check_host_source(a, git_token_credential(value, "file")["file"], "--git-token-file")
    if a.git_credentials:
        check_host_source(a, a.git_credentials, "--git-credentials")
    if privacy_rules_dir(a):
        # The agent must neither read the rules (they name what is private) nor change them.
        check_host_source(a, privacy_rules_dir(a), "--privacy-rules")
    for value in a.copy:
        if ":" in value:
            check_host_source(a, value.rsplit(":", 1)[0], "--copy")
    if kits_apply(a):
        kits = load_kits_module()
        for label, path in kits.sources(a):
            check_host_source(a, path, f"Kit ({label})")
        if not a.no_default_kit:
            check_host_source(a, kits.default_home(a.agent), "Default kit")
        if not a.no_memory:
            check_host_source(a, kits.memory_store(a.workspaces[0], a.agent), "Project memory")
        if a.agent == "claude" and not a.no_plugins:
            check_host_source(a, kits.plugin_store("claude"), "Plugin store")
        # The other way round: a workspace, rw mount or --state-dir inside a store or kit would let the
        # agent change what every sandbox gets (or what its own next run reads back).
        stores = [(path, label) for path, label, used in [
            (kits.default_home(a.agent), "the default kit", not a.no_default_kit),
            (kits.kits_dir(), "the kits directory", bool(a.kit)),
            (kits.state_root() / "memory", "the project memory", not a.no_memory),
            (kits.plugin_store("claude"), "the plugin store", a.agent == "claude" and not a.no_plugins)] if used]
        for tree in exposed_trees(a):
            for path, label in stores:
                path = Path(os.path.normpath(path.expanduser().absolute()))
                if tree == path or path in tree.parents or tree == path.resolve() or path.resolve() in tree.parents:
                    raise ValueError(f"{tree} lies inside {label} ({path}), which the agent would then change "
                                     "for other sandboxes; choose another directory")
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
    result = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=45)
    answer = json.loads(result.stdout)
    if not isinstance(answer, dict) or set(answer) != set(hosts):
        raise ValueError("Podman DNS returned an incomplete host list")
    for host in hosts:
        if not isinstance(answer[host], list) or not answer[host]:
            raise ValueError(f"Podman DNS returned no addresses for {host}")
        answer[host] = sorted({str(ipaddress.ip_address(addr)) for addr in answer[host]})
    return answer


def firewall_rules(networks, addresses, log=False, dns=(), allow_all=False, tcp=()):
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
             " chain output { type filter hook output priority 0; policy drop;",
             '  oifname "lo" accept', "  ct state established,related accept",
             "  ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept"]
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
    return "\n".join(lines + [" }", "}", ""])


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
    return "auto"


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
        try:
            if claude_recover_writeback(home, pair):
                print("agent-sandbox: the Claude login refreshed in an earlier run of this HOME was never "
                      "written back; wrote it back now", file=sys.stderr)
        except (OSError, ValueError) as exc:
            print(f"agent-sandbox: could not write back the Claude login of an earlier run: {exc}", file=sys.stderr)
        # The source is the authority: its login replaces whatever a previous run left.
        sync_claude_credentials(*pair, sandbox="target", newer_only=False)
    if a.state_dir:
        # Recorded only while this HOME shares the source's login; any other run (--no-auth, another
        # account) forgets it, so a later start never takes that login for a missed write-back.
        remember_sync("claude", home, oauth_token_digest(claude_oauth(pair[0])) if pair else None)
        sync_store_login(a, home)
    if a.agent == "claude":
        claude_default_settings(home)
        claude_default_state(home)
    if a.agent == "opencode":
        catalog = opencode_catalog()
        target = home / ".cache/opencode/models.json"
        if catalog.is_file() and not target.exists():
            copy_private(catalog, home, ".cache/opencode/models.json")
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


def claude_default_settings(home):
    """Default Claude model and effort; existing settings always win."""
    def apply(document):
        changed = "model" not in document or "modelSettings" not in document
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


def claude_oauth(path, follow=True):
    """Validated claudeAiOauth block, or None.

    follow=False for sandbox-side files: no symlinks, regular files of bounded
    size only, so a planted FIFO, device or huge file cannot hang or exhaust us."""
    try:
        data = Path(path).read_bytes() if follow else read_config_bytes(path)
        oauth = json.loads(data)["claudeAiOauth"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
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
        document = json.loads(data)
        document = document if isinstance(document, dict) else {}
    except (OSError, ValueError, RecursionError):
        document = {}
    current = document.get("claudeAiOauth")
    current = current if isinstance(current, dict) else {}
    document["claudeAiOauth"] = {**oauth_fields(new, OAUTH_INFO_FIELDS), **oauth_fields(current, OAUTH_INFO_FIELDS),
                                 **oauth_fields(new, OAUTH_TOKEN_FIELDS)}
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=Path(target).parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


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
    """Write tokens Claude refreshed in the sandbox back to the host; returns a status line or None.

    Only a refresh of the login the host handed in counts: the host must have had
    credentials, must be unchanged since start, and the sandbox tokens must differ
    from what was copied in. A host-side login during the run always wins."""
    host, sandbox = pair
    check_home_path(home, sandbox)
    if before["host"] is None or not before.get("host_login"):
        return None  # Never create a host login from the sandbox, not even in an existing file.
    store = auth_store() in Path(host).parents
    where = "sandbox-ai's login store" if store else "the host"
    if oauth_token_digest(claude_oauth(host)) != before["host"]:
        return f"the Claude login {'in' if store else 'on'} {where} changed during the run; kept that one"
    current = claude_oauth(sandbox, follow=False)
    if current is None or oauth_fields(current, OAUTH_TOKEN_FIELDS) == before["tokens"]:
        return None
    if sync_claude_credentials(sandbox, host, sandbox="source"):
        remember_sync("claude", home, oauth_token_digest(claude_oauth(host)))
        return f"refreshed Claude credentials written back to {where}"
    return None


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
    removed HOMEs are dropped."""
    known = load_login_sync()
    record = {key: value for key, value in known.items() if Path(key.partition(":")[2]).is_dir()}
    if digest is None:
        if sync_key(agent, home) not in record and record == known:
            return  # nothing to forget: do not create or rewrite the file
        record.pop(sync_key(agent, home), None)
    else:
        record[sync_key(agent, home)] = digest
    store = login_sync_store()
    try:
        store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".login-sync-", dir=store.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, store)
    except OSError:
        pass  # only a recovery aid


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


def store_writeback_state(a, home):
    """Codex and OpenCode logins from the login store: what to compare at the end, to keep a refresh."""
    if a.agent == "claude" or a.probe or auth_mode(a) not in ("auto", "copy") or not store_login(a.agent):
        return None
    source = auth_store(a.agent) / AUTH_TARGETS[a.agent]
    before = {"source": source, "store": credential_digest(source), "sandbox": sandbox_digest(home, AUTH_TARGETS[a.agent])}
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
    relative, source = AUTH_TARGETS[a.agent], before["source"]
    if credential_digest(source) != before["store"]:
        return "the stored login changed during the run; kept the stored one"
    path = check_home_path(home, home / relative)
    stored = stored_document(a.agent, source)
    try:
        data = read_config_bytes(path)
        sandbox = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return None
    if stored is None or hashlib.sha256(data).hexdigest() == before["sandbox"]:
        return None
    merged = merge_refreshed(a.agent, stored, sandbox)
    if merged is None:
        return None
    content = json.dumps(merged, indent=2).encode()
    write_private_file(source, content)
    if a.state_dir:
        # A persistent HOME keeps exactly what the store holds, so it can follow later refreshes.
        replace_private(content, home, relative)
        remember_sync(a.agent, home, hashlib.sha256(content).hexdigest())
    return "refreshed login written back to sandbox-ai's login store"


def sync_store_login(a, home):
    """A persistent HOME with a Codex or OpenCode login copied from the store, unchanged since: take over
    the store's newer tokens (another sandbox refreshed them, which used up the HOME's)."""
    if a.agent == "claude" or a.probe or auth_mode(a) not in ("auto", "copy") or not store_login(a.agent):
        if a.agent != "claude":
            remember_sync(a.agent, home, None)
        return
    relative = AUTH_TARGETS[a.agent]
    store = auth_store(a.agent) / relative
    last, current, stored = (load_login_sync().get(sync_key(a.agent, home)), sandbox_digest(home, relative),
                             credential_digest(store))
    if last and current == last and stored != last:
        replace_private(read_config_bytes(store), home, relative)
        print("agent-sandbox: took over the newer stored login (refreshed by another sandbox)", file=sys.stderr)
        current = sandbox_digest(home, relative)
    remember_sync(a.agent, home, stored if current == stored else None)


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
    for credential, token in git_tokens(a):
        context["servers"].append({key: credential.get(key) for key in ("host", "port", "user")})
        context["tokens"].append(token)
    return context


def privacy_workspaces(a):
    """[(host path, project name)] of the workspaces the privacy check looks at: the writable ones."""
    if a.workspace_ro:
        return []
    paths = [Path(src).expanduser().resolve() for src, _ in workspace_targets(a)]
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
    return lambda: privacy.check_after_run(
        privacy_workspaces(a), rules.get, own_hosts, a.privacy_check,
        lambda line: print(f"agent-sandbox: privacy: {printable(line)}", file=sys.stderr))


def kits_apply(a):
    """Kits, project memory and plugins are for agent sandboxes, not for probes or MCP servers."""
    return not a.probe and not a.mcp_socket_dir


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
    result = {"sources": [{"kit": label, "path": str(path)} for label, path in kits.sources(a)],
              "entries": kits.summary(plan),
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
                     "sandbox-ai plugins claude")
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
        elif a.agent == "opencode" and plugins:
            a.opencode_plugins = plugins
            if not a.egress_all_allow and "registry.npmjs.org" not in allowed_hosts(a):
                notes.append("OpenCode installs its plugins from npm: allow it with --allow-host registry.npmjs.org")
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
    if a.agent != "claude" or auth_mode(a) not in ("auto", "copy"):
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
    env = {key: os.environ[key] for key in a.env}
    env.update(git_environment(a))
    if a.local_config:
        env.update({"OPENCODE_CONFIG": "/root/.config/opencode/opencode.jsonc",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1"})
    if a.dangerously_skip_permissions:
        env.update(DANGER_ENV[a.agent])
    if a.tmux_socket_dir:
        env["TMUX_TMPDIR"] = TMUX_DIR
    content = {}
    if a.agent == "opencode" and a.mcp and a.binary is None:
        content["mcp"] = {name: {"type": "local", "command": ["python3", *mcp_bridge_args(name)], "enabled": True}
                          for name in dict.fromkeys(a.mcp)}
    if a.agent == "opencode" and getattr(a, "opencode_plugins", None) and a.binary is None:
        content["plugin"] = list(a.opencode_plugins)
    if content:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content)
    if a.gui:
        env["DISPLAY"] = GUI_DISPLAY
    if a.gui_wayland:
        env.update({"WAYLAND_DISPLAY": PurePosixPath(WAYLAND_TARGET).name,
                    "XDG_RUNTIME_DIR": str(PurePosixPath(WAYLAND_TARGET).parent),
                    "MOZ_ENABLE_WAYLAND": "1", "QT_QPA_PLATFORM": "wayland;xcb",
                    "ELECTRON_OZONE_PLATFORM_HINT": "auto"})
    return env


def agent_command(a, binary):
    args = list(a.agent_args)
    if a.dangerously_skip_permissions and not a.probe:
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
    return [binary, *args]


def mount_option(src, dest, mode):
    if any(c in str(src) + dest for c in (",", "\n", "\r")):
        raise ValueError("Podman bind paths cannot contain commas or newlines")
    return ["--mount", f"type=bind,src={src},dst={dest},{'ro' if mode == 'ro' else 'rw'}"]


def pod_commands(a, name, session, home):
    create = podman_command(a, "pod", "create", "--name", name,
              "--infra-image", a.image, "--infra-command", "/usr/bin/sleep infinity",
              "--userns=keep-id:uid=0,gid=0", "--share=net,ipc,uts",
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
           "--tmpfs=/run:rw,nosuid,nodev", "--tmpfs=/tmp:rw,nosuid,nodev",
           "--tmpfs=/var/tmp:rw,nosuid,nodev", "--workdir=/work", "--interactive")
    # The bootstrap's DNS logging stub binds port 53; Firejail drops it again.
    if a.pod_name:
        # Named so that `sandbox-session attach` can exec into it.
        run += ["--name", f"{name}-agent"]
    for cap in CAPS + (["NET_BIND_SERVICE"] if a.log_dir else []):
        run += ["--cap-add", cap]
    if sys.stdin.isatty() and sys.stdout.isatty():
        run += ["--tty"]
    bindings = [(HERE / "runtime.py", "/sandbox/runtime.py", "ro"),
                (session, "/sandbox/session", "ro"), (home, "/root", "rw"),
                *((Path(src).expanduser().resolve(), dest, "ro" if a.workspace_ro else "rw")
                  for src, dest in workspace_targets(a)),
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
    for name in dict.fromkeys(a.mcp):
        bindings.append((mcp_socket_dir(name), f"{MCP_AGENT_DIR}/{name}", "ro"))
    if a.gui_wayland:
        bindings.append((wayland_socket(), WAYLAND_TARGET, "rw"))
    for src, dest, mode in bindings:
        run += mount_option(src, dest, mode)
    for key, value in base_env().items():
        run += ["--env", f"{key}={value}"]
    run += ["--entrypoint=/usr/bin/python3", a.image, "-I", "/sandbox/runtime.py", "--internal-bootstrap"]
    return create, run


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


def run_pod(a, session, home):
    name = a.pod_name or "agent-" + a.agent + "-" + uuid.uuid4().hex[:12]
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
            subprocess.run(create, check=True, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            owned = False
            raise
        return wait_child(subprocess.Popen(run))
    finally:
        if owned:
            subprocess.run(podman_command(a, "pod", "rm", "--force", name), check=False, stdout=subprocess.DEVNULL)


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
            "--dir", "/var/tmp", "--dir", "/etc"]
    if a.gui or a.gui_wayland:
        cmd += ["--tmpfs", "/dev/shm"]
    for path in ("/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/firejail", "/etc/localtime", "/etc/ld.so.cache", "/etc/login.defs"):
        if Path(path).exists():
            cmd += ["--ro-bind", path, path]
    for name in ("hosts", "resolv.conf", "nsswitch.conf", "passwd", "group"):
        cmd += ["--ro-bind", str(session / name), "/etc/" + name]
    cmd += ["--ro-bind", str(HERE / "runtime.py"), "/sandbox/runtime.py",
            "--ro-bind", str(session), "/sandbox/session", "--bind", str(home), "/root"]
    for src, dest in workspace_targets(a):
        cmd += ["--ro-bind" if a.workspace_ro else "--bind", str(Path(src).expanduser().resolve()), dest]
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
    for name in dict.fromkeys(a.mcp):
        cmd += ["--ro-bind", str(mcp_socket_dir(name)), f"{MCP_AGENT_DIR}/{name}"]
    if a.gui_wayland:
        runtime = str(PurePosixPath(WAYLAND_TARGET).parent)
        cmd += ["--perms", "0700", "--dir", runtime, "--bind", str(wayland_socket()), WAYLAND_TARGET]
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
        child = subprocess.Popen(cmd + ["--info-fd", str(info_w), "--userns-block-fd", str(maps_r), "--block-fd", str(gate_r),
                                       "/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-bootstrap"],
                                 pass_fds=(info_w, maps_r, gate_r))
        os.close(info_w)
        info_w = -1
        os.close(gate_r)
        gate_r = -1
        os.close(maps_r)
        maps_r = -1
        pid = int(read_ready(info_r, child)["child-pid"])
        configure_user_maps(pid)
        os.write(maps_w, b"1")
        os.close(maps_w)
        maps_w = -1
        if not a.offline:
            slirp = subprocess.Popen(["slirp4netns", "--configure", "--disable-host-loopback",
                                      "--enable-sandbox", "--enable-seccomp", "--enable-ipv6",
                                      "--ready-fd", str(ready_w), str(pid), "tap0"], pass_fds=(ready_w,),
                                     stdout=subprocess.DEVNULL)
            os.close(ready_w)
            ready_w = -1
            read_ready(ready_r, slirp)
        os.write(gate_w, b"1")
        os.close(gate_w)
        gate_w = -1
        return wait_child(child)
    finally:
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


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("sandbox_mcp", HERE / "mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def terminate(signum, _frame):
    # SIGTERM/SIGHUP become a normal exit, so every cleanup in a finally block
    # runs (pod removal, temporary state, Claude write-back), even during startup.
    raise SystemExit(128 + signum)


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


def resolve_tmux(a):
    """--tmux/--no-tmux as given; by default tmux for every interactive start from a terminal, but not
    for probes, sessions (tmux already), MCP servers, other binaries, commands that print and exit, or
    bwrap without a host tmux."""
    if a.tmux is None:
        a.tmux = bool(not (a.probe or a.session is not None or a.tmux_socket_dir or a.mcp_socket_dir
                           or a.binary) and sys.stdin.isatty() and sys.stdout.isatty()
                      and interactive_args(a.agent, a.agent_args) and (a.backend == "podman" or shutil.which("tmux")))
    return a.tmux


def main(argv):
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return launch(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def launch(argv):
    a = parse_args(argv)
    config_path, document = load_project_config(a)
    if document:
        a = parse_args(merge_config(argv, a, document))
    # The checked document is passed on; nothing re-reads the file later.
    a.project_config, a.project_document = (str(config_path) if config_path else None), document
    validate(a)
    resolve_tmux(a)
    check_login(a)
    for note in git_credentials_file(a)[1]:
        print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
    if a.session is not None and not a.dry_run:
        return load_sessions_module().start(a, argv)
    if a.dry_run:
        print(json.dumps({"agent": a.agent, "backend": a.backend, "workspace": str(Path(a.workspace).resolve()),
                          "workspaces": [{"host": src, "sandbox": dest} for src, dest in workspace_targets(a)],
                          "workspace_mode": "ro" if a.workspace_ro else "rw", "mounts": a.mount,
                          "allow": a.allow, "https_hosts": allowed_hosts(a),
                          "https_networks": host_networks(allowed_hosts(a)), "offline": a.offline,
                          "dangerously_skip_permissions": a.dangerously_skip_permissions,
                          "egress_all_allow": a.egress_all_allow, "nameservers": nameservers(a),
                          "log_dir": str(a.log_dir.expanduser().resolve()) if a.log_dir else None,
                          "gui": gui_config(a), "gui_wayland": a.gui_wayland, "tmux": a.tmux, "session": a.session,
                          "allow_tcp": [f"{addr}:{port}" for addr, port in tcp_targets(a)],
                          "host_ports": host_ports(a),
                          "network_profiles": a.profile,
                          "git_identity": a.git_identity,
                          "privacy_check": a.privacy_check,
                          "privacy_rules": str(privacy_rules_dir(a)) if privacy_rules_dir(a) else None,
                          "git_credentials": [{key: c[key] for key in ("url", "user", "env", "file", "credentials",
                                                                       "line") if key in c}
                                              for c, _ in git_tokens(a)],
                          "mcp": [{"name": name, "socket": str(mcp_socket_dir(name) / "server.sock")}
                                  for name in dict.fromkeys(a.mcp)],
                          "project_config": a.project_config,
                          "kits": kit_plan(a),
                          "network": a.network, "ip": a.ip, "ip6": a.ip6, "image": a.image,
                          "podman_url": a.podman_url,
                          "local_config": str(a.local_config) if a.local_config else None,
                          "copy_auth": a.copy_auth, "auth_file": str(a.auth_file) if a.auth_file else None,
                          "auth_mode": auth_mode(a), "auth_copies": auth_plan(a),
                          "import_codex_auth": str(a.import_codex_auth) if a.import_codex_auth else None,
                          "copies": a.copy, "env_names": a.env, "agent_args": a.agent_args,
                          "firejail_required": True}, indent=2))
        return 0
    if os.geteuid() == 0:
        raise ValueError("Run as an ordinary host user; both backends use a rootless user namespace")
    required = ["podman"] if a.backend == "podman" else ["bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap"] + ([] if a.offline else ["slirp4netns"])
    if a.tmux and not a.probe and not a.tmux_socket_dir:
        if not sys.stdin.isatty():
            raise ValueError("--tmux needs an interactive terminal")
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
    for workspace in a.workspaces:
        prepare_workspace(workspace)
    for private in (a.log_dir, a.gui_vnc, a.tmux_socket_dir, a.mcp_socket_dir):
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
    hosts = resolve_pod_hosts(a, allowed_hosts(a)) if a.backend == "podman" else resolve_hosts(allowed_hosts(a))
    with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as tmp:
        session = Path(tmp)
        home = a.state_dir.expanduser().absolute() if a.state_dir else session / "home"
        if home.is_symlink():
            raise ValueError("--state-dir must not be a symlink")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if home.stat().st_uid != os.getuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
            raise ValueError("--state-dir must be owned by you and have mode 0700")
        for source, dest in prepare_home(a, home):
            print(f"agent-sandbox: auth copied: {Path(source).expanduser()} -> /root/{dest}", file=sys.stderr)
        a.kit_mounts, notes, memory = prepare_kits(a, home, session / "kits")
        for note in notes:
            print(f"agent-sandbox: {printable(note)}", file=sys.stderr)
        pair = claude_credential_pair(a, home)
        writeback = claude_writeback_state(pair) if pair and not a.probe else None
        stored = store_writeback_state(a, home)
        if a.agent == "claude" and auth_mode(a) != "none" and (home / AUTH_TARGETS["claude"]).exists():
            print(f"agent-sandbox: {claude_token_status(home / AUTH_TARGETS['claude'])}", file=sys.stderr)
        for port in host_ports(a):
            host = "the Podman machine at --podman-url" if a.podman_url else "the host"
            print(f"agent-sandbox: 127.0.0.1:{port} in the sandbox reaches port {port} on the loopback of {host}; "
                  "the sandbox cannot listen on that port itself", file=sys.stderr)
        cfg = {"command": agent_command(a, binary), "env": agent_environment(a), "probe": a.probe,
               "tmux": "detached" if a.tmux_socket_dir else a.tmux,
               "pinned": list(hosts), "watch": [*(dest for _, dest in workspace_targets(a)), "/root", "/tmp",
                         *(parse_mount(m)[1] for m in a.mount)]}
        resolv, nsswitch = resolver_files(a)
        files = {"config.json": json.dumps(cfg), "hosts": hosts_file(hosts),
                 "resolv.conf": resolv, "nsswitch.conf": nsswitch,
                 "passwd": "root:x:0:0:Sandbox:/root:/bin/bash\n", "group": "root:x:0:\n",
                 "firewall.nft": firewall_rules(a.allow, [addr for values in hosts.values() for addr in values]
                                                + host_networks(hosts), log=bool(a.log_dir),
                                                dns=nameservers(a), allow_all=a.egress_all_allow,
                                                tcp=tcp_targets(a))}
        if gui_config(a):
            files["gui.json"] = json.dumps(gui_config(a))
        for name, content in files.items():
            path = session / name
            path.write_text(content)
            path.chmod(0o600 if name == "config.json" else 0o644)
        privacy_after_run = prepare_privacy(a, session)
        try:
            return run_pod(a, session, home) if a.backend == "podman" else run_bwrap(a, session, home)
        finally:
            try:
                message = claude_writeback(home, pair, writeback) if writeback else None
                if message:
                    print(f"agent-sandbox: {message}", file=sys.stderr)
                message = store_writeback(home, a, stored) if stored else None
                if message:
                    print(f"agent-sandbox: {message}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - whatever the agent left, keep its exit status
                # The agent's own exit status must survive a failed write-back.
                print(f"agent-sandbox: could not write back Claude credentials: {exc}", file=sys.stderr)
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


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
        # A child killed by signal N reports -N; exit like a shell would (128 + N).
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
